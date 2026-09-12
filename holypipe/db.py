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
    _migrate_schema()


def _migrate_schema() -> None:
    """create_all() only adds missing tables, never columns on ones that
    already exist — anything added to an existing table needs its own small
    ALTER TABLE here, mirroring the pattern every connector's own prepare()
    already uses for the tables *it* manages."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "connections" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("connections")}
    if "group_id" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE connections ADD COLUMN group_id VARCHAR(32)"))
    _backfill_connection_groups()


def _backfill_connection_groups() -> None:
    """Connections created before group_id existed re-cluster here by the
    same (destination, base name) signature app.js's fanOutConnections uses
    when naming siblings ("<base>" / "<base> — <source>"), so pre-existing
    multi-source setups group correctly in the UI without needing to be
    recreated. Safe to run every startup: already-grouped connections are
    skipped, and it's a no-op once nothing ungrouped remains."""
    import uuid
    from collections import defaultdict

    from . import models

    with session_scope() as s:
        ungrouped = s.query(models.Connection).filter(models.Connection.group_id.is_(None)).all()
        clusters: dict[tuple[str, str], list] = defaultdict(list)
        for conn in ungrouped:
            base_name = conn.name.rsplit(" — ", 1)[0] if " — " in conn.name else conn.name
            clusters[(conn.destination_id, base_name)].append(conn)
        for members in clusters.values():
            if len(members) < 2:
                continue
            group_id = uuid.uuid4().hex
            for conn in members:
                conn.group_id = group_id
