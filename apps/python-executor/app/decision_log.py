"""Off-critical-path decision logging with read-after-write consistency.

Every /decide writes a Decision row. Doing that write synchronously on the request
path is the single biggest per-decision latency tax, so by default it runs on a
small background pool (ASYNC_DECISION_LOG=0 forces fully synchronous).

To keep reads correct in a single process (tests, one worker), any code that reads
decisions calls ``flush()`` first, which waits for the in-flight writes this process
submitted. Across multiple workers, read-after-write was never guaranteed anyway —
each pod has its own request; the DB is the shared source of truth.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional, Set

_pool: Optional[ThreadPoolExecutor] = None
_pending: Set[Future] = set()
_lock = threading.Lock()


def is_async() -> bool:
    """Async by default; ASYNC_DECISION_LOG=0 forces synchronous writes."""
    return os.getenv("ASYNC_DECISION_LOG", "1") != "0"


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=int(os.getenv("DECISION_LOG_WORKERS", "4")),
                                   thread_name_prefix="decision-log")
    return _pool


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a decision-log write off the request path (or inline when sync)."""
    if not is_async():
        fn(*args, **kwargs)
        return
    future = _get_pool().submit(fn, *args, **kwargs)
    with _lock:
        _pending.add(future)
    future.add_done_callback(_discard)


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
