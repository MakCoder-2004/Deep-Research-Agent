"""Bounded async research-job queue for the Telegram gateway (M2.15+)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from research_agent.config import Settings
from research_agent.models import JobState
from research_agent.observability.redaction import redact_text
from research_agent.persistence.database import open_db
from research_agent.persistence.repositories import JobRepository

logger = logging.getLogger(__name__)


class UserBusyError(Exception):
    """Raised when a user already has an active (queued/active) job."""

    def __init__(self, user_id: int, job_id: str = "") -> None:
        super().__init__(f"user {user_id} already has an active job {job_id}".strip())
        self.user_id = user_id
        self.job_id = job_id


@dataclass
class JobRef:
    """Lightweight reference to an enqueued research job."""

    job_id: str
    user_id: int
    query: str
    position: int = 1


async def _insert_job(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    language: str = "mixed",
) -> JobRef:
    """Insert one queued job on an open connection (shared enqueue core).

    Enforces one active job per user; raises :class:`UserBusyError` when the
    user already has a queued/active job. Computes FIFO position, commits,
    and returns the :class:`JobRef`. Callers reuse the same connection for
    check+insert so the two steps stay in one transaction scope; a UNIQUE
    partial index backs the check so concurrent writers on separate
    connections still serialize to one winner via ``IntegrityError``.
    """
    await _ensure_one_active_index(conn)
    if await has_active_for_user(conn, user_id):
        existing = await get_user_active_job(conn, user_id)
        existing_id = str(existing["job_id"]) if existing is not None else ""
        raise UserBusyError(user_id, existing_id)
    job_id = str(uuid4())
    try:
        await JobRepository().create(
            conn,
            job_id=job_id,
            user_id=user_id,
            query=query,
            language=language,
        )
    except sqlite3.IntegrityError as exc:
        # Concurrent winner committed first (partial unique index).
        try:
            existing = await get_user_active_job(conn, user_id)
        except Exception:  # noqa: BLE001 - error path must still raise busy
            existing = None
        if existing is not None:
            raise UserBusyError(user_id, str(existing["job_id"])) from exc
        raise UserBusyError(user_id, "") from exc
    except sqlite3.OperationalError as exc:
        # "database is locked" while another writer holds the lock.
        if "locked" in str(exc).lower():
            try:
                existing = await get_user_active_job(conn, user_id)
            except Exception:  # noqa: BLE001 - error path must still raise busy
                existing = None
            if existing is not None:
                raise UserBusyError(user_id, str(existing["job_id"])) from exc
        raise
    position = await queue_position(conn, job_id)
    await conn.commit()
    return JobRef(job_id=job_id, user_id=user_id, query=query, position=position)


_ONE_ACTIVE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_one_active_per_user "
    "ON jobs(user_id) WHERE state IN ('queued', 'active')"
)


async def _ensure_one_active_index(conn: aiosqlite.Connection) -> None:
    """Create the partial unique index backing atomic 1-per-user enqueue."""
    try:
        await conn.execute(_ONE_ACTIVE_INDEX_SQL)
    except sqlite3.OperationalError:
        # Concurrent CREATE INDEX or locked writer; the other connection
        # creates it. Proceed - INSERT still enforces via existing index.
        pass


async def enqueue_request(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    language: str = "mixed",
) -> JobRef:
    """Persist a queued job and return its reference with FIFO position.

    Enforces one active job per user (``max_active_per_user=1``); raises
    :class:`UserBusyError` when the user already has a queued/active job.
    NOTE: daily quotas are M9 scope (stub only); not enforced here.
    """
    return await _insert_job(conn, user_id, query, language)


async def get_user_active_job(conn: aiosqlite.Connection, user_id: int) -> aiosqlite.Row | None:
    """Return the oldest queued/active job for a user, if any."""
    cursor = await conn.execute(
        """
        SELECT * FROM jobs
        WHERE user_id = ? AND state IN ('queued', 'active')
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (user_id,),
    )
    return await cursor.fetchone()


