"""Cleans up state left behind by an ungraceful shutdown.

A container restart (redeploy, crash, `docker stop`) kills any in-flight
sync thread without warning — nothing ever sets its SyncRun or Connection
status back from "running"/"streaming", so without this the connection is
permanently stuck refusing every future run/resync with a 409, even though
nothing is actually running anymore. Runs once at API startup, before the
scheduler starts picking connections back up.
"""
from __future__ import annotations

from ..db import session_scope
from ..logging_util import log
from ..models import Connection, SyncRun
from ..timeutil import utcnow


def recover_orphaned_state() -> None:
    with session_scope() as s:
        stuck_runs = s.query(SyncRun).filter(SyncRun.status == "running").all()
        for run in stuck_runs:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = "Interrupted — HolyPipe restarted while this run was in progress"

        stuck_conns = s.query(Connection).filter(Connection.status.in_(["running", "streaming"])).all()
        for conn in stuck_conns:
            conn.status = "idle"
            conn.status_detail = None

    if stuck_runs:
        log(f"Recovered {len(stuck_runs)} orphaned run(s) left mid-flight by a previous process",
           level="WARNING")
    if stuck_conns:
        names = ", ".join(c.name for c in stuck_conns)
        log(f"Reset {len(stuck_conns)} connection(s) stuck running/streaming: {names}",
           level="WARNING")
