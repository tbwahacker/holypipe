"""Polls enabled connections and dispatches batch syncs / CDC workers on schedule."""
from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from croniter import croniter

from ..config import settings
from ..db import session_scope
from ..logging_util import log, prune_logs
from ..models import Connection
from ..timeutil import utcnow
from .cdc import supervisor
from .sync import run_batch_sync


#: CDC retry backoff: min/max delay (seconds) between restart attempts for a
#: connection whose stream keeps failing (bad credentials, wal_level not set,
#: network down, ...). Without this, a persistently-broken CDC source gets
#: hammered with a fresh connection attempt every poll tick.
CDC_BACKOFF_BASE = 5.0
CDC_BACKOFF_MAX = 300.0


class Scheduler(threading.Thread):
    def __init__(self, poll_seconds: float = 2.0, max_workers: int | None = None):
        super().__init__(name="holypipe-scheduler", daemon=True)
        max_workers = max_workers or settings.scheduler_workers
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sync")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._cdc_backoff: dict[str, dict] = {}  # connection_id -> {"fails": int, "next_attempt": float}

    def stop(self) -> None:
        self.stop_event.set()
        supervisor.stop_all()
        self._pool.shutdown(wait=False)

    def run(self) -> None:
        log("Scheduler started")
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log(f"Scheduler tick error: {exc}", level="ERROR", persist=False)
            self.stop_event.wait(self.poll_seconds)

    def _tick(self) -> None:
        now = utcnow()
        with session_scope() as s:
            connections = s.query(Connection).filter(Connection.enabled.is_(True)).all()
            due, cdc_wanted = [], []
            for c in connections:
                if c.mode == "cdc":
                    cdc_wanted.append(c.id)
                    continue
                if c.status == "running":
                    continue
                next_run = c.next_run_at
                if next_run is None or next_run <= now:
                    due.append(c.id)
                    c.next_run_at = self._compute_next_run(c, now)

        running_cdc = set(supervisor.running_ids())
        now_mono = time.monotonic()
        for cid in cdc_wanted:
            if cid in running_cdc:
                self._cdc_backoff.pop(cid, None)  # healthy; clear any prior backoff
                continue
            backoff = self._cdc_backoff.get(cid)
            if backoff and now_mono < backoff["next_attempt"]:
                continue  # still cooling down after a recent failure
            fails = (backoff["fails"] + 1) if backoff else 1
            delay = min(CDC_BACKOFF_BASE * (2 ** (fails - 1)), CDC_BACKOFF_MAX)
            self._cdc_backoff[cid] = {"fails": fails, "next_attempt": now_mono + delay}
            if fails > 1:
                log(f"CDC restart attempt {fails} — backing off {delay:.0f}s if it fails again",
                   connection_id=cid, level="WARNING")
            supervisor.start(cid)
        for cid in list(running_cdc):
            if cid not in cdc_wanted:
                supervisor.stop(cid)
                self._cdc_backoff.pop(cid, None)

        with self._lock:
            due = [cid for cid in due if cid not in self._inflight]
            self._inflight.update(due)

        for cid in due:
            self._pool.submit(self._run_one, cid)

        if time.time() - self._last_prune > 300:
            self._last_prune = time.time()
            prune_logs()

    @staticmethod
    def _compute_next_run(connection: Connection, now: dt.datetime) -> dt.datetime:
        if connection.schedule_type == "cron" and connection.cron_expression:
            try:
                return croniter(connection.cron_expression, now).get_next(dt.datetime)
            except (ValueError, KeyError):
                pass  # fall through to interval-based scheduling on a bad expression
        return now + dt.timedelta(seconds=max(5, connection.interval_seconds))

    def _run_one(self, connection_id: str) -> None:
        try:
            run_batch_sync(connection_id, trigger="schedule")
        except Exception as exc:  # noqa: BLE001
            log(f"Scheduled sync failed: {exc}", level="ERROR", connection_id=connection_id)
        finally:
            with self._lock:
                self._inflight.discard(connection_id)


_scheduler: Scheduler | None = None


def start_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = Scheduler()
        _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
