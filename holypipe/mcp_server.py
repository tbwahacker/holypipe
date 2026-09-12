"""MCP (Model Context Protocol) server exposing HolyPipe to AI agents.

Mounted at /mcp using the Streamable HTTP transport, so any MCP-capable
agent (Claude, Copilot, Codex, etc.) can attach over the network with a URL
+ a HolyPipe API token instead of needing local access to this machine —
see docs/MCP_INTEGRATION.md.

Auth is a thin ASGI wrapper reading `Authorization: Bearer <token>` and
resolving it through the same personal-access-token mechanism the REST API
uses (auth.py) — not the SDK's built-in OAuth provider, which needs a
client-registration flow most of these agents don't implement yet. The
resolved caller is stashed in a contextvar for the duration of each request
so tool functions can enforce permissions exactly like a REST call would.

Tool implementations call the *exact* functions the REST routes call
(imported directly, not re-implemented) so behavior never drifts between
"clicking in the dashboard" and "asking an agent to do it" — the only
difference is these open/close their own DB session instead of getting one
from FastAPI's dependency injection, and run/resync run synchronously
in-thread instead of via BackgroundTasks, which only fires inside an actual
FastAPI request/response cycle.
"""
import contextvars
from functools import wraps

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from sqlalchemy.orm import Session

from .api import routes as api
from .api import schemas as sch
from .auth import AuthUser, _load_auth_user_from_api_token
from .db import SessionLocal
from .engine.cdc import supervisor
from .engine.sync import run_batch_sync
from .models import Connection, SyncRun

_current_user: contextvars.ContextVar[AuthUser | None] = contextvars.ContextVar("_current_user", default=None)


class _Db:
    """Mirrors db.get_session()'s open/close-only behavior for calling route
    functions outside of FastAPI's own request cycle (no auto commit/rollback
    wrapper — each route function already commits itself where needed)."""

    def __enter__(self) -> Session:
        self.session = SessionLocal()
        return self.session

    def __exit__(self, *exc) -> None:
        self.session.close()


def _require_auth() -> AuthUser:
    user = _current_user.get()
    if user is None:
        raise RuntimeError("Not authenticated")
    return user


