# Connecting collectors

This server works with **any collector distro that ships
`extension/opampextension`** — `otelcol-contrib`, a core build with the extension
compiled in, a supervisor-managed collector, or a custom distro. Nothing has to be
rebuilt to connect one: adding the block below is the whole integration.

That is a hard product rule, not an accident of the current code: **using this
server must never require a proprietary or bespoke collector build.** Everything
here is Apache-2.0 (see [LICENSE](../LICENSE)), every component it talks to is
upstream OpenTelemetry, and a stock `otelcol-contrib` is always supported. If a
capability ever needs a component that upstream does not have, it will be published
under an OSS license together with a written explanation of why it is needed —
none is required today.

The README's `### Connect a collector` section is the copy-paste minimum (HTTP
transport plus the required `headers:` block). This page covers the variants.

`collector/e2e.yaml` is the known-good config that CI runs against a real
`otelcol-contrib` on every PR (`.github/workflows/collector-e2e.yml`, #113), so it
is also the shortest path to a connected agent. It includes the `headers:` block,
because an agent token is mandatory (#125) — a config without one is not "known
good", it is a 401. CI mints a fleet token and passes it to the container as
`OPAMP_AGENT_TOKEN`, which is why the committed YAML carries the header but not the
secret.

## WebSocket transport

Replace the `http` block with `ws`. One block, not both: the extension uses
whichever is present. The server accepts the upgrade by default; set
`OPAMP_WS_ENABLED=false` to refuse it.

```yaml
extensions:
  opamp:
    server:
      ws:
        endpoint: ws://127.0.0.1:4320/v1/opamp
        headers:                          # same bearer token, same reason
          Authorization: "Bearer ${env:OPAMP_AGENT_TOKEN}"
```

`server.ws` takes `endpoint` and `headers` only — `polling_interval` is an HTTP
concept and is **rejected** as an unknown key (verified against `0.140.0`). A
socket is server-push, so there is nothing to poll; the agent still sends its own
heartbeats. Both transports carry the identical message handler, so the state in
`/agents` is the same either way (`tests/test_ws_parity.py` asserts it). Wire-level
limits and close codes are in [transports.md](transports.md).

## TLS

Terminate it in front of the server rather than in it: set
`TLS_TERMINATED_UPSTREAM=true` (this asserts the posture; see
[security.md](security.md#before-you-expose-this-server)), keep port 4320 bound to
loopback, and point agents at the proxy:

```yaml
        endpoint: https://opamp.example.com/v1/opamp    # ws → wss://
```

`docker/nginx.conf` and `docker/Caddyfile` are ready-made terminators; forward
`Upgrade`/`Connection` and a long read timeout so the WebSocket transport keeps
working, and leave `Origin` intact.

## Polling interval

For HTTP this *is* the heartbeat. The server learns each agent's cadence and scales
its staleness timeout to it, so a 60 s poller is fine; what it cannot tell apart is
"slow poller" and "died on startup" before the second report, so a very long
interval widens the window in which a restart looks like a new agent. 5–30 s is a
good range. The timeout rule itself is in [transports.md](transports.md#when-an-agent-counts-as-gone-69).

## Verifying it worked

`/agents` shows the agent, its description attributes and its health.
`GET /agent/{id}` shows the detail worth checking on day one:
`available_components` present in full (the server asks once per hash, #77),
`components` with the parts actually used by the pipeline, and `capabilities`
telling you which optional features the agent will accept. `GET /health` reports
`spec_version` and the server's own capability bits, so "will this agent
interoperate?" is answerable without reading source.

```bash
curl -s localhost:4320/agents | jq '.count, .agents[].id'
opampctl agents list --pretty            # same data with names and capability tags
```

## Naming an agent

`agent.name` is not an OpenTelemetry semantic convention — it is a label you declare,
and this server treats it as exactly that:

- **Shown, never addressed.** The Agents table, its detail view, the compliance picker
  and the remote-config scope show the name; `GET /agents` returns it as `name`, and
  the SQL selection layer exposes `name` / `agent_name` columns (#137). Every link,
  selector, revision and `opampctl` argument still takes the `id`: a name is not unique
  and can change when the agent restarts, so nothing that has to keep pointing at
  *this* agent is keyed on it.
- **No name, no invention.** An agent that declares none is shown by an abbreviated id,
  and its `name` is `null` in the API.
- **Duplicates are disambiguated, not rejected.** Two collectors called `collector`
  render as `collector #97c3cacf` and `collector #1a2b3c4d`. The server does not enforce
  uniqueness — that would be a fleet constraint the agent's own config cannot see.

## Which distros work

| Distro | Works? | Note |
|---|---|---|
| `otelcol-contrib` | ✅ | The e2e config in this repo, [verified in CI](../.github/workflows/collector-e2e.yml) |
| Custom/core build | ✅ | Only if it includes `extension/opampextension` |
| `otelcol` (core, upstream) | ❌ | No `opamp` extension in the core distro — use contrib or a build that has it |
| OpAMP Supervisor | ✅ | Different config shape; it can also consume connection settings (the collector extension cannot) |

A **slim distro built with OCB** is an optional optimisation, not a requirement:
`POST /agent/{id}/manifest` (and the UI) generates a manifest for exactly the
components an agent uses, if you want to ship a smaller binary. Any distro works
without it.

Only the **OpAMP Supervisor** can apply a pushed config — the collector's
`extension/opampextension` never advertises `AcceptsRemoteConfig`. See
[remote-config.md](remote-config.md).
