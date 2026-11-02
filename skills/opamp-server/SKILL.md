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
| Alerts config (redacted read / operator write) | `alerts get` / `alerts set @f.json` | `get_alerts` / `update_alerts` | `GET`/`PUT /alerts` |
| Test alert (operator) | `alerts test` | `test_alerts` | `POST /alerts/test` |
| Who am I + role | `auth whoami` | `whoami` | `GET /auth/me` |
| Auth mode (no secrets) | `auth status` | `auth_status` | `GET /auth/status` |
| Log in for a session token (admin role holder) | `auth login --name <n> --raw` | — (never exposed to MCP) | `POST /auth/session` |
| Identities / roles (admin) | `auth principals list\|create\|revoke` | — | `GET`/`POST`/`DELETE /auth/principals` |
| Live sessions (admin) | `auth sessions list\|revoke` | — | `GET`/`DELETE /auth/sessions` |
| Health + posture | `health` | `health` | `GET /health` |
| Config push posture | `config posture` | `config_posture` | `GET /config/posture` |
| Audit log (admin) | `audit list [--actor] [--action] [--since] [--result]` | `audit_log` | `GET /audit` |
| Verify the audit chain (admin) | `audit verify` (exit 1 if broken) | `audit_verify` | `GET /audit/verify` |
| Export the audit log (admin) | `audit export -f jsonl\|csv [-o file]` | `audit_export` | `GET /audit/export` |
| Mint agent credential (admin) | `agents token create <id> [--raw]` | `create_agent_token` (write-gated) | `POST /agents/{id}/token` |
| Revoke agent credentials (admin) | `agents token revoke <id>` | `revoke_agent_token` (write-gated) | `DELETE /agents/{id}/token` |
| Desired + effective config (redacted) | `agents config get <id> [--reveal]` | `agent_config` (redacted only) | `GET /agent/{id}/config` |
| Push a config (approver) | `agents config set <id> --file f.yaml` | — (never exposed) | `PUT /agent/{id}/config` |
| Withdraw a config (approver) | `agents config withdraw <id>` | — | `DELETE /agent/{id}/config` |
| Validate a config (safe) | `config validate --file f.yaml` | `validate_config` | `POST /config/validate` |
| Propose a revision (draft) | `config propose --file f.yaml --attr env=prod` | `propose_config` (write-gated) | `POST /config/revisions` |
| Revision history (metadata) | `config revisions [--agent] [--status]` | `list_config_revisions` | `GET /config/revisions` |
| One proposal | `config show prop-…` | — | `GET /config/revisions/{id}` |
| Approve (approver) | `config approve prop-…` | — (never exposed) | `POST /config/revisions/{id}/approve` |
| Reject (approver) | `config reject prop-… --reason …` | — | `POST /config/revisions/{id}/reject` |
| Reactivate poisoned (approver) | `config reactivate prop-… --reason …` | — | `POST /config/revisions/{id}/reactivate` |
| Roll back one agent (approver) | `agents config rollback <id>` | — | `POST /agent/{id}/config/rollback` |
| Deliver the active revision now (operator) | `agents config deliver <id>` | — | `POST /agent/{id}/config/deliver` |
| Plan a staged rollout (dry run) | `config rollout plan prop-…` | — | `POST /config/rollouts` (`dry_run`) |
| Create a rollout = approve + stage (approver) | `config rollout create prop-…` | — (never exposed) | `POST /config/rollouts` |
| Rollout progress, per-agent state, pause reason | `config rollout show ro-…` / `config rollouts` | `get_config_rollout` / `list_config_rollouts` | `GET /config/rollouts[/{id}]` |
| Stop promoting (operator) | `config rollout abort ro-… --reason …` | — | `POST /config/rollouts/{id}/abort` |
| Undo a rollout, staged (approver) | `config rollout rollback ro-…` | — | `POST /config/rollouts/{id}/rollback` |
| Re-run validation for a proposal (read-only) | `config revalidate prop-…` | — | `POST /config/revisions/{id}/validate` |
| Restart an agent (operator, needs the capability) | `agents config restart <id>` | — | `POST /agent/{id}/restart` |
| Fleet push (admin, confirmed) | `config apply --file f.yaml --attr env=prod --confirm` | — | `POST /config/apply` |
| Re-encrypt stored bodies | `config rotate-key --db data/opamp.db` | — | — |
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


