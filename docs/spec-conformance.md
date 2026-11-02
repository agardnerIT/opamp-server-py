# OpAMP specification conformance

What this server promises, clause by clause, against **OpAMP
[v0.20.0](https://github.com/open-telemetry/opamp-spec/releases/tag/v0.20.0)** (commit
`b08624151d000c1ca043b300e1f5e3bc93107987`).

The point of this file is to make a spec bump a **diff of a table** instead of a fresh
code review. It exists next to `tests/conformance/`, which is the machine-checked half:
one thin test per requirement, named after the requirement. Between the two, a reader
can answer "does this server do X?" and — just as important — "is anything *not*
being done on purpose?".

Statuses used below:

| Status | Meaning |
|---|---|
| ✅ covered | A conformance case asserts it and fails if the behaviour is removed |
| ⚠️ deviation | Deliberately different from the spec text; the reason and the issue are named |
| ❌ not implemented | Tracked in an issue; the message is *accepted* and ignored (forward compatible) |
| — n/a | The clause does not apply to this kind of server |

When the spec moves: add or change a case in
`tests/conformance/test_spec_conformance.py`, then flip the row here in the same
commit. A row without a case, or a case without a row, is the drift this file exists
to catch.

## How to check it yourself

```bash
python -m pytest tests/conformance -q        # the spec index
curl -s localhost:4320/health | jq '.spec_version, .capabilities'
```

`GET /health` returns the spec version and the live capability bitmask, so "will my
agent interoperate?" is answerable without reading source (#79).

