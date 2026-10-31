---
name: opamp-server
description: Inspect and manage OpenTelemetry OpAMP agents via opamp-server-py — list/filter agents, generate slim OCB collector manifests, run fleet reports, check OPA compliance, and manage webhook alerts. Use when an agent needs to observe OTel collector fleets, build minimal collector builds, or automate compliance/alert workflows against an OpAMP server.
---

# OpAMP Server (opamp-server-py)

Control plane for OpenTelemetry collectors speaking the
[OpAMP](https://opentelemetry.io/docs/specs/opamp/) protocol.

## When to use

- You need to know **which collectors are connected**, their health, components, versions, or metadata
- You need to **build a slim collector** (OCB `manifest.yaml` from an agent's actually-used components)
- You need **fleet analysis** (heavy collectors with unused components, outdated component versions)
- You need **OPA compliance** results per agent or fleet-wide
- You need to **configure or test webhook alerts**

## Layered access — pick the right one

| Layer | When |
|-------|------|
| **`opampctl` CLI** | Default choice for shell agents. JSON output by default; `--raw` prints markdown/YAML. |
| **MCP server** (`opamp-mcp`) | When running as an MCP client (Claude Desktop/Code, etc.). 20 typed tools, stdio by default. |
| **`opamp_client` Python lib** | Inside Python code. One client for all HTTP/auth/error handling. |
| **Raw REST API** | Only when the above don't cover it. Self-documenting: `GET /openapi.json`, `/docs`. |

The CLI and MCP tools wrap `opamp_client`, which wraps the REST API — behavior is identical across all layers.

### Configuration (all layers)

- Server URL: `--server` flag / `OPAMP_SERVER_URL` env (default `http://localhost:4320`)
- Admin password: `--password` flag / `ADMIN_PASSWORD` env. Sent as HTTP Basic (any username, password only). Only admin endpoints need it: `PUT /alerts`, `POST /alerts/test`, `POST /compliance/check/{id}`, `POST /compliance/reload`, `GET /alerts`, `GET /config/posture`.
- Check auth mode first: `opampctl auth status` → `{"password_required": bool, "auth_mode": "password"|"disabled", "roles": [...]}`. `disabled` = read-only mode: no credential is configured and no privileged action is authenticated.

## Quickstart

```bash
# 1. Server (or docker-compose up -d)
uvicorn server.main:app --port 4320

# 2. Collector (OpAMP extension points at the server)
otelcol-contrib --config=collector/config.yaml
```

> **Gotcha:** the collector logs connection errors for the first seconds while
> it retries the OpAMP handshake — that is expected. Wait ~5s before concluding
> anything about connectivity.

```bash
# 3. First checks (read-only, no auth needed)
opampctl health                 # server status + agent count
opampctl agents list            # connected agents
opampctl agents list --healthy true --attr environment=prod
opampctl agents get <agent-id>  # instance UID is the hex id from list output
opampctl reports fleet --raw    # markdown fleet report
```

### Generate a slim collector manifest

```bash
opampctl agents manifest <agent-id> --raw > manifest.yaml
ocb build --config manifest.yaml
# 409 error => agent has no components in use; 422 => version must be semver
```

### Bring-up with OPA compliance (optional)

```bash
docker run --rm -d -p 8181:8181 -v $(pwd)/policies:/policies \
  openpolicyagent/opa run --server --bundle /policies --watch --addr :8181
OPA_ENABLED=true uvicorn server.main:app --port 4320
opampctl compliance summary
opampctl agents compliance <agent-id>
```

## Command map

| Task | opampctl | MCP tool | API |
|------|----------|----------|-----|
| List/filter agents | `agents list [--healthy] [--status] [--attr k=v]` | `list_agents` | `GET /agents` |
| Agent details | `agents get <id>` | `get_agent` | `GET /agent/{id}` |
| Agent metrics | `agents metrics <id>` | `get_agent_metrics` | `GET /agent/{id}/metrics` |
| OCB manifest | `agents manifest <id> [--raw]` | `generate_manifest` | `POST /agent/{id}/manifest` |
| Compliance (read) | `agents compliance <id>` | `get_compliance` | `GET /agent/{id}/compliance` |
| Compliance (admin) | `compliance check <id>` | `check_compliance` | `POST /compliance/check/{id}` |
| Fleet report | `reports fleet --raw` | `fleet_report` | `GET /reports/fleet` |
| Heavy collectors | `reports heavy [--threshold]` | `heavy_collectors_report` | `GET /reports/heavy-collectors` |
| Outdated components | `reports outdated [--version]` | `outdated_collectors_report` | `GET /reports/outdated-collectors` |
| Alerts config (admin) | `alerts get` / `alerts set @f.json` | `get_alerts` / `update_alerts` | `GET`/`PUT /alerts` |
| Test alert (admin) | `alerts test` | `test_alerts` | `POST /alerts/test` |
| Health + posture | `health` | `health` | `GET /health` |
| Config push posture | `config posture` | `config_posture` | `GET /config/posture` |
| Mint agent credential (admin) | `agents token create <id> [--raw]` | `create_agent_token` (write-gated) | `POST /agents/{id}/token` |
| Revoke agent credentials (admin) | `agents token revoke <id>` | `revoke_agent_token` (write-gated) | `DELETE /agents/{id}/token` |
| Offline (no server) | `--db data/opamp.db …` | — | — |

## Agent credentials (`OPAMP_AGENT_AUTH=token`)

When the server authenticates agents, each one sends `Authorization: Bearer <token>`
in its collector config (`extensions.opamp.server.http.headers`, or `server.ws.headers`).
The token **is** the agent's identity: `instance_uid` must match it, and a token for
one agent claiming another's uid is refused (403 / WS 1008).

```bash
opampctl agents list                                   # note the id
opampctl --password "$ADMIN_PASSWORD" agents token create <id> --label prod-1
#   -> {"token": "opamp_agent_...", "collector_snippet": "extensions:\n  opamp: ..."}
opampctl agents get <id> | jq .credential              # enrolled/active/last_seen_at
opampctl --password "$ADMIN_PASSWORD" agents token revoke <id>   # blocks delivery, keeps state
```

Rules that matter when acting on this:

- The token is returned **once**; only its SHA-256 is stored. If you lose it, mint a
  new one (rotation is zero-downtime: the old token retires on the first successful
  use of the new one; `--retire-previous` cuts over immediately).
- Never print a token into a report, issue, commit or chat transcript. `--raw` exists
  for `TOKEN=$(opampctl ... --raw)` pipelines, not for display.
- `401` on `/v1/opamp` = no/bad/revoked credential. `403` = valid credential, wrong
  agent. Both are counted in `opamp_agent_auth_failures_total{reason}`.
- Revocation is not a wipe: the agent's record, state and (later) desired config stay.
  Don't "clean up" by deleting agents you revoked.
- Minting needs the admin password, and the MCP tools for it are **absent** unless
  the MCP process runs with `OPAMP_MCP_ALLOW_WRITES=true`. That is intentional.


## Gotchas

- **Agent IDs are hex instance UIDs** from `list_agents` — not hostnames.
- **409 on manifest generation** means the agent reported no components *in use* (nothing to slim-build). Check `agents get <id>` → `components[].used`.
- **Version strings must be semver** (`1.0.0`, optionally `-prerelease`) for manifests and the outdated report — anything else is a 422.
- **Metrics persist in SQLite** (`DATA_DIR/opamp.db`), so they survive server restarts; but only the **latest snapshot per agent** is kept — no time series.
- **Metadata filters** (`--attr environment=prod`) match the agent's OpAMP description attributes; agents missing the attribute are excluded. Repeated values are OR.
- **OPA disabled** → compliance endpoints return `compliant: null` with a message; admin `compliance check` returns 503.
- **Offline mode** (`--db`) is read-only: no health, compliance, or alerts.
- **MCP config**: `python -m mcp_server` needs a Python where the package is installed (`pip install -e ".[mcp]"`); pass env vars in the client config, not flags.
- **Config push is off by default and fails closed.** `GET /health` → `config_push`
  (`{"enabled", "insecure", "agent_auth", "audit", "tls"}`) is the public summary;
  `opampctl config posture` adds *why* each requirement is unmet. Push routes
  return **404** when disabled — not 403, so a 404 tells you nothing about whether
  the endpoint exists. `insecure: true` means the development override
  (`OPAMP_ALLOW_INSECURE_CONFIG_PUSH`) is on and anyone reaching the server can
  rewrite the fleet's config: say so loudly instead of proceeding.
- **There is no default admin password.** Docker generates a random one on first
  start (`data/admin_password`, also logged once). If `auth status` says
  `password_required: false`, privileged endpoints are unauthenticated — do not
  assume your write succeeded securely just because it returned 200.
- **UI collectors tip**: when taking screenshots for UI work, start the collector first, wait 5s (ignore initial connection errors), then start the UI.
