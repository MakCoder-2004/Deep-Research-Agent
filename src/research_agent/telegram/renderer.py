"""Telegram MarkdownV2 rendering helpers (M2.22+).

PLAN section 15 delivery: concise reports must escape Telegram MarkdownV2
correctly while keeping ASCII ``[1]`` citation markers readable in both
English (LTR) and Arabic (RTL) text.

PLAN section 14/25: no report is delivered before Pydantic validation.
Every finding must carry at least one citation that maps to a listed
source. Use :func:`validate_before_render` (called automatically by
:func:`render_concise_report`) and never render unvalidated payloads.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from aiogram.types import BufferedInputFile, Message

from research_agent.models.reports import Finding, ResearchReport, Source

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
_RESEARCH_FILENAME_RE = re.compile(r"^research-[A-Za-z0-9-]+\.md$")

_SENTENCE_RE = re.compile(r"(?<=[.!?؟…])\s+")

_MARKDOWN_V2_SPECIALS: frozenset[str] = frozenset(
    {"_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"}
)


class FindingDict(TypedDict, total=False):
    """Typed shape for plain finding payloads (dict/JSON rows)."""

    statement: str
    citation_ids: list[int]


class SourceDict(TypedDict, total=False):
    """Typed shape for plain source payloads (dict/JSON/DB rows)."""

    id: int
    source_ref: int
    source_id: int
    ref: int
    title: str
    url: str
    publisher: str | None
    published_at: str | None
    accessed_at: str | None
    source_type: str


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


def _get_field(item: object, key: str) -> Any | None:
    """Read one field from a model, mapping, or DB row without chaining.

    Centralizes all dynamic access so rendering code works off validated
    Pydantic models instead of ``Any`` attribute/mapping chains. Returns
    ``None`` when the field is absent.
    """
    if isinstance(item, Mapping):
        try:
            return cast(Mapping[str, Any], item).get(key)
        except Exception:  # noqa: BLE001, S110 - fall through to other shapes
            pass
    try:
        subscript = cast(Any, item).__getitem__
    except AttributeError:
        pass
    else:
        try:
            return cast(Any, subscript(key))
        except Exception:  # noqa: BLE001, S110 - not a mapping-like row
            pass
    try:
        return cast(Any, getattr(item, key, None))
    except Exception:  # noqa: BLE001, S110 - unreadable attribute
        return None


def _coerce_citation_ids(raw: Any | None) -> list[int]:
    """Normalize raw citation ids preserving order, dropping invalid ones."""
    if raw is None:
        return []
    try:
        candidates = list(cast(Any, raw))
    except TypeError:
        return []
    ids: list[int] = []
    for cand in candidates:
        try:
            num = int(cast(Any, cand))
        except (TypeError, ValueError):
            continue
        if num >= 1 and num not in ids:
            ids.append(num)
    return ids


def coerce_finding(item: object) -> Finding:
    """Coerce a plain payload into a validated :class:`Finding`.

    Accepts a :class:`Finding`, a mapping with ``statement`` /
    ``citation_ids``, or an attribute object. Raises :class:`ValueError`
    when the statement is empty or when no citation is present, enforcing
    "every finding needs citations" before any delivery.
    """
    if isinstance(item, Finding):
        if not item.citation_ids:
            raise ValueError("Every finding must have at least one citation.")
        return item
    statement_raw = _get_field(item, "statement")
    statement = str(statement_raw).strip() if statement_raw is not None else ""
    if not statement:
        # Last resort mirrors legacy behavior for raw strings, but empty
        # statements are never deliverable.
        if isinstance(item, str) and item.strip():
            statement = item.strip()
        else:
            raise ValueError("Finding statement must be non-empty.")
    citation_ids = _coerce_citation_ids(_get_field(item, "citation_ids"))
    if not citation_ids:
        raise ValueError("Every finding must have at least one citation.")
    return Finding(statement=statement, citation_ids=citation_ids)


def coerce_source(item: object, fallback_id: int) -> Source:
    """Coerce a plain payload into a validated :class:`Source`.

    Accepts a :class:`Source`, a mapping, an ``aiosqlite.Row``-like object,
    or an attribute object. ``fallback_id`` supplies the sequential position
    when no explicit ``id`` / ``source_ref`` / ``source_id`` / ``ref`` is
    present. Missing ``accessed_at`` defaults to now (UTC). Raises
    :class:`ValueError` when ``url`` is absent so unvalidated sources are
    never delivered.
    """
    if isinstance(item, Source):
        return item
    source_id = fallback_id
    for key in ("id", "source_ref", "source_id", "ref"):
        raw_id = _get_field(item, key)
        if raw_id is None:
            continue
        try:
            num = int(cast(Any, raw_id))
        except (TypeError, ValueError):
            continue
        if num >= 1:
            source_id = num
            break
    title_raw = _get_field(item, "title")
    url_raw = _get_field(item, "url")
    url_text = str(url_raw).strip() if url_raw is not None else ""
    if not url_text:
        raise ValueError(f"Source {source_id} must include a URL.")
    title_text = str(title_raw).strip() if title_raw is not None and str(title_raw).strip() else ""
    if not title_text:
        title_text = url_text
    accessed_raw = _get_field(item, "accessed_at")
    if accessed_raw is None:
        accessed_at: datetime | str = datetime.now(UTC)
    elif isinstance(accessed_raw, datetime):
        accessed_at = accessed_raw
    else:
        accessed_at = str(accessed_raw)
    publisher_raw = _get_field(item, "publisher")
    publisher = str(publisher_raw) if publisher_raw is not None else None
    published_raw = _get_field(item, "published_at")
    published_at: datetime | str | None = None
    if published_raw is not None:
        published_at = published_raw if isinstance(published_raw, datetime) else str(published_raw)
    source_type_raw = _get_field(item, "source_type")
    source_type = str(source_type_raw) if source_type_raw is not None else "web"
    return Source(
        id=source_id,
        title=title_text,
        url=cast(Any, url_text),
        publisher=publisher,
        published_at=cast(Any, published_at),
        accessed_at=cast(Any, accessed_at),
        source_type=cast(Any, source_type),
    )


def validate_before_render(
    topic: str,
    findings: Sequence[object],
    sources: Sequence[object],
    tools_used: Sequence[str] | None = None,
) -> ResearchReport:
    """Validate report payloads and return a :class:`ResearchReport`.

    Coerces plain findings/sources via :func:`coerce_finding` /
    :func:`coerce_source`, then constructs :class:`ResearchReport` so
    citation mapping, source ordering, URL uniqueness, and schema rules are
    enforced. Raises on any validation failure; callers must never deliver
    when this raises.
    """
    clean_topic = topic.strip()
    if not clean_topic:
        raise ValueError("Report topic must be non-empty.")
    finding_list = list(findings)
    source_list = list(sources)
    if not finding_list:
        raise ValueError("Report must include at least one finding.")
    if not source_list:
        raise ValueError("Report must include at least one source.")
    finding_models = [coerce_finding(item) for item in finding_list]
    source_models = [coerce_source(item, idx) for idx, item in enumerate(source_list, start=1)]
    summary = "\n".join(finding.statement for finding in finding_models).strip()
    if not summary:
        raise ValueError("Report summary must be non-empty.")
    tools = [str(tool) for tool in list(tools_used)] if tools_used else []
    return ResearchReport(
        topic=clean_topic,
        key_findings=finding_models,
        summary=summary,
        sources=source_models,
        tools_used=tools,
    )


def _escape_url_for_link(url: str) -> str:
    """Escape a URL for the ``(url)`` part of a MarkdownV2 inline link."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def render_concise_report(
    topic: str,
    findings: Sequence[object],
    sources: Sequence[object],
    lang: str = "en",
    partial: bool = False,
    disclaimer: str | None = None,
    tools_used: Sequence[str] | None = None,
    tools_failed: Sequence[str] | None = None,
) -> str:
    """Render a concise in-chat report for Telegram MarkdownV2.

    Validates via :func:`validate_before_render` first so unvalidated
    payloads raise instead of being delivered. Every finding must carry at
    least one citation marker mapping to a listed source; otherwise
    :class:`ValueError` is raised.

    All user/model text (topic, statements, titles, tools, disclaimer) is
    escaped with :func:`escape_markdown_v2`. ASCII ``[n]`` citation markers
    are inserted after escaping so they stay plain and readable in both LTR
    English and RTL Arabic. Formatting asterisks for the bold topic are the
    only unescaped MarkdownV2 controls added by this function.

    ``partial`` adds a partial-coverage banner. ``tools_failed`` lists only
    failed tools that affected coverage (callers must filter to
    coverage-affecting failures); it renders as a separate escaped line.
    """
    validated = validate_before_render(topic, findings, sources, tools_used)
    failed = [str(tool) for tool in list(tools_failed)] if tools_failed else []
    normalized = _normalize_report_lang(lang)
    esc_topic = escape_markdown_v2(validated.topic)
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
    for finding in validated.key_findings:
        stmt = escape_markdown_v2(finding.statement)
        markers = " ".join(render_citation_marker(c) for c in finding.citation_ids)
        lines.append(f"• {stmt} {markers}")
    lines.append("")
    lines.append("المصادر:" if normalized == "ar" else "Sources:")
    for src in validated.sources:
        marker = src.id
        title_raw = src.title
        url_raw = str(src.url)
        esc_title = escape_markdown_v2(title_raw) if title_raw else escape_markdown_v2(url_raw)
        safe_url = _escape_url_for_link(url_raw)
        lines.append(f"[{marker}] [{esc_title}]({safe_url})")
    if validated.tools_used:
        esc_tools = ", ".join(escape_markdown_v2(str(tool)) for tool in validated.tools_used)
        lines.append("")
        lines.append(f"الأدوات: {esc_tools}" if normalized == "ar" else f"Tools: {esc_tools}")
    if failed:
        esc_failed = ", ".join(escape_markdown_v2(tool) for tool in failed)
        lines.append("")
        if normalized == "ar":
            lines.append(f"الأدوات الفاشلة (أثّرت على التغطية): {esc_failed}")
        else:
            lines.append(f"Failed tools (coverage affected): {esc_failed}")
    if disclaimer:
        lines.append("")
        lines.append(escape_markdown_v2(disclaimer))
    return "\n".join(lines)


