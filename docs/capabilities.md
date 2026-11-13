# OpAMP specification and capability matrix

This server targets **OpAMP [v0.20.0](https://github.com/open-telemetry/opamp-spec/releases/tag/v0.20.0)**
— the generated stubs in `proto/` are built from that release (#66). `GET /health`
returns `spec_version` and the exact server `capabilities` bitmask, so `opampctl`, the
MCP server and the UI never hardcode either.

Transports: **HTTP** (poll/reply) and **WebSocket** (`OPAMP_WS_ENABLED`, upgrade on
`/v1/opamp`, #89, on by default) — same framing, same message handler, same auth.
See [transports.md](transports.md).

| Direction | Capability | Status |
|---|---|---|
| Server | `AcceptsStatus` | ✅ always advertised |
| Server | `AcceptsEffectiveConfig` | ✅ always advertised (stored, redacted on read) |
| Server | `AcceptsPackagesStatus` | ✅ always advertised (stored/reported only) |
| Server | `OffersRemoteConfig` | ✅ advertised while the posture allows push (#81, #96) |
| Server | `OffersPackages` | ❌ [#82](https://github.com/agardnerIT/opamp-server-py/issues/82) closed as not planned — no stock agent consumes it and shipping binaries is deploy tooling's job (see [spec conformance](spec-conformance.md)). We ship the OCB manifest instead |
| Server | `OffersConnectionSettings` | ❌ [#83](https://github.com/agardnerIT/opamp-server-py/issues/83) |
| Server | `AcceptsConnectionSettingsRequest` | ❌ [#83](https://github.com/agardnerIT/opamp-server-py/issues/83) |
| Agent | `ReportsStatus`, `ReportsHealth`, `ReportsEffectiveConfig` | ✅ understood (health is optional; its absence is not unhealth) |
| Agent | `ReportsAvailableComponents` | ✅ understood (drives validation warnings) |
| Agent | `ReportsPackageStatuses` | ⚠️ stored and displayed, not acted on |
| Agent | `AcceptsRemoteConfig` | ✅ required before a config is offered (`remote_config.decision_for`) |
| Agent | `AcceptsRestartCommand` | ✅ [#84](https://github.com/agardnerIT/opamp-server-py/issues/84) — `POST /agent/{id}/restart`; 409 without the bit |
| Agent | custom capabilities / messages | ✅ [#85](https://github.com/agardnerIT/opamp-server-py/issues/85) — list stored and shown; `POST /agent/{id}/custom-message` delivers one, exactly once |

The classification lives in `server/remote_config.py`
(`ALWAYS_ADVERTISED` / `CONDITIONAL` / `UNIMPLEMENTED` / `RESERVED`), and
`tests/test_capabilities.py` fails when a proto bump adds a capability none of them
mentions: silently dropping a bit is a symptom, an unclassified bit is what
over-claims to every agent. `GET /health` publishes the same values, so "will my agent
interoperate?" is answered without reading the source (#79).

The same question, clause by clause — including what is **deliberately** different from
the spec and why — is [`spec-conformance.md`](spec-conformance.md), next
to the machine-checked spec index in `tests/conformance/` (#80). That pair is what a
spec bump should be reviewed against.

Both transports accept credential headers, which is what makes per-agent
authentication possible without a wire extension: `extension/opampextension` supports
`server.http.headers` **and** `server.ws.headers` (verified against v0.140.0 — the
values are `configopaque`, so the agent's own effective-config report redacts them),
and `opamp-go` sends them on every HTTP request and on the WebSocket upgrade. The
supervisor supports the same `headers` block. Token model: [agent-tokens.md](agent-tokens.md).
