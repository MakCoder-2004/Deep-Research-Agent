"""Unit tests for MarkdownV2 escaping, splitting, and concise reports (M2.29)."""

from __future__ import annotations

import re

import pytest

from research_agent.telegram.renderer import (
    TELEGRAM_TEXT_LIMIT,
    escape_markdown_v2,
    render_citation_marker,
    render_concise_report,
    render_safe_fallback,
    report_filename,
    split_message,
)


def test_escape_table_all_specials() -> None:
    specials = [
        "_",
        "*",
        "[",
        "]",
        "(",
        ")",
        "~",
        "`",
        ">",
        "#",
        "+",
        "-",
        "=",
        "|",
        "{",
        "}",
        ".",
        "!",
    ]
    for ch in specials:
        escaped = escape_markdown_v2(ch)
        assert escaped == "\\" + ch, f"special {ch!r} must be escaped"


def test_escape_en_and_ar_preserves_text() -> None:
    en = "Hello world 123"
    assert escape_markdown_v2(en) == en
    ar = "مرحبا بالعالم ١٢٣"
    assert escape_markdown_v2(ar) == ar
    mixed = (
        "Solar_الطاقة*test [demo] (v1.0) 100% #tag +more -less =eq |pipe {x} >q `code` ~tilde !bang"
    )
    escaped = escape_markdown_v2(mixed)
    assert "\\_" in escaped
    assert "\\*" in escaped
    assert "\\[" in escaped
    assert "\\]" in escaped
    assert "\\." in escaped
    assert "Solar" in escaped
    assert "الطاقة" in escaped


def test_escape_does_not_double_escape() -> None:
    already = "\\*already\\_escaped\\[1\\]"
    assert escape_markdown_v2(already) == already
    lone = "back\\slash"
    assert escape_markdown_v2(lone) == "back\\\\slash"


def test_render_citation_marker() -> None:
    assert render_citation_marker(1) == "[1]"
    assert render_citation_marker(12) == "[12]"
    with pytest.raises(ValueError):
        render_citation_marker(0)


def test_split_single_chunk_no_header() -> None:
    text = "Short message\n\nSecond paragraph."
    chunks = split_message(text)
    assert chunks == [text]


def test_split_respects_limit_and_rejoins() -> None:
    paras = [
        f"Paragraph {i} with some content. Second sentence here! Third? Yes. Extra filler."
        for i in range(80)
    ]
    text = "\n\n".join(paras)
    assert len(text) > TELEGRAM_TEXT_LIMIT
    chunks = split_message(text, TELEGRAM_TEXT_LIMIT)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_TEXT_LIMIT
        assert re.fullmatch(r"\(\d+/\d+\) .*", chunk, flags=re.DOTALL)
    stripped = [re.sub(r"^\(\d+/\d+\) ", "", chunk) for chunk in chunks]
    assert "".join(stripped) == text


def test_split_small_limit_paragraph_boundaries() -> None:
    text = (
        "Para one line1\nline2\n\nPara two sentence one. Sentence two! Question? Yes.\nLast line."
    )
    chunks = split_message(text, 60)
    for chunk in chunks:
        assert len(chunk) <= 60
    stripped = [re.sub(r"^\(\d+/\d+\) ", "", chunk) for chunk in chunks]
    assert "".join(stripped) == text


def test_split_never_mid_marker_or_escape() -> None:
    text = "Finding with citation [12] and escaped \\* content. " * 30
    assert len(text) > 200
    chunks = split_message(text, 100)
    for chunk in chunks:
        assert len(chunk) <= 100
        body = re.sub(r"^\(\d+/\d+\) ", "", chunk)
        # No dangling single escape at the end.
        trailing = len(body) - len(body.rstrip("\\"))
        assert trailing % 2 == 0
    stripped = [re.sub(r"^\(\d+/\d+\) ", "", chunk) for chunk in chunks]
    rejoined = "".join(stripped)
    assert rejoined == text
    assert "[12]" in rejoined
    # Markers must survive whole (not split as "[1" / "2]").
    for chunk in stripped:
        assert "[1" not in chunk or "[12]" in chunk or chunk.count("[") == chunk.count("]")


def test_split_invalid_limit() -> None:
    with pytest.raises(ValueError):
        split_message("hi", 5)


