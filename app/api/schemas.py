"""Pydantic request/response models for the HolyPipe API."""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class ResyncRequest(BaseModel):
    stream_names: list[str] | None = None  # None = every selected stream


class ConnectorIn(BaseModel):
    name: str
    type: str
    config: dict = Field(default_factory=dict)
    uri: str | None = None  # optional connection-string alternative to `config`


class ConnectorOut(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class TestResult(BaseModel):
    ok: bool
    message: str


class DiscoveredColumn(BaseModel):
    name: str
    type: str
    nullable: bool
    native_type: str


class DiscoveredStream(BaseModel):
    name: str
    table: str
    namespace: str | None
    columns: list[DiscoveredColumn]
    primary_key: list[str]


class StreamConfig(BaseModel):
    schema_: dict = Field(alias="schema")
    selected: bool = True
    sync_mode: str = "full_refresh"  # full_refresh | incremental | xmin
    cursor_field: str | None = None
    primary_key: list[str] = Field(default_factory=list)
    destination_table: str | None = None
    columns: list[str] | None = None  # None = all columns; otherwise an explicit subset

    model_config = {"populate_by_name": True}


class ConnectionIn(BaseModel):
    name: str
    source_id: str
    destination_id: str
    mode: str = "batch"  # batch | cdc
    schedule_type: str = "interval"  # interval | cron (batch mode only)
    interval_seconds: int = 60
    cron_expression: str | None = None
    enabled: bool = True
    destination_namespace: str | None = None
    table_prefix: str = ""
    streams: list[dict] = Field(default_factory=list)


class ConnectionPatch(BaseModel):
    name: str | None = None
    mode: str | None = None
    schedule_type: str | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    enabled: bool | None = None
    destination_namespace: str | None = None
    table_prefix: str | None = None
    streams: list[dict] | None = None


class ConnectionOut(BaseModel):
    id: str
    name: str
    source_id: str
    destination_id: str
    mode: str
    schedule_type: str
    interval_seconds: int
    cron_expression: str | None
    enabled: bool
    destination_namespace: str | None
    table_prefix: str
    streams: list
    status: str
    status_detail: str | None
    last_run_at: dt.datetime | None
    next_run_at: dt.datetime | None
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class SyncRunOut(BaseModel):
    id: str
    connection_id: str
    trigger: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    records_read: int
    records_written: int
    records_deleted: int
    error: str | None
    stream_stats: dict

    model_config = {"from_attributes": True}


class LogOut(BaseModel):
    id: int
    connection_id: str | None
    run_id: str | None
    ts: dt.datetime
    level: str
    message: str

    model_config = {"from_attributes": True}
