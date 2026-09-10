"""Integration tests for history, report retrieval, and forget (M2.27)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from research_agent.persistence.database import open_db
from research_agent.persistence.repositories import (
    JobRepository,
    ReportRepository,
    SessionRepository,
    SourceRepository,
    UserRepository,
)
from research_agent.telegram.handlers import (
    forget_handler,
    history_handler,
    report_handler,
)


def _msg(
    user_id: int | None,
    text: str | None,
    lang: str | None = "en",
) -> MagicMock:
    message = MagicMock()
    if user_id is None:
        message.from_user = None
    else:
        message.from_user = SimpleNamespace(id=user_id, language_code=lang)
    message.chat = SimpleNamespace(id=user_id or 0, type="private")
    message.text = text
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=21))
    message.answer_document = AsyncMock()
    return message


async def _seed_report(
    conn,
    user_id: int,
    job_id: str,
    report_id: str,
    topic: str,
    summary: str,
    tools: list[str] | None = None,
) -> None:
    await JobRepository().create(conn, job_id=job_id, user_id=user_id, query=topic)
    await ReportRepository().save(
        conn,
        report_id=report_id,
        job_id=job_id,
        topic=topic,
        summary=summary,
        markdown_path=f"data/reports/research-{report_id}.md",
        tools_used=tools or ["tavily_search"],
    )
    await SourceRepository().save_many(
        conn,
        report_id,
        [
            {
                "source_ref": 1,
                "title": f"{topic} source",
                "url": "https://example.com/article",
                "publisher": "Example",
                "published_at": None,
                "accessed_at": "2026-01-01T00:00:00+00:00",
                "source_type": "web",
            }
        ],
    )
    await conn.commit()


async def test_history_owner_scoped(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    async with open_db(db_path) as conn:
        await _seed_report(conn, 111, "job-a1", "rep-a1", "Solar topic", "Solar summary")
        await _seed_report(conn, 111, "job-a2", "rep-a2", "Battery topic", "Battery summary")
        await _seed_report(conn, 222, "job-b1", "rep-b1", "Other topic", "Other summary")
        mine = _msg(111, "/history", "en")
        await history_handler(mine, conn=conn)
        reply = mine.answer.call_args[0][0]
        assert "rep-a1" in reply
        assert "rep-a2" in reply
        assert "rep-b1" not in reply
        other = _msg(222, "/history", "en")
        await history_handler(other, conn=conn)
        other_reply = other.answer.call_args[0][0]
        assert "rep-b1" in other_reply
        assert "rep-a1" not in other_reply


async def test_history_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "history_empty.db"
    async with open_db(db_path) as conn:
        message = _msg(999, "/history", "en")
        await history_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "no saved reports" in reply.lower() or "لا توجد" in reply


async def test_report_found_not_found_forbidden(tmp_path: Path) -> None:
    db_path = tmp_path / "report.db"
    async with open_db(db_path) as conn:
        await _seed_report(conn, 111, "job-1", "rep-1", "Solar topic", "Solar summary text")
        found = _msg(111, "/report rep-1", "en")
        await report_handler(found, conn=conn)
        assert found.answer.await_count >= 1
        first_call = found.answer.call_args_list[0]
        text = first_call[0][0]
        kwargs = first_call[1]
        assert "Solar topic" in text
        assert "[1]" in text
        assert kwargs.get("parse_mode") == "MarkdownV2"
        missing = _msg(111, "/report does-not-exist", "en")
        await report_handler(missing, conn=conn)
        missing_reply = missing.answer.call_args[0][0]
        assert "not found" in missing_reply.lower() or "غير موجود" in missing_reply
        forbidden = _msg(222, "/report rep-1", "en")
        await report_handler(forbidden, conn=conn)
        forbidden_reply = forbidden.answer.call_args[0][0]
        assert "not found" in forbidden_reply.lower() or "غير موجود" in forbidden_reply
        assert "Solar topic" not in forbidden_reply
        usage = _msg(111, "/report", "en")
        await report_handler(usage, conn=conn)
        usage_reply = usage.answer.call_args[0][0]
        assert "Usage" in usage_reply or "الاستخدام" in usage_reply


async def test_forget_deletes_sessions_keeps_reports(tmp_path: Path) -> None:
    db_path = tmp_path / "forget.db"
    async with open_db(db_path) as conn:
        sessions = SessionRepository()
        await UserRepository().upsert(conn, 111, "en")
        await conn.commit()
        await sessions.save(conn, 111, "en", ["hello", "world"])
        await conn.commit()
        await _seed_report(conn, 111, "job-f1", "rep-f1", "Keep me", "Keep summary")
        before = await sessions.get(conn, 111)
        assert before is not None
        message = _msg(111, "/forget", "en")
        await forget_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "cleared" in reply.lower() or "مسح" in reply
        after = await sessions.get(conn, 111)
        assert after is None
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE user_id = 111")
        row = await cursor.fetchone()
        assert row is not None and int(row["n"]) >= 1
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM reports WHERE report_id = 'rep-f1'")
        row = await cursor.fetchone()
        assert row is not None and int(row["n"]) == 1
        history_msg = _msg(111, "/history", "en")
        await history_handler(history_msg, conn=conn)
        assert "rep-f1" in history_msg.answer.call_args[0][0]


async def test_report_tools_line_present(tmp_path: Path) -> None:
    db_path = tmp_path / "tools.db"
    async with open_db(db_path) as conn:
        await _seed_report(
            conn,
            111,
            "job-t1",
            "rep-t1",
            "Tool topic",
            "Tool summary",
            tools=["tavily_search", "wikipedia_search"],
        )
        message = _msg(111, "/report rep-t1", "en")
        await report_handler(message, conn=conn)
        text = message.answer.call_args_list[0][0][0]
        # Tools are MarkdownV2-escaped (underscore -> \_) but still present.
        assert "tavily" in text
        assert "wikipedia" in text
        assert "tavily\\_search" in text
        assert "wikipedia\\_search" in text
        assert json.loads('["tavily_search"]') == ["tavily_search"]
