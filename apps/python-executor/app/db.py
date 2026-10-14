from __future__ import annotations

import logging
import hashlib
import os

from . import env
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


logger = logging.getLogger("rulemind.db")

# Postgres is the intended production datastore; SQLite is for local dev. In production, set
# DATABASE_URL to a Postgres DSN. Dev can switch to Postgres too (just set DATABASE_URL).
DEFAULT_SQLITE_URL = "sqlite:///.runtime/rulemind_v4.db"

# Opt-in escape hatch for a deploy that genuinely wants SQLite outside local dev (a single-replica
# appliance install, an air-gapped demo). Without it, a non-dev boot with no DATABASE_URL fails
# loudly instead of quietly writing production data to a container-local file that dies with the pod.
ALLOW_SQLITE_VAR = "RULEMIND_ALLOW_SQLITE"


class DatabaseNotConfiguredError(RuntimeError):
    """Raised at startup when DATABASE_URL is unset/blank outside local development."""


def _is_local_dev() -> bool:
    """Shared with security_config so the environment test cannot drift.

    `os.getenv("NODE_ENV", "development") == "development"` was the third byte-identical copy
    of this test in the codebase (session_cookie._secure and middleware.admin_cookie_secure
    were the others) and it keys off the wrong variable: a deployment that sets RULEMIND_ENV
    and not NODE_ENV was classed as local dev.
    """
    from .security_config import _is_local_dev as _shared

    return _shared(os.environ)


def _warn_if_sqlite_in_production(url: str) -> None:
    """SQLite is single-writer; fine for dev, a scaling ceiling in production. Warn (don't fail —
    some deploys intentionally opt in via RULEMIND_ALLOW_SQLITE) so operators default to Postgres."""
    if not url.startswith("sqlite"):
        return
    if _is_local_dev():
        return
    logger.warning(
        "Running on SQLite in a non-development environment. SQLite is single-writer and will "
        "cap throughput; set DATABASE_URL to a Postgres DSN for production."
    )


def _default_database_url() -> str:
    """The URL to use when DATABASE_URL is unset or blank.

    Blank is the case that mattered: the Helm chart renders `DATABASE_URL: ""` (it pipes the
    value through `| quote`), and `os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)` returns the
    default only when the variable is *unset*. A set-but-empty value returned `""`, so
    `create_engine("")` raised at module import — CrashLoopBackOff with a stack trace and no
    statement of which variable was wrong.

    Falling back to SQLite instead is not a fix either: a production pod would then silently
    write decisions and audit rows to a container-local file that vanishes with the pod, and
    every replica would have its own. So the fallback now applies only in local dev (or with an
    explicit RULEMIND_ALLOW_SQLITE opt-in); anywhere else this refuses to start and names the
    variable.
    """
    if _is_local_dev() or env.env_bool(ALLOW_SQLITE_VAR):
        return DEFAULT_SQLITE_URL
    raise DatabaseNotConfiguredError(
        "DATABASE_URL is unset or empty. RuleMind will not start outside local development "
        "without a database: set DATABASE_URL to a Postgres DSN "
        "(e.g. postgresql+psycopg://user:pass@host:5432/rulemind). "
        "Note that an EMPTY value counts as unset — a Helm chart that renders "
        'DATABASE_URL: "" is the usual cause. '
        "To deliberately run on local SQLite outside development, set {0}=1.".format(
            ALLOW_SQLITE_VAR
        )
    )

_engine_cache: dict[str, Engine] = {}


def dispose_engine(url: str, schema: Optional[str] = None) -> bool:
    """Drop one cached engine and close its pooled connections. Returns whether one was cached.

    THE CACHE IS KEYED BY URL AND NOTHING EVER EVICTS, which is correct in a server (a handful of
    URLs, held for the process lifetime) and a slow leak in a test suite: every test that builds
    `Storage(path=<a fresh tempdir>)` mints a new key, and its pool keeps SQLite file handles open
    against a directory the test then deletes. At around 1700 tests that surfaced as
    `sqlite3.OperationalError: unable to open database file` in an unrelated module -- the
    accumulated pools, not the test that happened to be running.

    Exposed rather than left to tests to reach into `_engine_cache`, so the disposal happens
    through the pool's own `dispose()` instead of dropping a reference and hoping.

    A CAP ON THE CACHE WAS TRIED AND REVERTED (2026-09-14). Evicting the oldest entry once the
    cache passed 64 looked like the obvious fix for the leak described above. It made the suite
    WORSE, not better: ~1 flaky failure per run became 3-4. The reason is that "oldest" and
    "unused" are different things -- module-level storages are created early and used late, so
    eviction disposed engines that live Storage objects still held, producing the same
    `unable to open database file` the cap was meant to prevent. Switching FIFO to LRU helped
    and did not fix it, because an engine can be idle for hundreds of tests and still be needed.
    The cache has no way to know what is still referenced, so any eviction policy is a guess.
    Disposal at a point that DOES know -- a test's own teardown, right here -- is the only
    correct answer, and the remaining work is to call it from the ~138 modules that do not.
    """
    cache_key = "{0}#{1}".format(url, schema) if schema else url
    # The session factory holds a reference to the same engine, so dropping only the engine
    # entry would leave a live factory handing out connections from a pool nothing can reach.
    _session_factory_cache.pop(cache_key, None)
    engine = _engine_cache.pop(cache_key, None)
    if engine is None:
        return False
    try:
        engine.dispose()
    except Exception:  # pragma: no cover - a failed dispose must not fail a teardown
        pass
    return True
