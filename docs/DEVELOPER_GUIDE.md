---
title: HolyPipe Developer Guide — Codebase & Extending Connectors
description: >-
  How HolyPipe's codebase fits together — connectors, the sync engine, CDC
  workers, the scheduler, and the canonical-type system — and how to add a
  new connector or extend an existing one.
---

# HolyPipe — Developer Guide

How the codebase fits together, and how to extend it. For running the
dashboard see the [User Guide](USER_GUIDE.md); for deploying it see the
[Administrator Guide](ADMIN_GUIDE.md).

## Layout

```
holypipe/
  connectors/        Source & destination drivers (postgres, mysql, sqlite)
                      behind a shared canonical-type interface.
    base.py            The BaseSource/BaseDestination contracts, the
                       StreamSchema/Column dataclasses, and the canonical
                       type system every connector normalizes into.
    postgres.py        Batch reads, upsert/delete writes, logical
                       replication CDC (wal2json or test_decoding).
    mysql.py            Same shape, binlog CDC via mysql-replication.
    sqlite.py           Same shape, trigger-based CDC journal.
    registry.py         Maps type name -> connector class, and the field
                       specs the UI renders for each connector type.
    dsn.py             Parses a pasted connection URI into a config dict.
  engine/
    sync.py             Batch full-refresh/incremental/xmin runner —
                       one call processes every selected stream in a
                       connection (or a subset, for a per-table resync).
    cdc.py              CdcWorker: one background thread per streaming
                       connection. Initial snapshot, then continuous
                       streaming via the connector's stream_changes().
    scheduler.py         Polls enabled connections every ~2s: dispatches
                       due batch runs to a thread pool, starts/stops CDC
                       workers to match what's enabled, backs off retrying
                       a persistently-failing CDC connection.
    recovery.py         Runs once at startup: cleans up any run/connection
                       left mid-flight by an ungraceful previous shutdown.
  api/
    routes.py           All HTTP + the /ws websocket endpoint.
    schemas.py           Pydantic request/response models.
  web/static/           Plain HTML/CSS/JS dashboard — no build step, no
                       framework. app.js builds the whole UI by hand with
                       a small `el()` DOM-builder helper.
  models.py             SQLAlchemy ORM: Source, Destination, Connection,
                       SyncRun, LogEntry — HolyPipe's own metadata, not
                       the data being synced.
  cache.py             A tiny in-memory TTL cache (used for schema
                       discovery results).
  bus.py               Thread-safe event queue bridging sync-worker
                       threads to the asyncio websocket layer.
  timeutil.py           A single `utcnow()` used for everything stored in
                       or compared against the metadata DB — see the note
                       in that file about why (SQLite strips tzinfo on
                       round-trip; mixing aware/naive datetimes throws).
  db.py, config.py, logging_util.py, main.py
```

## The canonical type system

Every connector normalizes source column types into nine canonical types
(`holypipe/connectors/base.py`): `string`, `integer`, `number`, `boolean`,
`timestamp`, `date`, `time`, `json`, `binary`. This is what lets a
Postgres `numeric` column, a MySQL `decimal` column, and whatever SQLite
calls it all land in the same kind of destination column regardless of
which two engines are on either end. `StreamSchema` and `Column`
(dataclasses, not ORM models) are the shared shape connectors pass
between each other; `normalize_value()`/`normalize_record()` coerce
driver-native Python values (Decimal, UUID, memoryview, ...) into
JSON/DBAPI-friendly primitives at the point a row is read.

## How a batch sync runs (`engine/sync.py`)

`run_batch_sync(connection_id, trigger=..., only_streams=None)`:

1. Loads the connection's source/destination config and stored `state`
   (which holds incremental/xmin cursor positions per stream, keyed by
   stream name) from the metadata DB.
