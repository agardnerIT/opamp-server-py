# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **Run quality gates** (if code changed) - Tests, linters, builds
2. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
3. **Clean up** - Clear stashes, prune remote branches
4. **Verify** - All changes committed AND pushed
5. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds


## Build & Test

```bash
pip install -e ".[dev,ui]"     # install with test + UI deps (or: pip install -r requirements.txt)
pytest tests/ -v               # run the test suite
opampctl health                # smoke-check against a running server (default :4320)
```

- Server (dev): `uvicorn server.main:app --port 4320`
- UI (dev): `pip install -r requirements-ui.txt && streamlit run ui/app.py`
- MCP server (dev): `pip install -e ".[mcp]" && python -m mcp_server`

## AI Accessibility (driving this project programmatically)

This project is designed to be operated by AI agents. Layers (each wraps the previous):

1. **REST API** — FastAPI at `:4320`, self-documenting: `GET /openapi.json` (machine schema), `/docs` (Swagger). Endpoint table + auth model in README → "API Reference".
2. **`opamp_client`** (Python) — `from client import OpampClient`; reads `OPAMP_SERVER_URL` / `ADMIN_PASSWORD`. Structured errors: `OpampApiError(.status_code, .detail)`, `OpampConnectionError`.
3. **`opampctl` CLI** — JSON output by default, `--raw` for markdown/YAML, `--db data/opamp.db` for offline SQLite reads. Config: `--server`/`--password` or env.
4. **MCP server** — `python -m mcp_server` (stdio; `--transport sse` for remote). 19 typed tools; needs `pip install -e ".[mcp]"`. Client config snippets in README → "MCP Server".
5. **Agent skill** — `skills/opamp-server/SKILL.md`: when/how guidance, quickstart, gotchas.

When adding API endpoints: add the endpoint (docstring included), extend `client/opamp_client.py`, add an `opampctl` command, add an MCP tool, then add tests for each. All four layers must stay in sync.

## Architecture Overview

- `server/` — FastAPI app (`main.py`), SQLite-backed agent state + metrics (`state.py`), OCB manifest generation (`manifest.py`), report generators (`reports.py`), OPA client (`opa_client.py`), webhook alerts (`alerts.py`)
- `client/` — `opamp_client`: shared httpx client used by CLI + MCP (single HTTP implementation)
- `cli/` — `opampctl` (typer): JSON output, `--raw`, `--db` offline mode
- `mcp_server/` — FastMCP server (stdio default, optional SSE)
- `ui/` — Streamlit dashboard (imports shared logic from `server/`)
- `proto/` — generated OpAMP protobuf code
- `data/` — SQLite DB (`opamp.db`): agent state, metrics, alert config

## Conventions & Patterns

- **JSON everywhere** in the agent-facing layers; errors are structured (`{"error", "status_code", "detail"}`) with non-zero exit codes
- Admin auth = HTTP Basic (any username, `ADMIN_PASSWORD` as password) on admin endpoints only; never add a new auth system
- Shared logic (manifests, reports, state) lives in `server/` — the UI and agent layers import it, never duplicate it
- UI changes: screenshot and add to README (see memory.md); ignore the collector's initial OpAMP connection errors (expected retry noise)