## Audit log (#97)

Every privileged action writes one row: actor (`agent:<id>` for agents), role,
action, target (`target_kind`/`target_id`), result (`ok|denied|error|noop`), a
redacted `detail_json` and the chain fields `prev_hash`/`entry_hash`. Reads are
admin-only; the MCP tools for them are **never** write-gated — an agent asked "who
pushed this" must answer from evidence.

```bash
opampctl --password "$ADMIN_PASSWORD" audit list --action config. --limit 20
opampctl --password "$ADMIN_PASSWORD" audit verify   # ok:false + broken_at = tampering
```

Rules that matter when acting on this:

- **No bodies, no secrets.** Config bodies are never stored — only `config_hash`.
  Do not paste configs into audit-adjacent fields; the filter drops fields *named*
  like credentials, it cannot recognise a secret in a field called `detail`.
- **Fail closed.** A privileged write records its decision first; if the row cannot
  be written the write does not happen (HTTP 500, nothing mutated). If you see a 500
  from an otherwise-fine endpoint, check the audit store before retrying blindly.
- `ok: false` from `audit verify` means the log is no longer evidence. Do not delete
  rows to "fix" it — export what remains and tell a human.
- `anchored_by_checkpoint: true` is benign: retention pruned old entries and wrote a
  checkpoint row recording exactly what it cut.
- Turning `OPAMP_AUDIT_ENABLED` off makes the server refuse privileged writes. Do
  not disable it to get past an error.

## Admin auth and roles (`OPAMP_ADMIN_AUTH`, #95)

Every privileged endpoint takes a role, ordered `viewer` < `operator` < `approver` <
`admin` (a role implies the ones below it). Approval is deliberately a **different
role** from operator: #103's two-phase push only means something if one person
proposes and another approves.

- **Read-only mode** (no credential configured at all) is read-open and
  **write-closed**: reads succeed, writes answer `403` "admin auth is not
  configured". Do not report a 403 as "broken auth" — check `auth status` first.
- `401` = no/bad credential; `403` = valid credential, insufficient role (or a
  revoked principal); `429` = lockout (on `POST /auth/session` **and** on repeated
  failed Basic auth). Honour `Retry-After` and stop — retrying extends the backoff,
  and the lockout is per `(name, client IP)`, so a loop will keep you out.
- Credentials you can use, in order of preference: `--token` / `OPAMP_ADMIN_TOKEN`
  (a session token: carries an identity and role, revocable), `--password` /
  `ADMIN_PASSWORD` (Basic, legacy shared secret).
- Session tokens expire (`ADMIN_SESSION_TTL_SECONDS`, default 1 h) and idle out
  (`ADMIN_SESSION_IDLE_SECONDS`, default 15 min). A `401` mid-session usually means
  expired, not wrong: log in again.
- `GET /alerts` is **redacted**: `webhook_url` comes back as
  `scheme://host/***REDACTED***` and `headers` as `***REDACTED***`. Send those values
  back unchanged on `PUT` to keep the stored secret; do not "fix" them.
- Identity administration (principals, sessions, logins) has **no MCP tools** on
  purpose. If a task needs a new identity or a role change, hand it to a human
  (`opampctl auth principals create …`) rather than trying to do it through the API
  with an AI's credential.
- Never put a password, a session token or an agent token in a report, an issue, a
  commit or a chat transcript. Use `--raw` into a shell variable, and
  `auth whoami` to confirm which role you are acting as before a privileged call.

## Remote config (`OPAMP_CONFIG_PUSH_ENABLED=true`)

Off by default. When it is on and the posture is secure, the server stores a
*desired* config per agent and delivers it — immediately over a live WebSocket
connection, or on the agent's next status report — desired state, not a command,
so it survives restarts and is idempotent.

