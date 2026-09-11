"""A tiny thread-safe in-memory TTL cache.

Used to avoid re-scanning `information_schema` (or SQLite's `sqlite_master`)
on every click when a source has thousands of tables — discovery results are
cached per source for a short window and invalidated whenever that source's
config changes.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .config import settings


class TTLCache:
    def __init__(self, default_ttl: float = 300.0):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + (ttl if ttl is not None else self.default_ttl), value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        with self._lock:
            for k in [k for k in self._store if k.startswith(prefix)]:
                del self._store[k]

    def get_or_set(self, key: str, factory: Callable[[], Any], ttl: float | None = None) -> Any:
        value = self.get(key)
        if value is not None:
            return value
        value = factory()
        self.set(key, value, ttl)
        return value


discovery_cache = TTLCache(default_ttl=settings.discovery_cache_ttl)
