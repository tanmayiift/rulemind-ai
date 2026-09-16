"""Back up the database and PROVE the backup restores (Track B6).

An unverified backup is not a backup. The common failure is not that pg_dump fails loudly --
it is that it succeeds for months while writing something that cannot be restored (a missing
extension, an encoding mismatch, a permissions gap, a partial dump from a timeout), and this
is discovered during the incident. So this script does not stop at producing a file:

  1. dump      the live database
  2. restore   it into a scratch database that is created and dropped for the purpose
  3. verify    the restored copy, then drop the scratch

Verification is deliberately not "the file is non-empty". It asserts:

  * every table present in the source is present in the restore;
  * row counts match per table;
  * the audit hash chain still verifies IN THE RESTORED COPY (Track B3). This is the check
    worth the most: a dump that silently drops or reorders audit rows produces a restore that
    looks healthy by row count but whose tamper-evidence is broken -- and a compliance story
    that only holds on the primary is not a compliance story.

Postgres and SQLite are both supported because CI runs SQLite by default and the Postgres
matrix dimension runs the real path; a backup script exercised only in production is exactly
the kind that is discovered to be broken during an incident.

  python scripts/backup_verify.py --out ./backups
  python scripts/backup_verify.py --out ./backups --keep    # leave the dump in place
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


class BackupError(RuntimeError):
    pass


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_path(url: str) -> Path:
    # sqlite:///relative.db and sqlite:////absolute.db
    return Path(url.split("sqlite:///", 1)[-1]).expanduser()


def dump(url: str, out_dir: Path) -> Path:
    """Write a restorable dump and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    if _is_sqlite(url):
        source = _sqlite_path(url)
        if not source.exists():
            raise BackupError(f"sqlite database not found: {source}")
        target = out_dir / f"rulemind-{stamp}.sqlite"
        # .backup, not a file copy: a copy taken while a write is in flight can capture a
        # torn page or miss the WAL entirely, producing a file that opens and is subtly wrong.
        import sqlite3

        with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
        return target

    target = out_dir / f"rulemind-{stamp}.dump"
    # Custom format (-Fc): compressed, and restorable selectively with pg_restore, which
    # matters when a recovery needs one table rather than the whole database.
    result = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(target), url],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f"pg_dump failed: {result.stderr.strip()[:400]}")
    return target