```bash
opampctl config validate --file collector.yaml          # safe: no storage, no audit
opampctl --password "$ADMIN_PASSWORD" agents config set <id> --file collector.yaml
opampctl agents config get <id>                          # desired + effective, REDACTED
opampctl --password "$ADMIN_PASSWORD" config apply --file collector.yaml \
  --attr environment=prod --confirm                      # admin, explicit targets
opampctl --password "$ADMIN_PASSWORD" agents config withdraw <id>
opampctl --password "$ADMIN_PASSWORD" agents config deliver <id>   # re-send now
```

Rules that matter when acting on this:

- **Read `delivery.state`, do not assume instant.** Every push answers with
  `delivered` (handed to the agent's live WebSocket), `queued` (it will arrive on
  the next status report — `delivery.detail` says when) or `unchanged` (nothing to
  do; `delivery.reason` says why). `queued` is the designed fallback, not an error:
  the desired state is stored and the agent's own report collects it. Never report
  a push as "applied" — only the agent's `APPLIED` report means that
  (`agents get <id>` → `remote_config_status`, or `agents config get <id>` →
  `assignment.state`).
- **`agents config deliver` is not a way around approval.** It re-sends the
  *active* revision; it cannot create or change one, which is why `operator` is
  enough. If there is nothing active it answers `unchanged` with
  `reason: nothing_assigned` or `revision_not_deliverable`.

- **Only the OpAMP Supervisor can apply a pushed config.** `extension/opampextension`
  never advertises `AcceptsRemoteConfig`; a bare collector will simply never be
  offered one. Check `agents get <id>` → `capabilities`.
- **Never retried automatically.** If the agent reports `FAILED` for our revision it
  is *poisoned*: visible as `desired.failed_at`, audited as `config.apply.failed`,
  and never sent again. Push a new revision or roll back — do not "just retry".
- **A redacted body cannot be pushed.** Anything containing `<redacted>` is a 422.
  If you fetched a config with `agents config get`, it is a display artefact — go
  back to the source file.
- **`--reveal` is a privileged, audited action** (`approver` role, `config.reveal`
  row, `Cache-Control: no-store`). Do not reveal to debug something you could check
  with `redacted_paths`.
