# OpAMP Server

![Warning: Entirely vibecoded](https://img.shields.io/badge/Warning-Entirely%20vibecoded-orange?style=for-the-badge)

OpenTelemetry OpAMP server in Python with FastAPI + Streamlit UI.

This server also lets you:

- Filter collectors by metadata (such as `environment: production`)
- Build minimal OTel Collectors
- Validate connected collectors against OPA compliance policies

## Architecture

```
┌─────────────────────────┐        ┌─────────────────┐        ┌──────────────┐
│  OTel Collector         │───────▶│  OpAMP Server   │────────│  UI (:8501)  │
│  (OpAMP Extension)      │        │  (:4320)        │        │              │
└─────────────────────────┘        └─────────────────┘        └──────────────┘
            │                      │   │
            │                 ┌────┴───┴────┐
            ▼                │   OPA      │ (optional)
┌──────────────────┐       │  (:8181)   │
│  Slim Collector    │       └────────────┘
│  (OBR manifest)  │
└──────────────────┘
```

## Quick Start

### Server
```bash
python -m venv ./venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --port 4320
```

### UI
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
| `SERVER_PORT` | `4320` | Server port |
| `AGENT_TIMEOUT_SECONDS` | `60` | Seconds before stale agent removed |
| `DATA_DIR` | `data` | SQLite database directory |

## OPA (Optional)

Run OPA server, then set `OPA_ENABLED=true`.

### Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `OPA_ENABLED` | `false` | Enable OPA compliance |
| `OPA_URL` | `http://localhost:8181` | OPA server URL |
| `POLICIES_DIR` | `policies/tags` | Policies directory |

### Policies
```bash
docker run --rm -it -p 8181:8181 -v $(pwd)/policies:/policies \
  openpolicyagent/opa run --server --bundle /policies --watch
```

Add `package opamp.agent.compliance.<name>` policies to `policies/tags/`.

## Endpoints

| Endpoint | Method | Description |
|----------|---------|-------------|
| `/v1/opamp` | POST | OpAMP connection |
| `/metrics` | GET | Prometheus metrics |
| `/agents` | GET | List agents |
| `/agent/{id}` | GET | Agent details |
| `/health` | GET | Health check |

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