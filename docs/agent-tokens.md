# Agent tokens (agent → server)

Agents authenticate with `Authorization: Bearer <token>` **before** they connect;
the server refuses a request without one. There is no tokenless mode (#125): a
tokenless OpAMP endpoint believes whatever `instance_uid` the message body claims,
so it is not a deployment this server supports.

`OPAMP_AGENT_AUTH` still exists so an old `.env` says something, but the only
accepted value is `token` (unset means the same). Anything else refuses to start:
`off`, `mtls` (never enforced by this build — certificates are terminated by your
proxy) and any typo. The old `OPAMP_ALLOW_UNAUTHENTICATED_AGENTS=true` escape hatch
is gone and is refused by name, rather than silently enforcing `token` on a fleet
that was promised otherwise. To move an open deployment: mint a fleet token, put it
in every agent's config, then restart.

The credential rides in a header, because that is what `opampextension` supports:

```yaml
extensions:
  opamp:
    server:
      http:                               # or `ws:`, with the same headers block
        endpoint: http://127.0.0.1:4320/v1/opamp
        headers:
          Authorization: "Bearer ${env:OPAMP_AGENT_TOKEN}"
```

Keep the token out of git by passing it as an environment variable: `${env:…}` is
expanded by the collector and the value is `configopaque`, so the agent's own
effective-config report redacts it.

Why the token lives in the config file instead of being handed down the wire:
`extension/opampextension` registers no `OnOpampConnectionSettings` callback, so
an in-band "here is your token" reply is ignored. The supervisor can consume
connection settings; the collector extension cannot. Registration-on-first-use
and CSR/TOFU stay in #83.

## Two kinds of token

| Kind | Mint | What it is | Use when |
|---|---|---|---|
| **fleet** | `POST /agent-tokens` (`opampctl agent-tokens create`) | Access, up to `max_agents` agents. Each agent's `instance_uid` stays its identity | Onboarding a fleet — including a fleet that does not exist yet, because nothing has to be registered first |
| **bound** | `POST /agents/{id}/token` (`opampctl agents token create <id>`) | The credential **is** that one agent's identity; another uid is refused | Pinning a credential to an agent you already know |

The fleet token is the default path, and the reason is bootstrap: a bound token
needs an `instance_uid`, and an agent only reveals that by connecting — so a fresh
install could never mint the credential it needs to let the first agent in.

Tokens carry two limits, both optional and both enforced on every message:

- **`expires_in_days`** — 1, 7 or 30 (or any 1–365), or `null` for never. An expired
  token is refused `expired`.
- **`max_agents`** — the cap. The agent that would exceed it is refused
  `agent_limit`, with a line in the server log, and its state is never touched. A
  leaked token therefore cannot consume the server, and the count is of *distinct
  agents*, so re-polling does not eat slots. Slots are never released — a token that
  could recycle uids forever would defeat the cap; mint a new token when you need room.

The token response also carries a paste-ready collector snippet; the UI's connect
snippet (Help page and the Agents empty state) is not hardcoded either: it reads
`config_push.agent_auth` from `GET /health` and renders the `headers:` block. Agent
auth is mandatory, so the snippet always carries a credential and the empty fleet
mints one rather than printing a placeholder that would 401 (#124).

## What a rejected token sees

With a token, in either kind:

- a missing, unknown, revoked or expired token gets **401** (HTTP) or **1008**
  (WebSocket) with **no** `ServerToAgent` body — an unauthenticated client learns
  nothing, not even which capabilities exist;
- a **bound** token claiming a *different* `instance_uid` gets **403** / 1008,
  rejected before any state is applied, so the real agent's record is untouched;
- every rejection is counted in `opamp_agent_auth_failures_total{reason}` —
  `missing`, `malformed`, `unknown`, `revoked`, `expired`, `agent_limit`,
  `uid_mismatch`, `binding_conflict` — and written to the [audit log](security.md#audit-log);
- a live WebSocket binding may only move to the **same credential**, so a second
  token cannot take over an agent (a legitimate reconnect moves cleanly, and two
  agents sharing one fleet token do not fight over the socket map).

Tokens are `opamp_agent_…`, stored as SHA-256 only, and returned exactly once at
creation. They never appear in `/agents`, logs, audit entries or error bodies —
listings and audit entries use the `token_id` (`agtok_…`) handle instead.
`GET /agents` and `GET /agent/{id}` report each agent's credential state as
`{"enrolled", "active", "last_seen_at", "revoked_at", "tokens"}` so you can watch a
cutover and answer "which token let this agent in".

## First run (nothing to migrate)

```bash
# 1. Mint a token for the fleet — no agent has to exist yet
opampctl --password "$ADMIN_PASSWORD" agent-tokens create \
    --label prod --expires-in-days 30 --max-agents 50
#    -> {"token": "opamp_agent_...", "token_id": "agtok_...", "collector_snippet": ...}

# 2. Put the headers block in each agent's config (the response carries it ready to
#    paste) and reload the collector:
#      extensions.opamp.server.http.headers:
#        Authorization: "Bearer opamp_agent_..."
#    (server.ws.headers instead, if that agent speaks WebSocket). On an empty fleet the
#    UI's Agents page does steps 1 and 2 for you.

# 3. Watch them arrive, and watch the cap
opampctl agents list --pretty
opampctl agent-tokens list            # agents_used / max_agents, expires_at
```

## Upgrading a fleet that is still open

Agent auth is mandatory as of #125. An existing deployment that relied on a
tokenless endpoint starts answering 401, and a server that still has
`OPAMP_AGENT_AUTH=off`/`mtls` or `OPAMP_ALLOW_UNAUTHENTICATED_AGENTS=true` refuses
to start with a message naming the variable. Do this before upgrading:

```bash
# Close it with no per-agent work: mint ONE fleet token, deploy it everywhere
opampctl --password "$ADMIN_PASSWORD" agent-tokens create --label prod --max-agents 500
#    ...then remove OPAMP_AGENT_AUTH / OPAMP_ALLOW_UNAUTHENTICATED_AGENTS and restart.
```

The fleet token is the point: the old runbook needed a token per agent, minted only
after that agent had registered. Watch for stragglers with
`curl -s localhost:4320/metrics | grep opamp_agent_auth_failures_total` — the
`reason` label distinguishes a config that has not reloaded (`missing`) from a
token problem (`unknown`/`revoked`/`expired`/`agent_limit`).

Rotation needs no downtime: mint a new token, deploy it, and revoke the old one
once `agents_used` on the new one has caught up. Revocation
(`opampctl agent-tokens revoke <agtok_…>`, or `agents token revoke <id>` for a
bound one) blocks delivery but **keeps** the agent's record, state and desired
config — revocation is not a wipe, deliberately: you need that config to restore
the agent later.

An agent with a revoked or broken credential retries forever with backoff, because
opamp-go cannot tell 401 from 403 — watch the failure metric, not the agent log.
