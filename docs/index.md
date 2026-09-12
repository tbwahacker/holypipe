---
title: HolyPipe — ELT & CDC replication for PostgreSQL, MySQL, and SQLite
description: >-
  HolyPipe is a scoped, self-hosted ELT and change-data-capture (CDC)
  replication tool for PostgreSQL, MySQL, and SQLite. Real-time sync, batch
  sync, a web dashboard, and a built-in MCP server for AI agents like Claude,
  Copilot, and Codex.
---

# HolyPipe

**A small, purpose-built ELT / data-replication tool** — the parts of
Airbyte you actually need when you only care about **PostgreSQL, MySQL, and
SQLite** as sources and destinations. Runs entirely in Docker on Windows,
macOS, or Linux; syncs in real time via change-data-capture (CDC); and ships
with a live web dashboard and a built-in [MCP server](MCP_INTEGRATION.md) so
AI agents can operate it directly.

![HolyPipe architecture diagram](https://raw.githubusercontent.com/tbwahacker/holypipe/main/screenshots/architecture.png)

## Why HolyPipe

- **Real-time CDC** — Postgres logical replication, MySQL binlog streaming,
  or a SQLite trigger-based change journal, each starting with a one-time
  initial snapshot so you get every existing row *and* every future change.
- **Batch sync** — full refresh (loaded via an atomic shadow-table swap, so
  a dashboard querying the destination never sees a half-loaded table),
  incremental cursor columns, or Postgres's built-in `xmin` for
  change-detection without picking a cursor at all.
- **Any combination** of the three engines as source and destination —
  Postgres → MySQL, MySQL → SQLite, Postgres → Postgres warehouse, and so on.
- **A live web dashboard** for creating connectors, watching syncs, and
  managing users/roles — no separate admin tool.
- **A built-in MCP server** — point Claude, Copilot, Codex, or any other
  MCP-capable agent at `/mcp` with a personal access token and let it manage
  sources, connections, and syncs on your behalf, scoped to that token's
  own permissions.
- **One container, no extra infrastructure** — no Java, no Temporal, no
  message broker, no connector marketplace.

## Get started

- **New to HolyPipe?** Start with the [User Guide](USER_GUIDE.md) — core
  concepts, adding a source/destination, and creating your first connection.
- **Deploying it?** The [Administrator Guide](ADMIN_GUIDE.md) covers
  installation (Docker, pip, or building from source), configuration,
  enabling CDC on your own databases, backups, and security notes.
- **Hooking up an AI agent?** The [MCP Integration Guide](MCP_INTEGRATION.md)
  covers creating an API token and connecting Claude Code, Claude Desktop,
  or any other remote-HTTP MCP client.
- **Contributing or extending a connector?** The
  [Developer Guide](DEVELOPER_GUIDE.md) covers the codebase layout and the
  canonical-type system connectors are built on.

## Install

```bash
# Docker (recommended)
docker run -p 8090:8000 -v holypipe_data:/data ghcr.io/tbwahacker/holypipe:latest

# or via pip
pip install holypipe
holypipe
```

Then open `http://localhost:8090` — default login `admin` / `admin`, with a
forced password change on first sign-in.

Full instructions, including the bundled demo Docker Compose stack, are in
the [Administrator Guide](ADMIN_GUIDE.md#deployment).

---

Source code, issues, and releases: [github.com/tbwahacker/holypipe](https://github.com/tbwahacker/holypipe).
Licensed under the [MIT License](https://github.com/tbwahacker/holypipe/blob/main/LICENSE).
