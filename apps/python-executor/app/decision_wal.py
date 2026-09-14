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

Per-process files avoid cross-process write contention, and every file is held under an
exclusive ``flock`` for the lifetime of the process that owns it. recover() will only touch
a file it can lock — i.e. an ORPHAN whose owner is gone — so a booting worker heals the dead,
never the living.
Opt-in via DECISION_WAL=1 (kept off by default so existing deployments are unchanged;
the async path's SIGTERM-flush already covers graceful stops).

Three bugs this file used to have, all of which lost the decisions the WAL exists to protect:

* **recover() deleted its siblings' live WAL files.** It globbed every
  ``decision_wal_*.jsonl`` in the directory and ``os.remove``d each one, with no pid check
  and no lock. Under ``--workers 4`` a rolling restart of one worker therefore deleted the
  WAL files the other three were actively appending to, taking their unflushed decisions with
  them — the exact loss the WAL was added to prevent, made *more* likely by having it on.
* **A record whose replay INSERT failed was dropped permanently.** The loop ``continue``d past
  the failure while the file stayed queued for removal, so a transient DB error during startup
  replay silently destroyed the record instead of retrying it on the next boot.
* **The file and the in-memory sets grew forever.** Nothing ever compacted a WAL file at
  runtime, and ``_committed`` / ``_appended`` were only ever added to. A long-lived worker
  therefore leaked one set entry and one disk line per decision, unbounded.

**Where the file lives.** The default is ``$CWD/data/wal``, inside the container's own
filesystem. That is enough for the failure this WAL targets — a SIGKILLed/OOM-killed *worker*,
whose replacement boots against the same filesystem. It is NOT enough for pod eviction or node
loss: nothing mounts a volume there. ``recover()`` says so, once, at startup, so an operator
who believes they have durable decisions finds out from the logs rather than from an incident.
Point ``DECISION_WAL_DIR`` at a mounted volume to close that gap.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

from . import env
import threading
from typing import IO, Any, Dict, List, Optional, Set, Tuple

try:  # POSIX only; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover - not a supported deployment target
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("rulemind.decision_wal")

_lock = threading.Lock()
_file = None  # type: ignore[var-annotated]
_file_path: Optional[str] = None
_committed: Set[str] = set()
_appended: Set[str] = set()
_ephemeral_warned = False

_FILE_PREFIX = "decision_wal_"
_FILE_SUFFIX = ".jsonl"

# ── dead letter ────────────────────────────────────────────────────────────
# A replayed record that fails is KEPT and retried on the next boot, which is right for a
# transient DB error and wrong for a permanently-bad one. Without a bound, a record that can
# never insert -- a constraint violation, a schema the row predates -- is retried on every boot
# for the life of the deployment, it makes every startup slower, and nothing tells anyone it is
# there. The existing code comment even says such a record "stays inspectable on disk"; nothing
# ever pointed an operator at the disk.
#
# After MAX_REPLAY_ATTEMPTS it moves to a dead-letter file instead: still on disk, still exactly
# recoverable, but out of the boot path and counted where someone can see it. Deleting it was
# never an option -- it is a decision that was returned to a caller.
# NOT prefixed "decision_wal_": recover() collects candidates with
# `f.startswith(_FILE_PREFIX) and f.endswith(_FILE_SUFFIX)`, so a dead-letter file named that way
# is picked straight back up as a WAL file to replay -- which defeats the entire point and was
# caught here by the test asserting a later boot does not retry it.
_DEAD_LETTER_NAME = "dead_letter_decisions.jsonl"


def _max_replay_attempts() -> int:
    return max(1, env.env_int("DECISION_WAL_MAX_REPLAY_ATTEMPTS", 5))


def _dead_letter_path() -> str:
    return os.path.join(_dir(), _DEAD_LETTER_NAME)


def _dead_letter(entries: List[Dict[str, Any]]) -> None:
    """Append permanently-failing entries to the dead-letter file. Best effort: a dead-letter
    write that fails must not take down startup, because the alternative is a boot loop on a
    record that was already unusable."""
    if not entries:
        return
    try:
        with open(_dead_letter_path(), "a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, default=str) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover
                pass
    except OSError:  # pragma: no cover - disk full / read-only mount
        logger.error("could not write %d entries to the decision WAL dead-letter file",
                     len(entries))


def dead_letter_entries() -> List[Dict[str, Any]]:
    """Every entry that exhausted its replay attempts. For an operator surface, not the boot
    path -- nothing here is retried automatically."""
    try:
        with open(_dead_letter_path(), "r", encoding="utf-8") as fh:
            return _read_entries(fh)
    except OSError:
        return []


def replay_dead_letter(storage: Any) -> Dict[str, int]:
    """Re-attempt every dead-lettered record, once, on demand.

    This is the half that makes the dead letter a queue rather than a graveyard: the usual cause
    is a bug or a missing migration that has since been fixed, and the fix should not require
    hand-editing a JSONL file. Anything that still fails is written back, so a partial success
    leaves exactly the unresolved records behind. Attempt counters are RESET on the way in --
    the operator asking for this is asserting that the reason for the failure has changed.
    """
    entries = dead_letter_entries()
    if not entries:
        return {"attempted": 0, "replayed": 0, "still_failing": 0}
    replayed = 0
    still_failing: List[Dict[str, Any]] = []
    present = _existing_ids(storage, [str(e.get("id")) for e in entries if e.get("id") is not None])
    for entry in entries:
        rid = str(entry.get("id")) if entry.get("id") is not None else None
        if rid and rid in present:
            replayed += 1  # already durable: resolved, just not by us
            continue
        try:
            storage.add_decision(entry["record"], tenant_id=entry.get("tenant_id"))
            replayed += 1
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
            logger.warning("dead-letter replay failed for %s: %s", rid, exc)
            entry.pop("replay_attempts", None)
            still_failing.append(entry)
    try:
        if still_failing:
            with open(_dead_letter_path(), "w", encoding="utf-8") as fh:
                for entry in still_failing:
                    fh.write(json.dumps(entry, default=str) + "\n")
        else:
            os.remove(_dead_letter_path())
    except OSError:  # pragma: no cover
        pass
    return {"attempted": len(entries), "replayed": replayed, "still_failing": len(still_failing)}
# A unique-per-process-instance suffix. The old ``decision_wal_<pid>.jsonl`` collided under
# pid reuse: a new worker handed a dead worker's pid opened the dead worker's un-recovered
# file in append mode and silently mixed the two.
_INSTANCE = uuid.uuid4().hex[:8]


def enabled() -> bool:
    """WAL is opt-in (DECISION_WAL=1). Off by default: the async path already flushes on SIGTERM."""
    return env.env_bool("DECISION_WAL", False)


def _compact_every() -> int:
    """Committed entries to accumulate before rewriting the file without them."""
    return max(1, env.env_int("DECISION_WAL_COMPACT_EVERY", 1000))


def _dir() -> str:
    # env_str, not os.getenv: `.env.example` ships `DECISION_WAL_DIR=` (blank), and a
    # blank value defeats a getenv default — which made this call os.makedirs("") and
    # raise, so with DECISION_WAL=1 every decision 500'd.
    path = env.env_str("DECISION_WAL_DIR", os.path.join(os.getcwd(), "data", "wal"))
    os.makedirs(path, exist_ok=True)
    return path


def _default_dir_in_use() -> bool:
    return not env.env_opt("DECISION_WAL_DIR")


def _path_for(pid: Optional[int] = None) -> str:
    return os.path.join(
        _dir(),
        "{0}{1}_{2}{3}".format(_FILE_PREFIX, pid if pid is not None else os.getpid(), _INSTANCE, _FILE_SUFFIX),
    )


def _try_lock_exclusive(fh: IO[str]) -> bool:
    """Take a non-blocking exclusive advisory lock. False means somebody else holds it.

    This is the whole safety property of recovery: a WAL file whose owning process is alive
    is locked by that process, so a recovering process cannot read, rewrite or delete it.
    Without fcntl (non-POSIX) we report failure, which degrades recovery to "own file only"
    — strictly safe, just less thorough.
    """
    if fcntl is None:  # pragma: no cover - non-POSIX
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _get_file():
    global _file, _file_path
    if _file is None:
        path = _path_for()
        # "a+" (not "a") so compaction can read the file back through the same handle and
        # rewrite it in place, under the lock it already holds.
        fh = open(path, "a+", encoding="utf-8")
        # Held for the life of the process. recover() in a sibling uses exactly this lock to
        # tell "orphaned by a dead worker" from "in use by a live one".
        _try_lock_exclusive(fh)
        _file, _file_path = fh, path
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
        _maybe_compact_locked()


def mark_committed(rec_id: str) -> None:
    """Note that the async DB write for this id landed, so compaction can drop it."""
    with _lock:
        _committed.add(str(rec_id))
        _maybe_compact_locked()


def _maybe_compact_locked() -> None:
    """Rewrite this process's WAL without the entries already durable in the DB.

    Caller holds `_lock`. Without this, both the file and `_committed`/`_appended` grew by one
    entry per decision for the life of the process and were never truncated — an unbounded disk
    and memory leak in the component whose entire job is to survive a crash. Compaction is
    in-place under the flock this process already holds, so no other process can observe a
    half-written file; a crash mid-rewrite loses only entries already committed to the DB.
    """
    global _committed, _appended
    if _file is None or len(_committed) < _compact_every():
        return
    try:
        _file.flush()
        _file.seek(0)
        kept = []
        for raw in _file:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(entry.get("id")) not in _committed:
                kept.append(raw)
        _file.truncate(0)
        # The handle is O_APPEND, so writes land at EOF (== 0 after truncate) regardless of
        # the seek above; the explicit seek keeps the intent readable.
        _file.seek(0)
        if kept:
            _file.write("\n".join(kept) + "\n")
        _file.flush()
        try:
            os.fsync(_file.fileno())
        except OSError:  # pragma: no cover
            pass
    except OSError as exc:  # pragma: no cover - compaction is an optimisation, never a failure
        logger.warning("decision WAL compaction skipped: %s", exc)
        return
    still_pending = {str(json.loads(line).get("id")) for line in kept} if kept else set()
    _appended = still_pending
    _committed = set()


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


def _read_entries(fh: IO[str]) -> List[Dict[str, Any]]:
    fh.seek(0)
    entries: List[Dict[str, Any]] = []
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
    return entries


def _rewrite_locked(fh: IO[str], entries: List[Dict[str, Any]]) -> None:
    """Replace the contents of an exclusively-locked file with `entries`."""
    fh.seek(0)
    fh.truncate(0)
    for entry in entries:
        fh.write(json.dumps(entry, default=str) + "\n")
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:  # pragma: no cover
        pass


def _recover_one(storage: Any, path: str) -> Optional[Tuple[int, int, int, int]]:
    """Replay one orphaned WAL file. None means "skipped, another process owns it".

    Returns (scanned, replayed, already_durable, quarantined).
    """
    try:
        fh = open(path, "r+", encoding="utf-8")
    except OSError:
        return None
    try:
        if not _try_lock_exclusive(fh):
            # A LIVE process holds this file. Reading it is harmless but deleting it — which
            # is what this used to do unconditionally — destroys decisions that worker has
            # appended and not yet committed. Leave it entirely; its owner will recover it.
            return None
        entries = _read_entries(fh)
        by_id: Dict[str, Dict[str, Any]] = {}
        for entry in entries:
            rid = str(entry.get("id")) if entry.get("id") is not None else None
            if rid:
                by_id[rid] = entry  # de-dupe within the file, last wins
        present = _existing_ids(storage, list(by_id.keys()))
        replayed = 0
        failed: List[Dict[str, Any]] = []
        dead_lettered: List[Dict[str, Any]] = []
        for rid, entry in by_id.items():
            if rid in present:
                continue
            try:
                storage.add_decision(entry["record"], tenant_id=entry.get("tenant_id"))
                replayed += 1
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
                # KEPT, not skipped. The old code `continue`d here and then deleted the file,
                # so a record whose INSERT failed — a transient DB error during startup, a
                # constraint violation worth a human look — was destroyed by the very
                # mechanism that existed to preserve it. Holding it in the file means the
                # next boot retries it, and a permanently-bad record stays inspectable on disk.
                attempts = int(entry.get("replay_attempts") or 0) + 1
                entry["replay_attempts"] = attempts
                if attempts >= _max_replay_attempts():
                    # Out of the boot path, not deleted. See the _DEAD_LETTER_NAME block above.
                    logger.error(
                        "decision WAL replay failed for %s %d times; moving to the dead letter: %s",
                        rid, attempts, exc)
                    entry["dead_lettered_reason"] = str(exc)[:500]
                    dead_lettered.append(entry)
                else:
                    logger.warning(
                        "decision WAL replay failed for %s (attempt %d, kept for retry): %s",
                        rid, attempts, exc)
                    failed.append(entry)
        if dead_lettered:
            _dead_letter(dead_lettered)
        if failed:
            # Keep ONLY the unresolved records: everything else is durable in the DB, so
            # rewriting is compaction, and the file shrinks toward empty across boots.
            _rewrite_locked(fh, failed)
        else:
            # Safe to remove: we hold the exclusive lock, so no other process is appending.
            try:
                os.remove(path)
            except OSError:  # pragma: no cover
                pass
        return len(entries), replayed, len(present), len(failed) + len(dead_lettered)
    finally:
        try:
            fh.close()  # releases the flock
        except OSError:  # pragma: no cover
            pass


def recover(storage: Any) -> Dict[str, int]:
    """Startup replay: re-insert any WAL decision missing from the DB (idempotent), then compact.

    Only files this process can take an exclusive lock on are touched — that is how an orphan
    left by a dead worker is told apart from a file a live sibling is appending to. Files still
    owned by a live process are skipped entirely and counted in "skipped_locked".

    Returns {"files", "scanned", "replayed", "already_durable", "quarantined", "skipped_locked",
    "dead_lettered"} -- the last being the TOTAL sitting in the dead-letter file, not a delta,
    since it persists across boots until someone acts on it.
    """
    empty = {"files": 0, "scanned": 0, "replayed": 0, "already_durable": 0,
             "quarantined": 0, "skipped_locked": 0, "dead_lettered": 0}
    if not enabled():
        return empty
    _warn_if_ephemeral()
    directory = _dir()
    try:
        candidates = [os.path.join(directory, f) for f in os.listdir(directory)
                      if f.startswith(_FILE_PREFIX) and f.endswith(_FILE_SUFFIX)]
    except OSError:  # pragma: no cover - directory vanished between makedirs and listdir
        return empty
    # Release our own handle (and its flock) first, so this process's file is just another
    # orphan to replay. recover() is a STARTUP hook; anything already in our file predates
    # this boot. The next append() opens a fresh file. Doing this up front — rather than
    # after the loop, as the old code did — is what makes the lock test below meaningful:
    # the only files we then fail to lock are ones a LIVE sibling owns.
    global _file, _file_path
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:  # pragma: no cover
                pass
            _file = None
        _file_path = None
    scanned = replayed = already = quarantined = skipped = 0
    for path in candidates:
        outcome = _recover_one(storage, path)
        if outcome is None:
            skipped += 1
            continue
        file_scanned, file_replayed, file_present, file_failed = outcome
        scanned += file_scanned
        replayed += file_replayed
        already += file_present
        quarantined += file_failed
    return {"files": len(candidates), "scanned": scanned, "replayed": replayed,
            "already_durable": already, "quarantined": quarantined, "skipped_locked": skipped,
            # Reported separately from `quarantined` (which counts everything still unresolved
            # after this pass) because the two mean different things to an operator: quarantined
            # will be retried next boot on its own, dead-lettered will not and needs a decision.
            "dead_lettered": len(dead_letter_entries())}


def _warn_if_ephemeral() -> None:
    """Say once, loudly, that the default WAL location does not survive losing the pod.

    DECISION_WAL=1 reads as "decisions are now durable". With the default directory it means
    "durable against a killed worker process", which is the documented target, and nothing
    more: there is no volume mounted at $CWD/data/wal in helm/ or k8s/, so an evicted or
    rescheduled pod takes the WAL with it. An operator should learn that here, not later.
    """
    global _ephemeral_warned
    if _ephemeral_warned or not _default_dir_in_use():
        return
    _ephemeral_warned = True
    logger.warning(
        "DECISION_WAL=1 with the default DECISION_WAL_DIR (%s): this is container-local storage. "
        "It protects against a killed worker process, NOT against pod eviction or node loss. "
        "Set DECISION_WAL_DIR to a mounted persistent volume for that.", _dir())


def reset_for_test() -> None:
    """Test hook: close the open handle and clear in-memory sets between cases."""
    global _file, _file_path, _INSTANCE, _ephemeral_warned
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:
                pass
            _file = None
        _file_path = None
        # A fresh instance suffix per case, so a file left behind by the previous case is an
        # orphan to recover rather than "our own file" that recover() must skip.
        _INSTANCE = uuid.uuid4().hex[:8]
        _ephemeral_warned = False
        _committed.clear()
        _appended.clear()
