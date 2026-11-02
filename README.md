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

## Protocol definitions (`proto/`)

The wire format lives in [`proto/opamp.proto`](proto/opamp.proto) and
[`proto/anyvalue.proto`](proto/anyvalue.proto), synced to
[opamp-spec **v0.20.0**](https://github.com/open-telemetry/opamp-spec/tree/v0.20.0/proto/opamp/v1).
The Python stubs (`proto/*_pb2.py`, `proto/*_pb2.pyi`) are generated and committed.

Two deliberate differences from upstream:

- **Flat layout.** Files stay in `proto/` rather than `proto/opamp/v1/`, so `opamp.proto`
  imports `"proto/anyvalue.proto"` instead of `"opamp/v1/anyvalue.proto"`. That is what
  makes the stubs import each other as `from proto import anyvalue_pb2` and every call
  site (`server/`, `cli/`, `tests/`) keep using the `proto.opamp_pb2` path. Nothing on the
  wire changes: protobuf does not transmit package, file or message names.
- **Generated with protoc 34.1**, so the stubs refuse to import on a protobuf runtime older
  than 7.34.1 (`requirements.txt` pins that floor). Regenerating with a newer protoc
  raises the floor — bump the requirement in the same commit.

Regenerate after editing a `.proto`:

```bash
./scripts/generate_proto.sh      # prints the expected protoc release if it is missing
git diff --stat -- proto/        # a clean tree means the stubs were not stale
python -m pytest tests/test_proto_sync.py
```

`tests/test_proto_sync.py` is the guard rail: it pins the proto package, every
pre-existing field number, the byte-for-byte encoding of a pre-v0.20.0 message, and the
messages v0.20.0 added (`AgentConfigObject.role`, `ComponentHealth.attributes`). A
renumbered field is invisible in review — protobuf happily parses the old bytes into the
new field — so the test is the only thing that catches it.

## Quick Start

### Docker Compose (Recommended)

Pre-built images are available from GitHub Container Registry:
- `ghcr.io/agardnerit/opamp-server-py-server:latest`
- `ghcr.io/agardnerit/opamp-server-py-ui:latest`

Images are multi-arch (`linux/amd64`, `linux/arm64`). Pushing a `vX.Y.Z` git tag
publishes `X.Y.Z` and `X.Y`, and moves `latest`; a prerelease tag such as
`vX.Y.Z-rc1` publishes only `X.Y.Z-rc1` and leaves `latest` alone. Tags are
created with `python3 release.py --version vX.Y.Z`.

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

There is no default admin password. On first start the server generates a random
one, prints it once in the logs (`docker-compose logs server | grep -i "admin
password"`), and stores it in `./data/admin_password` (mode 0600). Set
`ADMIN_PASSWORD` in `.env` to choose your own. Both ports bind to `127.0.0.1` on
purpose — see [Before you expose this server](#before-you-expose-this-server).

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

With `OPAMP_AGENT_AUTH=token` (see [Agent authentication](#agent-authentication-agent--server))
the same block also carries the agent's credential:

```yaml
extensions:
  opamp:
    server:
      http:
        endpoint: http://127.0.0.1:4320/v1/opamp
        headers:
          Authorization: "Bearer ${env:OPAMP_AGENT_TOKEN}"
```

### Transports

Plain HTTP is the default and is what the collector's `opamp` extension uses
(`endpoint: http://host:4320/v1/opamp`). Set `OPAMP_WS_ENABLED=true` to accept a
WebSocket upgrade on the same path. Frames are a base-128 varint header (must be
`0`) followed by the protobuf message; messages over `OPAMP_MAX_MESSAGE_BYTES`
close the socket with `1009`. Both transports run the same message handler, so
agent state is identical either way.

WebSocket upgrades are not covered by `CORSMiddleware`, so the handshake checks
`Origin` itself: agents (which send no `Origin`) always pass, and a browser origin
must be listed in `CORS_ORIGINS` unless that is `*`. If you launch uvicorn
directly, set `--ws-max-size` to at least `OPAMP_MAX_MESSAGE_BYTES` or uvicorn's
16 MiB default will close frames before the app limit applies.

Both transports enforce `OPAMP_MAX_MESSAGE_BYTES` (64 MiB by default). On HTTP the
limit is checked against `Content-Length` first and then while streaming, **after**
`Content-Encoding: gzip` decompression — so a small compressed body cannot expand
past the cap. An oversized body gets `413 Content Too Large` and changes no state
(no registration, no heartbeat, no metrics). A body that is not a usable
`AgentToServer` — unparseable protobuf, an empty `instance_uid`, or corrupt gzip —
gets `400` with a `ServerToAgent.error_response` (`type: BAD_REQUEST`) and a
human-readable reason; a `Content-Encoding` the server does not implement gets
`415` with the same body. `/v1/metrics` carries OTLP, not OpAMP, and deliberately
does not share this limit. On WebSocket the same rejections send the error frame and
close `1002`; oversized frames close `1009`.

#### Compression

Agents may gzip their request body (`Content-Encoding: gzip` or `x-gzip`) and the
server unrolls it *inside* the bounded read, so a decompression bomb is refused at
the limit rather than expanded first. The reply is gzipped when the agent sent
`Accept-Encoding: gzip` **and** the body is at least 1 KiB — a 40-byte capabilities
reply costs more to compress than it saves, so it stays plain. `Vary:
Accept-Encoding` is set so a cache cannot serve gzip to a client that cannot read it.
The collector's `opamp` extension does not enable compression today; `opamp-go`
clients that call `EnableCompression()` get it for free.

#### Incomplete status reports

Agents may omit unchanged sub-messages, so an `AgentToServer` is not necessarily a
full picture. The only signal the spec provides is `sequence_num`: anything that is
not exactly one greater than the last accepted value means status was missed, and
the server answers with `ReportFullState` so the agent resends everything. Three
details are worth knowing:

- the counter is persisted per agent (`GET /agent/{id}` → `last_sequence_num`) so it
  survives a server restart, which is the case the spec's rule exists for;
- `sequence_num: 0` means "not set" and never triggers an ask, so an SDK that never
  fills the field is not asked for full state on every message;
- the ask is rate limited per agent to `REPORT_FULL_STATE_MIN_INTERVAL_SECONDS`
  (60), because a flapping agent with a broken counter would otherwise turn every
  poll into a full-state request. Counted in
  `opamp_full_state_requested_total{reason}` — `new_agent` for the registration
  baseline, `sequence_gap` for a repair.

### OpAMP specification and capability matrix

This server targets **OpAMP [v0.20.0](https://github.com/open-telemetry/opamp-spec/releases/tag/v0.20.0)**
— the generated stubs in `proto/` are built from that release (#66). `GET /health`
returns `spec_version` and the exact server `capabilities` bitmask, so `opampctl`, the
MCP server and the UI never hardcode either.

Transports: **HTTP** (poll/reply) and **WebSocket** (`OPAMP_WS_ENABLED`, upgrade on
`/v1/opamp`, #89) — same framing, same message handler, same auth.

| Direction | Capability | Status |
|---|---|---|
| Server | `AcceptsStatus` | ✅ always advertised |
| Server | `AcceptsEffectiveConfig` | ✅ always advertised (stored, redacted on read) |
| Server | `AcceptsPackagesStatus` | ✅ always advertised (stored/reported only) |
| Server | `OffersRemoteConfig` | ✅ advertised while the posture allows push (#81, #96) |
| Server | `OffersPackages` | ❌ [#82](https://github.com/agardnerIT/opamp-server-py/issues/82) — stays off: a set bit is a promise |
| Server | `OffersConnectionSettings` | ❌ [#83](https://github.com/agardnerIT/opamp-server-py/issues/83) |
| Server | `AcceptsConnectionSettingsRequest` | ❌ [#83](https://github.com/agardnerIT/opamp-server-py/issues/83) |
| Agent | `ReportsStatus`, `ReportsHealth`, `ReportsEffectiveConfig` | ✅ understood (health is optional; its absence is not unhealth) |
| Agent | `ReportsAvailableComponents` | ✅ understood (drives validation warnings) |
| Agent | `ReportsPackageStatuses` | ⚠️ stored and displayed, not acted on |
| Agent | `AcceptsRemoteConfig` | ✅ required before a config is offered (`remote_config.decision_for`) |
| Agent | `AcceptsRestartCommand` | ✅ [#84](https://github.com/agardnerIT/opamp-server-py/issues/84) — `POST /agent/{id}/restart`; 409 without the bit |
| Agent | custom capabilities / messages | ❌ [#85](https://github.com/agardnerIT/opamp-server-py/issues/85) |

The classification lives in `server/remote_config.py`
(`ALWAYS_ADVERTISED` / `CONDITIONAL` / `UNIMPLEMENTED` / `RESERVED`), and
`tests/test_capabilities.py` fails when a proto bump adds a capability none of them
mentions: silently dropping a bit is a symptom, an unclassified bit is what
over-claims to every agent. `GET /health` publishes the same values, so "will my agent
interoperate?" is answered without reading the source (#79).

Both transports accept credential headers, which is what makes per-agent
authentication possible without a wire extension: `extension/opampextension` supports
`server.http.headers` **and** `server.ws.headers` (verified against v0.140.0 — the
values are `configopaque`, so the agent's own effective-config report redacts them),
and `opamp-go` sends them on every HTTP request and on the WebSocket upgrade. The
supervisor supports the same `headers` block.

## Agent authentication (agent → server)

`OPAMP_AGENT_AUTH` decides whether the OpAMP endpoint believes the `instance_uid`
in the message body:

| Mode | Behaviour |
|---|---|
| `off` (default) | Unauthenticated: any client that can reach the port can claim any agent identity. Logs a startup warning |
| `token` | Each agent presents `Authorization: Bearer <token>`; the credential **is** the identity and must match the claimed `instance_uid` |
| `mtls` | Reported by the posture but **not enforced by this build** — certificates are terminated by your proxy, so the server would only be trusting a forgeable header. Use `token`; mTLS arrives with the CA work in #83 |

With `token`:

- a missing, unknown or revoked token gets **401** (HTTP) or **1008** (WebSocket)
  with **no** `ServerToAgent` body — an unauthenticated client learns nothing,
  not even which capabilities exist;
- a valid token claiming a *different* agent's `instance_uid` gets **403** / 1008,
  rejected before any state is applied, so the real agent's record is untouched;
- every rejection is counted in `opamp_agent_auth_failures_total{reason}` —
  `missing`, `malformed`, `unknown`, `revoked`, `uid_mismatch`,
  `binding_conflict` — and written to the [audit log](#audit-log);
- a live WebSocket binding may only move to the **same** principal, so a second
  connection cannot take over an agent (a legitimate reconnect moves cleanly).

Tokens are `opamp_agent_…`, stored as SHA-256 only, and returned exactly once at
creation. They never appear in `/agents`, logs, audit entries or error bodies.
`GET /agents` and `GET /agent/{id}` report each agent's credential state as
`{"enrolled", "active", "last_seen_at", "revoked_at"}` so you can watch a cutover.

### Cutover runbook (`off` → `token`)

```bash
# 1. While the server is open, let the fleet connect and note each agent id
opampctl agents list --pretty | jq '.agents[] | {id, name: .description}'

# 2. Mint a token per agent — the response carries a paste-ready snippet
opampctl --password "$ADMIN_PASSWORD" agents token create <agent-id> --label prod-1

# 3. Put the headers block in that agent's config and reload the collector
#      extensions.opamp.server.http.headers:
#        Authorization: "Bearer opamp_agent_..."
#    (server.ws.headers instead, if that agent speaks WebSocket)

# 4. Confirm each agent authenticates before closing the door
opampctl agents get <agent-id> | jq .credential     # last_seen_at must move

# 5. Flip the server and restart it
OPAMP_AGENT_AUTH=token uvicorn server.main:app --port 4320

# 6. Watch for stragglers
curl -s localhost:4320/metrics | grep opamp_agent_auth_failures_total
```

Rotation needs no downtime: mint a new token, deploy it, and the old one retires
on the *first successful use* of the new one (`--retire-previous` cuts over
immediately instead). Revocation (`opampctl agents token revoke <id>`) blocks
delivery but **keeps** the agent's record, state and desired config — revocation
is not a wipe, deliberately: you need that config to restore the agent later.

An agent with a revoked or broken credential retries forever with backoff, because
opamp-go cannot tell 401 from 403 — watch the failure metric, not the agent log.

Why the token lives in the config file instead of being handed down the wire:
`extension/opampextension` registers no `OnOpampConnectionSettings` callback, so
an in-band "here is your token" reply is ignored. The supervisor can consume
connection settings; the collector extension cannot. Registration-on-first-use
and CSR/TOFU stay in #83.

## Before you expose this server

This server ships **read-only by default** and is meant to be reachable by
trusted agents on a trusted network until you finish the checklist below.
Plain HTTP + no admin credential + agents that claim any `instance_uid` + a port
published to the world is a fleet takeover, so the defaults are now closed:

| Thing | Default | What that means |
|---|---|---|
| `ADMIN_PASSWORD` | *none* | Read-only mode: reads are open, **every privileged write answers 403** (#95). Prefer named principals with roles — see [Roles](#roles). Docker generates a random password on first start and stores it in `data/admin_password`; nothing ships a placeholder |
| Config push | **off** | Push routes return 404 (not 403 — a stranger does not get to learn the surface exists), the server does not advertise `OffersRemoteConfig`, and no `remote_config` field is ever sent |
| Agent auth | off | Any client can claim any agent identity. Set `OPAMP_AGENT_AUTH=token` and give each agent a credential — see [Agent authentication](#agent-authentication-agent--server) |
| TLS | off | Credentials and configs travel in cleartext |
| Config at rest | *no key* | Config bodies are not stored until `CONFIG_AT_REST_KEY` is set (or the plaintext opt-out is taken) — see [Config data and secrets](#config-data-and-secrets) |
| Ports in `docker-compose.yml` | `127.0.0.1` | Reachable from the host only, not from the network — the UI (`:8501`) included: it is a server-side admin console with a live session token and no TLS of its own |

**To expose it safely**, do all five:

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
3. **Authenticate your agents** (`OPAMP_AGENT_AUTH=token`) and mint a credential
   per agent. The token travels in the agent's config, so TLS is not optional
   once you turn this on.
4. **Set `CORS_ORIGINS`** to the origin the UI is served from instead of `*`.
5. **Set `CONFIG_AT_REST_KEY`** so pushed configs are encrypted on disk. Losing
   the key loses *desired* config, not the fleet — see
   [Config data and secrets](#config-data-and-secrets).
6. **Keep config push off** until the posture check passes for you. Turning it on
   is refused at startup unless admin auth, agent auth, TLS, a writable audit log
   and a config encryption key are all in place:

```bash
OPAMP_CONFIG_PUSH_ENABLED=true uvicorn server.main:app --port 4320
# refusing to start: OPAMP_CONFIG_PUSH_ENABLED=true but the server posture is insecure:
#   - admin auth is not configured: ...
#   - agents are not authenticated: ...
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
# {"enabled": false, "insecure": false, "agent_auth": "off", "audit": true, "tls": false,
#  "config_at_rest": "unconfigured"}
opampctl --password "$ADMIN_PASSWORD" health   # same, plus config_push
curl -s -u ":$ADMIN_PASSWORD" localhost:4320/config/posture | jq .requirements
```

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

## Config objects (#68)

`EffectiveConfig.config_map` holds **configuration objects**, and their keys are
agent-implementation-defined: `""` is valid and has no privileged meaning
([spec v0.20.0 §Configuration Objects](https://github.com/open-telemetry/opamp-spec/blob/v0.20.0/specification.md)).
`otelcol-contrib` with `reports_raw_config: true` reports two — the expanded config
under `""` and the raw config under `"raw"` — and other agents use `"collector"`,
`"config"`, … `AgentConfigObject` also carries an optional `role`, which is likewise
agent-defined.

`server/effective_config.py` is the one reader (`server/state.py` and the UI both go
through it, and `ui/config_view.py` renders it):

- **Every object is read.** Components reported by different objects are unioned, so a
  partial or alternative representation cannot hide a receiver, and none is counted
  twice.
- **Non-YAML is skipped, never raised.** A content type that is set and is not YAML, a
  body that is not UTF-8, a body that does not parse, a document that is not a mapping
  — each is skipped with a debug log naming the key, and the rest are still read.
- **Parsing is bounded** (the same loader the config validator uses: alias, depth and
  expansion budgets; duplicate keys refused), because the document comes from an agent.
- **`""` is preferred for display.** The "Collector Configuration" panel shows the `""`
  object when it parses, otherwise the first parseable object, untagged before
  role-tagged. A role marks an alternative representation, not the primary one.

A role is carried end-to-end by the push path too: validated (and bounded to 128
characters), included in the canonical hash **only when set** — so revisions stored
before v0.20.0 keep their hash and are not re-pushed after an upgrade — and set on the
`AgentConfigObject` delivered to the agent.

## Config data and secrets

Config bodies are the most sensitive data this server holds: exporter auth
headers, basic-auth extensions, TLS keys, internal endpoints. Three rules apply
everywhere (`server/redaction.py`, `server/crypto.py`):

**Redacted by default.** `GET /agents`, `GET /agent/{id}`, the reports, the alert
payloads, the CLI, the MCP server and the UI all pass through one filter. Keys that
look like credentials (`password`, `api_key`, `authorization`, `client_secret`,
`private_key`, …) are replaced with `<redacted>` at any depth, and auth-shaped
values (`Authorization: Basic …`, `https://user:pw@host`) are masked by shape. Each
response says what was hidden:

```bash
opampctl --password "$ADMIN_PASSWORD" agents config get <agent-id>
# {"desired": {...}, "effective": {"redacted": true,
#   "redacted_paths": ["configMap.configMap[].body"], ...}}
```

Base64 bodies (OpAMP bodies are `bytes`, and `MessageToDict` renders them base64)
are decoded, scrubbed and re-encoded — otherwise the filter would see nothing.

**Revealing is a separate, audited action.** `GET /agent/{id}/config?reveal=true`
needs the `approver` role (or higher), writes a `config.reveal` audit row and sets
`Cache-Control: no-store`. It is not exposed over MCP at all: an AI client has no
business reading fleet credentials.

**A redacted body can never be pushed.** A redacted document is a display
artefact; pushing one would silently wipe every credential in the fleet. Any
`PUT /agent/{id}/config` whose body contains `<redacted>` (or the redaction marker)
is a 422.

**Encrypted at rest.** Bodies are sealed with AES-256-GCM, bound to the row they
belong to (AAD = `agent:<id>` / `revision:<id>`), so copying a ciphertext between
rows fails to decrypt instead of handing an agent someone else's config:

```bash
# Generate a key (32 bytes, base64) and keep it out of git
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
CONFIG_AT_REST_KEY=...        # .env or a secret mount
```

Rotation is three steps: set the new `CONFIG_AT_REST_KEY`, put the old key in
`CONFIG_AT_REST_KEY_PREVIOUS`, run `opampctl config rotate-key --db data/opamp.db`,
then drop the previous key. Rows still sealed with a key you no longer supply fail
closed with an error naming the key id — they are never silently replaced with
NULL, so supplying the key later still works.

**Losing the key loses desired config, not the fleet.** Agents keep running the
config they last applied, and the server can rebuild desired state from the
`effective_config` each agent reports. Say that to your incident channel before
you need it.

**Reported configs can contain secrets you did not push.** Upstream warns that the
collector's `reports_raw_config` option exposes the full contents of the collector's
config files. Leave it off unless you trust this server with them.

Retention: superseded revisions keep their hash, author and timestamps but lose
the body after `CONFIG_RETENTION_DAYS` (default 30). The current revision is never
pruned — a restart must not forget the fleet.

## Remote config push

Off by default. When the [posture](#before-you-expose-this-server) is secure and
`OPAMP_CONFIG_PUSH_ENABLED=true`, the server stores a *desired* config per agent
and delivers it — it is desired state, not a command, so it survives restarts and
is idempotent.

```bash
# 1. Validate without storing anything (safe; this is what an AI agent may do)
opampctl --password "$ADMIN_PASSWORD" config validate --file collector.yaml
# {"valid": true, "config_hash": "9101…", "size_bytes": 214, "objects": 1}

# 2. Push to one agent (approver role)
opampctl --password "$ADMIN_PASSWORD" agents config set <agent-id> \
  --file collector.yaml --note "add otlp receiver"

# 3. Watch what the agent says about it
opampctl --password "$ADMIN_PASSWORD" agents config get <agent-id>

# 4. Fleet-wide, explicitly targeted and explicitly confirmed (admin role)
opampctl --password "$ADMIN_PASSWORD" config apply --file collector.yaml \
  --attr environment=prod --confirm

# 5. Withdraw: an empty config map, which tells the agent to run its local config
opampctl --password "$ADMIN_PASSWORD" agents config withdraw <agent-id>

# 6. Deliver now: re-send the active revision over the agent's live connection
#    (no new revision, no approval — a latency optimisation, not a new decision)
opampctl --password "$ADMIN_PASSWORD" agents config deliver <agent-id>
```

How delivery works:

- The agent must advertise `AcceptsRemoteConfig` — the spec's MUST. The
  collector's `extension/opampextension` never does; the **OpAMP Supervisor**
  (`cmd/opampsupervisor`) does, and it is the only agent that can actually apply a
  pushed config (it restarts the collector and auto-reverts on failure).
- The server compares the hash the agent reports (`RemoteConfigStatus.
  last_remote_config_hash`) with its own. Equal → nothing is sent. Different →
  the reply carries `remote_config`. That is the spec's reconciliation rule and
  what stops a re-push loop.
- **Desired state, not a command** (#102, epic #93 D5). The revision is stored
  once and delivered when the agent's hash differs. A live WebSocket connection
  only removes the wait for the next report: the server hands the frame to that
  connection immediately. There is one decision function behind both transports,
  so a push can never deliver something a poll would refuse.
- Every push response carries a `delivery` object instead of a promise:

  | `delivery.state` | Meaning |
  |---|---|
  | `delivered` | Handed to the agent's live WebSocket connection (`channel: "push"`) |
  | `queued` | No live connection, throttled, or refused — it arrives on the agent's next status report (`delivery.detail` says when) |
  | `unchanged` | Nothing to deliver; `delivery.reason` says why (`reported_hash_matches`, `revision_not_deliverable`, `attempts_exhausted`, `agent_does_not_accept`, …) |

  Nothing is ever called **applied** until the agent reports `APPLIED` for our
  hash. `GET /agent/{id}/config` → `assignment.state` is the same vocabulary,
  derived: `none | queued | delivered | applied | failed | poisoned | unconfirmed`.
- Delivery is recorded when the frame is **written**, not when it is queued: a
  dropped or superseded frame consumes no attempt, so the next report re-offers
  it. That is what makes a bounded send queue safe.
- A rollout cannot starve heartbeats on a shared socket: config frames ride a
  per-connection token bucket (`CONFIG_PUSH_RATE_PER_SECOND`,
  `CONFIG_PUSH_BURST`) plus a global in-flight cap
  (`CONFIG_PUSH_MAX_CONCURRENT`), and a throttled frame is simply `queued`.
  `opamp_ws_push_sent_total{outcome}` and `opamp_ws_config_pending` make that
  visible; `opamp_config_delivery_latency_seconds{channel}` measures written →
  `APPLIED`.
- `opampctl agents config deliver <agent-id>` (or `POST
  /agent/{id}/config/deliver`) re-delivers the **already approved** revision now.
  It moves no pointer and needs only `operator`, because there is nothing to
  approve — it is the manual lever for "the queue dropped that frame".
- **Never retried automatically.** A `FAILED` status for our revision poisons it:
  it is surfaced in the API, the UI and the audit log (`config.apply.failed`), and
  will not be sent again. Push a new revision or roll back — a human decision,
  on purpose.
- Hashes are SHA-256 over a length-prefixed, key-sorted encoding of the config
  map. Spec §Calculating Hashes makes the hash server-defined ("the Agent never
  calculates hashes, it only stores and compares them"), which the supervisor
  confirms: it echoes back exactly the hash we sent.
- Validation before storage is structural: shape, size (`CONFIG_MAX_BYTES`),
  YAML/JSON parses, and no redaction marker. A `role` field is rejected because
  this build's proto (v0.18-era) has no `AgentConfigObject.role` — that arrives
  with the v0.20.0 proto sync (#66). Protected invariants and the OPA policy gate
  are #100.

### Propose → approve → roll back (#99)

A direct push is approval-by-fiat: the caller holds `approver`. The two-phase flow
separates the duties, which is the point of having roles at all.

```bash
# 1. Propose: validates, drafts one immutable revision per target agent, delivers
#    NOTHING. Operator role. One of --agent / --agents / --attr is required.
opampctl --password "$ADMIN_PASSWORD" config propose --file collector.yaml \
  --attr environment=prod --note "add otlp receiver"
# {"proposal_id": "prop-…", "status": "proposed", "agents": 3, "verdict": {...}}

# 2. Review the blast radius and the verdict (metadata only, never a body)
opampctl --password "$ADMIN_PASSWORD" config show prop-…
opampctl --password "$ADMIN_PASSWORD" config revisions --status proposed

# 3. Approve: re-resolves the selector, re-runs validation, supersedes and activates
opampctl --password "$ADMIN_PASSWORD" config approve prop-… --note "reviewed"

# 4. Something went wrong: roll back to the previous revision (a new revision,
#    so history stays append-only and the chain records what replaced what)
opampctl --password "$ADMIN_PASSWORD" agents config rollback <agent-id> --note "v2 broke tracing"

# 5. A poisoned revision never returns on a timer — an approver decides
opampctl --password "$ADMIN_PASSWORD" config reactivate prop-… --reason "component now installed"
```

Lifecycle: `proposed → active → superseded | rolled_back | poisoned`, plus
`rejected` from `proposed`. Only `active`, unpoisoned revisions are deliverable.

- **Immutable revisions.** Nothing edits a body. Rollback creates a new revision
  whose `parent_revision_id` names the old one.
- **One active revision per agent.** Approving supersedes the previous one, which
  stays available for rollback for `CONFIG_RETENTION_DAYS`.
- **The selector resolves at approve time**, and the resolved list is stored. An
  agent that starts matching between propose and approve is captured — but if that
  widens the set, the approval is refused until the approver repeats it with
  `confirm_scope_change: true` (`added` / `dropped` report the diff). An agent that
  stops matching is skipped. Agents that arrive *after* approval inherit nothing
  unless the proposer set `include_future` on a selector scope.
- **Auto-revert detection (T9).** The OpAMP Supervisor restores
  `last_working_remote_config.dat` when a new config fails and then reports the
  *old* hash. A naive hash comparison re-pushes the broken config forever. The
  server records `delivered_hash` per agent and treats "a known older revision
  reported after we delivered ours" (past a grace window, or on an explicit
  `FAILED`) as an auto-revert: it poisons the revision and stops sending
  (`config.apply.failed` with `reason: auto_reverted`).
- **Delivery is bounded.** `CONFIG_MAX_DELIVERY_ATTEMPTS` frames per assignment;
  an agent that never acknowledges is surfaced as `unconfirmed` instead of being
  re-pushed forever.
- **Drift** is visible per agent (`GET /agent/{id}/config` → `drift` with the
  missing YAML paths) and fires **one** `config_drift` alert per (agent, revision),
  never one per poll.

### Staged rollouts (#101)

The failure this engine exists for: one typo, four hundred collectors down, and
because their `opamp` extension is no longer running there is no remote way to fix
them (upstream is explicit that an invalid config leaves the collector down until a
manual restart). So a rollout never moves from one agent to two hundred without
evidence that agent one came back healthy.

```
canary (CONFIG_PUSH_CANARY_PERCENT, ≥1 agent)
   → every agent APPLIED *with our hash*      ┐
   → every agent healthy                     │ promotion gate
   → no agent silent since delivery          │ (all must hold)
   → CONFIG_PUSH_HEALTH_GATE_SECONDS elapsed ┘
   → next batch, capped at CONFIG_PUSH_MAX_AGENTS_PER_BATCH *and*
     CONFIG_PUSH_MAX_PERCENT_PER_BATCH → … → completed
```

- **Approval assigns, the rollout releases.** Every revision becomes the agent's
desired config at approval time but is **held** (`desired_configs.held`), so an
HTTP agent polling in the meantime is refused the config (`held_by_rollout`) until
its batch is promoted. The hold is written in the same statement as the
assignment, so there is no window in which a report could collect an unpromoted
config. Any *new* revision releases the hold, which is the escape hatch after an
abort.
- **Deterministic batches.** Targets are sorted, so the same fleet produces the
same plan; the plan is stored per agent (`config_rollout_agents.batch_index`)
rather than recomputed, so a rollout is reproducible in a test and reviewable
before it runs.
- **A failure pauses; it never rolls the fleet back.** Rolling back 300 agents
because one failed is a second outage. The pause carries one reason
(`failed`, `auto_reverted`, `agent_went_silent`, `unhealthy`,
`delivery_unconfirmed`, `superseded`) and the summary is "N applied, K failed,
paused at batch 2". The two decisions stay human: retry the failed agents (an
approver `reactivate`, #99) or roll back the applied ones — which runs as its own
rollout, canary first, gated the same way.
- **The HTTP surface** (#103): `POST /config/rollouts?proposal_id=…` creates a rollout
  (approver; `dry_run: true` plans without approving), `GET /config/rollouts` and
  `GET /config/rollouts/{id}` read it (viewer), `POST /config/rollouts/{id}/abort`
  stops it (operator) and `POST /config/rollouts/{id}/rollback` undoes it as its own
  gated rollout (approver). `POST /config/revisions/{id}/apply` is the same operation
  under the flow's own verb, and `POST /config/revisions/{id}/validate` re-runs the
  pipeline without changing anything. From the shell:
  `opampctl config rollout plan|create|show|abort|rollback <id>`,
  `opampctl config rollouts`, `opampctl config revalidate <proposal>`.
- **Abort stops promotion immediately, and cannot recall.** `abort()` stops the
next batch, leaves already-applied agents exactly as they are (the config is on
the agent and it is running it), and leaves un-promoted agents held so a stopped
rollout can never widen.
- **A stalled rollout expires.** `CONFIG_PUSH_ROLLOUT_TTL_MINUTES` (default 60)
cancels a running or paused rollout, audits `config.rollout.cancelled` with
`reason: ttl_expired`, and leaves the remaining holds in place — an approval
nobody acted on must not fire three weeks later against a fleet that has moved on.
- **Gates are pure functions.** `rollout.batch_gate()` takes the agent facts, the
window and a clock value; it has no database, env or wall-clock dependency, so
"the gate never opened" is an assertion about values. `APPLIED` is only evidence
when the reported hash is *ours*, after base64→hex conversion — naive equality is
how a rollout promotes on an agent that is running something else.
- **One background task, not a thread per rollout.** `rollout_loop()` ticks every
`CONFIG_PUSH_TICK_SECONDS` (default 5) in a worker thread and does nothing at all
when config push is disabled.
- **Observability:** `opamp_config_rollout_agents_total{state}`,
`opamp_config_rollout_paused_total{reason}`,
`opamp_config_rollout_promoted_total{kind}`, `opamp_config_rollout_status{status,kind}`,
`opamp_config_rollout_batch{status}`, and one structured log line per batch
transition (`event=config.rollout.batch`). Pauses, completions, cancellations and
aborts are audit rows.

### What validation can and cannot promise (#100)

The server cannot prove a config will not crash a collector — that is why rollout
staging (#101) and the supervisor's auto-revert exist. It *can* refuse the
recognisable mistakes, and it does, in this order:

1. **Shape and size** — `CONFIG_MAX_BYTES` (1 MiB), and a **redacted** body is
   always refused (pushing one would wipe every credential in the fleet).
2. **Bounded parse** — `yaml.safe_load` only, via a loader that enforces an alias
   budget, a depth budget (`CONFIG_MAX_DEPTH`), an expanded-node ceiling
   (`CONFIG_MAX_NODES`, the alias-bomb guard) and **rejects duplicate keys**
   instead of PyYAML's silent last-wins.
3. **Structural rules** (for a complete config — one with a `service` section):
   every pipeline has at least one receiver and one exporter, and every referenced
   `receivers|processors|exporters|extensions` id exists in the matching section.
   A body with no `service` is treated as a fragment and only warned about; a body
   with no collector sections at all (a PEM file, a routing table) is not judged.
4. **Protected invariants (D7)** — `extensions.opamp` must exist **and** `opamp`
   must be in `service.extensions`, otherwise the agent loses its control channel
   and can never be fixed remotely. Moving the `opamp` endpoint away from this
   server is refused unless the request sets `allow_endpoint_change` **and** the
   approver confirms it.
5. **Warnings that do not block** — no `memory_limiter`, no `batch`, telemetry
   removed, an exporter host outside `CONFIG_ALLOWED_EXPORTER_HOSTS`, and any
   component the target agent does not ship (its reported
   `available_components`).
6. **Policy gate** — the project's OPA policies run against the proposed config.
   Violations block; an `approver` may override with a recorded reason (audited
   as `config.override`). When OPA is disabled the verdict says `skipped`, never
   "passed".

Validation runs at **propose** and again at **approve**: a config approved on
Monday must not be delivered on Friday without re-checking, because the schema,
the policies and the agent's inventory may all have changed (`409 re-validate: …`).

The verdict is `{ok, errors: [{path, message, hint, severity}], warnings, policy,
fingerprint, checked_at}` with YAML paths (`service.pipelines.traces.receivers[0]`)
so a UI or CLI can point at the problem.

## Capacity and deployment topology

**Run exactly one worker.** `AGENT_REGISTRY` is a per-process in-memory dict, so
`--workers > 1` gives each worker only the agents it has personally seen: `GET
/agents`, stale-agent cleanup and config-push targeting then disagree per worker,
and writes serialise on the SQLite write lock anyway (measured: +11% throughput
for p99 93 ms). One worker per container, scale by running more containers behind
a proxy.

Measured on an Apple Silicon laptop (Python 3.13, uvicorn, WAL, token auth on,
2 KB config per message), reproducible with
`scripts/bench_opamp_capacity.py`:

| scenario | msg/s | p50 / p99 |
|---|---|---|
| before #109 (5 full-row writes per heartbeat) | 452 | 2.0 / — ms |
| one worker, 8 concurrent clients, 2 client processes | **1,728** | 2.6 / 5.3 ms |
| one worker, 32 concurrent clients, 4 client processes | 1,983 | 10.5 / 32.0 ms |
| handler only, in process (no uvicorn/auth) | ~27,000 | — |

A heartbeat that changes nothing now performs **zero** writes; a message that
changes N field groups performs exactly one. The stored `last_heartbeat` is
flushed at most once a minute per agent, so a restart sees heartbeats at most
60 s stale.

At 50% headroom that is roughly **26,000 agents at a 30 s poll**, 8,600 at 10 s,
4,300 at 5 s per worker — but model a restart storm (every agent reconnecting at
once) against the p99 column, not the throughput column.

```bash
python3 scripts/bench_opamp_capacity.py                                  # one client process
BENCH_CLIENTS=2 BENCH_THREADS=4 BENCH_AGENTS=2000 python3 scripts/bench_opamp_capacity.py
BENCH_WORKERS=4 python3 scripts/bench_opamp_capacity.py                  # why not to
```

One client process saturates its own GIL at ~1,200 msg/s, so a single-process
client under-reports server capacity — use `BENCH_CLIENTS` to spread the load.

## When an agent counts as gone (#69)

The server learns each agent's reporting cadence instead of guessing it. The
`polling_interval` of a collector is operator-set (`30s` by default, minutes in a
large fleet), so a fixed timeout shorter than it deletes and re-registers a healthy
agent on every cycle — `connected_at` resets, its persisted metrics are dropped, and
`stale_agent` + `new_agent` webhooks fire forever.

An agent is removed once it has been silent for:

```
max(AGENT_TIMEOUT_SECONDS, AGENT_TIMEOUT_INTERVAL_MARGIN × observed_interval)
```

where `observed_interval` is an EWMA of the gaps between its own messages, not a
configured number. Two details make it behave:

- **One gap is not a cadence.** Gaps under a second (two messages in one batch,
  registration followed immediately by the first report) are ignored, and an
  estimate is only trusted after two real gaps — a restart measures its first gap
  from a heartbeat persisted before the restart.
- **Unknown cadence is not "fast".** An agent that has not reported twice yet gets
  `AGENT_FIRST_CONTACT_SECONDS` (15 min): until one full gap is observed, "polls
  every 10 minutes" and "died on startup" look identical.

What that means in practice: a 30s collector is held to the 180s floor (six
intervals of tolerance), a 120s collector to 360s, a 10-minute collector to 30
minutes — and a collector that dies is gone after ~3 missed polls in every case.

Observability, because "my agent disappeared" must be answerable from the API:
`GET /agent/{id}` reports `report_interval_seconds` (the learned cadence) and
`stale_timeout_seconds` (the budget actually applied to it), and the removal log
line carries the silence, the timeout and the interval.

For the same reason, `GET /agent/{id}` reports `last_sequence_num` — the last
`sequence_num` accepted from that agent. A gap against it is what triggers a
`ReportFullState` ask, so "why did the server ask this agent for everything?" is
answerable from the API instead of only from a log line.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_HTTP_SCHEME` | `http` | HTTP scheme |
| `SERVER_ADDRESS` | `localhost` | Server bind address |
| `SERVER_PORT` | `4320` | Server port |
| `AGENT_TIMEOUT_SECONDS` | `180` | Floor on the silence before an agent is removed (#69). An agent reporting more slowly than this is kept for `margin × its observed interval`, so a long `polling_interval` does not flap it |
| `AGENT_TIMEOUT_INTERVAL_MARGIN` | `3` | Multiplier on the agent's learned reporting interval: a silent agent is dropped after ~3 missed polls. `GET /agent/{id}` reports the resulting `stale_timeout_seconds` |
| `AGENT_FIRST_CONTACT_SECONDS` | `900` | Silence budget for an agent that has not reported twice yet, when its interval is still unknown |
| `DATA_DIR` | `data` | SQLite database directory |
| `ADMIN_PASSWORD` | *(empty)* | Admin password for privileged endpoints (see [API](#api-reference)). Empty = read-only mode; set `ADMIN_PASSWORD_FILE` or `OPAMP_ADMIN_AUTOGENERATE_PASSWORD` instead if you prefer |
| `ADMIN_PASSWORD_FILE` | *(empty)* | Read the admin password from a file (secret mount) instead of the environment |
| `OPAMP_ADMIN_AUTOGENERATE_PASSWORD` | `false` | Generate a random admin password on first start, print it once, store it in `$DATA_DIR/admin_password` (0600). `true` in `docker-compose.yml` |
| `ADMIN_SESSION_TTL_SECONDS` | `3600` | Absolute lifetime of a session issued by `POST /auth/session` (#95) |
| `ADMIN_SESSION_IDLE_SECONDS` | `900` | Idle timeout for a session; every use refreshes it (at most one write a minute) |
| `ADMIN_LOGIN_MAX_ATTEMPTS` | `5` | Failed logins per `(name, client IP)` before lockout starts |
| `ADMIN_LOGIN_BACKOFF_SECONDS` | `30` | First lockout wait; doubles per further failure |
| `ADMIN_LOGIN_MAX_BACKOFF_SECONDS` | `900` | Cap on the exponential lockout backoff |
| `TRUST_PROXY_HEADERS` | `false` | Believe `X-Forwarded-For` for the lockout key and audit client. Only behind a proxy you control — otherwise it is forgeable |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins (browser clients) |
| `OPAMP_WS_ENABLED` | `false` | Enable the WebSocket transport on `/v1/opamp` (see [Transports](#transports)) |
| `OPAMP_MAX_MESSAGE_BYTES` | `67108864` | Max OpAMP message size (64 MiB) on both transports; varint header included. HTTP request bodies are capped after gzip decompression. `/v1/metrics` (OTLP) is not affected |
| `OPAMP_WS_OUTBOUND_QUEUE` | `32` | Per-connection WebSocket send queue depth; oldest is dropped when full. A dropped config frame is re-sent by reconciliation, never counted as a delivery (#102) |
| `REPORT_FULL_STATE_MIN_INTERVAL_SECONDS` | `60` | Minimum gap between `ReportFullState` asks for one agent, when its `sequence_num` shows the server missed status. Stops a flapping counter from becoming a request storm; `0` disables the limit (#70) |
| `CONFIG_PUSH_RATE_PER_SECOND` | `20` | Config frames per second allowed on one WebSocket connection. Over the limit the push is `queued` (delivered on the next report), never blocked |
| `CONFIG_PUSH_BURST` | `20` | Burst allowance per connection before the rate applies |
| `CONFIG_PUSH_MAX_CONCURRENT` | `50` | Global cap on config frames queued but not yet written; over it, pushes degrade to `queued` |
| `CONFIG_PUSH_WAIT_SECONDS` | `2` | How long a route waits to hand a push to a live connection's event loop before answering `queued` |
| `OPAMP_CONFIG_PUSH_ENABLED` | `false` | Remote config push master switch. Off = push routes 404, no `remote_config` is ever sent. On = the server refuses to start unless the posture below is secure |
| `OPAMP_AGENT_AUTH` | `off` | Agent authentication on `/v1/opamp`: `off` \| `token` \| `mtls`. `token` requires `Authorization: Bearer <token>` per agent (see [Agent authentication](#agent-authentication-agent--server)); `mtls` is reported but not enforced by this build. `off` logs a startup warning, and config push refuses to start without `token` |
| `PUBLIC_URL` | *(empty)* | Public URL agents/browsers use. `https://` or `wss://` counts as TLS for the posture check |
| `TLS_TERMINATED_UPSTREAM` | `false` | Assert that a TLS terminator you control sits in front of the server (see `docker/`). Assertion only — keep the server on loopback |
| `OPAMP_AUDIT_ENABLED` | `true` | Append-only, hash-chained audit log for privileged actions (#97). **On by default**: a privileged action that cannot be recorded is refused, so turning this off makes the server read-only for writes. Reported in `/health` as `config_push.audit` |
| `AUDIT_RETENTION_DAYS` | `365` | Entries older than this are pruned (at most hourly) with an `audit.checkpoint` row recording the cut. `0` keeps everything |
| `CONFIG_AT_REST_KEY` | *(empty)* | base64-encoded 32-byte AES-256-GCM key for config bodies (#98). A config-push posture requirement. Generate with `python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"` |
| `CONFIG_AT_REST_KEY_PREVIOUS` | *(empty)* | The key you are rotating *from*: accepted for reads only, so `opampctl config rotate-key --db …` can re-encrypt. Clear it when rotation is done |
| `OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE` | `false` | **Local development only.** Store config bodies unencrypted and let push start without a key. Reported as `config_at_rest: plaintext` in `/health` |
| `CONFIG_MAX_BYTES` | `1048576` | Maximum size of one pushed config map (bytes). A collector config is kilobytes |
| `CONFIG_RETENTION_DAYS` | `30` | Superseded config revisions keep hash/author/timestamps but lose the body after this long. The current revision is never pruned. `0` keeps everything |
| `OPAMP_ALLOW_INSECURE_CONFIG_PUSH` | `false` | **Local development only.** Starts with config push enabled on an insecure posture: red warning, `insecure: true` in `/health`, permanent UI banner. See [Before you expose this server](#before-you-expose-this-server) |
| `LOG_FORMAT` | `json` (non-TTY) / `text` (TTY) | Log encoding. JSON is one object per line with `event`, `request_id`, `agent_id`, `trace_id`/`span_id` where relevant. `text` stays readable for local dev |
| `LOG_LEVEL` | `INFO` | Minimum level. Per-request lines for `/health`, `/metrics` and `/v1/metrics` are `DEBUG`; heartbeats log at `DEBUG` |
| `LOG_FILE` | *(empty)* | Optional rotated, gzip-compressed file sink (`LOG_FILE_ROTATION`, `LOG_FILE_RETENTION` tune it) |
| `OPAMP_TRACING_ENABLED` | `false` | Export OTLP spans (`opamp.message`, `opamp.ws.connection`, `opa.evaluate`, `alert.send`, plus HTTP spans). Requires `pip install -e ".[observability]"`; off = no provider, no exporter, no spans |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Standard OTLP/HTTP collector endpoint, used when tracing is on |
| `OTEL_SERVICE_NAME` | `opamp-server` | OTel resource `service.name` |
| `OTEL_TRACES_SAMPLER` / `_ARG` | `parentbased_always_on` | Sampler (`always_on`, `always_off`, `traceidratio`, `parentbased_*`) and its ratio |

### Observability

**Structured logs (#108).** Every line is one JSON object in a container and readable
text on a terminal. Log lines name an **event**, not just a sentence, so a
log-based alert needs no regex:

```bash
docker logs opamp-server | grep '"event":"admin.auth.failed"'
docker logs opamp-server | grep '"event":"config_push.gate_blocked"'
docker logs opamp-server | grep '"event":"posture.insecure"'
```

Every HTTP request gets an `X-Request-ID` (inbound ids are reused when well-formed,
generated otherwise), echoed on the response and bound to every line written while
the request is in flight. One `http.request` line per request at `INFO`;
`/health`, `/metrics` and `/v1/metrics` log at `DEBUG`. Heartbeats and their metric
scrapes are `DEBUG` too — the state transitions are `INFO`.
Logs are scrubbed by the same #98 filter as everything else: no admin password,
agent token, `Authorization` header, OpAMP body or config body reaches a sink,
including a traceback.

**Posture metrics.** In addition to the fleet gauges, `/metrics` exposes:

| Metric | Type | Why |
|---|---|---|
| `opamp_config_push_enabled` | gauge 0/1 | Is the push surface on at all |
| `opamp_config_push_insecure` | gauge 0/1 | **Pager condition**: push is on with an unsafe posture (the #96 override) |
| `opamp_security_posture_requirement{requirement="admin_auth\|agent_auth\|tls\|audit\|config_at_rest"}` | gauge 1/0 | Which requirement is unmet |
| `opamp_config_push_gate_blocked_total` | counter | Someone probed `/config/*` while push was off or insecure |
| `opamp_admin_auth_failures_total{reason}` | counter | Brute force / misconfigured client |

Values are already public on `/health`; nothing secret is added to the scrape.

**Traces (opt-in).** `pip install -e ".[observability]"` then
`OPAMP_TRACING_ENABLED=true` and point `OTEL_EXPORTER_OTLP_ENDPOINT` at your
collector. Spans: `opamp.message` (one per state-changing message, both transports),
`opamp.ws.connection` (one per socket, with close code), `opa.evaluate`,
`alert.send`, plus HTTP spans from the FastAPI instrumentor. Attributes carry ids,
counts and outcomes — never a config body, token or webhook URL query. With tracing
off, no provider or exporter is constructed. **No span per heartbeat.**

## Alerts (Webhook)

Configure via UI at `/alerts` endpoint. Webhook sends JSON with `event_type` and `message`.

| event_type | Trigger |
|-----------|--------|
| `new_agent` | New agent connects |
| `agent_disconnected` | Agent becomes stale |
| `compliance_violation` | OPA policy fails |
| `config_drift` | An agent is not running its approved config. Fires **once** per (agent, revision), not once per poll (#99) |

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

Two separate credential systems, deliberately:

| Channel | Credential | Enabled by |
|---|---|---|
| **Agents** → `POST/WS /v1/opamp` | `Authorization: Bearer opamp_agent_…` | `OPAMP_AGENT_AUTH=token` — see [Agent authentication](#agent-authentication-agent--server) |
| **Humans / tooling** → admin API | session token (`POST /auth/session`) or HTTP Basic | a principal, or the legacy `ADMIN_PASSWORD` |

#### Roles

Every privileged endpoint takes a role. Roles are ordered — a role satisfies every
role below it:

| Role | May | Why it exists |
|---|---|---|
| `viewer` | read everything (alert webhooks redacted) | a dashboard user should not need write access |
| `operator` | propose changes, apply *approved* revisions, start a staged rollout of an approved one, abort a rollout, deliver a config, ask an agent to restart, edit alerts, force compliance checks | the person who runs the fleet |
| `approver` | approve / reject / reactivate, create a staged rollout (which *is* the approval), roll a rollout back | the person who signs off — **deliberately not the same role** as `operator`, so two-phase config push (#99, #103) is an approval and not a formality |
| `admin` | everything, plus identity (principals, sessions) and fleet-wide actions | onboarding and break-glass |

#### Principals (recommended)

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

#### Legacy shared password (deprecated)

`ADMIN_PASSWORD`, `ADMIN_PASSWORD_FILE` or a generated password still works, as one
synthesised `admin` principal. It stays for scripts and existing deployments, with a
startup deprecation warning: there is no per-person identity, no `approver` role, and
no way to revoke one person without changing everyone's password.

```bash
# HTTP Basic: any username, the admin password as the password
curl -u admin:"$ADMIN_PASSWORD" http://localhost:4320/alerts
curl -H "Authorization: Basic $(printf ":$ADMIN_PASSWORD" | base64)" http://localhost:4320/alerts
```

#### Read-only mode (no credential at all)

With nothing configured the server is **read-open and write-closed**: `viewer`
requests are served and every privileged write answers `403` ("admin auth is not
configured"). That is what a dashboard-only deployment wants, and it means an
unconfigured server can no longer be used to change your fleet. Config push also
refuses to start in this state (#96).

#### Lockout

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

#### The Streamlit UI (admin console)

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

### Endpoints

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
| POST | `/v1/opamp` | 🔑 | OpAMP protocol endpoint — agents connect here (protobuf). With `OPAMP_AGENT_AUTH=token` the agent must send `Authorization: Bearer <token>`: 401 without a valid one, 403 when the token is for another `instance_uid`. Malformed input (including an empty `instance_uid`) is 400 with a `BAD_REQUEST` error body; a body over `OPAMP_MAX_MESSAGE_BYTES` (after decompression) is 413; an unsupported `Content-Encoding` is 415 |
| WS | `/v1/opamp` | 🔑 | Same endpoint over WebSocket when `OPAMP_WS_ENABLED=true`; credential checked before `accept()`, refusals close `1008`. Malformed messages (including an empty `instance_uid`) get a `BAD_REQUEST` error frame then close `1002`; oversized frames close `1009` |
| POST | `/v1/metrics` | | OTLP/HTTP metrics ingestion from collectors (JSON or protobuf) |
| GET | `/agents` | | List agents. Filters: `healthy=true\|false\|unknown`, `status=` (remote config status, or alias `remote_config_status=`), or any description attribute, e.g. `?environment=prod`. Repeated params are OR. Each agent carries a `credential` block |
| GET | `/agent/{id}` | | Agent details incl. latest OTLP metrics and `credential` state (`enrolled`, `active`, `last_seen_at`, `revoked_at` — never a token) |
| GET | `/agent/{id}/metrics` | | Latest OTLP metric values for the agent (persisted across restarts) |
| POST | `/agents/{id}/token` | admin | Mint/rotate an agent credential. Returns the token **once** plus a paste-ready collector snippet. Optional body `{"label", "retire_previous"}`. 422 if `id` is not a hex instance UID |
| DELETE | `/agents/{id}/token` | admin | Revoke every live credential for the agent (idempotent). Blocks delivery, keeps the agent's state |
| POST | `/agent/{id}/manifest` | | Generate OCB `manifest.yaml` + build command for a slim collector. Optional body `{"version": "0.123.0"}`. 409 if the agent has no components in use |
| GET | `/agent/{id}/compliance` | | Evaluate agent against OPA policies (no-op result if OPA disabled) |
| GET | `/agent/{id}/report` | | Markdown report for one agent (versions, health, components) |
| GET | `/agent/{id}/config` | viewer | Desired (pushed) + effective (reported) config, **redacted** with `redacted_paths`. `?reveal=true` needs `approver`, is audited (`config.reveal`) and sets `Cache-Control: no-store`. Includes revision `history` (metadata only) |
| PUT | `/agent/{id}/config` | approver | Push a desired config: `{"config_map": {"": {"body": "<yaml>", "content_type": "text/yaml"}}, "note": "…"}`. 404 when push is disabled, 422 with every validation error, including a redacted body. Creates an immutable revision |
| DELETE | `/agent/{id}/config` | approver | Withdraw by pushing an **empty** config map (agent falls back to its local config). Delivered once |
| POST | `/agent/{id}/config/deliver` | operator | Deliver the active revision over the agent's live WebSocket now. Idempotent: always 200 with `delivery.state` = `delivered` \| `queued` \| `unchanged` (+ `reason`). Moves no pointer, creates no revision, so it needs no approval. Audited as `config.deliver`; a refusal on principal grounds adds a second row with `result: "error"` |
| POST | `/agent/{id}/config/rollback` | approver | Return to the previous revision as a **new** revision with `parent_revision_id` set. 409 when there is nothing to roll back to |
| POST | `/config/apply` | admin | Push one config to many agents: `{"config_map", "agents": [...] \| "filter": {...}, "confirm": true}`. No selector is a 400 — never "all agents" |
| POST | `/config/revisions` | operator | **Propose** a revision — validates and drafts it, delivers nothing: `{"config_map", "scope": {"agent_id" \| "agents" \| "filter", "include_future"}, "note"}`. Returns `proposal_id`, per-agent revisions and the full verdict. 422 with every structured error |
| GET | `/config/revisions` | viewer | Revision metadata (no bodies): `?agent=`, `?status=proposed\|active\|superseded\|rolled_back\|poisoned\|rejected`, `?limit=` |
| GET | `/config/revisions/{proposal_id}` | viewer | One proposal: each revision's status, stored verdict and delivery record (`attempts`, `last_status`, `poisoned`) |
| POST | `/config/revisions/{id}/approve` | approver | Re-resolve the selector, **re-run validation**, supersede and activate. 409 `re-validate: …` when the config no longer passes, and 409 when the selector now matches *more* agents (repeat with `confirm_scope_change: true`); `override_reason` overrides a policy violation (audited as `config.override`) |
| POST | `/config/revisions/{id}/reject` | approver | Refuse a proposal; the revision is kept as evidence |
| POST | `/config/revisions/{id}/reactivate` | approver | Resume a **poisoned** revision after a fresh validation pass. Nothing retries on a timer |
| POST | `/config/validate` | viewer | Validate a config map without storing it: `{"config_map", "agent_id"?, "allow_endpoint_change"?}`. Always 200: `{valid, errors, details, warnings, policy, fingerprint, config_hash, size_bytes, objects}` |
| GET | `/reports/fleet` | | Full fleet summary report (markdown, same as the UI Reports page) |
| GET | `/reports/heavy-collectors` | | Collectors with many unused components. Param: `threshold` (0–1, default 0.5) |
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
| GET | `/metrics` | | Prometheus metrics |

`Role` is the minimum role; roles are ordered (`viewer` < `operator` < `approver` <
`admin`), so an `approver` may do what an `operator` may. `—` means no credential. In
read-only mode (nothing configured) reads still work and every non-`—` row answers
`403`.

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
opampctl config posture                     # config push posture + why each requirement fails

# Agents
opampctl agents list --healthy true --attr environment=prod
opampctl agents get <agent-id>              # full details incl. metrics
opampctl agents metrics <agent-id>          # latest OTLP metric values
opampctl agents manifest <agent-id> --raw   # OCB manifest.yaml for a slim build
opampctl agents manifest <agent-id> --version 1.2.3
opampctl agents compliance <agent-id>       # OPA evaluation (no-op if OPA disabled)

# Agent credentials (admin; OPAMP_AGENT_AUTH=token)
opampctl agents token create <agent-id> --label prod-1   # token (once) + collector snippet
opampctl agents token create <agent-id> --raw            # just the token, for $(...)
opampctl agents token create <agent-id> --retire-previous  # rotate with an immediate cutover
opampctl agents token revoke <agent-id>                  # blocks delivery, keeps agent state

# Remote config (#81) — config push must be enabled and the posture secure
opampctl agents config get <agent-id>        # desired + effective, REDACTED
opampctl agents config get <agent-id> --reveal   # real values: approver role, audited
opampctl agents config set <agent-id> --file collector.yaml --note "add otlp receiver"
opampctl agents config withdraw <agent-id>   # empty config map: run the local config
opampctl config validate --file collector.yaml   # side-effect-free check + hash + verdict
opampctl config apply --file collector.yaml --attr environment=prod --confirm  # admin
opampctl config rotate-key --db data/opamp.db    # re-encrypt under the current key

# Config control plane (#99/#100): propose -> approve -> roll back
opampctl config propose --file collector.yaml --attr environment=prod --note "why"
opampctl config revisions --status proposed      # metadata only, never a body
opampctl config show prop-…                      # one proposal, per-agent verdicts
opampctl config approve prop-… --note "reviewed" # approver: re-resolves + re-validates
opampctl config reject prop-… --reason "wrong tenant"
opampctl config reactivate prop-… --reason "component now installed"
opampctl agents config rollback <agent-id> --note "v2 broke tracing"
opampctl --db data/opamp.db config revisions     # offline: read the store directly

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

# Alerts (operator to write; webhook secrets come back redacted on read)
opampctl alerts get
opampctl alerts set @alerts.json            # JSON from file (or inline JSON)
opampctl alerts test --event-type new_agent

# Audit log (#97) — who did what
history = oc.audit_log(action="config.", limit=50)   # `action` is a prefix match
for entry in history["entries"]:
    print(entry["ts"], entry["actor"], entry["action"], entry["result"])
print(oc.audit_verify())          # {"ok": True, ...} — tamper check over the hash chain
print(oc.audit_export(format="jsonl"))   # raw text, oldest first

# Admin identity and sessions (#95)
opampctl auth status                        # auth_mode, roles, live principals
opampctl auth whoami                        # your name, role, session expiry
opampctl auth login --name bob --raw        # session token -> export OPAMP_ADMIN_TOKEN
opampctl auth logout                        # revoke this session
opampctl auth sessions list                 # live sessions (admin)
opampctl auth sessions revoke <session-id>  # sign one device out (admin)
opampctl auth principals list               # identities and roles (admin)
opampctl auth principals create bob --role operator --password '…'
opampctl auth principals revoke <principal-id>   # closes their sessions too
```

`--token` / `OPAMP_ADMIN_TOKEN` sends a session token (preferred); `--password` /
`ADMIN_PASSWORD` sends HTTP Basic (legacy, for scripts). When both are set the token
wins.

### CLI configuration

| Source | Flags | Env vars |
|--------|-------|----------|
| Server URL | `--server`, `-s` | `OPAMP_SERVER_URL` |
| Admin session token | `--token`, `-t` | `OPAMP_ADMIN_TOKEN` (wins over the password) |
| Admin password (legacy Basic) | `--password`, `-p` | `ADMIN_PASSWORD` |

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
exposes 29 typed tools mirroring the API: `list_agents`, `get_agent`,
`get_agent_metrics`, `generate_manifest`, `fleet_report`, `heavy_collectors_report`,
`outdated_collectors_report`, `get_compliance`, `check_compliance`,
`compliance_summary`, `list_policies`, `validate_policies`, `reload_policies`,
`get_alerts`, `update_alerts`, `test_alerts`, `agent_report`, `health`, `auth_status`,
`whoami`, `config_posture`, `agent_config`, `validate_config`,
`list_config_revisions`, `list_config_rollouts`, `get_config_rollout`, `audit_log`,
`audit_verify`, `audit_export`.

**Identity is never delegated to MCP.** Logins, principals and sessions have no MCP
tools at all — minting an identity is a human/IaC action, not a tool call an AI can
be talked into. `whoami` exists so an agent can *see* which role it is acting as
before suggesting a privileged step.

**Reading the audit log is always allowed.** `audit_log`, `audit_verify` and
`audit_export` are read-only tools and are never gated: an AI agent asked "who
pushed this" must be able to answer from evidence, not from its own summary.

**Write tools are absent, not failing.** `create_agent_token`, `revoke_agent_token`
and `propose_config` only exist when the MCP process is started with
`OPAMP_MCP_ALLOW_WRITES=true` (#94 introduced this gate for credential minting;
#106 applies it to every write tool). An agent that cannot see a tool will not
try to use it.

**An AI may propose, never approve.** `propose_config` creates a *draft* revision —
nothing is delivered until a human with the `approver` role approves it in the CLI,
UI or API. There is no MCP tool for approve, reject, reactivate, rollback, apply,
withdraw or **deliver** at any setting: a client that can approve its own proposal
has no separation of duties, and one that can *deliver* would make the proposal
arrive on its own schedule (D3/D9, #102).

Transport is **stdio** by default (works with every local MCP client); use
`--transport sse --port 8765` for remote agents. Configure with the same env vars
as the CLI (`OPAMP_SERVER_URL`, `ADMIN_PASSWORD`), plus
`OPAMP_MCP_ALLOW_WRITES` if you really want credential minting exposed to an AI client.

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

# Remote config (#81): validate, push, inspect (redacted by default)
print(oc.validate_config({"": {"body": open("collector.yaml").read(), "content_type": "text/yaml"}}))
oc.set_agent_config(agent_id, {"": {"body": yaml_text, "content_type": "text/yaml"}})
print(oc.get_agent_config(agent_id)["desired"]["revision_id"])
oc.delete_agent_config(agent_id)          # withdraw
oc.apply_agent_config(config_map, agents=[agent_id], confirm=True)

# Config control plane (#99/#100): propose -> approve -> roll back
proposal = oc.propose_config(config_map, scope={"agent_id": agent_id}, note="why")
print(proposal["verdict"])                # {ok, errors, warnings, policy, fingerprint}
oc.approve_config(proposal["proposal_id"], note="reviewed")   # approver role
print(oc.get_agent_config(agent_id)["drift"])                 # is it running it?
oc.rollback_agent_config(agent_id, note="v2 broke tracing")
print(oc.list_config_revisions(status="poisoned"))            # metadata only

# Agent credentials (admin) — the token is returned once
token = oc.create_agent_token("4f3c2b1a...", label="prod-1")["token"]
print(oc.get_agent("4f3c2b1a...")["credential"])
print(oc.revoke_agent_token("4f3c2b1a..."))   # {"revoked": 1}

# Audit log (#97) — who did what
history = oc.audit_log(action="config.", limit=50)   # `action` is a prefix match
for entry in history["entries"]:
    print(entry["ts"], entry["actor"], entry["action"], entry["result"])
print(oc.audit_verify())          # {"ok": True, ...} — tamper check over the hash chain
print(oc.audit_export(format="jsonl"))   # raw text, oldest first

# Admin identity and sessions (#95)
session = oc.login("bob", "…")             # POST /auth/session -> token, role, expiry
scoped = OpampClient(base_url="http://localhost:4320", token=session["token"])
print(scoped.whoami())                     # {"name": "bob", "role": "operator", ...}
print(scoped.create_principal("carol", "approver", "…"))   # admin role only
scoped.logout()

try:
    oc.get_agent("nope")
except OpampApiError as e:
    print(e.status_code, e.detail)     # 404 Agent not found
```

## Development

```bash
pip install -r requirements.txt
pip install -r requirements-ui.txt
pip install -e ".[dev]"
pytest tests/ -v                                   # logging defaults to JSON in CI
LOG_FORMAT=text pytest tests/ -v                   # readable local output
pip install -e ".[dev,observability]" && OPAMP_TRACING_ENABLED=true pytest tests/ -v
```

## Project Structure

```
opamp-server-py/
├── server/          # FastAPI server (logging.py/tracing.py = observability, #108)
├── client/          # opamp_client — shared HTTP client library
├── cli/             # opampctl CLI (typer, JSON output)
├── mcp_server/      # FastMCP server (stdio/SSE) for AI agents
├── ui/              # Streamlit dashboard
├── proto/           # Protobuf definitions
├── tests/           # Tests (fake_agent.py = scriptable OpAMP Supervisor, #107)
├── collector/       # Sample configs
├── docker/          # TLS terminator examples (nginx, Caddy) for #96
└── data/            # SQLite DB (+ generated admin_password)
```