def test_concise_en_contains_markers_tools_partial() -> None:
    topic = "Solar_batteries* v1.0"
    findings = [
        {"statement": "Efficiency improved 20% (2024).", "citation_ids": [1, 2]},
        {"statement": "Costs fell.", "citation_ids": [2]},
    ]
    sources = [
        {"id": 1, "title": "Paper_one", "url": "https://example.com/a"},
        {"id": 2, "title": "Report.two", "url": "https://example.com/b"},
    ]
    text = render_concise_report(
        topic,
        findings,
        sources,
        "en",
        partial=True,
        disclaimer="Not financial advice.",
        tools_used=["tavily_search"],
        tools_failed=["provider_down"],
    )
    assert text.startswith("⚠️ Partial report")
    assert "*Solar\\_batteries\\* v1\\.0*" in text
    assert "[1]" in text and "[2]" in text
    assert "\\[1\\]" not in text
    assert "Key findings:" in text
    assert "Sources:" in text
    assert "Tools:" in text
    assert "tavily\\_search" in text
    assert r"Failed tools \(coverage affected\): provider\_down" in text
    assert r"Not financial advice\." in text


def test_failed_tools_are_hidden_when_report_is_not_partial() -> None:
    text = render_concise_report(
        "Topic",
        [{"statement": "Claim", "citation_ids": [1]}],
        [{"id": 1, "title": "Source", "url": "https://example.com"}],
        tools_used=["successful_tool"],
        tools_failed=["optional_tool"],
    )
    assert r"successful\_tool" in text
    assert "Failed tools" not in text
    assert r"optional\_tool" not in text


def test_invalid_report_payloads_raise_before_rendering() -> None:
    source = {"id": 1, "title": "Source", "url": "https://example.com"}
    with pytest.raises(ValueError, match="Every finding must have at least one citation"):
        render_concise_report("Topic", [{"statement": "Claim", "citation_ids": []}], [source])
    with pytest.raises(ValueError, match="existing source"):
        render_concise_report("Topic", [{"statement": "Claim", "citation_ids": [2]}], [source])
    with pytest.raises(ValueError, match="positive integers"):
        render_concise_report("Topic", [{"statement": "Claim", "citation_ids": ["bad"]}], [source])
    with pytest.raises(ValueError, match="unsupported fields"):
        render_concise_report(
            "Topic",
            [{"statement": "Claim", "citation_ids": [1], "prompt": "do not render"}],
            [source],
        )


def test_report_local_source_ref_wins_over_database_id() -> None:
    text = render_concise_report(
        "Topic",
        [{"statement": "Claim", "citation_ids": [1]}],
        [
            {
                "id": 42,
                "source_ref": 1,
                "title": "Report-local source",
                "url": "https://example.com/local",
            }
        ],
    )
    assert r"[1] [Report\-local source]" in text
    assert "[42]" not in text


def test_safe_fallback_escapes_english_and_arabic_text() -> None:
    english = render_safe_fallback("Topic *one*", "Summary [unsafe] _text_.", "en")
    arabic = render_safe_fallback("موضوع [1]", "ملخص *غير آمن*.", "ar")
    assert "Topic \\*one\\*" in english
    assert r"Summary \[unsafe\] \_text\_\." in english
    assert r"موضوع \[1\]" in arabic
    assert r"ملخص \*غير آمن\*\." in arabic


def test_concise_ar_ltr_markers() -> None:
    topic = "بطاريات_الطاقة"
    findings = [{"statement": "تحسنت الكفاءة بنسبة 20%.", "citation_ids": [1]}]
    sources = [{"id": 1, "title": "تقرير الطاقة", "url": "https://example.com/ar"}]
    text = render_concise_report(topic, findings, sources, "ar")
    assert "أبرز النتائج:" in text
    assert "المصادر:" in text
    assert "[1]" in text
    assert "\\[1\\]" not in text
    assert "بطاريات\\_الطاقة" in text
    # ASCII markers stay LTR inside RTL text.
    assert re.search(r"\[1\]", text) is not None


def test_report_filename_valid_and_traversal() -> None:
    assert report_filename("abc123") == "research-abc123.md"
    assert report_filename("A-Z-0-9-x") == "research-A-Z-0-9-x.md"
    for bad in ["", "../etc", "a/b", "a\\b", "rep id", "rep.id", "rep_id", "a/b/../c"]:
        with pytest.raises(ValueError):
            report_filename(bad)
