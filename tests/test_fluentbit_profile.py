"""Fluent Bit end to end: the non-OTel profile that proves the abstraction (#123).

Every payload in this file was **captured from the real thing**: `opamp-observe/opamp-core`
0.6.0 (`consumer`, Python) supervising Fluent Bit 5.1.5 against this server. The spike and
its findings are recorded on #123; the point of replaying the captured bytes here is that
CI proves the abstraction with no product installed:

* identity resolves from `service.type = "Fluent Bit"` (not from a collector-style
  `service.name` reverse FQDN), and `service.name = "Fluentbit"` also matches;
* the config is **INI**, reported under the key `./fluent-bit.conf` with
  `content_type: text/plain` — a body PyYAML refuses, judged by the profile's own engine;
* the component inventory comes from that config, because `opamp-core` does not report
  `AvailableComponents` at all;
* a proposal is *structure-checked* (`structure_checked: True`), so approving it needs no
  acknowledgement, and the round trip ends at `APPLIED`.

The second class is the other half of the proof: an agent with **no** product knowledge
(`generic`), whose opaque body round-trips with nothing but the format layer and the
approver's acknowledgement.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from proto import anyvalue_pb2, opamp_pb2
from server import admin_auth, agent_auth, crypto, remote_config
from server.main import _handle_agent_message
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from tests.support import ADMIN, admin_headers

AGENT_ID = "01a1cb0f37fd70aab6dd374c3a7061ee"

#: The real `AgentDescription`, verbatim (captured 2026-11-12 from opamp-core 0.6.0).
DESCRIPTION = {
    "identifyingAttributes": [
        {"key": "service.name", "value": {"stringValue": "Fluentbit"}},
        {"key": "service.type", "value": {"stringValue": "Fluent Bit"}},
        {"key": "service.version", "value": {"stringValue": "Fluent Bit - 5.1.5 (Community)"}},
    ],
    "nonIdentifyingAttributes": [
        {"key": "os_type", "value": {"stringValue": "Darwin"}},
        {"key": "hostname", "value": {"stringValue": "spike.local"}},
        {"key": "mac_address", "value": {"stringValue": "52:d8:6d:36:83:9c"}},
    ],
}

#: What `opamp-core` advertises when `AcceptsRemoteConfig` and the reporting extras are
#: configured: 11271 = ReportsStatus|AcceptsRemoteConfig|ReportsEffectiveConfig|
#: AcceptsRestart|ReportsHealth|ReportsHeartbeat. Its *defaults* omit the first two.
CAPABILITIES = 11271

CONFIG_KEY = "./fluent-bit.conf"
CONFIG_TYPE = "text/plain"
CONFIG_BODY = """\
[SERVICE]
    Health_Check On
    Flush    2
    Daemon   off
    Log_Level info
    HTTP_Server On
    HTTP_Listen 0.0.0.0
    HTTP_Port 2021

[INPUT]
    Name  dummy
    Tag   dummy

[OUTPUT]
    Name  stdout
    Match *
"""


def _description(message) -> None:
    for source, target in (
        (DESCRIPTION["identifyingAttributes"], message.agent_description.identifying_attributes),
        (DESCRIPTION["nonIdentifyingAttributes"], message.agent_description.non_identifying_attributes),
    ):
        for entry in source:
            attribute = target.add()
            attribute.key = entry["key"]
            attribute.value.CopyFrom(anyvalue_pb2.AnyValue(string_value=entry["value"]["stringValue"]))


def _report(agent_id=AGENT_ID, body=CONFIG_BODY, status=None, hash_b64=None, healthy=True):
    """One `AgentToServer` in the shape opamp-core actually sends."""
    message = opamp_pb2.AgentToServer()
    message.instance_uid = bytes.fromhex(agent_id)
    message.capabilities = CAPABILITIES
    _description(message)
    message.health.healthy = healthy
    message.health.status = "heartbeat"

    entry = message.effective_config.config_map.config_map[CONFIG_KEY]
    entry.body = body.encode()
    entry.content_type = CONFIG_TYPE

    if status:
        message.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
        )
        if hash_b64:
            message.remote_config_status.last_remote_config_hash = base64.b64decode(hash_b64)
    return message


def _reply_for(message) -> opamp_pb2.ServerToAgent:
    """One `AgentToServer` through the real handler, and the reply it produced."""
    processed = _handle_agent_message(message.SerializeToString())
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(processed.response)
    return reply


def poll(**kwargs):
    """Deliver one report through the real handler and return the server's reply."""
    return _reply_for(_report(**kwargs))


