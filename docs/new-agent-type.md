# Proposing a new agent type (#145)

This server recognises an agent through a **profile** — one YAML file in
`server/profiles/data/`. `otelcol`, `fluentbit` and `osquery` are files, not code paths;
`generic` is the built-in fallback. A new type can therefore be one file and a restart,
and the mechanics are in [docs/agent-profiles.md](agent-profiles.md) ("Adding a profile").

This page is about the *proposal* half, and the message is short: **nominate first.**
Open the "Add a new agent type" issue with nothing more than the product's name. Fill in
whatever else you happen to know; skip everything else. We will work with you to connect
the product to a server, watch what it actually reports, and write the profile from
observation — that is how every shipped profile was built anyway.

## The one thing that genuinely helps

If it is easy, paste a **real status report from a running agent** into the issue. That
single artifact is worth more than any amount of documentation: it is how `fluentbit`
got identity right (`service.type`, not `service.name`) and how `osquery` found its
durable signal (the `osquery.` attribute namespace). But it is optional — if capturing
one is work, nominate and we will capture one together.

## What we will figure out together

None of this is required from a proposer. It is the list of questions the profile
eventually answers, kept here so the conversation has a shape and so nobody feels the
issue must arrive complete:

| Question | Where the answer lands in the profile |
|---|---|
| How does it speak OpAMP — which client/extension? | the identity evidence in `identity` |
| What MIME type does it report config as? | `config_formats` |
| Does it have a "components" concept, and does it report one? | `components` / `components_engine`, or an honest "unsupported" |
| Does it send metrics to the server? | `telemetry`, `panels` — omitted means the UI says so truthfully |
| What config snippet starts it against this server? | `snippet`, `onboarding_snippet` |
| What does it do with a pushed config, and a bad one? | `guidance` |
| Should the server judge its config's structure? | `validates_structure` + an engine — the only step that needs code, and it is optional |

A capture from a real agent answers most of the table by itself; the rest is a short
conversation.

## Who does what

- **The proposer** names the product and, ideally, helps get one agent talking to a
  server. That is the whole ask.
- **The maintainer (or an LLM agent) working the issue** captures the payload, writes the
  profile file, and writes the one test that replays the capture
  (`tests/test_<name>_profile.py` — see
  [docs/agent-profiles.md](agent-profiles.md#adding-a-profile)). A proposer is welcome to
  write the profile themselves — it can be one YAML file — and will get review, not gatekeeping.
- **Nobody** is asked to modify `server/*.py` unless the product wants config structure
  judged, and even then only in agreement.

## Why observation beats a specification

A profile that is *almost* right is the dangerous failure mode: it matches, it renders,
and it is quietly wrong about the product. Real captured payloads are the antidote, which
is why this project captures rather than asks people to specify. But that is work for the
session in which the profile is written — never a barrier for the person who had the
idea.

## References

- [docs/agent-profiles.md](agent-profiles.md) — the dictionary, the file format, the guardrails.
- `server/profiles/schema.py` — the field reference a profile is validated against.
- The issue template: `.github/ISSUE_TEMPLATE/new-agent-type.yml`.
- The proofs this process produced: #123 (Fluent Bit), #141 (osquery).
