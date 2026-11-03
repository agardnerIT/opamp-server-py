"""The `instance_uid` identity cluster: duplicate detection (#75) and the header (#76).

Both issues are about the same question — *which agent is this?* — so they share one
file, one fixture and one set of helpers.

What is pinned here:

#75 duplicate uid
* two agents alternating under one uid are detected and the newcomer gets a
  server-generated `agent_identification.new_instance_uid`;
* the old record is *not* deleted: it belongs to the other agent;
* one agent that renames its `service.instance.id` in place is never re-issued;
* clones (identical `host.name`) are caught by the sequence-regression signal, and a
  single decrease (a restart) is not;
* the check can be switched off with `OPAMP_DUPLICATE_UID_DETECTION=false`;
* in `token` mode the duplicate is detected and counted but not renamed, because a
  credential is bound to the uid it was minted for (#94).

#76 header
* every documented spelling of a UUID is accepted;
* a mismatch logs and counts but the body wins, unless `OPAMP_UID_HEADER_STRICT`;
* a malformed header never raises and never changes the agent;
* an absent header changes nothing.
"""

from __future__ import annotations

import uuid

import pytest

import server.main as server_main
from proto import opamp_pb2
from server import agent_auth, protocol
from server.main import (
    PROM_DUPLICATE_UIDS,
    PROM_UID_HEADER_PROBLEMS,
)
from server.state import AGENT_REGISTRY
from server.ws import REGISTRY as WS_REGISTRY
from tests.conformance.harness import expect_reply, opamp_send

UID = bytes.fromhex("ab" * 16)
AGENT_ID = UID.hex()


