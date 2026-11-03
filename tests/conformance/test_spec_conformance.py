"""The spec index: one case per OpAMP requirement this server claims to meet (#80).

Read this file top to bottom and you have read what the server promises. Cases are
named after the requirement, not after the issue that implemented it, so a spec bump
is a diff of this file instead of a fresh review of the whole server.

Two rules keep it honest:

* **A case asserts the requirement, not the implementation.** "A hash-only report is
  answered with `ReportAvailableComponents`", not "`_note_available_components` set a
  field".
* **A case is thin.** The exhaustive behavioural coverage lives in the suite that owns
  it (`test_sequence.py`, `test_http_limits.py`, `test_available_components_request.py`,
  ...) and each case names it, so this file stays a readable index rather than a
  second copy that drifts.

What is *not* here is as important: requirements this server deliberately does not
meet (or has not implemented yet) are recorded in `docs/spec-conformance.md` with the
issue that owns them, so silence is a decision rather than an oversight.

Spec: OpAMP `v0.20.0` (commit `b0862415…`, see `harness.SPEC_VERSION`). The
specification itself is not vendored in this repository, so cases cite section names
rather than line numbers.
"""

from __future__ import annotations

import gzip

import pytest

from proto import opamp_pb2
from server import remote_config
from server.main import app
from server.state import AGENT_REGISTRY
from server.ws import REGISTRY as WS_REGISTRY
from tests.conformance import harness as spec
from tests.conformance.harness import OpampHarness

PROTOBUF = "application/x-protobuf"


@pytest.fixture
def opamp(monkeypatch):
    """A clean in-process OpAMP client: no push, no agent auth, no leftover fleet."""
    WS_REGISTRY.reset()
    for existing in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(existing)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("OPAMP_MAX_MESSAGE_BYTES", raising=False)
    client = OpampHarness(app)
    yield client
    client.close()
    WS_REGISTRY.reset()
    for existing in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(existing)


# --- §Ensuring Interoperability: the message envelope ------------------------


def test_a_status_report_is_answered_with_a_server_to_agent_message(opamp):
    """§AgentToServer: every valid message gets a `ServerToAgent` reply.

    `instance_uid` is echoed so the agent can match the reply to the message it sent
    (§ServerToAgent), and `capabilities` is always present so the agent learns what the
    server can do on the very first exchange (§Initial Handshake).

    Envelope detail: `tests/test_server.py`.
    """
    uid = spec.uid("envelope")
    response = opamp.send(spec.status_only(uid, sequence_num=1))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(PROTOBUF)

    reply = opamp.expect(spec.status_only(uid, sequence_num=2))
    assert reply.instance_uid == uid
    assert reply.capabilities != 0
    assert not reply.HasField("error_response")


def test_a_gzipped_body_is_processed_identically(opamp):
    """§HTTP Transport: a gzipped `AgentToServer` is the same message.

    The reply must not depend on how the request was framed — that is the whole point
    of §Ensuring Interoperability.

    Detail: `tests/test_compression.py`.
    """
    uid = spec.uid("gzip")
    plain = opamp.expect(spec.status_only(uid, sequence_num=1))
    compressed = opamp.expect(spec.status_only(uid, sequence_num=2), gzip_body=True)

    assert compressed.instance_uid == plain.instance_uid == uid
    assert compressed.capabilities == plain.capabilities
    assert not compressed.HasField("error_response")


def test_an_unparseable_body_is_a_bad_request_and_changes_no_state(opamp):
    """§ServerErrorResponse: malformed input is `BAD_REQUEST`, not a dropped connection.

    The error body is a `ServerToAgent.error_response` so an agent can read why, and
    nothing is registered — a refusal must not create fleet entries.

    Detail: `tests/test_protocol_validation.py`.
    """
    response = opamp.send(raw_body=b"this is not a protobuf message")
    assert response.status_code == 400

    parsed = opamp_bad_request(response)
    assert parsed.error_response.type == (
        opamp_pb2.ServerErrorResponseType.ServerErrorResponseType_BadRequest
    )
    assert parsed.error_response.error_message
    assert AGENT_REGISTRY.count == 0


def test_an_unimplemented_content_encoding_is_refused(opamp):
    """§HTTP Transport: only encodings we implement are accepted.

    415 says "cannot decode this envelope" (the body is still a spec-shaped
    `BAD_REQUEST`), which is what stops a client hunting a protobuf bug that is not
    there.

    Detail: `tests/test_http_limits.py`.
    """
    response = opamp.send(
        spec.status_only(spec.uid("brotli"), sequence_num=1),
        headers={"Content-Encoding": "br"},
    )

    assert response.status_code == 415
    assert AGENT_REGISTRY.count == 0


