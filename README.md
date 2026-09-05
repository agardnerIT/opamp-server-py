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

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_HTTP_SCHEME` | `http` | HTTP scheme |
| `SERVER_ADDRESS` | `localhost` | Server bind address |
| `SERVER_PORT` | `4320` | Server port |
| `AGENT_TIMEOUT_SECONDS` | `60` | Seconds before stale agent removed |
| `DATA_DIR` | `data` | SQLite database directory |
| `ADMIN_PASSWORD` | *(empty)* | Enables HTTP Basic auth on admin endpoints (see [API](#api-reference)) |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins (browser clients) |

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

Auth is controlled by `ADMIN_PASSWORD`:

- **Empty/unset (default): auth is disabled** — all endpoints are open.
- **Set:** endpoints marked 🔒 below require HTTP Basic auth with any username and the admin password as the password:

```bash
# any username, ADMIN_PASSWORD as the password
curl -u admin:changeme http://localhost:4320/alerts
# or explicitly:
curl -H "Authorization: Basic $(printf ':changeme' | base64)" http://localhost:4320/alerts
```

Check the auth mode with `GET /auth/status` (`{"password_required": true|false}`) and validate credentials with `GET /auth/verify`.

### Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|:----:|-------------|
| GET | `/auth/status` | | Whether `ADMIN_PASSWORD` is set (auth required) |
| GET | `/auth/verify` | 🔒 | Validate Basic credentials (200 or 401) |
| POST | `/v1/opamp` | | OpAMP protocol endpoint — agents connect here (protobuf) |
| POST | `/v1/metrics` | | OTLP/HTTP metrics ingestion from collectors (JSON or protobuf) |
| GET | `/agents` | | List agents. Filters: `healthy=true\|false\|unknown`, `status=` (remote config status, or alias `remote_config_status=`), or any description attribute, e.g. `?environment=prod`. Repeated params are OR |
| GET | `/agent/{id}` | | Agent details incl. latest OTLP metrics |
| GET | `/agent/{id}/metrics` | | Latest OTLP metric values for the agent (persisted across restarts) |
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
| GET | `/health` | | Health check (server status, agent count, OPA availability) |
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

# Agents
opampctl agents list --healthy true --attr environment=prod
opampctl agents get <agent-id>              # full details incl. metrics
opampctl agents metrics <agent-id>          # latest OTLP metric values
opampctl agents manifest <agent-id> --raw   # OCB manifest.yaml for a slim build
opampctl agents manifest <agent-id> --version 1.2.3
opampctl agents compliance <agent-id>       # OPA evaluation (no-op if OPA disabled)

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
exposes 19 typed tools mirroring the API: `list_agents`, `get_agent`,
`get_agent_metrics`, `generate_manifest`, `fleet_report`, `heavy_collectors_report`,
`outdated_collectors_report`, `get_compliance`, `check_compliance`,
`compliance_summary`, `list_policies`, `validate_policies`, `reload_policies`,
`get_alerts`, `update_alerts`, `test_alerts`, `agent_report`, `health`, `auth_status`.

Transport is **stdio** by default (works with every local MCP client); use
`--transport sse --port 8765` for remote agents. Configure with the same env vars
as the CLI (`OPAMP_SERVER_URL`, `ADMIN_PASSWORD`).

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
└── data/            # SQLite DB
```