# OpAMP Server

![Warning: Entirely vibecoded](https://img.shields.io/badge/Warning-Entirely%20vibecoded-orange?style=for-the-badge)

OpenTelemetry OpAMP server in Python with FastAPI + Streamlit UI. Optional integration with [Open Policy Agent](https://www.openpolicyagent.org).

This server also lets you:

- Filter collectors by metadata (such as `environment: production`)
- Build minimal OTel Collectors (server generates a new `manifest.yaml` which you build with the [OpenTelemetry Collector Builder [OCB]](https://github.com/open-telemetry/opentelemetry-collector/tree/main/cmd/builder))
- Validate connected collectors against OPA compliance policies
- Be driven by **AI agents** — a REST API with a self-documenting OpenAPI schema, an `opampctl` CLI with JSON output, a shared `opamp_client` Python library, and an MCP server with typed tools

Want to learn more about OpAMP? [Read the spec](https://opentelemetry.io/docs/specs/opamp/).

## Architecture

```
┌─────────────────────────┐        ┌─────────────────┐        ┌──────────────┐
│  OTel Collector         │───────▶│  OpAMP Server   │────────│  UI (:8501)  │
│  (OpAMP Extension)      │        │  (:4320)        │        └──────────────┘
└─────────────────────────┘        └─────────────────┘        ┌──────────────┐
                                           │                  │ opampctl CLI │
                                  ┌────────┴──────────────┐   │ MCP server   │
                                  │   Open Policy Agent   │   └──────────────┘
                                  │  (:8181)              │
                                  └───────────────────────┘
```

## Quick Start

### Docker Compose (Recommended)

Pre-built images are available from GitHub Container Registry:
- `ghcr.io/agardnerit/opamp-server-py-server:latest`
- `ghcr.io/agardnerit/opamp-server-py-ui:latest`

```bash
# Start server and UI
docker-compose up -d

# With OPA for compliance checking
docker-compose --profile opa up -d

# View logs
docker-compose logs -f
```

Access:
- UI: http://localhost:8501
- Server: http://localhost:4320
- OPA (if enabled): http://localhost:8181

There is no default admin password. On first start the server generates a random
one, prints it once in the logs (`docker-compose logs server | grep -i "admin
password"`), and stores it in `./data/admin_password` (mode 0600). Set
`ADMIN_PASSWORD` in `.env` to choose your own. Both ports bind to `127.0.0.1` on
purpose — see [Before you expose this server](#before-you-expose-this-server).

### Manual Setup

#### Server
```bash
python -m venv ./venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --port 4320
```

#### UI
```bash
pip install -r requirements-ui.txt
streamlit run ui/app.py
```

#### CLI + MCP server (for humans and AI agents)

```bash
pip install -e .            # installs opampctl + opamp-mcp entry points
opampctl health             # → {"status": "healthy", ...}
```

For MCP tools (used by Claude Desktop, Claude Code, and other MCP clients):

```bash
pip install -e ".[mcp]"     # adds the mcp dependency
```

### Connect Agent

Modify collector YAML by adding the `opamp` extension:

```yaml
extensions:
  opamp:
    server:
      http:
        endpoint: http://127.0.0.1:4320/v1/opamp
```

With `OPAMP_AGENT_AUTH=token` (see [Agent authentication](#agent-authentication-agent--server))
the same block also carries the agent's credential:

```yaml
extensions:
  opamp:
    server:
      http:
        endpoint: http://127.0.0.1:4320/v1/opamp
        headers:
          Authorization: "Bearer ${env:OPAMP_AGENT_TOKEN}"
```

### Transports

Plain HTTP is the default and is what the collector's `opamp` extension uses
(`endpoint: http://host:4320/v1/opamp`). Set `OPAMP_WS_ENABLED=true` to accept a
WebSocket upgrade on the same path. Frames are a base-128 varint header (must be
`0`) followed by the protobuf message; messages over `OPAMP_MAX_MESSAGE_BYTES`
close the socket with `1009`. Both transports run the same message handler, so
agent state is identical either way.

WebSocket upgrades are not covered by `CORSMiddleware`, so the handshake checks
`Origin` itself: agents (which send no `Origin`) always pass, and a browser origin
must be listed in `CORS_ORIGINS` unless that is `*`. If you launch uvicorn
directly, set `--ws-max-size` to at least `OPAMP_MAX_MESSAGE_BYTES` or uvicorn's
16 MiB default will close frames before the app limit applies.

Both transports accept credential headers, which is what makes per-agent
authentication possible without a wire extension: `extension/opampextension`
supports `server.http.headers` **and** `server.ws.headers` (verified against
v0.140.0 — the values are `configopaque`, so the agent's own effective-config
report redacts them), and `opamp-go` sends them on every HTTP request and on the
WebSocket upgrade. The supervisor supports the same `headers` block.

## Agent authentication (agent → server)

`OPAMP_AGENT_AUTH` decides whether the OpAMP endpoint believes the `instance_uid`
in the message body:

| Mode | Behaviour |
|---|---|
| `off` (default) | Unauthenticated: any client that can reach the port can claim any agent identity. Logs a startup warning |
| `token` | Each agent presents `Authorization: Bearer <token>`; the credential **is** the identity and must match the claimed `instance_uid` |
| `mtls` | Reported by the posture but **not enforced by this build** — certificates are terminated by your proxy, so the server would only be trusting a forgeable header. Use `token`; mTLS arrives with the CA work in #83 |

With `token`:

- a missing, unknown or revoked token gets **401** (HTTP) or **1008** (WebSocket)
  with **no** `ServerToAgent` body — an unauthenticated client learns nothing,
  not even which capabilities exist;
- a valid token claiming a *different* agent's `instance_uid` gets **403** / 1008,
  rejected before any state is applied, so the real agent's record is untouched;
- every rejection is counted in `opamp_agent_auth_failures_total{reason}` —
  `missing`, `malformed`, `unknown`, `revoked`, `uid_mismatch`,
  `binding_conflict` — and written through the audit seam (#97 replaces it with
  the append-only log);
- a live WebSocket binding may only move to the **same** principal, so a second
  connection cannot take over an agent (a legitimate reconnect moves cleanly).

Tokens are `opamp_agent_…`, stored as SHA-256 only, and returned exactly once at
creation. They never appear in `/agents`, logs, audit entries or error bodies.
`GET /agents` and `GET /agent/{id}` report each agent's credential state as
`{"enrolled", "active", "last_seen_at", "revoked_at"}` so you can watch a cutover.

### Cutover runbook (`off` → `token`)

```bash
# 1. While the server is open, let the fleet connect and note each agent id
opampctl agents list --pretty | jq '.agents[] | {id, name: .description}'

# 2. Mint a token per agent — the response carries a paste-ready snippet
opampctl --password "$ADMIN_PASSWORD" agents token create <agent-id> --label prod-1

# 3. Put the headers block in that agent's config and reload the collector
#      extensions.opamp.server.http.headers:
#        Authorization: "Bearer opamp_agent_..."
#    (server.ws.headers instead, if that agent speaks WebSocket)

# 4. Confirm each agent authenticates before closing the door
opampctl agents get <agent-id> | jq .credential     # last_seen_at must move

# 5. Flip the server and restart it
OPAMP_AGENT_AUTH=token uvicorn server.main:app --port 4320

# 6. Watch for stragglers
curl -s localhost:4320/metrics | grep opamp_agent_auth_failures_total
```

Rotation needs no downtime: mint a new token, deploy it, and the old one retires
on the *first successful use* of the new one (`--retire-previous` cuts over
immediately instead). Revocation (`opampctl agents token revoke <id>`) blocks
delivery but **keeps** the agent's record, state and desired config — revocation
is not a wipe, deliberately: you need that config to restore the agent later.

An agent with a revoked or broken credential retries forever with backoff, because
opamp-go cannot tell 401 from 403 — watch the failure metric, not the agent log.

Why the token lives in the config file instead of being handed down the wire:
`extension/opampextension` registers no `OnOpampConnectionSettings` callback, so
an in-band "here is your token" reply is ignored. The supervisor can consume
connection settings; the collector extension cannot. Registration-on-first-use
and CSR/TOFU stay in #83.

## Before you expose this server

This server ships **read-only by default** and is meant to be reachable by
trusted agents on a trusted network until you finish the checklist below.
Plain HTTP + no admin credential + agents that claim any `instance_uid` + a port
published to the world is a fleet takeover, so the defaults are now closed:

| Thing | Default | What that means |
|---|---|---|
| `ADMIN_PASSWORD` | *none* | Read-only mode: no privileged action is authenticated. Docker generates a random password on first start and stores it in `data/admin_password`; nothing ships a placeholder |
| Config push | **off** | Push routes return 404 (not 403 — a stranger does not get to learn the surface exists), the server does not advertise `OffersRemoteConfig`, and no `remote_config` field is ever sent |
| Agent auth | off | Any client can claim any agent identity. Set `OPAMP_AGENT_AUTH=token` and give each agent a credential — see [Agent authentication](#agent-authentication-agent--server) |
| TLS | off | Credentials and configs travel in cleartext |
| Ports in `docker-compose.yml` | `127.0.0.1` | Reachable from the host only, not from the network |

**To expose it safely**, do all five:

1. **Put TLS in front.** Use `docker/nginx.conf` or `docker/Caddyfile`, then set
   `PUBLIC_URL=https://opamp.example.com` (or `TLS_TERMINATED_UPSTREAM=true`).
   Keep port 4320 bound to loopback so nothing can bypass the proxy, forward
   `Upgrade`/`Connection` and a long read timeout for the WebSocket transport,
   and leave `Origin` intact — `CORSMiddleware` does not apply to upgrades, so
   the server's own check is the only one.
2. **Set an admin password** (`ADMIN_PASSWORD` or a mounted `ADMIN_PASSWORD_FILE`).
   Don't leave the generated one on a shared volume if several people need access.
3. **Authenticate your agents** (`OPAMP_AGENT_AUTH=token`) and mint a credential
   per agent. The token travels in the agent's config, so TLS is not optional
   once you turn this on.
4. **Set `CORS_ORIGINS`** to the origin the UI is served from instead of `*`.
5. **Keep config push off** until the posture check passes for you. Turning it on
   is refused at startup unless admin auth, agent auth and TLS are all in place:

```bash
OPAMP_CONFIG_PUSH_ENABLED=true uvicorn server.main:app --port 4320
# refusing to start: OPAMP_CONFIG_PUSH_ENABLED=true but the server posture is insecure:
#   - admin auth is not configured: ...
#   - agents are not authenticated: ...
#   - the server is not served over TLS: ...
```

The only way past that is `OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true`, which is for
local development: it logs a red warning, reports `insecure: true` in `/health`,
and puts a permanent banner on every UI page. It is deliberately unpleasant.

Check your posture at any time:

```bash
curl -s localhost:4320/health | jq .config_push
# {"enabled": false, "insecure": false, "agent_auth": "off", "audit": false, "tls": false}
opampctl --password "$ADMIN_PASSWORD" health   # same, plus config_push
curl -s -u ":$ADMIN_PASSWORD" localhost:4320/config/posture | jq .requirements
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_HTTP_SCHEME` | `http` | HTTP scheme |
| `SERVER_ADDRESS` | `localhost` | Server bind address |
| `SERVER_PORT` | `4320` | Server port |
| `AGENT_TIMEOUT_SECONDS` | `60` | Seconds before stale agent removed |
| `DATA_DIR` | `data` | SQLite database directory |
| `ADMIN_PASSWORD` | *(empty)* | Admin password for privileged endpoints (see [API](#api-reference)). Empty = read-only mode; set `ADMIN_PASSWORD_FILE` or `OPAMP_ADMIN_AUTOGENERATE_PASSWORD` instead if you prefer |
| `ADMIN_PASSWORD_FILE` | *(empty)* | Read the admin password from a file (secret mount) instead of the environment |
| `OPAMP_ADMIN_AUTOGENERATE_PASSWORD` | `false` | Generate a random admin password on first start, print it once, store it in `$DATA_DIR/admin_password` (0600). `true` in `docker-compose.yml` |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins (browser clients) |
| `OPAMP_WS_ENABLED` | `false` | Enable the WebSocket transport on `/v1/opamp` (see [Transports](#transports)) |
| `OPAMP_MAX_MESSAGE_BYTES` | `67108864` | Max OpAMP message size (64 MiB), varint header included |
| `OPAMP_WS_OUTBOUND_QUEUE` | `32` | Per-connection WebSocket send queue depth; oldest is dropped when full |
| `OPAMP_CONFIG_PUSH_ENABLED` | `false` | Remote config push master switch. Off = push routes 404, no `remote_config` is ever sent. On = the server refuses to start unless the posture below is secure |
| `OPAMP_AGENT_AUTH` | `off` | Agent authentication on `/v1/opamp`: `off` \| `token` \| `mtls`. `token` requires `Authorization: Bearer <token>` per agent (see [Agent authentication](#agent-authentication-agent--server)); `mtls` is reported but not enforced by this build. `off` logs a startup warning, and config push refuses to start without `token` |
| `PUBLIC_URL` | *(empty)* | Public URL agents/browsers use. `https://` or `wss://` counts as TLS for the posture check |
| `TLS_TERMINATED_UPSTREAM` | `false` | Assert that a TLS terminator you control sits in front of the server (see `docker/`). Assertion only — keep the server on loopback |
| `OPAMP_AUDIT_ENABLED` | `false` | Reports audit-log availability for privileged actions (#97); always `false` today |
| `OPAMP_ALLOW_INSECURE_CONFIG_PUSH` | `false` | **Local development only.** Starts with config push enabled on an insecure posture: red warning, `insecure: true` in `/health`, permanent UI banner. See [Before you expose this server](#before-you-expose-this-server) |

## Alerts (Webhook)

Configure via UI at `/alerts` endpoint. Webhook sends JSON with `event_type` and `message`.

| event_type | Trigger |
|-----------|--------|
| `new_agent` | New agent connects |
| `agent_disconnected` | Agent becomes stale |
| `compliance_violation` | OPA policy fails |

## OPA (Optional)

Run OPA server, then set `OPA_ENABLED=true`.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPA_ENABLED` | `false` | Enable OPA compliance |
| `OPA_URL` | `http://localhost:8181` | OPA server URL |
| `POLICIES_DIR` | `policies/tags` | Policies directory |

### Policies
```bash
docker run --rm -it -p 8181:8181 -v $(pwd)/policies:/policies \
  openpolicyagent/opa run --server --bundle /policies --watch --addr :8181
```

Add `package opamp.agent.compliance.<name>` policies to `policies/tags/`.

## API Reference

The server is FastAPI-based and self-documenting:

- **Interactive docs:** [`/docs`](http://localhost:4320/docs) (Swagger UI) and [`/redoc`](http://localhost:4320/redoc)
- **Machine-readable schema:** [`/openapi.json`](http://localhost:4320/openapi.json) — fetch this to call every endpoint programmatically

### Authentication

Auth is controlled by the admin credential (`ADMIN_PASSWORD`, `ADMIN_PASSWORD_FILE`, or
one generated at startup with `OPAMP_ADMIN_AUTOGENERATE_PASSWORD=true`):

- Agents are authenticated **separately**, on `POST /v1/opamp` only, with
  `Authorization: Bearer <token>` when `OPAMP_AGENT_AUTH=token` — see
  [Agent authentication](#agent-authentication-agent--server). Admin endpoints
  take HTTP Basic, so the two never collide.

- **No credential (read-only mode):** endpoints marked 🔒 behave as they always have —
  open, because there is nothing to authenticate against. Closing that door is #95.
  Docker deployments generate a random credential, so read-only mode is mostly a
  local-development state.
- **Credential present:** endpoints marked 🔒 below require HTTP Basic auth with any
  username and the admin password as the password:

```bash
# any username, the admin password as the password
curl -u admin:"$ADMIN_PASSWORD" http://localhost:4320/alerts
# or explicitly:
curl -H "Authorization: Basic $(printf ":$ADMIN_PASSWORD" | base64)" http://localhost:4320/alerts
```

Check the auth mode with `GET /auth/status`
(`{"password_required": bool, "auth_mode": "password"|"disabled", "roles": [...]}`) and
validate credentials with `GET /auth/verify`.

### Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|:----:|-------------|
| GET | `/auth/status` | | Auth mode: `password_required`, `auth_mode` (`password`\|`disabled`), `roles`. No secrets |
| GET | `/auth/verify` | 🔒 | Validate Basic credentials (200 or 401) |
| POST | `/v1/opamp` | 🔑 | OpAMP protocol endpoint — agents connect here (protobuf). With `OPAMP_AGENT_AUTH=token` the agent must send `Authorization: Bearer <token>`: 401 without a valid one, 403 when the token is for another `instance_uid` |
| WS | `/v1/opamp` | 🔑 | Same endpoint over WebSocket when `OPAMP_WS_ENABLED=true`; credential checked before `accept()`, refusals close `1008` |
| POST | `/v1/metrics` | | OTLP/HTTP metrics ingestion from collectors (JSON or protobuf) |
| GET | `/agents` | | List agents. Filters: `healthy=true\|false\|unknown`, `status=` (remote config status, or alias `remote_config_status=`), or any description attribute, e.g. `?environment=prod`. Repeated params are OR. Each agent carries a `credential` block |
| GET | `/agent/{id}` | | Agent details incl. latest OTLP metrics and `credential` state (`enrolled`, `active`, `last_seen_at`, `revoked_at` — never a token) |
| GET | `/agent/{id}/metrics` | | Latest OTLP metric values for the agent (persisted across restarts) |
| POST | `/agents/{id}/token` | 🔒 | Mint/rotate an agent credential. Returns the token **once** plus a paste-ready collector snippet. Optional body `{"label", "retire_previous"}`. 422 if `id` is not a hex instance UID |
| DELETE | `/agents/{id}/token` | 🔒 | Revoke every live credential for the agent (idempotent). Blocks delivery, keeps the agent's state |
| POST | `/agent/{id}/manifest` | | Generate OCB `manifest.yaml` + build command for a slim collector. Optional body `{"version": "0.123.0"}`. 409 if the agent has no components in use |
| GET | `/agent/{id}/compliance` | | Evaluate agent against OPA policies (no-op result if OPA disabled) |
| GET | `/agent/{id}/report` | | Markdown report for one agent (versions, health, components) |
| GET | `/reports/fleet` | | Full fleet summary report (markdown, same as the UI Reports page) |
| GET | `/reports/heavy-collectors` | | Collectors with many unused components. Param: `threshold` (0–1, default 0.5) |
| GET | `/reports/outdated-collectors` | | Collectors with components older than `version` (default `0.149.0`) |
| POST | `/compliance/check/{id}` | 🔒 | Force a compliance check (503 if OPA disabled) |
| GET | `/compliance/summary` | | Fleet-wide compliance counts |
| GET | `/compliance/policies` | | Available OPA policies |
| POST | `/compliance/reload` | 🔒 | Ask OPA to reload policies from disk |
| GET | `/compliance/validate` | | Validate OPA policy files |
| GET | `/alerts` | 🔒 | Current alert configuration |
| PUT | `/alerts` | 🔒 | Update alert configuration |
| POST | `/alerts/test` | 🔒 | Send a test alert (optionally with a temporary `event_config`) |
| GET | `/health` | | Health check (server status, agent count, OPA availability) plus the public `config_push` posture block |
| GET | `/config/posture` | 🔒 | Full config push posture: every requirement, what is met, where the admin credential came from, whether the override is on. No secrets |
| GET | `/metrics` | | Prometheus metrics |

Example: filter agents by metadata and health

```bash
curl "http://localhost:4320/agents?healthy=true&environment=prod"
curl "http://localhost:4320/agents?status=UNSET"
```

## CLI (`opampctl`)

Every documented API call has a CLI equivalent. **Output is JSON by default**;
use `--raw` to print just the markdown/YAML payload. Install with `pip install -e .`

```bash
# First three commands to try (all safe, read-only):
opampctl health
opampctl agents list
opampctl reports fleet --raw
opampctl config posture                     # config push posture + why each requirement fails

# Agents
opampctl agents list --healthy true --attr environment=prod
opampctl agents get <agent-id>              # full details incl. metrics
opampctl agents metrics <agent-id>          # latest OTLP metric values
opampctl agents manifest <agent-id> --raw   # OCB manifest.yaml for a slim build
opampctl agents manifest <agent-id> --version 1.2.3
opampctl agents compliance <agent-id>       # OPA evaluation (no-op if OPA disabled)

# Agent credentials (admin; OPAMP_AGENT_AUTH=token)
opampctl agents token create <agent-id> --label prod-1   # token (once) + collector snippet
opampctl agents token create <agent-id> --raw            # just the token, for $(...)
opampctl agents token create <agent-id> --retire-previous  # rotate with an immediate cutover
opampctl agents token revoke <agent-id>                  # blocks delivery, keeps agent state

# Reports
opampctl reports fleet --raw
opampctl reports heavy --threshold 0.8 --raw
opampctl reports outdated --version 0.100.0 --raw

# Compliance
opampctl compliance summary
opampctl compliance policies
opampctl compliance validate
opampctl compliance reload                  # admin
opampctl compliance check <agent-id>        # admin

# Alerts (admin)
opampctl alerts get
opampctl alerts set @alerts.json            # JSON from file (or inline JSON)
opampctl alerts test --event-type new_agent
```

### CLI configuration

| Source | Flags | Env vars |
|--------|-------|----------|
| Server URL | `--server`, `-s` | `OPAMP_SERVER_URL` |
| Admin password | `--password`, `-p` | `ADMIN_PASSWORD` |

### Offline mode (`--db`)

Read the server's SQLite state file directly — no server needed. Supports the
read-only surface (agents list/get/metrics, manifests, all reports):

```bash
opampctl --db data/opamp.db agents list
opampctl --db data/opamp.db reports fleet --raw
opampctl --db data/opamp.db agents manifest <agent-id> --raw
```

Live-only commands (`health`, `alerts`, `compliance`) exit with a structured
error under `--db`.

### Shell completion

`opampctl` is built with [Typer](https://typer.tiangolo.com/) and ships with
shell completion:

```bash
opampctl --install-completion   # install for the current shell
opampctl --show-completion      # show the install command/instructions
```

## MCP Server (for AI agents)

An [MCP](https://modelcontextprotocol.io) server (`opamp-mcp` / `python -m mcp_server`)
exposes 20 typed tools mirroring the API: `list_agents`, `get_agent`,
`get_agent_metrics`, `generate_manifest`, `fleet_report`, `heavy_collectors_report`,
`outdated_collectors_report`, `get_compliance`, `check_compliance`,
`compliance_summary`, `list_policies`, `validate_policies`, `reload_policies`,
`get_alerts`, `update_alerts`, `test_alerts`, `agent_report`, `health`, `auth_status`,
`config_posture`.

**Write tools are absent, not failing.** `create_agent_token` and
`revoke_agent_token` only exist when the MCP process is started with
`OPAMP_MCP_ALLOW_WRITES=true` (#94 introduced this gate for credential minting;
#106 applies it to every write tool). An agent that cannot see a tool will not
try to use it.

Transport is **stdio** by default (works with every local MCP client); use
`--transport sse --port 8765` for remote agents. Configure with the same env vars
as the CLI (`OPAMP_SERVER_URL`, `ADMIN_PASSWORD`), plus
`OPAMP_MCP_ALLOW_WRITES` if you really want credential minting exposed to an AI client.

### Claude Desktop / generic MCP client config

```json
{
  "mcpServers": {
    "opamp-server": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "OPAMP_SERVER_URL": "http://localhost:4320",
        "ADMIN_PASSWORD": "changeme"
      }
    }
  }
}
```

The `command` must point at a Python where the package is installed
(`pip install -e ".[mcp]"`).

### Claude Code

```bash
claude mcp add opamp-server \
  -e OPAMP_SERVER_URL=http://localhost:4320 \
  -e ADMIN_PASSWORD=changeme \
  -- python -m mcp_server
```

## Python client library (`opamp_client`)

The CLI and MCP server both use one shared HTTP client — use it directly when
embedding OpAMP control into your own tooling:

```python
from client import OpampClient, OpampApiError

# Reads OPAMP_SERVER_URL / ADMIN_PASSWORD from the environment by default.
oc = OpampClient(base_url="http://localhost:4320", password="changeme")

agents = oc.list_agents(healthy="true", environment="prod")   # metadata filters
result = oc.generate_manifest(agents["agents"][0]["id"])
print(result["manifest_yaml"])

# Agent credentials (admin) — the token is returned once
token = oc.create_agent_token("4f3c2b1a...", label="prod-1")["token"]
print(oc.get_agent("4f3c2b1a...")["credential"])
print(oc.revoke_agent_token("4f3c2b1a..."))   # {"revoked": 1}

try:
    oc.get_agent("nope")
except OpampApiError as e:
    print(e.status_code, e.detail)     # 404 Agent not found
```

## Development

```bash
pip install -r requirements.txt
pip install -r requirements-ui.txt
pytest tests/ -v
```

## Project Structure

```
opamp-server-py/
├── server/          # FastAPI server
├── client/          # opamp_client — shared HTTP client library
├── cli/             # opampctl CLI (typer, JSON output)
├── mcp_server/      # FastMCP server (stdio/SSE) for AI agents
├── ui/              # Streamlit dashboard
├── proto/           # Protobuf definitions
├── tests/           # Tests
├── collector/       # Sample configs
├── docker/          # TLS terminator examples (nginx, Caddy) for #96
└── data/            # SQLite DB (+ generated admin_password)
```