def test_an_oversized_message_is_refused_before_it_is_read(opamp, monkeypatch):
    """§HTTP Transport: a message over the size limit is refused, not buffered.

    The limit is read from configuration per request, so the case can shrink it rather
    than allocating 64 MiB: what is being pinned is that the *declared* length is
    enough to refuse, before a single body byte is consumed.

    Detail: `tests/test_http_limits.py`.
    """
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "1024")

    response = opamp.send(
        spec.status_only(spec.uid("too-big"), sequence_num=1), content_length=4096
    )

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


def test_an_oversized_compressed_body_is_refused_on_the_decompressed_size(opamp, monkeypatch):
    """§HTTP Transport: gzip does not buy an attacker (or an agent) past the limit.

    A small gzipped body that expands past the limit is a decompression bomb, so the
    limit has to apply to the expanded bytes.

    Detail: `tests/test_http_limits.py`.
    """
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "1024")

    response = opamp.send(
        raw_body=gzip.compress(b"\x00" * (2 * 1024 * 1024)),
        headers={"Content-Encoding": "gzip", "Content-Type": PROTOBUF},
    )

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


# --- §Agent Identification --------------------------------------------------


def test_a_message_without_an_instance_uid_is_a_bad_request(opamp):
    """§Agent Identification: every message after the first carries an identity.

    A message with no uid cannot be filed, so it is refused rather than guessed at.
    The one exception is an agent asking for one (`test_request_instance_uid_*`).

    Detail: `tests/test_protocol_validation.py`.
    """
    message = opamp_pb2.AgentToServer()
    message.sequence_num = 1
    message.health.healthy = True

    response = opamp.send(message)

    assert response.status_code == 400
    assert AGENT_REGISTRY.count == 0


def test_request_instance_uid_is_answered_with_a_new_uid(opamp):
    """§Requesting instance_uid: the agent asks, the server mints.

    The replacement travels in `agent_identification.new_instance_uid`, and the record
    is keyed by the new uid — the temporary identity must not become a fleet entry.

    Note the deliberate edge: the flag is honoured for an agent that *has* a uid to be
    answered under, not for one that sends none (previous case). Minting onto an empty
    id would file every restored agent under the same key.

    Detail: `tests/test_protocol_validation.py`, `tests/test_instance_uid_identity.py`.
    """
    temporary = spec.uid("losing-its-identity")
    reply = opamp.expect(spec.request_instance_uid(temporary))

    assert reply.HasField("agent_identification")
    minted = reply.agent_identification.new_instance_uid
    assert len(minted) == 16
    assert minted != temporary
    assert AGENT_REGISTRY.get(minted.hex()) is not None
    assert AGENT_REGISTRY.get(temporary.hex()) is None


def test_two_agents_sharing_one_instance_uid_are_separated(opamp):
    """§Agent Identification: a uid identifies one agent.

    Two agents alternating under one uid (a cloned VM, a restored backup) are detected
    from the interleaving and the second one is answered with a replacement uid.

    Detail: `tests/test_instance_uid_identity.py`.
    """
    uid = spec.uid("duplicate")

    def with_host(host: str, sequence_num: int) -> opamp_pb2.AgentToServer:
        message = spec.status_only(uid, sequence_num=sequence_num)
        attribute = message.agent_description.identifying_attributes.add()
        attribute.key = "host.name"
        attribute.value.string_value = host
        return message

    opamp.expect(with_host("host-a", 1))
    opamp.expect(with_host("host-b", 2))
    reply = opamp.expect(with_host("host-a", 3))

    assert reply.HasField("agent_identification")
    assert reply.agent_identification.new_instance_uid
    # The incumbent's record is untouched: only the returning agent is re-identified.
    assert AGENT_REGISTRY.get(uid.hex()) is not None


# --- §Agent Status Compression ---------------------------------------------


def test_a_message_with_only_identity_and_counter_is_accepted(opamp):
    """§Agent Status Compression: omitted sub-messages mean "unchanged", not "gone".

    An agent MAY omit anything that has not changed, so the smallest legal message is
    `instance_uid` + `sequence_num`. The server must keep the state it already has.

    Detail: `tests/test_state.py`, `tests/test_agent_write_batching.py`.
    """
    uid = spec.uid("compression")
    opamp.expect(spec.first_report(uid, sequence_num=1))

    opamp.expect(spec.heartbeat(uid, sequence_num=2))

    agent = opamp.agent(uid.hex())
    assert agent["healthy"] is True
    assert agent["description"]["identifyingAttributes"]
    assert agent["components"]["receiver"]
    assert agent["package_statuses"]["packages"]


