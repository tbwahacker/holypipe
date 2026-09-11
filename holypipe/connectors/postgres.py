"""PostgreSQL source (batch + logical-replication CDC) and destination."""
from __future__ import annotations

import datetime as dt
import json
import re
import select
import time
from typing import Any, Callable, Iterator

import psycopg2
import psycopg2.extras
from psycopg2 import sql

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

PG_TYPE_MAP = {
    "smallint": INTEGER, "integer": INTEGER, "bigint": INTEGER, "int2": INTEGER,
    "int4": INTEGER, "int8": INTEGER, "smallserial": INTEGER, "serial": INTEGER,
    "bigserial": INTEGER,
    "numeric": NUMBER, "decimal": NUMBER, "real": NUMBER, "double precision": NUMBER,
    "float4": NUMBER, "float8": NUMBER, "money": NUMBER,
    "boolean": BOOLEAN, "bool": BOOLEAN,
    "timestamp without time zone": TIMESTAMP, "timestamp with time zone": TIMESTAMP,
    "timestamp": TIMESTAMP, "timestamptz": TIMESTAMP,
    "date": DATE,
    "time without time zone": TIME, "time with time zone": TIME, "time": TIME, "timetz": TIME,
    "json": JSONB, "jsonb": JSONB,
    "bytea": BINARY,
}

DDL_TYPES = {
    STRING: "TEXT", INTEGER: "BIGINT", NUMBER: "DOUBLE PRECISION", BOOLEAN: "BOOLEAN",
    TIMESTAMP: "TIMESTAMP", DATE: "DATE", TIME: "TIME", JSONB: "JSONB", BINARY: "BYTEA",
}

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def _canonical(native: str) -> str:
    native = (native or "").lower()
    if native in PG_TYPE_MAP:
        return PG_TYPE_MAP[native]
    if native.startswith(("character", "varchar", "text", "char", "uuid", "name", "citext",
                          "inet", "cidr", "macaddr", "xml", "enum")):
        return STRING
    if native.startswith("timestamp"):
        return TIMESTAMP
    if native.startswith("time"):
        return TIME
    if native.startswith(("int", "serial")):
        return INTEGER
    if native.startswith(("numeric", "float", "double", "real")):
        return NUMBER
    if native.endswith("[]") or native == "array":
        return JSONB
    return STRING


def _dsn(config: dict) -> dict:
    dsn = {
        "host": config.get("host", "localhost"),
        "port": int(config.get("port", 5432)),
        "dbname": config.get("database", "postgres"),
        "user": config.get("username", "postgres"),
        "password": config.get("password", ""),
        "connect_timeout": int(config.get("connect_timeout", 10)),
        "application_name": "holypipe",
    }
    sslmode = config.get("sslmode") or ("require" if config.get("ssl") else "prefer")
    dsn["sslmode"] = sslmode
    return dsn


