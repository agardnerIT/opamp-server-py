# Agent profiles: what weaver can check (#135)

**Generated — do not edit.** `python scripts/semconv_project.py` writes this file
and `docs/semconv/`; CI fails when the committed copy is stale.

This is a *projection*, not the dictionary. Weaver validates semantic conventions
(attribute and metric groups), so it is given exactly the parts of each profile
that are semantic conventions: identity evidence and telemetry names. Pinned to
schema `https://opentelemetry.io/schemas/1.30.0` and `semconv_version 1.30.0`.

## What this check proves, and what it does not

It proves the **identity and telemetry projection is well-formed**: attribute keys
are namespaced and snake_case, no two profiles claim the same attribute id, and the
metric groups are syntactically valid.

It proves **nothing** about the rest of a profile — `config_formats`, `structure`,
`components`, `panels`, `snippet`, `reports`. Those are document shapes and
behaviour, and the loader schema (`server/profiles/schema.py`, run with
`--strict-profiles`) is what validates them. A green `weaver registry check` that
is read as "the dictionary is valid" is the failure mode this paragraph exists to
prevent.

Namespace: `opamp.agent.*` — deliberately not a vendor's namespace and not
`service.*`. A profile match is a statement about this management plane; the
agent's own `service.name` stays an existing semantic convention that a rule
*reads*.

## Identity evidence

| Profile | Attribute | Match | Values |
|---|---|---|---|
| Fluent Bit | `opamp.agent.fluentbit.match.service_type` | prefix | `Fluent Bit`, `FluentBit`, `fluent-bit` |
| Fluent Bit | `opamp.agent.fluentbit.match.service_name` | prefix | `fluent-bit`, `fluentbit` |
| OpenTelemetry Collector | `opamp.agent.otelcol.match.service_name` | prefix | `io.opentelemetry.collector`, `opentelemetry-collector`, `otelcol` |

## Telemetry

| Profile | Metric id | Agent's own name |
|---|---|---|
| otelcol | `metric.opamp.agent.otelcol.otelcol` | `otelcol_*` |

## Running the check

```bash
python scripts/semconv_project.py           # regenerate
docker run --rm -v "$PWD/docs/semconv:/registry" \
  otel/weaver:0.28.0 registry check -r /registry
```

The weaver image is pinned by tag and digest in the CI workflow; it is a dev/CI
dependency only — not in the server image, not in `pyproject.toml`.
