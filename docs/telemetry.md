# Internal telemetry (#122)

The telemetry view is **one profile's capability, not the definition of telemetry**. What
this server accepts and what it shows:

- **Ingestion is OTLP-only.** `POST /v1/metrics` speaks OTLP over HTTP (JSON or protobuf)
  and nothing else. It is deliberately **unauthenticated** — existing behaviour, and
  changing it is a separate decision with its own threat review — so treat it as a
  write-only sink, not as a trusted source.
- **Identity is one derivation, in one place.** The reporting agent is the resource
  attribute `service.instance.id`, falling back to `service.name`, with dashes stripped
  (`server.main.IDENTITY_ATTRIBUTES`, `_identity_from_resource`). Both encodings go through
  that function, so JSON and protobuf cannot drift. An unknown identity is accepted, logged
  at `DEBUG` and **dropped**: metrics attach to a *known* agent or to nothing.
- **A profile that reports nothing shows nothing.** Which panels appear, and whether a
  missing value is a fact ("this profile does not report telemetry to the server") or a gap
  ("no telemetry received yet"), is the profile dictionary's answer (`telemetry:` on
  `otelcol`; absent on `generic`). The UI renders the profile's own sentence rather than
  guessing, and `null` values are not drawn as zeros.

If a product needs push-based metrics over another protocol, that is a new sub-issue with
its own design — not a widening of this route.

Metric values are persisted per agent and survive a restart (`GET /agent/{id}` and
`GET /agent/{id}/metrics`). Queue depth and delivery counters for the server's own
behaviour are in [operations.md](operations.md#observability) and
[remote-config.md](remote-config.md).
