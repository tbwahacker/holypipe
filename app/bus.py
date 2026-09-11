"""Thread-safe event bus bridging sync worker threads to the asyncio websocket layer."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import queue
import threading
from typing import Any

_events: "queue.Queue[dict]" = queue.Queue(maxsize=10000)
_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def publish(event_type: str, **payload: Any) -> None:
    """Called from any thread. Never blocks the caller."""
    event = {"type": event_type, "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **payload}
    try:
        _events.put_nowait(event)
    except queue.Full:  # pragma: no cover - drop oldest under pressure
        try:
            _events.get_nowait()
            _events.put_nowait(event)
        except queue.Empty:
            pass


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def _fanout(event: dict) -> None:
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def pump() -> None:
    """Background asyncio task: drains the thread queue onto websocket subscribers."""
    global _loop
    _loop = asyncio.get_running_loop()
    while True:
        try:
            event = await asyncio.to_thread(_events.get, True, 1.0)
        except queue.Empty:
            continue
        except Exception:  # pragma: no cover
            await asyncio.sleep(0.2)
            continue
        _fanout(event)


def encode(event: dict) -> str:
    return json.dumps(event, default=str)
