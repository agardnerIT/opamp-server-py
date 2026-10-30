#!/usr/bin/env python3
"""
Create the "secure remote config push + UI" epic and sub-issues.

Creates:
  - labels: security, config-push, phase-1-security ... phase-5-layers
  - one epic issue
  - one issue per work item, cross-linked from the epic

Work items are held in WI below (single source of truth). Rerunning is safe-ish:
it creates duplicates, so use --dry-run first, or check the printed titles.

Usage:
    python3 planning/create_config_push_issues.py --dry-run
    python3 planning/create_config_push_issues.py
"""
import argparse
import re
import subprocess
import sys

REPO = "agardnerIT/opamp-server-py"

AI_NOTE = (
    "\n---\n"
    "🤖 *Planned with the assistance of an AI coding agent. Design detail here is "
    "deliberate — read it before implementing, and update this issue if the design "
    "changes rather than silently diverging.*\n"
)

LABELS = {
    "security": ("b60205", "Authentication, authorisation, secrets, audit, threat model"),
    "config-push": ("0e8a16", "Remote configuration push (control plane + UI)"),
    "phase-1-security": ("f9d0c4", "Config push: security prerequisites (blocking)"),
    "phase-2-control-plane": ("fef2c0", "Config push: revision store, validation, rollout, delivery"),
    "phase-3-api": ("bfd4f2", "Config push: API surface"),
    "phase-4-ui": ("d4c5f9", "Config push: Streamlit UI"),
    "phase-5-layers": ("c2e0c6", "Config push: client / CLI / MCP / skill parity"),
    "phase-x-cross": ("e6e6e6", "Cross-cutting work"),
}

EPIC = (
    "Epic: secure remote config push + admin UI",
    ["security", "config-push"],
    """## Goal

Turn the now-available WebSocket push channel (#89) into a **safe** control plane for
remote collector configuration: an operator — or an AI agent — *proposes* a config, it is
validated and approved, rolled out in stages, verified against agent health, and rolled
back automatically when it goes wrong.

None of that is safe today. The OpAMP endpoint accepts any client claiming any
`instance_uid`, the admin surface is a single shared password that is **empty by default
in `docker-compose.yml`**, and there is no audit trail. A config-push endpoint bolted onto
that is fleet takeover as a service.

## Why now

- **#89** added the WebSocket transport. The server can push without waiting for the next
  poll, so the delivery-latency argument in #86 is settled. Push delivery is issue #9 below;
  the protocol-level remote-config semantics stay in **#81**.
- Config push converts this server from a *read-only observer* into a **remote control
  channel** for the fleet. Every threat below is currently unmitigated.
- Upstream reality check (verified against `opentelemetry-collector-contrib@main`):
  - `extension/opampextension` **never sets `AcceptsRemoteConfig`** — the collector alone
    cannot apply pushed config. The **OpAMP Supervisor** (`cmd/opampsupervisor`) can: it
    composes config files, restarts the collector via SIGHUP, persists
    `last_recv_remote_config.dat` + `last_working_remote_config.dat`, and **auto-reverts to
    the last working config**.
  - The collector extension's default transport is now **WebSocket** (`server.ws.endpoint`),
    and both transports support `headers`, an `auth` extension, and `tls` — so per-agent
    credentials are possible without inventing a wire extension.
  - Upstream warns that an **invalid config leaves the collector down** ("will error out
    and stay down until manually restarted"), and that `reports_raw_config` can expose
    **secrets**. Both constrain this design.

Consequences baked into the plan: only agents advertising `AcceptsRemoteConfig` are valid
targets; the agent may report a hash we never pushed (auto-revert) and we must not loop;
and the collector may not come back at all, which is why rollout staging (#8) is not
optional polish.

## Threat model

Assets: collector configs (they contain credentials), fleet availability, fleet integrity
(a config can point an exporter at an attacker or read local files), the truth of the audit
log, the admin credential.

Actors: unauthenticated network attacker with access to :4320, malicious or compromised
agent, compromised AI agent / MCP client, careless operator, insider.

| # | Threat | Mitigation | Issue |
|---|--------|-----------|-------|
| T1 | Anyone impersonates an agent (`instance_uid` is self-asserted) → receives a config meant for someone else, hijacks a live WebSocket binding, or suppresses a real agent's rollout status | Authenticate agents; derive identity from the credential, not the message body; bind uid ↔ principal; refuse binding takeover | #1 |
| T2 | Admin surface open by default → the push endpoint is unauthenticated fleet control | Fail closed: config push refuses to enable unless admin auth, agent auth and TLS are all in place | #3 |
| T3 | One shared password, no identity, brute-forceable (`==` compare, no lockout, `/auth/verify` is an oracle) | Roles, short-lived sessions, constant-time compares, lockout, revocation | #2 |
| T4 | Config and password in cleartext on the wire (HTTP default) | Require TLS / WSS, keep the `Origin` check (#89), document the proxy | #3 |
| T5 | No record of who pushed what, when, to whom | Append-only, hash-chained audit log; privileged actions fail if not logged | #4 |
| T6 | Secrets leak via `effective_config` (plaintext in SQLite), raw-config reporting, logs, reports, UI | Redaction, encryption at rest, masked reveal, never re-push a redacted body | #5 |
| T7 | Crafted or careless config: invalid YAML (collector stays down), YAML alias bomb, removal of the `opamp` extension (fleet orphaned, unfixable remotely), exporter pointed at an attacker | Validation pipeline + protected invariants + size limits + OPA policy gate | #7 |
| T8 | One typo takes the fleet down | Canary, health-gated batches, blast-radius caps, auto-pause, one-click rollback | #8 |
| T9 | Re-push loop: agent reports `FAILED` or auto-reverts, reports the *old* hash, server pushes the broken config forever | Never auto-retry; poison the revision on `FAILED`/auto-revert; require re-approval | #6, #81 |
| T10 | An AI/MCP client with write access pushes broken config to production | Write tools absent by default; non-interactive clients may only *propose*; approval is a human role | #13, #2 |
| T11 | UI session hijack / CSRF, password held in page state indefinitely | Session tokens, XSRF, short TTL, role-aware UI, deployment guidance | #12 |

## Design decisions

Recorded here so later sessions do not relitigate them. Change one and say why.

- **D1 — Identity comes from the credential, never from the body.** A token, client cert or
  auth extension identifies the agent; `AgentToServer.instance_uid` is then checked against
  the principal that presented it. Unauthenticated clients learn nothing (no capability
  advertisement).
- **D2 — Fail closed.** `OPAMP_CONFIG_PUSH_ENABLED=false` by default (the spec recommends
  remote config be opt-in, `specification.md:3644`). Enabling it while the posture is
  insecure is a startup error, not a warning.
- **D3 — Two-phase control plane: propose → approve.** Proposing is cheap and open to
  operators, CLI and MCP. Approval is human, role-gated, and required before anything is
  delivered to an agent. Fleet-wide scope needs a second confirmation.
- **D4 — Immutable revisions.** A revision is never edited after approval; rollback creates a
  new revision whose content is the previous one. Hashes are computed exactly as
  `specification.md §Calculating Hashes` says (#81) — no invented scheme.
- **D5 — Desired state, not a command.** The server stores what the agent *should* run and
  reconciles; delivery is idempotent and survives restart. "Push now" is a latency
  optimisation over the same model, never a separate code path.
- **D6 — Never auto-retry a failed config.** A `FAILED` status or an auto-revert poisons the
  revision, is surfaced loudly, and requires a human decision (rollback or re-approve).
- **D7 — Protected invariants.** A pushed config must still parse, stay under the size cap,
  keep the `opamp` extension wired to this server, and pass the project's OPA policies.
  Refusing is better than a fleet we cannot reach.
- **D8 — Config bodies are secrets.** Hashes only in audit and listings, encrypted at rest,
  redacted on read without the `secret:read` permission, never in reports, alerts or the UI.
- **D9 — Same rules in every layer.** Endpoint, `opamp_client`, `opampctl`, MCP and the skill
  all expose the same flow, and the write path is off by default in each (per AGENTS.md).

## Work breakdown

**Phase 1 — security prerequisites (blocking; nothing else ships first)**

- [ ] <WI:0> Agent authentication for the OpAMP channel
- [ ] <WI:1> Admin auth hardening (roles, sessions, lockout)
- [ ] <WI:2> Fail-closed enablement and secure-by-default posture
- [ ] <WI:3> Append-only audit log
- [ ] <WI:4> Config secret handling (redaction, encryption at rest)

**Phase 2 — control plane**

- [ ] <WI:5> Revision store and lifecycle (propose → approve → active → rollback)
- [ ] <WI:6> Validation and policy gate (structural, protected invariants, OPA)
- [ ] <WI:7> Staged rollout engine (canary, health gate, auto-stop, rollback)
- [ ] <WI:8> Delivery: unsolicited server→agent push over WebSocket + poll-queue for HTTP

**Phase 3 — surface**

- [ ] <WI:9> API endpoints for the config flow
- [ ] <WI:10> UI: Remote Config page
- [ ] <WI:11> UI: Streamlit auth/session hardening
- [ ] <WI:12> `opamp_client` / `opampctl` / MCP / skill parity
- [ ] <WI:13> Security regression suite + fake agent harness

## Ordering

```
#3 (posture/fail-closed, small)
 └── #1 (agent auth)  ─┐
 └── #2 (admin auth)  ─┼── #4 (audit) ── #5 (secrets)
                       │
                       └── #81 (wire protocol, existing) ── #6 (revisions)
                                                           ├── #7 (validation)
                                                           ├── #8 (rollout) ── #9 (delivery)
                                                           └── #10 (API) ── #11 (UI) ── #13 (layers)
#12 (UI auth)  — before #11 ships
#14            — written alongside #6..#11, not after
```

Pragmatic sequencing: land **#3 → #1 → #2** as one small "close the barn door" sprint; they
are independently useful and shrink the blast radius of everything later. **#81** (wire
protocol) and **#6/#7** (control plane) can then run in parallel; **#8/#9** finish the
safety story; **#10/#11** make it usable.

## Out of scope (separate issues)

- **#81** — the wire-level `remote_config` field, hashing and response logic. This epic is the
  secure control plane *around* it.
- **#82** packages, **#83** connection settings / CSR, **#84** restart command, **#85** custom
  messages. #84 is a **dependency** of a good UX (config changes need a restart), but this
  epic does not re-implement it.
- Git-backed config source of truth, config templating/merging DSL, multi-tenancy.
- Agent-side behaviour. We can validate and refuse; we cannot stop an agent doing something
  silly with a valid config. That is why #8 exists.
- **Drift** *alerting* is in scope (#6/#11 surface it); automated remediation is not.

## Definition of done

- [ ] With default settings, no client can alter fleet configuration (all push paths off,
      and the API path 401/404s rather than degrading)
- [ ] An unauthenticated or wrongly-credentialed client cannot receive a config, cannot bind
      another agent's `instance_uid`, and learns nothing about capabilities
- [ ] Every privileged action leaves an audit entry that names a *principal*, and the action
      fails if the entry cannot be written
- [ ] No config body appears in logs, reports, alert payloads, or any API listing
- [ ] Propose → approve → canary → promote → verified flow works from the UI, CLI and MCP
      (MCP may only propose)
- [ ] A config that would remove the `opamp` extension, exceed the size cap, or fail policy
      is refused with an actionable message
- [ ] A failing config pauses the rollout, poisons the revision, and does not retry
- [ ] Rollback is one action and reaches `APPLIED` on every targeted agent
- [ ] `README.md`, `.env.sample`, `docker-compose.yml` and `GET /health` all describe the
      real security posture
"""
)