## §Ensuring Interoperability — the message envelope

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| Every valid `AgentToServer` is answered with a `ServerToAgent` | ✅ | `test_a_status_report_is_answered_with_a_server_to_agent_message` | Content type `application/x-protobuf` |
| The reply echoes `instance_uid` | ✅ | same | A multiplexed socket addresses each reply to the uid in *that* message |
| `ServerToAgent.capabilities` is always present | ✅ | same, `tests/test_capabilities.py` | A capability bit is a promise, so only implemented bits are sent (#74) |
| A server MUST NOT break on a legal message it does not implement | ✅ | `test_a_legal_message_with_unimplemented_shapes_does_not_break_the_reply` | Unknown sub-messages are accepted and ignored |
| gzip request bodies | ✅ | `test_a_gzipped_body_is_processed_identically`, `tests/test_compression.py` | Reply is gzipped when the client advertised it and the body is worth it (#71) |
| Size limit on received messages | ✅ | `test_an_oversized_message_is_refused_before_it_is_read`, `tests/test_http_limits.py` | 413 on the declared length, the streamed bytes, **and** the decompressed size; `OPAMP_MAX_MESSAGE_BYTES` |
| Malformed input → `ServerErrorResponse{BAD_REQUEST}` | ✅ | `test_an_unparseable_body_is_a_bad_request_and_changes_no_state` | 400 with a spec-shaped `error_response`, nothing registered |
| Unsupported `Content-Encoding` | ⚠️ deviation | `test_an_unimplemented_content_encoding_is_refused` | **415** (body still a spec `BAD_REQUEST`). The spec defines no "I cannot decode this envelope" shape; 415 is what stops a client hunting a protobuf bug that is not there (#71) |
| `ServerErrorResponse{UNAVAILABLE, retry_info}` for overload | ⚠️ deviation | `tests/test_throttle.py` | Refused at the transport instead (HTTP 503 / refused WS upgrade, both with `Retry-After` ≥ 30 s). In-band `UNAVAILABLE` on an open socket made otelcol-contrib 0.162 hot-loop at ~300 reconnects/s/agent (#78) |

## §Agent Identification

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| An `instance_uid` identifies one agent | ✅ | `test_two_agents_sharing_one_instance_uid_are_separated`, `tests/test_instance_uid_identity.py` | Duplicates detected from interleaved host identities or a counter that keeps going backwards (#75) |
| Duplicate uid → `agent_identification.new_instance_uid` | ✅ | same | Only when the server may re-issue: a token is bound to the uid it was minted for (#94), so authenticated agents are detected and left alone |
| `instance_uid` is 16 bytes | ⚠️ deviation | `tests/test_protocol_validation.py` | Non-empty uids of any length are accepted; empty is refused. Unicode/ASCII ids are used by every harness and by this repo's own fixtures (#73) |
| Mismatch between a header and the body | ⚠️ extension | `tests/test_instance_uid_identity.py` | `OpAMP-Instance-UID` is read and cross-checked (#76). Not in the spec: the body always wins unless `OPAMP_UID_HEADER_STRICT=true` |

## §Requesting instance_uid

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `AgentToServerFlags_RequestInstanceUid` → reply carries a new uid | ✅ | `test_request_instance_uid_is_answered_with_a_new_uid` | UUIDv7, and the agent is keyed by the new uid; the temporary record is removed |
| The flag may be sent with no uid at all | ⚠️ deviation | `test_a_message_without_an_instance_uid_is_a_bad_request` | Refused: an empty uid cannot be filed (it would share one registry row), so the flag is honoured for an agent that still names itself |
| With agent authentication | ⚠️ deviation | `tests/test_protocol_validation.py` | Refused with `BAD_REQUEST` (#94, D1): a minted uid has no credential behind it |

## §Agent Status Compression

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| Omitted sub-messages mean "unchanged" | ✅ | `test_a_message_with_only_identity_and_counter_is_accepted` | The smallest legal message is `instance_uid` + `sequence_num` |
| `sequence_num` is exactly one greater than the last accepted | ✅ | `test_consecutive_sequence_nums_are_not_asked_for_full_state` | Wrap-aware arithmetic, not `current > previous` |
| "Not exactly one greater" → `ReportFullState` | ✅ | `test_a_sequence_num_gap_is_asked_for_full_state`, `test_a_duplicate_sequence_num_is_a_gap` | A decrease (restart with the same uid) and a repeat (replay) are both gaps (#70) |
| `sequence_num: 0` means "not set" | ✅ | `test_sequence_num_zero_never_asks_for_full_state` | Never asks, or an SDK that omits the field would be polled for everything forever |
| `ReportFullState` is rate limited | ⚠️ extension | `tests/test_sequence.py` | At most once per `REPORT_FULL_STATE_MIN_INTERVAL_SECONDS` (60) per agent: the spec mandates the flag, not a bounded number of them |
| The counter survives a server restart | ✅ | `tests/test_sequence.py` | Needed for the spec's rule to work across the case it exists for |

## §AvailableComponents

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `hash` is present in every AvailableComponents message | ✅ | `tests/test_available_components_request.py` | Used as the one-shot key for the ask below |
| `components` may be omitted unless the server asked | ✅ | `test_a_hash_only_report_is_asked_for_the_components` | The reply sets `ReportAvailableComponents` (#77) |
| The ask is not a poll loop | ⚠️ extension | `test_the_components_ask_is_one_shot_per_hash` | Once per hash, persisted, so an agent that keeps sending a hash is asked once and an upgraded agent is asked once more. Re-asking every poll is a loop the agent cannot leave |
| Full component lists are accepted and usable | ✅ | `test_a_full_component_list_is_accepted_without_an_ask` | Drives the OCB manifest, component reports and inventory validation warnings |

## §Agent Disconnect

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `agent_disconnect` → the server drops the agent's state | ✅ | `test_a_disconnect_removes_the_agent_and_asks_for_nothing`, `tests/test_ws.py` | Also unbinds the uid from a WebSocket connection |
| No reply fields other than what the spec allows on a disconnect | ✅ | same | No `ReportFullState`, no component ask: a request to an agent that is leaving is a request sent to nobody |

## §Remote Configuration (Beta)

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `OffersRemoteConfig` only while the posture allows push | ✅ | `tests/test_capabilities.py`, `tests/test_posture.py` | Without admin auth, agent auth, TLS upstream, audit and config-at-rest, the bit is absent and `/config/*` answers 403 (#96) |
| Offer only when the agent's reported hash differs | ✅ | `tests/test_push_delivery.py`, `tests/test_remote_config.py` | The reconciliation MUST is a hash comparison, so the hash is stored (hex) (#81) |
| `RemoteConfigStatus{APPLIED, APPLYING, FAILED, UNSET}` | ✅ | `test_a_remote_config_status_is_recorded_with_its_hash` | Exposed as the wire enum name on `GET /agent/{id}` |
| No `AcceptsRemoteConfig` → no offer | ✅ | `tests/test_push_delivery.py` | A supervisor that would ignore the config is not sent one |
| Two-phase propose → approve → apply before anything is delivered | ⚠️ extension | `tests/test_config_control.py`, `tests/test_rollout.py` | The spec has no approval concept; this server refuses to be a fleet-takeover tool (#99, #103) |
| A `FAILED` revision is never auto-retried | ⚠️ extension | `tests/test_push_delivery.py` | Poisoned, requires re-approval. Auto-retry is how a bad config loops a fleet |

## §Packages (Beta)

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `OffersPackages`, `PackagesAvailable` | ❌ not implemented | [#82](https://github.com/agardnerIT/opamp-server-py/issues/82) | Bit stays off: a set bit is a promise |
| `AcceptsPackagesStatus` | ✅ (stored only) | `test_a_legal_message_with_unimplemented_shapes_does_not_break_the_reply` | Stored, displayed, reported; nothing is acted on. Delivered once #82 lands |
| Package signatures (§Code Signing) | — n/a | — | Verification is the agent's job; this server distributes nothing yet |

## §Connection Settings (Beta)

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `OffersConnectionSettings`, `ConnectionSettingsOffers` | ❌ not implemented | [#83](https://github.com/agardnerIT/opamp-server-py/issues/83) | Endpoint migration (CSR/TOFU) is the use case |
| `AcceptsConnectionSettingsRequest` | ❌ not implemented | same | A request is accepted and ignored — see the forward-compatibility case |
| `ConnectionSettingsStatus` | ❌ not implemented | same | Not read; needed before an offer could be trusted |

## §Custom messages / capabilities

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| `CustomCapabilities`, `CustomMessage` | ❌ not implemented | [#85](https://github.com/agardnerIT/opamp-server-py/issues/85) | Upstream marks them *Development*; the message shapes are accepted and ignored |
| Own telemetry (`ReportsOwnMetrics/Traces/Logs`) | ✅ (out of band) | `tests/test_metrics_persistence.py` | Agents send OTLP to `/v1/metrics`; OpAMP carries only the capability bits. The spec's own-telemetry endpoint is the collector's, not the server's |

## §Security Considerations

The spec leaves most of this to the implementer; these are the promises this server
makes, all of them tested:

| Requirement | Pinned by | Notes |
|---|---|---|
| Agent → server authentication | `tests/test_agent_auth.py` | `OPAMP_AGENT_AUTH=token`; a token is bound to one uid (#94) |
| Admin authentication and roles | `tests/test_auth.py` | viewer < operator < approver < admin; two-phase push needs approver (#95) |
| Fail-closed posture | `tests/test_posture.py` | Push refuses to start without the prerequisites (#96) |
| Config bodies encrypted at rest | `tests/test_crypto.py`, `tests/test_redaction.py` | AES-GCM bound to the row; one redaction filter everywhere (#98) |
| Audit trail | `tests/test_audit.py` | Every privileged write, with the principal |

## WebSocket transport (§optional)

| Clause | Status | Pinned by | Notes |
|---|---|---|---|
| Framing and message handling identical to HTTP | ✅ | `tests/test_ws.py` (parity class) | Same handler, so the two cannot drift |
| Close codes: policy violation, protocol error, 1009 oversize | ✅ | `tests/test_ws.py` | Refused upgrade for overload, per the deviation above |
| Multiplexing several uids on one socket | ✅ | `tests/test_ws.py` | The reply is addressed to the uid in the message |
| Lifecycle/backpressure/parity hardening | ⚠️ partial | [#90](https://github.com/agardnerIT/opamp-server-py/issues/90), [#91](https://github.com/agardnerIT/opamp-server-py/issues/91) | Outbound queue drops the oldest frame and relies on reconciliation; a dropped config frame is re-sent, never counted as delivered (#102) |

## Not covered by this table

* **Interoperability with a real client.** Runs in CI on every PR
  (`.github/workflows/collector-e2e.yml`, #113): `otelcol-contrib 0.140.0` with the
  upstream `opamp` extension registers, reports health and its component list, and the
  server builds its OCB manifest from the pipeline it actually runs. Asserted by
  `scripts/collector_e2e.py`, which is also runnable locally. This is the one thing a
  self-consistent test suite cannot prove.
* **A supervisor's local agent management.** This is a server; it does not manage
  local processes, which is why several supervisor-only clauses are marked n/a.
