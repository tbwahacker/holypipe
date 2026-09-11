"""Structured logging that lands in the metadata DB, the websocket bus and stdout."""
from __future__ import annotations

import logging
import sys

from .bus import publish
from .config import settings
from .db import session_scope
from .models import LogEntry

_stdout = logging.getLogger("holypipe")
if not _stdout.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    _stdout.addHandler(handler)
    _stdout.setLevel(getattr(logging, settings.log_level, logging.INFO))


def log(message: str, *, level: str = "INFO", connection_id: str | None = None,
        run_id: str | None = None, persist: bool = True) -> None:
    _stdout.log(getattr(logging, level, logging.INFO), "[%s] %s", connection_id or "-", message)
    publish("log", connection_id=connection_id, run_id=run_id, level=level, message=message)
    if not persist:
        return
    try:
        with session_scope() as s:
            s.add(LogEntry(connection_id=connection_id, run_id=run_id, level=level, message=message[:8000]))
    except Exception:  # logging must never break a sync
        _stdout.exception("failed to persist log entry")


def prune_logs(keep: int | None = None) -> None:
    keep = keep or settings.log_retention
    try:
        with session_scope() as s:
            ids = [r[0] for r in s.query(LogEntry.id).order_by(LogEntry.id.desc()).offset(keep).limit(5000)]
            if ids:
                s.query(LogEntry).filter(LogEntry.id.in_(ids)).delete(synchronize_session=False)
    except Exception:
        pass