class PostgresMixin:
    _conn = None

    def _connect(self, **extra):
        try:
            return psycopg2.connect(**_dsn(self.config), **extra)
        except psycopg2.Error as exc:
            raise ConnectorError("PostgreSQL connection failed: %s" % exc) from exc

    def _get_conn(self):
        """A connection reused across calls on this connector instance.

        `with conn:` (used throughout this module) only wraps the
        transaction — it commits/rolls back but never closes the socket —
        so reusing one connection here avoids opening a fresh TCP+auth
        handshake on every read/write. Important under CDC, which calls
        write()/delete() on the same destination instance every ~1s.
        """
        if self._conn is not None and self._conn.closed == 0:
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
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
class PostgresSource(PostgresMixin, BaseSource):
    type = "postgres"
    supports_cdc = True

    def discover(self) -> list[StreamSchema]:
        schemas = self.config.get("schemas") or []
        with self._get_conn() as conn, conn.cursor() as cur:
            params: list[Any] = [list(SYSTEM_SCHEMAS)]
            where = "t.table_schema <> ALL(%s) AND t.table_type = 'BASE TABLE'"
            if schemas:
                where += " AND t.table_schema = ANY(%s)"
                params.append(schemas)
            cur.execute(
                "SELECT t.table_schema, t.table_name FROM information_schema.tables t "
                "WHERE " + where + " ORDER BY 1, 2", params)
            tables = cur.fetchall()

            cur.execute(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable, udt_name "
                "FROM information_schema.columns WHERE table_schema <> ALL(%s) "
                "ORDER BY table_schema, table_name, ordinal_position", [list(SYSTEM_SCHEMAS)])
            cols: dict[tuple, list[Column]] = {}
            for schema, table, name, data_type, nullable, udt in cur.fetchall():
                native = udt if data_type == "USER-DEFINED" else data_type
                cols.setdefault((schema, table), []).append(
                    Column(name, _canonical(native), nullable == "YES", native))

            cur.execute(
                "SELECT n.nspname, c.relname, a.attname "
                "FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indisprimary AND n.nspname <> ALL(%s)", [list(SYSTEM_SCHEMAS)])
            pks: dict[tuple, list[str]] = {}
            for schema, table, col in cur.fetchall():
                pks.setdefault((schema, table), []).append(col)

        return [
            StreamSchema(
                name="%s.%s" % (schema, table),
                table=table,
                namespace=schema,
                columns=cols.get((schema, table), []),
                primary_key=pks.get((schema, table), []),
            )
            for schema, table in tables
        ]

    def _relation(self, stream: StreamSchema):
        return sql.Identifier(stream.namespace or "public", stream.table)

    def read(self, stream: StreamSchema, *, cursor_field: str | None = None,
             cursor_value: Any = None, batch_size: int = 1000,
             columns: list[str] | None = None) -> Iterator[list[dict]]:
        conn = self._connect()
        conn.set_session(readonly=True)
        cursor_name = "hp_%d" % int(time.time() * 1000000)
        select_list = (sql.SQL("*") if not columns else
                       sql.SQL(", ").join(sql.Identifier(c) for c in columns))
        try:
            with conn.cursor(name=cursor_name, cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.itersize = batch_size
                params: list[Any] = []
                if cursor_field == "xmin":
                    # `xmin` (the system column holding the inserting/updating
                    # transaction id) has no native ordering operators, so cast
                    # it to a comparable integer. Good enough for change
                    # detection within a transaction-id epoch; it wraps at
                    # ~4 billion transactions, at which point a full refresh
                    # is needed.
                    query = sql.SQL("SELECT {}, xmin::text::bigint AS xmin FROM {}").format(
                        select_list, self._relation(stream))
                    if cursor_value is not None:
                        query = query + sql.SQL(" WHERE xmin::text::bigint > %s")
                        params.append(cursor_value)
                    query = query + sql.SQL(" ORDER BY xmin::text::bigint ASC")
                else:
                    query = sql.SQL("SELECT {} FROM {}").format(select_list, self._relation(stream))
                    if cursor_field:
                        if cursor_value is not None:
                            query = query + sql.SQL(" WHERE {} > %s").format(sql.Identifier(cursor_field))
                            params.append(cursor_value)
                        query = query + sql.SQL(" ORDER BY {} ASC").format(sql.Identifier(cursor_field))
                cur.execute(query, params)
                while True:
                    rows = cur.fetchmany(batch_size)
                    if not rows:
                        break
                    yield [normalize_record(dict(r)) for r in rows]
        finally:
            conn.close()

    def max_cursor(self, stream: StreamSchema, cursor_field: str) -> Any:
        with self._get_conn() as conn, conn.cursor() as cur:
            if cursor_field == "xmin":
                cur.execute(sql.SQL("SELECT MAX(xmin::text::bigint) FROM {}").format(
                    self._relation(stream)))
            else:
                cur.execute(sql.SQL("SELECT MAX({}) FROM {}").format(
                    sql.Identifier(cursor_field), self._relation(stream)))
            return normalize_value(cur.fetchone()[0])

    # -- CDC ----------------------------------------------------------------
    def _slot_name(self, state: dict) -> str:
        return state.get("slot") or self.config.get("replication_slot") or "holypipe_slot"

    def setup_cdc(self, streams: list[StreamSchema], state: dict) -> dict:
        slot = self._slot_name(state)
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SHOW wal_level")
            wal_level = cur.fetchone()[0]
            if wal_level != "logical":
                raise ConnectorError(
                    "PostgreSQL wal_level is '%s'; CDC needs 'logical'. "
                    "Restart the server with wal_level=logical." % wal_level)
            cur.execute("SELECT plugin FROM pg_replication_slots WHERE slot_name = %s", (slot,))
            row = cur.fetchone()
            if row:
                plugin = row[0]
            else:
                plugin = None
                for candidate in ("wal2json", "test_decoding"):
                    try:
                        cur.execute("SELECT pg_create_logical_replication_slot(%s, %s)",
                                    (slot, candidate))
                        conn.commit()
                        plugin = candidate
                        break
                    except psycopg2.Error:
                        conn.rollback()
                if plugin is None:
                    raise ConnectorError(
                        "Could not create a logical replication slot with either the "
                        "'wal2json' or the built-in 'test_decoding' output plugin.")
        state["slot"] = slot
        state["plugin"] = plugin
        return state

    def teardown_cdc(self, state: dict) -> None:
        slot = state.get("slot")
        if not slot:
            return
        try:
            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                    "(SELECT 1 FROM pg_replication_slots WHERE slot_name = %s AND active = false)",
                    (slot, slot))
                conn.commit()
        except Exception:
            pass

    def stream_changes(self, streams: list[StreamSchema], state: dict, stop_event,
                       on_batch: Callable[[list[ChangeEvent], dict], None]) -> None:
        slot = state["slot"]
        plugin = state.get("plugin", "test_decoding")
        wanted = set(s.name for s in streams)

        conn = psycopg2.connect(**_dsn(self.config),
                                connection_factory=psycopg2.extras.LogicalReplicationConnection)
        cur = conn.cursor()
        options: dict[str, str] = {}
        if plugin == "wal2json":
            options = {"format-version": "1", "include-lsn": "1"}
            tables = ",".join("%s.%s" % (s.namespace or "public", s.table) for s in streams)
            if tables:
                options["add-tables"] = tables
        try:
            cur.start_replication(slot_name=slot, decode=True, options=options)
        except psycopg2.Error as exc:
            conn.close()
            raise ConnectorError("Failed to start logical replication: %s" % exc) from exc

        buffer: list[ChangeEvent] = []
        last_flush = time.time()
        last_lsn = state.get("lsn")

        def flush() -> None:
            nonlocal buffer, last_flush
            if buffer:
                on_batch(buffer, {"lsn": last_lsn})
                buffer = []
            try:
                cur.send_feedback(flush_lsn=cur.wal_end, write_lsn=cur.wal_end, reply=True)
            except Exception:
                pass
            last_flush = time.time()

        try:
            while not stop_event.is_set():
                msg = cur.read_message()
                if msg is None:
                    select.select([cur], [], [], max(0.1, settings.cdc_flush_seconds))
                    if buffer or time.time() - last_flush >= 10:
                        flush()
                    continue

                payload = msg.payload
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", "replace")
                events = (_parse_wal2json(payload) if plugin == "wal2json"
                          else _parse_test_decoding(payload))
                last_lsn = _lsn_to_str(msg.data_start)
                for ev in events:
                    if ev.stream in wanted:
                        ev.position = last_lsn
                        buffer.append(ev)

                if (len(buffer) >= settings.cdc_flush_records
                        or time.time() - last_flush >= settings.cdc_flush_seconds):
                    flush()
            if buffer:
                flush()
        finally:
            try:
                cur.close()
            finally:
                conn.close()


