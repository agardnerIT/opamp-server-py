# Scoping: Falco support

**Status: scope only — no issue opened, nothing committed, nothing pushed upstream.**
This document reviews what is necessary to get Falco connected to this server over
OpAMP: what hooks exist, who builds what, and what a capture must still answer.
Session that produced this was planning-only; no dev stack was started.

Researched 2026-11-22 against `falcosecurity/falco` `master` (0.45.2, released
2026-10-29), `falcosecurity/falcoctl` `master`, and the upstream issue trackers —
every claim sourced from the repo/docs, not memory. Cross-checked against the
`osquery-opamp` repo (`github.com/agardnerIT/osquery-opamp`), whose pattern this
review borrows.

---

## 1. The finding that changes the scope

`agent-candidates.md` ranks Falco #2 with the verdict:

> Missing: config push (falco.yaml) + status. Client belongs in falcoctl (Go,
> `opamp-go`) — the osquery-opamp pattern: external client on base-product hooks.

**That premise is now half out of date.** Falco 0.45.0 (2026-09-21, PR #4005,
merged 2026-09-18) shipped an administrative reload-control API, motivated by the
Falco Operator:

- `POST /reload` — queues a reload; returns `202 Accepted` with a process
  identity (`instance_id`) and `started_generation` as a baseline. Available
  **only on the Unix socket** (`reload_control.enabled: true`, Incubating,
  Linux-only).
- `GET /reload` — reports readiness and `started`/`applied`/`rejected` reload
  generations. Available on both the Unix socket **and the TCP webserver**
  (`webserver.enabled`, port 8765 by default); a changed instance id means the
  process restarted and observation must be re-based.
- A rejection records "invalid input" and **preserves the active runtime** — i.e.
  the product already distinguishes "applied" from "rejected" and keeps the last
  good config running. This is exactly the apply-feedback OpAMP's
  `RemoteConfigStatus` exists for, and exactly what the osquery-opamp README
  describes building by hand over Thrift.
- Falco's own `watch_config_files: true` (Stable) auto-reloads on config/rules
  file changes — but that path is fire-and-forget: no feedback. The
  write → `POST /reload` → poll `GET /reload` protocol is the reliable one.

What is **still missing** is unchanged in kind: nobody speaks OpAMP. A search of
`falcosecurity/falco`, `falcosecurity/falcoctl` and `falcosecurity/falco-operator`
returns zero OpAMP mentions in code and issues (checked 2026-11-22). The client
is the build.

