# OpAMP Server

![Warning: Entirely vibecoded](https://img.shields.io/badge/Warning-Entirely%20vibecoded-orange?style=for-the-badge)

OpenTelemetry OpAMP server in Python with FastAPI + Streamlit UI. Optional integration with [Open Policy Agent](https://www.openpolicyagent.org).

This server also lets you:

- Filter collectors by metadata (such as `environment: production`)
- Build minimal OTel Collectors (server generates a new `manifest.yaml` which you build with the [OpenTelemetry Collector Builder [OCB]](https://github.com/open-telemetry/opentelemetry-collector/tree/main/cmd/builder))
- Validate connected collectors against OPA compliance policies

Want to learn more about OpAMP? [Read the spec](https://opentelemetry.io/docs/specs/opamp/).

## Architecture

```
┌─────────────────────────┐        ┌─────────────────┐        ┌──────────────┐
│  OTel Collector         │───────▶│  OpAMP Server   │────────│  UI (:8501)  │
│  (OpAMP Extension)      │        │  (:4320)        │        │              │
└─────────────────────────┘        └─────────────────┘        └──────────────┘
                                           │
                                  ┌────────┴──────────────┐
                                  │   Open Policy Agent   │ (optional)
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
| GET | `/agent/{id}/metrics` | | Latest OTLP metric values for the agent |
| POST | `/agent/{id}/manifest` | | Generate OCB `manifest.yaml` + build command for a slim collector. Optional body `{"version": "0.123.0"}`. 409 if the agent has no components in use |
| GET | `/agent/{id}/compliance` | | Evaluate agent against OPA policies (no-op result if OPA disabled) |
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
├── ui/              # Streamlit dashboard
├── proto/           # Protobuf definitions
├── tests/           # Tests
├── collector/       # Sample configs
└── data/           # SQLite DB
```