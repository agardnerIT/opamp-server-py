"""Grafana Alloy end to end: the supervised-engine profile (#150).

The payload is the one captured on #150 from grafana/alloy v1.22.0 running
`alloy otel-supervisor` (manual mode) against the dev stack — see the capture
checklist answered on the issue. Replaying it here proves, with no Alloy
installed:

* identity resolves from `service.name = alloy` — the value the workbench
  supervisor config sets, because the default presentation resolves to
  `otelcol` (the engine *is* a collector; the profile must not be);
* the config is ONE object, key `""`, `text/yaml`, body = otelcol YAML —
  the supervisor merges what it generates into what it applies;
* no structural checks: a push without an `opamp` extension is VALID, which is
  the capture fact that separates Alloy from the bare collector (the supervisor
  generates that block itself);
* the push round trip ends at `APPLIED` with the object the supervisor writes;
* no components, no metrics, no heavy report — every claim the profile makes
  about absence matches the capture.
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

AGENT_ID = "01a1fbca056c7a10b9bb31568bd653bb"

#: The `AgentDescription` from the #150 capture, verbatim in shape: the identity
#: the supervisor config sets plus the supervisor's own service.instance.id, and
#: the host/os attributes the supervisor adds as non-identifying.
DESCRIPTION = {
    "identifyingAttributes": [
        {"key": "service.instance.id", "value": {"stringValue": "01a1fbca-056c-7a10-b9bb-31568bd653bb"}},
        {"key": "service.name", "value": {"stringValue": "alloy"}},
        {"key": "service.version", "value": {"stringValue": "1.22.0"}},
    ],
    "nonIdentifyingAttributes": [
        {"key": "host.arch", "value": {"stringValue": "arm64"}},
        {"key": "host.name", "value": {"stringValue": "45e16112c6c8"}},
        {"key": "os.description", "value": {"stringValue": "Ubuntu 24.04"}},
        {"key": "os.type", "value": {"stringValue": "linux"}},
    ],
}

#: ReportsStatus | AcceptsRemoteConfig | ReportsRemoteConfig | AcceptsRestartCommand |
#: ReportsHealth | ReportsOwnTraces | AcceptsOpAMPConnectionSettings |
#: AcceptsOtherConnectionSettings | AcceptsCADENCEConnectionSettings |
#: ReportsEffectiveConfig — the bitmask the v1.22.0 supervisor advertises with the
#: workbench capabilities block (`reports_remote_config` is in the *advertised*
#: set even though setting it in the config crashes the binary; see the profile).
CAPABILITIES = 15431

#: One object, key `""`, content type `text/yaml` — exactly what the capture
#: reported. The body is the supervisor's merged view: generated opamp extension
#: plus the engine config.
CONFIG_KEY = ""
CONFIG_TYPE = "text/yaml"
CONFIG_BODY = """receivers:
    otlp:
        protocols:
            http:
                endpoint: 127.0.0.1:4318
exporters:
    debug:
        verbosity: detailed
extensions:
    opamp:
        instance_uid: 01a1fbca-056c-7a10-b9bb-31568bd653bb
service:
    extensions:
        - opamp
    pipelines:
        logs:
            receivers: [otlp]
            exporters: [debug]
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
            attribute.value.CopyFrom(anyvalue_pb2.AnyValue(string_value=value["stringValue"]))


def _report(
    agent_id=AGENT_ID,
    body=CONFIG_BODY,
    content_type=CONFIG_TYPE,
    description=None,
    capabilities=CAPABILITIES,
    status=None,
    hash_b64=None,
    healthy=True,
):
    """One `AgentToServer` in the shape the v1.22.0 supervisor actually sends."""
    message = opamp_pb2.AgentToServer()
    message.instance_uid = bytes.fromhex(agent_id)
    message.capabilities = capabilities
    _add_attributes(message, description or DESCRIPTION)
    message.health.healthy = healthy
    message.health.status = "StatusOK"

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


class TestTheRealAgentIsRecognisedAsAlloy:
    def test_identity_is_declared_from_service_name(self):
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        assert agent.profile.name == "alloy"
        info = agent.type_info()
        assert info["agent_type"] == "alloy"
        assert info["agent_type_source"] == "declared"

    def test_the_reverse_fqdn_form_matches_too(self):
        description = {
            "identifyingAttributes": [
                {"key": "service.name", "value": {"stringValue": "grafana.alloy"}},
            ],
            "nonIdentifyingAttributes": [],
        }
        poll(description=description)

        assert AGENT_REGISTRY.get(AGENT_ID).profile.name == "alloy"

    def test_the_default_presentation_resolves_to_otelcol_not_alloy(self):
        """The negative the issue asked for: `service.name` at the supervisor's
        default presentation (no `agent.description` block set) resolves to the
        collector — the engine underneath IS a collector, so without our config
        change the old classification was accurate."""
        from server.state import AgentState

        agent = AgentState(
            instance_uid=b"\x00" * 16,
            agent_id="a" * 32,
            description={
                "identifyingAttributes": [
                    {"key": "service.name", "value": {"stringValue": "otelcol"}},
                ]
            },
        )
        assert agent.profile.name == "otelcol"

    def test_a_collector_config_sniff_does_not_steal_an_alloy_agent(self):
        """Alloy's effective config IS collector YAML (receivers/exporters), so
        the otelcol profile's sniff matches. Priority must keep the winner."""
        poll()

        info = AGENT_REGISTRY.get(AGENT_ID).type_info()
        assert info["agent_type"] == "alloy"
        # The otelcol sniff does match (the config IS collector YAML), so the
        # server records the ambiguity — but the declared, higher-priority match
        # wins and no warning is raised about it.
        assert info["agent_type_ambiguous"] == ["alloy", "otelcol"]
        # The honest warning stands: the alloy profile does not interpret the
        # body, so collector-specific views do not describe it.
        assert "does not interpret" in info["agent_type_warning"]


