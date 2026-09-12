"""Batch (full refresh / incremental) sync execution for one connection."""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..bus import publish
from ..config import settings
from ..connectors import build_destination, build_source
from ..connectors.base import ConnectorError, StreamSchema, shadow_table_name
from ..db import session_scope
from ..logging_util import log
from ..models import Connection, SyncRun
from ..timeutil import utcnow


def _stream_config(conn: Connection, stream_def: dict) -> tuple[StreamSchema, dict]:
    schema = StreamSchema.from_dict(stream_def["schema"]) if "schema" in stream_def else \
        StreamSchema(name=stream_def["name"], table=stream_def["table"],
                     namespace=stream_def.get("namespace"),
                     primary_key=stream_def.get("primary_key", []))
    return schema, stream_def


def run_batch_sync(connection_id: str, *, trigger: str = "manual",
                   only_streams: list[str] | None = None) -> str:
    """Runs synchronously in the calling thread; returns the SyncRun id.
    `only_streams`, when given, limits the run to streams whose schema name
    is in the list — used for a per-table resync instead of the full
    connection."""
    with session_scope() as s:
        conn = s.get(Connection, connection_id)
        if not conn or not conn.enabled:
            raise ConnectorError("Connection not found or disabled")
        run = SyncRun(connection_id=connection_id, trigger=trigger, status="running")
        s.add(run)
        s.flush()
        run_id = run.id
        source_type, source_cfg = conn.source.type, dict(conn.source.config)
        dest_type, dest_cfg = conn.destination.type, dict(conn.destination.config)
        streams = list(conn.streams)
        namespace = conn.destination_namespace
        prefix = conn.table_prefix or ""
        state = dict(conn.state or {})

    with session_scope() as s:
        c = s.get(Connection, connection_id)
        c.status = "running"
        c.status_detail = None

    log(f"Batch sync started ({trigger})", connection_id=connection_id, run_id=run_id)
    publish("run_started", connection_id=connection_id, run_id=run_id, trigger=trigger)

    totals = {"read": 0, "written": 0, "deleted": 0, "bytes": 0}
    stream_stats: dict[str, Any] = {}
    error_text = None

    try:
        source = build_source(source_type, source_cfg)
        destination = build_destination(dest_type, dest_cfg)

        selected = [st for st in streams if st.get("selected", True)]
        if only_streams:
            selected = [st for st in selected if st["schema"]["name"] in only_streams]
        for stream_def in selected:
            schema = StreamSchema.from_dict(stream_def["schema"])
            dest_table = stream_def.get("destination_table") or f"{prefix}{schema.table}"
            sync_mode = stream_def.get("sync_mode", "full_refresh")  # full_refresh|incremental|xmin
            # "xmin" is Postgres's built-in incremental update method: it tracks
            # the system xmin column instead of a user-chosen cursor column.
            cursor_field = "xmin" if sync_mode == "xmin" else stream_def.get("cursor_field")
            incremental = sync_mode in ("incremental", "xmin")
            pk = stream_def.get("primary_key") or schema.primary_key
            # prepare() derives the destination table's PRIMARY KEY from
            # schema.primary_key — make sure a stream-level override (e.g.
            # xmin mode, which needs a PK for upserts but the source may not
            # have a real PK constraint to auto-discover) actually reaches it.
            schema.primary_key = pk

            # A user-picked column subset always keeps the primary key and
            # cursor column even if not explicitly checked — both are needed
            # structurally (upsert matching, incremental comparison) whether
            # or not they're meant to end up as "visible" synced data.
            selected_columns = stream_def.get("columns")
            if selected_columns:
                working_cols = set(selected_columns) | set(pk)
                if cursor_field and cursor_field != "xmin":
                    working_cols.add(cursor_field)
                schema.columns = [c for c in schema.columns if c.name in working_cols]
            read_columns = schema.column_names() if selected_columns else None

            # full_refresh loads into a shadow table and swaps it in only
            # once fully written, so a reader querying the destination mid-
            # sync sees the complete old table or the complete new one —
            # never a truncated-but-still-loading table (see swap_in()).
            if sync_mode == "full_refresh":
                write_table = shadow_table_name(dest_table)
                destination.prepare(write_table, schema, namespace)
                destination.truncate(write_table, namespace)
            else:
                write_table = dest_table
                destination.prepare(write_table, schema, namespace)

            cursor_value = None
            if incremental and cursor_field:
                cursor_value = (state.get("cursors") or {}).get(schema.name)

            read_count = written_count = 0
            max_seen = cursor_value
            columns = schema.column_names() + [settings.meta_extracted_at, settings.meta_op,
                                               settings.meta_deleted]
            now = utcnow()

            for batch in source.read(schema, cursor_field=cursor_field if incremental else None,
                                     cursor_value=cursor_value, batch_size=settings.batch_size,
                                     columns=read_columns):
                read_count += len(batch)
                rows = []
                for row in batch:
                    row = dict(row)
                    row[settings.meta_extracted_at] = now
                    row[settings.meta_op] = "read"
                    row[settings.meta_deleted] = False
                    rows.append(row)
                    if cursor_field and row.get(cursor_field) is not None:
                        if max_seen is None or row[cursor_field] > max_seen:
                            max_seen = row[cursor_field]

                write_mode = "upsert" if pk else "append"
                written_count += destination.write(write_table, namespace, columns, rows,
                                                    primary_key=pk, mode=write_mode)

                totals["read"] += len(batch)
                totals["written"] += len(rows)
                publish("run_progress", connection_id=connection_id, run_id=run_id,
                       stream=schema.name, read=totals["read"], written=totals["written"])

            if sync_mode == "full_refresh":
                destination.swap_in(dest_table, namespace, write_table)

            if incremental and cursor_field and max_seen is not None:
                cursors = state.setdefault("cursors", {})
                cursors[schema.name] = max_seen

            stream_stats[schema.name] = {"read": read_count, "written": written_count}
            log(f"Stream {schema.name}: read {read_count}, wrote {written_count}",
                connection_id=connection_id, run_id=run_id)

        with session_scope() as s:
            c = s.get(Connection, connection_id)
            c.state = state
            c.status = "idle"
            c.last_run_at = utcnow()
            c.status_detail = None

        status = "success"
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
        log(f"Batch sync failed: {error_text}", level="ERROR", connection_id=connection_id, run_id=run_id)
        with session_scope() as s:
            c = s.get(Connection, connection_id)
            if c:
                c.status = "error"
                c.status_detail = error_text[:2000]
        status = "failed"
    finally:
        try:
            source.close()
            destination.close()
        except Exception:
            pass

    with session_scope() as s:
        run = s.get(SyncRun, run_id)
        run.status = status
        run.finished_at = utcnow()
        run.records_read = totals["read"]
        run.records_written = totals["written"]
        run.records_deleted = totals["deleted"]
        run.stream_stats = stream_stats
        run.error = error_text

    publish("run_finished", connection_id=connection_id, run_id=run_id, status=status,
           records_read=totals["read"], records_written=totals["written"])
    log(f"Batch sync {status}: read={totals['read']} written={totals['written']}",
       connection_id=connection_id, run_id=run_id, level="ERROR" if status == "failed" else "INFO")
    return run_id