WI = []


def wi(title, labels, body):
    WI.append((title, labels, body))


wi(
    "Security: authenticate agents on the OpAMP channel (identity from credential, not message body)",
    ["security", "config-push", "phase-1-security"],
    """## Summary

`/v1/opamp` (HTTP and WebSocket) has no client authentication at all. Any client that can
reach port 4320 can present any `instance_uid` and be believed (`server/main.py`
`_handle_agent_message`, `server/ws.py`). Today that mostly corrupts inventory. Once config
push exists it means:

- an impostor claiming a real agent's uid **receives that agent's desired config** (which may
  contain credentials),
- a second connection claiming a live uid **takes over its WebSocket binding** — `bind()`
  currently logs `"replacement semantics belong to #75"` and moves the binding,
- the impostor can **suppress a rollout** by reporting health/`APPLIED` on the real agent's
  behalf, or poison its state.

So this is not just hygiene: it is the precondition for pushing config to anyone at all.

## Design

Credential is the identity. `AgentToServer.instance_uid` is a *claim* that must match the
principal that presented the credential.

**Modes** — `OPAMP_AGENT_AUTH=off|token|mtls` (default `off`, with a startup warning when
`OPAMP_CONFIG_PUSH_ENABLED=true` and mode is `off` — #3 makes that combination refuse to boot):

1. **`token`** (v1 target). Per-agent opaque token, sent as a header. Both transports support
   headers on the collector side (`server.ws.headers` / `server.http.headers`), or an `auth`
   extension — **verify which works on the target distribution before committing to it**,
   this is the one risky assumption in the plan.
   - `agent_credentials(agent_id, token_hash, label, created_at, last_seen, revoked_at)`,
     SHA-256 at rest, never logged, `hmac.compare_digest` on check.
   - Bootstrap: `OPAMP_AGENT_ENROLLMENT_TOKEN` lets a new agent register once; the server
     issues a per-agent token in `AgentIdentification`/connection settings (coordinate with
     #83 — do not build a second registration flow).
   - Rotation and revocation endpoints (`POST /agents/{id}/token`, `DELETE .../token`),
     both audited (#4).
2. **`mtls`** (v2). Client cert CN/SAN → agent identity. Borrow the CA work from #83 rather
   than inventing PKI here.

**Enforcement points** (all of them, not just the front door):

- HTTP: reject with `401` *before* parsing the protobuf; an unauthenticated client must not
  receive a `ServerToAgent` advertising capabilities.
- WS: reject the upgrade (`1008`) or close on first unauthenticated frame; `Origin` logic from
  #89 stays.
- Binding: on WS, refuse to move a uid to a connection with a different principal (close
  `1008`); on HTTP, refuse a message whose uid is bound to a different principal.
- Reject *before* `_apply_status_report`, so an impostor cannot mutate state even briefly.

**Non-goals:** authorisation of *what* an agent may report (out of scope), token scopes.

**Migration:** existing agents keep working with `off`. Document the cutover: enable `token`,
enrol agents, watch `opamp_agent_auth_failures_total`, then set `OPAMP_AGENT_AUTH=token`.
A revoked agent's desired config is **retained** (revocation blocks delivery; it is not a
config wipe) — state this explicitly in the docs, because it is the opposite of what people
expect.

## Work

1. Credential store + migration in `server/state.py` (`SQLiteAgentStore` pattern, WAL).
2. Auth dependency shared by the HTTP route and the WS handshake; put it next to `require_admin`
   and document it in the route docstrings (repo convention).
3. Principal ↔ `instance_uid` binding in `server/ws.py::ConnectionRegistry` and a parallel
   HTTP-side check; delete the "replacement semantics belong to #75" TODO by making it an
   authenticated decision (coordinate with #75, which handles *duplicate* legitimate agents).
4. Metrics: `opamp_agent_auth_failures_total{reason}`; audit each failure (#4).
5. `GET /agents` shows credential state (`enrolled`, `last_seen`, `revoked`) so an operator can
   see the fleet's cutover progress; body never exposes the token or its hash.
6. `.env.sample`, README auth/transports section, docker-compose template.

## Acceptance criteria

- [ ] With `OPAMP_AGENT_AUTH=token`, a request with no token / a bad token / a revoked token
      gets `401` (HTTP) or `1008` (WS) and **no** `ServerToAgent` body
- [ ] A valid token claiming a different agent's `instance_uid` is refused (HTTP 403 / WS 1008)
      and audited; the victim agent's state is untouched
- [ ] Opening a second WS connection and claiming a live uid with a different credential is
      refused; with the *same* credential (legitimate reconnect) the binding moves cleanly
- [ ] Token rotation works without downtime for a single agent; revocation blocks delivery
      while retaining desired config
- [ ] Tokens never appear in logs, `/agents`, audit entries, or error messages
- [ ] `OPAMP_AGENT_AUTH=off` logs a startup warning; combined with config push it fails to start
      (with #3)
- [ ] Tests cover each rejection path and the binding takeover attempt

## Metadata

- Phase: `1-security` · Priority: 1 · Effort: L · Blocks: #6 #7 #8 #9 #10
- Depends on: #3 (posture), #75 (duplicate-uid semantics), optional #83 (mtls/CSR later)
""" + AI_NOTE,
)

