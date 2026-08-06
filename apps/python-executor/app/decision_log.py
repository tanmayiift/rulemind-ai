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

To keep reads correct in a single process, any code that reads decisions calls ``flush()``
first, which waits for the in-flight writes this process submitted. Across multiple workers,
read-after-write was never guaranteed anyway — the DB is the shared source of truth.
"""
from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional, Set

_pool: Optional[ThreadPoolExecutor] = None
_pending: Set[Future] = set()
_lock = threading.Lock()
_semaphore: Optional[threading.BoundedSemaphore] = None
_atexit_registered = False


def is_async() -> bool:
    """Async by default; ASYNC_DECISION_LOG=0 forces synchronous writes."""
    return os.getenv("ASYNC_DECISION_LOG", "1") != "0"


def _max_queue() -> int:
    """Max outstanding (queued + in-flight) async writes before submit() falls back to inline."""
    try:
        return max(1, int(os.getenv("DECISION_LOG_MAX_QUEUE", "1000")))
    except ValueError:
        return 1000


def _get_pool() -> ThreadPoolExecutor:
    global _pool, _atexit_registered
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=int(os.getenv("DECISION_LOG_WORKERS", "4")),
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


def _discard(future: Future) -> None:
    with _lock:
        _pending.discard(future)


def flush(timeout: float = 10.0) -> None:
    """Block until the decision-log writes submitted by this process finish, so a
    subsequent read sees them. Cheap no-op when nothing is pending or in sync mode."""
    with _lock:
        pending = list(_pending)
    for future in pending:
        try:
            future.result(timeout=timeout)
        except Exception:  # a failed write must not break the reader
            pass


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