def build_concise_from_report(
    report: ResearchReport,
    lang: str = "en",
    partial: bool = False,
    disclaimer: str | None = None,
    tools_failed: Sequence[str] | None = None,
) -> str:
    """Render concise MarkdownV2 text from a validated :class:`ResearchReport`.

    The report is already Pydantic-validated; rendering reuses
    :func:`render_concise_report` so escaping, partial banners, disclaimers,
    and coverage-affecting failed-tool lines stay consistent.
    """
    return render_concise_report(
        report.topic,
        list(report.key_findings),
        list(report.sources),
        lang,
        partial=partial,
        disclaimer=disclaimer,
        tools_used=list(report.tools_used),
        tools_failed=tools_failed,
    )


def render_safe_fallback(topic: str, summary: str, lang: str = "en") -> str:
    """Render an escaped MarkdownV2 fallback when validation fails.

    Never returns raw unescaped content and never raises: all text is passed
    through :func:`escape_markdown_v2`. Use this instead of sending a raw
    plain-text bundle on render exceptions so Telegram parsing stays safe.
    """
    try:
        normalized = _normalize_report_lang(lang)
        esc_topic = escape_markdown_v2(topic.strip() or "report")
        esc_summary = escape_markdown_v2(summary.strip() or "")
        if normalized == "ar":
            lines = [
                f"*{esc_topic}*",
                "",
                "تعذّر عرض التقرير المفصّل، إليك الملخص الآمن:",
                "",
                esc_summary or "لا يتوفر ملخص.",
            ]
        else:
            lines = [
                f"*{esc_topic}*",
                "",
                "Detailed report unavailable, safe summary:",
                "",
                esc_summary or "No summary available.",
            ]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - fallback must never raise
        return "Report unavailable\\."


