"""SQLite source (batch + trigger-based CDC journal) and destination.

SQLite has no external replication protocol, so CDC is implemented with
AFTER INSERT/UPDATE/DELETE triggers that append change rows into a
`_holypipe_cdc_log` journal table. The CDC reader polls that journal on a
short interval, which is the closest a single-file embedded database can
get to real-time streaming.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import settings
from .base import (
    BINARY,
    BOOLEAN,
    DATE,
    INTEGER,
    JSONB,
    NUMBER,
    STRING,
    TIME,
    TIMESTAMP,
    BaseDestination,
    BaseSource,
    ChangeEvent,
    Column,
    ConnectorError,
    StreamSchema,
    bounded_name,
    normalize_record,
    normalize_value,
)

DDL_TYPES = {
    STRING: "TEXT", INTEGER: "INTEGER", NUMBER: "REAL", BOOLEAN: "INTEGER",
    TIMESTAMP: "TEXT", DATE: "TEXT", TIME: "TEXT", JSONB: "TEXT", BINARY: "BLOB",
}

CDC_LOG_TABLE = "_holypipe_cdc_log"
SYSTEM_TABLES = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4", CDC_LOG_TABLE}


def _canonical(native: str) -> str:
    native = (native or "").upper()
    if "INT" in native:
        return INTEGER
    if any(k in native for k in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return NUMBER
    if "BOOL" in native:
        return BOOLEAN
    if "BLOB" in native:
        return BINARY
    if "DATETIME" in native or "TIMESTAMP" in native:
        return TIMESTAMP
    if native == "DATE":
        return DATE
    if native == "JSON":
        return JSONB
    return STRING


def _path(config: dict) -> str:
    p = config.get("path") or config.get("database") or ":memory:"
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p


class SqliteMixin:
    _conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(_path(self.config), timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            return conn
        except sqlite3.Error as exc:
            raise ConnectorError(f"SQLite connection failed: {exc}") from exc

    def _get_conn(self) -> sqlite3.Connection:
        """A connection reused across calls on this connector instance —
        avoids re-running the WAL/busy_timeout pragmas on every CDC poll
        (as often as once a second) or every batch write."""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except sqlite3.Error:
                pass
        self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def test(self) -> str:
        conn = self._get_conn()
        row = conn.execute("SELECT sqlite_version()").fetchone()
        return f"SQLite {row[0]}"


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in SYSTEM_TABLES and not r[0].startswith("_holypipe_")]


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[list[Column], list[str]]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    cols, pks = [], []
    pk_rows = [r for r in rows if r[5] > 0]
    pk_rows.sort(key=lambda r: r[5])
    for cid, name, ctype, notnull, dflt, pk in rows:
        cols.append(Column(name, _canonical(ctype), notnull == 0, ctype or ""))
    pks = [r[1] for r in pk_rows]
    return cols, pks


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
class SqliteSource(SqliteMixin, BaseSource):
    type = "sqlite"
    supports_cdc = True

    def discover(self) -> list[StreamSchema]:
        conn = self._get_conn()
        streams = []
        for table in _user_tables(conn):
            cols, pks = _table_columns(conn, table)
            streams.append(StreamSchema(name=table, table=table, namespace=None,
                                        columns=cols, primary_key=pks))
        return streams

    def read(self, stream: StreamSchema, *, cursor_field: str | None = None,
             cursor_value: Any = None, batch_size: int = 1000,
             columns: list[str] | None = None) -> Iterator[list[dict]]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            select_list = "*" if not columns else ", ".join(f'"{c}"' for c in columns)
            query = f'SELECT {select_list} FROM "{stream.table}"'
            params: list[Any] = []
            if cursor_field:
                if cursor_value is not None:
                    query += f' WHERE "{cursor_field}" > ?'
                    params.append(cursor_value)
                query += f' ORDER BY "{cursor_field}" ASC'
            cur = conn.execute(query, params)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield [normalize_record(dict(r)) for r in rows]
        finally:
            conn.close()

    def max_cursor(self, stream: StreamSchema, cursor_field: str) -> Any:
        conn = self._get_conn()
        row = conn.execute(f'SELECT MAX("{cursor_field}") FROM "{stream.table}"').fetchone()
        return normalize_value(row[0]) if row else None

    # -- CDC (trigger-based journal) -----------------------------------------
    def setup_cdc(self, streams: list[StreamSchema], state: dict) -> dict:
        conn = self._get_conn()
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {CDC_LOG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                op TEXT NOT NULL,
                row_json TEXT NOT NULL,
                ts TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for stream in streams:
            cols = stream.column_names()
            json_obj = ", ".join(f"'{c}', NEW.\"{c}\"" for c in cols)
            json_obj_old = ", ".join(f"'{c}', OLD.\"{c}\"" for c in cols)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS _hp_trg_{stream.table}_ins
                AFTER INSERT ON "{stream.table}" BEGIN
                    INSERT INTO {CDC_LOG_TABLE} (table_name, op, row_json)
                    VALUES ('{stream.table}', 'insert', json_object({json_obj}));
                END;
            """)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS _hp_trg_{stream.table}_upd
                AFTER UPDATE ON "{stream.table}" BEGIN
                    INSERT INTO {CDC_LOG_TABLE} (table_name, op, row_json)
                    VALUES ('{stream.table}', 'update', json_object({json_obj}));
                END;
            """)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS _hp_trg_{stream.table}_del
                AFTER DELETE ON "{stream.table}" BEGIN
                    INSERT INTO {CDC_LOG_TABLE} (table_name, op, row_json)
                    VALUES ('{stream.table}', 'delete', json_object({json_obj_old}));
                END;
            """)
        state.setdefault("last_id", 0)
        return state

    def teardown_cdc(self, state: dict) -> None:
        pass

    def stream_changes(self, streams: list[StreamSchema], state: dict, stop_event,
                       on_batch: Callable[[list[ChangeEvent], dict], None]) -> None:
        pk_by_table = {s.table: s.primary_key for s in streams}
        wanted = {s.table for s in streams}
        last_id = int(state.get("last_id", 0))

        while not stop_event.is_set():
            conn = self._get_conn()
            rows = conn.execute(
                f"SELECT id, table_name, op, row_json FROM {CDC_LOG_TABLE} "
                "WHERE id > ? ORDER BY id ASC LIMIT ?",
                (last_id, settings.cdc_flush_records)).fetchall()

            if not rows:
                time.sleep(settings.cdc_flush_seconds)
                continue

            batch: list[ChangeEvent] = []
            for row_id, table_name, op, row_json in rows:
                last_id = row_id
                if table_name not in wanted:
                    continue
                data = normalize_record(json.loads(row_json))
                pk = pk_by_table.get(table_name, [])
                key = {p: data.get(p) for p in pk} if pk else data
                batch.append(ChangeEvent(stream=table_name, op=op, data=data,
                                         key=key, position=row_id))

            if batch:
                on_batch(batch, {"last_id": last_id})
            else:
                state["last_id"] = last_id

            # Trim consumed journal entries so the log file doesn't grow forever.
            conn.execute(f"DELETE FROM {CDC_LOG_TABLE} WHERE id <= ?", (last_id,))


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------
class SqliteDestination(SqliteMixin, BaseDestination):
    type = "sqlite"

    def prepare(self, table: str, stream: StreamSchema, namespace: str | None) -> None:
        conn = self._get_conn()
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        meta = [(settings.meta_extracted_at, "TEXT"),
                (settings.meta_op, "TEXT"),
                (settings.meta_deleted, "INTEGER DEFAULT 0")]
        if not existing:
            defs = [f'"{c.name}" {DDL_TYPES.get(c.type, "TEXT")}' for c in stream.columns]
            defs += [f'"{n}" {d}' for n, d in meta]
            if stream.primary_key:
                keys = ", ".join(f'"{p}"' for p in stream.primary_key)
                defs.append(f"PRIMARY KEY ({keys})")
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(defs)})')
        else:
            wanted = [(c.name, DDL_TYPES.get(c.type, "TEXT")) for c in stream.columns] + meta
            for name, ddl in wanted:
                if name not in existing:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')

    def write(self, table: str, namespace: str | None, columns: list[str],
              rows: list[dict], *, primary_key: list[str], mode: str) -> int:
        if not rows:
            return 0
        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["?"] * len(columns))
        if mode == "upsert" and primary_key:
            updatable = [c for c in columns if c not in primary_key]
            stmt = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
            if updatable:
                conflict_keys = ", ".join(f'"{p}"' for p in primary_key)
                setters = ", ".join(f'"{c}" = excluded."{c}"' for c in updatable)
                stmt += f"ON CONFLICT({conflict_keys}) DO UPDATE SET {setters}"
            else:
                stmt += "ON CONFLICT DO NOTHING"
        else:
            stmt = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
        values = [tuple(r.get(c) for c in columns) for r in rows]
        conn = self._get_conn()
        conn.execute("BEGIN")
        try:
            conn.executemany(stmt, values)
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        return len(rows)

    def delete(self, table: str, namespace: str | None, primary_key: list[str],
               keys: list[dict], *, soft: bool = False) -> int:
        if not keys or not primary_key:
            return 0
        where = " AND ".join(f'"{p}" = ?' for p in primary_key)
        if soft:
            stmt = (f'UPDATE "{table}" SET "{settings.meta_deleted}" = 1, '
                   f'"{settings.meta_op}" = \'delete\', "{settings.meta_extracted_at}" = ? '
                   f'WHERE {where}')
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            values = [tuple([now] + [k.get(p) for p in primary_key]) for k in keys]
        else:
            stmt = f'DELETE FROM "{table}" WHERE {where}'
            values = [tuple(k.get(p) for p in primary_key) for k in keys]
        conn = self._get_conn()
        conn.execute("BEGIN")
        try:
            conn.executemany(stmt, values)
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        return len(keys)

    def truncate(self, table: str, namespace: str | None) -> None:
        conn = self._get_conn()
        conn.execute(f'DELETE FROM "{table}"')

    def swap_in(self, table: str, namespace: str | None, shadow_table: str) -> None:
        conn = self._get_conn()
        existing = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        conn.execute("BEGIN")
        try:
            if existing:
                old = bounded_name(table, "__hpold")
                conn.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
                conn.execute(f'ALTER TABLE "{shadow_table}" RENAME TO "{table}"')
                conn.execute(f'DROP TABLE "{old}"')
            else:
                conn.execute(f'ALTER TABLE "{shadow_table}" RENAME TO "{table}"')
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
