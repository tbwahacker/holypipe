"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    metadata_url: str = os.getenv("HOLYPIPE_METADATA_URL", "sqlite:///./data/holypipe.db")
    host: str = os.getenv("HOLYPIPE_HOST", "0.0.0.0")
    port: int = int(os.getenv("HOLYPIPE_PORT", "8000"))
    scheduler_enabled: bool = _flag("HOLYPIPE_SCHEDULER")
    # Rows fetched per batch from a source and per batch written to a
    # destination. Higher = fewer round-trips but more memory per batch;
    # sources are still streamed (server-side cursors / SSCursor), so total
    # table size is never held in memory at once regardless of this value.
    batch_size: int = int(os.getenv("HOLYPIPE_BATCH_SIZE", "1000"))
    cdc_flush_seconds: float = float(os.getenv("HOLYPIPE_CDC_FLUSH_SECONDS", "1.0"))
    cdc_flush_records: int = int(os.getenv("HOLYPIPE_CDC_FLUSH_RECORDS", "500"))
    # Concurrent batch syncs the scheduler will run at once. CDC connections
    # each get their own dedicated thread regardless of this limit. Raise
    # this if you have many batch connections and want them to run in
    # parallel rather than queueing.
    scheduler_workers: int = int(os.getenv("HOLYPIPE_SCHEDULER_WORKERS", "4"))
    discovery_cache_ttl: float = float(os.getenv("HOLYPIPE_DISCOVERY_CACHE_TTL", "300"))
    log_level: str = os.getenv("HOLYPIPE_LOG_LEVEL", "INFO").upper()
    log_retention: int = int(os.getenv("HOLYPIPE_LOG_RETENTION", "2000"))
    # Columns HolyPipe adds to every destination table.
    meta_extracted_at = "_hp_extracted_at"
    meta_op = "_hp_op"
    meta_deleted = "_hp_deleted"


settings = Settings()
