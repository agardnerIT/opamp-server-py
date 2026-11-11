# Agent profiles: the dictionary (#117)

This server is not a collector dashboard with a generic mode bolted on. It manages
**OpAMP agents**, and what it can say about one depends on what that agent *is*. That
knowledge lives in one place: a directory of YAML files, one per agent type, in
`server/profiles/data/`. Nothing in `server/*.py` names a product.

This page is for someone adding a profile — including an LLM agent doing it — and for
anyone wondering why a specific panel or report is absent for their agent.

## The model in one table

| Question | Where the answer lives | What "no answer" means |
|---|---|---|
| What is this agent? | `identity.match_any` (+ `sniff`) in the profile file | It resolves to `generic`, and every surface says so rather than guessing |
| What config formats does it speak? | `config_formats` (content types + `default_filename`) | Accepted as opaque bytes, with the profile's declaration shown beside the editor |
| May the server judge its config's structure? | `validates_structure: true` + `structure: {engine: …}` | No structural checks run; the verdict says so and an approver must acknowledge it (#119) |
| What components does it have? | `components:` (from the agent's report) **or** `components_engine:` (from its config) | `components_status: unsupported` — not "0" (#120) |
| Which reports apply? | `reports: [fleet, agent, heavy, outdated]` | The report prints "Not applicable to N agent(s) on profiles: …" |
| Which telemetry panels? | `telemetry:` (metric prefix/map + panel titles) | "This profile does not report telemetry to the server" |
| What should the UI call collector concepts? | `guidance`, `panels`, `onboarding_snippet`, `config_formats.default_filename` | Generic chrome, no product vocabulary |
| How is it built? | `ocb_manifest: true` for Collector Builder products | `POST /agent/{id}/manifest` answers 409 naming the profile |

Resolution precedence is `pinned > declared (content type, then description) > sniffed >
fallback`; the winner and *why* are always reported (`agent_type_source`,
`agent_type_ambiguous`, `agent_type_warning`). An operator can pin with
`opampctl agents set-type <id> <profile>` — a pin outranks every signal and is audited.

## The shipped profiles

| Profile | Identity | Config | Structure rules | Components | Reports |
|---|---|---|---|---|---|
| `otelcol` | `service.name` prefixed `otelcol` / `io.opentelemetry.collector`; component sections | YAML/JSON | `server/profiles/otelcol.py` (pipelines, invariants, inventory) | from `AvailableComponents` + config | fleet, agent, heavy, outdated |
| `fluentbit` | `service.type` prefixed `Fluent Bit`; `[SERVICE]` in the config | INI (`text/plain`) and YAML | `server/profiles/fluentbit.py` (best effort: `[SERVICE]`, ≥1 input/output, `Name` per plugin) | from the config document `opamp-core` reports | fleet, agent |
| `generic` | nothing matches | anything, opaque | none | none (`unsupported`) | none |

`fluentbit` was written against real captured payloads (`opamp-observe/opamp-core` 0.6.0
supervising Fluent Bit 5.1.5, see #123): identity comes from `service.type` because
`service.name` is just `"Fluentbit"`, and the inventory comes from the INI document because
the supervisor sends no `AvailableComponents` at all.

## Adding a profile

Three steps, and only the second can need code.

### 1. The file

Copy `server/profiles/data/fluentbit.yaml` and change what is true for the product. The
schema is strict (`server/profiles/schema.py`): an unknown key is a load error, not a
silent no-op, and the docstring there is the field reference.

Two rules matter more than the rest:

- **Data may not import code.** There is no `hooks` key. `structure.engine` and
  `components_engine` name entries in a *closed* registry
  (`server/profiles/engines.py`); a file may name an engine, never a module path.
- **A profile may only subtract.** Nothing in a file can grant a capability, a trust
  level or a validation exemption. `validates_structure` is the one field that could make
  the server *less* safe, so it requires both a `components`/`components_engine` block and
  a `structure` block naming a known engine.

### 2. An engine, only if the product needs one

If the product's config is not shaped like anything the server already reads, add an engine
next to `server/profiles/fluentbit.py`:

```python
def validate(doc, path, ctx, verdict) -> bool:
    """Judge one document. Return True if you judged it, False to decline.

    `doc` is the parsed YAML/JSON document, or None when the body did not parse — the
    raw text is on `ctx.body`, which is how a non-YAML dialect gets judged at all.
    Report through `verdict.add_error/add_warning` with the config object's key as `path`.
    """

def components(agent) -> dict:
    """`{type: [{id, version, used}]}` from the agent's own report/config."""
```

Then register both at import time (see the bottom of `fluentbit.py`). Keep the findings
**honest**: a rule you cannot verify from the body (the daemon's runtime behaviour, a
plugin's options) is a warning at most, and the approver acknowledgement from #119 covers
what nothing can check.

### 3. One test, then a restart

`tests/test_fluentbit_profile.py` is the model: replay a **captured real payload** through
`_handle_agent_message`, then assert identity, inventory, a validation verdict and a push
round trip. A fixture invented from documentation hides exactly the bugs this tests.

Nothing else changes: `GET /agent/types`, the UI chrome, the reports and the editor all read
the dictionary. Restart the server (`--strict-profiles` in CI) and the new type is live.

## Why a profile can make a report or a panel disappear

`unsupported` is not zero, and "not applicable" is not a measurement. This is the same
lesson as #68 (an empty component list that meant "we read the wrong key"), applied one
layer up:

- `components_status`: `unsupported` (no component concept) / `unreported` (it has one, the
  agent has not told us) / `reported`;
- reports: an agent whose profile does not declare a report is counted in
  `not_applicable`, never as "0 heavy collectors";
- telemetry: a profile with no `telemetry` block gets the product fact ("this profile does
  not report telemetry to the server"), not the gap message;
- validation: a profile with no structure engine produces `structure_checked: false`, and
  approving such a revision needs `confirm_unchecked_structure` — recorded on the revision
  and in the audit log.

## What weaver checks, and what it does not (#135)

`docs/semconv/` is a **projection** of this dictionary into a semantic-convention registry:
one attribute group per profile for `identity.match_any`, one metric group per telemetry
name. `python scripts/semconv_project.py` generates it together with
[`agent-profiles-projection.md`](agent-profiles-projection.md), and CI runs a pinned
`weaver registry check` over it plus a staleness check on the generated files.

The limits are the important part:

- **Proven by the weaver check:** the identity and telemetry projection is well-formed —
  attribute ids are namespaced and snake_case, no two profiles claim the same id, metric
  groups are valid.
- **Not proven by it:** everything else in a profile. `config_formats`, `structure`,
  `components`, `panels`, `snippet`, `reports` are document shapes and behaviour, and
  `server/profiles/schema.py` (with `--strict-profiles`) is what validates them.

A green weaver run read as "the dictionary is valid" is the overclaim #135 exists to
prevent.

## References

- `server/profiles/schema.py` — the file format, field by field.
- `server/profiles/base.py` — the compiled `AgentProfile` and the "may only subtract" rule.
- `server/profiles/engines.py` — the closed engine registries.
- `server/config_validation.py` — the format layer every profile shares.
- #117 (the epic), #118/#134 (the dictionary), #119 (profile-owned validation), #120
  (scoped reports), #123 (the Fluent Bit proof).