async def has_active_for_user(conn: aiosqlite.Connection, user_id: int) -> bool:
    """Return True when the user has a queued/active job (max 1 active)."""
    return await get_user_active_job(conn, user_id) is not None


async def queue_position(conn: aiosqlite.Connection, job_id: str) -> int:
    """Return 1-indexed FIFO position counting older queued jobs (0 if not queued)."""
    cursor = await conn.execute(
        "SELECT job_id, state, created_at, rowid FROM jobs WHERE job_id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    if row is None or row["state"] != "queued":
        return 0
    created_at = row["created_at"]
    try:
        rowid = int(row["rowid"])
    except (KeyError, TypeError, ValueError):
        rowid = 0
    if rowid:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS n FROM jobs
            WHERE state = 'queued'
              AND (created_at < ? OR (created_at = ? AND rowid <= ?))
            """,
            (created_at, created_at, rowid),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS n FROM jobs
            WHERE state = 'queued' AND created_at <= ?
            """,
            (created_at,),
        )
    count_row = await cursor.fetchone()
    if count_row is None:
        return 1
    try:
        return max(1, int(count_row["n"]))
    except (TypeError, ValueError):
        return 1


_CANCEL_EVENTS: dict[str, asyncio.Event] = {}
_MAX_CANCEL_EVENTS = 1024

_DEFAULT_QUEUE: BoundedJobQueue | None = None


def get_cancel_event(job_id: str) -> asyncio.Event:
    """Return (creating if needed) the cooperative cancellation event.

    The registry is bounded to ``_MAX_CANCEL_EVENTS`` entries; when full the
    oldest entry is evicted to avoid unbounded growth from many jobs.
    """
    event = _CANCEL_EVENTS.get(job_id)
    if event is None:
        if len(_CANCEL_EVENTS) >= _MAX_CANCEL_EVENTS:
            try:
                oldest = next(iter(_CANCEL_EVENTS))
            except StopIteration:
                oldest = None
            if oldest is not None:
                _CANCEL_EVENTS.pop(oldest, None)
        event = asyncio.Event()
        _CANCEL_EVENTS[job_id] = event
    return event


def clear_cancel_event(job_id: str) -> None:
    """Remove a cancellation event from the registry."""
    _CANCEL_EVENTS.pop(job_id, None)


def clear_all_cancel_events() -> None:
    """Remove all cancellation events (used on queue stop)."""
    _CANCEL_EVENTS.clear()


def cancel_event_count() -> int:
    """Return the number of tracked cancellation events (for tests)."""
    return len(_CANCEL_EVENTS)


def set_default_queue(queue: BoundedJobQueue | None) -> None:
    """Register the live queue instance for handler wiring."""
    global _DEFAULT_QUEUE
    _DEFAULT_QUEUE = queue


def get_default_queue() -> BoundedJobQueue | None:
    """Return the registered live queue instance, if any."""
    return _DEFAULT_QUEUE


# Aliases for handler wiring compatibility.
def set_queue(queue: BoundedJobQueue | None) -> None:
    """Alias of :func:`set_default_queue` for handler wiring."""
    set_default_queue(queue)


def get_queue() -> BoundedJobQueue | None:
    """Alias of :func:`get_default_queue` for handler wiring."""
    return get_default_queue()


def set_job_queue(queue: BoundedJobQueue | None) -> None:
    """Alias of :func:`set_default_queue` for handler wiring."""
    set_default_queue(queue)


def get_job_queue() -> BoundedJobQueue | None:
    """Alias of :func:`get_default_queue` for handler wiring."""
    return get_default_queue()


_ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.ACTIVE, JobState.CANCELLED, JobState.FAILED},
    JobState.ACTIVE: {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED},
}