wi(
    "Security: harden admin authentication (roles, session tokens, constant-time compare, lockout)",
    ["security", "config-push", "phase-1-security"],
    """## Summary

The admin surface is one shared secret and it is weak in four ways:

1. **`ADMIN_PASSWORD` unset ⇒ auth disabled** (`server/main.py::require_admin` returns `True`).
   `docker-compose.yml` ships `ADMIN_PASSWORD=${ADMIN_PASSWORD:-}` — the documented default is
   *open*. Acceptable for a read-only dashboard; unacceptable once there is a push endpoint.
2. **No identity.** Every admin action is anonymous; audit (#4) would record "admin".
3. **Brute-forceable.** `password == ADMIN_PASSWORD` is not constant-time, there is no rate
   limit or lockout, and `GET /auth/verify` is a clean oracle for credential guessing.
4. **No roles.** Reading alerts and pushing a fleet config are the same privilege.

## Design

- **Roles:** `viewer` (reads, redacted), `operator` (propose, validate, apply approved
  revisions, abort), `approver` (approve/reject, rollback), `admin` (everything, incl. tokens
  and audit export). Approval and application are deliberately different roles — that is what
  makes #6's two-phase flow meaningful rather than theatre.
- **Principal store:** `admin_principals(id, name, role, secret_hash, created_at, expires_at,
  revoked_at, last_seen)`. When only `ADMIN_PASSWORD` is set, synthesise a single `admin`
  principal for backwards compatibility and log a deprecation warning.
- **Sessions:** `POST /auth/session {password|token}` → opaque random token (32 bytes),
  SHA-256 at rest, TTL `ADMIN_SESSION_TTL_SECONDS` (default 3600), idle timeout, revocable,
  `Authorization: Bearer <token>`. HTTP Basic stays supported for scripts but is documented as
  deprecated for interactive use.
- **Lockout:** `ADMIN_LOGIN_MAX_ATTEMPTS` (default 5) per `(identity, client IP)` with
  exponential backoff and `Retry-After`; failures do not distinguish "no such user" from "bad
  secret". Metric `opamp_admin_auth_failures_total{reason}` and an audit entry per failure.
- **Timing:** `hmac.compare_digest` everywhere; hash the presented secret before comparing so
  length is not leaked.
- **Proxy awareness:** if the server sits behind a proxy, trust `X-Forwarded-For` only when
  `TRUST_PROXY_HEADERS=true`; otherwise lockout is bypassable and IP logging is a lie.
- **Startup checks:** warn on a password shorter than 12 chars and on `ADMIN_PASSWORD` equal to
  a known placeholder; with config push on, refuse (see #3).

## Work

1. `server/auth.py` — principals, sessions, roles, `require_role()` FastAPI dependency
   replacing the current `require_admin()` helper; keep `require_admin` as an alias so the
   Streamlit page and existing tests do not all break at once.
2. Session endpoints + logout + "list my sessions"/revoke.
3. Apply role checks to existing admin routes (`/alerts*`, `/compliance/check`,
   `/compliance/reload`) as `admin`/`operator` as appropriate; document each in its docstring
   (auth is invisible to OpenAPI, see memory notes).
4. Rate limiting middleware for auth endpoints; return 429 with `Retry-After`.
5. `.env.sample` + README auth section rewrite (include the "auth disabled" warning in bold).

## Acceptance criteria

- [ ] Password → session token → call works; token expires; logout revokes; revoked token 401s
- [ ] `operator` cannot approve or roll back; `viewer` cannot propose; `approver` cannot mint
      tokens — each returns 403 with a clear message
- [ ] N failed logins from one identity/IP trigger 429 with `Retry-After`; a successful login
      resets the counter; the lockout is audited
- [ ] Credential comparison is constant-time (assert via code review + unit test with equal-length
      and different-length inputs)
- [ ] With only `ADMIN_PASSWORD` set, the server still works as one `admin` principal and warns
      about the deprecated single-secret mode
- [ ] `GET /auth/status` reports the auth mode and available roles without leaking secrets
- [ ] Tests for each role/endpoint combination

## Metadata

- Phase: `1-security` · Priority: 1 · Effort: M · Blocks: #4 #6 #10 #12
- Depends on: nothing (start immediately)
""" + AI_NOTE,
)

wi(
    "Security: fail-closed enablement and secure-by-default posture for config push",
    ["security", "config-push", "phase-1-security"],
    """## Summary

The dangerous default here is not a bug in a code path — it is a **combination** of defaults:
auth is disabled when `ADMIN_PASSWORD` is empty, the server speaks plain HTTP, agents are
unauthenticated, and `docker-compose.yml` publishes `4320:4320`. Adding a push endpoint to that
stack is fleet takeover for anyone on the network.

This issue makes the insecure combination impossible to start by accident, and makes the real
posture visible everywhere.

## Design

**Feature flag, off by default.** `OPAMP_CONFIG_PUSH_ENABLED=false` (spec recommends remote
config be opt-in — `specification.md:3644`). When `false`, the routes exist but return 404 with
`"config push is disabled"` (not 403 — do not confirm the surface to a stranger) and no
`remote_config` field is ever sent.

**Startup posture check.** With `OPAMP_CONFIG_PUSH_ENABLED=true`, refuse to start unless:

| Requirement | Config |
|---|---|
| Admin auth is actually configured | `ADMIN_PASSWORD` set, or at least one `admin_principals` row |
| Agents are authenticated | `OPAMP_AGENT_AUTH` in `token`/`mtls` (#1) |
| The server is served over TLS | `PUBLIC_URL` starts with `https://` / `wss://`, or `TLS_TERMINATED_UPSTREAM=true` with the proxy documented |

Override for local development only: `OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true`. That prints a
multi-line red warning at startup, sets `insecure: true` in `/health`, and renders a permanent
banner in the UI (#11). It should feel bad to use.

**Posture visibility.**

- `GET /health` gains `"config_push": {"enabled": bool, "insecure": bool,
  "agent_auth": "off|token|mtls", "audit": bool, "tls": bool}`.
- `GET /auth/status` gains `auth_mode` and `roles` (no secrets).
- `GET /config/posture` (or fold into `/health`) so the UI can render the banner without
  guessing from 404s.

**Deployment artefacts.**

- `docker-compose.yml`: template with `ADMIN_PASSWORD` required (compose `${VAR:?}` syntax),
  agent auth on, and the port bound to `127.0.0.1` by default with a comment explaining how to
  expose it safely.
- `docker/nginx.conf` + `docker/Caddyfile` examples showing TLS termination and `wss`
  upgrade passthrough (`proxy_set_header Upgrade/Connection`, long read timeout for
  long-lived sockets — call out the keepalive interaction with #89).
- README: a short, blunt "Before you expose this server" section. Also note the uvicorn
  `--ws-max-size` caveat already in the README.
- `.env.sample`: every new variable with a one-line explanation.

## Acceptance criteria

- [ ] Fresh `docker compose up` with no `.env` produces a server whose `/health` shows
      `config_push.enabled == false` and whose push routes 404
- [ ] Setting `OPAMP_CONFIG_PUSH_ENABLED=true` without admin auth / agent auth / TLS exits
      non-zero with an error naming every missing requirement
- [ ] `OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true` starts, warns loudly, reports `insecure: true`,
      and the UI shows a banner
- [ ] Compose templates cannot silently start with an empty admin password
- [ ] TLS example actually works: browser → nginx → server over http, WS upgrade works, and the
      `Origin` check (#89) still applies
- [ ] README and `.env.sample` document every flag added by this issue and by #1/#2/#4/#5

## Metadata

- Phase: `1-security` · Priority: 1 · Effort: S · Blocks: #1 #6 #10 #11
- Depends on: #1 and #2 for the requirements it enforces (build the check last, or behind a
  flag, but do it first if you want the guard in place early)
""" + AI_NOTE,
)

wi(
    "Security: append-only, tamper-evident audit log for privileged actions",
    ["security", "config-push", "phase-1-security"],
    """## Summary

Nothing in this server records who changed what. For a read-only dashboard that is tolerable.
For "this button rewrites the config of 400 collectors" it is not: after an incident you cannot
answer *who pushed it, when, and what exactly*.

## Design

**One table, hash-chained.** `audit_log`:

```
id            INTEGER PRIMARY KEY AUTOINCREMENT
ts            TEXT     NOT NULL   -- ISO-8601 UTC
actor         TEXT     NOT NULL   -- principal name, or "agent:<agent_id>", or "anonymous"
actor_kind    TEXT     NOT NULL   -- human | agent | system
role          TEXT                -- role used for the action
action        TEXT     NOT NULL   -- config.propose | config.approve | config.apply | ...
target_kind   TEXT                -- agent | revision | rollout | principal | server
target_id     TEXT
revision_id   TEXT
config_hash   TEXT                -- hex, never the body
result        TEXT     NOT NULL   -- ok | denied | error
detail_json   TEXT                -- redacted, small: counts, error text, validation verdicts
prev_hash     TEXT                -- entry_hash of the previous row
entry_hash    TEXT     NOT NULL   -- sha256 over canonical JSON of the row (sans entry_hash)
```

`entry_hash` chaining makes silent edits detectable without DB write access. Be honest in the
docs about what it does *not* do: an attacker with write access can rewrite the chain from the
start; ship entries off-box (CLI export, webhook) if you need real assurance.

**Rules.**

- **No bodies, no secrets.** Hashes, sizes, counts and verdict codes only. `detail_json` passes
  through the same redaction filter as #5.
- **Fail closed.** If the audit write fails, the privileged action fails. A config push that
  cannot be recorded must not happen. (Existing alert-config writes keep best-effort semantics;
  only the config-push and auth paths are fail-closed.)
- **Auto events, not opt-in calls.** Emit from the dependency/middleware layer so a new endpoint
  cannot forget. Unit-test that every write route emits an event.

**Events to cover:** login success/failure/lockout, session create/revoke, principal
create/role-change/revoke, agent token issue/rotate/revoke, agent auth failure, config
propose/validate/approve/reject/apply/abort/rollback/withdraw, rollout promotion/pause,
protected-invariant rejection, posture change (insecure override), DB migration.

**Read/export.** `GET /audit?since=&until=&actor=&action=&target=&result=` (admin, paginated,
newest first) and `GET /audit/export?format=jsonl|csv` for SIEM ingestion. `opampctl audit` and
`--db` offline read. Retention `AUDIT_RETENTION_DAYS` (default 365) with a checkpoint entry
recording each compaction so the chain stays verifiable.

## Work

1. Table + migration + `SQLiteAuditStore` in `server/state.py` (best-effort *reads*, fail-closed
   *writes*; WAL + `busy_timeout` since the CLI can read concurrently).
2. `audit_event()` helper + FastAPI dependency so routes declare `audit="config.apply"`.
3. Chain verification helper + `GET /audit/verify` (admin) — cheap, and it makes tampering
   visible when someone actually asks.
4. Retention job in the existing `periodic_cleanup` loop.
5. Redaction hook shared with #5 (build #5's filter first if convenient).

## Acceptance criteria

- [ ] Every privileged action from #2/#6/#10 produces exactly one audit row naming the principal
- [ ] Forcing the audit insert to fail makes the action 500 and the change does **not** happen
- [ ] Config bodies never appear in any column (test with a config containing a sentinel secret)
- [ ] `GET /audit` filters work; export downloads valid JSONL/CSV
- [ ] `GET /audit/verify` detects a hand-edited `detail_json` (test mutates a row directly)
- [ ] Retention deletes old rows and keeps the chain verifiable
- [ ] Offline `opampctl --db audit` shows the same rows

## Metadata

- Phase: `1-security` · Priority: 2 · Effort: M · Blocks: #6 #10
- Depends on: #2 (principals give the actor a name), #5 (redaction)
""" + AI_NOTE,
)

