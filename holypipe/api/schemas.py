"""Pydantic request/response models for the HolyPipe API."""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class ResyncRequest(BaseModel):
    stream_names: list[str] | None = None  # None = every selected stream


# --- auth --------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


class MeOut(BaseModel):
    id: str
    username: str
    must_change_password: bool
    permissions: list[str]


class RoleIn(BaseModel):
    name: str
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)


class RoleOut(BaseModel):
    id: str
    name: str
    description: str | None
    permissions: list[str]
    is_builtin: bool
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class UserIn(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role_ids: list[str] = Field(default_factory=list)
    is_active: bool = True


class UserPatch(BaseModel):
    role_ids: list[str] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


class UserOut(BaseModel):
    id: str
    username: str
    is_active: bool
    must_change_password: bool
    created_at: dt.datetime
    role_ids: list[str]
    role_names: list[str]


class ApiTokenIn(BaseModel):
    name: str
    expires_in_days: int | None = None  # None = never expires


class ApiTokenOut(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    expires_at: dt.datetime | None

    model_config = {"from_attributes": True}


class ApiTokenCreated(ApiTokenOut):
    token: str  # only ever present in the create response


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
