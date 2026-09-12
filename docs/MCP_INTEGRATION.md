# HolyPipe — MCP Integration Guide

HolyPipe ships a built-in [MCP](https://modelcontextprotocol.io) server, so
AI agents — Claude, GitHub Copilot, Codex, Cursor, Grok, or anything else
that speaks MCP — can create sources, wire up connections, kick off syncs,
and read back logs and run history on your behalf, using the same
permissions model as the web dashboard. This guide covers getting a token
and pointing an agent at it. If you haven't set up HolyPipe itself yet, see
the [Administrator Guide](ADMIN_GUIDE.md) first; for using the dashboard
directly, see the [User Guide](USER_GUIDE.md).

## How it works

- The MCP server is mounted at `/mcp` on the same HolyPipe instance you
  already run — nothing extra to deploy.
- It uses the **Streamable HTTP** transport, so any agent that supports a
  remote MCP server over HTTP can connect; no local process or stdio setup
  needed.
- Every call authenticates with a **personal access token**, not your
  login/password. A token carries the permissions of whichever user
  created it — the same roles/permissions the dashboard's Users & Roles
  panel manages. Give an agent a token from a read-only role if you want it
  to look but not touch.
- Tool calls run the exact same code path as the dashboard's buttons and
  the REST API — there's no separate, looser "agent mode."

## 1. Create an API token

**From the dashboard:** click your username in the top bar → **API
Tokens** → **+ New Token** → give it a name (e.g. "Claude Code") and
optionally an expiry → copy the token shown. It is only ever displayed
once — if you lose it, revoke it and create a new one.

**From the API**, if you'd rather script it:

```bash
curl -s -X POST http://localhost:8090/api/tokens \
  -H "Content-Type: application/json" \
  --cookie "hp_session=<your logged-in session cookie>" \
  -d '{"name": "Claude Code", "expires_in_days": 90}'
```

Either way you get back something like:

```json
{"id": "...", "name": "Claude Code", "token": "hp_pat_AbCdEf...", "expires_at": null}
```

Save the `token` value — that's what goes into an agent's config below.
Revoke a token any time from the same **API Tokens** panel, or
`DELETE /api/tokens/{id}`.

## 2. Point an agent at it

The MCP endpoint is `http://<your-holypipe-host>:8090/mcp/` (note the
trailing slash — a bare `/mcp` 307-redirects to it, which most clients
follow automatically, but some don't). Every client needs the same two
things: that URL, and an `Authorization: Bearer <token>` header.

### Claude Code

```bash
claude mcp add --transport http holypipe http://localhost:8090/mcp/ \
  --header "Authorization: Bearer hp_pat_AbCdEf..."
```

Scope it to a project with `--scope project`, or drop `--scope` for a
personal (user-level) server available in every session.

### Claude Desktop / other JSON-config MCP clients

Add to the client's MCP server config (for Claude Desktop:
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "holypipe": {
      "url": "http://localhost:8090/mcp/",
      "headers": {
        "Authorization": "Bearer hp_pat_AbCdEf..."
      }
    }
  }
}
```

Some older clients only support stdio servers and don't yet have a "remote
HTTP server with headers" option — for those, use a local stdio-to-HTTP
bridge such as [`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "holypipe": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8090/mcp/",
               "--header", "Authorization:Bearer hp_pat_AbCdEf..."]
    }
  }
}
```

### Codex CLI, Copilot, Cursor, Grok, or anything else

Any MCP client that supports a remote Streamable HTTP server with custom
headers works the same way: give it the URL above and the `Authorization`
header. Consult that tool's own docs for exactly where the URL/header
fields live — the values themselves are identical to the examples above.

If you're deploying HolyPipe somewhere other than `localhost` (a shared
server, a cloud VM), put it behind a reverse proxy with TLS before handing
out tokens to agents — like the dashboard's session cookie, the bearer
token is a real credential and HolyPipe doesn't encrypt the connection
itself.

## 3. What an agent can do

Once connected, ask the agent in plain language — "discover the tables in
my orders Postgres source and set up a connection to the warehouse
syncing the `orders` and `customers` tables every 5 minutes" is a
reasonable single request. Under the hood it's calling these tools:

| Tool | Does |
|---|---|
| `list_connector_types` | Lists supported source/destination types and their config fields |
| `list_sources` / `create_source` / `update_source` / `delete_source` | Manage sources |
| `test_source` / `test_source_config` | Test connectivity (saved or not-yet-saved) |
| `discover_source` | List a source's tables/columns/primary keys |
| `list_destinations` / `create_destination` / `update_destination` / `delete_destination` | Manage destinations |
| `test_destination` / `test_destination_config` | Test connectivity |
| `list_connections` / `get_connection` / `create_connection` / `update_connection` / `delete_connection` | Manage connections and their stream config |
| `run_sync` | Run a batch sync now and wait for the result |
| `resync_connection` | Force a full reload (drops cursors / redoes the CDC snapshot) |
| `start_cdc` / `stop_cdc` | Control real-time streaming |
| `list_runs` | Sync history for a connection |
| `get_logs` | Recent log lines, optionally scoped to a connection |

`run_sync` and `resync_connection` block until the sync finishes, so the
agent gets back real rows-read/written numbers instead of a vague "started"
— worth knowing if you ask it to reload a very large table, since the
reply will take as long as the sync does.

## 4. Troubleshooting

- **"Missing Authorization: Bearer \<token\> header"** — the client isn't
  sending the header at all; check its config syntax.
- **"Invalid, expired, or revoked API token"** — the token was mistyped,
  its `expires_in_days` window passed, or it was revoked from the API
  Tokens panel.
- **"Missing permission: X"** — the token's user doesn't hold a role that
  grants that permission. Check/assign roles in Users & Roles.
- A tool call for a large sync appears to hang — it's working;
  `run_sync`/`resync_connection` are intentionally synchronous (see above).
  Use `list_runs` from a second call if your client supports concurrent
  tool calls and you don't want to wait.
