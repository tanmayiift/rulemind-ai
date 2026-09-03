"""Off-critical-path decision logging with read-after-write consistency.

Every /decide writes a Decision row. Doing that write synchronously on the request
path is the single biggest per-decision latency tax, so by default it runs on a
small background pool (ASYNC_DECISION_LOG=0 forces fully synchronous, zero-loss).

Durability & memory safety of the async path:

  - **Bounded backpressure.** The number of outstanding (queued + in-flight) writes is
    capped at DECISION_LOG_MAX_QUEUE (default 1000). When the cap is reached — i.e. the
    DB is momentarily slower than intake — submit() runs the write *inline* on the calling
    thread instead of queueing it. This bounds worker memory (no unbounded internal queue)
    and, crucially, means a write is **never silently dropped** under load.
  - **Graceful-shutdown flush.** shutdown() flushes all pending writes and joins the pool.
    It is wired into the FastAPI shutdown event and registered with atexit, so a normal
    stop / rolling deploy (SIGTERM) never loses queued decisions.

The remaining loss window is a hard, uncatchable kill (SIGKILL / OOM) between a decision
being returned and its async write landing — covered by the durable-outbox follow-up, or
today by ASYNC_DECISION_LOG=0 (synchronous writes have no window).

Read-after-write:

  - **Same process.** Any code that reads decisions calls ``flush()`` first, which waits for
    the in-flight writes this process submitted.
  - **Across workers.** ``flush()`` cannot help: ``_pending`` is per-process, so the worker
    handling the follow-up GET has nothing to wait on and returns immediately. The API hands
    back a ``decision_id`` that then 404s roughly 3 times in 4 under ``--workers 4``. Point
    reads use ``await_row()`` instead, which flushes locally and then re-polls for a short
    bounded window — the cost lands only on the miss path.
"""
from __future__ import annotations

import atexit
import time

from . import env
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Set

from prometheus_client import Counter

from .metrics_registry import get_or_create

_pool: Optional[ThreadPoolExecutor] = None
_pending: Set[Future] = set()
_lock = threading.Lock()
_semaphore: Optional[threading.BoundedSemaphore] = None
_atexit_registered = False


def _failure_counter() -> Counter:
    """Reload-safe Counter: the durability tests importlib.reload() this module, which would re-run a
    bare Counter() and raise 'Duplicated timeseries' on the global registry. Reuse the already-
    registered collector on reload instead of redefining it.

    The private-registry poke this used to do inline now lives in `metrics_registry.get_or_create`,
    which additionally falls back on the duplicate-registration ValueError. That matters under the
    multiprocess registry: `_names_to_collectors` is not a stable contract, and a miss there used to
    mean a hard 'Duplicated timeseries' crash on reload rather than a reused collector. A Counter
    aggregates correctly across workers with no extra configuration (unlike a Gauge, which needs an
    explicit multiprocess_mode)."""
    return get_or_create(
        Counter,
        "rulemind_decision_log_write_failures",
        "Decision-log writes that failed (decision computed but not persisted)",
        ("source",),
    )


# A decision-log write that fails on the async path used to be swallowed silently — the decision was
# computed and returned (200) but never persisted, with no signal anywhere. This counter is the
# reliable alarm (no DB needed); record_write_failure also best-effort writes an error_event.
DECISION_LOG_FAILURES = _failure_counter()


def record_write_failure(storage: Any, tenant_id: Optional[str], context: Optional[Dict[str, Any]], exc: BaseException) -> None:
    """Never-raising recorder for a dropped decision-log write. Increments the failure metric (the
    reliable signal) and best-effort writes an error_event so the drop is alertable instead of silent.
    Both steps are guarded — recording a failure must never itself break anything."""
    ctx = context or {}
    try:
        DECISION_LOG_FAILURES.labels(source=str(ctx.get("source", "unknown"))).inc()
    except Exception:  # pragma: no cover - metric must never raise
        pass
    try:
        storage.add_error_event(
            {
                "tenant_id": tenant_id,
                "scope": "decision",
                "stage": "decision_log_write",
                "entity_type": "policy",
                "entity_id": ctx.get("policy_id"),
                "message": "Decision-log write failed — decision computed but NOT persisted: {0}".format(exc),
                "details": {"error": str(exc), "source": ctx.get("source")},
            },
            tenant_id=tenant_id,
        )
    except Exception:  # pragma: no cover - error_event uses the same DB; may also be down
        pass


def is_async() -> bool:
    """Async by default; ASYNC_DECISION_LOG=0 forces synchronous writes."""
    return env.env_str("ASYNC_DECISION_LOG", "1") != "0"


def _max_queue() -> int:
    """Max outstanding (queued + in-flight) async writes before submit() falls back to inline."""
    try:
        return max(1, env.env_int("DECISION_LOG_MAX_QUEUE", 1000))
    except ValueError:
        return 1000


