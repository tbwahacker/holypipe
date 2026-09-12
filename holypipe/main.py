"""HolyPipe entrypoint: FastAPI app wiring, scheduler lifecycle, static UI."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.auth_routes import router as auth_router
from .api.routes import router as api_router
from .auth import seed_default_admin
from .bus import pump
from .config import settings
from .db import init_db
from .engine.recovery import recover_orphaned_state
from .engine.scheduler import start_scheduler, stop_scheduler
from .logging_util import log
from .mcp_server import asgi_app as mcp_asgi_app
from .mcp_server import mcp as mcp_instance

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web", "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_default_admin()
    recover_orphaned_state()
    pump_task = asyncio.create_task(pump())
    if settings.scheduler_enabled:
        start_scheduler()
        log("HolyPipe scheduler enabled")
    else:
        log("HolyPipe scheduler disabled (HOLYPIPE_SCHEDULER=0)")
    log("HolyPipe API ready")
    # The MCP session manager backs the /mcp Streamable HTTP mount below —
    # it has to be running for the whole app lifetime, entered here rather
    # than left to the sub-app's own lifespan, which Starlette never invokes
    # for a mounted (non-root) ASGI app.
    async with mcp_instance.session_manager.run():
        try:
            yield
        finally:
            stop_scheduler()
            pump_task.cancel()


app = FastAPI(title="HolyPipe", version="1.0.2", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(api_router)
app.mount("/mcp", mcp_asgi_app)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        # Never cache the HTML shell itself — it references versioned
        # (?v=...) static assets, so revalidating it on every load is what
        # lets a new release's JS/CSS actually take effect in the browser
        # instead of silently running a stale cached copy.
        return FileResponse(index_path, headers={"Cache-Control": "no-cache, must-revalidate"})
    return {"status": "ok", "ui": "not built"}
