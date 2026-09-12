"""M2 close-out tests: the three M2.GATE items plus wiring coverage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from research_agent.config import Settings
from research_agent.models import JobState
from research_agent.persistence.database import open_db
from research_agent.persistence.repositories import (
    CacheRepository,
    JobRepository,
    ReportRepository,
    SessionRepository,
    SourceRepository,
    ToolRunRepository,
    UserRepository,
)
from research_agent.services.queue import (
    BoundedJobQueue,
    JobRef,
    _check_daily_quota,
    _coerce_enqueue_args,
    _deadline_from_created_at,
    _parse_timestamp,
    _set_job_deadline,
    cancel_user_job,
    enqueue_request,
    get_queue,
    get_user_active_job,
    get_user_latest_job_in_state,
    set_default_queue,
    set_state,
)
from research_agent.services.reports import list_recent_reports
from research_agent.services.retention import RetentionPolicy, run_retention
from research_agent.services.sessions import get_language, set_language
from research_agent.telegram.bot import (
    _reconcile_persisted_jobs,
    _recover_queue,
    create_dispatcher,
    start_polling,
    start_queue_worker,
    stop_queue_worker,
)
from research_agent.telegram.handlers import (
    cancel_handler,
    forget_handler,
    history_handler,
    language_handler,
    non_text_handler,
    plaintext_handler,
    report_handler,
    research_handler,
    router,
    start_handler,
    status_handler,
)
from research_agent.telegram.middlewares import AllowlistMiddleware
from research_agent.telegram.texts import UNAUTHORIZED_EN


def _msg(
    user_id: int | None,
    text: str | None,
    lang: str | None = "en",
    chat_type: str = "private",
    caption: str | None = None,
) -> MagicMock:
    message = MagicMock()
    if user_id is None:
        message.from_user = None
    else:
        message.from_user = SimpleNamespace(id=user_id, language_code=lang)
    message.chat = SimpleNamespace(id=user_id or 0, type=chat_type)
    message.text = text
    message.caption = caption
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11))
    message.answer_document = AsyncMock()
    return message


def _handler_by_name(name: str):  # type: ignore[no-untyped-def]
    for handler_obj in router.message.handlers:
        if handler_obj.callback.__name__ == name:
            return handler_obj
    raise AssertionError(f"handler {name} not registered")


async def _job_count(conn) -> int:  # type: ignore[no-untyped-def]
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM jobs")
    row = await cursor.fetchone()
    assert row is not None
    return int(row["n"])


# --- M2.GATE(1): cancel success reports the verified job ID -----------------


async def test_cancel_success_reports_verified_id(tmp_path: Path) -> None:
    db_path = tmp_path / "cancel_ok.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 111, "cancel me")
        message = _msg(111, "/cancel", "en")
        await cancel_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert job.job_id in reply
        assert "cancelled" in reply.lower()
        row = await JobRepository().get(conn, job.job_id)
        assert row is not None and row["state"] == "cancelled"


# --- M2.GATE(2): full unauthorized-command matrix ----------------------------


@pytest.mark.parametrize(
    "handler_name",
    [
        "research_handler",
        "plaintext_handler",
        "non_text_handler",
        "status_handler",
        "cancel_handler",
        "history_handler",
        "report_handler",
        "forget_handler",
        "language_handler",
    ],
)
def test_protected_handlers_lack_bypass_flag(handler_name: str) -> None:
    obj = _handler_by_name(handler_name)
    assert obj.flags.get("allow_unauthorized", False) is not True


@pytest.mark.parametrize("handler_name", ["whoami_handler", "start_handler", "help_handler"])
def test_harmless_handlers_allow_unauthorized(handler_name: str) -> None:
    obj = _handler_by_name(handler_name)
    assert obj.flags.get("allow_unauthorized") is True


@pytest.mark.parametrize(
    "handler_name",
    [
        "research_handler",
        "status_handler",
        "cancel_handler",
        "history_handler",
        "report_handler",
        "forget_handler",
        "language_handler",
    ],
)
async def test_unauthorized_matrix_blocked_without_state_change(
    tmp_path: Path, handler_name: str
) -> None:
    db_path = tmp_path / f"matrix_{handler_name}.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 999, "seed query")
        middleware = AllowlistMiddleware({123})
        message = _msg(999, "/cmd", "en")
        message.answer = AsyncMock()
        obj = _handler_by_name(handler_name)
        handler = AsyncMock()
        result = await middleware(handler, message, {"handler": obj})
        assert result is None
        handler.assert_not_awaited()
        message.answer.assert_awaited_once()
        assert UNAUTHORIZED_EN in message.answer.call_args[0][0]
        assert await _job_count(conn) == 1
        row = await JobRepository().get(conn, job.job_id)
        assert row is not None and row["state"] == "queued"


# --- M2.GATE(3): three jobs run concurrently, the fourth waits --------------


async def test_three_concurrent_fourth_waits(tmp_path: Path) -> None:
    db_path = tmp_path / "conc3.db"
    queue = BoundedJobQueue(db_path, max_concurrent_jobs=3)
    active = 0
    max_active = 0
    started = 0
    started_event = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(job: JobRef) -> None:
        nonlocal active, max_active, started
        active += 1
        max_active = max(max_active, active)
        started += 1
        if started == 3:
            started_event.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=10)
        finally:
            active -= 1
        async with open_db(db_path) as conn:
            await set_state(conn, job.job_id, JobState.ACTIVE)
            await set_state(conn, job.job_id, JobState.COMPLETED)

    queue.run_placeholder = fake_run  # type: ignore[method-assign]
    refs = []
    async with open_db(db_path) as conn:
        for user in (1, 2, 3, 4):
            refs.append(await enqueue_request(conn, user, f"q{user}"))
    for ref in refs:
        await queue.put(ref)
    await queue.start()
    try:
        await asyncio.wait_for(started_event.wait(), timeout=10)
        await asyncio.sleep(0.05)
        assert max_active == 3
        async with open_db(db_path) as conn:
            fourth = await JobRepository().get(conn, refs[3].job_id)
            assert fourth is not None and fourth["state"] == "queued"
        release.set()
        for _ in range(100):
            states = []
            async with open_db(db_path) as conn:
                for ref in refs:
                    row = await JobRepository().get(conn, ref.job_id)
                    assert row is not None
                    states.append(row["state"])
            if all(state == "completed" for state in states):
                break
            await asyncio.sleep(0.05)
        assert all(state == "completed" for state in states)
        assert max_active == 3
    finally:
        release.set()
        await queue.stop()


# --- quota reply ------------------------------------------------------------


async def test_quota_exceeded_renders_quota_message(tmp_path: Path) -> None:
    db_path = tmp_path / "quota.db"
    settings = Settings(_env_file=None, REQUESTS_PER_USER_PER_DAY=1)
    queue = BoundedJobQueue.from_settings(settings, db_path=db_path)
    first = _msg(111, "/research first query", "en")
    await research_handler(first, db_path=db_path, job_queue=queue)
    assert "received" in first.answer.call_args_list[0][0][0]
    second = _msg(111, "/research second query", "en")
    await research_handler(second, db_path=db_path, job_queue=queue)
    reply = second.answer.call_args_list[0][0][0]
    assert "quota" in reply.lower()
    assert "1" in reply
    await queue.stop()


# --- persistence wiring -----------------------------------------------------


async def test_fk_rejects_orphan_tool_run_and_report(tmp_path: Path) -> None:
    db_path = tmp_path / "fk.db"
    async with open_db(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await ToolRunRepository().record(conn, job_id="ghost", tool_name="t", success=True)
        with pytest.raises(sqlite3.IntegrityError):
            await ReportRepository().save(
                conn,
                report_id="ghost-r",
                job_id="ghost",
                topic="T",
                summary="S",
                markdown_path="p",
                tools_used=[],
            )


async def test_fts_syncs_on_raw_insert_and_update(tmp_path: Path) -> None:
    db_path = tmp_path / "fts.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j1", user_id=1, query="q")
        await conn.execute(
            "INSERT INTO reports (report_id, job_id, topic, summary, markdown_path,"
            " tools_used, created_at) VALUES ('r1','j1','Quantum batteries','S','p','[]',?)",
            (datetime.now(UTC).isoformat(),),
        )
        await conn.commit()
        hits = await ReportRepository().search(conn, "batteries")
        assert [row["report_id"] for row in hits] == ["r1"]
        await conn.execute("UPDATE reports SET topic='Solar sails' WHERE report_id='r1'")
        await conn.commit()
        assert await ReportRepository().search(conn, "batteries") == []
        assert [row["report_id"] for row in await ReportRepository().search(conn, "sails")] == [
            "r1"
        ]


async def test_source_url_persisted_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "url.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/research https://example.com/article", "en")
        await research_handler(message, conn=conn)
        job = await get_user_active_job(conn, 111)
        assert job is not None
        assert job["source_url"] == "https://example.com/article"


async def test_research_appends_session_interaction(tmp_path: Path) -> None:
    db_path = tmp_path / "sess_append.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/research session query", "en")
        await research_handler(message, conn=conn)
        row = await SessionRepository().get(conn, 111)
        assert row is not None
        import json

        assert any("session query" in item for item in json.loads(str(row["interactions"])))


async def test_get_language_prefers_valid_session(tmp_path: Path) -> None:
    db_path = tmp_path / "lang_ttl.db"
    async with open_db(db_path) as conn:
        await UserRepository().upsert(conn, 111, "ar")
        await conn.commit()
        assert await get_language(conn, 111) == "ar"
        stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        await conn.execute(
            "INSERT OR REPLACE INTO sessions (user_id, language, interactions, updated_at)"
            " VALUES (111, 'ar', '[]', ?)",
            (stale,),
        )
        await conn.execute("UPDATE users SET language='xx' WHERE user_id=111")
        await conn.commit()
        assert await get_language(conn, 111) == "en"


async def test_set_language_refreshes_session_timestamp(tmp_path: Path) -> None:
    db_path = tmp_path / "lang_ts.db"
    async with open_db(db_path) as conn:
        await set_language(conn, 111, "en")
        row = await SessionRepository().get(conn, 111)
        assert row is not None
        first = str(row["updated_at"])
        await asyncio.sleep(0.01)
        await set_language(conn, 111, "ar")
        row = await SessionRepository().get(conn, 111)
        assert row is not None
        assert str(row["updated_at"]) >= first
        assert await get_language(conn, 111) == "ar"


async def test_run_retention_sweeps_all_stores(tmp_path: Path) -> None:
    db_path = tmp_path / "retention.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j-old", user_id=1, query="q")
        old_report = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        await conn.execute(
            "INSERT INTO reports (report_id, job_id, topic, summary, markdown_path,"
            " tools_used, created_at) VALUES ('r-old','j-old','T','S','p','[]',?)",
            (old_report,),
        )
        stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        await UserRepository().upsert(conn, 9, "en")
        await conn.execute(
            "INSERT INTO sessions (user_id, language, interactions, updated_at)"
            " VALUES (9, 'en', '[]', ?)",
            (stale,),
        )
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await conn.execute(
            "INSERT INTO cache (cache_key, payload, expires_at, created_at) VALUES ('k','v',?,?)",
            (past, datetime.now(UTC).isoformat()),
        )
        await conn.commit()
        outcome = await run_retention(conn, RetentionPolicy())
        await conn.commit()
        assert outcome["reports"] == 1
        assert outcome["cache"] == 1
        assert await SessionRepository().get(conn, 9) is None
        assert await ReportRepository().get(conn, "r-old") is None


async def test_history_tiebreak_and_limit_clamp(tmp_path: Path) -> None:
    db_path = tmp_path / "hist.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j1", user_id=1, query="q")
        stamp = datetime.now(UTC).isoformat()
        for report_id in ("r1", "r2"):
            await conn.execute(
                "INSERT INTO reports (report_id, job_id, topic, summary, markdown_path,"
                " tools_used, created_at) VALUES (?,?,?,?,'p','[]',?)",
                (report_id, "j1", f"T-{report_id}", "S", stamp),
            )
        await conn.commit()
        rows = await list_recent_reports(conn, 1, limit=5)
        assert [row["report_id"] for row in rows] == ["r2", "r1"]
        assert len(await list_recent_reports(conn, 1, limit=0)) == 1
        assert len(await list_recent_reports(conn, 1, limit=500)) == 2


async def test_trace_id_survives_error_transition(tmp_path: Path) -> None:
    db_path = tmp_path / "trace.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j1", user_id=1, query="q")
        await JobRepository().update_state(conn, "j1", "active", trace_id="trace-1")
        assert await set_state(conn, "j1", "completed", error="boom")
        row = await JobRepository().get(conn, "j1")
        assert row is not None
        assert row["trace_id"] == "trace-1"
        assert row["error"] == "boom"


# --- report tools semantics -------------------------------------------------


async def _seed_report(conn, user_id: int, job_id: str, report_id: str, tools: list[str]) -> None:  # type: ignore[no-untyped-def]
    await JobRepository().create(conn, job_id=job_id, user_id=user_id, query="q")
    await ReportRepository().save(
        conn,
        report_id=report_id,
        job_id=job_id,
        topic="T",
        summary="Summary text",
        markdown_path="missing.md",
        tools_used=tools,
    )
    await SourceRepository().save_many(
        conn,
        report_id,
        [
            {
                "source_ref": 1,
                "title": "A",
                "url": "https://example.com/a",
                "publisher": None,
                "published_at": None,
                "accessed_at": datetime.now(UTC).isoformat(),
                "source_type": "web",
            }
        ],
    )
    await conn.commit()


async def test_report_tools_uses_successful_runs_set_semantics(tmp_path: Path) -> None:
    db_path = tmp_path / "tools.db"
    async with open_db(db_path) as conn:
        # Reordered stored list still matches recorded successes.
        await _seed_report(conn, 111, "j-re", "r-re", ["wiki", "tavily"])
        await ToolRunRepository().record(conn, job_id="j-re", tool_name="tavily", success=True)
        await ToolRunRepository().record(conn, job_id="j-re", tool_name="wiki", success=True)
        await conn.commit()
        message = _msg(111, "/report r-re", "en")
        await report_handler(message, conn=conn)
        assert "tavily" in message.answer.call_args[0][0]


async def test_report_tools_omits_failed_only_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "tools_fail.db"
    async with open_db(db_path) as conn:
        await _seed_report(conn, 111, "j-f", "r-f", ["tavily"])
        await ToolRunRepository().record(conn, job_id="j-f", tool_name="tavily", success=False)
        await conn.commit()
        message = _msg(111, "/report r-f", "en")
        await report_handler(message, conn=conn)
        assert "Tools" not in message.answer.call_args[0][0]


async def test_report_tools_omits_mismatched_claims(tmp_path: Path) -> None:
    db_path = tmp_path / "tools_mm.db"
    async with open_db(db_path) as conn:
        await _seed_report(conn, 111, "j-m", "r-m", ["imaginary-tool"])
        await ToolRunRepository().record(conn, job_id="j-m", tool_name="tavily", success=True)
        await conn.commit()
        message = _msg(111, "/report r-m", "en")
        await report_handler(message, conn=conn)
        assert "Tools" not in message.answer.call_args[0][0]


# --- delivery ---------------------------------------------------------------


async def test_deliver_long_caption_splits_text_and_attaches(tmp_path: Path) -> None:
    from research_agent.telegram.renderer import deliver_report

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    target = reports_dir / "research-big1.md"
    target.write_text("# Big\n\nbody\n", encoding="utf-8")
    message = _msg(111, "/report research-big1", "en")
    caption = "y" * 1500
    assert await deliver_report(message, target, caption, reports_dir) is True
    message.answer.assert_awaited()
    _, kwargs = message.answer_document.call_args
    assert "caption" not in kwargs


# --- worker language --------------------------------------------------------


async def test_run_placeholder_prefers_stored_user_language(tmp_path: Path) -> None:
    from research_agent.telegram.progress import _PROGRESS_REGISTRY, _PROGRESS_STATES

    db_path = tmp_path / "wlang.db"
    queue = BoundedJobQueue(db_path)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    queue.set_bot(bot)
    async with open_db(db_path) as conn:
        ref = await enqueue_request(conn, 777, "q", language="mixed")
        await UserRepository().upsert(conn, 777, "ar")
        await conn.commit()
    try:
        # Registry entry without tracked state: simulates a restarted worker
        # whose in-memory language map was evicted.
        _PROGRESS_REGISTRY[ref.job_id] = (777, 55)
        _PROGRESS_STATES.pop(ref.job_id, None)
        await queue.run_placeholder(JobRef(job_id=ref.job_id, user_id=777, query="q"))
        assert bot.edit_message_text.await_count == 6
        first_text = bot.edit_message_text.call_args_list[0][0][0]
        assert any("\u0600" <= ch <= "\u06ff" for ch in first_text)
    finally:
        from research_agent.telegram.progress import cleanup_progress

        cleanup_progress(ref.job_id)
        await queue.stop()


# --- startup / recovery -----------------------------------------------------


async def test_start_polling_deletes_webhook_first() -> None:
    calls: list[str] = []
    bot = MagicMock()
    bot.delete_webhook = AsyncMock(side_effect=lambda *a, **k: calls.append("webhook"))
    dp = MagicMock()
    dp.start_polling = AsyncMock(side_effect=lambda *a, **k: calls.append("polling"))
    await start_polling(bot, dp)
    assert calls == ["webhook", "polling"]


async def test_worker_lifecycle_runs_retention_task(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle.db"
    queue = BoundedJobQueue(db_path)
    dp = create_dispatcher(
        {111},
        db_path,
        job_queue=queue,
        reports_dir=tmp_path,
        retention=RetentionPolicy(),
    )
    await start_queue_worker(dp, queue, None)
    try:
        assert "retention_task" in dp.workflow_data
        task = dp.workflow_data["retention_task"]
        assert not task.done()
    finally:
        await stop_queue_worker(dp, queue)
    assert task.done()


async def test_reconcile_fallback_cancels_queued_and_fails_active(tmp_path: Path) -> None:
    from types import SimpleNamespace as NS

    db_path = tmp_path / "reconcile.db"
    async with open_db(db_path) as conn:
        queued = await enqueue_request(conn, 1, "q1")
        active_ref = await enqueue_request(conn, 2, "q2")
        await set_state(conn, active_ref.job_id, JobState.ACTIVE)
        await conn.commit()
    await _reconcile_persisted_jobs(NS(db_path=db_path))  # type: ignore[arg-type]
    async with open_db(db_path) as conn:
        crow = await JobRepository().get(conn, queued.job_id)
        arow = await JobRepository().get(conn, active_ref.job_id)
        assert crow is not None and crow["state"] == "cancelled"
        assert arow is not None and arow["state"] == "failed"


# --- routing matrix ---------------------------------------------------------


async def test_leading_slash_plaintext_shows_usage(tmp_path: Path) -> None:
    db_path = tmp_path / "slash.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/unknowncommand do things", "en")
        await plaintext_handler(message, conn=conn)
        assert "Usage" in message.answer.call_args[0][0]
        assert await _job_count(conn) == 0


async def test_caption_routes_like_research(tmp_path: Path) -> None:
    db_path = tmp_path / "caption.db"
    async with open_db(db_path) as conn:
        message = _msg(111, None, "en", caption="caption query")
        message.text = None
        await plaintext_handler(message, conn=conn)
        assert "received" in message.answer.call_args_list[0][0][0]
        assert await _job_count(conn) == 1


async def test_researcher_is_not_research(tmp_path: Path) -> None:
    db_path = tmp_path / "researcher.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/researcher query", "en")
        await research_handler(message, conn=conn)
        assert "Usage" in message.answer.call_args[0][0]
        assert await _job_count(conn) == 0


async def test_research_command_is_case_insensitive(tmp_path: Path) -> None:
    db_path = tmp_path / "case.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/RESEARCH case query", "en")
        await research_handler(message, conn=conn)
        assert "received" in message.answer.call_args_list[0][0][0]


async def test_research_at_mention_routes(tmp_path: Path) -> None:
    db_path = tmp_path / "mention.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/research@mybot mention query", "en")
        await research_handler(message, conn=conn)
        assert "received" in message.answer.call_args_list[0][0][0]


async def test_uppercase_scheme_url_uses_too_long_branch(tmp_path: Path) -> None:
    db_path = tmp_path / "upper.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "/research HTTPS://example.com/" + "A" * 2500, "en")
        await research_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "4000" in reply
        assert await _job_count(conn) == 0


async def test_pure_arabic_text_enqueues(tmp_path: Path) -> None:
    db_path = tmp_path / "ar.db"
    async with open_db(db_path) as conn:
        message = _msg(111, "ما هي أحدث التطورات في الطاقة الشمسية؟", "ar")
        await plaintext_handler(message, conn=conn)
        assert "استلام" in message.answer.call_args_list[0][0][0]
        assert await _job_count(conn) == 1


@pytest.mark.parametrize(
    "scheme",
    ["javascript", "vbscript", "mailto", "tel", "ssh", "ws", "wss", "gopher", "ldap", "dict"],
)
async def test_blocked_schemes_rejected(tmp_path: Path, scheme: str) -> None:
    db_path = tmp_path / f"scheme_{scheme}.db"
    async with open_db(db_path) as conn:
        message = _msg(111, f"/research {scheme}:something", "en")
        await research_handler(message, conn=conn)
        assert "invalid" in message.answer.call_args[0][0].lower()
        assert await _job_count(conn) == 0


async def test_url_length_boundaries(tmp_path: Path) -> None:
    base = "https://example.com/"
    ok_url = base + "a" * (2000 - len(base))
    long_url = base + "a" * (2001 - len(base))
    assert len(ok_url) == 2000
    db_path = tmp_path / "bounds.db"
    async with open_db(db_path) as conn:
        first = _msg(111, f"/research {ok_url}", "en")
        await research_handler(first, conn=conn)
        assert "received" in first.answer.call_args_list[0][0][0]
    db_path2 = tmp_path / "bounds2.db"
    async with open_db(db_path2) as conn:
        second = _msg(111, f"/research {long_url}", "en")
        await research_handler(second, conn=conn)
        assert "4000" in second.answer.call_args[0][0]
        assert await _job_count(conn) == 0


# --- unavailable paths (no persistence configured) ---------------------------


async def test_handlers_without_persistence_are_unavailable() -> None:
    cases = [
        (status_handler, "/status"),
        (cancel_handler, "/cancel"),
        (history_handler, "/history"),
        (forget_handler, "/forget"),
        (language_handler, "/language"),
        (report_handler, "/report r1"),
    ]
    for handler, text in cases:
        message = _msg(111, text, "en")
        await handler(message, conn=None, db_path=None)
        reply = message.answer.call_args[0][0]
        assert "unavailable" in reply.lower() or "try again" in reply.lower()


async def test_non_text_needs_no_persistence() -> None:
    message = _msg(111, None, "en")
    message.text = None
    await non_text_handler(message, conn=None, db_path=None)
    assert "only handle text" in message.answer.call_args[0][0]


async def test_language_flows_without_persistence() -> None:
    current = _msg(111, "/language", "en")
    await language_handler(current, conn=None, db_path=None)
    assert "unavailable" in current.answer.call_args[0][0].lower()
    # Invalid arguments are rejected before any DB access.
    invalid = _msg(111, "/language xx", "en")
    await language_handler(invalid, conn=None, db_path=None)
    assert "invalid" in invalid.answer.call_args[0][0].lower()


async def test_status_none_and_report_usage(tmp_path: Path) -> None:
    db_path = tmp_path / "status_none.db"
    async with open_db(db_path) as conn:
        none_msg = _msg(111, "/status", "en")
        await status_handler(none_msg, conn=conn)
        assert "no active" in none_msg.answer.call_args[0][0].lower()
        usage_msg = _msg(111, "/report", "en")
        await report_handler(usage_msg, conn=conn)
        assert "Usage" in usage_msg.answer.call_args[0][0]
        report_none = _msg(111, "/report ghost", "en")
        await report_handler(report_none, conn=conn)
        assert "not found" in report_none.answer.call_args[0][0].lower()


# --- group privacy -----------------------------------------------------------


@pytest.mark.parametrize(
    "handler_name",
    [
        "status_handler",
        "cancel_handler",
        "history_handler",
        "report_handler",
        "forget_handler",
        "language_handler",
        "non_text_handler",
    ],
)
async def test_group_operations_rejected_in_groups(tmp_path: Path, handler_name: str) -> None:
    import research_agent.telegram.handlers as handlers_module

    db_path = tmp_path / f"group_{handler_name}.db"
    async with open_db(db_path) as conn:
        handler = getattr(handlers_module, handler_name)
        text = None if handler_name == "non_text_handler" else "/cmd"
        message = _msg(111, text, "en", chat_type="group")
        if text is None:
            message.text = None
        await handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "private" in reply.lower()


async def test_start_help_in_groups_still_answer(tmp_path: Path) -> None:
    from research_agent.telegram.handlers import help_handler

    db_path = tmp_path / "group_start.db"
    async with open_db(db_path) as conn:
        start_msg = _msg(111, "/start", "en", chat_type="group")
        await start_handler(start_msg, conn=conn)
        assert start_msg.answer.call_args[0][0]
        help_msg = _msg(111, "/help", "en", chat_type="group")
        await help_handler(help_msg, conn=conn)
        assert help_msg.answer.call_args[0][0]


# --- report handler branches -------------------------------------------------


async def _seed_single_source_report(
    conn, user_id: int, job_id: str, report_id: str, tools: object, markdown: str = "missing.md"
) -> None:
    await JobRepository().create(conn, job_id=job_id, user_id=user_id, query="q")
    await ReportRepository().save(
        conn,
        report_id=report_id,
        job_id=job_id,
        topic="T",
        summary="Summary text",
        markdown_path=markdown,
        tools_used=tools,  # type: ignore[arg-type]
    )
    await SourceRepository().save_many(
        conn,
        report_id,
        [
            {
                "source_ref": 1,
                "title": "A",
                "url": "https://example.com/a",
                "publisher": None,
                "published_at": None,
                "accessed_at": datetime.now(UTC).isoformat(),
                "source_type": "web",
            }
        ],
    )
    await conn.commit()


async def test_report_malformed_tools_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "tools_garbage.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j-g", user_id=111, query="q")
        await conn.execute(
            "INSERT INTO reports (report_id, job_id, topic, summary, markdown_path,"
            " tools_used, created_at) VALUES ('r-g','j-g','T','Summary text','missing.md',"
            " 'not-json',?)",
            (datetime.now(UTC).isoformat(),),
        )
        await SourceRepository().save_many(
            conn,
            "r-g",
            [
                {
                    "source_ref": 1,
                    "title": "A",
                    "url": "https://example.com/a",
                    "publisher": None,
                    "published_at": None,
                    "accessed_at": datetime.now(UTC).isoformat(),
                    "source_type": "web",
                }
            ],
        )
        await conn.commit()
        message = _msg(111, "/report r-g", "en")
        await report_handler(message, conn=conn)
        reply = message.answer.call_args[0][0]
        assert "Summary" in reply
        assert "Tools" not in reply


async def test_report_served_from_reports_dir(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "research-r-dir1.md").write_text("# T\n\nbody\n", encoding="utf-8")
    db_path = tmp_path / "dir.db"
    async with open_db(db_path) as conn:
        await _seed_single_source_report(conn, 111, "j-d", "r-dir1", [], markdown="")
        message = _msg(111, "/report r-dir1", "en")
        await report_handler(message, conn=conn, reports_dir=reports_dir)
        message.answer_document.assert_awaited_once()


# --- queue helpers -----------------------------------------------------------


async def test_queue_position_helpers_and_aliases(tmp_path: Path) -> None:
    db_path = tmp_path / "helpers.db"
    queue = BoundedJobQueue(db_path)
    assert queue.db_path == Path(db_path)
    assert queue.max_concurrent_jobs == 3
    assert queue.pending == 0
    assert await queue.queue_position("ghost") == 0
    async with open_db(db_path) as conn:
        with pytest.raises(ValueError, match="unknown job"):
            await _set_job_deadline(conn, "ghost", 300)
        assert await get_user_latest_job_in_state(conn, 111, "cancelled") is None
        assert await cancel_user_job(conn, 111) is False
    assert _parse_timestamp(123) is None
    assert _parse_timestamp("garbage") is None
    assert _deadline_from_created_at("garbage", 60) > datetime.now(UTC)
    with pytest.raises(ValueError):
        _coerce_enqueue_args("xx", None)
    with pytest.raises(ValueError):
        _coerce_enqueue_args("en", "bad id!")
    async with open_db(db_path) as conn:
        await _check_daily_quota(conn, 111, 0)
    set_default_queue(queue)
    try:
        assert get_queue() is queue
    finally:
        set_default_queue(None)
    with pytest.raises(ValueError, match="unknown job"):
        await queue.put(JobRef(job_id="ghost", user_id=1, query="q"))
    await queue.stop()


async def test_put_rejects_terminal_and_foreign_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "put.db"
    queue = BoundedJobQueue(db_path)
    async with open_db(db_path) as conn:
        ref = await enqueue_request(conn, 111, "q")
        await set_state(conn, ref.job_id, JobState.CANCELLED)
        await conn.commit()
    with pytest.raises(ValueError, match="terminal"):
        await queue.put(JobRef(job_id=ref.job_id, user_id=111, query="q"))
    with pytest.raises(ValueError, match="another user"):
        await queue.put(JobRef(job_id=ref.job_id, user_id=222, query="q"))
    await queue.stop()


async def test_from_settings_uses_configured_paths(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "s.db"))
    queue = BoundedJobQueue.from_settings(settings)
    assert queue.db_path == tmp_path / "s.db"
    assert queue.max_concurrent_jobs == 3
    await queue.stop()


# --- recovery variants -------------------------------------------------------


async def test_recover_queue_prefers_hook_then_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "hook.db"
    queue = BoundedJobQueue(db_path)
    async with open_db(db_path) as conn:
        ref = await enqueue_request(conn, 1, "q1")
        await conn.commit()
    await _recover_queue(queue)
    async with open_db(db_path) as conn:
        row = await JobRepository().get(conn, ref.job_id)
        assert row is not None and row["state"] == "cancelled"
    await queue.stop()


async def test_recover_queue_uses_legacy_hook_name(tmp_path: Path) -> None:
    db_path = tmp_path / "sync_hook.db"
    async with open_db(db_path) as conn:
        ref = await enqueue_request(conn, 1, "q1")
        await conn.commit()
    calls: list[str] = []

    class LegacyQueue:
        def reconcile(self) -> None:
            calls.append("reconcile")

    await _recover_queue(LegacyQueue())  # type: ignore[arg-type]
    assert calls == ["reconcile"]
    async with open_db(db_path) as conn:
        row = await JobRepository().get(conn, ref.job_id)
        assert row is not None and row["state"] == "queued"


# --- legacy migration --------------------------------------------------------


async def test_legacy_database_gains_source_url_column(tmp_path: Path) -> None:
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    raw = await aiosqlite.connect(str(db_path))
    try:
        await raw.execute("PRAGMA journal_mode=WAL;")
        # A realistic pre-source_url schema: all current columns except
        # source_url (and deadline_at, to exercise both migrations).
        await raw.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,"
            " query TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'mixed',"
            " domain TEXT NOT NULL DEFAULT 'general', depth TEXT NOT NULL DEFAULT 'standard',"
            " risk_level TEXT NOT NULL DEFAULT 'normal', state TEXT NOT NULL DEFAULT 'queued',"
            " repair_count INTEGER NOT NULL DEFAULT 0, trace_id TEXT, error TEXT,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        await raw.execute(
            "INSERT INTO jobs (job_id, user_id, query, language, state, created_at,"
            " updated_at) VALUES ('old-1', 7, 'legacy query', 'en', 'completed',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        await raw.commit()
    finally:
        await raw.close()
    async with open_db(db_path) as conn:
        cursor = await conn.execute("PRAGMA table_info('jobs')")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        assert "source_url" in columns
        assert "deadline_at" in columns
        row = await JobRepository().get(conn, "old-1")
        assert row is not None and str(row["query"]) == "legacy query"
        await JobRepository().create(
            conn, job_id="new-1", user_id=7, query="q", source_url="https://example.com/x"
        )
        await conn.commit()
        fresh = await JobRepository().get(conn, "new-1")
        assert fresh is not None and fresh["source_url"] == "https://example.com/x"


# --- repositories edges ------------------------------------------------------


async def test_session_get_valid_rejects_garbage_timestamp(tmp_path: Path) -> None:
    db_path = tmp_path / "sess_garbage.db"
    async with open_db(db_path) as conn:
        await UserRepository().upsert(conn, 5, "en")
        await conn.execute(
            "INSERT INTO sessions (user_id, language, interactions, updated_at)"
            " VALUES (5, 'en', '[]', 'not-a-timestamp')"
        )
        await conn.commit()
        assert await SessionRepository().get_valid(conn, 5) is None


async def test_session_append_resets_corrupt_data(tmp_path: Path) -> None:
    db_path = tmp_path / "sess_corrupt.db"
    async with open_db(db_path) as conn:
        await UserRepository().upsert(conn, 6, "en")
        await conn.execute(
            "INSERT INTO sessions (user_id, language, interactions, updated_at)"
            " VALUES (6, 'en', '{broken', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        await conn.commit()
        await SessionRepository().append(conn, 6, "en", "hello")
        row = await SessionRepository().get(conn, 6)
        assert row is not None
        assert json.loads(str(row["interactions"])) == ["hello"]


async def test_report_search_empty_and_cache_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "search.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j1", user_id=1, query="q")
        await ReportRepository().save(
            conn,
            report_id="r1",
            job_id="j1",
            topic="Deep sea vents",
            summary="Hydrothermal facts",
            markdown_path="p",
            tools_used=[],
        )
        await conn.commit()
        assert await ReportRepository().search(conn, "zebra-no-match") == []
        assert [r["report_id"] for r in await ReportRepository().search(conn, "vents")] == ["r1"]
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        await CacheRepository().set(conn, "k", "v", future)
        await conn.commit()
        assert await CacheRepository().get(conn, "k") == "v"
        assert await CacheRepository().delete_expired(conn) == 0


async def test_reports_service_limit_clamp(tmp_path: Path) -> None:
    db_path = tmp_path / "clamp.db"
    async with open_db(db_path) as conn:
        await JobRepository().create(conn, job_id="j1", user_id=1, query="q")
        for index in range(3):
            await ReportRepository().save(
                conn,
                report_id=f"r{index}",
                job_id="j1",
                topic=f"T{index}",
                summary="S",
                markdown_path="p",
                tools_used=[],
            )
        await conn.commit()
        assert len(await list_recent_reports(conn, 1, limit=-5)) == 1
        assert len(await list_recent_reports(conn, 1, limit=2)) == 2
        assert await list_recent_reports(conn, 2, limit=5) == []