@pytest.fixture(autouse=True)
def push_server(monkeypatch, tmp_path):
    """A pushing server with an admin credential, and a clean registry per test."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    # The local-development override: this suite is about the push pipeline, not the
    # posture (which has its own tests), and an insecure server is the cheapest way to
    # have every requirement met without a TLS proxy.
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "off")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.setenv("CONFIG_PUSH_HEALTH_GATE_SECONDS", "0")
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    crypto.reset_key_cache()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    crypto.reset_key_cache()


@pytest.fixture()
def client():
    return TestClient(__import__("server.main", fromlist=["app"]).app)


# ------------------------------------------------------------- identity, dispatch


class TestTheRealAgentIsRecognisedAsFluentBit:
    def test_identity_is_declared_from_service_type(self):
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.profile.name == "fluentbit"
        info = agent.type_info()
        assert info["agent_type"] == "fluentbit"
        assert info["agent_type_source"] == "declared"

    def test_the_component_inventory_comes_from_the_config(self):
        """`opamp-core` sends no `AvailableComponents`; the document is the evidence."""
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.components_status == "reported"
        assert agent.components == {
            "input": [{"id": "dummy", "version": "", "used": True}],
            "output": [{"id": "stdout", "version": "", "used": True}],
        }

    def test_the_reports_that_do_not_apply_say_so(self):
        poll()
        payloads = [agent.to_dict() for agent in AGENT_REGISTRY.list_all()]

        from server import reports

        heavy = reports.generate_heavy_collectors_report({"agents": payloads}, 0.5)
        assert "Not applicable to 1 agent(s) on profiles: fluentbit (1)." in heavy


# --------------------------------------------------------------- validation (#119)


class TestTheProfileJudgesItsOwnDialect:
    """An INI body is not a broken collector config: it is this product's config."""

    def test_a_valid_config_passes_and_is_structure_checked(self, client):
        poll()

        verdict = client.post(
            "/config/validate",
            json={
                "config_map": {CONFIG_KEY: {"body": CONFIG_BODY, "content_type": CONFIG_TYPE}},
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        ).json()

        assert verdict["valid"] is True, verdict
        assert verdict["profile"] == "fluentbit"
        assert verdict["structure_checked"] is True
        # Not one word about receivers, pipelines or the collector's rules.
        assert "receivers" not in json.dumps(verdict).lower()

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("[INPUT]\n    Name dummy\n", "no `[SERVICE]` section"),
            ("[SERVICE]\n    Flush 1\n[INPUT]\n    Name dummy\n", "no `[OUTPUT]` section"),
            ("[SERVICE]\n    Flush 1\n[OUTPUT]\n    Name stdout\n", "no `[INPUT]` section"),
            (
                "[SERVICE]\n    Flush 1\n[INPUT]\n    Tag x\n[OUTPUT]\n    Name stdout\n",
                "no `Name`",
            ),
        ],
    )
    def test_a_config_the_daemon_would_refuse_is_an_error(self, client, body, expected):
        poll()

        verdict = client.post(
            "/config/validate",
            json={
                "config_map": {CONFIG_KEY: {"body": body, "content_type": CONFIG_TYPE}},
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        ).json()

        assert verdict["valid"] is False
        assert expected in json.dumps(verdict)

    def test_a_yaml_body_is_judged_as_a_fluentbit_document_too(self):
        """`fluent-bit.yaml` is a real file format; the engine does not insist on INI."""
        from server.config_validation import check_config_map

        verdict = check_config_map(
            {CONFIG_KEY: {"body": "service: {}\n", "content_type": "text/yaml"}},
            profile="fluentbit",
            run_policy=False,
        )

        # YAML parses, so the INI rules decline the document rather than inventing
        # findings about a body they do not understand; the approver is told nothing ran.
        assert verdict.structure_checked is False
        assert not verdict.errors


# ------------------------------------------------------- propose → approve → apply


def _propose(client, body=CONFIG_BODY):
    response = client.post(
        "/config/revisions",
        json={
            "config_map": {CONFIG_KEY: {"body": body, "content_type": CONFIG_TYPE}},
            "scope": {"agent_id": AGENT_ID},
            "note": "add a record_modifier filter",
        },
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_push_round_trip_ends_at_applied(client):
    """The acceptance criterion of #123, with the agent's own key and its own dialect."""
    poll()
    proposed = _propose(client)
    assert proposed["verdict"]["structure_checked"] is True

    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposed["proposal_id"]},
        json={"canary_percent": 100, "health_gate_seconds": 0},
        headers=admin_headers(),
    )
    assert created.status_code == 200, created.text

    # The agent's next poll collects the config, and it echoes the spec hash back.
    reply = poll()
    assert reply.HasField("remote_config")
    offered = reply.remote_config
    assert list(offered.config.config_map) == [CONFIG_KEY]
    assert offered.config.config_map[CONFIG_KEY].content_type == CONFIG_TYPE
    assert offered.config.config_map[CONFIG_KEY].body.decode() == CONFIG_BODY
    wire_hash = base64.b64encode(offered.config_hash).decode()

    poll(status="APPLIED", hash_b64=wire_hash)
    agent = AGENT_REGISTRY.get(AGENT_ID)
    assert agent.remote_config_status == "RemoteConfigStatuses_APPLIED"
    # The revision is now the assignment, which is what makes drift detectable.
    assert remote_config.REGISTRY.get(AGENT_ID)["config_hash"]


