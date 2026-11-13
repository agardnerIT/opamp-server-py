# Configuration

Under Compose, set any of these in `.env` — it is the single source of truth: both
services load the whole file, and the compose file restates only container topology
(see the notes there). Running the server directly, `server/main.py` loads the same
file with `python-dotenv` (shell variables win, matching Compose). The default column
is the app's own fallback, which is what applies when a line is absent from `.env`.

`.env.sample` is the committed template: copy it and every setting is there, commented,
with its default.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_HTTP_SCHEME` | `http` | HTTP scheme |
| `SERVER_ADDRESS` | `localhost` | Server bind address |
| `SERVER_PORT` | `4320` | Server port |
| `AGENT_TIMEOUT_SECONDS` | `180` | Floor on the silence before an agent is removed (#69). An agent reporting more slowly than this is kept for `margin × its observed interval`, so a long `polling_interval` does not flap it. See [transports.md](transports.md#when-an-agent-counts-as-gone-69) |
| `AGENT_TIMEOUT_INTERVAL_MARGIN` | `3` | Multiplier on the agent's learned reporting interval: a silent agent is dropped after ~3 missed polls. `GET /agent/{id}` reports the resulting `stale_timeout_seconds` |
| `AGENT_FIRST_CONTACT_SECONDS` | `900` | Silence budget for an agent that has not reported twice yet, when its interval is still unknown |
| `DATA_DIR` | `data` | SQLite database directory |
| `ADMIN_PASSWORD` | *(empty)* | Admin password for privileged endpoints (see [security.md](security.md#authentication)). Empty = read-only mode; set `ADMIN_PASSWORD_FILE` or `OPAMP_ADMIN_AUTOGENERATE_PASSWORD` instead if you prefer |
| `ADMIN_PASSWORD_FILE` | *(empty)* | Read the admin password from a file (secret mount) instead of the environment |
| `OPAMP_ADMIN_AUTOGENERATE_PASSWORD` | `false` | Generate a random admin password on first start, print it once, store it in `$DATA_DIR/admin_password` (0600). The compose server defaults it to `true` so a fresh stack has a way in; `.env` still overrides |
| `ADMIN_SESSION_TTL_SECONDS` | `3600` | Absolute lifetime of a session issued by `POST /auth/session` (#95) |
| `ADMIN_SESSION_IDLE_SECONDS` | `900` | Idle timeout for a session; every use refreshes it (at most one write a minute) |
| `ADMIN_LOGIN_MAX_ATTEMPTS` | `5` | Failed logins per `(name, client IP)` before lockout starts |
| `ADMIN_LOGIN_BACKOFF_SECONDS` | `30` | First lockout wait; doubles per further failure |
| `ADMIN_LOGIN_MAX_BACKOFF_SECONDS` | `900` | Cap on the exponential lockout backoff |
| `TRUST_PROXY_HEADERS` | `false` | Believe `X-Forwarded-For` for the lockout key and audit client. Only behind a proxy you control — otherwise it is forgeable |
| `UPDATE_SUBSCRIBE_URL` | *(empty)* | Public URL of the release-email Worker. When set, the Release emails page shows a signup box; the server stores no address. Empty hides it (#46) |
| `UPDATE_CHECK` | `on` | Automatic release check (#46). `on` or `off`; `off` wins over the admin toggle and is not recommended |
| `UPDATE_CHECK_INTERVAL_HOURS` | `24` | Minimum time between automatic checks |
| `UPDATE_CHECK_JITTER` | `0.1` | Fractional jitter added to the interval so installs do not synchronise |
| `UPDATE_CHECK_TIMEOUT_SECONDS` | `5` | GitHub request timeout; one attempt, no retries |
| `UPDATE_HTTP_PROXY` | *(empty)* | Explicit proxy for the check. The check does not use `HTTP(S)_PROXY` or `.netrc` |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins (browser clients) |
| `OPAMP_WS_ENABLED` | `true` | Accept a WebSocket upgrade on `/v1/opamp` (see [transports.md](transports.md)). `false` refuses it with `1008`; HTTP is unaffected either way |
| `OPAMP_MAX_MESSAGE_BYTES` | `67108864` | Max OpAMP message size (64 MiB) on both transports; varint header included. HTTP request bodies are capped after gzip decompression. `/v1/metrics` (OTLP) is not affected |
| `OPAMP_WS_OUTBOUND_QUEUE` | `32` | Per-connection WebSocket send queue depth; oldest is dropped when full. A dropped config frame is re-sent by reconciliation, never counted as a delivery (#102) |
| `REPORT_FULL_STATE_MIN_INTERVAL_SECONDS` | `60` | Minimum gap between `ReportFullState` asks for one agent, when its `sequence_num` shows the server missed status. Stops a flapping counter from becoming a request storm; `0` disables the limit (#70) |
| `OPAMP_DUPLICATE_UID_DETECTION` | `true` | Detect two agents presenting one `instance_uid` and answer with `agent_identification.new_instance_uid` (#75). `false` disables the check entirely (nothing is observed or counted) |
| `OPAMP_UID_HEADER_STRICT` | `false` | `true` makes an `OpAMP-Instance-UID` header that disagrees with the message body a `400 BAD_REQUEST` (#76). Default: log both uids, trust the body |
| `OPAMP_THROTTLE_ENABLED` | `false` | Refuse agent messages with `503` + `Retry-After` when the server is behind (#78). Off by default; `/health`, `/metrics` and admin routes are never throttled. See [operations.md](operations.md#overload-shedding-78) |
| `OPAMP_THROTTLE_MAX_LAG_SECONDS` | `1.0` | The overload signal: how late the event loop wakes from a 100 ms sleep. Above it, agent requests are refused |
| `OPAMP_THROTTLE_RETRY_AFTER_SECONDS` | `30` | `Retry-After` value. Raised to 30 if configured lower — the spec's minimum recommendation |
| `OPAMP_THROTTLE_REAPER_GRACE_SECONDS` | `180` | How long the stale-agent reaper stands down after the last refusal, so a throttled agent is not deleted as dead (#69) |
| `CONFIG_PUSH_RATE_PER_SECOND` | `20` | Config frames per second allowed on one WebSocket connection. Over the limit the push is `queued` (delivered on the next report), never blocked |
| `CONFIG_PUSH_BURST` | `20` | Burst allowance per connection before the rate applies |
| `CONFIG_PUSH_MAX_CONCURRENT` | `50` | Global cap on config frames queued but not yet written; over it, pushes degrade to `queued` |
| `CONFIG_PUSH_WAIT_SECONDS` | `2` | How long a route waits to hand a push to a live connection's event loop before answering `queued` |
| `OPAMP_CONFIG_PUSH_ENABLED` | `false` | Remote config push master switch. Off = push routes 404, no `remote_config` is ever sent. On = the server refuses to start unless the posture is secure (see [security.md](security.md#before-you-expose-this-server)) |
| `OPAMP_AGENT_AUTH` | `token` | The only supported agent-auth mode (#125). Unset means `token`; `off`, `mtls` and unknown values refuse to start, as does the removed `OPAMP_ALLOW_UNAUTHENTICATED_AGENTS`. Every agent must send `Authorization: Bearer <token>` — see [agent-tokens.md](agent-tokens.md) |
| `PUBLIC_URL` | *(empty)* | Public URL agents/browsers use. `https://` or `wss://` counts as TLS for the posture check |
| `TLS_TERMINATED_UPSTREAM` | `false` | Assert that a TLS terminator you control sits in front of the server (see `docker/`). Assertion only — keep the server on loopback |
| `OPAMP_AUDIT_ENABLED` | `true` | Append-only, hash-chained audit log for privileged actions (#97). **On by default**: a privileged action that cannot be recorded is refused, so turning this off makes the server read-only for writes. Reported in `/health` as `config_push.audit` |
| `AUDIT_RETENTION_DAYS` | `365` | Entries older than this are pruned (at most hourly) with an `audit.checkpoint` row recording the cut. `0` keeps everything |
| `CONFIG_AT_REST_KEY` | *(empty)* | base64-encoded 32-byte AES-256-GCM key for config bodies (#98). A config-push posture requirement. Generate with `python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"` |
| `CONFIG_AT_REST_KEY_PREVIOUS` | *(empty)* | The key you are rotating *from*: accepted for reads only, so `opampctl config rotate-key --db …` can re-encrypt. Clear it when rotation is done |
| `OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE` | `false` | **Local development only.** Store config bodies unencrypted and let push start without a key. Reported as `config_at_rest: plaintext` in `/health` |
| `CONFIG_MAX_BYTES` | `1048576` | Maximum size of one pushed config map (bytes). A collector config is kilobytes |
| `CONFIG_RETENTION_DAYS` | `30` | Superseded config revisions keep hash/author/timestamps but lose the body after this long. The current revision is never pruned. `0` keeps everything |
| `OPAMP_ALLOW_INSECURE_CONFIG_PUSH` | `false` | **Local development only.** Starts with config push enabled on an insecure posture: red warning, `insecure: true` in `/health`, permanent UI banner. See [security.md](security.md#before-you-expose-this-server) |
| `LOG_FORMAT` | `json` (non-TTY) / `text` (TTY) | Log encoding. JSON is one object per line with `event`, `request_id`, `agent_id`, `trace_id`/`span_id` where relevant. `text` stays readable for local dev |
| `LOG_LEVEL` | `INFO` | Minimum level. Per-request lines for `/health`, `/metrics` and `/v1/metrics` are `DEBUG`; heartbeats log at `DEBUG` |
| `LOG_FILE` | *(empty)* | Optional rotated, gzip-compressed file sink (`LOG_FILE_ROTATION`, `LOG_FILE_RETENTION` tune it) |
| `OPAMP_TRACING_ENABLED` | `false` | Export OTLP spans (`opamp.message`, `opamp.ws.connection`, `opa.evaluate`, `alert.send`, plus HTTP spans). Requires `pip install -e ".[observability]"`; off = no provider, no exporter, no spans |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Standard OTLP/HTTP collector endpoint, used when tracing is on |
| `OTEL_SERVICE_NAME` | `opamp-server` | OTel resource `service.name` |
| `OTEL_TRACES_SAMPLER` / `_ARG` | `parentbased_always_on` | Sampler (`always_on`, `always_off`, `traceidratio`, `parentbased_*`) and its ratio |

Logging, metrics and traces are covered in
[operations.md](operations.md#observability).

## Alerts (Webhook)

Configure via UI at `/alerts` endpoint. Webhook sends JSON with `event_type` and `message`.

| event_type | Trigger |
|-----------|---------|
| `new_agent` | New agent connects |
| `agent_disconnected` | Agent becomes stale |
| `compliance_violation` | OPA policy fails |
| `config_drift` | An agent is not running its approved config. Fires **once** per (agent, revision), not once per poll (#99) |

## OPA (Optional)

Run OPA server, then set `OPA_ENABLED=true`.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPA_ENABLED` | `false` | Enable OPA compliance |
| `OPA_URL` | `http://localhost:8181` | OPA server URL (with the compose `opa` profile it is `http://opa:8181`, the service name) |
| `POLICIES_DIR` | `policies/tags` | Policies directory |

### Policies
```bash
docker run --rm -it -p 8181:8181 -v $(pwd)/policies:/policies \
  openpolicyagent/opa run --server --bundle /policies --watch --addr :8181
```

Add `package opamp.agent.compliance.<name>` policies to `policies/tags/`.

## Update checks (#46)

Once per 24 h the server asks GitHub for the latest release. The UI shows the
current version, a banner when a newer one exists, and copyable upgrade commands.
Nothing is ever upgraded automatically.

- **On by default.** `UPDATE_CHECK=off` disables it for the deployment and wins
  over the admin toggle. Off is not recommended: you may miss security fixes.
- **Updates** (Admin section) has the toggle, "Check now", and "Use deployment default".
  The admin override is stored in `data/update-settings.json`; the deployer's
  `off` always wins.
- **The request is anonymous.** One `GET` to GitHub's releases API, with no
  current version, instance ID, hostname, fleet data, cookies, `.netrc`, or
  proxy environment. The comparison is local. `UPDATE_HTTP_PROXY` is the only
  explicit proxy hook.
- `GET /updates` is a cached read and never performs network I/O.
  `POST /updates/check` is admin-only and rate-limited to one check a minute.
- `opampctl updates status|check|enable|disable|default`; MCP tools
  `get_update_status`, `check_for_updates`, and (with writes enabled)
  `set_update_check`.

Full design: [updates-available.md](updates-available.md).

## Release emails (optional)

The **Release emails** page can show a signup box for project release
announcements. It is an iframe to a Cloudflare Worker; the address goes browser →
Worker → the project's Resend segment. This server never receives or stores it, and
the Worker has no endpoint to list subscribers.

The project's Worker URL is the default, so the box appears without any
configuration. Set `UPDATE_SUBSCRIBE_URL` to a different URL to use your own
Worker, or to `off` to hide the box:

```bash
UPDATE_SUBSCRIBE_URL=off
```

Double opt-in is mandatory: nothing is added to Resend until the recipient clicks
the confirmation link. Every broadcast carries Resend's unsubscribe link. The
Worker's setup, data retention and tests are in
[`cloudflare/updates-worker/README.md`](../cloudflare/updates-worker/README.md);
the design is in [release-emails.md](release-emails.md).
