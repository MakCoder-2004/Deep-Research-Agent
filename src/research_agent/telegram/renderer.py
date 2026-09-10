"""Telegram MarkdownV2 rendering helpers (M2.22+).

PLAN section 15 delivery: concise reports must escape Telegram MarkdownV2
correctly while keeping ASCII ``[1]`` citation markers readable in both
English (LTR) and Arabic (RTL) text.
"""

from __future__ import annotations

import re

TELEGRAM_TEXT_LIMIT = 4096

_SENTENCE_RE = re.compile(r"(?<=[.!?؟…])\s+")

_MARKDOWN_V2_SPECIALS: frozenset[str] = frozenset(
    {"_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"}
)


def escape_markdown_v2(text: str) -> str:
    """Escape text for Telegram MarkdownV2.

    Every ``_ * [ ] ( ) ~ ` > # + - = | { } . !`` character is prefixed with
    a backslash. A lone backslash is escaped as ``\\\\``. Sequences that are
    already escaped (``\\\\`` + special) are preserved as-is so markers and
    previously escaped content are not double-escaped.
    """
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < length else ""
            if nxt in _MARKDOWN_V2_SPECIALS or nxt == "\\":
                out.append("\\")
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        if ch in _MARKDOWN_V2_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def render_citation_marker(n: int) -> str:
    """Return an ASCII citation marker ``[n]`` readable in LTR and RTL text."""
    if n < 1:
        raise ValueError("Citation marker index must be >= 1.")
    return f"[{n}]"


def _adjust_hard_cut(text: str, cut: int) -> int:
    """Adjust a hard cut to avoid splitting escapes, markers, or CRLF."""
    length = len(text)
    if cut <= 0 or cut >= length:
        return cut
    # Keep CRLF together: never split between \r and \n.
    if text[cut - 1] == "\r" and text[cut] == "\n":
        cut -= 1
        if cut <= 0:
            return cut
    # Never end a chunk with a dangling escape backslash.
    chunk = text[:cut]
    trailing = len(chunk) - len(chunk.rstrip("\\"))
    if trailing % 2 == 1:
        cut -= 1
        if cut <= 0:
            return cut
    # Never split inside an ASCII [digits] citation marker.
    open_idx = text.rfind("[", 0, cut)
    if open_idx != -1 and open_idx < cut:
        close_idx = text.find("]", open_idx + 1, cut + 8)
        if close_idx != -1 and open_idx < cut <= close_idx:
            inner = text[open_idx + 1 : close_idx]
            if inner.isdigit() and len(inner) <= 6:
                if open_idx == 0:
                    # Marker longer than the budget: keep the limit so the
                    # chunk stays within bounds rather than emitting empty.
                    return cut
                return open_idx
    return cut


def _find_cut_position(remaining: str, effective: int) -> int:
    """Find the best cut index within ``effective`` preserving boundaries."""
    if len(remaining) <= effective:
        return len(remaining)
    pos = remaining.rfind("\n\n", 0, effective)
    if pos != -1:
        cut = pos + 2
        if 0 < cut < len(remaining):
            return cut
    pos = remaining.rfind("\n", 0, effective)
    if pos != -1:
        cut = pos + 1
        if 0 < cut < len(remaining):
            return cut
    last_end = -1
    for match in _SENTENCE_RE.finditer(remaining[:effective]):
        last_end = match.end()
    if last_end > 0 and last_end < len(remaining):
        return last_end
    pos = remaining.rfind(" ", 0, effective)
    if pos != -1:
        cut = pos + 1
        if 0 < cut < len(remaining):
            return cut
    return _adjust_hard_cut(remaining, effective)


def _split_without_headers(text: str, effective: int) -> list[str]:
    """Split text into chunks each within ``effective`` preserving content."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= effective:
            chunks.append(remaining)
            break
        cut = _find_cut_position(remaining, effective)
        if cut <= 0 or cut >= len(remaining):
            cut = min(effective, len(remaining))
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
        if len(chunks) > 10000:
            raise ValueError("Text too fragmented to split safely.")
    return chunks


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split oversized text on paragraph-safe boundaries.

    Priority is ``\\n\\n`` then ``\\n`` then sentence boundaries then spaces,
    falling back to a hard cut that never splits mid-code-point (Python
    slicing is code-point safe), inside ``[n]`` markers, on a dangling
    escape backslash, or inside CRLF. When more than one chunk is needed,
    each chunk is prefixed with a ``(i/n)`` header and the header length is
    counted inside ``limit`` so every returned chunk satisfies
    ``len(chunk) <= limit``.
    """
    if limit < 10:
        raise ValueError("Split limit must be >= 10.")
    if len(text) <= limit:
        return [text]
    total_guess = max(2, len(text) // max(1, limit - 10) + 1)
    raw: list[str] = []
    total = total_guess
    for _ in range(10):
        header_len = len(f"({total_guess}/{total_guess}) ")
        effective = max(1, limit - header_len)
        raw = _split_without_headers(text, effective)
        total = len(raw)
        if total == total_guess:
            break
        total_guess = total
    else:
        header_len = len(f"({total_guess}/{total_guess}) ")
        effective = max(1, limit - header_len)
        raw = _split_without_headers(text, effective)
        total = len(raw)
    return [f"({i}/{total}) {chunk}" for i, chunk in enumerate(raw, start=1)]
