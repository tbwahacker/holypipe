"""Metadata store: SQLAlchemy engine/session for HolyPipe's own bookkeeping."""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .config import settings

_url = settings.metadata_url
_kwargs: dict = {"future": True, "pool_pre_ping": True}

if _url.startswith("sqlite"):
    path = _url.split("///", 1)[-1]
    if path and path not in (":memory:",):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    _kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(_url, **_kwargs)

if _url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    """Transactional scope; used by background workers that own their session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)

    models.Base.metadata.create_all(engine)
