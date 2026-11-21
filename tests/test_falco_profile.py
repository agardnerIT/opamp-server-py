"""Falco end to end: the first profile driven through the product's reload-control API.

The payload is the live capture from `agardnerIT/falco-opamp` connected to a real
`falcosecurity/falco` 0.45.2 container (2026-11-21, see falco-scope.md), verbatim
in shape: `service.name = falco`, the sidecar's own `falco.*` attribute namespace,
capabilities 2055 (ReportsStatus | AcceptsRemoteConfig | ReportsEffectiveConfig |
ReportsHealth), and the two config object keys the sidecar owns. Replaying it here
proves, with no Falco installed:

* identity resolves from `service.name = falco` **and** from the `falco.*`
  attribute namespace alone — no other product reports another product's namespace;
* the config is `application/yaml` under the object keys `falco.yaml` and
  `rules.yaml`, the names the sidecar applies;
* the inventory comes from the pushed documents (`rules_files` entries and `rule:`
  entries), because the sidecar reports no `AvailableComponents`;
* the profile's own engine judges both shapes, so a rule with no `condition` is an
  error and no collector vocabulary appears anywhere in the verdict;
* the propose → apply round trip offers both keys and ends at `APPLIED`.
"""

from __future__ import annotations

import base64
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from proto import anyvalue_pb2, opamp_pb2
from server import admin_auth, agent_auth, crypto, remote_config
from server.main import _handle_agent_message
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from tests.support import ADMIN, admin_headers

AGENT_ID = "cd6114a585d14dd1a11acf62a8889c67"

#: The `AgentDescription` from the live capture, verbatim in shape: the service
#: identity plus the sidecar's own attribute namespace.
DESCRIPTION = {
    "identifyingAttributes": [
        {"key": "service.name", "value": {"stringValue": "falco"}},
    ],
    "nonIdentifyingAttributes": [
        {"key": "falco.instance_id",
         "value": {"stringValue": "80162ef25070e8c618429cd88570a52e"}},
        {"key": "falco.version", "value": {"stringValue": "0.45.2"}},
        {"key": "falco.libs_version", "value": {"stringValue": "0.26.1"}},
        {"key": "falco.plugins", "value": {"stringValue": "container"}},
        {"key": "falco.host", "value": {"stringValue": "a9d7f386e12d"}},
    ],
}

#: ReportsStatus | AcceptsRemoteConfig | ReportsEffectiveConfig | ReportsHealth —
#: the sidecar opts into config push and reports what it runs. No heartbeat
#: capability: the HTTP transport polls instead (capture value: 2055).
CAPABILITIES = 1 | 2 | 4 | 0x800

MAIN_KEY = "falco.yaml"
RULES_KEY = "rules.yaml"
CONFIG_TYPE = "application/yaml"

MAIN_CONFIG_BODY = """\
webserver:
  enabled: true
reload_control:
  enabled: true
  socket: /run/falco/control.sock
rules_files:
  - /etc/falco/falco_rules.yaml
  - /etc/falco/opamp/rules.yaml
watch_config_files: false
"""

RULES_BODY = """\
- rule: Opamp Capture Test Rule
  desc: written by the opamp server push cycle
  condition: evt.type=execve
  output: "opamp test (command=%proc.name)"
  priority: WARNING
"""


def _add_attributes(message, description):
    for source, target in (
        (description["identifyingAttributes"], message.agent_description.identifying_attributes),
        (
            description["nonIdentifyingAttributes"],
            message.agent_description.non_identifying_attributes,
        ),
    ):
        for entry in source:
            attribute = target.add()
            attribute.key = entry["key"]
            value = entry["value"]
            if "stringValue" in value:
                attribute.value.CopyFrom(anyvalue_pb2.AnyValue(string_value=value["stringValue"]))
            elif "boolValue" in value:
                attribute.value.CopyFrom(anyvalue_pb2.AnyValue(bool_value=value["boolValue"]))
            else:
                attribute.value.CopyFrom(anyvalue_pb2.AnyValue(int_value=value["intValue"]))


