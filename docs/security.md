# Security: posture, authentication and the audit log

Agent credentials are their own topic: [agent-tokens.md](agent-tokens.md). Config
bodies and their encryption: [remote-config.md](remote-config.md#config-data-and-secrets).

## Before you expose this server

This server ships **read-only by default** and is meant to be reachable by
trusted agents on a trusted network until you finish the checklist below.
Plain HTTP + no admin credential + a tokenless agent that can claim any
`instance_uid` + a port published to the world is a fleet takeover, so the defaults
are now closed:

| Thing | Default | What that means |
|---|---|---|
| `ADMIN_PASSWORD` | *none* | Read-only mode: reads are open, **every privileged write answers 403** (#95). Prefer named principals with roles — see [Roles](#roles). Docker generates a random password on first start and stores it in `data/admin_password`; nothing ships a placeholder |
| Config push | **off** | Push routes return 404 (not 403 — a stranger does not get to learn the surface exists), the server does not advertise `OffersRemoteConfig`, and no `remote_config` field is ever sent |
| Agent tokens | **required** | A tokenless agent gets 401, and there is no opt-out (#125). Mint one with `opampctl agent-tokens create` and put it in the agent's config — see [agent-tokens.md](agent-tokens.md) |
| TLS | off | Credentials and configs travel in cleartext |
| Config at rest | *no key* | Config bodies are not stored until `CONFIG_AT_REST_KEY` is set (or the plaintext opt-out is taken) — see [config data and secrets](remote-config.md#config-data-and-secrets) |
| Ports in `docker-compose.yml` | `127.0.0.1` | Reachable from the host only, not from the network — the UI (`:8501`) included: it is a server-side admin console with a live session token and no TLS of its own |

**To expose it safely**, do all of these:

1. **Put TLS in front.** Use `docker/nginx.conf` or `docker/Caddyfile`, then set
   `PUBLIC_URL=https://opamp.example.com` (or `TLS_TERMINATED_UPSTREAM=true`).
   Keep port 4320 bound to loopback so nothing can bypass the proxy, forward
   `Upgrade`/`Connection` and a long read timeout for the WebSocket transport,
   and leave `Origin` intact — `CORSMiddleware` does not apply to upgrades, so
   the server's own check is the only one.
2. **Give each person an identity** (`opampctl auth principals create … --role admin|approver|operator|viewer`) — the
   generated `data/admin_password` is a bootstrap credential, not a way to run a team:
   with one shared secret there is no `approver` distinct from `operator`, and no way
   to revoke one person without logging everyone out.
3. **Authenticate your agents.** Agent auth is mandatory (#125): mint a fleet or
   bound token and put it in every agent's config. The token travels in the agent's
   config, so TLS is not optional once you expose the port.
4. **Set `CORS_ORIGINS`** to the origin the UI is served from instead of `*`.
5. **Set `CONFIG_AT_REST_KEY`** so pushed configs are encrypted on disk. Losing
   the key loses *desired* config, not the fleet — see
   [config data and secrets](remote-config.md#config-data-and-secrets).
6. **Keep config push off** until the posture check passes for you. Turning it on
   is refused at startup unless admin auth, agent auth, TLS, a writable audit log
   and a config encryption key are all in place:

```bash
OPAMP_CONFIG_PUSH_ENABLED=true uvicorn server.main:app --port 4320
# refusing to start: OPAMP_CONFIG_PUSH_ENABLED=true but the server posture is insecure:
#   - admin auth is not configured: ...
#   - the server is not served over TLS: ...
#   - the audit log is not available: ...
#   - config bodies would be stored unencrypted: ...
```

The only way past that is `OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true`, which is for
local development: it logs a red warning, reports `insecure: true` in `/health`,
and puts a permanent banner on every UI page. It is deliberately unpleasant.

Check your posture at any time:

```bash
curl -s localhost:4320/health | jq .config_push
# {"enabled": false, "insecure": false, "agent_auth": "token", "audit": true, "tls": false,
#  "config_at_rest": "unconfigured"}
opampctl --password "$ADMIN_PASSWORD" health   # same, plus config_push
curl -s -u ":$ADMIN_PASSWORD" localhost:4320/config/posture | jq .requirements
```

## Authentication

Two separate credential systems, deliberately:

| Channel | Credential | Enabled by |
|---|---|---|
| **Agents** → `POST/WS /v1/opamp` | `Authorization: Bearer opamp_agent_…` | **Mandatory** (#125) — see [agent-tokens.md](agent-tokens.md) |
| **Humans / tooling** → admin API | session token (`POST /auth/session`) or HTTP Basic | a principal, or the legacy `ADMIN_PASSWORD` |

### Roles

Every privileged endpoint takes a role. Roles are ordered — a role satisfies every
role below it:

| Role | May | Why it exists |
|---|---|---|
| `viewer` | read everything (alert webhooks redacted) | a dashboard user should not need write access |
| `operator` | propose changes, save/edit collector groups, apply *approved* revisions, start a staged rollout of an approved one, abort a rollout, deliver a config, ask an agent to restart, edit alerts, force compliance checks | the person who runs the fleet |
| `approver` | approve / reject / reactivate, create a staged rollout (which *is* the approval), roll a rollout back | the person who signs off — **deliberately not the same role** as `operator`, so two-phase config push (#99, #103) is an approval and not a formality |
| `admin` | everything, plus identity (principals, sessions) and fleet-wide actions | onboarding and break-glass |

### Principals (recommended)

A principal is a named identity with a role and its own password, stored
scrypt-hashed in `data/opamp.db`:

```bash
# Bootstrap: creating the first principal needs an admin credential, so start with
# ADMIN_PASSWORD set (docker-compose writes the generated one to data/admin_password):
export ADMIN_PASSWORD="$(cat data/admin_password)"
opampctl auth principals create alice --role admin    --password '…'
opampctl auth principals create bob   --role operator --password '…'
opampctl auth principals create carol --role approver --password '…'
opampctl auth principals list                        # roles, last seen, active

# Log in as a person. The token is a credential — keep it out of chat and commits.
export OPAMP_ADMIN_TOKEN="$(opampctl auth login --name bob --raw)"
opampctl auth whoami                                 # name, role, session expiry
opampctl alerts set @alerts.json                     # operator: allowed
opampctl auth logout                                 # revoke this session

# Drop the shared password once every person has an identity:
unset ADMIN_PASSWORD
```

Sessions expire after `ADMIN_SESSION_TTL_SECONDS` (1 h) **and** after
`ADMIN_SESSION_IDLE_SECONDS` (15 min) of inactivity, and can be revoked
individually (`opampctl auth sessions list` → `auth sessions revoke <id>`) or all at
once by revoking the principal. Revoking a principal keeps its row, so the name stays
reserved — "remove alice, re-add alice" cannot silently hand her identity to someone
else.

### Legacy shared password (deprecated)

`ADMIN_PASSWORD`, `ADMIN_PASSWORD_FILE` or a generated password still works, as one
synthesised `admin` principal. It stays for scripts and existing deployments, with a
startup deprecation warning: there is no per-person identity, no `approver` role, and
no way to revoke one person without changing everyone's password.

```bash
# HTTP Basic: any username, the admin password as the password
curl -u admin:"$ADMIN_PASSWORD" http://localhost:4320/alerts
curl -H "Authorization: Basic $(printf ":$ADMIN_PASSWORD" | base64)" http://localhost:4320/alerts
```

### Read-only mode (no credential at all)

With nothing configured the server is **read-open and write-closed**: `viewer`
requests are served and every privileged write answers `403` ("admin auth is not
configured"). That is what a dashboard-only deployment wants, and it means an
unconfigured server can no longer be used to change your fleet. Config push also
refuses to start in this state (#96).

### Lockout

Failed logins are counted per `(name, client IP)` for **every** Basic-authenticated
request, not just `POST /auth/session` — otherwise `/auth/verify` would be a free
credential oracle. Session tokens are not counted: 256 random bits are not guessable.
After `ADMIN_LOGIN_MAX_ATTEMPTS` (default 5) the server answers `429` with
`Retry-After`, backing off exponentially
(`ADMIN_LOGIN_BACKOFF_SECONDS` → 2x → 4x… capped by
`ADMIN_LOGIN_MAX_BACKOFF_SECONDS`). The response never reveals whether the name
exists. Behind the bundled proxies set `TRUST_PROXY_HEADERS=true` so the key is the
real client and not the proxy; **do not** set it when the server is directly
reachable — a header any client can forge would let an attacker evade the lockout and
collapse everyone else into one bucket.

Discovery and self-inspection:

```bash
curl -s localhost:4320/auth/status | jq   # auth_mode, roles, sessions, principals
opampctl auth whoami                      # who am I, and what may I do
opampctl auth verify                      # exit 0 if the credential works
```

`auth_mode` is `principals` (named identities exist), `password` (legacy shared
secret only) or `disabled` (read-only mode).

### The Streamlit UI (admin console)

The UI signs in the same way the CLI does — `POST /auth/session` — and then holds an
**opaque session token**, not your password (#105):

- The password is sent once and discarded. There is no `admin_password` in browser
  session state, and no HTTP Basic on every request. A tab that survived an upgrade
  deletes the old key on its next render, so it cannot keep re-sending Basic.
- The sidebar shows **who you are, your role and when the session expires**. *Extend*
  is an **audited re-auth** (`admin.login.ok`): it issues a new token and revokes the
  superseded one, so a stolen copy dies at the same moment. It is not a silent sliding
  TTL.
- **Sign out** revokes the token (`DELETE /auth/session`); **Sign out everywhere**
  additionally revokes every other session of *your* principal
  (`GET /auth/sessions` → `DELETE /auth/sessions/{id}`, `admin` only). Revoking another
  *person* is a principal revocation, not a sign-out button.
- An expired or revoked token returns you to the login form with a sentence, not a
  stack trace; a `429` shows the retry delay from `Retry-After`; a `403` explains the
  role you need instead of logging you out (a role denial is not a dead session).
- **Roles hide controls, the server enforces them.** Approve/rollback, policy reload and
  the Sessions tab are hidden below `admin`, but the API refuses them anyway — hiding a
  button is ergonomics, never the boundary.
- **No secrets in URLs.** Only non-secret identifiers (agent ids, grouping) go into query
  parameters, because query strings end up in proxy logs.
- **Posture:** `.streamlit/config.toml` pins `enableXsrfProtection = true` and
  `enableCORS = false`, and `docker-compose.yml` binds the UI to `127.0.0.1:8501`. The UI
  is a server-side session with no TLS of its own — do not publish port 8501; terminate
  TLS in the proxy and keep the UI on loopback.

## Audit log

Every privileged decision — a login, a lockout, a session revoked, a principal
created, an agent credential minted or revoked, an agent auth failure — writes
exactly one row to `audit_log` in `data/opamp.db`. From #99 onward, every
config-push action lands there too, so "who pushed that config to 400 collectors"
has an answer that does not depend on anyone remembering.

```bash
opampctl --password "$ADMIN_PASSWORD" audit list --action config. --limit 20
opampctl --password "$ADMIN_PASSWORD" audit list --actor alice --since 2026-10-01
opampctl --password "$ADMIN_PASSWORD" audit verify            # exit 1 if the chain is broken
opampctl --password "$ADMIN_PASSWORD" audit export -f jsonl -o audit.jsonl
```

**Hash-chained.** Each row stores `prev_hash` (the previous row's `entry_hash`) and
an `entry_hash` over its own canonical JSON. `GET /audit/verify` recomputes the
whole chain: an edited row fails an `entry_hash` check, a deleted or reordered row
fails a `prev_hash` check, and `broken_at` names where it shows. Be honest about
the limits — an attacker with write access to the database can rebuild the chain
from the start, so export entries off-box (`audit export`) if the log is evidence.

**Fail closed.** A privileged action records its decision *before* acting, and if
the row cannot be written the action does not happen (HTTP 500, nothing mutated).
Denials and auth failures are the exception: those are logged best-effort, because
turning a 401 into a 500 when the disk is full is an outage caused by bookkeeping.
After 5 consecutive write failures the best-effort path pauses for 60s — `AUDIT`
lines in the server log keep flowing, and `/health` reports `audit: false`.

**No bodies, no secrets.** Entries carry actor, role, action, target, result and a
small redacted `detail_json` — hashes, counts, verdict codes. Fields named like
credentials are dropped at the seam, so passing a token by accident cannot store
it. Config *bodies* never appear; `config_hash` does (that is the #99 seam).

**Retention.** `AUDIT_RETENTION_DAYS` (default 365; `0` keeps forever) prunes at
most hourly. A prune writes an `audit.checkpoint` row recording the hash it cut to
before deleting, so `/audit/verify` anchors there instead of reporting a permanent
break; deleting rows *before* the checkpoint still shows up as tampering.

| Result | Means |
|---|---|
| `ok` | the action was performed |
| `denied` | refused before acting (bad credential, insufficient role, locked out) |
| `error` | recorded, then the mutation failed (see the second row for the outcome) |
| `noop` | idempotent no-change (e.g. logging out twice) |
