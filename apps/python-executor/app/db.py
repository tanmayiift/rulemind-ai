from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


DEFAULT_SQLITE_URL = "sqlite:///.runtime/rulemind_v4.db"

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
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
    _engine_cache[url] = engine
    return engine


def session_factory(path: Optional[str] = None) -> sessionmaker[Session]:
    url = database_url(path)
    factory = _session_factory_cache.get(url)
    if factory is not None:
        return factory
    factory = sessionmaker(bind=engine_for(path), autoflush=False, autocommit=False, future=True)
    _session_factory_cache[url] = factory
    return factory