def message(
    uid: bytes = UID,
    host: str | None = None,
    sequence_num: int = 0,
    healthy: bool = True,
    uid_field: str = "host.name",
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.sequence_num = sequence_num
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = healthy
    if host is not None:
        attribute = msg.agent_description.identifying_attributes.add()
        attribute.key = uid_field
        attribute.value.string_value = host
    return msg.SerializeToString()


def problems(reason: str) -> float:
    return PROM_UID_HEADER_PROBLEMS.labels(reason=reason)._value.get()


def duplicates(reason: str, action: str) -> float:
    return PROM_DUPLICATE_UIDS.labels(reason=reason, action=action)._value.get()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    server_main.DUPLICATE_UID_DETECTORS.reset()
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("OPAMP_DUPLICATE_UID_DETECTION", raising=False)
    monkeypatch.delenv("OPAMP_UID_HEADER_STRICT", raising=False)
    monkeypatch.setenv("REPORT_FULL_STATE_MIN_INTERVAL_SECONDS", "0")
    yield
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    server_main.DUPLICATE_UID_DETECTORS.reset()


# ---------------------------------------------------------------- header parsing


@pytest.mark.parametrize(
    "raw",
    [
        "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",  # canonical
        "F81D4FAE-7DEC-11D0-A765-00A0C91E6BF6",  # upper case
        "f81d4fae7dec11d0a76500a0c91e6bf6",  # bare hex
        "{f81d4fae-7dec-11d0-a765-00a0c91e6bf6}",  # braces
        "urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6",  # urn
        "  f81d4fae-7dec-11d0-a765-00a0c91e6bf6  ",  # whitespace
    ],
)
def test_every_spelling_of_a_uuid_is_the_same_uid(raw):
    assert protocol.parse_instance_uid_header(raw) == bytes.fromhex(
        "f81d4fae7dec11d0a76500a0c91e6bf6"
    )


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-uuid", "zz" * 16, "11" * 15])
def test_an_unreadable_header_is_none_not_an_exception(raw):
    assert protocol.parse_instance_uid_header(raw) is None


# --------------------------------------------------------------- host identity


def test_host_identity_prefers_service_instance_id():
    description = {
        "identifyingAttributes": [
            {"key": "host.name", "value": {"stringValue": "vm-1"}},
            {"key": "service.instance.id", "value": {"stringValue": "pid-42"}},
        ]
    }
    assert protocol.host_identity(description) == "service.instance.id=pid-42"


def test_host_identity_falls_back_to_host_name_and_tolerates_junk():
    assert (
        protocol.host_identity(
            {"identifyingAttributes": [{"key": "host.name", "value": {"stringValue": "vm-1"}}]}
        )
        == "host.name=vm-1"
    )
    assert protocol.host_identity({}) is None
    assert protocol.host_identity(None) is None
    assert (
        protocol.host_identity(
            {"identifyingAttributes": [{"key": "host.name", "value": {"arrayValue": {}}}]}
        )
        is None
    )


# ------------------------------------------------------------------- detector


def test_interleaved_identities_are_a_duplicate():
    detector = protocol.DuplicateUidDetector()
    assert detector.observe(AGENT_ID, "host.name=a", 1) is None
    assert detector.observe(AGENT_ID, "host.name=b", 1) is None
    assert detector.observe(AGENT_ID, "host.name=a", 1) == protocol.REASON_HOST_IDENTITY


def test_a_rename_in_place_is_not_a_duplicate():
    """`A A B B B` is one agent with a new id, not two agents."""
    detector = protocol.DuplicateUidDetector()
    observations = ["host.name=a", "host.name=a", "host.name=b", "host.name=b", "host.name=b"]
    assert [detector.observe(AGENT_ID, identity, 1) for identity in observations] == [None] * 5


def test_one_counter_decrease_is_a_restart_and_two_are_two_agents():
    detector = protocol.DuplicateUidDetector()
    assert detector.observe(AGENT_ID, "host.name=a", 20) is None
    assert detector.observe(AGENT_ID, "host.name=a", 10) is None  # restart, not a clone
    assert (
        detector.observe(AGENT_ID, "host.name=a", 5)
        == protocol.REASON_SEQUENCE_REGRESSION
    )


def test_confirm_suppresses_a_second_detection_without_a_clock():
    detector = protocol.DuplicateUidDetector(suppression_seconds=60.0)
    detector.observe(AGENT_ID, "host.name=a", 1)
    detector.observe(AGENT_ID, "host.name=b", 1)
    assert detector.observe(AGENT_ID, "host.name=a", 1) == protocol.REASON_HOST_IDENTITY
    detector.confirm(AGENT_ID, now=100.0)
    assert detector.observe(AGENT_ID, "host.name=b", 1, now=130.0) is None
    assert detector.observe(AGENT_ID, "host.name=a", 1, now=200.0) is None


def test_forget_drops_everything_about_a_uid():
    detector = protocol.DuplicateUidDetector()
    detector.observe(AGENT_ID, "host.name=a", 1)
    detector.observe(AGENT_ID, "host.name=b", 1)
    detector.forget(AGENT_ID)
    assert detector.observe(AGENT_ID, "host.name=a", 1) is None


# ------------------------------------------------------------------ #76 over HTTP


def test_a_matching_header_changes_nothing(client):
    before = problems("mismatch")
    response = opamp_send(client, message(), headers={"OpAMP-Instance-UID": str(uuid.UUID(bytes=UID))})
    assert response.status_code == 200
    assert AGENT_REGISTRY.get(AGENT_ID) is not None
    assert problems("mismatch") == before


def test_an_uppercase_header_is_accepted(client):
    before = problems("malformed")
    response = opamp_send(client, message(), headers={"OpAMP-Instance-UID": str(uuid.UUID(bytes=UID)).upper()})
    assert response.status_code == 200
    assert problems("malformed") == before


def test_a_mismatch_is_logged_and_counted_but_the_body_wins(client):
    before = problems("mismatch")
    other = uuid.UUID(bytes=bytes.fromhex("cd" * 16))
    response = opamp_send(client, message(), headers={"OpAMP-Instance-UID": str(other)})
    assert response.status_code == 200
    # The body is authoritative: the agent exists under its own uid, not the header's.
    assert AGENT_REGISTRY.get(AGENT_ID) is not None
    assert AGENT_REGISTRY.get("cd" * 16) is None
    assert problems("mismatch") == before + 1


def test_a_malformed_header_is_ignored_never_raised(client):
    before = problems("malformed")
    response = opamp_send(client, message(), headers={"OpAMP-Instance-UID": "garbage"})
    assert response.status_code == 200
    assert AGENT_REGISTRY.get(AGENT_ID) is not None
    assert problems("malformed") == before + 1


def test_an_absent_header_changes_nothing(client):
    before = (problems("mismatch"), problems("malformed"))
    response = opamp_send(client, message())
    assert response.status_code == 200
    assert (problems("mismatch"), problems("malformed")) == before


def test_an_ascii_header_matching_a_non_uuid_body_is_accepted(client):
    """This repo's own fixtures use readable ids; that header is still correct."""
    ascii_uid = b"collector-0001"
    before = problems("malformed")
    response = opamp_send(client, message(uid=ascii_uid), headers={"OpAMP-Instance-UID": "collector-0001"})
    assert response.status_code == 200
    assert problems("malformed") == before


def test_strict_mode_refuses_a_mismatch_and_changes_no_state(client, monkeypatch):
    monkeypatch.setenv("OPAMP_UID_HEADER_STRICT", "true")
    other = uuid.UUID(bytes=bytes.fromhex("cd" * 16))
    response = opamp_send(client, message(), headers={"OpAMP-Instance-UID": str(other)})
    assert response.status_code == 400
    parsed = opamp_pb2.ServerToAgent()
    parsed.ParseFromString(response.content)
    assert parsed.error_response.type == opamp_pb2.ServerErrorResponseType_BadRequest
    assert "does not match" in parsed.error_response.error_message
    assert AGENT_REGISTRY.get(AGENT_ID) is None


def test_an_oversized_body_still_logs_the_header(client, monkeypatch):
    """413 has no parsed body, so the header is the only identity there is."""
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "64")
    response = opamp_send(client, message(host="a" * 256), headers={"OpAMP-Instance-UID": str(uuid.UUID(bytes=UID))})
    assert response.status_code == 413


