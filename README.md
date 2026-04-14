# OpAMP Server

[![Vibe Coded with OpenCode](https://img.shields.io/badge/Vibe%20Coded-OpenCode-7C3AED?style=flat-square)](https://opencode.ai)

> This project was vibe coded with [OpenCode](https://opencode.ai)

OpenTelemetry OpAMP server in Python with FastAPI.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    OpenTelemetry Collector                        │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌──────────────────┐   │
│  │Receiver │→ │Processor│→ │Exporter │  │   OpAMP Extension│───┼──┐
│  └─────────┘  └─────────┘  └─────────┘  └──────────────────┘   │  │
│                                                              │  │
│                                                              │  │
│  ┌──────────────────────────────────────────────────────┐     │  │
│  │              Available Components Report               │     │  │
│  │  Receivers: otlp, prometheus, jaeger...              │←────┘  │
│  │  Processors: batch, memory_limiter, filter...       │        │
│  │  Exporters: otlp, prometheus, debug...               │        │
│  └──────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP/POST (OpAMP Protocol)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      OpAMP Server (:4320)                       │
│  ┌─────────────┐  ┌─────────────────┐  ┌────────────────────┐  │
│  │ Agent Reg.  │  │   SQLite DB     │  │  Prometheus Metrics│  │
│  │             │  │  (persistence)  │  │   (:4321/metrics)  │  │
│  └─────────────┘  └─────────────────┘  └────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ REST API
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Streamlit UI (:8501)                          │
│  ┌─────────────┐  ┌─────────────────┐  ┌────────────────────┐  │
│  │ Agent Fleet │  │ Agent Details    │  │  Slim Distro Builder│  │
│  │   Table     │  │ Components/Caps  │  │   OCB Manifest Gen │  │
│  └─────────────┘  └─────────────────┘  └────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Start the server
```bash
python -m venv ./venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --host 127.0.0.1 --port 4320 --timeout-keep-alive 300
```
Note: The `--timeout-keep-alive` flag controls how long idle HTTP connections are kept alive. A value of 300 seconds (5 minutes) is recommended to prevent premature disconnects when the collector adapts its heartbeat cadence. You can increase this for debugging if needed.

### 2. Start the UI (optional)
```bash
pip install -r requirements-ui.txt
streamlit run ui/app.py --server.port 8501
```

### 3. Connect an agent
Use the OpenTelemetry Collector with the OpAMP extension:

```yaml
extensions:
  opamp:
    server:
      http:
        endpoint: http://127.0.0.1:4320/v1/opamp
```

Sample collector configs can be found in `/collector`

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_HTTP_SCHEME` | `http` | HTTP scheme for server URLs |
| `SERVER_ADDRESS` | `localhost` | Server bind address |
| `SERVER_PORT` | `4320` | Server port |
| `AGENT_TIMEOUT_SECONDS` | `60` | Seconds before a stale agent is removed |
| `DATA_DIR` | `data` | Directory for SQLite database (persists agent state) |
| `OPA_ENABLED` | `false` | Enable OPA compliance checking |
| `OPA_URL` | `http://localhost:8181` | OPA server URL |
| `POLICIES_DIR` | `policies/tags` | Directory containing OPA policies |
| `AGENT_NAME` | `otelcol-contrib` | Agent identifier |
| `AGENT_ENVIRONMENT` | `development` | Deployment environment |
| `OPAMP_SERVER_URL` | `http://server:4320/v1/opamp` | Server endpoint |

## Compliance Policies (OPA)

The server supports OPA (Open Policy Agent) for compliance checking. Run OPA with the `--watch` flag to automatically reload policies when rego files change:

```bash
docker run --rm -it \
  -p 8181:8181 \
  -v $(pwd)/policies:/policies \
  openpolicyagent/opa:latest \
  run --server --addr=0.0.0.0:8181 --bundle /policies --watch
```

### Adding a New Policy

1. Create a new file in `policies/tags/require_MYPOLICY.rego`
2. Use the package name: `package opamp.agent.compliance.MYPOLICY`
3. Add violation checks:

```rego
package opamp.agent.compliance.my_policy

violations contains msg if {
    not some_condition
    msg := "Violation message here"
}
```

4. The policy is automatically picked up (no restart needed with `--watch`)

### Policy Input Format

Policies receive this input structure:

```json
{
  "agent_id": "...",
  "description": {
    "identifyingAttributes": [
      {"key": "service.version", "value": {"stringValue": "0.149.0"}}
    ],
    "nonIdentifyingAttributes": [
      {"key": "agent.name", "value": {"stringValue": "collector2"}},
      {"key": "environment", "value": {"stringValue": "production"}}
    ]
  }
}
```

## Server Endpoints

Base URL: `http://127.0.0.1:4320`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/opamp` | POST | OpAMP agent connection |
| `/metrics` | GET | Prometheus metrics |
| `/agents` | GET | List all agents |
| `/agent/{id}` | GET | Get agent details |
| `/health` | GET | Health check |

## Agent Fields

Each agent in `/agents` and `/agent/{id}` includes:

- `capability_tags`: A list of supported capabilities derived from the agent's `capabilities` bitmask. Each tag includes `label` (human-readable name) and `icon` (emoji).
- `components`: A dict of components grouped by type (Receiver, Processor, Exporter, Extension, Connector). Each component has `id`, `version`, and `used` (true if in an active pipeline).

## UI Filters

The dashboard provides filters for the agent fleet:

- **Show in-use agents only**: Filters to agents that have at least one component in an active pipeline.

In agent details, the **Hide unused** toggle collapses components that are available but not in use.

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Install UI dependencies
pip install -r requirements-ui.txt

# Run tests
pytest tests/ -v
```

## Project Structure

```
opampserver/
├── server/          # FastAPI server
│   ├── main.py     # Main app with OpAMP endpoint
│   └── state.py    # Agent registry
├── ui/             # Streamlit dashboard
├── proto/          # Protobuf definitions
├── tests/          # Unit tests
├── collector/      # Sample collector config
├── docker-compose.yml
├── Dockerfile.server
└── Dockerfile.ui
```
