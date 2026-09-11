# HolyPipe — User Guide

This guide covers using the HolyPipe web dashboard to move data between
PostgreSQL, MySQL, and SQLite databases. If you need to install or deploy
HolyPipe itself, see the [Administrator Guide](ADMIN_GUIDE.md) instead.

## Opening the dashboard

Once HolyPipe is running, open it in a browser — by default
`http://localhost:8090` (the port depends on how it was deployed; ask
whoever set it up if you're not sure). The dashboard has four tabs:
**Connections**, **Sources**, **Destinations**, and **Live Logs**.

## Core concepts

- **Source** — a database HolyPipe reads from.
- **Destination** — a database HolyPipe writes to.
- **Connection** — one source paired with one destination, plus the list of
  tables to sync and how often. This is the thing that actually moves data.
- **Stream** — one table within a connection.

A connection always has exactly one source. If you want to pull from
several source databases into the same destination, HolyPipe creates one
connection per source for you automatically when you check multiple
sources in the wizard (see below) — this is because real-time sync (CDC)
is fundamentally tied to a single database's replication log, so mixing
sources into one connection isn't something any tool can safely do.

## Step 1 — Add a source

**Sources** tab → **+ New Source**.

1. Give it a name and pick a type: PostgreSQL, MySQL, or SQLite.
2. Fill in the connection details, either:
   - **Form fields** — host, port, database, username, password, and an
     SSL mode, or
   - **Connection URI** — paste a full connection string, e.g.
     `postgresql://user:pass@host:5432/dbname?sslmode=require`,
     `mysql://user:pass@host:3306/dbname?ssl_mode=REQUIRED`, or
     `sqlite:////data/app.db`.
3. Click **Test connection** before saving — it tells you immediately if
   the credentials or network path are wrong.
4. **Save**.

You can **Edit** a source later (e.g. to rotate a password) or **Delete**
it once no connection uses it anymore.

## Step 2 — Add a destination

**Destinations** tab → **+ New Destination**. Same form as a source — pick
a type, fill in details or paste a URI, test, save. Destination tables are
created and evolved automatically by HolyPipe; you don't need to
pre-create anything on the destination side.

## Step 3 — Create a connection

**Connections** tab → **+ New Connection**.

1. **Name** it.
2. **Source(s)** — check one or more. Checking more than one creates a
   separate connection per source once you save, all pointed at the same
   destination with the same settings.
3. **Destination** — pick where the data lands.
4. **Sync mode**:
   - **Batch (scheduled polling)** — runs on a schedule you set (a fixed
     interval in seconds, or a cron expression).
   - **Real-time (CDC streaming)** — starts a continuous stream the moment
     you save, and keeps the destination up to date within about a second
     of a change happening at the source. Only available for sources that
     support it (currently PostgreSQL and MySQL — see the Administrator
     Guide for what the source database needs enabled).
5. **Destination table prefix** / **namespace** (optional) — useful if
   you're syncing tables with the same name from two different source
   schemas into one destination; give one of them a prefix so they don't
   collide.
6. Click **Discover tables**. HolyPipe scans the source and lists every
   table. For each one you can set:
   - **Select all / Select none** — bulk-select the checkboxes.
   - Per table: check it in or out, choose its **update method** (below),
     and click **Columns (N/M)** to expand a picker if you only want a
     subset of that table's columns synced (the primary key is always
     included — it's needed to match rows during updates).
   - **Set update method for selected…** + **Apply** — bulk-set the
     update method across everything currently checked/visible, instead
     of clicking through each table's dropdown one at a time.
   - The **Filter tables by name…** box narrows a long list down as you
     type.
7. **Create connection**.

### Update methods (per table, batch mode)

- **Full refresh** — every run, the destination table is wiped and
  reloaded completely. Simple and safe, but wasteful for large tables that
  don't need re-copying every time.
- **Incremental (cursor column)** — you pick a column (usually an
  auto-incrementing ID or an "updated at" timestamp) and each run only
  reads rows newer than the highest value seen last time.
- **Xmin** (PostgreSQL sources only) — like incremental, but uses
  Postgres's own internal row-version column instead of you having to pick
  one. Handy when a table doesn't have a clean incrementing column. Note:
  it wraps around after roughly 4 billion transactions on that table, at
  which point a full refresh is needed — a non-issue for the vast majority
  of tables.
- **CDC** isn't a per-table choice — it's set at the connection level (step
  4 above) and applies to every table in that connection.

## Managing a connection

Click anywhere on a connection's card (not on one of its buttons) to open
its **detail view** — the full picture: status, source → destination,
stream counts, last/next run, every action, and the complete table list.

### Actions

- **Run now** (batch) / **Start · Stop** (CDC) — the normal sync action.
  For batch, this reads whatever incremental/xmin cursor is stored and
  only picks up what's new. For CDC, Start begins streaming (including a
  one-time copy of every row currently in the table before it switches to
  live changes); Stop ends it cleanly.
