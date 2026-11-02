"""Asking a hash-only agent for its full component list (#77).

Spec §AvailableComponents says `components` "may be omitted from the message if the
server has not explicitly requested it be sent by setting the `ReportAvailableComponents`
flag in the previous ServerToAgent message", while `hash` "should be included in every
AvailableComponents message". So an agent is *allowed* to send only a hash — and today
nothing ever asks for more, which leaves the OCB manifest feature (and every component
report) permanently empty for that agent.

What is pinned here:

* a hash-only report makes the next reply ask, and the ask is one-shot per hash — an
  agent that keeps sending the same hash is asked once, not on every poll;
* a changed hash (upgrade, pipeline edit) earns exactly one new ask;
* the full list arriving is the answer: no flag, and `POST /agent/{id}/manifest` works;
* the hash we asked about is a *state* field, so it survives a restart, while the
  non-compliance counter saturates so an ignoring agent stops causing a write per poll;
* an agent that ignores the ask is counted and logged, because otherwise "the agent's
  SDK never implements the flag" is silent;
* the #70 and #77 asks are independent and can share one reply.

No sleeping: the batched state write is exercised through `TestClient` in-process.
"""

from __future__ import annotations

import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient
from google.protobuf import json_format

import server.main as server_main
from proto import opamp_pb2
from server.main import app
from server.state import AGENT_REGISTRY, COMPONENTS_IGNORED_LOG_AT, SQLiteAgentStore
from server.ws import REGISTRY as WS_REGISTRY

UID = bytes.fromhex("77" * 16)
AGENT_ID = UID.hex()

ASK_FOR_COMPONENTS = (
    opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportAvailableComponents
)
FULL_STATE = opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState

HASH_A = b"\xaa\x01"
HASH_B = b"\xbb\x02"

# A components payload in the JSON shape `MessageToDict` produces, plus a health map
# that names the same component as used — that is what makes a manifest buildable.
FULL_COMPONENTS = {"receivers": {"subComponentMap": {"otlp": {}}}}
HEALTH_USING_OTLP = {
    "healthy": True,
    "componentHealthMap": {
        "pipeline:traces": {"componentHealthMap": {"receiver:otlp": {"healthy": True}}}
    },
}


def report(
    component_hash: bytes | None = HASH_A,
    components: dict | None = None,
    sequence_num: int = 0,
    health: dict | None = None,
    disconnect: bool = False,
) -> bytes:
    """One `AgentToServer` with an `available_components` report.

    Built through `ParseDict` so the hash goes on the wire as the base64 string every
    real agent sends (the server reads it with `MessageToDict`).
    """
    available: dict = {}
    if component_hash is not None:
        available["hash"] = base64.b64encode(component_hash).decode()
    if components is not None:
        available["components"] = components
    payload: dict = {
        "instanceUid": base64.b64encode(UID).decode(),
        "capabilities": int(
            opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsAvailableComponents
        ),
        "availableComponents": available,
    }
    if sequence_num:
        payload["sequenceNum"] = sequence_num
    if health:
        payload["health"] = health
    if disconnect:
        payload["agentDisconnect"] = {}
    msg = opamp_pb2.AgentToServer()
    json_format.ParseDict(payload, msg)
    return msg.SerializeToString()


def reply(response) -> opamp_pb2.ServerToAgent:
    assert response.status_code == 200
    parsed = opamp_pb2.ServerToAgent()
    parsed.ParseFromString(response.content)
    return parsed


def asks_for_components(response) -> bool:
    return bool(reply(response).flags & ASK_FOR_COMPONENTS)


def asks_for_full_state(response) -> bool:
    return bool(reply(response).flags & FULL_STATE)


@pytest.fixture
def client(monkeypatch):
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    yield TestClient(app)
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


def post(client, body: bytes):
    return client.post(
        "/v1/opamp", content=body, headers={"Content-Type": "application/x-protobuf"}
    )