- **MCP can validate and read, never approve.** `validate_config`, `agent_config`
  and `list_config_revisions` are always available; `propose_config` only exists
  with `OPAMP_MCP_ALLOW_WRITES=true`. There is no approve/reject/reactivate/
  rollback/apply tool and there never will be (epic #93, D3/D9). Propose the config
  and its hash to a human instead.
- **Fleet-wide needs `--confirm` and an explicit selector.** A missing selector is a
  400 — it never means "all agents".
- **`config get` offline (`--db`)** shows metadata only and always redacts; there is
  no credential and no audit trail offline, so there is no reveal.

### Two-phase flow: propose → approve → roll back (#99/#100)

Use this when the change matters. A direct `agents config set` is approval-by-fiat
(it needs the `approver` role); propose/approve separates the duties.

```bash
opampctl --password "$ADMIN_PASSWORD" config propose --file collector.yaml \
  --attr environment=prod --note "why"
# {"proposal_id": "prop-…", "status": "proposed", "agents": 3, "verdict": {…}}
opampctl --password "$ADMIN_PASSWORD" config approve prop-… --note "reviewed"
opampctl agents config get <id>                 # drift + assignment + verdict
opampctl --password "$ADMIN_PASSWORD" agents config rollback <id> --note "why"
```

- **Proposing delivers nothing.** It validates and drafts one immutable revision per
  target. Approving re-resolves the selector and **re-runs validation** — a config
  that no longer passes is `409 re-validate: …`. If the selector now matches *more*
  agents than it did at propose time, the approval is refused until you repeat it
  with `confirm_scope_change` (`added` / `dropped` in the response show the diff).
- **Statuses:** `proposed → active → superseded | rolled_back | poisoned`, plus
  `rejected`. Only `active`, unpoisoned revisions are deliverable. Read the status
  from `desired.status`, never assume "pushed" meant "live".
- **`include_future` is opt-in and only on a selector scope.** Without it a
  later-joining agent inherits nothing. Never set it when the intent is "these three
  hosts".
- **A policy violation needs `--override-reason`**, which is recorded and audited as
  `config.override`. Do not supply a reason you cannot defend in a postmortem.
- **`poisoned` is permanent until an approver runs `config reactivate`.** That is
  deliberate: retry loops against a live fleet are worse than a stale config.
- **Drift** in `agents config get` is `true | false | null` plus `missing` YAML
  paths. `null` means "cannot tell yet" (no effective config reported, or the
  revision is not active) — do not read it as healthy.

### Staged rollouts (#101/#103)

A rollout is how an approved config reaches a fleet: **canary first, then gated
batches**. Creating one *is* the approval, so it needs `approver`; `config rollout
plan` is the same call with `dry_run` and changes nothing.

```bash
opampctl --password "$ADMIN_PASSWORD" config rollout plan prop-…   # batches, caps, verdict
opampctl --password "$ADMIN_PASSWORD" config rollout create prop-… --health-gate 90
opampctl --password "$ADMIN_PASSWORD" config rollout show ro-…     # per-agent state
opampctl --password "$ADMIN_PASSWORD" config rollout abort ro-… --reason "typo in exporter"
opampctl --password "$ADMIN_PASSWORD" config rollout rollback ro-… # undo, staged
```

- **Promotion needs all four:** every agent in the batch reports `APPLIED` **with our
  hash**, is `healthy` (only `healthy=false` blocks — health reporting is optional
  upstream), has not gone silent, and
  `CONFIG_PUSH_HEALTH_GATE_SECONDS` has elapsed since the last delivery in the batch.
- **Nothing is delivered to a later batch while it waits.** Approval *holds* every
  assignment (`desired.held: true`, delivery state `held`); the engine releases one
  batch per gate pass. A new revision releases the hold.
- **A pause is not a retry timer.** `paused_reason` is exactly one of `failed`,
  `auto_reverted`, `agent_went_silent`, `unhealthy`, `delivery_unconfirmed`,
  `superseded`. Nothing is retried and nothing is rolled back automatically: an
  approver either `config reactivate`s the revision or starts a rollback rollout.
- **`abort` cannot recall** a config that is already on an agent. It stops the next
  batch, keeps un-promoted agents held, and leaves applied agents alone.
- **TTL:** a running *or paused* rollout older than `CONFIG_PUSH_ROLLOUT_TTL_MINUTES`
  is cancelled and audited (`reason: ttl_expired`) — expired holds stay held.
- **`--restart`** queues a restart (#84) for each agent once its batch passes the
  gate. Works only for collectors with `accepts_restart_command: true`; the others are
  skipped with a log line, never silently "restarted".
- **MCP can read rollouts, never create, abort or roll back one** (`get_config_rollout`,
  `list_config_rollouts`). Creating a rollout is an approval (D9).

### What validation checks (#100)

`config validate` is side-effect-free and always exits 0. It refuses: malformed
YAML, duplicate keys, an alias bomb, a pipeline referencing a component that is not
defined, an empty `service.pipelines`, a config that drops `extensions.opamp` or
removes `opamp` from `service.extensions` (that orphans the agent with no remote
way back), a body over `CONFIG_MAX_BYTES`, and a redacted body. It warns (never
blocks) about: a missing `memory_limiter` or `batch`, missing
`service.telemetry`, an exporter host outside `CONFIG_ALLOWED_EXPORTER_HOSTS`, and a
component the target agent does not ship. Moving the `opamp` endpoint needs
`allow_endpoint_change` **and** an approver confirmation.

A body with no `service` section is a *fragment*: structural checks are skipped with
a warning. A body with no collector sections at all is not judged (it may be a
certificate or a routing table).

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
  (`{"enabled", "insecure", "agent_auth", "audit", "tls", "config_at_rest"}`) is the
  public summary;
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
