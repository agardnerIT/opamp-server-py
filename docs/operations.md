# Running it: capacity, overload and the SQLite trap

Deployment shape, what happens when the box is behind, and the one infrastructure
mistake that looks like data corruption. Environment variables are in
[configuration.md](configuration.md).

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

## Overload shedding (#78)

When the box is genuinely behind, the polite failure is a refusal the client already
understands. With `OPAMP_THROTTLE_ENABLED=true`, `POST /v1/opamp` answers
**`503` + `Retry-After`** instead of accepting work it cannot finish. The WebSocket
transport refuses the **upgrade** the same way — `503` + `Retry-After` on the
handshake — because that is the backoff a real client applies. The spec's in-band
`ServerErrorResponse{UNAVAILABLE, retry_info}` was implemented first and measured
against `otelcol-contrib` 0.162 (opamp-go v0.24): the client ignores `retry_info` on
an established socket, tears it down on any `error_response` and reconnects
immediately — a restart stampede became ~300 reconnects/s per agent, so the throttle
amplified the load it was meant to shed. `Retry-After` on a refused upgrade *is*
honoured (verified: 30.0 s apart). An already-open socket is therefore never cut off
mid-stream; its next reconnect is what gets shed. 503 rather than 429 because 429
means "you sent too much" and this means "I cannot take it"; the spec allows either.

**One signal: event-loop lag.** With one worker on one event loop and a synchronous
message handler, an in-flight request counter never rises above ~1 and registry size
measures fleet size, not load. Lag does mean the backlog: a sampler wakes every
100 ms and compares when it should have woken with when it did. Over
`OPAMP_THROTTLE_MAX_LAG_SECONDS` (default `1.0`; measured p99 request latency is
~32 ms at 32 concurrent clients), new agent messages are refused.

Four decisions worth knowing:

- **Off by default.** For a handful of collectors, overload means the box is already
  in trouble and throttling only makes the failure polite. Turn it on for restart
  stampedes, autoscaler signals, or a fleet with a very short `polling_interval`.
- **`/health`, `/metrics` and every admin route are exempt by construction.** Only
  the agent protocol path is gated. Throttling your own monitoring turns a slow
  server into a blind one.
- **The gate releases as soon as the lag does.** `Retry-After` is a promise, not a
  cooldown: a recovered server accepts work on the next poll.
- **The stale reaper stands down** while shedding, and for
  `OPAMP_THROTTLE_REAPER_GRACE_SECONDS` (default `180`) after the last refusal. A
  throttled agent is silent on purpose — deleting it would reset `connected_at`, drop
  its persisted metrics and fire `stale_agent` + `new_agent` for a client that is
  doing exactly what it was told (#69). It is counted in
  `opamp_stale_reaper_deferrals_total`, so "the reaper stopped" is visible.
- **The refusal happens before the body is read**, so a shedding server does not
  decompress and parse the message it is about to reject — and an unauthenticated
  request is still `401`: identity is checked before load.

`Retry-After` never goes below **30 s**; the spec says "the minimum recommended retry
interval is 30 seconds", so a configured `5` is raised rather than honoured. Verify
it the way an agent sees it:

```bash
curl -si -X POST localhost:4320/v1/opamp --data-binary @msg.bin | head -3
# HTTP/1.1 503 Service Unavailable
# retry-after: 30
curl -s localhost:4320/health | jq .throttle
# {"enabled": true, "max_lag_seconds": 1.0, "retry_after_seconds": 30,
#  "lag_seconds": 2.4, "shedding": true}
```

## If every write answers 503

`data/opamp.db` is SQLite, and SQLite is only safe on a filesystem that implements
POSIX locking and `mmap` correctly. A **Docker bind mount from macOS** (virtiofs) is
not one of those: in practice the database can report `database disk image is
malformed` to the running process while `PRAGMA integrity_check` on the same file, from
the host, returns `ok`. Every write then returns 503 until the process is restarted,
because the process keeps its stale view of the write-ahead log.

So the compose file does not bind-mount the data directory. It uses a named volume:

```yaml
services:
  server:
    volumes:
      - opamp-data:/app/data
volumes:
  opamp-data:
```

A named volume is a directory in the VM's own filesystem, so the failure cannot
happen. Keep it that way on Docker Desktop; a bind mount is safe on a Linux host with
a native filesystem, and only there. `docker compose down` keeps the volume —
`down -v` deletes it, admin password included.

If you inherited a `./data:/app/data` deployment and the database is already damaged,
switch to the volume and start from a clean database rather than repairing one:

```bash
# copy the non-database state out of the bind mount, then let the volume boot empty
docker compose down
mkdir -p /tmp/opamp-data-keep
cp -f data/admin_password /tmp/opamp-data-keep/ 2>/dev/null || true
docker compose up -d --wait
for f in /tmp/opamp-data-keep/*; do docker compose cp "$f" "server:/app/data/$(basename "$f")"; done
docker compose restart server
```

What the server does about it in the meantime:

- the boot log carries one line, `state database check: ok` (or the failure);
- a state-database error answers **503** with `Retry-After` and a message that says
  what to do, instead of a bare 500;
- the handler never attempts a repair. Rewriting a database underneath the process
  that just failed to read it is how a bad afternoon becomes data loss.

One diagnostic worth knowing, because it looks like the opposite problem: a *host-side*
`PRAGMA integrity_check` run against a bind-mounted file returns `ok` while the server
reports corruption, and a run against a **copy** of that file reproduces the corruption.
The file really is damaged; the live reader is the one that is wrong. The copy is the
one to believe.

`docker compose cp server:/app/data/opamp.db data/` gets the live database onto the
host for `opampctl --db data/opamp.db`, which never runs inside the container.

## Observability

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

**Overload (#78) metrics.** Only meaningful when `OPAMP_THROTTLE_ENABLED=true`, but
always published, because "were we behind?" is the first question after a slow day:

| Metric | Type | Why |
|---|---|---|
| `opamp_event_loop_lag_seconds` | gauge | The signal itself: how late the loop woke from a 100 ms sleep |
| `opamp_throttled_requests_total{transport="http\|ws"}` | counter | How many agent messages were refused, by transport |
| `opamp_stale_reaper_deferrals_total` | counter | Cleanup ticks that stood down because the server was shedding load |

**Component inventory (#77) metrics.** A hash-only agent is a silent feature failure —
the manifest endpoint simply answers 409 — so the asks are numbers:

| Metric | Type | Why |
|---|---|---|
| `opamp_available_components_requested_total{reason="hash_only\|hash_changed"}` | counter | `ReportAvailableComponents` asks sent, by why |
| `opamp_available_components_ignored_total` | counter | Agents that kept sending a hash after the ask (an incomplete SDK) |

**Traces (opt-in).** `pip install -e ".[observability]"` then
`OPAMP_TRACING_ENABLED=true` and point `OTEL_EXPORTER_OTLP_ENDPOINT` at your
collector. Spans: `opamp.message` (one per state-changing message, both transports),
`opamp.ws.connection` (one per socket, with close code), `opa.evaluate`,
`alert.send`, plus HTTP spans from the FastAPI instrumentor. Attributes carry ids,
counts and outcomes — never a config body, token or webhook URL query. With tracing
off, no provider or exporter is constructed. **No span per heartbeat.**

Config push and rollout metrics are listed in
[remote-config.md](remote-config.md#staged-rollouts-101).