wi(
    "Security: keep config secrets out of logs, API responses, reports and the UI; encrypt config at rest",
    ["security", "config-push", "phase-1-security"],
    """## Summary

Desired configs are the most sensitive data this server will ever hold: exporter auth headers,
basic-auth extensions, TLS keys, internal endpoints. Today:

- `effective_config` is stored as **plaintext JSON** in `data/opamp.db` (`server/state.py`).
- The collector can report a **raw** config that upstream explicitly warns may expose secrets
  (`reports_raw_config` in `extensions/opampextension/README.md`: *"The raw configuration may
  expose secrets… only enable it if you trust the OpAMP server with the full contents of your
  configuration files"*).
- API responses return that blob verbatim to any caller.
- Nothing stops a future `logger.info(config)` from putting it in a log file.

Once the server *stores* configs it becomes a secrets store, and it should behave like one.

## Design

**One redaction filter, used everywhere.** Match on key names, case-insensitive, at any depth:
`password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `authorization`, `private_key`,
`client_secret`, `bearer`, `access_key`. Plus value-shape heuristics for `headers:` maps
(`Authorization: Basic …`). Replace with `"<redacted>"`, and mark the containing document
`redacted: true` with a list of redacted paths so consumers know.

**Where it applies:** loguru sink filter (a `record["extra"]` sanitiser, plus a unit test that
asserts a sentinel never reaches a captured sink), `/agents`, `/agent/{id}`,
`/agent/{id}/config`, reports (`server/reports.py`), alert payloads (`server/alerts.py`),
audit `detail_json`, CLI/MCP output, and the UI.

**Never re-push a redacted body.** A redacted config is a *display artefact*. It must be
impossible to save one as a revision: tag the payload with an unguessable marker and have the
propose endpoint reject any body containing `"<redacted>"` or the marker. This is the single
most dangerous foot-gun in the whole feature — a redacted push silently wipes credentials
across the fleet.

**Encryption at rest.** `CONFIG_AT_REST_KEY` (32 bytes, base64) → AES-256-GCM per body, AAD =
`revision_id`. Fallback when unset: refuse config push unless
`OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE=true` (dev), because silently degrading here is how
secrets end up in a backup. Document rotation (re-encrypt-on-write migration job) and the
honest failure mode: **losing the key loses desired config, not the fleet** — agents keep
running their last applied config and the server can recover from the agent's reported
effective config. Say that in the docs; operators will ask.

**Retention.** Config bodies are kept only while referenced by an assignment plus a short
rollback window (`CONFIG_RETENTION_DAYS`, default 30); superseded revisions keep hash +
metadata. Smaller blast radius, and it makes "delete my config" answerable.

## Work

1. `server/redaction.py` — filter + `redact_document()` + path list; property-style tests.
2. Apply in `AgentState.to_dict()` (single choke point for the API), reports, alerts, logs.
3. `server/crypto.py` — AES-GCM helpers with a key-version prefix for rotation; SQLite column
   type change + migration (`config_blob BLOB, key_version INT`).
4. UI: masked rendering + audited "reveal" (needs `secret:read`, recorded in #4).
5. README: "Config data and secrets" section, including the `reports_raw_config` upstream warning
   and a recommendation to leave it off.

## Acceptance criteria

- [ ] A config containing `password`, `api_key` and an `Authorization` header renders redacted in
      `/agents`, `/agent/{id}`, reports, alert payloads, CLI, MCP and the UI
- [ ] A captured loguru sink never contains the sentinel secret, at any log level, including
      exception traces
- [ ] Proposing a body containing `"<redacted>"` is rejected with a clear error
- [ ] DB file inspected with `sqlite3` contains no sentinel secret when encryption is enabled
- [ ] Starting with config push on and no `CONFIG_AT_REST_KEY` fails unless the explicit
      plaintext opt-out is set, and that opt-out is reported in `/health`
- [ ] Key rotation re-encrypts existing rows and old-key rows fail closed with a clear error
- [ ] Retention deletes old bodies while keeping hash/metadata and leaving assignments intact

## Metadata

- Phase: `1-security` · Priority: 2 · Effort: M · Blocks: #6 #10 #11
- Depends on: nothing. Land the redaction filter early; it is independently useful.
""" + AI_NOTE,
)

