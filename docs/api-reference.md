# API reference

The server is FastAPI-based and self-documenting:

- **Interactive docs:** [`/docs`](http://localhost:4320/docs) (Swagger UI) and [`/redoc`](http://localhost:4320/redoc)
- **Machine-readable schema:** [`/openapi.json`](http://localhost:4320/openapi.json) — fetch this to call every endpoint programmatically

Roles, principals and the two credential systems are in
[security.md](security.md#authentication); agent tokens in
[agent-tokens.md](agent-tokens.md).

## Endpoints

| Method | Endpoint | Role | Description |
|--------|----------|:----:|-------------|
| GET | `/auth/status` | — | Auth mode: `auth_mode` (`principals`\|`password`\|`disabled`), `roles`, `sessions`, `principals`. No secrets |
| POST | `/auth/session` | — | Log in with `{"name", "password"}` → `{"token", …}` (bearer, shown once, SHA-256 at rest). 401 bad credentials, 403 revoked/expired principal, 429 + `Retry-After` when locked out |
| DELETE | `/auth/session` | viewer | Log out: revoke the session token used for this request. 400 when authenticating with Basic (nothing to revoke) |
| GET | `/auth/me` | viewer | Identity, role, auth `method` (`session`\|`password`\|`legacy`\|`anonymous`) and session expiry |
| GET | `/auth/sessions` | admin | Live admin sessions with principal, client IP, user agent, expiry. Never a token |
| DELETE | `/auth/sessions/{id}` | admin | Revoke one session by its short id ("sign that device out") |
| GET | `/auth/principals` | admin | Admin identities, roles, last seen, `active`. Never a password or hash |
| POST | `/auth/principals` | admin | Create an identity: `{"name", "role", "password", "expires_at"}`. scrypt-hashed; 409 taken name, 422 bad role/short password |
| DELETE | `/auth/principals/{id}` | admin | Revoke an identity and close its sessions (row kept, name stays reserved) |
| GET | `/auth/verify` | viewer | Validate a credential — any role passes (200, 401, or 429 when locked out); failures feed the lockout |
| POST | `/v1/opamp` | 🔑 | OpAMP protocol endpoint — agents connect here (protobuf). The agent must send `Authorization: Bearer <token>`: 401 without a valid one, 403 when a bound token is for another `instance_uid`, 403 `agent_limit` when a fleet token is at its cap. Malformed input (including an empty `instance_uid`) is 400 with a `BAD_REQUEST` error body; a body over `OPAMP_MAX_MESSAGE_BYTES` (after decompression) is 413; an unsupported `Content-Encoding` is 415; when the server is overloaded the request is 503 with `Retry-After` and no state change (#78). The `OpAMP-Instance-UID` header is read and cross-checked against the body (#76); a duplicate uid is answered with a server-generated replacement (#75) |
| WS | `/v1/opamp` | 🔑 | Same endpoint over WebSocket (`OPAMP_WS_ENABLED`, on by default); credential checked before `accept()`, refusals close `1008`. Malformed messages (including an empty `instance_uid`) get a `BAD_REQUEST` error frame then close `1002`; oversized frames close `1009`; an overloaded server refuses the upgrade with `503` + `Retry-After` (#78) |
| POST | `/v1/metrics` | | OTLP/HTTP metrics ingestion (JSON or protobuf), keyed on `service.instance.id` then `service.name`. **Unauthenticated and OTLP-only** — one profile's telemetry, not a general metrics API (#122). See [telemetry.md](telemetry.md) |
| GET | `/agents` | | List agents. Filters: `healthy=true\|false\|unknown`, `status=` (remote config status, or alias `remote_config_status=`), or any description attribute, e.g. `?environment=prod` or `?agent.name=e2e-collector`. Repeated params are OR. Each agent carries `name` (#137 — the declared `agent.name`, `null` when it declared none; a display label, **not** an identity) and a `credential` block |
| GET | `/agent/types` | | The agent types (profiles) this build recognises, in match order: `name`, `display_name`, `icon`, `priority`, `summary`, `validates_structure`, `content_types`, `default_filename`, `reports`, `reports_telemetry`, `telemetry`, `detectable`, `identity` (`match_any` rules + `sniff`), `panels`, `components`, `bootstrap_default`. `otelcol` is an OpenTelemetry Collector, `fluentbit` a Fluent Bit supervised by `opamp-observe/opamp-core`, and `generic` any other OpAMP agent, whose config is stored and relayed **without being interpreted**. The list comes from the profile dictionary — see [agent-profiles.md](agent-profiles.md) |
| PUT | `/agent/{id}/type` | operator | **Pin** the agent's type (`{"agent_type": "otelcol"}`) or clear the pin (`null`). A pin outranks every signal the agent sends (`service.name`, `content_type`, payload shape), survives restarts and is audited (`agent.type.pinned` / `agent.type.auto`). 422 for an unknown name (never a silent `generic`), 404 for an unknown agent. Returns the four `agent_type*` fields with provenance |
| GET | `/agent/{id}` | | Agent details incl. latest OTLP metrics, `credential` state (`enrolled`, `active`, `last_seen_at`, `revoked_at` — never a token) and `agent_type` / `agent_type_source` (`pinned`\|`declared`\|`sniffed`\|`fallback`) / `agent_type_pinned` / `agent_type_warning` |
| GET | `/agent/{id}/metrics` | | Latest OTLP metric values for the agent (persisted across restarts) |
| POST | `/agent-tokens` | admin | Mint a **fleet** token: `{"label", "expires_in_days": 1\|7\|30\|null, "max_agents": n\|null}`. Not bound to an `instance_uid`, so it works before any agent exists. Returns the token **once**, its `token_id` and a paste-ready snippet. 422 outside 1-365 days / `max_agents` < 1 |
| GET | `/agent-tokens` | viewer | Every token's state — `token_id`, label, bound uid, `expires_at`, `expired`, `max_agents`, `agents_used`, `last_seen_at`, `revoked_at`, `active`. Never a token, never a hash |
| DELETE | `/agent-tokens/{token_id}` | admin | Revoke one token by handle. Idempotent; 404 when it does not exist or was already revoked. Blocks delivery, keeps the row and every agent's state |
| POST | `/agents/{id}/token` | admin | Mint/rotate a **bound** agent credential. Returns the token **once** plus a paste-ready collector snippet. Optional body `{"label", "retire_previous", "expires_in_days"}`. 422 if `id` is not a hex instance UID |
| DELETE | `/agents/{id}/token` | admin | Revoke every live credential for the agent (idempotent). Blocks delivery, keeps the agent's state |
| POST | `/agent/{id}/manifest` | | Generate OCB `manifest.yaml` + build command for a slim collector. Optional body `{"version": "0.123.0"}`. 409 if the agent has no components in use |
| GET | `/agent/{id}/compliance` | | Evaluate agent against OPA policies (no-op result if OPA disabled) |
| GET | `/agent/{id}/report` | | Markdown report for one agent (versions, health, components) |
| GET | `/agent/{id}/config` | viewer | Desired (pushed) + effective (reported) config, **redacted** with `redacted_paths`. `?reveal=true` needs `approver`, is audited (`config.reveal`) and sets `Cache-Control: no-store`. Includes revision `history` (metadata only) |
| PUT | `/agent/{id}/config` | approver | Push a desired config: `{"config_map": {"": {"body": "<yaml>", "content_type": "text/yaml"}}, "note": "…"}`. 404 when push is disabled, 422 with every validation error, including a redacted body. Creates an immutable revision |
| DELETE | `/agent/{id}/config` | approver | Withdraw by pushing an **empty** config map (agent falls back to its local config). Delivered once |
| POST | `/agent/{id}/config/deliver` | operator | Deliver the active revision over the agent's live WebSocket now. Idempotent: always 200 with `delivery.state` = `delivered` \| `queued` \| `unchanged` (+ `reason`). Moves no pointer, creates no revision, so it needs no approval. Audited as `config.deliver`; a refusal on principal grounds adds a second row with `result: "error"` |
| POST | `/agent/{id}/config/rollback` | approver | Return to the previous revision as a **new** revision with `parent_revision_id` set. 409 when there is nothing to roll back to |
| POST | `/agent/{id}/custom-message` | operator | Send a `ServerToAgent.custom_message` (spec §Custom Messages): `{"capability", "type", "data"}` with `data` base64. 400 listing `supported_capabilities` when the agent did not advertise the capability, 422 for bad base64, 413 above 64 KiB, 404 when `OPAMP_CUSTOM_MESSAGES_ENABLED=false`. Delivered **exactly once** on the next reply |
| POST | `/config/apply` | admin | Push one config to many agents: `{"config_map", "agents": [...] \| "filter": {...} \| "group": "prod-emea", "confirm": true}`. No selector is a 400 — never "all agents". A group target obeys the same guards as a proposal (`use_stored_resolution`, `confirm_scope_change`) |
| POST | `/config/revisions` | operator | **Propose** a revision — validates and drafts it, delivers nothing: `{"config_map", "scope": {"agent_id" \| "agents" \| "filter" \| "group", "include_future", "use_stored_resolution"}, "confirm_scope_change", "note"}`. Returns `proposal_id`, per-agent revisions and the full verdict. 422 with every structured error |
| GET | `/agents?q=...` | | **SQL selection** over the live fleet (#132): `SELECT * FROM agents WHERE host_arch = 'amd64'`. Wins over `healthy`/`status`/attribute params. Only `SELECT` from `agents`; descriptions only, never a config body. Returns `agents`, `count`, `query`, `query_columns`. Language guide: [sql-agent-queries.md](sql-agent-queries.md) |
| GET | `/agents/query/schema` | | SQL schema + autosuggestion: core columns, one entry per description attribute with observed values, operators and example statements. This is what makes `host_arch = 'amd64'` a suggestion instead of a guess |
| GET | `/groups` | viewer | Saved selectors with a **live** member count, `selector_kind` (`query`/`definition`) and `last_resolution`. Membership is resolved on read, never stored |
| POST | `/groups` | operator | Create a group: `{"name", "query": "SELECT * FROM agents WHERE ..."}` (recommended) **or** `{"name", "definition": {"environment": "prod"}}` (legacy), plus `"include"?, "exclude"?, "description"?, "owner"?`. Exactly one selector; names are unique case-insensitively; a query may not use `LIMIT`/`OFFSET`. Rules: [groups.md](groups.md) |
| GET | `/groups/{id}` | viewer | Query (or legacy definition), current members, `sources` (selector vs pins), `unknown_pins`, `membership_changes` and the resolution history |
| PUT | `/groups/{id}` | operator | Edit a group; `{"query": "..."}` replaces the selector (and clears a legacy definition), `{"definition": {...}}` goes the other way. The audit row carries the **selector diff**, and `changes` is returned. Affects future pushes only: a revision keeps its `group_snapshot` |
| DELETE | `/groups/{id}` | operator | **Archive** (there is no delete — revisions and rollouts reference the id). 409 while a live rollout uses it. Restorable |
| POST | `/groups/{id}/restore` | operator | Un-archive |
| POST | `/groups/{id}/preview` | viewer | `added`/`dropped`/`unchanged` versus the last recorded resolution; records the new baseline. This is how a health-defined group gets a membership for `use_stored_resolution` |
| GET | `/groups/attribute-keys` | viewer | Attribute keys and observed values from live agents (typo-proof authoring; descriptions only, never a config body). Superseded by `/agents/query/schema` for SQL authoring |
| GET | `/config/revisions` | viewer | Revision metadata (no bodies): `?agent=`, `?status=proposed\|active\|superseded\|rolled_back\|poisoned\|rejected`, `?limit=` |
| GET | `/config/revisions/{proposal_id}` | viewer | One proposal: each revision's status, stored verdict and delivery record (`attempts`, `last_status`, `poisoned`) |
| POST | `/config/revisions/{id}/approve` | approver | Re-resolve the selector, **re-run validation**, supersede and activate. 409 `re-validate: …` when the config no longer passes, and 409 when the selector now matches *more* agents (repeat with `confirm_scope_change: true`); `override_reason` overrides a policy violation (audited as `config.override`) |
| POST | `/config/revisions/{id}/reject` | approver | Refuse a proposal; the revision is kept as evidence |
| POST | `/config/revisions/{id}/reactivate` | approver | Resume a **poisoned** revision after a fresh validation pass. Nothing retries on a timer |
| POST | `/config/validate` | viewer | Validate a config map without storing it: `{"config_map", "agent_id"?, "allow_endpoint_change"?}`. Always 200: `{valid, errors, details, warnings, policy, fingerprint, config_hash, size_bytes, objects, profile, profile_source}` — the last two name the agent profile whose rules produced the verdict |
| GET | `/reports/fleet` | | Full fleet summary report (markdown, same as the UI Reports page) |
| GET | `/reports/heavy-collectors` | | Collectors with many unused components. Params: `threshold` (0–1, default 0.5). Also returns `applicable_count` and `not_applicable` (agents per profile the report does not cover — #120) |
| GET | `/reports/outdated-collectors` | | Collectors with components older than `version` (default `0.149.0`) |
| POST | `/compliance/check/{id}` | operator | Force a compliance check (503 if OPA disabled) |
| GET | `/compliance/summary` | | Fleet-wide compliance counts |
| GET | `/compliance/policies` | | Available OPA policies |
| POST | `/compliance/reload` | admin | Ask OPA to reload policies from disk |
| GET | `/compliance/validate` | | Validate OPA policy files |
| GET | `/alerts` | viewer | Alert configuration with `webhook_url` and `headers` **redacted** |
| PUT | `/alerts` | operator | Merge alert configuration. `***REDACTED***` keeps the stored secret |
| POST | `/alerts/test` | operator | Send a test alert (optionally with a temporary `event_config`) |
| GET | `/health` | | Health check (server status, agent count, OPA availability, `desired_configs`) plus the public `config_push` posture block (incl. `config_at_rest`) |
| GET | `/config/posture` | viewer | Full config push posture: every requirement, what is met, where the admin credential came from, whether the override is on. No secrets |
| GET | `/audit` | admin | Privileged-action history, newest first. Filters `since`, `until`, `actor`, `action` (prefix), `target`, `result`; `limit`/`offset` paginated. Hashes only — never a config body or a secret |
| GET | `/audit/verify` | admin | Recompute the hash chain. `ok: false` + `broken_at` means the log is no longer evidence; `anchored_by_checkpoint` is benign retention |
| GET | `/audit/export` | admin | `format=jsonl` (SIEM) or `csv` (spreadsheet), oldest first, `Content-Disposition: attachment` |
| GET | `/metrics` | | Prometheus metrics. Listed in [operations.md](operations.md#observability) |

`Role` is the minimum role; roles are ordered (`viewer` < `operator` < `approver` <
`admin`), so an `approver` may do what an `operator` may. `—` means no credential. In
read-only mode (nothing configured) reads still work and every non-`—` row answers
`403`. 🔑 means an agent credential (`Authorization: Bearer opamp_agent_…`).

Example: filter agents by metadata and health

```bash
curl "http://localhost:4320/agents?healthy=true&environment=prod"
curl "http://localhost:4320/agents?status=UNSET"
```
