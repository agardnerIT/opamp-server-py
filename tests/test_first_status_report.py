"""A new agent's first OpAMP message must apply the status it carries (#67)."""

import base64
import binascii
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict, ParseDict

from proto import opamp_pb2
from server import main
from server.state import AgentRegistry, SQLiteAgentStore


@pytest.fixture
def isolated_server(monkeypatch, tmp_path):
    registry = AgentRegistry()
    registry._store = SQLiteAgentStore(tmp_path / "agents.db")
    registry._agents = {}
    alert = Mock()
    monkeypatch.setattr(main, "AGENT_REGISTRY", registry)
    monkeypatch.setattr(main, "send_new_agent_alert", alert)
    return TestClient(main.app), registry, alert


def full_report(healthy=True):
    return ParseDict(
        {
            "agentDescription": {
                "identifyingAttributes": [
                    {"key": "service.name", "value": {"stringValue": "collector"}}
                ]
            },
            "health": {"healthy": healthy, "lastError": "" if healthy else "starting"},
            "effectiveConfig": {
                "configMap": {
                    "configMap": {"": {"body": base64.b64encode(b"service:\n  extensions: [health_check]\n").decode()}}
                }
            },
            "availableComponents": {
                "components": {
                    "extensions": {"subComponentMap": {"health_check": {}}}
                }
            },
            "packageStatuses": {
                "packages": {"collector": {"name": "collector", "agentHasVersion": "1.0.0"}}
            },
            "remoteConfigStatus": {"status": "RemoteConfigStatuses_APPLIED"},
        },
        opamp_pb2.AgentToServer(instance_uid=b"first-report-0001"),
    )


def send(client, message):
    response = client.post(
        "/v1/opamp",
        content=message.SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert response.status_code == 200
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(response.content)
    return reply


@pytest.mark.parametrize("healthy", [True, False])
def test_first_report_is_visible_without_another_poll(isolated_server, healthy):
    client, registry, alert = isolated_server
    message = full_report(healthy)
    expected = MessageToDict(message)
    agent_id = binascii.hexlify(message.instance_uid).decode()
    counters = {
        kind: main.PROM_MESSAGES_RECEIVED.labels(message_type=kind)._value.get()
        for kind in ("health", "effective_config", "available_components", "package_status", "heartbeat")
    }

    reply = send(client, message)

    assert reply.flags == opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState
    detail = client.get(f"/agent/{agent_id}").json()
    assert detail["healthy"] is healthy
    assert detail["description"] == expected["agentDescription"]
    assert json.loads(detail["effective_config"]) == expected["effectiveConfig"]
    assert detail["available_components"] == expected["availableComponents"]
    assert detail["package_statuses"] == expected["packageStatuses"]
    assert detail["remote_config_status"] == "RemoteConfigStatuses_APPLIED"
    assert registry.get(agent_id).health == expected["health"]
    assert client.get("/agents").json()["agents"][0]["healthy"] is healthy
    assert client.post(f"/agent/{agent_id}/manifest").status_code == 200
    report = client.get(f"/agent/{agent_id}/report")
    assert report.status_code == 200
    assert "Extension: 1 total, 1 in use" in report.json()["report_markdown"]
    alert.assert_called_once_with(agent_id)
    for kind, before in counters.items():
        assert main.PROM_MESSAGES_RECEIVED.labels(message_type=kind)._value.get() == before + 1


def test_later_report_updates_status_without_reregistering(isolated_server):
    client, registry, alert = isolated_server
    message = full_report()
    agent_id = binascii.hexlify(message.instance_uid).decode()
    send(client, message)
    connected_at = registry.get(agent_id).connected_at
    message.health.healthy = False
    message.health.last_error = "unavailable"
    message.package_statuses.packages["collector"].agent_has_version = "2.0.0"

    reply = send(client, message)

    detail = client.get(f"/agent/{agent_id}").json()
    assert reply.flags == 0
    assert detail["healthy"] is False
    assert detail["package_statuses"]["packages"]["collector"]["agentHasVersion"] == "2.0.0"
    assert registry.get(agent_id).connected_at == connected_at
    alert.assert_called_once_with(agent_id)


def test_heartbeat_without_status_preserves_first_report(isolated_server):
    client, registry, alert = isolated_server
    message = full_report()
    agent_id = binascii.hexlify(message.instance_uid).decode()
    send(client, message)
    before = client.get(f"/agent/{agent_id}").json()

    send(client, opamp_pb2.AgentToServer(instance_uid=message.instance_uid))

    after = client.get(f"/agent/{agent_id}").json()
    for field in ("healthy", "description", "effective_config", "available_components", "package_statuses", "remote_config_status"):
        assert after[field] == before[field]
    alert.assert_called_once_with(agent_id)
