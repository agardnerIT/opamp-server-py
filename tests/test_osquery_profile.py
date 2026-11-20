"""osquery end to end: the JSON profile the dictionary was missing (#141).

The payload is the one recorded on #141 (osqueryd 5.24.0 through
`agardnerit/osquery-opamp` 0.1.0), with the extension's own `osquery.*` attributes and
its config object key. Replaying it here proves, with no osquery installed:

* identity resolves from `service.name = osqueryd` **and** from the `osquery.*`
  attribute namespace alone — no other product reports another product's namespace;
* the config is `application/json` under the object key `osquery.json`, the name the
  extension applies and refuses to substitute;
* the inventory comes from the document (`schedule` queries and `packs`), because the
  extension reports no `AvailableComponents`;
* the profile's own engine judges the JSON document, so a schedule with no SQL is an
  error and no collector vocabulary appears anywhere in the verdict;
* the propose → approve → apply round trip ends at `APPLIED` for a JSON body.
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

AGENT_ID = "4e89c0e64f974e0fb7b681699b7f82ad"

#: The `AgentDescription` from #141, verbatim in shape: the service identity plus the
#: extension's own attribute namespace.
DESCRIPTION = {
    "identifyingAttributes": [
        {"key": "service.name", "value": {"stringValue": "osqueryd"}},
        {"key": "service.version", "value": {"stringValue": "5.24.0"}},
        {"key": "service.instance.id", "value": {"stringValue": "2662e975c6d0445fb7838ab4cc8f1f83"}},
        {"key": "host.name", "value": {"stringValue": "spike.local"}},
        {"key": "os.type", "value": {"stringValue": "darwin"}},
    ],
    "nonIdentifyingAttributes": [
        {"key": "osquery.config_plugin", "value": {"stringValue": "opamp"}},
        {"key": "osquery.opamp_extension_version", "value": {"stringValue": "0.1.0"}},
        {"key": "osquery.config_valid", "value": {"boolValue": True}},
        {"key": "osquery.pid", "value": {"intValue": 3510}},
    ],
}

#: ReportsStatus | AcceptsRemoteConfig | ReportsEffectiveConfig | ReportsHealth |
#: ReportsHeartbeat — the extension opts into config push and reports what it runs.
CAPABILITIES = 1 | 2 | 4 | 0x800 | 0x2000

CONFIG_KEY = "osquery.json"
CONFIG_TYPE = "application/json"
CONFIG_BODY = json.dumps(
    {
        "options": {"config_refresh": 10},
        "schedule": {
            "battery_stats": {
                "query": "SELECT percent_remaining FROM battery;",
                "interval": 32,
            }
        },
        "packs": {
            "baseline": {
                "queries": {
                    "users": {"query": "SELECT * FROM users;", "interval": 3600}
                }
            }
        },
    },
    indent=2,
)


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
    body=CONFIG_BODY,
    content_type=CONFIG_TYPE,
    description=None,
    status=None,
    hash_b64=None,
    healthy=True,
):
    """One `AgentToServer` in the shape the osquery extension actually sends."""
    message = opamp_pb2.AgentToServer()
    message.instance_uid = bytes.fromhex(agent_id)
    message.capabilities = CAPABILITIES
    _add_attributes(message, description or DESCRIPTION)
    message.health.healthy = healthy
    message.health.status = "heartbeat"

    entry = message.effective_config.config_map.config_map[CONFIG_KEY]
    entry.body = body.encode()
    entry.content_type = content_type

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


class TestTheRealAgentIsRecognisedAsOsquery:
    def test_identity_is_declared_from_service_name(self):
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.profile.name == "osquery"
        info = agent.type_info()
        assert info["agent_type"] == "osquery"
        assert info["agent_type_source"] == "declared"
        # The profile interprets the body, so the "we cannot read this" warning is gone.
        assert info["agent_type_warning"] is None

    def test_the_osquery_attribute_namespace_alone_is_enough(self):
        """The issue's argument: no other agent reports attributes prefixed `osquery.`."""
        description = {
            "identifyingAttributes": [],
            "nonIdentifyingAttributes": [
                {"key": "osquery.watcher_pid", "value": {"stringValue": "3472"}},
                {"key": "service.name", "value": {"stringValue": "renamed-by-an-operator"}},
            ],
        }
        poll(description=description)

        info = AGENT_REGISTRY.get(AGENT_ID).type_info()
        assert info["agent_type"] == "osquery"
        assert info["agent_type_source"] == "declared"

    def test_a_collector_is_not_mistaken_for_osquery(self):
        """`service.name` is not the osquery namespace, so the collector keeps its rules."""
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

    def test_the_component_inventory_comes_from_the_config(self):
        """The extension reports no `AvailableComponents`; the document is the evidence."""
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.components_status == "reported"
        assert agent.components == {
            "query": [{"id": "battery_stats", "version": "", "used": True}],
            "pack": [{"id": "baseline", "version": "", "used": True}],
        }

    def test_the_default_filename_is_the_object_the_extension_applies(self):
        from server import profiles

        assert profiles.get("osquery").filename_for(CONFIG_TYPE) == CONFIG_KEY

    def test_the_reports_that_do_not_apply_say_so(self):
        poll()
        payloads = [agent.to_dict() for agent in AGENT_REGISTRY.list_all()]

        from server import reports

        heavy = reports.generate_heavy_collectors_report({"agents": payloads}, 0.5)
        assert "Not applicable to 1 agent(s) on profiles: osquery (1)." in heavy