_session_factory_cache: dict[str, sessionmaker[Session]] = {}


def test_schema_for(path: Optional[str], force_sqlite: bool = False) -> Optional[str]:
    """Map a test's SQLite path to a dedicated Postgres SCHEMA, when asked to.

    Why this exists. `Storage(path=...)` returns a SQLite URL and short-circuits DATABASE_URL
    entirely, so the CI job that claims to run "the same suite against Postgres" actually runs
    most of it on SQLite -- 66 of 124 modules, 797 of 1360 tests. The modules on the wrong side
    of that line are precisely the ones where the two databases differ most: migrations, the
    decision WAL, cross-worker read-after-write, tenant isolation, crypto rotation and the audit
    chain.

    The naive fix -- honour DATABASE_URL whenever it is set -- breaks the suite, because a
    per-test SQLite file is what gives each test an empty database. Point them all at one
    Postgres and they collide on seeded tenants and fixed entity ids.

    So each path gets its own schema instead, which is the Postgres equivalent of its own file.
    Off by default (RULEMIND_TEST_PG_SCHEMA_PER_PATH), so local runs and production are
    untouched; the Postgres CI lane turns it on.

    `force_sqlite` opts a caller out entirely. A handful of tests are ABOUT the SQLite backend --
    backup/restore of a .db file, WAL mode, the key-rotation script run against a file path --
    and they build a `sqlite:///...` URL by hand afterwards. Silently redirecting those to
    Postgres leaves that file empty and they fail with "no such table". They are not Postgres
    tests and must not be run as if they were.
    """
    if not path or force_sqlite or not env.env_bool("RULEMIND_TEST_PG_SCHEMA_PER_PATH"):
        return None
    raw = env.env_opt("DATABASE_URL") or ""
    if not raw.startswith(("postgresql", "postgres")):
        return None
    # Deterministic, and bounded well under Postgres's 63-byte identifier limit. Derived from
    # the resolved path so two Storage() instances on the same temp file share a schema, exactly
    # as they would share a SQLite file.
    digest = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:24]
    return "rmtest_{0}".format(digest)


def database_url(path: Optional[str] = None, force_sqlite: bool = False) -> str:
    if path:
        if test_schema_for(path, force_sqlite):
            # Postgres-backed test: the schema, not the URL, provides the isolation.
            return env.env_opt("DATABASE_URL") or ""
        absolute = Path(path).resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite:///{0}".format(absolute)
    raw = env.env_opt("DATABASE_URL") or _default_database_url()
    if raw.startswith("sqlite:///"):
        absolute = Path(raw.replace("sqlite:///", "", 1)).resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite:///{0}".format(absolute)
    return raw