def report_filename(report_id: str) -> str:
    """Return the attachment filename ``research-<report-id>.md``.

    Only ``[A-Za-z0-9-]`` is accepted so ``..``, ``/``, and ``\\\\`` traversal
    payloads are rejected with :class:`ValueError` before any filesystem use.
    """
    if not _REPORT_ID_RE.fullmatch(report_id):
        raise ValueError(f"Invalid report id {report_id!r}: must match [A-Za-z0-9-]+.")
    return f"research-{report_id}.md"


def _is_safe_markdown_path(path: Path) -> bool:
    """Return True when a markdown path has a safe research filename."""
    if ".." in path.parts:
        return False
    return _RESEARCH_FILENAME_RE.fullmatch(path.name) is not None


async def _send_caption_as_text(message: Message, caption: str) -> None:
    """Send a caption as MarkdownV2 text messages, splitting when oversized."""
    if not caption:
        return
    if len(caption) <= TELEGRAM_TEXT_LIMIT:
        await message.answer(caption, parse_mode="MarkdownV2")
        return
    for chunk in split_message(caption, TELEGRAM_TEXT_LIMIT):
        await message.answer(chunk, parse_mode="MarkdownV2")


async def deliver_report(
    message: Message,
    markdown_path: Path | str,
    caption: str,
    reports_dir: Path | str | None = None,
) -> bool:
    """Attach a full markdown report, falling back to text when unavailable.

    The file is sent via :class:`BufferedInputFile` with its safe
    ``research-<id>.md`` filename. ``report_id`` charset validation and
    traversal prevention run before any filesystem access: unsafe names,
    paths escaping ``reports_dir``, and missing files all fall back to
    sending ``caption`` as MarkdownV2 text and return ``False``. Returns
    ``True`` only when ``answer_document`` was called. Never raises.
    """
    try:
        path = Path(markdown_path)
    except Exception:  # noqa: BLE001 - graceful fallback to caption text
        try:
            await _send_caption_as_text(message, caption)
        except Exception:  # noqa: BLE001, S110 - delivery must never raise
            pass
        return False
    try:
        if ".." in path.parts or not _is_safe_markdown_path(path):
            await _send_caption_as_text(message, caption)
            return False
        if reports_dir is not None:
            base = Path(reports_dir).resolve()  # noqa: ASYNC240 - tiny path resolve
            try:
                if path.is_absolute():
                    resolved = path.resolve()  # noqa: ASYNC240 - tiny path resolve
                else:
                    resolved = (base / path.name).resolve()  # noqa: ASYNC240 - tiny resolve
            except Exception:  # noqa: BLE001 - treat unresolvable as unsafe
                await _send_caption_as_text(message, caption)
                return False
            try:
                if not resolved.is_relative_to(base):
                    await _send_caption_as_text(message, caption)
                    return False
            except AttributeError:
                # Python < 3.9 fallback: compare parts manually.
                if base.parts != resolved.parts[: len(base.parts)]:
                    await _send_caption_as_text(message, caption)
                    return False
            path = resolved
        if not path.is_file():
            await _send_caption_as_text(message, caption)
            return False
        data = await asyncio.to_thread(path.read_bytes)
        document = BufferedInputFile(data, filename=path.name)
        if caption and len(caption) <= TELEGRAM_CAPTION_LIMIT:
            await message.answer_document(
                document=document, caption=caption, parse_mode="MarkdownV2"
            )
        elif caption:
            for chunk in split_message(caption, TELEGRAM_TEXT_LIMIT):
                await message.answer(chunk, parse_mode="MarkdownV2")
            await message.answer_document(document=document)
        else:
            await message.answer_document(document=document)
        return True
    except Exception as exc:  # noqa: BLE001 - missing-file and edit failures stay graceful
        logger.warning("report delivery failed: %s: %s", type(exc).__name__, path.name)
        try:
            await _send_caption_as_text(message, caption)
        except Exception:  # noqa: BLE001, S110 - never raise from delivery
            pass
        return False