def _get_pool() -> ThreadPoolExecutor:
    global _pool, _atexit_registered
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=env.env_int("DECISION_LOG_WORKERS", 4),
                                   thread_name_prefix="decision-log")
        if not _atexit_registered:
            atexit.register(shutdown)
            _atexit_registered = True
    return _pool


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = threading.BoundedSemaphore(_max_queue())
    return _semaphore


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a decision-log write off the request path (or inline when sync / under backpressure)."""
    if not is_async():
        fn(*args, **kwargs)
        return
    sem = _get_semaphore()
    if not sem.acquire(blocking=False):
        # Backpressure: the async queue is full (DB slower than intake). Run inline so the
        # write is never dropped and worker memory stays bounded — the request pays the cost.
        fn(*args, **kwargs)
        return
    try:
        future = _get_pool().submit(fn, *args, **kwargs)
    except RuntimeError:
        # Pool is shutting down -> do the write inline rather than lose it.
        sem.release()
        fn(*args, **kwargs)
        return

    def _done(f: Future) -> None:
        sem.release()
        _discard(f)

    with _lock:
        _pending.add(future)
    future.add_done_callback(_done)


def persist_decision(storage: Any, record: Dict[str, Any], tenant_id: Optional[str]) -> None:
    """Durable-by-default decision write. When DECISION_WAL=1, the decision is appended to the
    write-ahead log (fsync) BEFORE the async DB write is scheduled, so a hard SIGKILL/OOM between
    the decision returning and the DB write landing loses nothing — startup replay re-inserts any
    WAL entry missing from the DB (idempotent by id). Without the WAL flag this is exactly the old
    async submit(), so behaviour is unchanged unless a deployment opts in."""
    from . import decision_wal

    rec_id = record.get("id")
    use_wal = decision_wal.enabled() and bool(rec_id)
    if use_wal:
        decision_wal.append(record, tenant_id)

    def _write() -> None:
        try:
            storage.add_decision(record, tenant_id=tenant_id)
        except Exception as exc:  # never fail the decision; surface the drop (metric + error_event)
            record_write_failure(storage, tenant_id, {"source": record.get("source"), "policy_id": record.get("policy_id")}, exc)
            return  # leave the WAL entry UNcommitted so startup replay re-inserts it
        if use_wal:
            decision_wal.mark_committed(str(rec_id))

    submit(_write)


def _discard(future: Future) -> None:
    with _lock:
        _pending.discard(future)


def flush(timeout: float = 10.0) -> None:
    """Block until the decision-log writes **this process** submitted finish.

    That is the whole guarantee, and the previous wording ("so a subsequent read sees them")
    promised more than it delivers. `_pending` holds futures created in *this* process. Under
    `--workers 4` the POST /decide that created a decision and the GET that reads it back are
    routinely handled by different workers, and the reader's `_pending` is empty — so flush()
    returns instantly and the read 404s on a decision the API just returned an id for.

    Cheap no-op when nothing is pending or in sync mode. Use `await_row` for a read that must
    tolerate the cross-worker case.
    """
    with _lock:
        pending = list(_pending)
    for future in pending:
        try:
            future.result(timeout=timeout)
        except Exception:  # a failed write must not break the reader
            pass


def read_after_write_seconds() -> float:
    """How long a read may wait for a row another worker is still writing.

    Small on purpose. It only ever elapses in full for a decision id that does not exist,
    which turns a fast 404 into a slow one; the window just needs to cover an in-flight
    async insert (single-digit milliseconds in practice)."""
    return max(0.0, env.env_int("DECISION_READ_AFTER_WRITE_MS", 500) / 1000.0)


def await_row(fetch: Callable[[], Optional[Any]], timeout_seconds: Optional[float] = None) -> Optional[Any]:
    """Read a row that a *possibly different worker* may still be writing.

    A true cross-worker barrier is not cheap: it would need every worker's in-flight write
    ids in shared state, consulted on every read, which costs more than the async write saves.
    The reader instead tolerates the gap — flush what this process owes, then re-poll for a
    bounded window. The cost falls entirely on the miss path: a row that is already committed
    returns on the first fetch with no sleep at all.

    Returns whatever `fetch` last returned (None if it never appeared), so the caller's
    existing 404 handling is unchanged.
    """
    flush()
    row = fetch()
    if row is not None:
        return row
    deadline = time.monotonic() + (read_after_write_seconds() if timeout_seconds is None else timeout_seconds)
    delay = 0.005
    while time.monotonic() < deadline:
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        row = fetch()
        if row is not None:
            return row
        delay = min(delay * 2, 0.05)  # back off: a slow DB should not be polled hot
    return row


def shutdown(timeout: float = 15.0) -> None:
    """Flush pending writes and tear down the pool. Safe to call more than once. Wired to the
    FastAPI shutdown event and atexit so a graceful stop / rolling deploy loses no queued write."""
    flush(timeout)
    global _pool
    with _lock:
        pool = _pool
        _pool = None
    if pool is not None:
        pool.shutdown(wait=True)