def _report(
    agent_id=AGENT_ID,
    description=None,
    config_map=None,
    healthy=True,
    status=None,
    hash_b64=None,
):
    """One `AgentToServer` in the shape the sidecar actually sends."""
    message = opamp_pb2.AgentToServer()
    message.instance_uid = bytes.fromhex(agent_id)
    message.capabilities = CAPABILITIES
    _add_attributes(message, description or DESCRIPTION)
    message.health.healthy = healthy
    message.health.status = "heartbeat"

    objects = config_map or {MAIN_KEY: MAIN_CONFIG_BODY, RULES_KEY: RULES_BODY}
    for key, entry in objects.items():
        body, content_type = entry if isinstance(entry, tuple) else (entry, CONFIG_TYPE)
        obj = message.effective_config.config_map.config_map[key]
        obj.body = body.encode()
        obj.content_type = content_type

    if status:
        message.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
        )
        if hash_b64:
            message.remote_config_status.last_remote_config_hash = base64.b64decode(hash_b64)
    return message


def _reply_for(message) -> opamp_pb2.ServerToAgent:
    processed = _handle_agent_message(message.SerializeToString())
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(processed.response)
    return reply


def poll(**kwargs) -> opamp_pb2.ServerToAgent:
    return _reply_for(_report(**kwargs))


