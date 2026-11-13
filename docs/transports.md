# Transports, limits and message semantics

How the server behaves on the wire, and what it does when an agent's reports are
partial, duplicated or late. Agent-side config lives in
[connect-collectors.md](connect-collectors.md); the endpoint table is in
[api-reference.md](api-reference.md).

## HTTP and WebSocket

Plain HTTP is the default and is what the collector's `opamp` extension uses
(`endpoint: http://host:4320/v1/opamp`). The same path also accepts a WebSocket
upgrade, which it does by default: an HTTP-only client never sends one, so the
default costs it nothing, and a WebSocket client needs no server-side setup.
`OPAMP_WS_ENABLED=false` refuses the upgrade with `1008`. Frames are a base-128
varint header (must be `0`) followed by the protobuf message. Both transports run
the same message handler, so agent state is identical either way
(`tests/test_ws_parity.py` asserts it).

WebSocket upgrades are not covered by `CORSMiddleware`, so the handshake checks
`Origin` itself: agents (which send no `Origin`) always pass, and a browser origin
must be listed in `CORS_ORIGINS` unless that is `*`. If you launch uvicorn
directly, set `--ws-max-size` to at least `OPAMP_MAX_MESSAGE_BYTES` or uvicorn's
16 MiB default will close frames before the app limit applies.

Both transports enforce `OPAMP_MAX_MESSAGE_BYTES` (64 MiB by default). On HTTP the
limit is checked against `Content-Length` first and then while streaming, **after**
`Content-Encoding: gzip` decompression — so a small compressed body cannot expand
past the cap. An oversized body gets `413 Content Too Large` and changes no state
(no registration, no heartbeat, no metrics). A body that is not a usable
`AgentToServer` — unparseable protobuf, an empty `instance_uid`, or corrupt gzip —
gets `400` with a `ServerToAgent.error_response` (`type: BAD_REQUEST`) and a
human-readable reason; a `Content-Encoding` the server does not implement gets
`415` with the same body. `/v1/metrics` carries OTLP, not OpAMP, and deliberately
does not share this limit. On WebSocket the same rejections send the error frame and
close `1002`; oversized frames close `1009`.

### Compression

Agents may gzip their request body (`Content-Encoding: gzip` or `x-gzip`) and the
server unrolls it *inside* the bounded read, so a decompression bomb is refused at
the limit rather than expanded first. The reply is gzipped when the agent sent
`Accept-Encoding: gzip` **and** the body is at least 1 KiB — a 40-byte capabilities
reply costs more to compress than it saves, so it stays plain. `Vary:
Accept-Encoding` is set so a cache cannot serve gzip to a client that cannot read it.
The collector's `opamp` extension does not enable compression today; `opamp-go`
clients that call `EnableCompression()` get it for free.

## Incomplete status reports

Agents may omit unchanged sub-messages, so an `AgentToServer` is not necessarily a
full picture. The only signal the spec provides is `sequence_num`: anything that is
not exactly one greater than the last accepted value means status was missed, and
the server answers with `ReportFullState` so the agent resends everything. Three
details are worth knowing:

- the counter is persisted per agent (`GET /agent/{id}` → `last_sequence_num`) so it
  survives a server restart, which is the case the spec's rule exists for;
- `sequence_num: 0` means "not set" and never triggers an ask, so an SDK that never
  fills the field is not asked for full state on every message;
- the ask is rate limited per agent to `REPORT_FULL_STATE_MIN_INTERVAL_SECONDS`
  (60), because a flapping agent with a broken counter would otherwise turn every
  poll into a full-state request. Counted in
  `opamp_full_state_requested_total{reason}` — `new_agent` for the registration
  baseline, `sequence_gap` for a repair.

## Hash-only component lists

