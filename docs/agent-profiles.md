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

## The dictionary on disk

```text
server/profiles/
  data/otelcol.yaml   the dictionary — the only place collector knowledge lives
  data/*.yaml         one file per agent type; drop one in and restart
  schema.py           what a file may say (strict: an unknown key is a load error)
  loader.py           read, validate, compile; priority/duplicate rules
  engines.py          the snippets, sniffers and component readers built from a file
  base.py             the compiled, frozen `AgentProfile` the rest of the server reads
  generic.py          the fallback — built in, never a file
  registry.py         match order, provenance, resolution cache, reload
```

`server/profiles/data/otelcol.yaml` is the worked example and is worth reading first. The
shape in one screen:

```yaml
name: otelcol                  # stored in agents.agent_type; lower case, boring
display_name: OpenTelemetry Collector
icon: "🧩"                     # the badge glyph (#121 renders it)
priority: 100                  # match order, high wins; a tie is a load error
summary: >-                    # what GET /agent/types says about it
  ...
config_formats:
  - content_types: ["", "text/yaml", ...]
    default_filename: otelcol-config.yaml
    distinctive: false         # only a distinctive MIME type may *identify* a profile
validates_structure: true      # requires `components` — see the guardrails
identity:
  match_any:                   # evidence from AgentDescription
    - {attr: service.name, prefix: [otelcol, io.opentelemetry.collector]}
  sniff:                       # evidence from the payloads
    component_sections_any: [receivers, exporters]
    config_sections_any: [receivers, service]
reports: [fleet, agent, heavy, outdated]
telemetry:                     # omit entirely = "this agent reports no telemetry here"
  metric_prefix: "otelcol_"
  title: Collector Telemetry   # the words the detail page uses, because "collector"
  caption: Real-time metrics…  # is accurate for this product and a lie about others
components: {sections: {receivers: receiver, ...}}   # how to read the inventory
snippet: |                     # {endpoint}, {token}, {scheme}, {auth} are substituted
  extensions:
    opamp:
      server:
        {scheme}:
          endpoint: {endpoint}
# A *whole* config for a new agent (#121), as opposed to the fragment above. Optional:
# a profile without one shows its `snippet`, rather than an invented file.
onboarding_snippet: |
  extensions:
    opamp:
      server:
        {scheme}:
          endpoint: {endpoint}
{auth}
guidance: |                    # shown on the Help page under this profile only
  What this product does with a pushed config, and what it does with a bad one.
panels:                        # the agent-detail blocks this profile declares
  - id: data-flow-inputs
    widget: metrics            # WIDGETS is closed: a widget with no renderer is a lie
    metrics: [{label: Logs In, metric: otelcol_receiver_accepted_log_records}]
bootstrap_default: false       # at most one profile; the snippet shown before an agent connects
```

**The UI is rendered from this file (#121).** The agent-detail page has no product
vocabulary of its own: the icon in the badge, the telemetry heading, the panels and
their metric labels, the download filename, the Help page's per-profile section and
the reports a fleet is offered are all fields above. `git grep -i collector ui/`
returns nothing but strings that came from a profile.

**Adding an agent type** can be one file and a restart. No Python, no release:

```bash
cat > server/profiles/data/fluentbit.yaml <<'YAML'
name: fluentbit
display_name: Fluent Bit
icon: "🪵"
priority: 50
identity:
  match_any: [{attr: service.name, prefix: [fluent-bit, fluentbit]}]
config_formats: [{content_types: [text/yaml], default_filename: fluent-bit.conf}]
YAML
```

`GET /agent/types` lists it immediately after the restart, and an agent whose
`service.name` starts with `fluent-bit` resolves to it (`GET /agents` →
`agent_type` + `agent_type_source: declared`).

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

### Guardrails (enforced by the loader, not by review)

- **A profile may only subtract.** A file can never grant a capability, a validation
  exemption or a trust level — it decides what the server is willing to *show*, not what
  it may *do* (`server/profiles/base.py`, rule 1).
- **No code from data.** There is no `hooks` key: every behaviour is a field the schema
  understands, compiled by `engines.py`. An editable dictionary that could import a
  module would be remote code execution with a YAML front end.
- **`validates_structure: true` requires `components` *and* a `structure` engine.** A
  profile may only claim to interpret bodies if it brings the rules: `structure: {engine:
  otelcol}` names a closed set the loader knows (a file may name an engine, never import
  one — #119), and an unknown engine is a load error. The reverse is refused too: an
  engine no field points at is a typo, not a feature.
- **Match order is data.** `priority`, high to low — never filename order, because a
  locale, a rename or a parallel checkout would change fleet behaviour silently. Two
  profiles sharing a priority, or two claiming the same *distinctive* content type, is a
  load error.
- **A bad file must not take the server down.** An invalid document is logged, that
  profile is absent, the fleet is still served. `--strict-profiles`
  (`OPAMP_PROFILES_STRICT=true`) makes the same validation fatal — that is the form CI
  and the test suite use, because log-and-carry-on is exactly how a typo ships.
- **Ambiguity is visible.** When more than one profile's evidence matches, `priority`
  still picks a winner, but `agent_type_ambiguous` on the agent dict names the others.
  A tie is a decision nobody can audit, so it is reported rather than hidden.
- **A reload invalidates cached resolutions.** `registry.reload()` bumps a generation
  counter that is part of every resolution fingerprint, so nothing keeps serving a
  decision made against the old dictionary.

`generic` is not in the dictionary. It is the answer for "nothing matched", so it cannot
be the thing that failed to load; it declares no formats, no matchers and no sniffer, so
it can never win a match either.

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
