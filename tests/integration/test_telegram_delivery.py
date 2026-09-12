"""Integration tests for report file delivery (M2.29)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from research_agent.persistence.database import open_db
from research_agent.persistence.repositories import (
    JobRepository,
    ReportRepository,
    SourceRepository,
)
from research_agent.telegram.handlers import report_handler
from research_agent.telegram.renderer import deliver_report, report_filename


def _msg(user_id: int, text: str, lang: str = "en") -> MagicMock:
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=lang)
    message.chat = SimpleNamespace(id=user_id, type="private")
    message.text = text
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=31))
    message.answer_document = AsyncMock()
    return message


async def test_answer_document_called(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_id = "abc123"
    filename = report_filename(report_id)
    assert filename == "research-abc123.md"
    md_path = reports_dir / filename
    md_path.write_text("# Topic\n\nBody with [1].\n", encoding="utf-8")
    message = _msg(111, "caption")
    caption = "Hello \\*world\\*"
    ok = await deliver_report(message, md_path, caption, reports_dir)
    assert ok is True
    message.answer_document.assert_awaited_once()
    kwargs = message.answer_document.call_args[1]
    assert kwargs["caption"] == caption
    assert kwargs["parse_mode"] == "MarkdownV2"
    assert kwargs["document"].filename == filename
    message.answer.assert_not_awaited()


async def test_missing_file_graceful(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    missing = reports_dir / "research-missing1.md"
    assert not missing.exists()
    message = _msg(111, "caption")
    ok = await deliver_report(message, missing, "Fallback \\*caption\\*", reports_dir)
    assert ok is False
    message.answer_document.assert_not_awaited()
    message.answer.assert_awaited_once()
    kwargs = message.answer.call_args[1]
    assert kwargs.get("parse_mode") == "MarkdownV2"


async def test_raw_caption_is_escaped_on_text_fallback(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    message = _msg(111, "caption")
    ok = await deliver_report(
        message,
        reports_dir / "research-missing2.md",
        "Raw *caption* [x].",
        reports_dir,
    )
    assert ok is False
    delivered = message.answer.call_args[0][0]
    assert delivered == "Raw \\*caption\\* \\[x\\]\\."
    assert message.answer.call_args[1]["parse_mode"] == "MarkdownV2"


async def test_raw_caption_is_escaped_on_document_delivery(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    path = reports_dir / "research-caption1.md"
    path.write_text("# Report\n", encoding="utf-8")
    message = _msg(111, "caption")
    ok = await deliver_report(message, path, "Raw *caption* [x].", reports_dir)
    assert ok is True
    kwargs = message.answer_document.call_args[1]
    assert kwargs["caption"] == "Raw \\*caption\\* \\[x\\]\\."


async def test_traversal_prevented(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    message = _msg(111, "caption")
    traversal = reports_dir / ".." / "research-evil.md"
    ok = await deliver_report(message, traversal, "cap", reports_dir)
    assert ok is False
    message.answer_document.assert_not_awaited()
    message.answer.assert_awaited_once()
    bad_name = tmp_path / "research-evil!.md"
    message2 = _msg(111, "caption")
    ok2 = await deliver_report(message2, bad_name, "cap2", reports_dir)
    assert ok2 is False
    message2.answer_document.assert_not_awaited()


async def test_report_handler_attaches_file(tmp_path: Path) -> None:
    db_path = tmp_path / "delivery.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_id = "rep-file1"
    filename = report_filename(report_id)
    md_path = reports_dir / filename
    md_path.write_text("# Keep me\n\nFull body.\n", encoding="utf-8")
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="job-del1", user_id=111, query="Keep me")
        await ReportRepository().save(
            conn,
            report_id=report_id,
            job_id="job-del1",
            topic="Keep me",
            summary="Keep summary",
            markdown_path=str(md_path),
            tools_used=["tavily_search"],
        )
        await SourceRepository().save_many(
            conn,
            report_id,
            [
                {
                    "source_ref": 1,
                    "title": "Source one",
                    "url": "https://example.com/1",
                    "publisher": None,
                    "published_at": None,
                    "accessed_at": "2026-01-01T00:00:00+00:00",
                    "source_type": "web",
                }
            ],
        )
        await conn.commit()
        message = _msg(111, f"/report {report_id}", "en")
        await report_handler(message, conn=conn, reports_dir=reports_dir)
        message.answer_document.assert_awaited_once()
        kwargs = message.answer_document.call_args[1]
        assert kwargs["document"].filename == filename


async def test_report_handler_uses_report_local_source_refs(tmp_path: Path) -> None:
    db_path = tmp_path / "source_refs.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    first_path = reports_dir / report_filename("rep-first")
    second_path = reports_dir / report_filename("rep-second")
    first_path.write_text("# First\n", encoding="utf-8")
    second_path.write_text("# Second\n", encoding="utf-8")
    async with open_db(db_path) as conn:
        await JobRepository().create(
            conn,
            job_id="job-first",
            user_id=111,
            query="First",
            state="completed",
        )
        await ReportRepository().save(
            conn,
            report_id="rep-first",
            job_id="job-first",
            topic="First",
            summary="First summary",
            markdown_path=str(first_path),
            tools_used=[],
        )
        await SourceRepository().save_many(
            conn,
            "rep-first",
            [
                {
                    "source_ref": 1,
                    "title": "First source",
                    "url": "https://example.com/first",
                    "publisher": None,
                    "published_at": None,
                    "accessed_at": "2026-01-01T00:00:00+00:00",
                    "source_type": "web",
                }
            ],
        )
        await JobRepository().create(conn, job_id="job-second", user_id=111, query="Second")
        await ReportRepository().save(
            conn,
            report_id="rep-second",
            job_id="job-second",
            topic="Second",
            summary="Second summary",
            markdown_path=str(second_path),
            tools_used=[],
        )
        await SourceRepository().save_many(
            conn,
            "rep-second",
            [
                {
                    "source_ref": 1,
                    "title": "Second source",
                    "url": "https://example.com/second",
                    "publisher": None,
                    "published_at": None,
                    "accessed_at": "2026-01-01T00:00:00+00:00",
                    "source_type": "web",
                }
            ],
        )
        await conn.commit()
        message = _msg(111, "/report rep-second", "en")
        await report_handler(message, conn=conn, reports_dir=reports_dir)
        kwargs = message.answer_document.call_args[1]
        assert kwargs["caption"].find("\\[1\\]") >= 0
        assert "\\[2\\]" not in kwargs["caption"]


async def test_report_handler_missing_file_sends_text(tmp_path: Path) -> None:
    db_path = tmp_path / "delivery_missing.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="job-del2", user_id=111, query="Topic X")
        await ReportRepository().save(
            conn,
            report_id="rep-missing",
            job_id="job-del2",
            topic="Topic X",
            summary="Summary X",
            markdown_path=str(reports_dir / "research-rep-missing.md"),
            tools_used=[],
        )
        await SourceRepository().save_many(
            conn,
            "rep-missing",
            [
                {
                    "source_ref": 1,
                    "title": "T",
                    "url": "https://example.com/x",
                    "publisher": None,
                    "published_at": None,
                    "accessed_at": "2026-01-01T00:00:00+00:00",
                    "source_type": "web",
                }
            ],
        )
        await conn.commit()
        message = _msg(111, "/report rep-missing", "en")
        await report_handler(message, conn=conn, reports_dir=reports_dir)
        message.answer_document.assert_not_awaited()
        assert message.answer.await_count >= 1