def engine_for(path: Optional[str] = None, force_sqlite: bool = False) -> Engine:
    url = database_url(path, force_sqlite=force_sqlite)
    schema = test_schema_for(path, force_sqlite)
    # The cache is keyed by URL; under schema-per-path every test shares one URL, so the schema
    # has to be part of the key or the first test's engine would be handed to all of them.
    cache_key = "{0}#{1}".format(url, schema) if schema else url
    engine = _engine_cache.get(cache_key)
    if engine is not None:
        return engine
    _warn_if_sqlite_in_production(url)
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        # check_same_thread=False so the FastAPI threadpool can share connections.
        engine = create_engine(url, future=True, pool_pre_ping=True,
                               connect_args={"check_same_thread": False})
        _enable_sqlite_wal(engine)
    elif schema:
        # ---- the schema-per-path TEST lane -------------------------------------------------
        # Two things here are not preferences.
        #
        # search_path is pinned as a libpq CONNECTION option, not by issuing `SET search_path`
        # from a "connect" event listener. The listener version looks correct and silently does
        # nothing: psycopg2 opens an implicit transaction to run the SET, the pool rolls the
        # connection back on check-in, and `SET` is itself transactional -- so the rollback
        # discards it and every query runs against `public`, which is the shared state this
        # exists to prevent. `-c` is applied by the server at session start and no rollback
        # touches it. The schema need not exist yet; search_path resolves lazily.
        #
        # NullPool, not the tuned pool below. The engine cache holds one engine per test PATH --
        # thousands over a full run -- and a pooled engine keeps its connections open until it
        # is collected. With pool_size 20 the suite exhausted max_connections partway through
        # and 204 tests died on "sorry, too many clients already". NullPool closes on release,
        # so concurrent connections track in-flight use rather than engine count.
        from sqlalchemy.pool import NullPool

        engine = create_engine(url, future=True, poolclass=NullPool,
                               connect_args={"options": "-csearch_path={0}".format(schema),
                                             **_libpq_connect_args(url)})
    else:
        # Tunable connection pool for the target QPS (defaults suit a single
        # replica; raise for higher concurrency, keeping total <= DB max_connections).
        engine = create_engine(
            url, future=True, pool_pre_ping=True,
            connect_args=_libpq_connect_args(url),
            pool_size=env.env_int("DB_POOL_SIZE", 20),
            max_overflow=env.env_int("DB_MAX_OVERFLOW", 20),
            pool_recycle=env.env_int("DB_POOL_RECYCLE", 1800),
            pool_timeout=env.env_int("DB_POOL_TIMEOUT", 30),
        )
    if schema:
        _bind_test_schema(engine, schema)
    _engine_cache[cache_key] = engine
    return engine


def _libpq_connect_args(url: str) -> Dict[str, Any]:
    """libpq connection options every Postgres engine gets: GSSAPI encryption OFF unless asked for.

    libpq defaults to `gssencmode=prefer`, so every connect first tries to negotiate GSSAPI
    (Kerberos) encryption -- initialising the krb5 library, reading its configuration and
    environment -- before falling back. Nothing here uses Kerberos, so that is wasted work on
    every connect and extra C code reading the process environment from whichever thread
    connects. Added while chasing segfaults inside psycopg2.connect in the Linux Postgres lane;
    the trace that settled it showed a TEST mutating os.environ (a glibc getenv/setenv race) --
    see run_all_tests.sh -- so this is defensive rather than the fix. An operator who does use
    Kerberos sets `gssencmode=` in DATABASE_URL or PGGSSENCMODE, and this steps aside.
    """
    if "gssencmode=" in url or os.environ.get("PGGSSENCMODE"):
        return {}
    return {"gssencmode": "disable"}


def _bind_test_schema(engine: Engine, schema: str) -> None:
    """Create the per-test schema. The search_path itself is pinned in `engine_for` via the
    libpq `options` connect arg -- see the comment there for why it cannot be a `SET`.

    This runs eagerly at engine construction, before any caller gets the engine, so `create_all`
    never races the schema into existence.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS "{0}"'.format(schema)))


def drop_test_schema(path: str) -> None:
    """Tear down a per-test schema. Safe to call when schema-per-path is off (a no-op)."""
    schema = test_schema_for(path)
    if not schema:
        return
    from sqlalchemy import text

    engine = engine_for(path)
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA IF EXISTS "{0}" CASCADE'.format(schema)))


def _enable_sqlite_wal(engine: Engine) -> None:
    """WAL lets readers run concurrently with a writer and cuts 'database is locked'
    contention under the concurrent request/simulation load; a busy_timeout makes a
    brief writer collision wait instead of erroring."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial pragma setup
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def session_factory(path: Optional[str] = None, force_sqlite: bool = False) -> sessionmaker[Session]:
    url = database_url(path, force_sqlite=force_sqlite)
    schema = test_schema_for(path, force_sqlite)
    # Same reason as engine_for: under schema-per-path the URL alone is not a unique key.
    cache_key = "{0}#{1}".format(url, schema) if schema else url
    factory = _session_factory_cache.get(cache_key)
    if factory is not None:
        return factory
    factory = sessionmaker(bind=engine_for(path, force_sqlite=force_sqlite), autoflush=False, autocommit=False, future=True)
    _session_factory_cache[cache_key] = factory
    return factory