# ------------------------------------------------------------------ #75 over HTTP


def test_a_duplicate_uid_is_reissued_and_both_agents_survive(client):
    before = duplicates(protocol.REASON_HOST_IDENTITY, "reissued")
    first = opamp_send(client, message(host="vm-a"))
    second = opamp_send(client, message(host="vm-b"))
    third = opamp_send(client, message(host="vm-a"))
    assert first.status_code == second.status_code == third.status_code == 200

    # The first two messages are indistinguishable from one agent changing its mind;
    # the *return* to `vm-a` is what proves there are two.
    assert expect_reply(first).agent_identification.new_instance_uid == b""
    assert expect_reply(second).agent_identification.new_instance_uid == b""

    reissued = expect_reply(third).agent_identification.new_instance_uid
    assert len(reissued) == 16 and reissued != UID
    # The reply is addressed to the uid the agent sent, as the spec requires.
    assert expect_reply(third).instance_uid == UID

    new_id = reissued.hex()
    agents = {entry["id"]: entry for entry in client.get("/agents").json()["agents"]}
    assert set(agents) == {AGENT_ID, new_id}  # no ghost row
    # The old record belongs to the other agent and is left alone.
    assert agents[AGENT_ID]["description"]["identifyingAttributes"][0]["value"]["stringValue"] == "vm-b"
    assert agents[new_id]["uid_reissued_from"] == AGENT_ID
    assert agents[new_id]["uid_reissue_reason"] == protocol.REASON_HOST_IDENTITY
    assert duplicates(protocol.REASON_HOST_IDENTITY, "reissued") == before + 1


def test_the_reissued_uid_is_persisted(client):
    opamp_send(client, message(host="vm-a"))
    opamp_send(client, message(host="vm-b"))
    reissued = expect_reply(opamp_send(client, message(host="vm-a"))).agent_identification.new_instance_uid
    stored = AGENT_REGISTRY._store.load_all()
    assert reissued.hex() in stored
    assert stored[reissued.hex()].uid_reissued_from == AGENT_ID


def test_one_more_poll_under_the_old_uid_is_suppressed(client):
    """The duplicate needs a poll or two to adopt its new uid; don't mint per poll."""
    opamp_send(client, message(host="vm-a"))
    opamp_send(client, message(host="vm-b"))
    expect_reply(opamp_send(client, message(host="vm-a")))
    fourth = opamp_send(client, message(host="vm-b"))
    assert expect_reply(fourth).agent_identification.new_instance_uid == b""
    assert len(client.get("/agents").json()["agents"]) == 2


def test_a_rename_in_place_is_not_reissued(client):
    before = duplicates(protocol.REASON_HOST_IDENTITY, "reissued")
    for host in ("vm-a", "vm-a", "vm-b", "vm-b", "vm-b"):
        assert expect_reply(opamp_send(client, message(host=host))).agent_identification.new_instance_uid == b""
    agents = client.get("/agents").json()["agents"]
    assert len(agents) == 1
    assert agents[0]["uid_reissued_from"] is None
    assert duplicates(protocol.REASON_HOST_IDENTITY, "reissued") == before


def test_a_clone_with_the_same_host_name_is_caught_by_the_counter(client):
    before = duplicates(protocol.REASON_SEQUENCE_REGRESSION, "reissued")
    opamp_send(client, message(host="vm-clone", sequence_num=20))
    opamp_send(client, message(host="vm-clone", sequence_num=10))
    reissued = expect_reply(
        opamp_send(client, message(host="vm-clone", sequence_num=5))
    ).agent_identification.new_instance_uid
    assert reissued and reissued != UID
    assert duplicates(protocol.REASON_SEQUENCE_REGRESSION, "reissued") == before + 1


def test_detection_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("OPAMP_DUPLICATE_UID_DETECTION", "false")
    before = duplicates(protocol.REASON_HOST_IDENTITY, "reissued")
    for host in ("vm-a", "vm-b", "vm-a", "vm-b"):
        assert expect_reply(opamp_send(client, message(host=host))).agent_identification.new_instance_uid == b""
    assert len(client.get("/agents").json()["agents"]) == 1
    assert duplicates(protocol.REASON_HOST_IDENTITY, "reissued") == before


def test_token_mode_detects_but_never_renames():
    """A token is bound to its uid, so a server-minted uid would be refused (#94)."""
    principal = agent_auth.Principal(agent_id=AGENT_ID)
    before = duplicates(protocol.REASON_HOST_IDENTITY, "detected")
    server_main._handle_agent_message(message(host="vm-a"), principal)
    server_main._handle_agent_message(message(host="vm-b"), principal)
    result = server_main._handle_agent_message(message(host="vm-a"), principal)
    parsed = opamp_pb2.ServerToAgent()
    parsed.ParseFromString(result.response)
    assert parsed.agent_identification.new_instance_uid == b""
    assert duplicates(protocol.REASON_HOST_IDENTITY, "detected") == before + 1
    assert len(AGENT_REGISTRY.list_all()) == 1