# ------------------------------------------------------------- effective config


class TestTheConfigIsTheSupervisorsMergedView:
    def test_one_unnamed_object_as_yaml(self):
        poll()

        agent = AGENT_REGISTRY.get(AGENT_ID)
        config_map = json.loads(agent.effective_config)["configMap"]["configMap"]
        body = config_map[""]
        assert body["contentType"] == "text/yaml"
        assert "pipelines" in base64.b64decode(body["body"]).decode()

    def test_the_default_filename(self):
        from server import profiles

        assert profiles.get("alloy").filename_for(CONFIG_TYPE) == "alloy-config.yaml"


# --------------------------------------------------------------- validation


class TestTheProfileDoesNotJudgeStructure:
    def test_a_push_without_the_opamp_extension_is_valid(self, client):
        """The capture fact that separates Alloy from a bare collector: the
        supervisor generates the `opamp` block itself, so a push must NOT carry
        one. The collector engine's invariant would reject every correct push."""
        poll()

        verdict = client.post(
            "/config/validate",
            json={
                "config_map": {
                    CONFIG_KEY: {
                        "body": "receivers:\n  otlp:\n    protocols:\n      http:\n"
                        "        endpoint: 127.0.0.1:4318\nexporters:\n  debug:\n"
                        "service:\n  pipelines:\n    logs:\n      receivers: [otlp]\n"
                        "      exporters: [debug]\n",
                        "content_type": CONFIG_TYPE,
                    }
                },
                "agent_id": AGENT_ID,
            },
            headers=admin_headers(),
        ).json()

        assert verdict["valid"] is True, verdict
        assert verdict["profile"] == "alloy"
        assert verdict["structure_checked"] is False
        # Not one word about the collector's opamp-extension invariant.
        assert "opamp" not in json.dumps(verdict.get("errors"))

    def test_the_reports_that_do_not_apply_say_so(self):
        poll()
        payloads = [agent.to_dict() for agent in AGENT_REGISTRY.list_all()]

        from server import reports

        heavy = reports.generate_heavy_collectors_report({"agents": payloads}, 0.5)
        assert "Not applicable to 1 agent(s) on profiles: alloy (1)." in heavy

    def test_no_metrics_no_panels(self):
        """Nothing reaches POST /v1/metrics (engine telemetry is level: none);
        the UI says so instead of showing empty tiles."""
        from server import profiles

        profile = profiles.get("alloy")
        assert profile.telemetry is None
        assert profile.panels == ()


# ------------------------------------------------------- propose → approve → apply


def _propose(client, body=CONFIG_BODY):
    response = client.post(
        "/config/revisions",
        json={
            "config_map": {CONFIG_KEY: {"body": body, "content_type": CONFIG_TYPE}},
            "scope": {"agent_id": AGENT_ID},
            "note": "#150 capture: valid push",
        },
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_push_round_trip_ends_at_applied(client):
    """The whole chain, with the unnamed key and content type the capture used."""
    poll()
    proposed = _propose(client)
    assert proposed["verdict"]["ok"] is True

    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposed["proposal_id"]},
        # The guardrail for a profile without structural checks: the approver
        # must explicitly accept that the server cannot pre-flight the body.
        json={"canary_percent": 100, "health_gate_seconds": 0,
              "confirm_unchecked_structure": True},
        headers=admin_headers(),
    )
    assert created.status_code == 200, created.text

    reply = poll()
    assert reply.HasField("remote_config")
    offered = reply.remote_config
    assert list(offered.config.config_map) == [CONFIG_KEY]
    assert offered.config.config_map[CONFIG_KEY].content_type == CONFIG_TYPE
    wire_hash = base64.b64encode(offered.config_hash).decode()

    poll(status="APPLIED", hash_b64=wire_hash)
    agent = AGENT_REGISTRY.get(AGENT_ID)
    assert agent.remote_config_status == "RemoteConfigStatuses_APPLIED"
    assert remote_config.REGISTRY.get(AGENT_ID)["config_hash"]


def test_a_failed_config_status_is_recorded(client):
    """The capture: a bad body crashes the engine and the supervisor reports
    FAILED while it restart-loops. The server stores what the agent said."""
    poll()
    _propose(client)
    client.post(
        "/config/rollouts",
        params={"proposal_id": _propose(client)["proposal_id"]},
        json={"canary_percent": 100, "health_gate_seconds": 0,
              "confirm_unchecked_structure": True},
        headers=admin_headers(),
    )
    reply = poll()
    wire_hash = base64.b64encode(reply.remote_config.config_hash).decode()

    poll(status="FAILED", hash_b64=wire_hash, healthy=False)
    agent = AGENT_REGISTRY.get(AGENT_ID)
    assert agent.remote_config_status == "RemoteConfigStatuses_FAILED"
    assert agent.healthy is False


# ------------------------------------------------------------------- snippet


def test_the_snippet_is_the_supervisor_config_with_the_placeholders():
    from server import profiles

    snippet = profiles.get("alloy").snippet_template  # the template itself
    assert "alloy otel-supervisor" in snippet
    assert "{endpoint}" in snippet and "{token}" in snippet
    # The crash warning from the capture, where an operator will read it.
    assert "reports_remote_config" in snippet


def test_the_snippet_renders_without_placeholder_leftovers():
    from server import profiles

    built = profiles.get("alloy").bootstrap_snippet(
        "wss://opamp.example.com/v1/opamp", "opamp_agent_x"
    )
    assert "{endpoint}" not in built and "{token}" not in built
    assert "Bearer opamp_agent_x" in built
