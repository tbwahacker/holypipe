"""Real-time CDC streaming worker: one background thread per streaming connection."""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

from ..bus import publish
from ..config import settings
from ..connectors import build_destination, build_source
from ..connectors.base import ChangeEvent, ConnectorError, StreamSchema
from ..db import session_scope
from ..logging_util import log
from ..models import Connection, SyncRun
from ..timeutil import utcnow


class CdcWorker(threading.Thread):
    """Owns one long-lived replication stream for a single connection."""

    def __init__(self, connection_id: str):
        super().__init__(name=f"cdc-{connection_id}", daemon=True)
        self.connection_id = connection_id
        self.stop_event = threading.Event()
        self._run_id: str | None = None
        self._totals = {"read": 0, "written": 0, "deleted": 0}
        self._last_persist = 0.0

    def stop(self) -> None:
        self.stop_event.set()

    # -- lifecycle ------------------------------------------------------------
    def run(self) -> None:
        with session_scope() as s:
            conn = s.get(Connection, self.connection_id)
            if not conn or not conn.enabled:
                return
            source_type, source_cfg = conn.source.type, dict(conn.source.config)
            dest_type, dest_cfg = conn.destination.type, dict(conn.destination.config)
            streams_def = [st for st in conn.streams if st.get("selected", True)]
            namespace = conn.destination_namespace
            prefix = conn.table_prefix or ""
            state = dict(conn.state or {})

            run = SyncRun(connection_id=self.connection_id, trigger="cdc", status="running")
            s.add(run)
            s.flush()
            self._run_id = run.id

        with session_scope() as s:
            c = s.get(Connection, self.connection_id)
            c.status = "streaming"
            c.status_detail = None

        log("CDC streaming started", connection_id=self.connection_id, run_id=self._run_id)
        publish("run_started", connection_id=self.connection_id, run_id=self._run_id, trigger="cdc")

        source = None
        destination = None
        error_text = None
        try:
            source = build_source(source_type, source_cfg)
            destination = build_destination(dest_type, dest_cfg)
            if not source.supports_cdc:
                raise ConnectorError(f"{source_type} source does not support CDC")

            schemas: dict[str, StreamSchema] = {}
            dest_tables: dict[str, str] = {}
            for st in streams_def:
                schema = StreamSchema.from_dict(st["schema"])
                # Respect a stream-level primary_key override (the source may
                # not have a real PK constraint to auto-discover) — prepare()
                # and every write()/delete() below key off schema.primary_key.
                schema.primary_key = st.get("primary_key") or schema.primary_key
                # A user-picked column subset always keeps the primary key —
                # CDC still receives full rows over the replication stream
                # (no column-level filtering at that layer), but only the
                # kept columns get created in / written to the destination.
                selected_columns = st.get("columns")
                if selected_columns:
                    keep = set(selected_columns) | set(schema.primary_key)
                    schema.columns = [c for c in schema.columns if c.name in keep]
                schemas[schema.name] = schema
                dest_tables[schema.name] = st.get("destination_table") or f"{prefix}{schema.table}"
                destination.prepare(dest_tables[schema.name], schema, namespace)

            # Create the replication slot / binlog position marker / trigger
            # journal *before* snapshotting, so any change that lands during
            # the snapshot read below is still captured by the log and
            # replayed by stream_changes() afterwards (an upsert, so replaying
            # it over the just-snapshotted row is harmless).
            state = source.setup_cdc(list(schemas.values()), state)
            self._persist_state(state)
            log(f"CDC initialized for {len(schemas)} stream(s)",
               connection_id=self.connection_id, run_id=self._run_id)

            if not state.get("snapshot_done"):
                self._initial_snapshot(source, destination, schemas, dest_tables)
                state["snapshot_done"] = True
                self._persist_state(state)
                log("Initial snapshot complete", connection_id=self.connection_id, run_id=self._run_id)
            else:
                log("Resuming CDC (initial snapshot already done)",
                   connection_id=self.connection_id, run_id=self._run_id)

            def on_batch(events: list[ChangeEvent], position: dict) -> None:
                self._apply_batch(destination, schemas, dest_tables, events)
                state.update(position)
                self._persist_state(state, force=False)

            source.stream_changes(list(schemas.values()), state, self.stop_event, on_batch)
            self._persist_state(state, force=True)
            status = "cancelled" if self.stop_event.is_set() else "success"
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc)
            log(f"CDC streaming error: {error_text}", level="ERROR",
               connection_id=self.connection_id, run_id=self._run_id)
            status = "failed"
        finally:
            try:
                if source:
                    source.close()
                if destination:
                    destination.close()
            except Exception:
                pass

        with session_scope() as s:
            run = s.get(SyncRun, self._run_id)
            if run:
                run.status = status
                run.finished_at = utcnow()
                run.records_read = self._totals["read"]
                run.records_written = self._totals["written"]
                run.records_deleted = self._totals["deleted"]
                run.error = error_text
            c = s.get(Connection, self.connection_id)
            if c:
                c.status = "error" if status == "failed" else "idle"
                c.status_detail = error_text[:2000] if error_text else None
                c.last_run_at = utcnow()

        publish("run_finished", connection_id=self.connection_id, run_id=self._run_id,
               status=status, records_read=self._totals["read"], records_written=self._totals["written"])
        log(f"CDC streaming stopped ({status})", connection_id=self.connection_id, run_id=self._run_id,
           level="ERROR" if status == "failed" else "INFO")

    # -- helpers ----------------------------------------------------------------
    def _initial_snapshot(self, source, destination, schemas: dict[str, StreamSchema],
                          dest_tables: dict[str, str]) -> None:
        """One-time full copy of each stream's current rows, run once per
        connection right after the CDC log position is captured. Without
        this, CDC mode would only ever see rows that change *after* it
        starts — anything already in the table would never arrive."""
        now = utcnow()
        for schema in schemas.values():
            table = dest_tables[schema.name]
            pk = schema.primary_key
            columns = schema.column_names() + [settings.meta_extracted_at, settings.meta_op,
                                               settings.meta_deleted]
            read_count = written_count = 0
            for batch in source.read(schema, batch_size=settings.batch_size,
                                     columns=schema.column_names()):
                rows = []
                for row in batch:
                    row = dict(row)
                    row[settings.meta_extracted_at] = now
                    row[settings.meta_op] = "snapshot"
                    row[settings.meta_deleted] = False
                    rows.append(row)
                mode = "upsert" if pk else "append"
                written_count += destination.write(table, None, columns, rows, primary_key=pk, mode=mode)
                read_count += len(batch)
                self._totals["read"] += len(batch)
                self._totals["written"] += len(rows)
                publish("run_progress", connection_id=self.connection_id, run_id=self._run_id,
                       stream=schema.name, read=self._totals["read"], written=self._totals["written"])
            log(f"Snapshot {schema.name}: read {read_count}, wrote {written_count}",
               connection_id=self.connection_id, run_id=self._run_id)

    def _apply_batch(self, destination, schemas: dict[str, StreamSchema],
                     dest_tables: dict[str, str], events: list[ChangeEvent]) -> None:
        by_stream: dict[str, list[ChangeEvent]] = {}
        for ev in events:
            by_stream.setdefault(ev.stream, []).append(ev)

        now = utcnow()
        for stream_name, evs in by_stream.items():
            schema = schemas.get(stream_name)
            if not schema:
                continue
            table = dest_tables[stream_name]
            pk = schema.primary_key
            columns = schema.column_names() + [settings.meta_extracted_at, settings.meta_op,
                                               settings.meta_deleted]

            upserts, deletes = [], []
            for ev in evs:
                if ev.op == "delete":
                    deletes.append(ev.key or ev.data)
                else:
                    row = {c: ev.data.get(c) for c in schema.column_names()}
                    row[settings.meta_extracted_at] = now
                    row[settings.meta_op] = ev.op
                    row[settings.meta_deleted] = False
                    upserts.append(row)

            written = deleted = 0
            if upserts:
                mode = "upsert" if pk else "append"
                written = destination.write(table, None, columns, upserts, primary_key=pk, mode=mode)
            if deletes and pk:
                deleted = destination.delete(table, None, pk, deletes, soft=True)

            self._totals["read"] += len(evs)
            self._totals["written"] += written
            self._totals["deleted"] += deleted

        publish("run_progress", connection_id=self.connection_id, run_id=self._run_id,
               read=self._totals["read"], written=self._totals["written"],
               deleted=self._totals["deleted"])

    def _persist_state(self, state: dict, force: bool = True) -> None:
        now = time.time()
        if not force and now - self._last_persist < 2.0:
            return
        self._last_persist = now
        with session_scope() as s:
            c = s.get(Connection, self.connection_id)
            if c:
                c.state = dict(state)


class CdcSupervisor:
    """Registry of running CDC worker threads, keyed by connection id."""

    def __init__(self) -> None:
        self._workers: dict[str, CdcWorker] = {}
        self._lock = threading.Lock()

    def is_running(self, connection_id: str) -> bool:
        with self._lock:
            w = self._workers.get(connection_id)
            return bool(w and w.is_alive())

    def start(self, connection_id: str) -> None:
        with self._lock:
            existing = self._workers.get(connection_id)
            if existing and existing.is_alive():
                return
            worker = CdcWorker(connection_id)
            self._workers[connection_id] = worker
            worker.start()

    def stop(self, connection_id: str, timeout: float = 15.0) -> None:
        with self._lock:
            worker = self._workers.pop(connection_id, None)
        if worker:
            worker.stop()
            worker.join(timeout=timeout)

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=15.0)

    def running_ids(self) -> list[str]:
        with self._lock:
            return [cid for cid, w in self._workers.items() if w.is_alive()]


supervisor = CdcSupervisor()
