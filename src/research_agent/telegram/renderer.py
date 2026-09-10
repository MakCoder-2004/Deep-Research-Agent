"""Telegram MarkdownV2 rendering helpers (M2.22+).

PLAN section 15 delivery: concise reports must escape Telegram MarkdownV2
correctly while keeping ASCII ``[1]`` citation markers readable in both
English (LTR) and Arabic (RTL) text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

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


def _normalize_report_lang(lang: str | None) -> str:
    """Normalize a language code to ``ar`` or ``en``."""
    if lang and lang.lower().startswith("ar"):
        return "ar"
    return "en"


def _finding_statement(item: Any) -> str:
    """Extract a finding statement from a model, mapping, or row."""
    try:
        val = getattr(item, "statement", None)
        if val is not None:
            return str(val)
    except Exception:  # noqa: BLE001, S110 - fall back to mapping access
        pass
    try:
        return str(item["statement"])
    except Exception:  # noqa: BLE001, S110 - last resort stringifies the item
        return str(item)


def _finding_citations(item: Any) -> list[int]:
    """Extract citation ids preserving order and dropping invalid entries."""
    raw: Any = None
    try:
        raw = getattr(item, "citation_ids", None)
        if raw is None:
            raise AttributeError("no citation_ids attr")
    except Exception:  # noqa: BLE001, S110 - try mapping access next
        try:
            raw = item["citation_ids"]
        except Exception:  # noqa: BLE001, S110 - no citations available
            return []
    ids: list[int] = []
    try:
        candidates = list(raw)
    except TypeError:
        return []
    for cand in candidates:
        try:
            num = int(cand)
        except (TypeError, ValueError):
            continue
        if num >= 1 and num not in ids:
            ids.append(num)
    return ids


def _source_field(item: Any, *names: str, default: str = "") -> str:
    """Extract a string field trying attributes then mapping keys."""
    for name in names:
        try:
            val = getattr(item, name, None)
            if val is not None and str(val) != "":
                return str(val)
        except Exception:  # noqa: BLE001, S112 - try next source
            continue
    for name in names:
        try:
            val = item[name]
            if val is not None and str(val) != "":
                return str(val)
        except Exception:  # noqa: BLE001, S112 - try next key
            continue
    return default


def _source_id(item: Any, fallback: int) -> int:
    """Extract a numeric source id, falling back to sequential position."""
    for name in ("id", "source_ref", "source_id", "ref"):
        try:
            val = getattr(item, name, None)
            if val is not None:
                num = int(val)
                if num >= 1:
                    return num
        except (TypeError, ValueError, AttributeError):
            continue
        try:
            val = item[name]
            if val is not None:
                num = int(val)
                if num >= 1:
                    return num
        except Exception:  # noqa: BLE001, S112 - try next key
            continue
    return fallback


def _escape_url_for_link(url: str) -> str:
    """Escape a URL for the ``(url)`` part of a MarkdownV2 inline link."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def render_concise_report(
    topic: str,
    findings: Sequence[Any],
    sources: Sequence[Any],
    lang: str = "en",
    partial: bool = False,
    disclaimer: str | None = None,
    tools_used: Sequence[str] | None = None,
) -> str:
    """Render a concise in-chat report for Telegram MarkdownV2.

    All user/model text (topic, statements, titles, tools, disclaimer) is
    escaped with :func:`escape_markdown_v2`. ASCII ``[n]`` citation markers
    are inserted after escaping so they stay plain and readable in both LTR
    English and RTL Arabic. Formatting asterisks for the bold topic are the
    only unescaped MarkdownV2 controls added by this function.
    """
    normalized = _normalize_report_lang(lang)
    esc_topic = escape_markdown_v2(topic)
    lines: list[str] = []
    if partial:
        if normalized == "ar":
            lines.append("⚠️ تنبيه — هذا تقرير جزئي وقد تكون تغطيته غير مكتملة")
        else:
            lines.append("⚠️ Partial report — coverage may be incomplete")
        lines.append("")
    lines.append(f"*{esc_topic}*")
    lines.append("")
    lines.append("أبرز النتائج:" if normalized == "ar" else "Key findings:")
    for item in list(findings):
        stmt = escape_markdown_v2(_finding_statement(item))
        cids = _finding_citations(item)
        markers = " ".join(render_citation_marker(c) for c in cids)
        if markers:
            lines.append(f"• {stmt} {markers}")
        else:
            lines.append(f"• {stmt}")
    lines.append("")
    lines.append("المصادر:" if normalized == "ar" else "Sources:")
    for idx, src in enumerate(list(sources), start=1):
        marker = _source_id(src, idx)
        title_raw = _source_field(src, "title", default="")
        url_raw = _source_field(src, "url", default="")
        if title_raw:
            esc_title = escape_markdown_v2(title_raw)
        else:
            esc_title = escape_markdown_v2(url_raw or f"source {marker}")
        if url_raw:
            safe_url = _escape_url_for_link(url_raw)
            lines.append(f"[{marker}] [{esc_title}]({safe_url})")
        else:
            lines.append(f"[{marker}] {esc_title}")
    if tools_used:
        esc_tools = ", ".join(escape_markdown_v2(str(tool)) for tool in list(tools_used))
        lines.append("")
        lines.append(f"الأدوات: {esc_tools}" if normalized == "ar" else f"Tools: {esc_tools}")
    if disclaimer:
        lines.append("")
        lines.append(escape_markdown_v2(disclaimer))
    return "\n".join(lines)
