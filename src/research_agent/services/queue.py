"""Bounded async research-job queue for the Telegram gateway (M2.15+)."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

_KNOWN_ENQUEUE_LANGUAGES = frozenset({"en", "ar", "mixed"})
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEFAULT_JOB_TIMEOUT_SECONDS = 300
_TERMINAL_STATES = frozenset({JobState.CANCELLED, JobState.COMPLETED, JobState.FAILED})


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
    request_id: str | None = None,
    *,
    job_timeout_seconds: int = _DEFAULT_JOB_TIMEOUT_SECONDS,
) -> JobRef:
    """Insert one queued job on an open connection (shared enqueue core).

    Enforces one active job per user; raises :class:`UserBusyError` when the
    user already has a queued/active job. Computes FIFO position, commits,
    and returns the :class:`JobRef`. Callers reuse the same connection for
    check+insert so the two steps stay in one transaction scope; a UNIQUE
    partial index backs the check so concurrent writers on separate
    connections still serialize to one winner via ``IntegrityError``.

    ``request_id``, when given, is used as the ``job_id`` for idempotent
    handler wiring; otherwise a UUID is generated.
    """
    language, request_id = _coerce_enqueue_args(language, request_id)
    if request_id is not None:
        existing_row = await JobRepository().get(conn, request_id)
        if existing_row is not None:
            ref = await _existing_request_ref(conn, existing_row, user_id, request_id)
            await _set_job_deadline(conn, request_id, job_timeout_seconds)
            await conn.commit()
            return ref
    if await has_active_for_user(conn, user_id):
        existing = await get_user_active_job(conn, user_id)
        existing_id = str(existing["job_id"]) if existing is not None else ""
        raise UserBusyError(user_id, existing_id)
    job_id = request_id if request_id else str(uuid4())
    try:
        await JobRepository().create(
            conn,
            job_id=job_id,
            user_id=user_id,
            query=query,
            language=language,
        )
    except sqlite3.IntegrityError as exc:
        # A concurrent idempotent retry may have committed this request ID.
        dup = await JobRepository().get(conn, job_id)
        if dup is not None:
            if request_id is None:
                raise
            ref = await _existing_request_ref(conn, dup, user_id, request_id)
            await _set_job_deadline(conn, request_id, job_timeout_seconds)
            await conn.commit()
            return ref
        # Concurrent winner committed first (partial unique index).
        existing = await get_user_active_job(conn, user_id)
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
    await _set_job_deadline(conn, job_id, job_timeout_seconds)
    position = await queue_position(conn, job_id)
    await conn.commit()
    return JobRef(job_id=job_id, user_id=user_id, query=query, position=position)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a persisted timestamp as an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _deadline_from_created_at(created_at: object, timeout_seconds: int) -> datetime:
    """Calculate a deadline, failing closed for malformed legacy timestamps."""
    created = _parse_timestamp(created_at)
    if created is None:
        created = datetime.now(UTC)
    return created + timedelta(seconds=timeout_seconds)


async def _set_job_deadline(conn: aiosqlite.Connection, job_id: str, timeout_seconds: int) -> None:
    """Persist a deadline derived from the durable enqueue timestamp."""
    cursor = await conn.execute("SELECT created_at FROM jobs WHERE job_id = ?", (job_id,))
    row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"unknown job {job_id}")
    deadline = _deadline_from_created_at(row["created_at"], timeout_seconds).isoformat()
    await conn.execute(
        "UPDATE jobs SET deadline_at = COALESCE(deadline_at, ?) WHERE job_id = ?",
        (deadline, job_id),
    )


async def _existing_request_ref(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    user_id: int,
    request_id: str,
) -> JobRef:
    """Return an idempotent live request, rejecting collisions and requeues."""
    owner_id = int(row["user_id"])
    if owner_id != user_id:
        raise ValueError(f"request_id {request_id!r} belongs to another user")
    try:
        state = JobState(str(row["state"]))
    except ValueError as exc:
        raise ValueError(f"unknown state for request_id {request_id!r}") from exc
    if state not in {JobState.QUEUED, JobState.ACTIVE}:
        raise ValueError(f"request_id {request_id!r} is already terminal")
    return JobRef(
        job_id=request_id,
        user_id=owner_id,
        query=str(row["query"]),
        position=await queue_position(conn, request_id),
    )


def _coerce_enqueue_args(language: str, request_id: str | None) -> tuple[str, str | None]:
    """Validate the language and explicit request ID arguments."""
    if language not in _KNOWN_ENQUEUE_LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    if request_id is not None and (
        not isinstance(request_id, str) or _REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise ValueError("request_id must be 1-128 safe identifier characters")
    return language, request_id


async def enqueue_request(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    language: str = "mixed",
    request_id: str | None = None,
    *,
    job_timeout_seconds: int = _DEFAULT_JOB_TIMEOUT_SECONDS,
) -> JobRef:
    """Persist a queued job and return its reference with FIFO position.

    Enforces one active job per user (``max_active_per_user=1``); raises
    :class:`UserBusyError` when the user already has a queued/active job.
    ``request_id`` optionally fixes the ``job_id`` for idempotent retries.
    NOTE: daily quotas are M9 scope (stub only); not enforced here.
    """
    return await _insert_job(
        conn,
        user_id,
        query,
        language,
        request_id,
        job_timeout_seconds=job_timeout_seconds,
    )


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
# Jobs whose worker still holds the event. These entries must survive a
# terminal state transition until the worker has observed cancellation.
_CANCEL_EVENT_IN_USE: set[str] = set()
# Kept as a diagnostic threshold for callers/tests; live entries are never
# evicted. Workers remove their own entries when they finish.
_MAX_CANCEL_EVENTS = 1024

_DEFAULT_QUEUE: BoundedJobQueue | None = None


def get_cancel_event(job_id: str) -> asyncio.Event:
    """Return (creating if needed) the cooperative cancellation event.

    Events are keyed by job and cleaned when a job reaches a terminal state or
    its worker finishes. Live entries are never evicted, so cancellation keeps
    working even during high job churn.
    """
    event = _CANCEL_EVENTS.get(job_id)
    if event is None:
        event = asyncio.Event()
        _CANCEL_EVENTS[job_id] = event
    return event


def clear_cancel_event(job_id: str) -> None:
    """Remove a cancellation event from the registry."""
    if job_id in _CANCEL_EVENT_IN_USE:
        event = _CANCEL_EVENTS.get(job_id)
        if event is not None:
            event.set()
        return
    _CANCEL_EVENTS.pop(job_id, None)


def clear_all_cancel_events() -> None:
    """Remove events that are not held by a running worker."""
    for job_id, event in list(_CANCEL_EVENTS.items()):
        event.set()
        if job_id not in _CANCEL_EVENT_IN_USE:
            _CANCEL_EVENTS.pop(job_id, None)


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
) -> bool:
    """Atomically persist a job lifecycle transition.

    Allows ``queued -> active -> completed|failed|cancelled`` plus
    ``queued -> cancelled`` and ``queued -> failed`` (timeout while queued);
    same-state is a no-op. Sanitizes ``error``
    with redaction, refreshes ``updated_at``, and commits. Returns whether a
    transition was applied. Cancellation of an already-terminal job is a
    normal no-op; unknown jobs and illegal non-cancellation moves raise
    ``ValueError``.
    """
    target = JobState(state)
    sanitized_error = redact_text(error) if error else None
    repository = JobRepository()
    for _ in range(3):
        current_row = await repository.get(conn, job_id)
        if current_row is None:
            raise ValueError(f"unknown job {job_id}")
        try:
            current = JobState(str(current_row["state"]))
        except ValueError as exc:
            raise ValueError(f"unknown current state for job {job_id}") from exc
        if target == current:
            return False
        if current in _TERMINAL_STATES:
            if target == JobState.CANCELLED:
                return False
            raise ValueError(f"illegal job transition {current.value} -> {target.value}")
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(f"illegal job transition {current.value} -> {target.value}")
        cursor = await conn.execute(
            """
            UPDATE jobs
            SET state = ?, error = ?, trace_id = ?, updated_at = ?
            WHERE job_id = ? AND state = ?
            """,
            (
                target.value,
                sanitized_error,
                trace_id,
                datetime.now(UTC).isoformat(),
                job_id,
                current.value,
            ),
        )
        if cursor.rowcount == 1:
            await conn.commit()
            if target in _TERMINAL_STATES:
                # Cancellation must wake the exact event already held by a worker.
                event = _CANCEL_EVENTS.get(job_id)
                if target == JobState.CANCELLED and event is not None:
                    event.set()
                if job_id not in _CANCEL_EVENT_IN_USE:
                    clear_cancel_event(job_id)
            return True
        # Another connection won the state race. Re-read and either retry a
        # still-legal transition or report the now-terminal outcome.
    raise RuntimeError(f"job state transition was repeatedly contended: {job_id}")


async def cancel_user_job(conn: aiosqlite.Connection, user_id: int) -> bool:
    """Cooperatively cancel the user's active job; persist cancelled state."""
    job = await get_user_active_job(conn, user_id)
    if job is None:
        return False
    job_id = str(job["job_id"])
    # Set the existing event before the state transition can clean it up.
    get_cancel_event(job_id).set()
    return await set_state(conn, job_id, JobState.CANCELLED)


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
        self._scheduled_job_ids: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        self._stopping = False
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

    async def enqueue(
        self,
        user_id: int,
        query: str,
        language: str = "mixed",
        request_id: str | None = None,
    ) -> JobRef:
        """Persist a queued job and schedule it FIFO; return its reference.

        Enforces one active job per user; raises :class:`UserBusyError`
        when the user already has a queued/active job. ``request_id``
        optionally fixes the ``job_id`` for idempotent handler wiring. The
        third positional argument remains the language.
        """
        # NOTE: daily quotas are M9 scope (stub only); not enforced here.
        async with open_db(self._db_path) as conn:
            ref = await _insert_job(
                conn,
                user_id,
                query,
                language,
                request_id,
                job_timeout_seconds=self._job_timeout_seconds,
            )
        await self.put(ref)
        logger.info(
            "job enqueued job_id=%s user_id=%d position=%d",
            ref.job_id,
            user_id,
            ref.position,
        )
        return ref

    async def put(self, job: JobRef) -> None:
        """Schedule a pre-persisted job reference FIFO (handler wiring compat)."""
        async with open_db(self._db_path) as conn:
            row = await JobRepository().get(conn, job.job_id)
            if row is None:
                raise ValueError(f"unknown job {job.job_id}")
            if int(row["user_id"]) != job.user_id:
                raise ValueError(f"job {job.job_id} belongs to another user")
            state = JobState(str(row["state"]))
            if state in {
                JobState.CANCELLED,
                JobState.COMPLETED,
                JobState.FAILED,
            }:
                raise ValueError(f"job {job.job_id} is already terminal")
            await _set_job_deadline(conn, job.job_id, self._job_timeout_seconds)
            await conn.commit()
        if state == JobState.ACTIVE or job.job_id in self._scheduled_job_ids:
            return
        self._scheduled_job_ids.add(job.job_id)
        get_cancel_event(job.job_id)
        try:
            await self._queue.put(job)
        except BaseException:
            self._scheduled_job_ids.discard(job.job_id)
            clear_cancel_event(job.job_id)
            raise

    def put_nowait(self, job: JobRef) -> None:
        """Non-blocking variant of :meth:`put` for handler wiring."""
        if job.job_id in self._scheduled_job_ids or job.job_id in self._tasks:
            return
        self._scheduled_job_ids.add(job.job_id)
        get_cancel_event(job.job_id)
        try:
            self._queue.put_nowait(job)
        except BaseException:
            self._scheduled_job_ids.discard(job.job_id)
            clear_cancel_event(job.job_id)
            raise

    def get_cancel_event(self, job_id: str) -> asyncio.Event:
        """Return the cooperative cancellation event for a job (instance helper)."""
        return get_cancel_event(job_id)

    async def queue_position(self, job_id: str) -> int:
        """Return 1-indexed FIFO position for a job (instance helper)."""
        async with open_db(self._db_path) as conn:
            return await queue_position(conn, job_id)

    async def recover_pending_jobs(self, *, exclude_job_ids: Collection[str] | None = None) -> int:
        """Terminally reconcile persisted jobs left by a prior worker.

        Queued jobs are cancelled because their in-memory FIFO position cannot
        be reconstructed safely. Active jobs are failed with a sanitized
        recovery reason. ``exclude_job_ids`` lets :meth:`start` preserve jobs
        already scheduled by this queue instance.
        """
        excluded = set(self._scheduled_job_ids if exclude_job_ids is None else exclude_job_ids)
        recovered = 0
        async with open_db(self._db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT job_id, state FROM jobs
                WHERE state IN ('queued', 'active')
                ORDER BY created_at ASC, rowid ASC
                """
            )
            rows = await cursor.fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                if job_id in excluded:
                    continue
                current = JobState(str(row["state"]))
                if current == JobState.QUEUED:
                    target = JobState.CANCELLED
                    reason = "job cancelled during queue recovery"
                else:
                    target = JobState.FAILED
                    reason = "job failed during queue recovery"
                if await set_state(conn, job_id, target, error=reason):
                    recovered += 1
        return recovered

    async def start(self) -> None:
        """Start the background FIFO worker loop (idempotent)."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        # Only reconcile rows not already scheduled by this instance. A caller
        # may enqueue before start, while a fresh process has no such IDs.
        await self.recover_pending_jobs()
        self._stopping = False
        self._running = True
        self._worker_task = asyncio.create_task(self.worker_loop())

    async def stop(self) -> None:
        """Stop workers and durably reconcile every remaining live job."""
        self._stopping = True
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        try:
            # Mark rows before cancelling semaphore waiters so a waiter cannot
            # acquire the slot and turn a queued job into active during stop.
            await self.recover_pending_jobs(exclude_job_ids=())
        except Exception as exc:  # noqa: BLE001 - retry after task cancellation
            logger.error(
                "queue shutdown recovery failed error=%s",
                redact_text(f"{type(exc).__name__}: {exc}"),
            )
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
        self._scheduled_job_ids.clear()
        try:
            await self.recover_pending_jobs(exclude_job_ids=())
        except Exception as exc:  # noqa: BLE001 - leave rows for next startup recovery
            logger.error(
                "queue shutdown recovery failed error=%s",
                redact_text(f"{type(exc).__name__}: {exc}"),
            )
        finally:
            # All tracked tasks have been awaited, so no live worker can lose
            # its cancellation event at this point.
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

        def _done(task: asyncio.Task[None]) -> None:
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - callbacks must consume task failures
                logger.error(
                    "job task failed after queue handling error job_id=%s error=%s",
                    redact_text(job_id),
                    redact_text(f"{type(exc).__name__}: {exc}"),
                )
            if self._tasks.get(job_id) is task:
                self._tasks.pop(job_id, None)
                self._scheduled_job_ids.discard(job_id)

        return _done

    async def _prepare_job(self, job: JobRef) -> datetime | None:
        """Return a live job deadline, failing an expired job before execution."""
        async with open_db(self._db_path) as conn:
            row = await JobRepository().get(conn, job.job_id)
            if row is None:
                return None
            try:
                current = JobState(str(row["state"]))
            except ValueError:
                logger.error("job has unknown persisted state job_id=%s", redact_text(job.job_id))
                return None
            if current in _TERMINAL_STATES:
                return None
            deadline = _parse_timestamp(row["deadline_at"])
            if deadline is None:
                deadline = _deadline_from_created_at(row["created_at"], self._job_timeout_seconds)
            if deadline <= datetime.now(UTC):
                try:
                    await set_state(conn, job.job_id, JobState.FAILED, error="job timeout")
                except ValueError:
                    # Cancellation or completion won the race.
                    pass
                return None
            return deadline

    async def _persist_terminal_state(
        self, job_id: str, state: JobState, *, error: str | None = None
    ) -> bool:
        """Persist a terminal state without allowing a DB outage to kill a task."""
        try:
            async with open_db(self._db_path) as conn:
                return await set_state(conn, job_id, state, error=error)
        except ValueError:
            # A concurrent terminal transition is expected and durable.
            return False
        except Exception as exc:  # noqa: BLE001 - restart recovery handles the old state
            logger.error(
                "job terminal state persistence failed job_id=%s error=%s",
                redact_text(job_id),
                redact_text(f"{type(exc).__name__}: {exc}"),
            )
            return False

    async def _run_with_semaphore(self, job: JobRef) -> None:
        """Run one job within the deadline measured from enqueue time."""
        _CANCEL_EVENT_IN_USE.add(job.job_id)
        get_cancel_event(job.job_id)
        try:
            try:
                deadline = await self._prepare_job(job)
                if deadline is None:
                    return
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    await self._persist_terminal_state(
                        job.job_id, JobState.FAILED, error="job timeout"
                    )
                    return

                async def _run() -> None:
                    async with self._semaphore:
                        if self._stopping:
                            return
                        latest_deadline = await self._prepare_job(job)
                        if latest_deadline is None:
                            return
                        await self.run_placeholder(job)

                await asyncio.wait_for(_run(), timeout=remaining)
            except TimeoutError:
                logger.warning("job timed out job_id=%s", redact_text(job.job_id))
                await self._persist_terminal_state(job.job_id, JobState.FAILED, error="job timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - placeholder must not crash worker
                sanitized = redact_text(f"{type(exc).__name__}: {exc}")
                logger.warning("job failed job_id=%s error=%s", redact_text(job.job_id), sanitized)
                await self._persist_terminal_state(job.job_id, JobState.FAILED, error=sanitized)
            finally:
                self._queue.task_done()
        finally:
            _CANCEL_EVENT_IN_USE.discard(job.job_id)
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
            row = await JobRepository().get(conn, job.job_id)
            if row is None:
                return
            try:
                current = JobState(str(row["state"]))
            except ValueError:
                return
            if current != JobState.QUEUED:
                return
            if cancel_event.is_set():
                await set_state(conn, job.job_id, JobState.CANCELLED)
                return
            try:
                await set_state(conn, job.job_id, JobState.ACTIVE)
            except ValueError:
                # Cancellation may win between the state read and update.
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
