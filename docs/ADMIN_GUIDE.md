# HolyPipe — Administrator Guide

Deploying, configuring, and operating HolyPipe. For using the dashboard
day-to-day see the [User Guide](USER_GUIDE.md); for the codebase see the
[Developer Guide](DEVELOPER_GUIDE.md).

## Platform support

HolyPipe runs entirely inside Docker containers (Python app + web
dashboard, all Linux-based images), so **it works identically on Windows,
macOS, and Linux** — the host OS never runs HolyPipe's code directly, only
Docker does. Requirements are just:

- Docker Desktop (Windows/Mac) or Docker Engine + Compose plugin (Linux).
- Nothing else — no Python, no Node, no local database client required on
  the host.

The one platform difference worth knowing: HolyPipe's own container needs
`host.docker.internal` to reach a database running directly on the host
machine (as opposed to in another container). Docker Desktop resolves this
automatically on Windows and Mac. On native Linux Docker it needs an
explicit mapping, which `docker-compose.yml` already includes
(`extra_hosts: host.docker.internal:host-gateway`) — so this works
out of the box on all three platforms as shipped. If you hand-roll your
own `docker run` instead of using the provided compose file, add
`--add-host=host.docker.internal:host-gateway` yourself on Linux.

## Deployment

```bash
git clone <this repo>
cd holypipe
docker compose up -d --build
```

This starts four containers: the app itself, plus three demo databases
(`postgres_demo`, `mysql_demo`, `warehouse`) seeded with sample data so
there's something to point HolyPipe at immediately. In a real deployment
you'll typically remove the three demo services from `docker-compose.yml`
(or just never add sources pointing at them) and configure HolyPipe to
reach your actual databases instead — see the User Guide for adding
sources/destinations through the dashboard.

The app listens on port 8000 inside the container; the compose file maps
it to `8090` on the host (`ports: "8090:8000"`) — change the host side of
that mapping if 8090 is taken, or if you're running behind a reverse
proxy, point the proxy at the container's 8000 directly.

### Updating

```bash
git pull
docker compose up -d --build
```