wi(
    "Control plane: config revision store and lifecycle (propose → approve → active → rollback)",
    ["config-push", "phase-2-control-plane"],
    """## Summary

#81 covers the *wire*: computing the hash, comparing it with `RemoteConfigStatus`, and putting
`remote_config` on the response. This issue is the **control plane** around it — the part that
makes it safe to operate:

- immutable revisions with an approval record,
- which agent is assigned which revision, and what happened when it was delivered,
- rollback as a first-class operation,
- poisoning rules so a failed revision is never re-delivered (see T9 in the epic).

## Data model

```sql
config_revisions(
  id TEXT PRIMARY KEY,            -- UUIDv7 (reuse #73's generator)
  scope_kind TEXT NOT NULL,       -- agent | selector | fleet
  scope_json TEXT NOT NULL,       -- {"agent_id": ...} or resolved filter set
  config_map_json BLOB NOT NULL,  -- encrypted (#5): one or more objects {key: {body, content_type, role}}
  config_hash TEXT NOT NULL,      -- spec §Calculating Hashes (#81)
  author TEXT NOT NULL,
  note TEXT,
  status TEXT NOT NULL,           -- draft|proposed|approved|rejected|active|superseded|rolled_back|poisoned
  approved_by TEXT, approved_at TEXT,
  parent_revision_id TEXT,        -- rollback / supersede chain
  created_at TEXT NOT NULL
)

config_assignments(
  agent_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL,
  delivered_hash TEXT,            -- what we last put on the wire
  delivered_at TEXT,
  last_status TEXT,               -- UNSET|APPLYING|APPLIED|FAILED (+ auto-reverted)
  last_error TEXT,
  attempts INTEGER DEFAULT 0,
  poisoned INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL
)

config_rollouts(id, revision_id, state, canary_percent, max_batch, created_by, created_at,
                aborted_at, paused_reason)
```

Two tables and a rollout row, not a generic workflow engine. If this starts to look like a
state machine library, stop.

## Lifecycle rules (each is an acceptance criterion)

1. **Immutable.** After `approved`, a revision's content never changes. New content = new
   revision. Rollback = new revision whose body is the parent's, `parent_revision_id` set, note
   recording why.
2. **Approve is human** (#2 role `approver`), recorded with principal + timestamp, audited (#4).
   Reject keeps the revision for the record.
3. **One active revision per agent.** Approving a new one supersedes the previous; the previous
   stays available for rollback within `CONFIG_RETENTION_DAYS`.
4. **Selector scope resolves at approve time** and the resolved agent list is stored on the
   revision. Later-joining agents do **not** silently inherit it unless the operator ticked
   "apply to future matches" (stored as `scope_json.include_future: true`). Silent inheritance
   is how a fleet-wide config reaches a machine nobody thought about.
5. **Poisoning.** `FAILED` status, or a detected auto-revert (agent reports a hash that is
   neither ours nor `UNSET`, see below), sets `poisoned = 1`, surfaces loudly, and **stops all
   delivery** of that revision. Reactivation requires explicit re-approval by an `approver`.
6. **Withdraw.** `DELETE /agent/{id}/config` clears the assignment (per #81) and records a
   withdraw revision event. History stays.
7. **Agent lifecycle.** Stale-removal must **not** delete assignments (so a restarted agent gets
   its config back). Re-registration with the same uid inherits the assignment. A *new* uid
   (cloned VM, uid reissue from #75) inherits **nothing** — that is the correct, safe default and
   it also prevents config leaking to a lookalike.

## The auto-revert trap (T9) — read this before implementing

The OpAMP Supervisor persists `last_working_remote_config.dat` and reverts to it when the new
config fails (`cmd/opampsupervisor/supervisor/supervisor.go`:
`restoreLastWorkingRemoteConfig`, `shouldReportLastWorkingRemoteConfigStatus`). So the agent can
report an `APPLIED` status whose `last_remote_config_hash` is the **last working** config, not
the one you pushed. A naive #81 comparison then sees "hash differs → send again" and you have an
infinite push/revert loop against a production fleet.

Rules: record `delivered_hash` per agent; if the reported hash is a *known older revision* for
this agent (not ours), classify as **auto-reverted**, poison, and stop. Only ever re-deliver when
the agent's reported hash is `UNSET`/empty *and* we have not poisoned the revision.

## Work

1. Schema + migration; `SQLiteConfigStore` next to the other stores.
2. Lifecycle transitions as small functions with guards (no direct SQL from routes).
3. Drift detection: `desired.config_hash != effective_config_hash` → `drift: true` in
   `/agent/{id}/config` (and a `config_drift` alert event in `server/alerts.py`).
4. Rollback + withdraw + poison/reactivate.
5. Admin surfaces to inspect history (`GET /config/revisions?agent=`, `GET /agent/{id}/config`).

## Acceptance criteria

- [ ] propose → approve → delivered → `APPLIED` with a matching hash; assignment reflects it
- [ ] Approving a second revision supersedes the first and keeps it rollback-able
- [ ] Rollback reaches `APPLIED` on every targeted agent and records the chain
- [ ] An agent reporting a *last-working* hash after a failed push is classified as auto-reverted,
      the revision is poisoned, and no further frames are sent (assert over 5 simulated polls)
- [ ] Stale-agent cleanup keeps the assignment; the agent gets the same config on reconnect
- [ ] A new uid never inherits an assignment
- [ ] Selector scope resolves at approve time and does not capture later agents unless opted in
- [ ] Drift is visible per agent and fires one `config_drift` alert (not one per poll)
- [ ] Server restart mid-rollout resumes from persisted state without double-delivering

## Metadata

- Phase: `2-control-plane` · Priority: 2 · Effort: L · Blocks: #7 #8 #10
- Depends on: #1, #2, #4, #5, #66 (multi-object config), #73 (UUIDv7), #81 (wire/hash)
""" + AI_NOTE,
)

wi(
    "Control plane: pre-push validation and policy gate (structural, protected invariants, OPA)",
    ["security", "config-push", "phase-2-control-plane"],
    """## Summary

The server is about to send YAML that a remote process will boot from. Upstream is explicit
about the failure mode (`extensions/opampextension/README.md`):

> *an invalid config will cause the Collector to error out and stay down until it is manually
> restarted using some other mechanism (since the OpAMP extension won't be active). Regular
> healthchecks and using the Collector's `validate` subcommand to validate config before
> applying it are **highly recommended**.*

So validation is not a nicety — but be honest about its limits: **the server cannot prove a
config will not crash a collector.** It can catch structural mistakes, policy violations and
self-inflicted unreachability. The rest is #8 (canary + health gate) and the supervisor's own
auto-revert.

## Pipeline (ordered, all server-side, re-run at delivery time)

Validation runs at **propose** and again at **apply**. Configs approved on Monday must not be
delivered on Friday without re-checking: the schema, the policies and the agent's component
inventory may all have changed.

1. **Size** — `CONFIG_MAX_BYTES` (default 1 MiB) per object and per revision. Reject early, before
   parsing (the existing `OPAMP_MAX_MESSAGE_BYTES` ceiling applies on the wire; this is about
   storage and UI sanity).
2. **Decode + parse** — UTF-8 strict, `yaml.safe_load` only. Alias/depth guard: a small alias
   budget (e.g. reject > 10k aliases or > 50 levels) to stop a memory bomb; test it.
3. **Structural rules** — top-level mapping; `service` mapping; `service.pipelines` non-empty and
   every pipeline has ≥1 receiver and ≥1 exporter; every referenced
   `receivers|processors|exporters|extensions|connectors` id exists in the corresponding top-level
   section; no duplicate keys (PyYAML silently takes the last — reject instead).
4. **Protected invariants (D7)** — the pushed config must keep:
   - an `extensions.opamp` section **and** `opamp` listed in `service.extensions`
     (otherwise the agent loses its control channel and can never be fixed remotely),
   - the `opamp` endpoint pointing at *this* server, unless the request explicitly sets
     `allow_endpoint_change: true` **and** the approver confirmed it (coordinate with #83; moving
     an endpoint is exactly the chicken-and-egg outage that issue warns about).
   Also warn on: `service.telemetry.logs` removed, `memory_limiter` removed, no `batch` processor,
   and any exporter URL that is not on `CONFIG_ALLOWED_EXPORTER_HOSTS` (if set).
5. **Policy gate** — run the project's OPA policies against the *proposed* config (the differentiator
   #81 flagged). Violations block by default; `approver` may override with a recorded reason.
   Skipped with a warning when OPA is disabled — do not silently pretend it passed.
6. **Verdict** — `{ok, errors: [{path, message, hint}], warnings: [...]}`. Paths are YAML paths
   (`service.pipelines.traces.receivers[0]`) so the UI/CLI can point at the line. Store the
   verdict on the revision for the audit trail.

## Work

1. `server/config_validation.py` — pure functions, no FastAPI, so the CLI `--db` mode and tests can
   use them too (mirrors `server/manifest.py` and `server/reports.py`).
2. Reuse `parse_effective_config()`/`AgentState.components` to warn when the pushed config
   references a component the agent does not have available (`available_components`) — catches the
   "you pushed a config for a distro that doesn't ship `redismetricsreceiver`" class of outage.
3. API surface: validate endpoint returning the structured verdict (see #10).
4. Tests with corpus files in `tests/fixtures/configs/`: valid, invalid YAML, alias bomb, missing
   component section, opamp extension removed, opamp endpoint changed, oversized, policy violation.

## Acceptance criteria

- [ ] Each pipeline stage has a failing fixture and a test asserting the structured error path
- [ ] Removing `extensions.opamp` or `service.extensions: [opamp]` is refused with an explanation
- [ ] Changing the opamp endpoint is refused unless explicitly allowed and approver-confirmed
- [ ] Alias bomb is rejected in bounded time and memory (assert a wall-clock ceiling)
- [ ] Duplicate YAML keys are rejected (not silently last-wins)
- [ ] A policy violation blocks apply; an approver override requires a recorded reason and is audited
- [ ] Validation is re-run at apply time; if the verdict changed, the apply is refused with
      "re-validate: policy/inventory changed"
- [ ] Warnings (unavailable component, missing memory_limiter) are shown but do not block

## Metadata

- Phase: `2-control-plane` · Priority: 2 · Effort: M · Blocks: #8 #10 #11
- Depends on: #6 (revisions to attach a verdict to), #7's policy input from `server/opa_client.py`
""" + AI_NOTE,
)

