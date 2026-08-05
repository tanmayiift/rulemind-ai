from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


logger = logging.getLogger("rulemind.db")

# Postgres is the intended production datastore; SQLite is for local dev. In production, set
# DATABASE_URL to a Postgres DSN. Dev can switch to Postgres too (just set DATABASE_URL).
DEFAULT_SQLITE_URL = "sqlite:///.runtime/rulemind_v4.db"


def _warn_if_sqlite_in_production(url: str) -> None:
    """SQLite is single-writer; fine for dev, a scaling ceiling in production. Warn (don't fail —
    some deploys intentionally run SQLite) so operators default to Postgres in prod."""
    if not url.startswith("sqlite"):
        return
    if os.getenv("NODE_ENV", "development") == "development" or os.getenv("PYTEST_CURRENT_TEST"):
        return
    logger.warning(
        "Running on SQLite in a non-development environment. SQLite is single-writer and will "
        "cap throughput; set DATABASE_URL to a Postgres DSN for production."
    )

_engine_cache: dict[str, Engine] = {}
_session_factory_cache: dict[str, sessionmaker[Session]] = {}


def database_url(path: Optional[str] = None) -> str:
    if path:
        absolute = Path(path).resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite:///{0}".format(absolute)
    raw = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)
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
            pool_size=int(os.getenv("DB_POOL_SIZE", "20")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
            pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
            pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
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