def _tool(permission: str):
    """Enforces the caller's permission and turns the REST layer's
    HTTPException into a plain message an MCP client can surface to the
    agent (the raw exception type isn't meaningful outside an HTTP
    response)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _require_auth()
            if not user.has(permission):
                raise RuntimeError(f"Missing permission: {permission}")
            try:
                return fn(*args, **kwargs)
            except HTTPException as exc:
                raise RuntimeError(f"{exc.status_code}: {exc.detail}") from None
        return wrapper
    return deco


def _dump(model_cls, row) -> dict:
    return model_cls.model_validate(row).model_dump(mode="json")


mcp = FastMCP("HolyPipe", instructions=(
    "Tools for managing HolyPipe: PostgreSQL/MySQL/SQLite data replication — "
    "sources, destinations, connections, batch syncs, and CDC. Every call "
    "acts with the permissions of the API token used to connect, the same "
    "roles/permissions model as the web dashboard, so calls can 403 with "
    "'Missing permission: ...' depending on what the token's role grants."
))


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@mcp.tool()
@_tool("sources.view")
def list_sources() -> list[dict]:
    """List every configured source connector: id, name, type, config."""
    with _Db() as db:
        return [_dump(sch.ConnectorOut, r) for r in api.list_sources(db)]


@mcp.tool()
@_tool("sources.manage")
def create_source(name: str, type: str, config: dict | None = None, uri: str | None = None) -> dict:
    """Create a source connector. Either pass `config` (fields depend on
    `type`: postgres/mysql/sqlite — call list_connector_types to see them)
    or a connection `uri` to parse instead. Test with test_source_config
    first if unsure the credentials/host are correct."""
    with _Db() as db:
        body = sch.ConnectorIn(name=name, type=type, config=config or {}, uri=uri)
        return _dump(sch.ConnectorOut, api.create_source(body, db))


@mcp.tool()
@_tool("sources.manage")
def update_source(source_id: str, name: str, type: str, config: dict | None = None, uri: str | None = None) -> dict:
    """Replace a source connector's name/type/config (or reparse from a
    pasted connection `uri`)."""
    with _Db() as db:
        body = sch.ConnectorIn(name=name, type=type, config=config or {}, uri=uri)
        return _dump(sch.ConnectorOut, api.update_source(source_id, body, db))


@mcp.tool()
@_tool("sources.manage")
def delete_source(source_id: str) -> dict:
    """Delete a source connector. Fails if a connection still uses it."""
    with _Db() as db:
        return api.delete_source(source_id, db)


@mcp.tool()
@_tool("sources.view")
def test_source(source_id: str) -> dict:
    """Test connectivity for an already-saved source."""
    with _Db() as db:
        return _dump(sch.TestResult, api.test_source(source_id, db))


@mcp.tool()
@_tool("sources.view")
def test_source_config(name: str, type: str, config: dict | None = None, uri: str | None = None) -> dict:
    """Test connectivity for source settings before saving them."""
    body = sch.ConnectorIn(name=name, type=type, config=config or {}, uri=uri)
    return _dump(sch.TestResult, api.test_source_config(body))


@mcp.tool()
@_tool("sources.view")
def discover_source(source_id: str, refresh: bool = False) -> list[dict]:
    """List every table/stream a source exposes, with column names, types,
    and primary keys — use this to decide what to pass as `streams` when
    creating a connection. Cached briefly; pass refresh=true to force a
    rescan after schema changes on the source."""
    with _Db() as db:
        return [_dump(sch.DiscoveredStream, s) for s in api.discover_source(source_id, refresh, db)]


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------
@mcp.tool()
@_tool("destinations.view")
def list_destinations() -> list[dict]:
    """List every configured destination connector: id, name, type, config."""
    with _Db() as db:
        return [_dump(sch.ConnectorOut, r) for r in api.list_destinations(db)]


@mcp.tool()
@_tool("destinations.manage")
def create_destination(name: str, type: str, config: dict | None = None, uri: str | None = None) -> dict:
    """Create a destination connector (postgres/mysql/sqlite)."""
    with _Db() as db:
        body = sch.ConnectorIn(name=name, type=type, config=config or {}, uri=uri)
        return _dump(sch.ConnectorOut, api.create_destination(body, db))


@mcp.tool()
@_tool("destinations.manage")
def update_destination(destination_id: str, name: str, type: str,
                       config: dict | None = None, uri: str | None = None) -> dict:
    """Replace a destination connector's name/type/config."""
    with _Db() as db:
        body = sch.ConnectorIn(name=name, type=type, config=config or {}, uri=uri)
        return _dump(sch.ConnectorOut, api.update_destination(destination_id, body, db))


@mcp.tool()
@_tool("destinations.manage")
def delete_destination(destination_id: str) -> dict:
    """Delete a destination connector. Fails if a connection still uses it."""
    with _Db() as db:
        return api.delete_destination(destination_id, db)


@mcp.tool()
@_tool("destinations.view")
def test_destination(destination_id: str) -> dict:
    """Test connectivity for an already-saved destination."""
    with _Db() as db:
        return _dump(sch.TestResult, api.test_destination(destination_id, db))


@mcp.tool()
@_tool("destinations.view")
def test_destination_config(name: str, type: str, config: dict | None = None, uri: str | None = None) -> dict:
    """Test connectivity for destination settings before saving them."""
    body = sch.ConnectorIn(name=name, type=type, config=config or {}, uri=uri)
    return _dump(sch.TestResult, api.test_destination_config(body))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
@mcp.tool()
@_tool("connections.view")
def list_connections() -> list[dict]:
    """List every connection with its status, schedule, and stream config."""
    with _Db() as db:
        return [_dump(sch.ConnectionOut, r) for r in api.list_connections(db)]