wi(
    "Control plane: staged rollout engine with health gate, blast-radius caps and auto-pause",
    ["config-push", "phase-2-control-plane"],
    """## Summary

The failure mode nobody defends against in config-push features: one typo, 400 collectors down,
and because their `opamp` extension is not running you have no remote way to fix them. Upstream
confirms the collector stays down on a bad config. So the server must never move from one agent
to two hundred without evidence that agent one came back healthy.

This engine owns **who gets it next and whether we may continue**. It does not own the wire
(#81) or the transport (#9).

## Model

Per assignment: `pending → delivered → applying → applied | failed | reverted | paused`.

Promotion gate for a batch (all must hold):

- every agent in the batch reports `remote_config_status == APPLIED` **with our hash** (#6's
  poisoning rules apply, not naive equality), and
- every agent in the batch is `healthy == true`, and
- no agent in the batch went stale since delivery, and
- `CONFIG_PUSH_HEALTH_GATE_SECONDS` (default 90) has elapsed since the last delivery in the batch.

Batches: canary = `CONFIG_PUSH_CANARY_PERCENT` (default 5, minimum 1 agent), then fixed steps
bounded by `CONFIG_PUSH_MAX_AGENTS_PER_BATCH` (default 25) and
`CONFIG_PUSH_MAX_PERCENT_PER_BATCH` (default 25). Deterministic order, recorded, so a rollout is
reproducible in tests.

On failure: **pause, do not auto-rollback the fleet.** Rolling back 300 agents because one failed
is a second outage. Surface "N applied, K failed, paused at batch 2" with two buttons: *retry
failed agents* (only after re-approval, see #6) and *roll back applied agents to the previous
revision*. Rollback runs as its own rollout with the same gating.

## Work

1. Rollout row + per-batch state transitions; a single `asyncio` task (or the existing
   `periodic_cleanup` tick) that advances rollouts — not a thread per rollout.
2. Gates as pure functions over agent state so they are unit-testable without a clock.
3. Caps and rate limits: `CONFIG_PUSH_MAX_CONCURRENT` deliveries, minimum interval between
   batches; never exceed the WS outbound queue depth from #89 (a full queue drops messages —
   a dropped config frame must be retried by reconciliation, not lost silently).
4. Abort: `POST /config/rollouts/{id}/abort` stops promotion immediately. Document that it does
   **not** recall already-delivered configs (it cannot).
5. TTL: `CONFIG_PUSH_ROLLOUT_TTL_MINUTES` (default 60). An approval that nobody acted on must not
   fire three weeks later against a fleet that has moved on. Expired → cancelled + audited.
6. Observability: `opamp_config_rollout_agents_total{state}`, `opamp_config_rollout_paused_total
   {reason}`; one structured log line per batch transition.

## Acceptance criteria

- [ ] Canary single agent: patch applies, health green, gate opens, batch 2 proceeds
- [ ] Canary reports `FAILED` → rollout pauses, no further delivery, revision poisoned, nothing
      auto-retried
- [ ] Canary goes stale/dead after delivery → gate never opens; rollout pauses with a distinct
      reason ("agent went silent")
- [ ] Agent reports a last-working hash (auto-revert) → pause + poison, no loop (assert over time)
- [ ] Caps are honoured: a 500-agent rollout never exceeds the configured batch/concurrency limits
- [ ] Abort stops promotion within one tick and leaves already-applied agents untouched
- [ ] Rollback rollout reaches `APPLIED` on all previously-applied agents
- [ ] TTL expiry cancels a stalled rollout and audits the reason
- [ ] Deterministic batch ordering (same seed/targets → same batches)

## Metadata

- Phase: `2-control-plane` · Priority: 2 · Effort: L · Blocks: #11
- Depends on: #6, #7, #9, #84 (restart command, for configs that need a restart to take effect)
""" + AI_NOTE,
)

wi(
    "Delivery: unsolicited server→agent push over WebSocket + poll queue for HTTP agents",
    ["config-push", "phase-2-control-plane"],
    """## Summary

#89 built a *reply* path: `server/ws.py` reads a frame, calls the shared handler, and enqueues the
response. There is no way for the server to send an unsolicited message. Without one, "push now"
is impossible and the rollout engine (#8) can only nudge agents and wait for the next poll.

This issue adds exactly one capability — **send to an agent's live connection** — and defines
delivery semantics for both transports. Note the collector's opamp extension now defaults to
**WebSocket** (`server.ws.endpoint`), so this is the primary path, not an optimisation.

## Design

**WS push path.**

```python
# server/ws.py
async def send_to_agent(uid: bytes, payload: bytes) -> bool:
    '''Enqueue an unsolicited ServerToAgent frame onto the uid's connection.'''
```

- Registry currently stores `Dict[bytes, int]`; add a per-connection outbound queue handle and an
  `asyncio.Lock` (the registry is a module global touched from multiple tasks).
- Reuse `_enqueue` so backpressure behaviour is identical to replies: drop-oldest, count drops.
  **But** a dropped config frame must not be lost: reconciliation (#81/#6) re-sends on the next
  message, so record `delivered_hash` only when the frame is actually written, not when queued.
  Simplest correct approach: hand the writer a callback, or mark the assignment
  `delivery_attempted` and let reconciliation settle it.
- Multi-agent sockets (spec §WebSocket Message Exchange): address by uid, never "the agent on this
  socket". A uid bound to a different principal must be refused (#1).
- Return `False` when there is no live connection → the caller falls back to the poll queue.

**HTTP agents.** Nothing at the wire level: the desired config rides on the next response (#81).
Record `delivered_at` when it is included so the UI can honestly say *"queued — will be delivered
within one polling interval (30 s default)"* instead of pretending it is instant. Never call it
"applied" until the agent says `APPLIED`.

**Ordering and duplicates.** One writer task per connection already guarantees frame order
(#89). Config frames must never overtake each other: latest revision wins, and an in-flight older
revision is superseded rather than queued twice.

**Rate limiting.** `CONFIG_PUSH_MAX_CONCURRENT` sends in flight; a token bucket per connection so a
large rollout cannot starve heartbeats on a shared socket.

## Work

1. `ConnectionRegistry` gains queue handles + principal binding; `send_to_agent()`.
2. `serve()` exposes a handle (or registers it in the registry) so the push path can enqueue.
3. Handler hook: `ProcessedMessage` gains an optional post-reply action so a rollout can react to
   a state transition (e.g. promotion) rather than racing the reply.
4. Metrics: `opamp_ws_push_sent_total{outcome}`, `opamp_config_delivery_latency_seconds`
   (delivered → `APPLIED`).
5. Tests in `tests/test_ws.py`: unsolicited send arrives, ordering preserved, full queue behaviour,
   offline uid returns `False`, push to a foreign principal refused, concurrent rollouts.

## Acceptance criteria

- [ ] A connected agent with no pending messages receives an unsolicited config frame within
      one event-loop tick of `send_to_agent()`
- [ ] Sending to an offline uid returns `False` (no exception, no unbounded buffering)
- [ ] Two concurrent rollouts to the same socket do not interleave frames or duplicate a revision
- [ ] A full outbound queue is observable and the config is re-delivered by reconciliation rather
      than silently lost (test fills the queue)
- [ ] HTTP-only agents still work; the UI distinguishes "delivered" from "queued for next poll"
- [ ] Pushing to a uid bound to a different credential is refused and audited
- [ ] Heartbeats are not starved during a 100-agent rollout (latency assertion)

## Metadata

- Phase: `2-control-plane` · Priority: 2 · Effort: M · Blocks: #8 #11
- Depends on: #89 (done), #1 (principal binding), #81 (response logic)
""" + AI_NOTE,
)

wi(
    "API: config push endpoints (propose / validate / approve / apply / rollback / withdraw / status)",
    ["config-push", "phase-3-api"],
    """## Summary

The HTTP surface for the flow the rest of the epic defines. Every route follows the repo
convention: `tags=["config"]`, a docstring that states the auth requirement (auth is invisible to
OpenAPI — see `memory.md`), and a summary.

The rule that makes this safe: **there is no single "push config" endpoint.** There is a propose
step, an approve step, and an apply step, and they are different roles (#2).

## Routes

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/config/revisions` | operator | Propose: scope + config map + note → revision, hash, verdict |
| POST | `/config/revisions/{id}/validate` | operator | Re-run validation (schema/policy/inventory) without changing the revision |
| GET | `/config/revisions` | viewer | List, newest first; bodies redacted, hashes + status + author + note |
| GET | `/config/revisions/{id}` | viewer | Detail; body only with `secret:read`, else `{redacted: true, paths: [...]}` |
| POST | `/config/revisions/{id}/approve` | approver | Approve (records principal + time); reject via `?reject=true&reason=` or a separate route |
| POST | `/config/revisions/{id}/apply` | operator | Start a rollout. Body: `{dry_run, canary_percent, max_batch, allow_endpoint_change}` |
| GET | `/config/rollouts/{id}` | viewer | Per-agent state table, batch progress, pause reason |
| POST | `/config/rollouts/{id}/abort` | operator | Stop promotion (does not recall delivered configs) |
| POST | `/config/revisions/{id}/rollback` | approver | Create + approve + apply a rollback rollout |
| GET | `/agent/{id}/config` | viewer | `desired`, `effective`, `drift`, `last_status`, `last_error`, `attempts`, revision history |
| DELETE | `/agent/{id}/config` | operator | Withdraw the assignment (agent reverts to local config) |

`POST /config/revisions` body:

```json
{
  "scope": {"kind": "selector", "selector": {"healthy": "true", "environment": ["prod"]},
            "include_future": false},
  "config_map": {"": {"body": "receivers: ...", "content_type": "text/yaml"}},
  "note": "raise batch timeout to 10s",
  "allow_endpoint_change": false
}
```

**`dry_run: true`** resolves the target list, runs validation, and returns the resolved agents +
a diff summary per agent — and sends nothing. This is the button people will press most; make it
the default in the UI, and make it cheap.

## Error semantics

- `401`/`403` — not authenticated / wrong role (never leak which).
- `404` — unknown revision/agent, **and** config push disabled (#3).
- `409` — revision not approved; rollout already active for this revision; agent does not
  advertise `AcceptsRemoteConfig`; revision poisoned and requires re-approval.
- `422` — invalid config, with the structured verdict (#7) in `detail`.
- `429` — rollout/rate limits, with `Retry-After`.

## Work

1. Pydantic models for scope/config map/apply/rollout (`extra="forbid"` so typos are errors).
2. Routes delegate to the control-plane functions — no SQL in routes.
3. Explicitly reject a request that tries to push a redacted body (#5).
4. `#81`'s low-level `PUT /agent/{id}/config` either goes away or stays as an internal-only route
   requiring `admin` + `allow_direct: true`; the public surface is revision-based. Document the
   decision in #81.
5. OpenAPI examples for each route (`openapi_examples`) — this repo is driven by AI agents, and
   examples are the cheapest documentation that actually gets used.

## Acceptance criteria

- [ ] Full happy path over HTTP: propose → approve → apply (dry run) → apply → status `APPLIED`
- [ ] Every route enforces its role; wrong role = 403 with no information leak
- [ ] `dry_run` sends no frames (assert on a fake agent receiving nothing) and returns the resolved
      target list
- [ ] Pushing an unapproved, poisoned or disabled-config-push revision returns the documented code
- [ ] `422` responses carry `{errors: [{path, message}], warnings: []}`
- [ ] Aiming at an agent without `AcceptsRemoteConfig` is refused with an actionable message
      ("run the OpAMP Supervisor; the collector extension cannot apply remote config")
- [ ] No route returns a config body without `secret:read`
- [ ] Every write route emits an audit entry (#4)

## Metadata

- Phase: `3-api` · Priority: 2 · Effort: M · Blocks: #11 #13
- Depends on: #2, #3, #4, #6, #7, #8
""" + AI_NOTE,
)