# --------------------------------------------------------------- validation (#119)


class TestTheProfileJudgesItsOwnDialect:
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
        assert verdict["profile"] == "osquery"
        assert verdict["structure_checked"] is True
        # Not one word about receivers, pipelines or the collector's rules.
        assert "receivers" not in json.dumps(verdict).lower()

    @pytest.mark.parametrize(
        "body,expected",
        [
            (json.dumps({"schedule": []}), "`schedule` must be an object"),
            (
                json.dumps({"schedule": {"battery_stats": {"interval": 60}}}),
                "has no `query` SQL",
            ),
            (
                json.dumps(
                    {"schedule": {"battery_stats": {"query": "SELECT 1;", "interval": "60"}}}
                ),
                "not a positive number of seconds",
            ),
            (json.dumps({"packs": []}), "`packs` must be an object"),
            (json.dumps(["SELECT 1;"]), "is a JSON object, not list"),
            (
                json.dumps({"packs": {"baseline": {"queries": []}}}),
                "must be an object of query name -> query",
            ),
        ],
    )
    def test_a_config_osquery_would_refuse_is_an_error(self, client, body, expected):
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

    def test_a_missing_interval_is_advice_not_an_error(self, client):
        """osquery applies `--schedule_default_interval`, so the document is runnable."""
        poll()

        verdict = client.post(
            "/config/validate",
            json={
                "config_map": {
                    CONFIG_KEY: {
                        "body": json.dumps(
                            {"schedule": {"battery_stats": {"query": "SELECT 1;"}}}
                        ),
                        "content_type": CONFIG_TYPE,
                    }
                },
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        ).json()

        assert verdict["valid"] is True, verdict
        assert "sets no `interval`" in json.dumps(verdict)

    def test_a_foreign_body_is_declined_not_judged(self):
        """A collector YAML is not an osquery document: nothing structural ran."""
        from server.config_validation import check_config_map

        verdict = check_config_map(
            {
                CONFIG_KEY: {
                    "body": "receivers:\n  otlp:\n",
                    "content_type": "text/yaml",
                }
            },
            profile="osquery",
            run_policy=False,
        )

        assert verdict.structure_checked is False
        assert not verdict.errors
        assert "not an osquery configuration" in json.dumps(verdict.to_dict())


# ------------------------------------------------------- propose → approve → apply


def _propose(client, body=CONFIG_BODY):
    response = client.post(
        "/config/revisions",
        json={
            "config_map": {CONFIG_KEY: {"body": body, "content_type": CONFIG_TYPE}},
            "scope": {"agent_id": AGENT_ID},
            "note": "adjust the battery query interval",
        },
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_push_round_trip_ends_at_applied(client):
    """The whole chain, with the key and dialect the extension actually uses."""
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
    assert list(offered.config.config_map) == [CONFIG_KEY]
    assert offered.config.config_map[CONFIG_KEY].content_type == CONFIG_TYPE
    assert offered.config.config_map[CONFIG_KEY].body.decode() == CONFIG_BODY
    wire_hash = base64.b64encode(offered.config_hash).decode()

    poll(status="APPLIED", hash_b64=wire_hash)
    agent = AGENT_REGISTRY.get(AGENT_ID)
    assert agent.remote_config_status == "RemoteConfigStatuses_APPLIED"
    assert remote_config.REGISTRY.get(AGENT_ID)["config_hash"]


def test_a_pushed_config_the_profile_rejects_never_reaches_the_agent(client):
    poll()

    response = client.post(
        "/config/revisions",
        json={
            "config_map": {
                CONFIG_KEY: {
                    "body": json.dumps({"schedule": {"battery_stats": {"interval": 32}}}),
                    "content_type": CONFIG_TYPE,
                }
            },
            "scope": {"agent_id": AGENT_ID},
        },
        headers=admin_headers(),
    )

    assert response.status_code == 422
    assert "has no `query` SQL" in json.dumps(response.json())
    assert remote_config.REGISTRY.get(AGENT_ID) is None