@mcp.tool()
@_tool("connections.view")
def get_connection(connection_id: str) -> dict:
    """Get one connection's full detail, including its per-stream config."""
    with _Db() as db:
        return _dump(sch.ConnectionOut, api.get_connection(connection_id, db))


@mcp.tool()
@_tool("connections.manage")
def create_connection(name: str, source_id: str, destination_id: str, streams: list[dict],
                      mode: str = "batch", schedule_type: str = "interval", interval_seconds: int = 60,
                      cron_expression: str | None = None, enabled: bool = True,
                      destination_namespace: str | None = None, table_prefix: str = "") -> dict:
    """Create a connection. `streams` is the list returned by
    discover_source, each augmented with: selected (bool), sync_mode
    ('full_refresh'|'incremental'|'xmin'), cursor_field (for 'incremental'),
    primary_key (list[str], needed for upsert/xmin), destination_table
    (optional override), columns (optional subset, None = all). mode is
    'batch' (scheduled) or 'cdc' (real-time, source must support it)."""
    with _Db() as db:
        body = sch.ConnectionIn(
            name=name, source_id=source_id, destination_id=destination_id, mode=mode,
            schedule_type=schedule_type, interval_seconds=interval_seconds,
            cron_expression=cron_expression, enabled=enabled,
            destination_namespace=destination_namespace, table_prefix=table_prefix, streams=streams,
        )
        return _dump(sch.ConnectionOut, api.create_connection(body, db))


@mcp.tool()
@_tool("connections.manage")
def update_connection(connection_id: str, name: str | None = None, mode: str | None = None,
                      schedule_type: str | None = None, interval_seconds: int | None = None,
                      cron_expression: str | None = None, enabled: bool | None = None,
                      destination_namespace: str | None = None, table_prefix: str | None = None,
                      streams: list[dict] | None = None) -> dict:
    """Partially update a connection — only pass the fields to change.
    Common uses: enabled=false to pause, streams=[...] to change which
    tables/columns sync, interval_seconds to reschedule."""
    with _Db() as db:
        body = sch.ConnectionPatch(
            name=name, mode=mode, schedule_type=schedule_type, interval_seconds=interval_seconds,
            cron_expression=cron_expression, enabled=enabled,
            destination_namespace=destination_namespace, table_prefix=table_prefix, streams=streams,
        )
        return _dump(sch.ConnectionOut, api.patch_connection(connection_id, body, db))


@mcp.tool()
@_tool("connections.manage")
def delete_connection(connection_id: str) -> dict:
    """Delete a connection and its run history."""
    with _Db() as db:
        return api.delete_connection(connection_id, db)


@mcp.tool()
@_tool("connections.operate")
def run_sync(connection_id: str) -> dict:
    """Run a batch sync right now and wait for it to finish (not for CDC
    connections — use start_cdc for those). Can take a while for large
    tables; returns the finished run's status, rows read/written, and any
    error once complete."""
    with _Db() as db:
        conn = db.get(Connection, connection_id)
        if not conn:
            raise RuntimeError("Connection not found")
        if conn.mode == "cdc":
            raise RuntimeError("CDC connections stream continuously; use start_cdc instead")
        if conn.status == "running":
            raise RuntimeError("A sync is already running for this connection")
    run_id = run_batch_sync(connection_id, trigger="mcp")
    with _Db() as db:
        return _dump(sch.SyncRunOut, db.get(SyncRun, run_id))


