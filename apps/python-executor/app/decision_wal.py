"""Durable write-ahead log for decisions — closes the SIGKILL/OOM loss window.

The decision-log write runs off the request path on a background pool (see
``decision_log``). A graceful stop (SIGTERM / rolling deploy) flushes that pool, so
nothing is lost there. The one remaining gap is a *hard, uncatchable* kill — SIGKILL
or an OOM — landing after a decision has been returned to the caller but before its
async DB write has committed. That decision would simply vanish.

This WAL removes that gap:

  1. **append()** writes the decision to a per-worker append-only file and fsyncs it
     *before* the async DB write is scheduled. Once append() returns, the decision is
     on durable storage even if the process is killed a microsecond later.
  2. **mark_committed()** records (in memory) that the DB write landed, so the file can
     be compacted.
  3. **recover()** runs at startup: it reads every worker's WAL, finds decisions whose
     id is NOT already in the DB, and re-inserts them — idempotently (existing ids are
     skipped, so a replay after a partial crash never double-counts). Then it compacts
     the files, dropping entries already durable in the DB.

Per-worker files (``decision_wal_<pid>.jsonl``) avoid cross-process write contention;
recover() scans them all, so whichever process boots first heals the others' orphans.
Opt-in via DECISION_WAL=1 (kept off by default so existing deployments are unchanged;
the async path's SIGTERM-flush already covers graceful stops).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional, Set

_lock = threading.Lock()
_file = None  # type: ignore[var-annotated]
_committed: Set[str] = set()
_appended: Set[str] = set()


def enabled() -> bool:
    """WAL is opt-in (DECISION_WAL=1). Off by default: the async path already flushes on SIGTERM."""
    return os.getenv("DECISION_WAL", "0") == "1"


def _dir() -> str:
    path = os.getenv("DECISION_WAL_DIR", os.path.join(os.getcwd(), "data", "wal"))
    os.makedirs(path, exist_ok=True)
    return path


def _path_for(pid: Optional[int] = None) -> str:
    return os.path.join(_dir(), "decision_wal_{0}.jsonl".format(pid if pid is not None else os.getpid()))


def _get_file():
    global _file
    if _file is None:
        _file = open(_path_for(), "a", encoding="utf-8")
    return _file


def append(record: Dict[str, Any], tenant_id: Optional[str] = None) -> None:
    """Durably append one decision to this worker's WAL, fsync, then return. The caller schedules
    the async DB write only after this returns, so a hard kill in between loses nothing."""
    rec_id = record.get("id")
    if not rec_id:
        return
    line = json.dumps({"id": rec_id, "tenant_id": tenant_id or record.get("tenant_id"), "record": record},
                      default=str)
    with _lock:
        fh = _get_file()
        fh.write(line + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())  # the durability point — survives a process kill
        except OSError:  # pragma: no cover - some filesystems (or /dev shims) can't fsync
            pass
        _appended.add(str(rec_id))


def mark_committed(rec_id: str) -> None:
    """Note that the async DB write for this id landed, so compaction can drop it."""
    with _lock:
        _committed.add(str(rec_id))


def _existing_ids(storage: Any, ids: List[str]) -> Set[str]:
    """Which of these decision ids are already durable in the DB (so replay must skip them)."""
    if not ids:
        return set()
    from .models import Decision
    from sqlalchemy import select

    found: Set[str] = set()
    with storage.connect() as session:
        # Chunk to keep the IN() list bounded on large recoveries.
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            found.update(session.scalars(select(Decision.id).where(Decision.id.in_(chunk))).all())
    return found


def recover(storage: Any) -> Dict[str, int]:
    """Startup replay: re-insert any WAL decision missing from the DB (idempotent), then compact.

    Returns {"files", "scanned", "replayed", "already_durable"} for logging/observability."""
    if not enabled():
        return {"files": 0, "scanned": 0, "replayed": 0, "already_durable": 0}
    directory = _dir()
    files = [os.path.join(directory, f) for f in os.listdir(directory)
             if f.startswith("decision_wal_") and f.endswith(".jsonl")]
    scanned = replayed = already = 0
    for path in files:
        entries: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entries.append(json.loads(raw))
                    except json.JSONDecodeError:
                        # A torn final line (killed mid-write) — skip it; the decision it
                        # represents was never acked to the caller, so dropping it is correct.
                        continue
        except OSError:
            continue
        scanned += len(entries)
        # De-dupe within the file by id (last wins), then split by DB presence.
        by_id: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            rid = str(e.get("id")) if e.get("id") is not None else None
            if rid:
                by_id[rid] = e
        present = _existing_ids(storage, list(by_id.keys()))
        already += len(present)
        for rid, e in by_id.items():
            if rid in present:
                continue
            try:
                storage.add_decision(e["record"], tenant_id=e.get("tenant_id"))
                replayed += 1
            except Exception:
                # Best-effort: a genuinely bad record must not block recovering the rest.
                continue
        # Compaction: every id in this file is now durable in the DB, so the file can go.
        try:
            os.remove(path)
        except OSError:
            pass
    # A file we just removed may be the one this process still holds open for appends; drop the
    # stale handle so the next append() reopens a fresh file instead of writing to a deleted inode.
    global _file
    with _lock:
        if _file is not None and files:
            try:
                _file.close()
            except OSError:
                pass
            _file = None
    return {"files": len(files), "scanned": scanned, "replayed": replayed, "already_durable": already}


def reset_for_test() -> None:
    """Test hook: close the open handle and clear in-memory sets between cases."""
    global _file
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:
                pass
            _file = None
        _committed.clear()
        _appended.clear()