The spec also lets an agent send only the `hash` of its `AvailableComponents` unless
the server asked for the details with `ReportAvailableComponents`. An agent that does
this is compliant but useless for the manifest feature: `POST /agent/{id}/manifest`,
the component report and the heavy/outdated collector reports all have nothing to work
with. So the server asks — carefully (#77):

- the ask is **one-shot per hash**: a hash-only report makes the next reply set
  `ReportAvailableComponents`, and the same hash is never asked about again. An agent
  that keeps sending it is not in a request loop it cannot leave;
- a **changed hash** (agent upgraded, pipeline edited) earns exactly one new ask;
- the hash we asked about is persisted (`GET /agent/{id}` →
  `last_requested_components_hash`), so a restart does not repeat the ask;
- an agent that ignores the ask is counted (once, at the third hash-only report for
  that hash, then the counter saturates so the steady state stays write-free) and
  logged at `WARNING` — otherwise "this SDK never implements the flag" is invisible.
  `GET /agent/{id}` exposes it as `components_request_ignored`;
- the #70 and #77 asks are independent: one reply may carry `ReportFullState` **and**
  `ReportAvailableComponents`.

Counted in `opamp_available_components_requested_total{reason}` (`hash_only` for the
first ask, `hash_changed` after the list moved) and
`opamp_available_components_ignored_total`.

## When an agent counts as gone (#69)

The server learns each agent's reporting cadence instead of guessing it. The
`polling_interval` of a collector is operator-set (`30s` by default, minutes in a
large fleet), so a fixed timeout shorter than it deletes and re-registers a healthy
agent on every cycle — `connected_at` resets, its persisted metrics are dropped, and
`stale_agent` + `new_agent` webhooks fire forever.

An agent is removed once it has been silent for:

```
max(AGENT_TIMEOUT_SECONDS, AGENT_TIMEOUT_INTERVAL_MARGIN × observed_interval)
```

where `observed_interval` is an EWMA of the gaps between its own messages, not a
configured number. Two details make it behave:

- **One gap is not a cadence.** Gaps under a second (two messages in one batch,
  registration followed immediately by the first report) are ignored, and an
  estimate is only trusted after two real gaps — a restart measures its first gap
  from a heartbeat persisted before the restart.
- **Unknown cadence is not "fast".** An agent that has not reported twice yet gets
  `AGENT_FIRST_CONTACT_SECONDS` (15 min): until one full gap is observed, "polls
  every 10 minutes" and "died on startup" look identical.

What that means in practice: a 30s collector is held to the 180s floor (six
intervals of tolerance), a 120s collector to 360s, a 10-minute collector to 30
minutes — and a collector that dies is gone after ~3 missed polls in every case.

Observability, because "my agent disappeared" must be answerable from the API:
`GET /agent/{id}` reports `report_interval_seconds` (the learned cadence) and
`stale_timeout_seconds` (the budget actually applied to it), and the removal log
line carries the silence, the timeout and the interval. The same endpoint reports
`last_sequence_num` — the last `sequence_num` accepted — so "why did the server ask
this agent for everything?" is answerable without a log line.

## Duplicate `instance_uid`

Two agents can present one uid — a cloned VM, a restored backup, a collector image
with a broken id generator. Left alone they overwrite each other's status, so the
server detects the condition and mints a replacement, per spec §Duplicate
`instance_uid` Detection (#75).

Detection is the interesting half, because a false positive renames a healthy agent:

- **Interleaving host identity.** `service.instance.id` (or `host.name`) is taken
  from `AgentDescription`. Two agents produce `A B A`; a single agent that *renames*
  itself in place produces `A A B B B`, which is **not** a duplicate and is never
  re-issued a uid;
- **Counter regression.** A cloned VM reports the same `host.name` too, so there is
  no identity signal at all. Two agents with independent `sequence_num` counters
  alternating produce a counter that goes *backwards repeatedly* — two decreases in a
  row. One decrease is an agent that restarted, and is left alone.

The newcomer is answered with `agent_identification.new_instance_uid` and keeps being
served under the new id — better than dropping an agent because its clone shares an
id. The old uid's record stays with the agent that owns it, and the new row carries
`uid_reissued_from` and `uid_reissue_reason` so `GET /agent/{id}` explains the rename.
Counted in `opamp_duplicate_instance_uid_total{reason,action}`.

Two limits, both deliberate:

- **An authenticated agent cannot be re-issued.** A bound token is tied to the uid it
  was minted for (#94), and handing any authenticated agent a new uid would get it
  refused on its next message. The duplicate is detected, logged and counted
  (`action="detected"`) but never renamed;
- detection is in-memory. A restart loses the streak and re-detects within two
  messages; the outcome (the new uid) is persisted.

## The `OpAMP-Instance-UID` header

Clients SHOULD send the uid as a request header (spec §Plain HTTP Transport), and the
Go SDK always does. The server reads it (#76) and uses it in exactly two ways, neither
of which can break a working client:

- **cross-check.** A value that disagrees with the body — or is not a uuid at all —
  logs both uids and increments `opamp_instance_uid_header_problems_total{reason}`.
  The body always wins, because proxies legitimately rewrite headers. Set
  `OPAMP_UID_HEADER_STRICT=true` to make a mismatch a `400` instead;
- **naming a body it refused.** For `413` (too large), `415` (unsupported encoding)
  and `400` (unparseable), there is no parsed message to identify, so the header is the
  uid in the log line. That is the difference between "an agent is sending garbage"
  and "some agent is sending garbage".

Uppercase, braces, a `urn:uuid:` prefix and bare 32-character hex are all accepted;
a value that matches the body written as plain ascii is accepted too, which is how
this project's own fixtures name their agents.