2. For each selected stream (optionally filtered to `only_streams`, used
   by the per-table resync API):
   - Resolves the effective column set: the stream's explicit column
     selection (if any) unioned with its primary key and cursor column
     (both structurally required regardless of what's "visible"), then
     mutates a local copy of `schema.columns` to that set — everything
     downstream (`prepare()`, the SELECT list, the write column list)
     reads off that filtered schema, so one filter point controls all
     three.
   - `destination.prepare()` creates the destination table if missing, or
     adds any newly-required columns to an existing one (never drops or
     alters existing columns).
   - For `full_refresh`, truncates the destination table first.
   - For `incremental`/`xmin`, reads the stored cursor from `state` (a
     resync clears it first, so this naturally becomes a full reload
     without any separate code path).
   - Streams the source in `HOLYPIPE_BATCH_SIZE`-row batches (a named
     server-side cursor for Postgres, `SSCursor`/`SSDictCursor` for MySQL,
     plain `fetchmany` for SQLite — a full table is never held in memory),
     upserting (if there's a primary key) or appending each batch to the
     destination, and tracking the max cursor value seen.
3. Persists the updated cursor state and writes a `SyncRun` row with
   final counts.

## How CDC runs (`engine/cdc.py`)

`CdcWorker` is a `threading.Thread`, one per streaming connection,
managed by a `CdcSupervisor` the scheduler keeps in sync with which
connections are currently `enabled` and in `cdc` mode.

1. `source.setup_cdc()` — creates the Postgres replication slot / records
   the MySQL binlog position / installs the SQLite trigger journal. This
   happens **before** anything else so the replication log starts
   capturing changes immediately, even ones that land during the next
   step.
2. **Initial snapshot** (`_initial_snapshot`, only once per connection —
   gated on a `snapshot_done` flag in `state`): a full read+write of every
   selected stream's current rows, same batching/upsert mechanics as a
   batch full refresh. Without this, CDC would only ever see rows that
   change *after* it starts; existing rows would never arrive. Any change
   that happens to land during the snapshot window is still captured by
   the replication log from step 1 and gets replayed as an upsert
   afterward — harmless, since it just re-writes the same row.
3. `source.stream_changes()` — blocks, calling `on_batch()` whenever
   `HOLYPIPE_CDC_FLUSH_RECORDS` events have buffered or
   `HOLYPIPE_CDC_FLUSH_SECONDS` has elapsed, whichever first.
   `_apply_batch()` groups events by stream and upserts/soft-deletes them.

CDC's per-connector `stream_changes()` implementations:
- **Postgres**: a dedicated `LogicalReplicationConnection`, decoding
  `wal2json` if the slot was created with that plugin, otherwise parsing
  the built-in `test_decoding` text format by hand (see
  `_parse_test_decoding`/`_scan_columns` — there's no library for this).
- **MySQL**: `pymysqlreplication.BinLogStreamReader`, filtered to the
  relevant schemas/tables.
- **SQLite**: polls the `_holypipe_cdc_log` journal table the triggers
  write to, trims consumed rows after each poll.

## Connection reuse and why `close()` matters

`with conn:` on a psycopg2 connection only commits/rolls back the current
transaction — it does **not** close the socket. Every `PostgresMixin`,
`MysqlMixin`, and `SqliteMixin` therefore holds one lazily-opened
connection per connector instance (`_get_conn()`), reused across calls,
with `close()` overridden to actually close it. `sync.py`/`cdc.py` call
`source.close()`/`destination.close()` in a `finally` block at the end of
every run. If you add a new connector or a new method on an existing one,
route it through `_get_conn()`, not a fresh `_connect()` call, unless you
have a specific reason to isolate the connection (like `read()`'s
dedicated named-cursor/readonly connection on Postgres, or the CDC
replication connection).

## Adding a new connector

Implement `BaseSource` and/or `BaseDestination` from
`holypipe/connectors/base.py`:

- `test()` — return a short string on success, raise `ConnectorError` on
  failure.
- `discover()` — return `list[StreamSchema]` for every table.
- `read(stream, *, cursor_field=None, cursor_value=None, batch_size=1000,
  columns=None)` — a generator yielding lists of row dicts.
- `max_cursor()` — currently unused by the engine but part of the
  interface; implement for completeness.
- For CDC: `supports_cdc = True`, plus `setup_cdc()`, `stream_changes()`,
  `teardown_cdc()`.
- Destination: `prepare()`, `write()`, `delete()`, `truncate()`.

Then register it in `holypipe/connectors/registry.py` — add it to
`SOURCE_TYPES`/`DESTINATION_TYPES` and give it a `CONFIG_SPEC` entry (the
field list the dashboard's connector form renders; supports `text`,
`password`, `number`, `checkbox`, and `select` field kinds).

## Frontend

`holypipe/web/static/app.js` is one file, no build step, no framework. The
`el(tag, attrs, children)` helper builds DOM nodes by hand (attributes,
an `onX` key wires an event listener, `class`/`html` are special-cased).
Modals are a single reused `#modal`/`#modalBackdrop` pair — `openModal()`/
`closeModal()` swap their content; pass `{ wide: true }` for the larger
detail/edit views. `createSourcePicker()` and `createTableToolbar()` are
the reusable building blocks behind both the New Connection wizard and
the "+ Add source"/"+ Add more tables" flows — extend those rather than
duplicating a table-picker UI if you add another place that needs one.

State lives in a plain `state` object (`sources`, `destinations`,
`connections`, `connectorTypes`) refreshed from the API; there's no
client-side framework reactivity — after a mutation, code explicitly calls
`refreshAll()`/`refreshConnections()` and re-renders. Live updates (log
lines, run progress) arrive over `/api/ws` and are applied directly to the
DOM (`app.js`'s `connectWebSocket()`), independent of the periodic REST
refresh.

## Local development without Docker

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e .
uvicorn holypipe.main:app --reload
```

You'll need your own Postgres/MySQL to point sources/destinations at, or
use `sqlite` (just a file path) for a zero-setup source/destination.
Static files are served straight from `holypipe/web/static/` — edit and
refresh the browser, no build/watch step.

There is currently no automated test suite. When adding one, the natural
seams are: connector unit tests against real dockerized Postgres/MySQL/
SQLite instances (the `demo/` compose services are a reasonable base),
and API tests against the FastAPI app directly (`TestClient`).

## Conventions

- No ORM migration system — `models.py`'s `Base.metadata.create_all()`
  only adds missing tables, never alters existing columns. Keep this in
  mind when changing a model: existing deployments won't pick up an
  altered column without a manual `ALTER TABLE` or a fresh volume.
- Every datetime stored in or compared against the metadata DB goes
  through `timeutil.utcnow()` (naive UTC) — never
  `datetime.now(timezone.utc)` directly in code that touches `Connection`/
  `SyncRun` timestamps. See `holypipe/timeutil.py` for why.
- Prefer extending an existing connector's shape over adding
  connector-specific branches in `engine/`. The engine code should only
  ever call the `BaseSource`/`BaseDestination` interface.
