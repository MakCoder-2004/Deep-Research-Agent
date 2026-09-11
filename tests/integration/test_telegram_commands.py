"""Integration tests for Telegram commands, routing, URLs, and language (M2.27)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from research_agent.persistence.database import open_db
from research_agent.persistence.repositories import UserRepository
from research_agent.services.queue import BoundedJobQueue, get_user_active_job
from research_agent.telegram.handlers import (
    handle_research_request,
    help_handler,
    language_handler,
    non_text_handler,
    plaintext_handler,
    research_handler,
    start_handler,
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
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11))
    message.answer_document = AsyncMock()
    return message


async def _job_count_for(conn, user_id: int) -> int:
    job = await get_user_active_job(conn, user_id)
    return 1 if job is not None else 0


async def test_start_upserts_user_and_replies(tmp_path: Path) -> None:
    db_path = tmp_path / "cmd.db"
    async with open_db(db_path) as conn:
        message = _msg(123, "/start", "en")
        await start_handler(message, conn=conn)
        message.answer.assert_awaited_once()
        reply = message.answer.call_args[0][0]
        assert "/research" in reply
        row = await UserRepository().get(conn, 123)
        assert row is not None
        assert str(row["language"]) == "en"


async def test_start_arabic_upserts_ar(tmp_path: Path) -> None:
    db_path = tmp_path / "cmd_ar.db"
    async with open_db(db_path) as conn:
        message = _msg(124, "/start", "ar-EG")
        await start_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "وكيل البحث" in reply
        row = await UserRepository().get(conn, 124)
        assert row is not None
        assert str(row["language"]) == "ar"


async def test_help_replies_without_job(tmp_path: Path) -> None:
    db_path = tmp_path / "help.db"
    async with open_db(db_path) as conn:
        message = _msg(123, "/help", "en")
        await help_handler(message)
        reply = message.answer.call_args[0][0]
        assert "/research" in reply
        assert await _job_count_for(conn, 123) == 0


async def test_research_ok_enqueues(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    async with open_db(db_path) as conn:
        message = _msg(123, "/research What is solar energy?", "en")
        await research_handler(message, conn=conn)
        assert message.answer.await_count >= 1
        first = message.answer.call_args_list[0][0][0]
        assert "What is solar energy?" in first
        assert await _job_count_for(conn, 123) == 1


async def test_research_empty_usage_no_job(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    async with open_db(db_path) as conn:
        message = _msg(123, "/research   ", "en")
        await research_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "Usage" in reply or "الاستخدام" in reply
        assert await _job_count_for(conn, 123) == 0


async def test_plaintext_routes_like_research(tmp_path: Path) -> None:
    db_path = tmp_path / "plain.db"
    async with open_db(db_path) as conn:
        research_msg = _msg(111, "/research Which batteries last longest?", "en")
        await research_handler(research_msg, conn=conn)
        research_reply = research_msg.answer.call_args_list[0][0][0]
        plain_msg = _msg(222, "Which batteries last longest?", "en")
        await plaintext_handler(plain_msg, conn=conn)
        plain_reply = plain_msg.answer.call_args_list[0][0][0]
        assert "Which batteries last longest?" in research_reply
        assert "Which batteries last longest?" in plain_reply
        assert "received" in research_reply or "استلام" in research_reply
        assert "received" in plain_reply or "استلام" in plain_reply


async def test_plaintext_empty_no_job(tmp_path: Path) -> None:
    db_path = tmp_path / "plain_empty.db"
    async with open_db(db_path) as conn:
        message = _msg(123, "   ", "en")
        await plaintext_handler(message, conn=conn)
        assert await _job_count_for(conn, 123) == 0


async def test_non_text_ignored_no_job(tmp_path: Path) -> None:
    db_path = tmp_path / "nontext.db"
    async with open_db(db_path) as conn:
        message = _msg(123, None, "en")
        await non_text_handler(message)
        reply = message.answer.call_args[0][0]
        assert "text" in reply.lower() or "نص" in reply
        assert await _job_count_for(conn, 123) == 0


async def test_url_matrix(tmp_path: Path) -> None:
    cases = [
        ("https://example.com/article", True),
        ("http://example.com/article", True),
        ("https://example.com/بحث-عربي", True),
        ("javascript:alert(1)", False),
        ("ftp://example.com/file", False),
        ("file:///etc/passwd", False),
        ("data:text/plain,hello", False),
        ("https://user:pass@example.com/", False),
        ("https://", False),
        ("https://" + "a" * 1995 + ".com", False),
    ]
    for idx, (url, should_enqueue) in enumerate(cases):
        db_path = tmp_path / f"url_{idx}.db"
        async with open_db(db_path) as conn:
            user_id = 5000 + idx
            message = _msg(user_id, f"/research {url}", "en")
            await research_handler(message, conn=conn)
            count = await _job_count_for(conn, user_id)
            if should_enqueue:
                assert count == 1, f"expected enqueue for {url}"
            else:
                assert count == 0, f"expected reject for {url}"
                reply = message.answer.call_args[0][0]
                assert "invalid" in reply.lower() or "غير صالح" in reply


async def test_language_set_get_invalid(tmp_path: Path) -> None:
    db_path = tmp_path / "lang.db"
    async with open_db(db_path) as conn:
        current = _msg(123, "/language", "en")
        await language_handler(current, conn=conn)
        reply = current.answer.call_args[0][0]
        assert "en" in reply.lower() or "English" in reply
        set_ar = _msg(123, "/language ar", "en")
        await language_handler(set_ar, conn=conn)
        reply_ar = set_ar.answer.call_args[0][0]
        assert "ar" in reply_ar.lower() or "العربية" in reply_ar
        get_again = _msg(123, "/language", "en")
        await language_handler(get_again, conn=conn)
        assert (
            "ar" in get_again.answer.call_args[0][0].lower()
            or "العربية" in (get_again.answer.call_args[0][0])
        )
        invalid = _msg(123, "/language xx", "en")
        await language_handler(invalid, conn=conn)
        invalid_reply = invalid.answer.call_args[0][0]
        assert "Invalid" in invalid_reply or "غير صالحة" in invalid_reply
        set_en = _msg(123, "/language english", "ar")
        await language_handler(set_en, conn=conn)
        assert "en" in set_en.answer.call_args[0][0].lower()


async def test_research_no_db_replies_unavailable() -> None:
    message = _msg(123, "/research hello", "en")
    result = await handle_research_request(message, "hello")
    assert result is None
    reply = message.answer.call_args[0][0]
    assert "unavailable" in reply.lower() or "غير متاحة" in reply
    assert "Job ID" not in reply and "معرّف المهمة" not in reply


async def test_research_no_db_arabic_unavailable() -> None:
    message = _msg(123, "/research hello", "ar-EG")
    result = await handle_research_request(message, "hello")
    assert result is None
    reply = message.answer.call_args[0][0]
    assert "غير متاحة" in reply


async def test_plaintext_no_db_replies_unavailable() -> None:
    message = _msg(123, "hello world", "en")
    await plaintext_handler(message)
    reply = message.answer.call_args[0][0]
    assert "unavailable" in reply.lower() or "غير متاحة" in reply


async def test_research_with_live_queue_reaches_terminal_state(tmp_path: Path) -> None:
    db_path = tmp_path / "live_queue.db"
    queue = BoundedJobQueue(db_path)
    await queue.start()
    try:
        message = _msg(321, "/research live queue query", "en")
        result = await handle_research_request(
            message,
            "live queue query",
            db_path=db_path,
            job_queue=queue,
        )
        assert result is not None

        async def _wait_for_terminal() -> None:
            while True:
                async with open_db(db_path) as conn:
                    cursor = await conn.execute(
                        "SELECT state FROM jobs WHERE job_id = ?", (result.job_id,)
                    )
                    row = await cursor.fetchone()
                if row is not None and str(row["state"]) in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait_for_terminal(), timeout=2)
        async with open_db(db_path) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n, state FROM jobs WHERE job_id = ?", (result.job_id,)
            )
            row = await cursor.fetchone()
        assert row is not None and int(row["n"]) == 1
        assert str(row["state"]) == "completed"
    finally:
        await queue.stop()