The metadata volume (`holypipe_data`) persists across rebuilds — your
sources, destinations, connections, and run history survive an update.
CDC connections and any batch sync that was mid-run when the container
stopped are automatically recovered on the next startup (see
[Stability and recovery](#stability-and-recovery) below) — no manual
cleanup needed after a redeploy.

## Configuration

All configuration is environment variables on the `holypipe` service in
`docker-compose.yml` (or passed however you run the container).

| Variable | Default | Meaning |
|---|---|---|
| `HOLYPIPE_METADATA_URL` | `sqlite:///./data/holypipe.db` | Where HolyPipe stores its own config/state — sources, destinations, connections, run history, logs. Not your synced data. |
| `HOLYPIPE_SCHEDULER` | `1` | Set `0` to disable the background scheduler/CDC supervisor entirely — useful for a read-only API instance that only serves the dashboard without running any syncs. |
| `HOLYPIPE_BATCH_SIZE` | `1000` | Rows fetched per batch from a source and written per batch to a destination. Sources stream via server-side cursors regardless, so raising this trades more memory per batch for fewer round-trips; it doesn't change whether a whole table gets loaded into memory (it never does). |
| `HOLYPIPE_CDC_FLUSH_SECONDS` | `1.0` | Max latency before a buffered CDC batch is flushed to the destination. |
| `HOLYPIPE_CDC_FLUSH_RECORDS` | `500` | Max buffered CDC events before an early flush, regardless of the time-based flush above. |
| `HOLYPIPE_SCHEDULER_WORKERS` | `4` | Concurrent **batch** syncs the scheduler runs at once. Raise this if you have many batch connections you want running in parallel rather than queueing. CDC connections are unaffected — each gets its own dedicated thread no matter what this is set to. |
| `HOLYPIPE_DISCOVERY_CACHE_TTL` | `300` | Seconds a source's discovered table/column list is cached before "Discover tables" re-scans it. The dashboard's "↻ Refresh" button bypasses this on demand. |
| `HOLYPIPE_LOG_LEVEL` | `INFO` | stdout log verbosity (standard Python levels). |
| `HOLYPIPE_LOG_RETENTION` | `2000` | How many log rows to keep in the metadata DB before old ones are pruned. |

## Enabling CDC on your own databases

Real-time (CDC) connections need the source database configured for
logical/row-based replication:

- **PostgreSQL**: `wal_level = logical` in `postgresql.conf` (or
  `ALTER SYSTEM SET wal_level = logical;`), which requires a **server
  restart** to take effect — this can't be changed live. The connecting
  user needs the `REPLICATION` privilege
  (`ALTER ROLE <user> WITH REPLICATION;`). Tables benefit from
  `REPLICA IDENTITY FULL` (or having a real primary key) so updates and
  deletes carry enough of the old row to match correctly. No `wal2json`
  extension install is required — HolyPipe uses it when present and falls
  back to Postgres's built-in `test_decoding` output plugin otherwise.
- **MySQL**: `log_bin = ON` and `binlog_format = ROW` (both usually set as
  server startup flags — see `docker-compose.yml`'s `mysql_demo` service
  for a working example), plus a user with `REPLICATION SLAVE,
  REPLICATION CLIENT` privileges. Give each concurrent CDC connection
  against the same MySQL server a unique `server_id` in its source config
  (the field is right there in the source form) — two CDC streams sharing
  one server_id will conflict.
- **SQLite**: nothing to configure — HolyPipe installs its own
  `AFTER INSERT/UPDATE/DELETE` triggers on the file the first time CDC
  starts, and polls the resulting change journal sub-second. This is the
  closest a single-file embedded database can get to real replication.

If a CDC connection can't meet these requirements (e.g. you don't control
`wal_level` on a managed Postgres instance), use batch mode with
`incremental` or `xmin` instead — no server-side changes needed, just a
short poll interval.

### CDC and destination primary keys

CDC (and `xmin` batch mode) need a primary key to upsert rows correctly.
If the source table has a real `PRIMARY KEY` constraint, HolyPipe finds it
automatically. If it doesn't (common with tables originally populated by
another ETL tool, which sometimes create "raw" tables with a natural key
column like `id` but no formal constraint), you'll need to either add a
primary key on the source, or note that upserts will fail with "no unique
or exclusion constraint matching the ON CONFLICT specification" until the
**destination** table has one — which you can add by hand with
`ALTER TABLE ... ADD PRIMARY KEY (...)` once you've confirmed the
destination table has no duplicate values in that column, since HolyPipe
never drops or alters an already-existing destination table's constraints
for you.

## Stability and recovery