def _lsn_to_str(lsn: int) -> str:
    return "%X/%X" % (lsn >> 32, lsn & 0xFFFFFFFF)


# --- wal2json ---------------------------------------------------------------
def _parse_wal2json(payload: str) -> list[ChangeEvent]:
    try:
        doc = json.loads(payload)
    except json.JSONDecodeError:
        return []
    events: list[ChangeEvent] = []
    for change in doc.get("change", []):
        schema = change.get("schema", "public")
        table = change.get("table")
        kind = change.get("kind")
        if not table or kind not in ("insert", "update", "delete"):
            continue
        names = change.get("columnnames") or []
        values = change.get("columnvalues") or []
        data = dict((n, normalize_value(v)) for n, v in zip(names, values))
        oldkeys = change.get("oldkeys") or {}
        key = dict((n, normalize_value(v)) for n, v in zip(oldkeys.get("keynames") or [],
                                                           oldkeys.get("keyvalues") or []))
        if kind == "delete" and not data:
            data = dict(key)
        events.append(ChangeEvent(stream="%s.%s" % (schema, table), op=kind,
                                  data=data, key=key or data))
    return events


# --- test_decoding ----------------------------------------------------------
_OP_RE = re.compile(r"^(INSERT|UPDATE|DELETE):\s*")


