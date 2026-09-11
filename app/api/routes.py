"""HTTP + websocket API for HolyPipe."""
from __future__ import annotations

import datetime as dt

from croniter import croniter
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bus import encode, subscribe, unsubscribe
from ..cache import discovery_cache
from ..connectors import DESTINATION_TYPES, SOURCE_TYPES, build_destination, build_source, config_spec
from ..connectors.base import ConnectorError
from ..connectors.dsn import DsnError, parse_dsn
from ..db import get_session, session_scope
from ..engine.cdc import supervisor
from ..engine.sync import run_batch_sync
from ..models import Connection, Destination, LogEntry, Source, SyncRun
from ..timeutil import utcnow
from . import schemas as sch

router = APIRouter(prefix="/api")


def _resolve_config(body: sch.ConnectorIn) -> dict:
    """Config from the form fields, or parsed from a pasted connection URI."""
    if body.uri:
        try:
            return parse_dsn(body.type, body.uri)
        except DsnError as exc:
            raise HTTPException(400, str(exc)) from exc
    return body.config


def _next_cron_fire(expression: str, base: dt.datetime | None = None) -> dt.datetime:
    try:
        itr = croniter(expression, base or utcnow())
        return itr.get_next(dt.datetime)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, f"Invalid cron expression: {exc}") from exc


