"""Metadata schema: sources, destinations, connections, runs and logs."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import utcnow


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    type: Mapped[str] = mapped_column(String(20))  # postgres | mysql | sqlite
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    connections: Mapped[list["Connection"]] = relationship(back_populates="source")


class Destination(Base):
    __tablename__ = "destinations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    connections: Mapped[list["Connection"]] = relationship(back_populates="destination")


class Connection(Base):
    """A source -> destination replication pipeline."""

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    destination_id: Mapped[str] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"))

    # "cdc" streams the write-ahead log / binlog / trigger journal continuously.
    # "batch" polls on a schedule (interval or cron).
    mode: Mapped[str] = mapped_column(String(10), default="batch")
    # "interval" uses interval_seconds; "cron" uses cron_expression (5-field crontab syntax).
    schedule_type: Mapped[str] = mapped_column(String(10), default="interval")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    destination_namespace: Mapped[str | None] = mapped_column(String(200), nullable=True)
    table_prefix: Mapped[str] = mapped_column(String(100), default="")

    # [{name, namespace, table, selected, sync_mode, cursor_field, primary_key, destination_table, columns}]
    streams: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|streaming|error
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[Source] = relationship(back_populates="connections")
    destination: Mapped[Destination] = relationship(back_populates="connections")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual|schedule|cdc
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|failed|cancelled
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    records_read: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    records_deleted: Mapped[int] = mapped_column(Integer, default=0)
    bytes_moved: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_stats: Mapped[dict] = mapped_column(JSON, default=dict)


class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Auth: users, dynamic roles, sessions
# ---------------------------------------------------------------------------
class UserRole(Base):
    """User <-> Role membership. A user can hold several roles; its
    effective permissions are the union of every held role's permissions."""

    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class Role(Base):
    """A named, admin-creatable bundle of permissions — this is the "dynamic"
    part: the set of possible permission codes is fixed (see auth.PERMISSIONS,
    what the code actually knows how to check), but which codes make up a
    given role, and which roles exist at all, is entirely user-defined
    through the API/UI. `["*"]` means every permission, including ones added
    in the future — used by the built-in Administrator role."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    permissions: Mapped[list] = mapped_column(JSON, default=list)  # list[str] of permission codes
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)  # protects Administrator from edit/delete
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list["User"]] = relationship(secondary="user_roles", back_populates="roles")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Forced on the seeded admin/admin account; cleared on the next successful
    # password change. Checked by the frontend to block dashboard use until done.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    roles: Mapped[list[Role]] = relationship(secondary="user_roles", back_populates="users")


class UserSession(Base):
    """An opaque bearer token handed out on login, stored server-side so it
    can be revoked (logout, deactivating a user) — deliberately not a
    self-contained JWT, which can't be revoked before it expires on its own."""

    __tablename__ = "user_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
