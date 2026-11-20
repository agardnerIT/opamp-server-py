# Proposing a new agent type (#145)

This server recognises an agent through a **profile** — one YAML file in
`server/profiles/data/`. `otelcol`, `fluentbit` and `osquery` are files, not code paths;
`generic` is the built-in fallback. A new type can therefore be one file and a restart,
and the mechanics are in
[docs/agent-profiles.md](agent-profiles.md) ("Adding a profile"). This page is about the
other half: **what a proposer must bring to the issue** so that the profile someone
writes is true of the product, rather than plausible-sounding.

There is a GitHub issue template for this ("Add a new agent type"). Its fields are the
checklist below.

## What to bring, and why each field exists

| # | Field | Why it is asked | Where it lands |
|---|---|---|---|
| 1 | **Product name and what it is** | `name` becomes the value in `agents.agent_type` and the word operators type; `display_name`, `icon`, `summary` are every surface's chrome | `name`, `display_name`, `icon`, `summary` |
| 2 | **How it speaks OpAMP** | Which library/extension and version determines everything else: whether `AvailableComponents` is sent, which content types are declared, what capabilities are advertised | the identity evidence in `identity` |
| 3 | **A captured real OpAMP payload** | The single most valuable item. Real payloads are how `fluentbit` got it right (`service.type`, not `service.name`) and how `osquery` found its durable signal (the `osquery.` attribute namespace). A fixture invented from documentation hides exactly the bugs this project tests for | the profile's test fixture (`tests/test_<name>_profile.py`) |
| 4 | **Config format(s)** | The MIME type the agent reports config as, whether any type is *distinctive* to the product, and the download filename | `config_formats` |
| 5 | **Components / inventory** | Whether the product has a component concept at all. Yes → from `AvailableComponents` or from the config document (`components` / `components_engine`). No → the UI honestly says `unsupported`; an invented list would be a fake zero (#120's lesson) | `components` / `components_engine` |
| 6 | **Telemetry reported to the server** | Metric names/prefix, and the product's own metric namespace. A profile with no `telemetry` block gets "does not report telemetry" — the product fact, not a gap message | `telemetry`, `panels` |
| 7 | **Working config snippets** | The fragment an existing config gains, and the complete config for a new agent, written with `{endpoint}`, `{token}`, `{scheme}`, `{auth}`. A snippet that does not start the product is a load-time lie the schema cannot catch | `snippet`, `onboarding_snippet` |
| 8 | **Behaviour under a pushed config** | What the product does with a good push and a bad one (restart? stays down? keeps the old config?). This becomes Help-page prose shown to operators as fact | `guidance` |
| 9 | **Should structure be judged?** | Yes → a validation engine must be written and the profile must declare its inventory (a profile may only claim to interpret bodies if it brings the rules). No → opaque bytes; approving a push needs the explicit `confirm_unchecked_structure` acknowledgement | `validates_structure`, `structure`, engine file |
| 10 | **Priority and collisions** | Identity signals another product could also match, or a distinctive content type already claimed. Ties and duplicate claims are load errors, so this is thought through before the PR | `priority`, `distinctive` |

Item 3 deserves its own paragraph: **capture, do not write from memory.** Run the agent
against this server (or any OpAMP server that echoes status reports) and paste the
`AgentDescription`, capabilities and a post-push status. Redact secrets; structure is
what matters. Every shipped profile was written against real captures, and the reason is
that the second-most-dangerous failure mode here is a profile that is *almost* right —
it matches, it renders, and it is wrong about the product in ways only a real payload
shows.

## What the proposer is expected to do — and not do

- **Do** file the issue with the checklist filled in. Items 1–5 and 7 are the minimum for
  anyone to write the profile; item 3 is what makes it correct.
- **Do not** need to modify `server/*.py`. A plain profile is data only. The one
  exception is item 9: a structure engine is Python, registered in
  `server/profiles/<name>.py`, and a proposer who volunteers to write or review it should
  say so in the issue.
- **Do not** ask for new capability in the profile file. Profiles may only subtract: no
  field can grant a capability, a trust level or a validation exemption, and there is no
  `hooks` key — data may not import code. If the product seems to need a server-side
  feature it does not have, that is a separate issue, not a field in the profile.
- **Do** say if the product is built by the OpenTelemetry Collector Builder
  (`ocb_manifest: true`), so the manifest endpoint answers correctly instead of
  "you have no components in use", which is true and misleading at the same time.

## After the issue is filed

The rest is the three-step recipe in
[docs/agent-profiles.md](agent-profiles.md#adding-a-profile): copy the closest shipped
file, change what is true for the product, write one test that replays the captured
payload (from the issue) through `_handle_agent_message`, and restart. `GET /agent/types`
lists the new type immediately; an agent whose identity matches resolves to it, with
`agent_type_source` saying why.

## References

- [docs/agent-profiles.md](agent-profiles.md) — the dictionary, the file format, the guardrails.
- `server/profiles/schema.py` — the field reference a profile is validated against.
- The issue template: `.github/ISSUE_TEMPLATE/new-agent-type.yml`.
- The proofs this process produced: #123 (Fluent Bit), #141 (osquery).
