"""One OpAMP message performs at most one agent-row write (#109).

The capacity ceiling was 5 full-row SQLite upserts per heartbeat, one per status
field group. A message that changes nothing must now write nothing, and a
message that changes N field groups must write exactly once.
"""

import binascii
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import ParseDict
from unittest.mock import Mock

from proto import opamp_pb2
from server import main
from server.state import AgentRegistry, SQLiteAgentStore


class CountingStore:
    """Delegates to the real store and counts row writes."""

    def __init__(self, store):
        self.store = store
        self.upserts = 0

    def upsert(self, state):
        self.upserts += 1
        self.store.upsert(state)

    def load_all(self):
        return self.store.load_all()

    def remove(self, agent_id):
        return self.store.remove(agent_id)


@pytest.fixture
def isolated_server(monkeypatch, tmp_path):
    db_path = tmp_path / "agents.db"
    registry = AgentRegistry()
    registry._store = CountingStore(SQLiteAgentStore(db_path))
    registry._agents = {}
    monkeypatch.setattr(main, "AGENT_REGISTRY", registry)
    monkeypatch.setattr(main, "send_new_agent_alert", Mock())
    return TestClient(main.app), registry, db_path


def full_report(healthy=True, version="1.0.0"):
    return ParseDict(
        {
            "agentDescription": {
                "identifyingAttributes": [
                    {"key": "service.name", "value": {"stringValue": "collector"}}
                ]
            },
            "health": {"healthy": healthy, "lastError": "" if healthy else "starting"},
            "effectiveConfig": {"configMap": {"configMap": {"": {"body": "c2VydmljZToK"}}}},
            "availableComponents": {"components": {"extensions": {"subComponentMap": {"health_check": {}}}}},
            "packageStatuses": {"packages": {"collector": {"name": "collector", "agentHasVersion": version}}},
            "remoteConfigStatus": {"status": "RemoteConfigStatuses_APPLIED"},
        },
        opamp_pb2.AgentToServer(instance_uid=b"write-batching-0001"),
    )


def send(client, message):
    response = client.post(
        "/v1/opamp",
        content=message.SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert response.status_code == 200


def stored_row(db_path, agent_id):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()


def test_new_agent_message_writes_the_row_once(isolated_server):
    client, registry, db_path = isolated_server
    message = full_report()
    agent_id = binascii.hexlify(message.instance_uid).decode()

    send(client, message)

    assert registry._store.upserts == 1
    row = stored_row(db_path, agent_id)
    assert row["healthy"] == 1
    assert row["remote_config_status"] == "RemoteConfigStatuses_APPLIED"
    assert json.loads(row["package_statuses"])["packages"]["collector"]["agentHasVersion"] == "1.0.0"
    assert json.loads(row["effective_config"])["configMap"]


def test_unchanged_status_report_writes_nothing(isolated_server):
    client, registry, db_path = isolated_server
    message = full_report()
    agent_id = binascii.hexlify(message.instance_uid).decode()
    send(client, message)
    after_first = registry._store.upserts

    for _ in range(5):
        send(client, message)

    assert registry._store.upserts == after_first
    assert stored_row(db_path, agent_id)["healthy"] == 1


def test_changed_field_groups_are_written_once_per_message(isolated_server):
    client, registry, db_path = isolated_server
    message = full_report()
    agent_id = binascii.hexlify(message.instance_uid).decode()
    send(client, message)
    after_first = registry._store.upserts

    message.health.healthy = False
    message.health.last_error = "unavailable"
    message.package_statuses.packages["collector"].agent_has_version = "2.0.0"
    message.remote_config_status.status = 2
    send(client, message)

    assert registry._store.upserts == after_first + 1
    row = stored_row(db_path, agent_id)
    assert row["healthy"] == 0
    assert json.loads(row["package_statuses"])["packages"]["collector"]["agentHasVersion"] == "2.0.0"


def test_bare_heartbeat_is_flushed_only_when_the_stored_heartbeat_is_stale(
    isolated_server, monkeypatch
):
    client, registry, db_path = isolated_server
    message = full_report()
    agent_id = binascii.hexlify(message.instance_uid).decode()
    send(client, message)
    after_first = registry._store.upserts
    heartbeat = opamp_pb2.AgentToServer(instance_uid=message.instance_uid)

    send(client, heartbeat)
    assert registry._store.upserts == after_first

    # Pretend the last persisted heartbeat is old enough to matter for recovery.
    registry._persisted_at[agent_id] = 0.0
    send(client, heartbeat)
    assert registry._store.upserts == after_first + 1
    assert stored_row(db_path, agent_id)["health"] is not None
