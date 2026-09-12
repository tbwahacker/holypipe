"""Connector contracts and the canonical type system that sits between engines."""
from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# --- canonical types ---------------------------------------------------------
STRING = "string"
INTEGER = "integer"
NUMBER = "number"
BOOLEAN = "boolean"
TIMESTAMP = "timestamp"
DATE = "date"
TIME = "time"
JSONB = "json"
BINARY = "binary"

CANONICAL_TYPES = (STRING, INTEGER, NUMBER, BOOLEAN, TIMESTAMP, DATE, TIME, JSONB, BINARY)

#: Canonical types that can safely act as an incremental cursor.
CURSOR_TYPES = (INTEGER, NUMBER, TIMESTAMP, DATE, TIME, STRING)


def bounded_name(base: str, suffix: str, limit: int = 63) -> str:
    """`base` truncated so `base + suffix` fits within `limit` chars — keeps
    generated identifiers under every supported engine's limit (Postgres 63,
    MySQL 64; SQLite has none, but staying uniform avoids two code paths)."""
    return base[: max(limit - len(suffix), 1)] + suffix


def shadow_table_name(table: str) -> str:
    """Name of the shadow table a full_refresh sync loads into before being
    atomically swapped in via `BaseDestination.swap_in` — see its docstring."""
    return bounded_name(table, "__hpswap")


@dataclass
class Column:
    name: str
    type: str = STRING
    nullable: bool = True
    native_type: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "nullable": self.nullable,
                "native_type": self.native_type}

    @staticmethod
    def from_dict(d: dict) -> "Column":
        return Column(d["name"], d.get("type", STRING), d.get("nullable", True), d.get("native_type", ""))


@dataclass
class StreamSchema:
    """One replicable table."""
    name: str                    # fully-qualified logical name, e.g. "public.users"
    table: str
    namespace: str | None = None
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "table": self.table,
            "namespace": self.namespace,
            "columns": [c.as_dict() for c in self.columns],
            "primary_key": list(self.primary_key),
        }

    @staticmethod
    def from_dict(d: dict) -> "StreamSchema":
        return StreamSchema(
            name=d["name"],
            table=d["table"],
            namespace=d.get("namespace"),
            columns=[Column.from_dict(c) for c in d.get("columns", [])],
            primary_key=list(d.get("primary_key", [])),
        )


@dataclass
class ChangeEvent:
    """A single row-level change emitted by a CDC stream."""
    stream: str
    op: str                       # insert | update | delete
    data: dict
    key: dict = field(default_factory=dict)
    position: Any = None


class ConnectorError(RuntimeError):
    pass


# --- value normalisation -----------------------------------------------------
def normalize_value(value: Any) -> Any:
    """Coerce driver-native values into JSON/DBAPI-friendly Python primitives."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(list(value) if isinstance(value, (tuple, set)) else value, default=str)
    return str(value)


def normalize_record(record: dict) -> dict:
    return {k: normalize_value(v) for k, v in record.items()}


# --- interfaces --------------------------------------------------------------
class BaseConnector:
    type: str = ""

    def __init__(self, config: dict):
        self.config = config or {}

    def test(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class BaseSource(BaseConnector):
    supports_cdc: bool = False

    def discover(self) -> list[StreamSchema]:
        raise NotImplementedError

    def read(self, stream: StreamSchema, *, cursor_field: str | None = None,
             cursor_value: Any = None, batch_size: int = 1000,
             columns: list[str] | None = None) -> Iterator[list[dict]]:
        """Yield batches of rows. Incremental when `cursor_field` is provided.
        Fetches only `columns` when given (must already include any primary
        key / cursor column the caller needs), otherwise every column."""
        raise NotImplementedError

    def max_cursor(self, stream: StreamSchema, cursor_field: str) -> Any:
        raise NotImplementedError

    # -- CDC ------------------------------------------------------------------
    def setup_cdc(self, streams: list[StreamSchema], state: dict) -> dict:
        raise ConnectorError(f"{self.type} source does not support CDC")

    def stream_changes(self, streams: list[StreamSchema], state: dict,
                       stop_event, on_batch: Callable[[list[ChangeEvent], dict], None]) -> None:
        raise ConnectorError(f"{self.type} source does not support CDC")

    def teardown_cdc(self, state: dict) -> None:
        pass


class BaseDestination(BaseConnector):
    def prepare(self, table: str, stream: StreamSchema, namespace: str | None) -> None:
        """Create the table if absent, add any newly discovered columns."""
        raise NotImplementedError

    def write(self, table: str, namespace: str | None, columns: list[str],
              rows: list[dict], *, primary_key: list[str], mode: str) -> int:
        """mode is 'append' or 'upsert'. Returns rows written."""
        raise NotImplementedError

    def delete(self, table: str, namespace: str | None, primary_key: list[str],
               keys: list[dict], *, soft: bool = False) -> int:
        raise NotImplementedError

    def truncate(self, table: str, namespace: str | None) -> None:
        raise NotImplementedError

    def swap_in(self, table: str, namespace: str | None, shadow_table: str) -> None:
        """Atomically replace `table` with the already fully-loaded
        `shadow_table` (built via `prepare()`/`write()` under the name from
        `shadow_table_name()`) via a rename swap, so a full_refresh sync
        never leaves readers looking at a truncated-but-not-yet-reloaded
        table. `shadow_table` no longer exists once this returns.

        Trade-off: any index/grant added directly on the destination table
        outside of HolyPipe is lost on swap, since the object itself is
        replaced rather than its rows updated in place."""
        raise NotImplementedError