def test_consecutive_sequence_nums_are_not_asked_for_full_state(opamp):
    """§Status Compression: a correct counter means nothing was missed, so ask nothing."""
    uid = spec.uid("consecutive")
    opamp.expect(spec.heartbeat(uid, sequence_num=1))  # registration baseline

    reply = opamp.expect(spec.heartbeat(uid, sequence_num=2))

    assert spec.asks_for_full_state(reply) is False


def test_a_sequence_num_gap_is_asked_for_full_state(opamp):
    """§Status Compression: a gap means status was missed; the recovery is `ReportFullState`.

    Detail: `tests/test_sequence.py`.
    """
    uid = spec.uid("gap")
    opamp.expect(spec.heartbeat(uid, sequence_num=1))
    opamp.expect(spec.heartbeat(uid, sequence_num=2))

    reply = opamp.expect(spec.heartbeat(uid, sequence_num=9))

    assert spec.asks_for_full_state(reply) is True


def test_a_duplicate_sequence_num_is_a_gap(opamp):
    """§Status Compression: "not exactly one greater" includes a repeat (replay)."""
    uid = spec.uid("duplicate-seq")
    opamp.expect(spec.heartbeat(uid, sequence_num=1))
    opamp.expect(spec.heartbeat(uid, sequence_num=2))

    reply = opamp.expect(spec.heartbeat(uid, sequence_num=2))

    assert spec.asks_for_full_state(reply) is True


def test_sequence_num_zero_never_asks_for_full_state(opamp):
    """§Status Compression: `0` is "not set", so an SDK that never fills it is left alone.

    Anything else turns a client that does not implement the field into a
    full-state request on every poll.
    """
    uid = spec.uid("no-sequence")
    opamp.expect(spec.heartbeat(uid, sequence_num=0))  # registration baseline

    for _ in range(3):
        assert spec.asks_for_full_state(
            opamp.expect(spec.heartbeat(uid, sequence_num=0))
        ) is False

    assert opamp.agent(uid.hex())["last_sequence_num"] is None


# --- §AvailableComponents ---------------------------------------------------


def test_a_hash_only_report_is_asked_for_the_components(opamp):
    """§AvailableComponents: `components` may be omitted until the server asks.

    `ReportAvailableComponents` is that ask. Without it the component list never
    arrives, and every component-shaped feature is empty for a compliant agent.

    Detail: `tests/test_available_components_request.py`.
    """
    reply = opamp.expect(spec.hash_only_components(spec.uid("hash-only")))

    assert spec.asks_for_components(reply) is True


def test_the_components_ask_is_one_shot_per_hash(opamp):
    """§AvailableComponents: the ask is a request, not a poll loop.

    An agent that keeps sending a hash it has already been asked about is not asked
    again; a *changed* hash (upgrade, pipeline edit) earns one new ask.
    """
    uid = spec.uid("hash-loop")
    assert spec.asks_for_components(
        opamp.expect(spec.hash_only_components(uid, sequence_num=1, seed="v1"))
    )

    for sequence_num in (2, 3, 4):
        assert spec.asks_for_components(
            opamp.expect(
                spec.hash_only_components(uid, sequence_num=sequence_num, seed="v1")
            )
        ) is False

    assert spec.asks_for_components(
        opamp.expect(spec.hash_only_components(uid, sequence_num=5, seed="v2"))
    )


def test_a_full_component_list_is_accepted_without_an_ask(opamp):
    """§AvailableComponents: the full list is what `components` is for.

    Once it arrives the server can answer questions about the agent's components, and
    it must not ask again for the list it just received.
    """
    uid = spec.uid("full-components")
    opamp.expect(spec.first_report(uid, sequence_num=1))

    reply = opamp.expect(spec.full_components(uid, sequence_num=2))

    assert spec.asks_for_components(reply) is False
    receiver = opamp.agent(uid.hex())["components"]["receiver"][0]
    assert receiver["id"] == "otlp"
    assert receiver["used"] is True


# --- §Custom Messages -------------------------------------------------------