- **Orphaned runs after a restart**: if the container is stopped while a
  sync is mid-flight (a redeploy, a crash, `docker stop`), the in-flight
  thread is killed with no chance to clean up. Without recovery this would
  leave that connection permanently stuck refusing new runs. HolyPipe
  checks for this on every startup, before the scheduler starts: any
  `SyncRun` still marked `running` is marked `failed` with a clear
  "interrupted by restart" message, and any connection stuck in
  `running`/`streaming` is reset to `idle` so the scheduler picks it back
  up normally. You'll see this in the startup log as `Recovered N orphaned
  run(s)` / `Reset N connection(s) stuck running/streaming`.
- **CDC retry backoff**: if a CDC connection's source is unreachable or
  misconfigured (e.g. `wal_level` not set), the scheduler doesn't hammer
  it with a fresh connection attempt every poll tick — retries back off
  exponentially (5s, 10s, 20s, … capped at 5 minutes) until it either
  succeeds or you intervene. Watch for `CDC restart attempt N — backing
  off` in the logs.
- **Connection reuse**: each running sync/CDC worker holds one connection
  per source and destination for its whole lifetime rather than
  reconnecting per batch — this matters most for CDC, which can flush as
  often as once a second. A dropped connection (e.g. the destination
  database restarted) is detected and transparently reconnected on the
  next call.

## Monitoring

- `GET /health` — plain liveness check, no auth, suitable for a container
  healthcheck or load balancer probe (already wired into the provided
  `Dockerfile`'s `HEALTHCHECK`).
- `GET /api/logs?connection_id=...` — recent log lines from the metadata
  DB.
- `GET /api/connections/{id}/runs` — run history with read/write counts
  and errors, per connection.
- The dashboard's **Live Logs** tab and the `/api/ws` websocket it uses
  are the real-time view of everything above.

## Security notes

- **Login is required** (default `admin`/`admin`, forced password change on
  first login) with dynamic roles/permissions — see the Administrator
  Guide's user management section for creating additional users and
  scoping their access. HolyPipe still doesn't terminate TLS itself, so put
  it behind a reverse proxy with HTTPS before exposing it beyond a private
  network or trusted VPN.
- **API tokens** (created from the dashboard's top-bar username menu, or
  `POST /api/tokens`) let scripts and AI agents — including the built-in
  MCP server at `/mcp` — act with a user's permissions without the
  cookie-based login flow. A token is a real credential: only ever transmit
  it over HTTPS, give it to an agent from a least-privilege role rather
  than an Administrator account when possible, and revoke it immediately
  if it might have leaked. See the
  [MCP Integration Guide](MCP_INTEGRATION.md).
- Source and destination credentials (including passwords) are stored in
  **plaintext** in the metadata database (`HOLYPIPE_METADATA_URL`) and are
  returned as plaintext by the API to populate the edit form. Treat that
  database file/volume as sensitive.
- SSL modes are configurable per source/destination (`sslmode` for
  Postgres, `ssl_mode` for MySQL) — use `require`/`REQUIRED` or stronger
  for anything crossing an untrusted network.
- The demo `docker-compose.yml` ships with hardcoded demo credentials
  (`holypipe`/`holypipe`, MySQL root password `rootpass`) for the
  bundled sample databases only — these are not meant to protect anything
  and are fine to leave as-is for local/demo use, but don't reuse them for
  a real database.

## Backup and restore

Everything HolyPipe needs to remember — sources, destinations,
connections, run history, logs — lives in one place: the
`holypipe_data` Docker volume (specifically the SQLite file at
`HOLYPIPE_METADATA_URL`, default `/data/holypipe.db` inside the
container). It does **not** contain any of the data being synced — only
configuration and history.

```bash
# Back up
docker run --rm -v holyprice-data-pipeline_holypipe_data:/data -v "$PWD":/backup \
  alpine tar czf /backup/holypipe-backup.tar.gz -C /data .

# Restore into a fresh volume
docker run --rm -v holyprice-data-pipeline_holypipe_data:/data -v "$PWD":/backup \
  alpine tar xzf /backup/holypipe-backup.tar.gz -C /data
```

(Adjust the volume name to whatever `docker volume ls` shows for your
deployment — Compose prefixes it with the project/directory name.)

There is no formal schema-migration system — HolyPipe creates any missing
tables on startup but never alters or drops existing columns. This has
been fine across this project's development so far; if a future update
needs a real column change, check the release notes before upgrading a
production instance.

## Troubleshooting

- **Docker Desktop returns `500 Internal Server Error` / `_ping` fails
  on every command** — this is a Docker Desktop engine fault, not a
  HolyPipe issue. Restart Docker Desktop (tray icon → Restart); if that
  doesn't clear it, run `wsl --shutdown` in an elevated PowerShell
  (Windows/WSL2 backend) and relaunch Docker Desktop.
- **A connection is stuck on "running" and every run/resync returns
  `409 Conflict`** — this shouldn't happen anymore (see
  [Stability and recovery](#stability-and-recovery)); if you're on an
  older build without that fix, restart the HolyPipe container to clear
  it, or upgrade.
- **CDC connection loops with a `wal_level`/`binlog` error** — see
  [Enabling CDC on your own databases](#enabling-cdc-on-your-own-databases)
  above; it will keep retrying with backoff rather than failing
  permanently, so fixing the source config and waiting (or clicking
  Start again) is enough — no need to recreate the connection.
- **Port already in use** — change the host side of the `ports:` mapping
  for whichever service collides (commonly `8090` for the app, or `5433`
  /`3307`/`5434` for the demo databases) in `docker-compose.yml`.