wi(
    "UI: Remote Config page (edit, validate, diff, dry-run, approve, live rollout, rollback)",
    ["config-push", "phase-4-ui"],
    """## Summary

The operator surface. Streamlit, following the existing page conventions
(`ui/pages/N_Name.py`, `st.set_page_config` per page per `memory.md`, `render_sidebar()`).

Design tension to resolve up front: **this button can take down a fleet.** So the page is built
around *review before action*, not around a big red "Push" button.

## Flow

1. **Target** — fleet / `healthy` / `status` / metadata filters, reusing the existing
   `GET /agents` query semantics (`AgentState.matches_filters`). Show a live count and a
   breakdown ("42 prod, 3 staging, 1 unknown") so the blast radius is visible before anything
   else. "Select all" is not a one-click action.
2. **Edit** — YAML editor (`st.text_area`, height by content; no new dependency). Prefill from an
   agent's current `effective_config` (decoded from the config map) so the common edit is a small
   diff. Never prefill from a redacted document (#5) — show "secrets hidden; edit requires
   `secret:read`" instead.
3. **Validate** — calls `POST /config/revisions`, shows errors/warnings with YAML paths, links to
   the offending line where possible. Validation errors block the next step in the UI.
4. **Review** — side-by-side diff (desired vs each agent's current effective config, collapsed
   when identical), a summary table of affected agents, the protected-invariant warnings, the
   policy verdict, and *who will approve*. This is the screen that gets screenshotted into the PR.
5. **Approve** — visible only to an `approver` (server-enforced, mirror in the UI). Separate
   Streamlit form; typing the agent count is required for >5 agents (cheap, effective speed bump).
6. **Roll out** — start with the default canary, show batch progress live: per-agent status
   (`pending/delivered/applying/applied/failed/reverted/paused`), batch number, elapsed time,
   gate countdown. Refresh with `st.fragment(run_every=...)` (or `st.rerun` + a "refresh" button
   if the Streamlit version predates fragments — check at implementation time).
7. **Abort / rollback** — prominent, one click + confirm. "Rollback applied agents" vs "rollout
   from scratch" must be visibly different actions.
8. **History / audit** — revisions for this scope, who approved, what happened, and the audit
   trail (#4).

**Cross-page additions:** a `desired vs effective` drift badge on the Agents page and in the
fleet table, and a "Config" tab on the agent detail page linking into the flow with that agent
pre-selected.

**Posture banner (#3):** when `config_push.insecure == true`, a persistent red banner at the top
of this page ("push enabled without TLS/agent auth — do not use this on a real fleet").

## Work

1. `ui/pages/5_Remote_Config.py` + helpers in `ui/shared.py` for the config endpoints.
2. Reuse the existing admin login flow today, but write it against the session-token helper from
   #12 so switching over is a one-line change.
3. Failing states must be informative: no agents match, push disabled, revision not approved,
   agent lacks `AcceptsRemoteConfig`, rollout paused (with the reason).
4. Screenshots into `README.md` once implemented (project convention: UI changes ship with
   screenshots — start a collector first per the note in `memory.md`), plus an entry on the Help
   page explaining propose → approve → canary.

## Acceptance criteria

- [ ] A user can go from "edit YAML" to "canary applied and healthy" without leaving the page
- [ ] Target count and per-attribute breakdown are visible before any action
- [ ] Validation errors carry YAML paths and block approval
- [ ] Approve is not offered to a non-approver, and the server agrees (test with both roles)
- [ ] Rollout progress updates without a page reload and shows the pause reason
- [ ] Abort and rollback each work and are visually distinct
- [ ] A redacted document cannot be edited-and-pushed from the UI
- [ ] The insecure-posture banner appears when the server reports `insecure: true`
- [ ] README screenshots updated; Help page updated

## Metadata

- Phase: `4-ui` · Priority: 3 · Effort: L · Blocks: nothing
- Depends on: #10 (all endpoints), #12 (session auth), #3 (posture banner), #5 (redaction)
""" + AI_NOTE,
)

wi(
    "UI: Streamlit auth and session hardening for privileged actions",
    ["security", "phase-4-ui"],
    """## Summary

The UI's auth pattern is copy-pasted into `3_Admin.py` and `ui/shared.py` and holds the raw
password in `st.session_state["admin_password"]` for the life of the session, re-sending HTTP
Basic on every request. There is no logout, no TTL, no role awareness, and no XSRF review. That
was tolerable for editing webhook URLs; it is not tolerable for a control plane.

## Design

- **Store a session token, not a password.** After `POST /auth/session` (#2), keep the opaque
  token in session state; drop the password from memory immediately. Tokens expire; the UI shows
  the expiry and offers "extend" (which is an audited re-auth).
- **One login implementation.** Replace the duplicated forms in `3_Admin.py` and
  `ui/shared.py::prompt_for_password` with `shared.require_admin(role=...)` + `shared.admin_request()`
  so 401/403 handling and retry-after-lockout exist in exactly one place.
- **Roles in the UI.** Hide actions the principal cannot perform (`approve`, `rollback`, token
  management) — cosmetic only, the server is the boundary — and label the current role in the
  sidebar so nobody wonders why a button is missing.
- **Logout** everywhere, plus "sign out of all sessions" (calls the revoke endpoint).
- **XSRF / cookie posture.** `.streamlit/config.toml`: `server.enableXsrfProtection = true`,
  `server.enableCORS = false`, `server.address` bound to loopback by default in the compose
  template, and session cookies not shared across origins. Document that Streamlit is a
  server-side session and must not be exposed publicly without a TLS proxy — it is an admin
  console, not a public site.
- **No secrets in URLs or titles** (query params land in proxy logs): agent ids are fine, config
  bodies never.
- **Lockout UX.** A 429 from the login endpoint shows the retry delay rather than an endless
  "invalid password" loop.

## Work

1. `ui/shared.py`: `require_admin(role)`, `admin_request(method, path, **kw)` with central
   401/403/429 handling, `logout()`, role display.
2. Migrate `3_Admin.py` and the other admin call sites; delete the duplicate form code.
3. `.streamlit/config.toml` review + compose/README deployment guidance.
4. Tests: the existing pages are not unit-tested, so at minimum test the helper's 401/403/429
   handling directly.

## Acceptance criteria

- [ ] The password is never kept in session state after login; the token is used for every call
- [ ] Token expiry returns the user to the login form with a clear message (not a stack trace)
- [ ] Lockout (429) is displayed with the retry delay
- [ ] Role-gated actions are hidden and server-enforced
- [ ] Logout and "sign out everywhere" both work
- [ ] XSRF protection enabled; CORS off; compose binds Streamlit to loopback with a comment
- [ ] No admin page contains its own copy of the login form

## Metadata

- Phase: `4-ui` · Priority: 2 · Effort: S · Blocks: #11 (ship before it)
- Depends on: #2 (session endpoint, roles)
""" + AI_NOTE,
)

