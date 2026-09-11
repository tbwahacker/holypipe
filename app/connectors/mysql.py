"""MySQL source (batch + binlog CDC) and destination."""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable, Iterator

import pymysql
import pymysql.cursors

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
    normalize_record,
    normalize_value,
)

MYSQL_TYPE_MAP = {
    "tinyint": INTEGER, "smallint": INTEGER, "mediumint": INTEGER, "int": INTEGER,
    "integer": INTEGER, "bigint": INTEGER, "year": INTEGER,
    "decimal": NUMBER, "numeric": NUMBER, "float": NUMBER, "double": NUMBER, "real": NUMBER,
    "bit": BOOLEAN,
    "date": DATE,
    "time": TIME,
    "datetime": TIMESTAMP, "timestamp": TIMESTAMP,
    "json": JSONB,
    "blob": BINARY, "tinyblob": BINARY, "mediumblob": BINARY, "longblob": BINARY,
    "binary": BINARY, "varbinary": BINARY,
}

DDL_TYPES = {
    STRING: "TEXT", INTEGER: "BIGINT", NUMBER: "DOUBLE", BOOLEAN: "TINYINT(1)",
    TIMESTAMP: "DATETIME", DATE: "DATE", TIME: "TIME", JSONB: "JSON", BINARY: "LONGBLOB",
}

SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")


def _canonical(native: str) -> str:
    base = (native or "").split("(")[0].strip().lower()
    if base.startswith("tinyint(1)") or base == "bool" or base == "boolean":
        return BOOLEAN
    return MYSQL_TYPE_MAP.get(base, STRING)


def _conn_kwargs(config: dict) -> dict:
    kwargs = {
        "host": config.get("host", "localhost"),
        "port": int(config.get("port", 3306)),
        "db": config.get("database"),
        "user": config.get("username", "root"),
        "password": config.get("password", ""),
        "connect_timeout": int(config.get("connect_timeout", 10)),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.SSCursor,
        "autocommit": True,
    }
    ssl_mode = (config.get("ssl_mode") or "DISABLED").upper()
    if ssl_mode != "DISABLED":
        ssl_opts: dict[str, Any] = {}
        if config.get("ssl_ca"):
            ssl_opts["ca"] = config["ssl_ca"]
        if config.get("ssl_cert"):
            ssl_opts["cert"] = config["ssl_cert"]
        if config.get("ssl_key"):
            ssl_opts["key"] = config["ssl_key"]
        ssl_opts["check_hostname"] = ssl_mode == "VERIFY_IDENTITY"
        kwargs["ssl"] = ssl_opts
    return kwargs