The second refinement: **the hooks are in Falco itself, not falcoctl.** The
reload-control API, `/healthz`, `/versions` (JSON component versions — the same
endpoint falcoctl's follower polls for compat checks) and a Prometheus `/metrics`
endpoint (Incubating, needs `metrics.enabled`) all live in the Falco process.
falcoctl contributes exactly one OpAMP-relevant capability: **artifact/package
management** (index → OCI pull → cosign verification → version compat check →
install to `rulesfilesdir`/`pluginsdir`, with a state file). An external sidecar
therefore does not need falcoctl at all for the config/status round trip;
falcoctl matters only if we want `PackageService` to drive rules/plugin
installation — which is the phase-2, upstream-facing piece (and precisely what
the OpAMP SIG's "add-on packages + Supervisor" item sharpens).

## 2. Hook inventory (the osquery-opamp table, redone for Falco)

| OpAMP need | Falco mechanism | Status |
| --- | --- | --- |
| deliver remote config | sidecar writes `falco.yaml` + rules files it owns on disk, then `POST /reload` (Unix socket) | ✅ 0.45.0, Incubating, Linux-only |
| apply feedback | `GET /reload`: `applied_generation` > baseline ⇒ APPLIED; newer `rejected_generation` ⇒ FAILED with reason | ✅ 0.45.0 |
| safe failure | reload rejection preserves the active runtime | ✅ 0.45.0 |
| fire-and-forget fallback | `watch_config_files: true` — Falco notices file writes itself | ✅ Stable, no feedback |
| report what is running | `GET /versions` (JSON, same output as `falco --version -o json_output=true`), `GET /healthz`, `GET /reload` readiness | ✅ Stable |
| package management | falcoctl follower: indexes → OCI → cosign → install dirs, per-artifact state | ✅ exists; not OpAMP-driven |
| metrics | Prometheus `/metrics` on the webserver (Incubating, needs `metrics.enabled`) | ⚠️ optional |
| effective config read-back | **none** — no endpoint returns the running config | ❌ sidecar reports what it wrote (source ownership), as osquery-opamp reports sources |
| restart command | **none** — no RPC restarts the daemon; the Falco Operator restarts pods | ❌ refuse `RESTART` like osquery-opamp does |
| withdrawal (empty `config_map`) | spec has no "delete"; `falco.yaml` is mandatory (rules, outputs) | ❌ sidecar must restore a last-known-good local config — a designed answer needed, not an empty file |
| pre-apply validation | `falco --validate` exists for rules files; reload itself validates and reports a rejection | ✅ client can pre-check like osquery-opamp pre-compiles queries |

Two consequences worth stating:

1. **This is simpler than osquery-opamp.** No Thrift, no extension
   registration/liveness, no push-vs-pull mode fork, no config-cache machinery:
   the config is on disk, so a Falco restart keeps the pushed config by
   construction (the property osquery-opamp only got in pull mode).
2. **Falco already has an external controller.** The Falco Operator drives
   `POST /reload` today. A sidecar is a second writer to the same files/socket;
   the two must not share ownership (see risks).

## 3. Work breakdown

### 3.1 Phase A — `falco-opamp` sidecar (the build)

A new standalone Go client, sibling repo to `osquery-opamp`, using the official
`opamp-go` client (the lowest-effort row of the client-library table — no
hand-rolled protocol, unlike the Python osquery extension which vendored
`opamp.proto`). Scope:

- Transports: HTTP poll + WebSocket push, chosen by endpoint scheme, with auth
  header/token — same UX contract as osquery-opamp (`--endpoint`,
  `--header 'Authorization: Bearer <token>'`).
- `AgentDescription`: composed by the sidecar from `GET /versions` + host info.
  `service.name: falco` (the spec's naming convention).
- Config ownership: the sidecar owns (a) a managed rules directory and (b) a
  designated config file. A push = write files → `POST /reload` → poll
  `GET /reload` until `ready=true` and `applied_generation > max(baseline,
  rejected_generation)` on the **same instance id** → report `APPLIED`; a newer
  rejection → `FAILED` with Falco's reason. Disconnect/timeout ⇒ unknown
  outcome (the API's own caveat) — surface honestly, retry observation.
- Pre-validation: run `falco --validate` on pushed rules when a falco binary is
  reachable, so a bad rules file is refused before the reload (mirrors
  osquery-opamp's pre-compile; `--no-validate` opt-out).
- `ComponentHealth`: readiness, reload generations, falco version, last error;
  refreshed on an interval (osquery-opamp's `--state-interval` shape).
- Effective config: report the config as written (path + hash), stated as "what
  we handed Falco", never claimed as read-back.
- Capability bitmask: honest — advertise remote config + health; **not**
  packages (`PackageService`), restart, connection settings, own metrics
  (phase 1).
- Withdrawal: empty `config_map` ⇒ restore the captured last-known-good local
  config and reload. Document it as the contract.

**Bootstrap contract** (the one deliberate friction point): Falco must be told
once to enable the webserver + `reload_control` and to read rules from the
managed dir. Like osquery's `--config_plugin=opamp --extensions_require=opamp`,
that is a one-time edit the operator makes (or the sidecar writes before first
start); after it, this server is the source of truth. The profile's `snippet`
documents exactly this, the way `osquery.yaml` documents both modes.

Not in phase 1: falcoctl integration, `PackageService`, metrics.

### 3.2 Phase B — server profile (cheap, after the capture)

One file `server/profiles/data/falco.yaml` per docs/agent-profiles.md, plus at
most one engine and one test — nothing else in `server/*.py`:

- `identity.match_any`: `service.name` prefixed `falco`; durable signal: an
  attribute key namespace the sidecar owns (`falco.`), matching the osquery
  profile's key-prefix pattern rather than enumerating values.
- `config_formats`: YAML, `default_filename: falco.yaml`. `distinctive: false` —
  YAML is a format, not a product; the profile's engine/identity is what makes
  it Falco's.
- `validates_structure` + a `falco` engine (only step needing code, optional in
  v1): best-effort shape checks — `rules_files`/`rules` present and non-empty,
  at least one output enabled, `watch_config_files` state surfaced as a warning
  when it would bypass the feedback protocol. Findings honest: rules semantics
  can only be judged by `falco --validate` on the host, which the server cannot
  run — the sidecar pre-check plus the approver are the last check (same
  reasoning as `osquery.yaml`).
- `components_engine`: rules files and enabled outputs from the config document,
  mirroring how `osquery` inventories schedule/packs.
- `reports: [fleet, agent]` — `heavy` needs unused-component data and `outdated`
  needs reported component versions; if the capture shows the sidecar
  advertising the falco version as an attribute, `outdated` can be revisited.
- `snippet`: the sidecar's own quickstart with `{endpoint}`/`{token}`
  substituted (#144 pattern), including the bootstrap edit.
- `telemetry`: omitted in phase 1 — the UI must say "does not report telemetry
  to the server" truthfully.
- Test: `tests/test_falco_profile.py` replaying the **captured** status payload —
  never written from imagination (`docs/new-agent-type.md`'s "almost right,
  quietly wrong" failure mode).

### 3.3 Phase C — upstream (optional, the candidates file's real play)

Once the sidecar works against this server:

1. Open a proposal issue on `falcosecurity/falcoctl`: an OpAMP client there
   (`falcoctl` is Go, `opamp-go` is available, the follower already polls
   Falco's `/versions` for compat) could map OpAMP `PackageService` status onto
   the follower's artifact machinery — packages + config push in one control
   plane. The sidecar is the working proof; falcoctl is the distribution
   vehicle. Gate: maintainer appetite, unknown — and the honest fallback is the
   standalone sidecar, which needs nothing from them.
2. `reload_control` is Incubating: engage early, the client we build is a
   consumer whose feedback may stabilise it (the PR's own motivation was the
   Operator — we are the second known consumer).
3. List this server in the OpAMP contrib space once that exists (SIG action
   already noted in `agent-candidates.md`).

## 4. Risks / open questions the capture must answer

**Live capture performed 2026-11-22** against `falcosecurity/falco:0.45.2`
(`engine.kind=nodriver`, webserver on, `reload_control` enabled; payloads in
`/tmp/falco-capture/payloads/`). Answers in the right-hand column:

| # | Question | Answer from the capture |
| --- | --- | --- |
| 1 | What does one reload re-read — full `falco.yaml` or rules only? | **The full config set.** Changing `time_format_iso8601` in `falco.yaml` plus a fixed rules file applied (gen 5→9); a broken rules file rejects the *whole* reload (gen 7 rejected). All files are validated together; one bad file rejects everything. |
| 2 | Rejection reason granularity in `GET /reload`'s JSON | **Generation numbers only** (`instance_id`, `started/applied/rejected`, `ready`). The human-readable reason is only in Falco's stderr logs (`Critical Falco internal: hot restart failure: <file>: Invalid`). The sidecar must read Falco's log stream for the reason text; `FAILED` status can carry a coarse reason only. |
| 3 | `reload_control` is Linux-only and Incubating | Unchanged (docs-level); container capture was Linux. |
| 4 | Socket permissions | Confirmed strict: a 0755 socket dir is refused at startup (`socket directory must be owned by Falco, without group write or other access`); must pre-exist, 0700/0750, owned by Falco's effective UID. A pre-created empty named volume works. |
| 5 | Two writers problem | Not exercised live; ownership discipline stands as designed. |
| 6 | `watch_config_files` racing the sidecar | **Real.** With the watcher on, generation numbers jumped 2 per change (watcher reload + POST reload both fire). The sidecar protocol should either run with `watch_config_files: false` (the PR states manual reloads work without it) or treat the baseline check as tolerant of extra generations. |
| — | (new) POST wire contract | The handler (`userspace/falco/reload_control.cpp`) 400s a POST **without a `Content-Length: 0` header** (httplib rejects the headerless empty body) and any body or query string. The client must send an explicitly zero-length body. |
| — | (new) Process identity | `instance_id` is stable across reloads and changes only on restart — a clean restart signal for the client, as the docs promise. |

## 5. Effort shape

Rough, for comparison against `osquery-opamp` (65 tests, ~58s):

- Sidecar (phase A): the smallest client built so far — all-HTTP, official Go
  client, no IPC layer, restart persistence free. Order of **1–2 focused
  weeks** plus the live-capture iteration.
- Profile (phase B): **~1 day** after the capture (file + optional engine +
  replay test), matching the fluentbit/osquery/alloy cost.
- Upstream (phase C): unbounded (their cadence), and not on the critical path.

## 6. Corrections owed to `agent-candidates.md`

1. Falco is no longer "missing status": reload status and apply feedback shipped
   in 0.45.0 (2026-09-21). The missing piece is the OpAMP client, full stop.
2. "Client belongs in falcoctl" is too strong as a *requirement*: the hooks live
   in Falco and are consumable by any sidecar; falcoctl is the phase-2 home for
   package management, not a prerequisite.
3. The verdict row should move from "watch" to the `osquery` class of candidate:
   base product has the mechanisms; the profile is cheap; the client is the
   whole game and is buildable today.

The recommended next step is the capture, not code: boot Falco 0.45.2 with
`reload_control.enabled: true` and the webserver on, observe a
write → `POST /reload` → `GET /reload` cycle and what a reload actually re-reads
(open questions 1, 2, 6), and only then write the profile and the sidecar
against recorded payloads.