@mcp.tool()
@_tool("connections.operate")
def resync_connection(connection_id: str, stream_names: list[str] | None = None) -> dict:
    """Force a full reload. For a batch connection this drops the stored
    incremental/xmin cursor for `stream_names` (or every selected stream)
    and re-syncs from scratch, blocking until done. For a CDC connection it
    restarts the worker to redo the initial snapshot (stream_names isn't
    supported there — the snapshot covers the whole connection)."""
    with _Db() as db:
        conn = db.get(Connection, connection_id)
        if not conn:
            raise RuntimeError("Connection not found")
        valid_names = {s["schema"]["name"] for s in conn.streams}
        if stream_names:
            unknown = set(stream_names) - valid_names
            if unknown:
                raise RuntimeError(f"Unknown stream(s): {', '.join(sorted(unknown))}")

        if conn.mode == "cdc":
            if stream_names:
                raise RuntimeError("Per-table resync isn't available for CDC connections")
            state = dict(conn.state or {})
            state.pop("snapshot_done", None)
            conn.state = state
            conn.enabled = True
            db.commit()
            supervisor.stop(connection_id)
            supervisor.start(connection_id)
            return {"ok": True, "mode": "cdc"}

        if conn.status == "running":
            raise RuntimeError("A sync is already running for this connection")

        targets = stream_names or sorted(valid_names)
        state = dict(conn.state or {})
        cursors = dict(state.get("cursors") or {})
        for name in targets:
            cursors.pop(name, None)
        state["cursors"] = cursors
        conn.state = state
        conn.enabled = True
        db.commit()

    run_id = run_batch_sync(connection_id, trigger="resync", only_streams=targets)
    with _Db() as db:
        return _dump(sch.SyncRunOut, db.get(SyncRun, run_id))


@mcp.tool()
@_tool("connections.operate")
def start_cdc(connection_id: str) -> dict:
    """Start (or resume) real-time CDC streaming for a cdc-mode connection."""
    with _Db() as db:
        return api.start_cdc(connection_id, db)


@mcp.tool()
@_tool("connections.operate")
def stop_cdc(connection_id: str) -> dict:
    """Stop real-time CDC streaming for a connection (and pause its
    schedule — it won't auto-restart until start_cdc is called again)."""
    with _Db() as db:
        return api.stop_cdc(connection_id, db)


@mcp.tool()
@_tool("connections.view")
def list_runs(connection_id: str, limit: int = 50) -> list[dict]:
    """List recent sync runs for a connection — status, rows read/written,
    timing, and any error — most recent first."""
    with _Db() as db:
        return [_dump(sch.SyncRunOut, r) for r in api.list_runs(connection_id, limit, db)]


# ---------------------------------------------------------------------------
# Logs / connector metadata
# ---------------------------------------------------------------------------
@mcp.tool()
@_tool("logs.view")
def get_logs(connection_id: str | None = None, limit: int = 200) -> list[dict]:
    """Tail recent log lines, optionally scoped to one connection."""
    with _Db() as db:
        return [_dump(sch.LogOut, r) for r in api.get_logs(connection_id, limit, db)]


@mcp.tool()
def list_connector_types() -> dict:
    """List every supported source/destination type and the config fields
    each one needs — use this before create_source/create_destination to
    know what belongs in `config`."""
    _require_auth()
    return api.connector_types()


# ---------------------------------------------------------------------------
# ASGI mounting — Streamable HTTP transport with Bearer-token auth
# ---------------------------------------------------------------------------
# FastMCP's own app mounts its handler at settings.streamable_http_path
# ("/mcp" by default) *inside itself* — since main.py already mounts this
# whole module's asgi_app at "/mcp", leaving that default would require
# clients to hit "/mcp/mcp". Point it at root here so "/mcp" (from main.py)
# is the only prefix in the final URL.
mcp.settings.streamable_http_path = "/"
_inner_app = mcp.streamable_http_app()  # also lazily creates mcp.session_manager


async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        await _inner_app(scope, receive, send)
        return
    headers = dict(scope.get("headers") or [])
    raw = headers.get(b"authorization", b"").decode("latin-1")
    if not raw.lower().startswith("bearer "):
        await JSONResponse({"error": "Missing Authorization: Bearer <token> header"}, status_code=401)(
            scope, receive, send)
        return
    user = _load_auth_user_from_api_token(raw[7:].strip())
    if not user:
        await JSONResponse({"error": "Invalid, expired, or revoked API token"}, status_code=401)(
            scope, receive, send)
        return
    reset_token = _current_user.set(user)
    try:
        await _inner_app(scope, receive, send)
    finally:
        _current_user.reset(reset_token)