def test_a_pushed_config_the_profile_rejects_never_reaches_the_agent(client):
    poll()

    response = client.post(
        "/config/revisions",
        json={
            "config_map": {CONFIG_KEY: {"body": "[INPUT]\n    Name dummy\n", "content_type": CONFIG_TYPE}},
            "scope": {"agent_id": AGENT_ID},
        },
        headers=admin_headers(),
    )

    assert response.status_code == 422
    assert "no `[SERVICE]` section" in json.dumps(response.json())
    assert remote_config.REGISTRY.get(AGENT_ID) is None


# ------------------------------------------- the synthetic agent with no product


class TestAnOpaqueAgentRoundTripsWithNoProductKnowledge:
    """#123 deliverable 3: nothing but the format layer and a human's acknowledgement.

    No profile is invented for this agent (`generic`), which is the point: the chain has
    to work for a product the dictionary has never heard of.
    """

    OPAQUE = "routing:\n  - match: *\n    sink: /var/log\n"

    def _register_opaque(self, agent_id="bc" * 16):
        message = opamp_pb2.AgentToServer()
        message.instance_uid = bytes.fromhex(agent_id)
        message.capabilities = (
            opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
            | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        )
        entry = message.effective_config.config_map.config_map["routing.toml"]
        entry.body = self.OPAQUE.encode()
        entry.content_type = "text/plain"
        _reply_for(message)
        return agent_id

    def test_it_registers_lists_and_says_its_config_is_opaque(self, client):
        agent_id = self._register_opaque()

        listing = client.get("/agents").json()
        row = next(agent for agent in listing["agents"] if agent["id"] == agent_id)
        assert row["agent_type"] == "generic"
        assert row["components_status"] == "unsupported"
        assert row["capabilities"] & 0x2  # it does accept remote config

    def test_a_push_needs_the_acknowledgement_and_then_lands_verbatim(self, client):
        agent_id = self._register_opaque()
        proposed = client.post(
            "/config/revisions",
            json={
                "config_map": {
                    "routing.toml": {"body": self.OPAQUE, "content_type": "text/plain"}
                },
                "scope": {"agent_id": agent_id},
            },
            headers=admin_headers(),
        )
        assert proposed.status_code == 200, proposed.text
        verdict = proposed.json()["verdict"]
        assert verdict["structure_checked"] is False

        refused = client.post(
            f"/config/revisions/{proposed.json()['proposal_id']}/approve",
            json={},
            headers=admin_headers(),
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["unchecked_agents"] == [agent_id]

        approved = client.post(
            f"/config/revisions/{proposed.json()['proposal_id']}/approve",
            json={"confirm_unchecked_structure": True},
            headers=admin_headers(),
        )
        assert approved.status_code == 200, approved.text

        # The agent collects the body exactly as it was approved: no re-encoding, no
        # re-formatting, no invented structure.
        # A real follow-up poll: the offer rides the next status report, so this has to
        # look like one (capabilities + a sequence number), not a bare uid.
        message = opamp_pb2.AgentToServer()
        message.instance_uid = bytes.fromhex(agent_id)
        message.sequence_num = 2
        message.capabilities = (
            opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
            | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        )
        reply = _reply_for(message)
        assert reply.HasField("remote_config")
        offered = reply.remote_config.config.config_map["routing.toml"]
        assert offered.body.decode() == self.OPAQUE
        assert offered.content_type == "text/plain"