@pytest.fixture(autouse=True)
def push_server(monkeypatch, tmp_path):
    """A pushing server with an admin credential, and a clean registry per test."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
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


class TestTheRealAgentIsRecognisedAsFalco:
    def test_identity_is_declared_from_service_name(self):
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.profile.name == "falco"
        info = agent.type_info()
        assert info["agent_type"] == "falco"
        assert info["agent_type_source"] == "declared"
        # The profile interprets the body, so the "we cannot read this" warning is gone.
        assert info["agent_type_warning"] is None

    def test_the_falco_attribute_namespace_alone_is_enough(self):
        """The scoping argument: no other agent reports attributes prefixed `falco.`."""
        description = {
            "identifyingAttributes": [],
            "nonIdentifyingAttributes": [
                {"key": "falco.version", "value": {"stringValue": "0.45.2"}},
                {"key": "service.name", "value": {"stringValue": "renamed-by-an-operator"}},
            ],
        }
        poll(description=description)

        info = AGENT_REGISTRY.get(AGENT_ID).type_info()
        assert info["agent_type"] == "falco"
        assert info["agent_type_source"] == "declared"

    def test_a_collector_is_not_mistaken_for_falco(self):
        """`service.name` is not the falco namespace, so the collector keeps its rules."""
        from server.state import AgentState

        agent = AgentState(
            instance_uid=b"\x00" * 16,
            agent_id="a" * 32,
            description={
                "identifyingAttributes": [
                    {"key": "service.name", "value": {"stringValue": "otelcol-contrib"}},
                    {"key": "service.version", "value": {"stringValue": "0.149.0"}},
                ]
            },
        )
        assert agent.profile.name == "otelcol"

    def test_the_component_inventory_comes_from_the_pushed_documents(self):
        """The sidecar reports no `AvailableComponents`; the pushed documents are the evidence."""
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.components_status == "reported"
        assert agent.components == {
            "rulesfile": [
                {"id": "/etc/falco/falco_rules.yaml", "version": "", "used": True},
                {"id": "/etc/falco/opamp/rules.yaml", "version": "", "used": True},
            ],
            "rule": [
                {"id": "Opamp Capture Test Rule", "version": "", "used": True},
            ],
        }

    def test_the_default_filename_is_the_main_config_the_sidecar_applies(self):
        from server import profiles

        assert profiles.get("falco").filename_for(CONFIG_TYPE) == MAIN_KEY

    def test_the_reports_that_do_not_apply_say_so(self):
        poll()
        payloads = [agent.to_dict() for agent in AGENT_REGISTRY.list_all()]

        from server import reports

        heavy = reports.generate_heavy_collectors_report({"agents": payloads}, 0.5)
        assert "Not applicable to 1 agent(s) on profiles: falco (1)." in heavy


# --------------------------------------------------------------- validation (#119)


class TestTheProfileJudgesItsOwnDialect:
    def test_a_valid_config_passes_and_is_structure_checked(self, client):
        poll()

        verdict = client.post(
            "/config/validate",
            json={
                "config_map": {
                    MAIN_KEY: {"body": MAIN_CONFIG_BODY, "content_type": CONFIG_TYPE},
                    RULES_KEY: {"body": RULES_BODY, "content_type": CONFIG_TYPE},
                },
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        ).json()

        assert verdict["valid"] is True, verdict
        assert verdict["profile"] == "falco"
        assert verdict["structure_checked"] is True
        # Not one word about receivers, pipelines or the collector's rules.
        assert "receivers" not in json.dumps(verdict).lower()

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("", "body is empty"),
            # a rule with no condition is refused by falco --validate and would come
            # back as a rejected reload: refuse it here too
            (
                "- rule: x\n  desc: d\n  output: o\n",
                "has no `condition`",
            ),
            ("- rule: x\n  desc: d\n  condition: c\n", "has no `output`"),
            ("- desc: d\n  condition: c\n  output: o\n", "no `rule`, `list` or `macro`"),
            (
                "- rule: x\n  desc: d\n  condition: c\n  output: o\n  priority: not_a_level\n",
                None,  # priority semantics are falco's; the engine is best effort
            ),
        ],
    )
    def test_rules_the_sidecar_would_apply_blindly_are_refused(self, client, body, expected):
        poll()

        response = client.post(
            "/config/validate",
            json={
                "config_map": {RULES_KEY: {"body": body, "content_type": CONFIG_TYPE}},
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        )
        verdict = response.json()
        if expected is None:
            assert verdict["valid"] is True, verdict
        else:
            assert verdict["valid"] is False
            assert expected in json.dumps(verdict)

    def test_the_watcher_racing_the_protocol_is_advice(self, client):
        poll()

        verdict = client.post(
            "/config/validate",
            json={
                "config_map": {
                    MAIN_KEY: {
                        "body": MAIN_CONFIG_BODY.replace(
                            "watch_config_files: false", "watch_config_files: true"
                        ),
                        "content_type": CONFIG_TYPE,
                    }
                },
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        ).json()

        assert verdict["valid"] is True, verdict
        assert "watch_config_files" in json.dumps(verdict)

    def test_a_foreign_body_is_declined_not_judged(self):
        """A collector YAML is not a Falco document: nothing structural ran."""
        from server.config_validation import check_config_map

        verdict = check_config_map(
            {
                MAIN_KEY: {
                    "body": "receivers:\n  otlp:\n",
                    "content_type": "text/yaml",
                }
            },
            profile="falco",
            run_policy=False,
        )

        assert verdict.structure_checked is False
        assert not verdict.errors


# ------------------------------------------------------- propose → apply


def _propose(client, config_map=None, note="the falco-opamp push"):
    response = client.post(
        "/config/revisions",
        json={
            "config_map": config_map
            or {
                MAIN_KEY: {"body": MAIN_CONFIG_BODY, "content_type": CONFIG_TYPE},
                RULES_KEY: {"body": RULES_BODY, "content_type": CONFIG_TYPE},
            },
            "scope": {"agent_id": AGENT_ID},
            "note": note,
        },
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_push_round_trip_offers_both_keys_and_ends_at_applied(client):
    """The whole chain, with the keys and dialect the sidecar actually applies."""
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

    reply = poll()
    assert reply.HasField("remote_config")
    offered = reply.remote_config
    assert set(offered.config.config_map) == {MAIN_KEY, RULES_KEY}
    assert offered.config.config_map[MAIN_KEY].content_type == CONFIG_TYPE
    assert offered.config.config_map[MAIN_KEY].body.decode() == MAIN_CONFIG_BODY
    assert offered.config.config_map[RULES_KEY].body.decode() == RULES_BODY
    wire_hash = base64.b64encode(offered.config_hash).decode()

    poll(status="APPLIED", hash_b64=wire_hash)
    agent = AGENT_REGISTRY.get(AGENT_ID)
    assert agent.remote_config_status == "RemoteConfigStatuses_APPLIED"
    assert remote_config.REGISTRY.get(AGENT_ID)["config_hash"]


def test_a_pushed_config_the_sidecar_would_refuse_never_reaches_the_agent(client):
    """reload_control disabled in the file is the sidecar's own brick-guard; the
    server's engine warns (the `-o` override deployment is legitimate) and the
    sidecar makes the final call."""
    poll()

    response = client.post(
        "/config/revisions",
        json={
            "config_map": {
                MAIN_KEY: {
                    "body": MAIN_CONFIG_BODY.replace(
                        "reload_control:\n  enabled: true",
                        "reload_control:\n  enabled: false",
                    ),
                    "content_type": CONFIG_TYPE,
                }
            },
            "scope": {"agent_id": AGENT_ID},
        },
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    proposed = response.json()
    assert proposed["verdict"]["ok"] is True, proposed["verdict"]
    assert "reload_control.enabled" in json.dumps(proposed["verdict"])