- **Activate · Deactivate** — pauses or resumes the connection without
  deleting it. A deactivated connection is skipped by the scheduler
  entirely (no runs, no CDC streaming) until you reactivate it.
- **↻ Resync all** / the **↻** button next to each table — forces a full
  reload, ignoring any stored incremental/xmin cursor. Use this when you
  need to be sure the destination has *everything* again — for example
  after fixing a data issue at the source, or after changing which columns
  are synced. This is different from Run/Start, which only ever picks up
  the delta. Per-table resync isn't available for CDC connections (its
  initial copy covers the whole connection at once); use "Resync all"
  there instead.
- **Edit** — change the name, schedule, table prefix/namespace, which
  tables/columns are synced, and each table's update method. You can also
  click **+ Add more tables from source** here to pick up new tables that
  appeared since the connection was created.
- **+ Add source** — attach another source database to feed the same
  destination, without recreating everything. Opens a picker like the
  original wizard; it creates a new connection alongside this one, sharing
  the same destination, mode, and schedule.
- **Runs** — history of every sync attempt: when, how long, how many rows
  read/written, and the error message if it failed.
- **Delete** — removes the connection (and stops any CDC stream). It does
  **not** delete data already written to the destination.

### Reading the status badges

- **active / paused** reflects whether the connection is enabled at all.
- The colored status pill (idle / running / streaming / error) reflects
  what it's doing right now.
- A red line under the title is the last error, if any.

## Live Logs

The **Live Logs** tab streams everything HolyPipe is doing across every
connection, in real time, over a websocket — useful for watching a sync
happen or diagnosing a problem as it occurs rather than after the fact.

## Frequently asked questions

**Does deleting a source/destination delete the underlying database?**
No. HolyPipe only stores connection details; deleting a source or
destination just removes HolyPipe's record of how to reach it. You can't
delete one that's still in use by a connection — delete the connection(s)
first.

**What are the `_hp_extracted_at` / `_hp_op` / `_hp_deleted` columns in my
destination tables?** HolyPipe adds these to every table it writes to:
when the row was last synced, what kind of write it was (`read` / `insert`
/ `update` / `snapshot` / `delete`), and whether it's been soft-deleted
(rows deleted at the source are marked deleted here rather than physically
removed, so you have a record of what disappeared and when).

**I changed which columns are synced — why does the destination table
still have the old columns?** HolyPipe never drops columns from an
existing destination table, only adds new ones it needs. The old column
just stops receiving new data. Drop it yourself if you want it gone.

**A run failed with an error about `wal_level` or `binlog` — what do I
do?** That's a source-database configuration requirement for CDC, not
something wrong with your connection setup. See the Administrator Guide's
CDC section, or switch that connection to batch mode with `xmin`
(Postgres) or `incremental` in the meantime — no source restart needed.