async def set_state(
    conn: aiosqlite.Connection,
    job_id: str,
    state: JobState | str,
    *,
    error: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Persist a job lifecycle transition via JobRepository.

    Allows ``queued -> active -> completed|failed|cancelled`` plus
    ``queued -> cancelled`` and ``queued -> failed`` (timeout while queued);
    same-state is a no-op. Sanitizes ``error``
    with redaction, refreshes ``updated_at`` via the repository, and
    commits. Raises ``ValueError`` for unknown jobs or illegal moves.
    """
    target = JobState(state)
    current_row = await JobRepository().get(conn, job_id)
    if current_row is None:
        raise ValueError(f"unknown job {job_id}")
    try:
        current = JobState(str(current_row["state"]))
    except ValueError as exc:
        raise ValueError(f"unknown current state for job {job_id}") from exc
    if target != current:
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(f"illegal job transition {current.value} -> {target.value}")
    sanitized_error = redact_text(error) if error else None
    await JobRepository().update_state(
        conn, job_id, target, error=sanitized_error, trace_id=trace_id
    )
    await conn.commit()
    if target in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
        # Terminal states no longer need cooperative cancellation state.
        clear_cancel_event(job_id)


async def cancel_user_job(conn: aiosqlite.Connection, user_id: int) -> bool:
    """Cooperatively cancel the user's active job; persist cancelled state."""
    job = await get_user_active_job(conn, user_id)
    if job is None:
        return False
    job_id = str(job["job_id"])
    await set_state(conn, job_id, JobState.CANCELLED)
    get_cancel_event(job_id).set()
    return True


class BoundedJobQueue:
    """FIFO bounded queue limiting globally concurrent research jobs.

    Global concurrency is bounded by an ``asyncio.Semaphore`` sized from
    ``Settings.max_concurrent_jobs`` (default 3). FIFO order comes from an
    ``asyncio.Queue`` plus ``queue_position`` counting older queued rows.
    Live job tasks and cooperative cancel events are tracked in dicts.

    NOTE (M9 scope): daily quotas (10 requests/user/day, 3 deep/day)
    are intentionally not enforced here; see BudgetManager in M9.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        max_concurrent_jobs: int = 3,
        job_timeout_seconds: int = 300,
    ) -> None:
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be >= 1.")
        if job_timeout_seconds < 1:
            raise ValueError("job_timeout_seconds must be >= 1.")
        self._db_path = Path(db_path)
        self._max_concurrent_jobs = max_concurrent_jobs
        self._job_timeout_seconds = job_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._queue: asyncio.Queue[JobRef] = asyncio.Queue()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        self._bot: Any | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, db_path: Path | str | None = None
    ) -> BoundedJobQueue:
        """Build a queue with Semaphore sized from settings.max_concurrent_jobs."""
        path = db_path if db_path is not None else settings.database_path
        return cls(
            path,
            max_concurrent_jobs=int(settings.max_concurrent_jobs),
            job_timeout_seconds=int(settings.job_timeout_seconds),
        )

    @property
    def db_path(self) -> Path:
        """Return the SQLite path backing job persistence."""
        return self._db_path

    @property
    def max_concurrent_jobs(self) -> int:
        """Return the global concurrency limit."""
        return self._max_concurrent_jobs

    @property
    def pending(self) -> int:
        """Return the number of jobs waiting in the FIFO queue."""
        return self._queue.qsize()

    def set_bot(self, bot: Any) -> None:
        """Attach the aiogram Bot used for progress edits (optional)."""
        self._bot = bot

    async def enqueue(self, user_id: int, query: str, language: str = "mixed") -> JobRef:
        """Persist a queued job and schedule it FIFO; return its reference.

        Enforces one active job per user; raises :class:`UserBusyError`
        when the user already has a queued/active job.
        """
        # NOTE: daily quotas are M9 scope (stub only); not enforced here.
        async with open_db(self._db_path) as conn:
            ref = await _insert_job(conn, user_id, query, language)
        get_cancel_event(ref.job_id)
        await self._queue.put(ref)
        logger.info(
            "job enqueued job_id=%s user_id=%d position=%d",
            ref.job_id,
            user_id,
            ref.position,
        )
        return ref

    async def start(self) -> None:
        """Start the background FIFO worker loop (idempotent)."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._running = True
        self._worker_task = asyncio.create_task(self.worker_loop())

    async def stop(self) -> None:
        """Stop the worker loop and cancel tracked job tasks."""
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        # Avoid leaking cooperative cancellation state across restarts.
        clear_all_cancel_events()

    async def worker_loop(self) -> None:
        """Consume FIFO jobs; each runs bounded by the global semaphore."""
        self._running = True
        while self._running:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break
            task = asyncio.create_task(self._run_with_semaphore(job))
            self._tasks[job.job_id] = task
            task.add_done_callback(self._make_done_callback(job.job_id))
            # Avoid unbounded task growth: yield control each dispatch.
            await asyncio.sleep(0)

    def _make_done_callback(self, job_id: str) -> Callable[[asyncio.Task[None]], None]:
        """Return a done-callback removing a finished job task."""

        def _done(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(job_id, None)

        return _done

    async def _run_with_semaphore(self, job: JobRef) -> None:
        """Run one placeholder job while holding the global semaphore slot."""
        async with self._semaphore:
            try:
                await asyncio.wait_for(
                    self.run_placeholder(job), timeout=float(self._job_timeout_seconds)
                )
            except TimeoutError:
                logger.warning("job timed out job_id=%s", redact_text(job.job_id))
                try:
                    async with open_db(self._db_path) as conn:
                        await set_state(conn, job.job_id, JobState.FAILED, error="job timeout")
                except ValueError:
                    # Already terminal (e.g. cancelled during timeout); keep it.
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - placeholder must not crash worker
                sanitized = redact_text(f"{type(exc).__name__}: {exc}")
                logger.warning("job failed job_id=%s error=%s", job.job_id, sanitized)
                try:
                    async with open_db(self._db_path) as conn:
                        await set_state(conn, job.job_id, JobState.FAILED, error=sanitized)
                except Exception:  # noqa: BLE001, S110 - persistence best effort
                    pass
            finally:
                self._queue.task_done()
                clear_cancel_event(job.job_id)

    async def run_placeholder(self, job: JobRef) -> None:
        """Placeholder execution cycling the single progress message.

        Transitions queued -> active -> completed|cancelled, editing the
        same Telegram message through Analyzing/Selecting/Searching/
        Reading/Checking/Preparing. Honors cooperative cancellation.
        """
        from research_agent.telegram.progress import (
            ProgressStage,
            get_progress,
            update_progress,
        )

        cancel_event = get_cancel_event(job.job_id)
        async with open_db(self._db_path) as conn:
            await set_state(conn, job.job_id, JobState.ACTIVE)
        if cancel_event.is_set():
            async with open_db(self._db_path) as conn:
                try:
                    await set_state(conn, job.job_id, JobState.CANCELLED)
                except ValueError:
                    pass
            return
        cancelled = False
        for stage in list(ProgressStage):
            if cancel_event.is_set():
                cancelled = True
                break
            if self._bot is not None:
                entry = get_progress(job.job_id)
                if entry is not None:
                    chat_id, message_id = entry
                    try:
                        await update_progress(
                            self._bot,
                            chat_id,
                            message_id,
                            stage,
                            "en",
                            job_id=job.job_id,
                        )
                    except Exception as exc:  # noqa: BLE001 - never fail job on edit
                        logger.warning(
                            "progress edit failed job_id=%s error=%s",
                            job.job_id,
                            redact_text(f"{type(exc).__name__}: {exc}"),
                        )
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=0.01)
                cancelled = True
                break
            except TimeoutError:
                continue
        async with open_db(self._db_path) as conn:
            try:
                if cancelled or cancel_event.is_set():
                    await set_state(conn, job.job_id, JobState.CANCELLED)
                else:
                    await set_state(conn, job.job_id, JobState.COMPLETED)
            except ValueError:
                # Terminal already (e.g. concurrent cancel); keep first state.
                pass
