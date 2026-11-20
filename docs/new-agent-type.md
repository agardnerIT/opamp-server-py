# Proposing a new agent type (#145)

This server recognises an agent through a **profile** — one YAML file in
`server/profiles/data/`. `otelcol`, `fluentbit` and `osquery` are files, not code paths;
`generic` is the built-in fallback. A new type can therefore be one file and a restart,
and the mechanics are in [docs/agent-profiles.md](agent-profiles.md) ("Adding a profile").

Proposing one is deliberately tiny: open the "Add a new agent type" issue with the
**project's name and a link to it** (preferably GitHub). That is the whole form. We will
reach out, connect the project to a server, and observe what the profile needs — the same
capture-driven way every shipped profile was built. Nobody is asked to fill in a
specification, because the server can see the answers; the proposer's job is to point us
at the project and want it supported.

## What we will figure out together

Nothing here is required from a proposer. It is the list of questions the profile
eventually answers, kept so the conversation has a shape:

| Question | Where the answer lands in the profile |
|---|---|
| How does it speak OpAMP — which client/extension? | the identity evidence in `identity` |
| What MIME type does it report config as? | `config_formats` |
| Does it have a "components" concept, and does it report one? | `components` / `components_engine`, or an honest "unsupported" |
| Does it send metrics to the server? | `telemetry`, `panels` — omitted means the UI says so truthfully |
| What config snippet starts it against this server? | `snippet`, `onboarding_snippet` |
| What does it do with a pushed config, and a bad one? | `guidance` |
| Should the server judge its config's structure? | `validates_structure` + an engine — the only step that needs code, and it is optional |

A real status report captured from a running agent answers most of the table by itself.
That capture is our job, not the proposer's — it is how `fluentbit` got identity right
(`service.type`, not `service.name`) and how `osquery` found its durable signal (the
`osquery.` attribute namespace). A profile that is *almost* right is the dangerous
failure mode: it matches, it renders, and it is quietly wrong about the product — and
real captures are the antidote.

## Who does what

- **The proposer** names the project and links to it. Done.
- **The maintainer (or an LLM agent) working the issue** connects an agent to a server,
  captures what it reports, writes the profile file, and writes the one test that replays
  the capture (`tests/test_<name>_profile.py` — see
  [docs/agent-profiles.md](agent-profiles.md#adding-a-profile)). A proposer who wants to
  write the profile themselves is welcome to — it can be one YAML file — and gets review,
  not gatekeeping.
- **Nobody** is asked to modify `server/*.py` unless the product wants config structure
  judged, and even then only in agreement.

## References

- [docs/agent-profiles.md](agent-profiles.md) — the dictionary, the file format, the guardrails.
- `server/profiles/schema.py` — the field reference a profile is validated against.
- The issue template: `.github/ISSUE_TEMPLATE/new-agent-type.yml`.
- The proofs this process produced: #123 (Fluent Bit), #141 (osquery).