def _parse_test_decoding(payload: str) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line.startswith("table "):
            continue
        rest = line[6:]
        sep = rest.find(": ")
        if sep == -1:
            continue
        relation = rest[:sep].strip().replace('"', "")
        rest = rest[sep + 2:]
        m = _OP_RE.match(rest)
        if not m:
            continue
        op = m.group(1).lower()
        body = rest[m.end():]

        key: dict = {}
        if body.startswith("old-key:"):
            body = body[len("old-key:"):]
            marker = body.find("new-tuple:")
            if marker != -1:
                key = _scan_columns(body[:marker])
                body = body[marker + len("new-tuple:"):]
            else:
                key = _scan_columns(body)
                body = ""
        data = _scan_columns(body) if body.strip() else {}
        if op == "delete" and not data:
            data = dict(key)
        events.append(ChangeEvent(stream=relation, op=op, data=data, key=key or data))
    return events


def _scan_columns(s: str) -> dict:
    """Parse test_decoding's `name[type]:value name[type]:value ...` tuple format."""
    out: dict[str, Any] = {}
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] == " ":
            i += 1
        if i >= n:
            break
        # column name (optionally double-quoted)
        if s[i] == '"':
            j, buf = i + 1, []
            while j < n:
                if s[j] == '"':
                    if j + 1 < n and s[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(s[j])
                j += 1
            name, i = "".join(buf), j
        else:
            j = s.find("[", i)
            if j == -1:
                break
            name, i = s[i:j], j
        if i >= n or s[i] != "[":
            break
        # bracketed native type (may nest, e.g. numeric[10,2] or int4[])
        depth, j = 0, i
        while j < n:
            if s[j] == "[":
                depth += 1
            elif s[j] == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        native, i = s[i + 1:j], j + 1
        if i < n and s[i] == ":":
            i += 1
        # value
        if i < n and s[i] == "'":
            j, buf = i + 1, []
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(s[j])
                j += 1
            out[name], i = "".join(buf), j
        else:
            j = i
            while j < n and s[j] != " ":
                j += 1
            out[name], i = _coerce_unquoted(s[i:j], native), j
    return out


def _coerce_unquoted(raw: str, native: str) -> Any:
    if raw in ("null", "NULL", ""):
        return None
    if raw in ("t", "true"):
        return True
    if raw in ("f", "false"):
        return False
    canonical = _canonical(native)
    try:
        if canonical == INTEGER:
            return int(raw)
        if canonical == NUMBER:
            return float(raw)
    except ValueError:
        pass
    return raw


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------
class PostgresDestination(PostgresMixin, BaseDestination):
    type = "postgres"

    def _schema(self, namespace: str | None) -> str:
        return namespace or self.config.get("schema") or "public"

    def _rel(self, table: str, namespace: str | None):
        return sql.Identifier(self._schema(namespace), table)

    def prepare(self, table: str, stream: StreamSchema, namespace: str | None) -> None:
        schema = self._schema(namespace)
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s", (schema, table))
            existing = set(r[0] for r in cur.fetchall())
            meta = [(settings.meta_extracted_at, "TIMESTAMP"),
                    (settings.meta_op, "TEXT"),
                    (settings.meta_deleted, "BOOLEAN DEFAULT FALSE")]
            if not existing:
                defs = [sql.SQL("{} {}").format(sql.Identifier(c.name),
                                                sql.SQL(DDL_TYPES.get(c.type, "TEXT")))
                        for c in stream.columns]
                defs += [sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(d)) for n, d in meta]
                if stream.primary_key:
                    defs.append(sql.SQL("PRIMARY KEY ({})").format(
                        sql.SQL(", ").join(sql.Identifier(p) for p in stream.primary_key)))
                cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                    self._rel(table, schema), sql.SQL(", ").join(defs)))
            else:
                wanted = [(c.name, DDL_TYPES.get(c.type, "TEXT")) for c in stream.columns] + meta
                for name, ddl in wanted:
                    if name not in existing:
                        cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                            self._rel(table, schema), sql.Identifier(name), sql.SQL(ddl)))
            conn.commit()

    def write(self, table: str, namespace: str | None, columns: list[str],
              rows: list[dict], *, primary_key: list[str], mode: str) -> int:
        if not rows:
            return 0
        rel = self._rel(table, namespace)
        cols_sql = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        values = [tuple(r.get(c) for c in columns) for r in rows]
        with self._get_conn() as conn, conn.cursor() as cur:
            stmt = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(rel, cols_sql)
            if mode == "upsert" and primary_key:
                updatable = [c for c in columns if c not in primary_key]
                if updatable:
                    setters = sql.SQL(", ").join(
                        sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in updatable)
                    action = sql.SQL("DO UPDATE SET ") + setters
                else:
                    action = sql.SQL("DO NOTHING")
                stmt = stmt + sql.SQL(" ON CONFLICT ({}) ").format(
                    sql.SQL(", ").join(sql.Identifier(p) for p in primary_key)) + action
            psycopg2.extras.execute_values(cur, stmt.as_string(cur), values, page_size=500)
            conn.commit()
        return len(rows)

    def delete(self, table: str, namespace: str | None, primary_key: list[str],
               keys: list[dict], *, soft: bool = False) -> int:
        if not keys or not primary_key:
            return 0
        rel = self._rel(table, namespace)
        where = sql.SQL(" AND ").join(
            sql.SQL("{} = %s").format(sql.Identifier(p)) for p in primary_key)
        if soft:
            stmt = sql.SQL("UPDATE {} SET {} = TRUE, {} = 'delete', {} = %s WHERE ").format(
                rel, sql.Identifier(settings.meta_deleted), sql.Identifier(settings.meta_op),
                sql.Identifier(settings.meta_extracted_at)) + where
            now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            params = [tuple([now] + [k.get(p) for p in primary_key]) for k in keys]
        else:
            stmt = sql.SQL("DELETE FROM {} WHERE ").format(rel) + where
            params = [tuple(k.get(p) for p in primary_key) for k in keys]
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.executemany(stmt.as_string(cur), params)
            conn.commit()
        return len(keys)

    def truncate(self, table: str, namespace: str | None) -> None:
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("TRUNCATE TABLE {}").format(self._rel(table, namespace)))
            conn.commit()

    def swap_in(self, table: str, namespace: str | None, shadow_table: str) -> None:
        schema = self._schema(namespace)
        old = bounded_name(table, "__hpold")
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = %s AND table_name = %s", (schema, table))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                    self._rel(table, schema), sql.Identifier(old)))
            cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                self._rel(shadow_table, schema), sql.Identifier(table)))
            if exists:
                cur.execute(sql.SQL("DROP TABLE {}").format(self._rel(old, schema)))
            conn.commit()
