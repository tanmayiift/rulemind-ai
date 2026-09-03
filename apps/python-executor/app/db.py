from __future__ import annotations

import logging
import os

from . import env
from pathlib import Path
from typing import Optional

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
_session_factory_cache: dict[str, sessionmaker[Session]] = {}


def database_url(path: Optional[str] = None) -> str:
    if path:
        absolute = Path(path).resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite:///{0}".format(absolute)
    raw = env.env_opt("DATABASE_URL") or _default_database_url()
    if raw.startswith("sqlite:///"):
        absolute = Path(raw.replace("sqlite:///", "", 1)).resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite:///{0}".format(absolute)
    return raw


def engine_for(path: Optional[str] = None) -> Engine:
    url = database_url(path)
    engine = _engine_cache.get(url)
    if engine is not None:
        return engine
    _warn_if_sqlite_in_production(url)
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        # check_same_thread=False so the FastAPI threadpool can share connections.
        engine = create_engine(url, future=True, pool_pre_ping=True,
                               connect_args={"check_same_thread": False})
        _enable_sqlite_wal(engine)
    else:
        # Tunable connection pool for the target QPS (defaults suit a single
        # replica; raise for higher concurrency, keeping total <= DB max_connections).
        engine = create_engine(
            url, future=True, pool_pre_ping=True,
            pool_size=env.env_int("DB_POOL_SIZE", 20),
            max_overflow=env.env_int("DB_MAX_OVERFLOW", 20),
            pool_recycle=env.env_int("DB_POOL_RECYCLE", 1800),
            pool_timeout=env.env_int("DB_POOL_TIMEOUT", 30),
        )
    _engine_cache[url] = engine
    return engine


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


def session_factory(path: Optional[str] = None) -> sessionmaker[Session]:
    url = database_url(path)
    factory = _session_factory_cache.get(url)
    if factory is not None:
        return factory
    factory = sessionmaker(bind=engine_for(path), autoflush=False, autocommit=False, future=True)
    _session_factory_cache[url] = factory
    return factory
