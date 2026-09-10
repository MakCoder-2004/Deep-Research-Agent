"""Telegram MarkdownV2 rendering helpers (M2.22+).

PLAN section 15 delivery: concise reports must escape Telegram MarkdownV2
correctly while keeping ASCII ``[1]`` citation markers readable in both
English (LTR) and Arabic (RTL) text.
"""

from __future__ import annotations

TELEGRAM_TEXT_LIMIT = 4096

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