class MysqlMixin:
    _conn = None

    def _connect(self, dict_cursor: bool = False):
        kwargs = _conn_kwargs(self.config)
        if dict_cursor:
            kwargs["cursorclass"] = pymysql.cursors.SSDictCursor
        try:
            return pymysql.connect(**kwargs)
        except pymysql.MySQLError as exc:
            raise ConnectorError(f"MySQL connection failed: {exc}") from exc

    def _get_conn(self):
        """A non-streaming connection reused across calls on this connector
        instance, so CDC's frequent write()/delete() flushes don't pay a
        fresh TCP+auth handshake every time."""
        if self._conn is not None and getattr(self._conn, "open", False):
            return self._conn
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
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
class MysqlSource(MysqlMixin, BaseSource):
    type = "mysql"
    supports_cdc = True

    def discover(self) -> list[StreamSchema]:
        database = self.config.get("database")
        conn = self._get_conn()
        with conn.cursor() as cur:
                if database:
                    cur.execute(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY 1, 2",
                        (database,))
                else:
                    placeholders = ",".join(["%s"] * len(SYSTEM_SCHEMAS))
                    cur.execute(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        f"WHERE table_schema NOT IN ({placeholders}) AND table_type = 'BASE TABLE' "
                        "ORDER BY 1, 2", SYSTEM_SCHEMAS)
                tables = cur.fetchall()

                schemas = sorted(set(t[0] for t in tables)) if not database else [database]
                if not schemas:
                    return []
                placeholders = ",".join(["%s"] * len(schemas))
                cur.execute(
                    "SELECT table_schema, table_name, column_name, data_type, is_nullable, column_type "
                    f"FROM information_schema.columns WHERE table_schema IN ({placeholders}) "
                    "ORDER BY table_schema, table_name, ordinal_position", schemas)
                cols: dict[tuple, list[Column]] = {}
                for schema, table, name, data_type, nullable, column_type in cur.fetchall():
                    native = column_type if data_type == "tinyint" and "(1)" in column_type else data_type
                    cols.setdefault((schema, table), []).append(
                        Column(name, _canonical(native), nullable == "YES", native))

                cur.execute(
                    "SELECT table_schema, table_name, column_name FROM information_schema.statistics "
                    f"WHERE table_schema IN ({placeholders}) AND index_name = 'PRIMARY' "
                    "ORDER BY seq_in_index", schemas)
                pks: dict[tuple, list[str]] = {}
                for schema, table, col in cur.fetchall():
                    pks.setdefault((schema, table), []).append(col)

        return [
            StreamSchema(
                name=f"{schema}.{table}",
                table=table,
                namespace=schema,
                columns=cols.get((schema, table), []),
                primary_key=pks.get((schema, table), []),
            )
            for schema, table in tables
        ]

    def _quoted(self, stream: StreamSchema) -> str:
        return f"`{stream.namespace}`.`{stream.table}`"

    def read(self, stream: StreamSchema, *, cursor_field: str | None = None,
             cursor_value: Any = None, batch_size: int = 1000,
             columns: list[str] | None = None) -> Iterator[list[dict]]:
        conn = self._connect(dict_cursor=True)
        select_list = "*" if not columns else ", ".join(f"`{c}`" for c in columns)
        try:
            with conn.cursor() as cur:
                query = f"SELECT {select_list} FROM {self._quoted(stream)}"
                params: list[Any] = []
                if cursor_field:
                    if cursor_value is not None:
                        query += f" WHERE `{cursor_field}` > %s"
                        params.append(cursor_value)
                    query += f" ORDER BY `{cursor_field}` ASC"
                cur.execute(query, params)
                while True:
                    rows = cur.fetchmany(batch_size)
                    if not rows:
                        break
                    yield [normalize_record(r) for r in rows]
        finally:
            conn.close()

    def max_cursor(self, stream: StreamSchema, cursor_field: str) -> Any:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(`{cursor_field}`) FROM {self._quoted(stream)}")
            return normalize_value(cur.fetchone()[0])

    # -- CDC (binlog row-based replication) ----------------------------------
    def setup_cdc(self, streams: list[StreamSchema], state: dict) -> dict:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SHOW VARIABLES LIKE 'log_bin'")
            row = cur.fetchone()
            if not row or row[1].upper() not in ("ON", "1"):
                raise ConnectorError("MySQL binary logging (log_bin) is OFF; CDC requires it.")
            cur.execute("SHOW VARIABLES LIKE 'binlog_format'")
            row = cur.fetchone()
            if row and row[1].upper() != "ROW":
                raise ConnectorError(
                    f"MySQL binlog_format is '{row[1]}'; CDC requires 'ROW'.")
            if not state.get("log_file"):
                cur.execute("SHOW MASTER STATUS")
                pos = cur.fetchone()
                if pos:
                    state["log_file"], state["log_pos"] = pos[0], int(pos[1])
        state.setdefault("server_id", int(self.config.get("server_id", 1_047_200)))
        return state

    def teardown_cdc(self, state: dict) -> None:
        pass

    def stream_changes(self, streams: list[StreamSchema], state: dict, stop_event,
                       on_batch: Callable[[list[ChangeEvent], dict], None]) -> None:
        from pymysqlreplication import BinLogStreamReader
        from pymysqlreplication.row_event import (
            DeleteRowsEvent,
            UpdateRowsEvent,
            WriteRowsEvent,
        )

        wanted = {s.name for s in streams}
        pk_by_table = {s.name: s.primary_key for s in streams}
        table_map = {}
        for s in streams:
            table_map.setdefault(s.namespace, set()).add(s.table)

        conn_settings = _conn_kwargs(self.config)
        conn_settings.pop("cursorclass", None)
        conn_settings.pop("autocommit", None)
        conn_settings.pop("db", None)

        reader = BinLogStreamReader(
            connection_settings=conn_settings,
            server_id=state.get("server_id", 1_047_200),
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent],
            only_schemas=list(table_map.keys()) or None,
            only_tables=sorted({t for tbls in table_map.values() for t in tbls}) or None,
            resume_stream=bool(state.get("log_file")),
            log_file=state.get("log_file"),
            log_pos=state.get("log_pos"),
            blocking=False,
            freeze_schema=True,
        )

        buffer: list[ChangeEvent] = []
        last_flush = time.time()
        log_file, log_pos = state.get("log_file"), state.get("log_pos")

        def flush() -> None:
            nonlocal buffer, last_flush
            if buffer:
                on_batch(buffer, {"log_file": log_file, "log_pos": log_pos})
                buffer = []
            last_flush = time.time()

        try:
            while not stop_event.is_set():
                event = reader.fetchone()
                if event is None:
                    if buffer and time.time() - last_flush >= settings.cdc_flush_seconds:
                        flush()
                    time.sleep(0.2)
                    continue

                stream_name = f"{event.schema}.{event.table}"
                log_file, log_pos = reader.log_file, reader.log_pos
                if stream_name in wanted:
                    pk = pk_by_table.get(stream_name, [])
                    if isinstance(event, WriteRowsEvent):
                        for row in event.rows:
                            data = normalize_record(row["values"])
                            key = {p: data.get(p) for p in pk} if pk else data
                            buffer.append(ChangeEvent(stream_name, "insert", data, key,
                                                     position=f"{log_file}:{log_pos}"))
                    elif isinstance(event, UpdateRowsEvent):
                        for row in event.rows:
                            data = normalize_record(row["after_values"])
                            before = normalize_record(row["before_values"])
                            key = {p: before.get(p) for p in pk} if pk else before
                            buffer.append(ChangeEvent(stream_name, "update", data, key,
                                                     position=f"{log_file}:{log_pos}"))
                    elif isinstance(event, DeleteRowsEvent):
                        for row in event.rows:
                            data = normalize_record(row["values"])
                            key = {p: data.get(p) for p in pk} if pk else data
                            buffer.append(ChangeEvent(stream_name, "delete", data, key,
                                                     position=f"{log_file}:{log_pos}"))

                if (len(buffer) >= settings.cdc_flush_records
                        or time.time() - last_flush >= settings.cdc_flush_seconds):
                    flush()
            flush()
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------
class MysqlDestination(MysqlMixin, BaseDestination):
    type = "mysql"

    def _db(self, namespace: str | None) -> str:
        return namespace or self.config.get("database") or "public"

    def _quoted(self, table: str, namespace: str | None) -> str:
        return f"`{self._db(namespace)}`.`{table}`"

    def prepare(self, table: str, stream: StreamSchema, namespace: str | None) -> None:
        db = self._db(namespace)
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4")
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s", (db, table))
            existing = {r[0] for r in cur.fetchall()}
            meta = [(settings.meta_extracted_at, "DATETIME"),
                    (settings.meta_op, "VARCHAR(20)"),
                    (settings.meta_deleted, "TINYINT(1) DEFAULT 0")]
            if not existing:
                defs = [f"`{c.name}` {DDL_TYPES.get(c.type, 'TEXT')}" for c in stream.columns]
                defs += [f"`{n}` {d}" for n, d in meta]
                if stream.primary_key:
                    pk_defs = []
                    for p in stream.primary_key:
                        col = stream.column(p)
                        ddl = DDL_TYPES.get(col.type if col else STRING, "TEXT")
                        if ddl == "TEXT":
                            ddl = "VARCHAR(255)"
                        pk_defs.append((p, ddl))
                    for name, ddl in pk_defs:
                        defs = [d.replace(f"`{name}` TEXT", f"`{name}` {ddl}") for d in defs]
                    keys = ", ".join(f"`{p}`" for p in stream.primary_key)
                    defs.append(f"PRIMARY KEY ({keys})")
                cur.execute(f"CREATE TABLE IF NOT EXISTS {self._quoted(table, db)} "
                           f"({', '.join(defs)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
            else:
                wanted = [(c.name, DDL_TYPES.get(c.type, "TEXT")) for c in stream.columns] + meta
                for name, ddl in wanted:
                    if name not in existing:
                        cur.execute(f"ALTER TABLE {self._quoted(table, db)} "
                                   f"ADD COLUMN `{name}` {ddl}")

    def write(self, table: str, namespace: str | None, columns: list[str],
              rows: list[dict], *, primary_key: list[str], mode: str) -> int:
        if not rows:
            return 0
        rel = self._quoted(table, namespace)
        col_list = ", ".join(f"`{c}`" for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        stmt = f"INSERT INTO {rel} ({col_list}) VALUES ({placeholders})"
        if mode == "upsert" and primary_key:
            updatable = [c for c in columns if c not in primary_key]
            if updatable:
                setters = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in updatable)
                stmt += f" ON DUPLICATE KEY UPDATE {setters}"
            else:
                stmt += f" ON DUPLICATE KEY UPDATE `{primary_key[0]}` = `{primary_key[0]}`"
        values = [tuple(r.get(c) for c in columns) for r in rows]
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.executemany(stmt, values)
        conn.commit()
        return len(rows)

    def delete(self, table: str, namespace: str | None, primary_key: list[str],
               keys: list[dict], *, soft: bool = False) -> int:
        if not keys or not primary_key:
            return 0
        rel = self._quoted(table, namespace)
        where = " AND ".join(f"`{p}` = %s" for p in primary_key)
        if soft:
            stmt = (f"UPDATE {rel} SET `{settings.meta_deleted}` = 1, "
                   f"`{settings.meta_op}` = 'delete', `{settings.meta_extracted_at}` = %s "
                   f"WHERE {where}")
            now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            values = [tuple([now] + [k.get(p) for p in primary_key]) for k in keys]
        else:
            stmt = f"DELETE FROM {rel} WHERE {where}"
            values = [tuple(k.get(p) for p in primary_key) for k in keys]
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.executemany(stmt, values)
        conn.commit()
        return len(keys)

    def truncate(self, table: str, namespace: str | None) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {self._quoted(table, namespace)}")
        conn.commit()