def _scratch_url(url: str, name: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{name}"))


def _psql_admin(url: str, statement: str) -> None:
    admin = _scratch_url(url, "postgres")
    result = subprocess.run(["psql", "--quiet", "--no-psqlrc", "-c", statement, admin],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f"psql failed: {result.stderr.strip()[:400]}")


def restore(url: str, dump_path: Path, scratch_name: str) -> Tuple[str, callable]:
    """Restore `dump_path` into a scratch database. Returns (url, cleanup)."""
    if _is_sqlite(url):
        tmp_dir = Path(tempfile.mkdtemp(prefix="rm-restore-"))
        restored = tmp_dir / "restored.db"
        shutil.copy2(dump_path, restored)
        return f"sqlite:///{restored}", lambda: shutil.rmtree(tmp_dir, ignore_errors=True)

    _psql_admin(url, f'DROP DATABASE IF EXISTS "{scratch_name}"')
    _psql_admin(url, f'CREATE DATABASE "{scratch_name}"')
    target = _scratch_url(url, scratch_name)
    result = subprocess.run(
        ["pg_restore", "--no-owner", "--no-acl", "--dbname", target, str(dump_path)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f"pg_restore failed: {result.stderr.strip()[:400]}")
    return target, lambda: _psql_admin(url, f'DROP DATABASE IF EXISTS "{scratch_name}"')


def table_counts(url: str) -> Dict[str, int]:
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            names = inspect(conn).get_table_names()
            return {name: int(conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
                    for name in sorted(names)}
    finally:
        engine.dispose()


def _storage_for(url: str):
    """A Storage bound to `url`.

    Storage takes a filesystem `path` (SQLite) and otherwise reads $DATABASE_URL -- see
    app/db.py database_url(). So a Postgres target is selected by setting the environment
    for the construction, which is why this is a helper rather than one constructor call.

    `force_sqlite` makes the SQLite branch mean what this function says it means: bound to
    `url`, whatever the ambient $DATABASE_URL is. Without it, verifying a SQLite dump on a host
    that has DATABASE_URL pointing at Postgres silently walks the audit chain of the LIVE
    Postgres database instead of the restored file -- and reports the restore as clean, which
    is the one answer a backup verifier must never give wrongly.
    """
    from app.storage import Storage

    if _is_sqlite(url):
        return Storage(path=str(_sqlite_path(url)), force_sqlite=True)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        return Storage()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def audit_chains_verify(url: str) -> List[str]:
    """Re-walk every tenant's audit chain in the RESTORED copy. Returns failure descriptions.

    DISPOSES THE ENGINE BEFORE RETURNING, and that is not tidiness. `Storage` opens a SQLAlchemy
    connection POOL against the scratch database; the caller's next move is to DROP that
    database, and Postgres refuses to drop one that still has a session attached:

        psql failed: ERROR: database "rulemind_restore_check" is being accessed by other users
        DETAIL: There is 1 other session using the database.

    So without this the Postgres path cannot clean up after itself and leaves a scratch database
    behind on every run. SQLite never showed it -- there is no server to refuse the drop -- and
    this workflow has never had a green run, so nothing surfaced it until 2026-09-16.
    """
    storage = _storage_for(url)
    problems = []
    try:
        for tenant in storage.list_tenants():
            tenant_id = tenant.get("id")
            if not tenant_id:
                continue
            report = storage.verify_audit_chain(tenant_id=tenant_id)
            if not report["ok"]:
                problems.append(f"tenant {tenant_id}: {report['breaks'][:3]}")
    finally:
        # `finally`, so a mid-walk failure still releases the pool -- otherwise a chain problem
        # (the thing this script exists to find) would ALSO leave an undroppable database.
        try:
            storage.engine.dispose()
        except Exception:  # pragma: no cover - cleanup must not mask the real result
            pass
    return problems


def verify(source_url: str, restored_url: str) -> Dict[str, object]:
    """Compare the restore against the source. Returns a report; `ok` is the verdict."""
    source, restored = table_counts(source_url), table_counts(restored_url)
    problems: List[str] = []

    for name in sorted(set(source) - set(restored)):
        problems.append(f"table missing from restore: {name}")
    for name in sorted(set(source) & set(restored)):
        if source[name] != restored[name]:
            problems.append(f"row count differs for {name}: source={source[name]} restore={restored[name]}")

    try:
        problems.extend(f"audit chain broken after restore -- {p}" for p in audit_chains_verify(restored_url))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"audit chain could not be verified in the restore: {exc}")

    return {"ok": not problems, "problems": problems,
            "tables": len(source), "rows": sum(source.values())}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("DATABASE_URL", ""),
                        help="database URL (default: $DATABASE_URL)")
    parser.add_argument("--out", default="./backups", help="directory for the dump")
    parser.add_argument("--keep", action="store_true", help="keep the dump after verifying")
    parser.add_argument("--scratch-name", default="rulemind_restore_check")
    args = parser.parse_args(argv)

    url = args.url
    if not url:
        from app.db import DEFAULT_SQLITE_URL

        url = DEFAULT_SQLITE_URL
    print(f"source: {url.split('@')[-1]}")

    dump_path = dump(url, Path(args.out))
    print(f"dumped: {dump_path} ({dump_path.stat().st_size} bytes)")

    restored_url, cleanup = restore(url, dump_path, args.scratch_name)
    try:
        report = verify(url, restored_url)
    finally:
        cleanup()

    print(f"verified {report['tables']} tables / {report['rows']} rows")
    for problem in report["problems"]:
        print(f"  PROBLEM  {problem}")
    if not args.keep:
        dump_path.unlink(missing_ok=True)
        print("dump removed (pass --keep to retain it)")

    if not report["ok"]:
        print("\nBACKUP IS NOT RESTORABLE — do not rely on it.")
        return 1
    print("backup restores cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