def metric(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


# --- the ask ----------------------------------------------------------------


def test_hash_only_report_makes_the_next_reply_ask_for_the_details(client):
    response = post(client, report(component_hash=HASH_A))

    assert asks_for_components(response)
    # Persisted in our hex form, which is what the API and the one-shot check use.
    assert AGENT_REGISTRY.get(AGENT_ID).last_requested_components_hash == HASH_A.hex()
    assert client.get(f"/agent/{AGENT_ID}").json()[
        "last_requested_components_hash"
    ] == HASH_A.hex()


def test_full_components_are_never_asked_about(client):
    response = post(client, report(component_hash=HASH_A, components=FULL_COMPONENTS))

    assert asks_for_components(response) is False
    assert AGENT_REGISTRY.get(AGENT_ID).last_requested_components_hash is None


def test_report_with_neither_hash_nor_components_is_not_asked(client):
    """Nothing to key a one-shot ask on: asking would be a request per message."""
    response = post(client, report(component_hash=None))

    assert asks_for_components(response) is False


def test_follow_up_with_full_components_answers_the_ask(client):
    assert asks_for_components(post(client, report(component_hash=HASH_A)))

    response = post(
        client,
        report(component_hash=HASH_A, components=FULL_COMPONENTS, health=HEALTH_USING_OTLP),
    )

    assert asks_for_components(response) is False
    assert client.get(f"/agent/{AGENT_ID}").json()["components"]["receiver"][0]["id"] == "otlp"
    manifest = client.post(f"/agent/{AGENT_ID}/manifest")
    assert manifest.status_code == 200
    assert "otlpreceiver" in manifest.json()["manifest_yaml"]


def test_agent_that_keeps_sending_the_same_hash_is_asked_once(client):
    assert asks_for_components(post(client, report(component_hash=HASH_A)))

    for _ in range(4):
        assert asks_for_components(post(client, report(component_hash=HASH_A))) is False

    agent = AGENT_REGISTRY.get(AGENT_ID)
    assert agent.components_request_ignored == COMPONENTS_IGNORED_LOG_AT


def test_a_changed_hash_is_asked_about_again(client):
    assert asks_for_components(post(client, report(component_hash=HASH_A)))
    assert asks_for_components(post(client, report(component_hash=HASH_A))) is False

    assert asks_for_components(post(client, report(component_hash=HASH_B)))
    assert AGENT_REGISTRY.get(AGENT_ID).last_requested_components_hash == HASH_B.hex()
    # The counter is about the *current* ask, not the agent's whole life.
    assert AGENT_REGISTRY.get(AGENT_ID).components_request_ignored == 0


def test_ignoring_the_ask_is_visible_in_state_and_resets_on_compliance(client):
    post(client, report(component_hash=HASH_A))
    for _ in range(COMPONENTS_IGNORED_LOG_AT + 2):
        post(client, report(component_hash=HASH_A))

    detail = client.get(f"/agent/{AGENT_ID}").json()
    assert detail["components_request_ignored"] == COMPONENTS_IGNORED_LOG_AT

    post(client, report(component_hash=HASH_A, components=FULL_COMPONENTS))

    assert client.get(f"/agent/{AGENT_ID}").json()["components_request_ignored"] == 0


def test_disconnecting_agent_is_not_asked(client):
    response = post(client, report(component_hash=HASH_A, disconnect=True))

    assert asks_for_components(response) is False
    assert AGENT_REGISTRY.get(AGENT_ID) is None


def test_both_asks_can_ride_one_reply(client):
    """#70 (sequence gap) and #77 (hash-only components) are independent."""
    post(client, report(component_hash=HASH_A, sequence_num=1))
    post(client, report(component_hash=HASH_A, sequence_num=2))

    response = post(client, report(component_hash=HASH_B, sequence_num=9))

    assert asks_for_full_state(response)
    assert asks_for_components(response)


# --- persistence ------------------------------------------------------------


def test_requested_hash_survives_a_restart(client):
    post(client, report(component_hash=HASH_A))

    reloaded = SQLiteAgentStore(AGENT_REGISTRY._store.db_path).load_all()[AGENT_ID]
    assert reloaded.last_requested_components_hash == HASH_A.hex()

    # What a restarted server sees: the same rows, a fresh in-memory registry.
    AGENT_REGISTRY._agents.clear()
    AGENT_REGISTRY._agents.update({AGENT_ID: reloaded})

    assert asks_for_components(post(client, report(component_hash=HASH_A))) is False


def test_an_existing_database_gains_the_columns_by_migration(tmp_path):
    """A live fleet's `agents` table predates #77; opening it must not fail."""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, instance_uid BLOB,"
            " connected_at TEXT, last_heartbeat TEXT, healthy INTEGER,"
            " capabilities INTEGER DEFAULT 0, description TEXT, effective_config TEXT,"
            " remote_config_status TEXT DEFAULT 'UNSET', package_statuses TEXT,"
            " available_components TEXT, health TEXT, compliance TEXT, updated_at TEXT,"
            " last_remote_config_hash TEXT, remote_config_error TEXT,"
            " last_sequence_num INTEGER)"
        )
        conn.execute(
            "INSERT INTO agents (agent_id, instance_uid, connected_at, last_heartbeat,"
            " healthy, updated_at) VALUES (?, ?, ?, ?, 1, ?)",
            (
                AGENT_ID,
                UID,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    loaded = SQLiteAgentStore(db).load_all()[AGENT_ID]

    # No ask was ever made, so the first hash-only report gets one.
    assert loaded.last_requested_components_hash is None
    assert loaded.components_request_ignored == 0
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(agents)")]
    assert "last_requested_components_hash" in columns
    assert "components_request_ignored" in columns


# --- metrics ----------------------------------------------------------------


def test_metric_counts_asks_by_reason(client):
    before_first = metric(server_main.PROM_COMPONENTS_REQUESTS, reason="hash_only")
    before_changed = metric(server_main.PROM_COMPONENTS_REQUESTS, reason="hash_changed")

    post(client, report(component_hash=HASH_A))  # first hash -> hash_only
    post(client, report(component_hash=HASH_A))  # same hash, already asked
    post(client, report(component_hash=HASH_B))  # agent upgraded -> hash_changed

    assert (
        metric(server_main.PROM_COMPONENTS_REQUESTS, reason="hash_only")
        == before_first + 1
    )
    assert (
        metric(server_main.PROM_COMPONENTS_REQUESTS, reason="hash_changed")
        == before_changed + 1
    )


def test_metric_counts_an_agent_that_ignores_the_ask(client):
    before = server_main.PROM_COMPONENTS_IGNORED._value.get()

    post(client, report(component_hash=HASH_A))
    for _ in range(COMPONENTS_IGNORED_LOG_AT + 2):
        post(client, report(component_hash=HASH_A))

    # Counted once when it crosses the threshold, not once per ignored poll.
    assert server_main.PROM_COMPONENTS_IGNORED._value.get() == before + 1