# ---------------------------------------------------------------------------
# Connector metadata
# ---------------------------------------------------------------------------
@router.get("/connector-types")
def connector_types():
    def describe(types: dict, kind: str) -> list[dict]:
        return [{"type": t, "fields": config_spec(kind, t)} for t in types]

    return {
        "sources": describe(SOURCE_TYPES, "source"),
        "destinations": describe(DESTINATION_TYPES, "destination"),
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@router.get("/sources", response_model=list[sch.ConnectorOut])
def list_sources(db: Session = Depends(get_session)):
    return db.execute(select(Source).order_by(Source.created_at.desc())).scalars().all()


@router.post("/sources", response_model=sch.ConnectorOut)
def create_source(body: sch.ConnectorIn, db: Session = Depends(get_session)):
    if body.type not in SOURCE_TYPES:
        raise HTTPException(400, f"Unknown source type '{body.type}'")
    if db.execute(select(Source).where(Source.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, "A source with this name already exists")
    src = Source(name=body.name, type=body.type, config=_resolve_config(body))
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


@router.get("/sources/{source_id}", response_model=sch.ConnectorOut)
def get_source(source_id: str, db: Session = Depends(get_session)):
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    return src


@router.put("/sources/{source_id}", response_model=sch.ConnectorOut)
def update_source(source_id: str, body: sch.ConnectorIn, db: Session = Depends(get_session)):
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    src.name, src.type, src.config = body.name, body.type, _resolve_config(body)
    db.commit()
    db.refresh(src)
    discovery_cache.invalidate(source_id)
    return src


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, db: Session = Depends(get_session)):
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    if src.connections:
        raise HTTPException(409, "Source is used by an existing connection")
    db.delete(src)
    db.commit()
    discovery_cache.invalidate(source_id)
    return {"ok": True}


@router.post("/sources/{source_id}/test", response_model=sch.TestResult)
def test_source(source_id: str, db: Session = Depends(get_session)):
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    try:
        conn = build_source(src.type, src.config)
        message = conn.test()
        conn.close()
        return sch.TestResult(ok=True, message=message)
    except ConnectorError as exc:
        return sch.TestResult(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001
        return sch.TestResult(ok=False, message=str(exc))


@router.post("/sources/test-config", response_model=sch.TestResult)
def test_source_config(body: sch.ConnectorIn):
    if body.type not in SOURCE_TYPES:
        raise HTTPException(400, f"Unknown source type '{body.type}'")
    try:
        conn = build_source(body.type, _resolve_config(body))
        message = conn.test()
        conn.close()
        return sch.TestResult(ok=True, message=message)
    except Exception as exc:  # noqa: BLE001
        return sch.TestResult(ok=False, message=str(exc))


@router.get("/sources/{source_id}/discover", response_model=list[sch.DiscoveredStream])
def discover_source(source_id: str, refresh: bool = False, db: Session = Depends(get_session)):
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "Source not found")

    if refresh:
        discovery_cache.invalidate(source_id)

    cached = discovery_cache.get(source_id)
    if cached is not None:
        return cached

    try:
        conn = build_source(src.type, src.config)
        streams = conn.discover()
        conn.close()
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc

    result = [
        sch.DiscoveredStream(
            name=s.name, table=s.table, namespace=s.namespace,
            columns=[sch.DiscoveredColumn(**c.as_dict()) for c in s.columns],
            primary_key=s.primary_key,
        )
        for s in streams
    ]
    # Schema discovery scans information_schema/sqlite_master, which can be
    # slow on databases with thousands of tables — cache briefly so opening
    # the connection wizard repeatedly doesn't re-scan every time.
    discovery_cache.set(source_id, result)
    return result


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------
@router.get("/destinations", response_model=list[sch.ConnectorOut])
def list_destinations(db: Session = Depends(get_session)):
    return db.execute(select(Destination).order_by(Destination.created_at.desc())).scalars().all()


@router.post("/destinations", response_model=sch.ConnectorOut)
def create_destination(body: sch.ConnectorIn, db: Session = Depends(get_session)):
    if body.type not in DESTINATION_TYPES:
        raise HTTPException(400, f"Unknown destination type '{body.type}'")
    if db.execute(select(Destination).where(Destination.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, "A destination with this name already exists")
    dst = Destination(name=body.name, type=body.type, config=_resolve_config(body))
    db.add(dst)
    db.commit()
    db.refresh(dst)
    return dst


@router.get("/destinations/{destination_id}", response_model=sch.ConnectorOut)
def get_destination(destination_id: str, db: Session = Depends(get_session)):
    dst = db.get(Destination, destination_id)
    if not dst:
        raise HTTPException(404, "Destination not found")
    return dst


@router.put("/destinations/{destination_id}", response_model=sch.ConnectorOut)
def update_destination(destination_id: str, body: sch.ConnectorIn, db: Session = Depends(get_session)):
    dst = db.get(Destination, destination_id)
    if not dst:
        raise HTTPException(404, "Destination not found")
    dst.name, dst.type, dst.config = body.name, body.type, _resolve_config(body)
    db.commit()
    db.refresh(dst)
    return dst


@router.delete("/destinations/{destination_id}")
def delete_destination(destination_id: str, db: Session = Depends(get_session)):
    dst = db.get(Destination, destination_id)
    if not dst:
        raise HTTPException(404, "Destination not found")
    if dst.connections:
        raise HTTPException(409, "Destination is used by an existing connection")
    db.delete(dst)
    db.commit()
    return {"ok": True}


@router.post("/destinations/{destination_id}/test", response_model=sch.TestResult)
def test_destination(destination_id: str, db: Session = Depends(get_session)):
    dst = db.get(Destination, destination_id)
    if not dst:
        raise HTTPException(404, "Destination not found")
    try:
        conn = build_destination(dst.type, dst.config)
        message = conn.test()
        conn.close()
        return sch.TestResult(ok=True, message=message)
    except Exception as exc:  # noqa: BLE001
        return sch.TestResult(ok=False, message=str(exc))


@router.post("/destinations/test-config", response_model=sch.TestResult)
def test_destination_config(body: sch.ConnectorIn):
    if body.type not in DESTINATION_TYPES:
        raise HTTPException(400, f"Unknown destination type '{body.type}'")
    try:
        conn = build_destination(body.type, _resolve_config(body))
        message = conn.test()
        conn.close()
        return sch.TestResult(ok=True, message=message)
    except Exception as exc:  # noqa: BLE001
        return sch.TestResult(ok=False, message=str(exc))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
@router.get("/connections", response_model=list[sch.ConnectionOut])
def list_connections(db: Session = Depends(get_session)):
    return db.execute(select(Connection).order_by(Connection.created_at.desc())).scalars().all()


@router.post("/connections", response_model=sch.ConnectionOut)
def create_connection(body: sch.ConnectionIn, db: Session = Depends(get_session)):
    source = db.get(Source, body.source_id)
    destination = db.get(Destination, body.destination_id)
    if not source or not destination:
        raise HTTPException(404, "Source or destination not found")
    if body.mode not in ("batch", "cdc"):
        raise HTTPException(400, "mode must be 'batch' or 'cdc'")
    if body.mode == "cdc" and not SOURCE_TYPES[source.type].supports_cdc:
        raise HTTPException(400, f"{source.type} source does not support CDC")
    if body.schedule_type not in ("interval", "cron"):
        raise HTTPException(400, "schedule_type must be 'interval' or 'cron'")
    for stream in body.streams:
        if stream.get("sync_mode") == "xmin" and source.type != "postgres":
            raise HTTPException(400, "xmin update method is only available for PostgreSQL sources")
    if db.execute(select(Connection).where(Connection.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, "A connection with this name already exists")

    now = utcnow()
    next_run = now
    if body.mode == "batch" and body.schedule_type == "cron":
        next_run = _next_cron_fire(body.cron_expression or "", now)

    conn = Connection(
        name=body.name, source_id=body.source_id, destination_id=body.destination_id,
        mode=body.mode, schedule_type=body.schedule_type, interval_seconds=body.interval_seconds,
        cron_expression=body.cron_expression, enabled=body.enabled,
        destination_namespace=body.destination_namespace, table_prefix=body.table_prefix,
        streams=body.streams, state={}, next_run_at=next_run,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@router.get("/connections/{connection_id}", response_model=sch.ConnectionOut)
def get_connection(connection_id: str, db: Session = Depends(get_session)):
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    return conn


@router.patch("/connections/{connection_id}", response_model=sch.ConnectionOut)
def patch_connection(connection_id: str, body: sch.ConnectionPatch, db: Session = Depends(get_session)):
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("schedule_type") not in (None, "interval", "cron"):
        raise HTTPException(400, "schedule_type must be 'interval' or 'cron'")
    new_mode = data.get("mode", conn.mode)
    if new_mode not in ("batch", "cdc"):
        raise HTTPException(400, "mode must be 'batch' or 'cdc'")
    if new_mode == "cdc" and not SOURCE_TYPES[conn.source.type].supports_cdc:
        raise HTTPException(400, f"{conn.source.type} source does not support CDC")
    for stream in data.get("streams", []):
        if stream.get("sync_mode") == "xmin" and conn.source.type != "postgres":
            raise HTTPException(400, "xmin update method is only available for PostgreSQL sources")
    mode_changed = "mode" in data and data["mode"] != conn.mode
    schedule_changed = "schedule_type" in data or "cron_expression" in data
    for k, v in data.items():
        setattr(conn, k, v)
    if conn.schedule_type == "cron" and not conn.cron_expression:
        raise HTTPException(400, "cron_expression is required when schedule_type is 'cron'")
    if mode_changed and conn.mode == "batch":
        conn.next_run_at = utcnow()
    elif schedule_changed and conn.mode == "batch" and conn.schedule_type == "cron":
        conn.next_run_at = _next_cron_fire(conn.cron_expression)
    db.commit()
    db.refresh(conn)
    if mode_changed and conn.mode == "batch":
        supervisor.stop(connection_id)
    return conn


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: str, db: Session = Depends(get_session)):
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    supervisor.stop(connection_id)
    db.delete(conn)
    db.commit()
    return {"ok": True}


@router.post("/connections/{connection_id}/run")
def trigger_run(connection_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_session)):
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    if conn.mode == "cdc":
        raise HTTPException(400, "CDC connections stream continuously; use /start instead")
    if conn.status == "running":
        raise HTTPException(409, "A sync is already running for this connection")

    background_tasks.add_task(run_batch_sync, connection_id, trigger="manual")
    return {"ok": True, "status": "started"}


@router.post("/connections/{connection_id}/resync")
def resync_connection(connection_id: str, body: sch.ResyncRequest, background_tasks: BackgroundTasks,
                      db: Session = Depends(get_session)):
    """Forces a full reload — for incremental/xmin streams this drops the
    stored cursor first so the next run re-reads everything instead of just
    the delta, unlike a plain /run. For CDC, restarts the worker with the
    initial-snapshot flag cleared so it re-copies every current row before
    resuming streaming. `stream_names` (optional) scopes it to specific
    tables instead of the whole connection — CDC doesn't support this since
    its snapshot is connection-wide, not per-table."""
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    valid_names = {s["schema"]["name"] for s in conn.streams}
    if body.stream_names:
        unknown = set(body.stream_names) - valid_names
        if unknown:
            raise HTTPException(400, f"Unknown stream(s): {', '.join(sorted(unknown))}")

    if conn.mode == "cdc":
        if body.stream_names:
            raise HTTPException(400, "Per-table resync isn't available for CDC connections "
                                     "(the initial snapshot covers the whole connection) — "
                                     "resync without stream_names to redo it for every table.")
        state = dict(conn.state or {})
        state.pop("snapshot_done", None)
        conn.state = state
        conn.enabled = True
        db.commit()
        supervisor.stop(connection_id)
        supervisor.start(connection_id)
        return {"ok": True, "mode": "cdc"}

    if conn.status == "running":
        raise HTTPException(409, "A sync is already running for this connection")

    targets = body.stream_names or sorted(valid_names)
    state = dict(conn.state or {})
    cursors = dict(state.get("cursors") or {})
    for name in targets:
        cursors.pop(name, None)
    state["cursors"] = cursors
    conn.state = state
    conn.enabled = True  # an explicit resync click should work even while paused
    db.commit()

    background_tasks.add_task(run_batch_sync, connection_id, trigger="resync", only_streams=targets)
    return {"ok": True, "mode": "batch", "streams": targets}


@router.post("/connections/{connection_id}/start")
def start_cdc(connection_id: str, db: Session = Depends(get_session)):
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    if conn.mode != "cdc":
        raise HTTPException(400, "Connection is not in CDC mode")
    conn.enabled = True
    db.commit()
    supervisor.start(connection_id)
    return {"ok": True}


@router.post("/connections/{connection_id}/stop")
def stop_cdc(connection_id: str, db: Session = Depends(get_session)):
    conn = db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    # Must clear `enabled`, not just kill the worker — the scheduler treats
    # every enabled CDC connection as "should be running" and would restart
    # this on its very next tick otherwise.
    conn.enabled = False
    conn.status = "idle"
    db.commit()
    supervisor.stop(connection_id)
    return {"ok": True}


@router.get("/connections/{connection_id}/runs", response_model=list[sch.SyncRunOut])
def list_runs(connection_id: str, limit: int = 50, db: Session = Depends(get_session)):
    q = (select(SyncRun).where(SyncRun.connection_id == connection_id)
         .order_by(SyncRun.started_at.desc()).limit(min(limit, 200)))
    return db.execute(q).scalars().all()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
@router.get("/logs", response_model=list[sch.LogOut])
def get_logs(connection_id: str | None = None, limit: int = 200, db: Session = Depends(get_session)):
    q = select(LogEntry)
    if connection_id:
        q = q.where(LogEntry.connection_id == connection_id)
    q = q.order_by(LogEntry.id.desc()).limit(min(limit, 1000))
    rows = list(db.execute(q).scalars().all())
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# Live events
# ---------------------------------------------------------------------------
@router.websocket("/ws")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    queue = subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_text(encode(event))
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe(queue)