wi(
    "Layers: opamp_client, opampctl, MCP (write-gated) and skill updates for config push",
    ["ai-access", "config-push", "phase-5-layers"],
    """## Summary

Per AGENTS.md every endpoint lands in all four layers. Config push is the first **write**
capability, so the layers need a policy as well as methods — otherwise an LLM with an MCP client
gets fleet-wide config authority by default.

## Policy

- **MCP: propose only.** `propose_config` and `validate_config` are exposed (when writes are
  enabled); `approve`, `apply`, `rollback`, `abort` are **never** exposed. An AI agent can draft
  and check a config; a human approves and applies it (#2's `approver` role exists exactly for
  this).
- **Write tools are absent, not failing,** unless `OPAMP_MCP_ALLOW_WRITES=true` (default false).
  Absence is discoverable by the model and cannot be talked around; a tool that always returns
  "denied" invites retries. Update `mcp_server/server.py` registration accordingly.
- **CLI:** destructive/multi-agent commands require `--yes`; `--dry-run` exists on `apply` and
  prints the resolved target list.
- **Docs:** the skill and README state the rule plainly ("you may propose and validate; only a
  human approves and applies").

## Work

1. `client/opamp_client.py`: `propose_config`, `validate_config`, `get_revision`,
   `list_revisions`, `approve_revision`, `apply_revision`, `get_rollout`, `abort_rollout`,
   `rollback_revision`, `get_agent_config`, `withdraw_config`, `list_audit`. Reuse
   `OpampApiError(.status_code, .detail)`; surface a `422` verdict as a structured object rather
   than a string blob.
2. `cli/main.py`: `opampctl config propose --file cfg.yaml --agents <a,b> | --selector env=prod`,
   `config validate`, `config approve <rev>`, `config apply <rev> --dry-run|--yes`,
   `config status <rev|rollout>`, `config abort`, `config rollback`, `config withdraw <agent>`,
   `config diff <agent>`, plus `opampctl audit list`. `--db` mode: read-only revisions/assignments/
   audit.
3. `mcp_server/server.py`: two new tools, gated as above; update the tool count in README (currently
   "19 typed tools").
4. `skills/opamp-server/SKILL.md`: the propose → approve flow, the never-apply-directly rule, the
   `AcceptsRemoteConfig` prerequisite (supervisor vs plain collector), and the "redacted bodies
   are un-pushable" gotcha.
5. Tests for each layer using the established `InProcessASGITransport` monkeypatch pattern.

## Acceptance criteria

- [ ] `opampctl config propose/validate/apply --dry-run` work end to end against the server
- [ ] `opampctl config apply` without `--yes` on a multi-agent scope refuses and prints the list
- [ ] MCP exposes propose/validate only; with `OPAMP_MCP_ALLOW_WRITES` unset the tools are absent
- [ ] A `422` verdict is rendered readably by CLI (JSON) and by the MCP tool (structured fields)
- [ ] Skill documents the flow and the two traps (redaction, supervisor requirement)
- [ ] README tool count and CLI section updated; `--db` read-only modes documented
- [ ] All four layers have tests

## Metadata

- Phase: `5-layers` · Priority: 3 · Effort: M · Blocks: nothing
- Depends on: #10 (endpoints), #2 (roles for approve)
""" + AI_NOTE,
)

wi(
    "Testing: security regression suite + fake agent harness for config apply and rollback",
    ["security", "config-push", "phase-x-cross"],
    """## Summary

Everything above is a security boundary, and boundaries without tests rot. This issue builds (a)
a fake agent that behaves like the OpAMP Supervisor, and (b) a regression suite where every test
corresponds to a threat in the epic's model.

Extend the harness from #80 rather than starting a third one; `tests/test_ws.py` already has a
`TestClient`-based WS harness and `tests/asgi_transport.py` covers in-process HTTP.

## Fake agent

`tests/fake_agent.py`:

- Speaks protobuf over HTTP and WS (reuses `server.framing.encode_message`).
- Advertises a configurable capability set, including `AcceptsRemoteConfig` (and a mode that does
  not advertise it, to test the refusal).
- Maintains an in-memory config store: accepts `remote_config`, records the hash, and reports
  `APPLIED` / `APPLYING` / `FAILED` on a scripted schedule.
- Modes: `apply_ok`, `apply_fail` (report FAILED + error message), `auto_revert` (mimic the
  supervisor: fall back to `last_working` hash and report `APPLIED` for it — the T9 trap),
  `silent` (never reports; goes stale), `crash` (never comes back).
- Can be driven by time-warping the rollout gates (inject a clock) so tests are fast.

## Suites

1. **AuthN/AuthZ** — unauthenticated HTTP/WS rejected before parsing; wrong token claiming another
   uid refused; WS binding takeover refused; every config route × every role matrix; a revoked
   token loses access immediately.
2. **Posture** — config push off by default; enabling with insecure posture fails to boot; 404 (not
   403) when disabled; `insecure` override reported everywhere.
3. **Validation** — corpus of bad configs (invalid YAML, alias bomb, duplicate keys, missing
   component section, opamp extension removed, endpoint changed, oversized, policy violation);
   each must produce the expected structured error and a bounded runtime.
4. **Lifecycle** — propose → approve → delivered → APPLIED; supersede; rollback; withdraw; staleness
   keeps the assignment; new uid inherits nothing.
5. **Poison / no-loop** — `FAILED` and `auto_revert` produce exactly one delivery attempt and zero
   re-deliveries over N polls (the most important test in the suite).
6. **Rollout gating** — canary success promotes; canary failure pauses; silent agent blocks the
   gate; caps respected; abort is immediate; TTL cancels.
7. **Secrets** — a sentinel secret never appears in `/agents`, `/agent/{id}`, reports, alert
   payloads, captured logs (any level, including exceptions), CLI output, or the DB file when
   encryption is on; redacted bodies are un-pushable.
8. **Audit** — one entry per privileged action; insert failure blocks the action; tamper detection
   on a hand-edited row.
9. **E2E** — `propose → approve → canary → promote → verified → bad revision → FAILED → poisoned →
   rollback → APPLIED`, asserted as a single narrative test with the fake agent. This is the test
   that proves the feature is safe, and it belongs in the PR that closes the epic.

## Acceptance criteria

- [ ] `tests/fake_agent.py` supports the six modes and both transports
- [ ] Every threat row in the epic's table has at least one test that fails when its mitigation is
      removed (state the mapping in a test docstring)
- [ ] The no-loop test fails if poisoning is disabled (verify by reverting the mitigation locally)
- [ ] No test writes to the repo's `data/opamp.db` (use `tmp_path`, per `memory.md`)
- [ ] The E2E narrative test runs in CI in a few seconds (clock injection, no sleeps)
- [ ] `pytest tests/ -q` green with the feature both off and on

## Metadata

- Phase: `x-cross` · Priority: 2 · Effort: M · Blocks: nothing (it gates closing the epic)
- Depends on: #80 (harness), #89 (WS harness), everything else in this epic to test
""" + AI_NOTE,
)


def run(cmd, input_text=None):
    return subprocess.run(cmd, input=input_text, text=True, capture_output=True)


def ensure_labels(dry_run: bool) -> None:
    proc = run(["gh", "label", "list", "--repo", REPO, "--limit", "100", "--json", "name"])
    existing = set()
    if proc.returncode == 0:
        import json

        existing = {lbl["name"] for lbl in json.loads(proc.stdout)}
    for name, (color, desc) in LABELS.items():
        if name in existing:
            continue
        if dry_run:
            print(f"[dry-run] would create label {name!r}")
            continue
        proc = run(["gh", "label", "create", name, "--repo", REPO,
                    "--color", color, "--description", desc])
        if proc.returncode != 0:
            print(f"warning: could not create label {name}: {proc.stderr.strip()}")


def create_issue(title: str, labels: list[str], body: str) -> int:
    cmd = ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body-file", "-"]
    for label in labels:
        cmd += ["--label", label]
    proc = run(cmd, input_text=body)
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue create failed for {title!r}:\n{proc.stderr}")
    return int(re.search(r"/issues/(\d+)$", proc.stdout.strip()).group(1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ensure_labels(args.dry_run)

    epic_title, epic_labels, epic_body = EPIC
    if args.dry_run:
        print(f"[dry-run] epic: {epic_title}")
        for title, labels, _ in WI:
            print(f"[dry-run]   issue: {title}  [{', '.join(labels)}]")
        return 0

    epic_number = create_issue(epic_title, epic_labels, epic_body)
    print(f"created epic #{epic_number}")

    created = []
    for title, labels, body in WI:
        number = create_issue(title, labels, body)
        created.append((number, title))
        print(f"created #{number}: {title}")

    # Rewrite the epic's "Work breakdown" placeholders with real issue links.
    patched = epic_body
    for index, (number, _title) in enumerate(created):
        patched = patched.replace(f"<WI:{index}>", f"[#{number}](https://github.com/{REPO}/issues/{number})")

    proc = run(["gh", "issue", "edit", str(epic_number), "--repo", REPO, "--body-file", "-"],
               input_text=patched)
    if proc.returncode != 0:
        print(f"warning: could not patch epic body: {proc.stderr.strip()}")

    print(f"\nEpic: https://github.com/{REPO}/issues/{epic_number}")
    print(f"Issues created: {len(created)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
