# Scoping: Grafana Alloy support

**Status: scope only — no issue opened, nothing committed, nothing pushed upstream.**
This document is the handoff for an implementation agent. It contains two
self-contained implementation briefs (**A** and **B**) that are *alternatives*, not
stages: pick one per the decision in [The decision](#the-decision) before writing any
code. Session that produced this was planning-only; no dev stack was started.

Researched 2026-11-21 against grafana/alloy `main` (v1.22.0, released 2026-11-13),
grafana/alloy-remote-config `main`, and the upstream issue tracker. Every claim is
sourced from the repo/docs, not from memory.

---

## 1. Shared ground (both options)

### 1.1 The finding that changes the scope

`agent-candidates.md` says Alloy is "OpAMP-based Fleet Management is already GA.
Profile work only." **That premise is wrong.** Alloy does not speak OpAMP for its own
configuration. It has two remote-config surfaces, and only one of them is OpAMP:

1. **Alloy proper (River config)** — the `remotecfg` config block
   (`docs/sources/reference/config-blocks/remotecfg.md` in grafana/alloy) polls a
   server implementing the **alloy-remote-config API**
   (github.com/grafana/alloy-remote-config): a ConnectRPC/protobuf service
   `collector.v1.CollectorService` with `RegisterCollector`, `GetConfig`,
   `UnregisterCollector`. This is what Grafana Cloud Fleet Management serves for
   Alloy fleets. Not OpAMP. The upstream ask for OpAMP (grafana/alloy#1236, open
   since 2024-07) is still open.
2. **Alloy's OTel Engine** — an OpenTelemetry Collector distribution embedded in the
   Alloy binary, run via `alloy otel`, which reads **standard otelcol YAML**
   (`docs/sources/set-up/otel_engine/`). Since v1.19.0 (Aug 2026, PR #6815) the
   `alloy otel-supervisor` subcommand runs it under the **stock upstream OpAMP
   supervisor** (`opentelemetry-collector-contrib/cmd/opampsupervisor`) — the exact
   supervisor this server's `otelcol` profile already documents and supports. In
   "manual mode" (`--config=<supervisor.yaml>`) it can point at any OpAMP server,
   including ours. The config it receives and applies over OpAMP is otelcol YAML;
   nothing River-shaped is managed on this path.

### 1.2 Comparison at a glance

| | Option A: OTel Engine via `otel-supervisor` | Option B: Alloy River via `remotecfg` |
|---|---|---|
| Protocol | OpAMP (stock upstream supervisor) | alloy-remote-config ConnectRPC |
| Config managed | otelcol YAML inside the engine | the full Alloy River config |
| Server-side work | one profile file + capture test | **new transport**: ConnectRPC endpoint pair in `server/`, synthesised identity into the profile dictionary |
| What the fleet gets | health, effective config, remote config push, restart (standard supervisor capabilities), `otelcol_*` internal metrics | poll-based config assignment, `hash`/`not_modified` revisioning, RemoteConfigStatus, reported effective config, attributes; **no health, no capabilities, no restart command, no component inventory**, ≥10s poll |
| Profile-machinery fit | native (supervisor sends a real AgentDescription) | foreign (no AgentDescription — the bridge must synthesise one) |
| Risk | low — reuses the otelcol engine, validators, telemetry prefix | high — new protocol code path, new identity model, the "almost right, quietly wrong" profile failure docs/new-agent-type.md warns about |
| Upstream posture | nothing to wait for; subcommand is labelled experimental | Alloy still would not speak OpAMP (#1236 open); we would implement Grafana's proprietary API alongside OpAMP |
| Effort shape | capture-driven, like `fluentbit` #123 / `osquery` #141 | feasibility-study-sized; own issue first |

### 1.3 Sources

- `remotecfg` doc + alloy-remote-config `api/collector/v1/collector.proto`: not OpAMP.
- grafana/alloy#1236 (open, 2024-07): community ask for OpAMP — unfulfilled.
- PR #6815 (merged 2026-08-13, first shipped v1.19.0): `alloy otel-supervisor` wraps
  the stock upstream supervisor; `agent.executable` is forced to the running Alloy
  binary, which runs the OTel Engine (`alloy otel`).
- `docs/sources/reference/cli/otel-supervisor.md`: stage **experimental**; "simple
  mode" is Grafana-Cloud-only (env vars); **manual mode is the generic path**.
- `docs/sources/configure/load-remote-configuration.md`: the two-pattern model
  (module imports vs `remotecfg`).
- v1.22.0 is current (2026-11-13). `otel-supervisor` exists since v1.19.0.

---

## 2. Option A — Alloy OTel Engine via the embedded OpAMP supervisor

**One profile file, one capture test, no `server/*.py` changes.** The implementation
agent should follow the capture-driven process every shipped profile took
(docs/new-agent-type.md): connect a real agent, capture what it reports, write the
profile from the capture, replay the capture in a test. Nothing below is "assumed
correct" — it is the starting hypothesis the capture will confirm or correct.

### 2.1 Local workbench

The dev stack is `./startDevStack.sh` (= `docker compose up --build` with
`COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`). Docker Desktop must be
running first (it was not at scope time).

1. **Token** — agent tokens are mandatory (#125/#126): mint one with
   `opampctl agent-tokens create --expires-in-days 30 --max-agents 5`.
2. **Agent container** — stock `grafana/alloy` image, v1.22.0, running
   `alloy otel-supervisor --config=/etc/alloy/supervisor.yaml`. Do **not** use the
   Grafana-Cloud "simple mode" env vars — manual mode is the generic path.
   Add the service to a new `docker-compose.alloy.yml` (explicitly named, following
   the repo's rule that the dev overlay is never an override file) and run it with
   `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml:docker-compose.alloy.yml`.
3. **Supervisor YAML** (mount at `/etc/alloy/supervisor.yaml`; this is also the
   draft material for the profile's `snippet`):

   ```yaml
   server:
     endpoint: ws://<stack-host>:4320/v1/opamp     # or http; the supervisor supports both
     headers:
       Authorization: "Bearer ${env:OPAMP_AGENT_TOKEN}"
   capabilities:
     reports_effective_config: true
     accepts_remote_config: true
     reports_remote_config: true
     reports_health: true
     accepts_restart_command: true
   agent:
     description:
       # THE identity decision (see 2.3): without this the agent presents as a
       # collector and the otelcol profile claims it.
       identifying_attributes:
         service.name: "alloy"
         service.version: "1.22.0"
     args: [otel, --storage.path, /var/lib/alloy/data]
   storage:
     directory: /var/lib/alloy/supervisor-data   # on a volume; identity persists here
   ```

   Notes on this YAML:
   - It is the upstream opampsupervisor spec config (link in the Alloy doc:
     `opentelemetry-collector-contrib/cmd/opampsupervisor/specification/README.md`),
     so everything is upstream-defined, nothing Alloy-proprietary.
   - `agent.executable` is ignored/forced to the Alloy binary (verified in
     `collector/otelsupervisor/supervisor.go`).
   - The supervisor is the OpAMP client; the engine subprocess is not. Expect the
     AgentDescription to come from the supervisor config, and **no
     `AvailableComponents`** (that is an `extension/opampextension` feature, which is
     not in play here) — see 2.4.
   - The exact capability bitmask the supervisor advertises is a capture item.

### 2.2 Capture checklist (answers the table in docs/new-agent-type.md)

Record from a real v1.22.0 run against the dev stack, before writing the profile:

- [ ] AgentDescription: exact `service.*` values sent (does the supervisor pass
      `identifying_attributes` through verbatim? does it add anything of its own?)
- [ ] Capabilities bitmask actually advertised.
- [ ] Effective config: object key, `content_type` (hypothesis: `text/yaml` or
      `application/x-yaml`; the body is otelcol YAML), body shape.
- [ ] Remote config: does a pushed config reach the engine and apply? What does the
      status read after a **bad** config (this drives `guidance` — the otelcol
      profile's "down until manual restart" paragraph is about a bare
      `opampextension`, not a supervisor, and may differ here).
- [ ] Health: what the supervisor reports (`agentHealth`? subprocess status?), and
      whether it is meaningful or just process-liveness.
- [ ] Telemetry: internal metrics under `POST /v1/metrics`. The engine is a
      collector distro (OCB-built), so `otelcol_*` names are likely but the capture
      decides — including whether the engine extension's own components
      (`otelcol.engine.*`?) appear.
- [ ] Component inventory: confirm absence/presence of `AvailableComponents`, and
      what the health map's scopes look like instead.
- [ ] Version signal: `code.namespace` metadata, `service.version`, or neither.
- [ ] Restart command: accepted and executed (supervisor restarts the engine)?

### 2.3 Identity (the one real design decision)

The `otelcol` profile matches `service.name` prefix `otelcol` /
`io.opentelemetry.collector` / `opentelemetry-collector`. If the supervisor is left
at its default presentation, this server will classify a running Alloy as an
OpenTelemetry Collector — the "it matches, it renders, and it is quietly wrong about
the product" failure docs/new-agent-type.md explicitly calls out.

**Fix (pre-approved in the scoping conversation): set `service.name: "alloy"` in the
supervisor config's `agent.description.identifying_attributes`** (template in 2.1)
and let the new profile match it:

```yaml
identity:
  match_any:
    - attr: service.name
      prefix: [alloy, grafana.alloy]
```

The capture may reveal a stronger/simpler signal (e.g. a dedicated attribute key the
supervisor or engine sets) — prefer that over enumerating values if it exists,
following the `osquery` profile's `attr_prefix: "osquery."` precedent.

### 2.4 Draft profile skeleton (fill from the capture, do not ship this as-is)

File: `server/profiles/data/alloy.yaml`. Schema is `server/profiles/schema.py`
(pydantic, `extra="forbid"` — every key below must be understood by the schema; a
typo is a load error, not a silent ignore). Guardrails that bite:

- At most one profile may set `bootstrap_default: true` — `otelcol` has it; **do not
  set it here**.
- `validates_structure: true` requires a `components` block **and** a
  `structure.engine` naming a registered engine (schema rule 2; loader enforces).
  Engines live in `server/profiles/*.py`; a profile file may **name** an engine but
  never import one ("no code from data").
- `reports` values are a closed set: `fleet, agent, heavy, outdated`
  (`schema.py:REPORTS`). `heavy` counts *unused* components — with no component
  inventory it cannot work, so it must not be listed (the `fluentbit`/`osquery`
  precedent: skip by declaration).
- Registered engines today: `structure`: `otelcol`, `fluentbit`, `osquery`;
  `components_engine`: `fluentbit`, `osquery` only. **There is no
  `components_engine: otelcol`** — if the capture confirms no `AvailableComponents`,
  the component inventory comes from either (a) the `components` spec reading the
  health map, or (b) nothing (no inventory shown), or (c) a new components engine —
  which would be the only `server/profiles/otelcol.py`-adjacent code change in this
  option, and needs its own agreement per docs/new-agent-type.md. Decide from the
  capture; default to (b), honestly empty, over an invented extractor.

```yaml
name: alloy
display_name: Grafana Alloy (OTel Engine)
icon: "◆"                       # capture may suggest better
priority: 95                    # below otelcol (100); ties are a load error
summary: >-
  <from capture: what the server can and cannot claim about this agent>

config_formats:
  - content_types: ["", "text/yaml", "application/yaml", "application/x-yaml", "text/plain"]  # capture decides
    default_filename: otelcol-config.yaml    # the engine reads collector YAML
    distinctive: false                       # format is not identity

validates_structure: true
structure:
  engine: otelcol                # the body genuinely is a collector config
  address_sections: [receivers, processors, exporters, extensions, connectors, pipelines]

identity:
  match_any:
    - attr: service.name
      prefix: [alloy, grafana.alloy]

reports: [fleet, agent]          # no heavy/outdated without a component inventory

telemetry:                       # only if the capture shows otelcol_* OTLP push
  metric_prefix: "otelcol_"
  title: OTel Engine Telemetry
  caption: <capture>

components:                      # required by the guardrail while validates_structure is true;
  sections: {}                   # fill from capture (health-map scopes, known ids)
  known: {}

snippet: |                       # SUPERVISOR yaml, not an extensions.opamp block —
  <the 2.1 yaml, parameterised>  # mirror the fluentbit precedent: the OpAMP settings
                                 # live in the supervisor's config, not in a file we push

guidance: |
  <must say, truthfully: this profile manages the OTel Engine inside Alloy
  (alloy otel-supervisor), not Alloy's River pipelines; what a bad config does
  on this path; whether restart works; that River/remotecfg is Option B>
```

Snippet placeholder rules (schema `SNIPPET_FIELDS`): `{endpoint}`, `{token}`,
`{scheme}` in `snippet`; `onboarding_snippet` additionally gets `{auth}` and
`{agent_name}` (engine-supplied). A complete runnable document in
`onboarding_snippet` is what the empty-fleet view shows (#121 precedent).

### 2.5 Test

`tests/test_alloy_profile.py`, patterned on `tests/test_fluentbit_profile.py` /
`tests/test_osquery_profile.py`: the captured `AgentDescription` verbatim, captured
capabilities, captured config body (otelcol YAML) + content type, round trip through
`server.main._handle_agent_message`, asserting identity resolves to the `alloy`
profile, structure validation applies, and the reports/telemetry claims match the
profile. Also assert the negative: the same description with `service.name`
*unchanged from default* resolves to `otelcol` — that is the bug 2.3 prevents.

### 2.6 Four-layer sync

Profile-only changes do not touch `client/`, `cli/`, or `mcp_server/` (they read the
dictionary dynamically) — but verify: `tests/test_profile_dictionary.py`,
`tests/test_profiles.py` (incl. the #117h byte-compare golden test), and a restart of
the UI to see the new type. If (and only if) the capture forces a new components
engine or engine change, the `AGENTS.md` four-layer rule still does not apply
(profiles are data + engine modules, not API endpoints), but `server/profiles/`
tests must.

### 2.7 Scope honesty (must appear in the PR/commit/issue text)

Option A gives the fleet OpAMP control of **the OTel Engine inside Alloy** — otelcol
YAML pipelines — not of Alloy's River configuration. It is a real, working path
(upstream designed it for exactly this), but it is not "managing Alloy" in the River
sense. The profile's `guidance` and the eventual issue text must say this plainly.

---

## 3. Option B — Alloy River support via the alloy-remote-config API

**A new server-side transport, not a profile.** Alloy's `remotecfg` block speaks the
`collector.v1.CollectorService` ConnectRPC service (protobuf over HTTP POST at
`<url>/collector.v1.CollectorService/<Method>`). This option implements that service
alongside OpAMP and feeds the existing profile dictionary through a synthesised
AgentDescription. **This is feasibility-study-sized; do the study before building.**
The brief below is the shape of the work, found by reading `collector.proto` and the
server.

### 3.1 Protocol surface (from `collector.proto`)

Three RPCs, all unary HTTP POST (Connect protocol):

| RPC | Request | Response | Server-side meaning |
|---|---|---|---|
| `RegisterCollector` | `id`, `name`, `local_attributes` | empty | agent first contact: create/refresh registration |
| `GetConfig` | `id`, `local_attributes`, `hash`, `remote_config_status{status, error_message}`, `effective_config{config_map}` | `content`, `hash`, `not_modified` | the whole loop: config assignment, drift reporting, status, liveness |
| `UnregisterCollector` | `id` | empty | explicit agent goodbye |

The Alloy client (`remotecfg` block) polls `GetConfig` every `poll_frequency`
(minimum **10s**), sends bearer/basic auth from the block's auth settings, and
applies `content` (a full River config document) when `hash` differs.

### 3.2 Mapping onto the existing domain

| alloy-remote-config | this server |
|---|---|
| `id` | agent identity. OpAMP uses `instance_uid` (16 bytes) → `agent_id`; the `remotecfg` `id` is an operator-set string (often `constants.hostname`) or a random UUID persisted in `alloy_seed.json`. Decide: accept any non-empty string as the agent id (column is TEXT, so it fits), and derive a synthetic `instance_uid` (e.g. SHA-256 of the string, truncated) so `AgentState.to_dict()` and everything downstream works unchanged. |
| `local_attributes` + `name` | a synthesised AgentDescription: identifying `service.name = "alloy"`, `service.version` from the reserved automatic attribute `collector.version` (the `remotecfg` block always sends it; keys with the `collector.` prefix are server-set), non-identifying from `local_attributes`. The profile dictionary then resolves identity the normal way. |
| `hash` / `not_modified` | the config-revision mechanism. The server already tracks `last_remote_config_hash` per agent (#81). The ConnectRPC variant: server stores a hash of the served content; on `GetConfig` with a matching `hash`, respond `not_modified: true`. Note the semantic difference from OpAMP's spec-hash reconciliation — here the *server* computes the hash of what it served; there is no spec-mandated algorithm. |
| `remote_config_status` (UNSET/APPLIED/APPLYING/FAILED + error_message) | map 1:1 onto `remote_config_status` / `remote_config_error` columns (already exist). FAILED + error_message is the Alloy-side equivalent of the OpAMP `RemoteConfigStatus.error_message` the server already models. |
| `effective_config.config_map` | `effective_config` column (string) — drift detection works. `config_map` is a map of named files; the engine extension reports the River document under some key (capture it: likely `""` or a path). |
| Bearer/basic auth | the existing agent-token model (#125/#126). `remotecfg` supports `bearer_token`, `bearer_token_file`, `basic_auth`, `authorization` — the profile's `snippet` shows the `bearer_token` form. Token-less connections must be refused, same as OpAMP. |
| `poll_frequency` ≥ 10s | liveness/heartbeat. There is no push, no health report: `healthy` is at best "polling recently". The EWMA `report_interval` machinery (#69) can learn the cadence the same way it does for OpAMP HTTP polling. |
| — | **Absent vs OpAMP**: capabilities negotiation, health, package statuses, restart commands, AvailableComponents, custom messages/commands, heartbeats. The server must not invent any of them. UI surfaces that assume capabilities must degrade honestly (the "profile decides what the server is willing to *show*" rule already covers this if the profile declares honestly). |

### 3.3 Server touchpoints (surveyed, not written)

- **Transport**: new FastAPI routes in a new module (e.g. `server/remote_config_api.py`)
  wired in `server/main.py`, next to `@app.post("/v1/opamp")` and
  `@app.websocket("/v1/opamp")`. Content type is `application/proto` (Connect
  unary). The protobuf types are small and stable — vendor
  `collector.proto` under `proto/` and generate Python bindings the same way the
  OpAMP protos are generated today (check `proto/` + build tooling for the pattern).
  Alternative considered: Connect JSON (`application/json` bodies) to avoid codegen —
  rejected as a guess; Alloy's client is a Go ConnectRPC client and the byte-level
  dialect must match what it sends. **A capture of real Alloy↔Grafana-FM or
  Alloy↔test-server traffic is step zero of any implementation.**
- **Identity bridge**: a function that turns (id, name, local_attributes) into an
  AgentDescription-shaped dict and inserts into `AGENT_REGISTRY` via the same code
  path `_handle_agent_message` uses, so every downstream view keeps working without
  special cases.
- **State**: `agents` table needs a transport discriminator (e.g. `transport TEXT`)
  or the profile/type does the discrimination — decide during the study;
  `remote_config_status`, `last_remote_config_hash`, `effective_config`, `description`
  all already exist and are reusable.
- **Push flow**: `config_control` / `rollout` currently address agents over OpAMP
  (`ServerToAgent`). For an Alloy-River agent there is no push channel — the config
  is *assigned* (stored against the agent) and the agent fetches it on its next poll.
  The existing "staged → approved → applied" rollout gating can survive, but the
  "APPLIED" signal arrives as the next `GetConfig`'s `remote_config_status`, and the
  health gate (rollouts pause on unhealthy) has nothing to read — rollout gating for
  this transport is a design decision, not a free ride.
- **Profile**: a `alloy.yaml` (River-shaped) profile with `config_formats`
  (`distinctive: false`), `structure.engine: alloy` — a **new** engine module
  (`server/profiles/alloy.py`) with real River validation (River is HCL-like, not
  YAML — PyYAML is the wrong tool and an honest engine is a project of its own; a
  "structure-checked = false, approval needs acknowledgement" version is the honest
  v1, mirroring how `generic` works), and a `snippet` that is the `remotecfg` block +
  bearer token.
- **Docs**: `docs/transports.md` (wire behaviour), `docs/api-reference.md`,
  `docs/connect-collectors.md` or a new `connect-alloy.md`, `docs/security.md`
  (token model over a third protocol).

### 3.4 Work breakdown (study first, then)

1. **Feasibility study / spike**: stand up a tiny ConnectRPC server, point a stock
   `grafana/alloy` at it, capture the exact wire traffic (headers, content types,
   RegisterCollector-before-GetConfig ordering, retry behaviour on 4xx/5xx, what
   `name`/attributes actually arrive). Everything above is hypothesis until this
   exists.
2. **Proto vendoring + codegen** for `collector.v1`.
3. **Transport routes** + identity bridge + token auth.
4. **Config assignment/poll serving** + status/effective-config ingestion.
5. **Profile + engine** (honest v1: no River structural validation, approval
   requires acknowledgement).
6. **UI degradation check** (reports list, panels, health display for a
   no-health-capabilities agent).
7. **Tests**: wire-level replay of the spike capture; profile dictionary test;
   degradation tests.
8. **Docs** listed in 3.3.

### 3.5 Strategic alternative (should be part of the B decision)

Implementing Grafana's proprietary API is adapting to the product as-is. The other
play is the one `agent-candidates.md` itself prescribes for shortlist agents:
lobby on grafana/alloy#1236 for native OpAMP adoption and revisit. If B is chosen,
note in the issue text that a future OpAMP-speaking Alloy would ideally *replace*
the ConnectRPC transport rather than accumulate beside it.

---

## 4. The decision

Already put to the operator in the scoping conversation; repeat here for the
implementation agent:

1. **Which option?** A (profile-only, capture-driven, start today) vs B (new
   transport, feasibility study first) vs A-then-B. **Scoping recommendation: A
   now; B only after its own study (3.4 step 1) — and only if managing River
   pipelines is the actual goal.**
2. **Identity** (A): set `service.name: "alloy"` in the supervisor config —
   pre-approved; the capture may still override the match rule.
3. **For B**: is implementing Grafana's proprietary API acceptable, or lobby #1236?

## 5. Correction owed to `agent-candidates.md`

The "Grafana Alloy — OpAMP-based Fleet Management is already GA. Profile work
only." line conflates Grafana Cloud Fleet Management (which speaks OpAMP for the
OTel Engine *and* the proprietary alloy-remote-config API for River) with Alloy
speaking OpAMP. Alloy itself still does not speak OpAMP (#1236). After the option
is chosen, update that bullet to say: "OTel Engine path is OpAMP-capable today
(`alloy otel-supervisor`, stock upstream supervisor); River path is not —
proprietary `remotecfg` API (#1236 open)." Keep the candidates-file convention
(one bullet, no scope creep).