def test_custom_capabilities_are_stored_and_a_message_is_answered(opamp):
    """§Custom Messages: capabilities are data, and a legal message is never dropped.

    The server stores the agent's whole `custom_capabilities` list (replaced, never
    appended) and accepts an inbound `custom_message` without interpreting it — the
    payload contract belongs to the capability, and upstream marks both messages
    *Development*, so guessing at a shape would be the one unrecoverable move.

    Detail: `tests/test_custom_messages.py` (delivery, refusals, exactly-once).
    """
    uid = spec.uid("custom")
    opamp.expect(spec.first_report(uid, sequence_num=1))

    spec_custom = spec.unsupported_optional_message(uid, sequence_num=2)
    reply = opamp.expect(spec_custom)

    assert not reply.HasField("error_response")
    agent = opamp.agent(uid.hex())
    assert agent["custom_capabilities"] == ["conformance.example"]


# --- §Agent Disconnect ------------------------------------------------------


def test_a_disconnect_removes_the_agent_and_asks_for_nothing(opamp):
    """§Agent Disconnect: the agent is leaving; the server neither keeps nor asks.

    A `ReportFullState` or component ask on the reply to a disconnect is a request sent
    to nobody, so the flags must be clear.

    Detail: `tests/test_sequence.py`, `tests/test_ws.py`.
    """
    uid = spec.uid("disconnect")
    opamp.expect(spec.first_report(uid, sequence_num=1))

    reply = opamp.expect(spec.disconnect(uid, sequence_num=2))

    assert reply.flags == 0
    assert opamp.get(f"/agent/{uid.hex()}").status_code == 404


# --- §ServerToAgent: capabilities -------------------------------------------


def test_the_reply_advertises_only_classified_capabilities(opamp):
    """§ServerToAgent: `capabilities` is a promise, so nothing unclassified is sent.

    The server advertises exactly what it implements (plus `OffersRemoteConfig` only
    while push is allowed), and every bit is in the classified set — a proto bump that
    adds a bit without a decision fails here.

    Detail: `tests/test_capabilities.py`.
    """
    reply = opamp.expect(spec.status_only(spec.uid("capabilities"), sequence_num=1))
    advertised = reply.capabilities

    assert advertised == remote_config.server_capabilities()
    assert advertised & ~sum(remote_config.CLASSIFIED_CAPABILITIES) == 0
    # Push is off in this fixture, so the offer bit must not be promised.
    assert not advertised & opamp_pb2.ServerCapabilities.ServerCapabilities_OffersRemoteConfig


# --- forward compatibility --------------------------------------------------


def test_a_legal_message_with_unimplemented_shapes_does_not_break_the_reply(opamp):
    """§Ensuring Interoperability: a legal message is never rejected for being new.

    `ConnectionSettingsRequest` (#83) is not implemented here yet, and package statuses
    (#82) will never be offered — but a `PackageStatuses` report stays legal input. The
    server must still accept the message, store what it understands (package statuses,
    custom capabilities) and reply normally — a client must never be punished for using
    a part of the spec this server has not got to, or has declined to implement.

    Detail: `tests/test_proto_sync.py` (the wire schema itself).
    """
    uid = spec.uid("forward-compat")
    reply = opamp.expect(spec.unsupported_optional_message(uid, sequence_num=1))

    assert not reply.HasField("error_response")
    # The package status is stored even though no package was ever offered.
    agent = opamp.agent(uid.hex())
    assert agent["package_statuses"]["packages"]["collector"]["status"] == (
        "PackageStatusEnum_Installing"
    )


def test_a_remote_config_status_is_recorded_with_its_hash(opamp):
    """§RemoteConfigStatus: the agent's word on the config it was given is kept.

    Reconciliation compares hashes, so the echoed hash is stored (hex) next to the
    status — the server cannot answer "is this agent running my config?" otherwise.

    Detail: `tests/test_push_delivery.py`, `tests/test_remote_config.py`.
    """
    uid = spec.uid("remote-config-status")
    opamp.expect(spec.first_report(uid, sequence_num=1))
    reported_hash = b"\x07" * 32

    opamp.expect(
        spec.remote_config_status(uid, status="APPLIED", config_hash=reported_hash, sequence_num=2)
    )

    agent = opamp.agent(uid.hex())
    # `GET /agent/{id}` exposes the wire enum name (`MessageToDict`), so compare against
    # the proto's own spelling rather than a copy of it (#81).
    applied = opamp_pb2.RemoteConfigStatuses.Name(
        opamp_pb2.RemoteConfigStatuses.RemoteConfigStatuses_APPLIED
    )
    assert agent["remote_config_status"] == applied
    assert agent["last_remote_config_hash"] == reported_hash.hex()


# --- helpers ----------------------------------------------------------------


def opamp_bad_request(response) -> opamp_pb2.ServerToAgent:
    """Parse a refusal body — the only shape the spec defines for an error."""
    parsed = opamp_pb2.ServerToAgent()
    parsed.ParseFromString(response.content)
    return parsed
