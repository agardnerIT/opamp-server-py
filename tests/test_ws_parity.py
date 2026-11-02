"""HTTP/WebSocket transport parity (#91).

The spec says the two transports are interchangeable: an agent may use either, so the
same `AgentToServer` must produce the same agent state and the same reply *flags*
whichever one carried it. Two handlers drifting apart is not hypothetical — it is what
#67 was — and the only durable cure is to assert the same case twice, once per
transport.

Every case here is parametrized and asserts on `GET /agent/{id}` (the state) rather
than on either transport's plumbing: a test that asserts *how* the reply arrived
cannot prove the transports are interchangeable.

Deep behaviour stays in the per-transport suites (`test_server.py`,
`test_ws.py`, `test_sequence.py`); this file is the equality claim.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server.main import app
from server.state import AGENT_REGISTRY
from server.ws import REGISTRY
from tests.conformance import harness as spec
from tests.conformance.harness import expect_reply, opamp_send
from tests.ws_client import ws_session

TRANSPORTS = ["http", "ws"]


class HTTPTransport:
    """The HTTP transport, shaped like the WebSocket client so a test is transport-blind."""

    def __init__(self, client: TestClient):
        self.client = client

    def send(self, message: opamp_pb2.AgentToServer) -> opamp_pb2.ServerToAgent:
        return expect_reply(opamp_send(self.client, message))

    def close(self) -> None:
        pass


class WSTransport:
    """The socket client, wrapped so `send()` returns the reply like HTTP does."""

    def __init__(self, session):
        self.session = session

    def send(self, message: opamp_pb2.AgentToServer) -> opamp_pb2.ServerToAgent:
        self.session.send(message)
        return self.session.recv()

    def close(self) -> None:
        self.session.close()


@contextlib.contextmanager
def session(client: TestClient, transport: str):
    """One agent's view of the server, over `transport`."""
    if transport == "http":
        yield HTTPTransport(client)
    else:
        with ws_session(client) as ws:
            yield WSTransport(ws)


@pytest.fixture
def client(monkeypatch):
    REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    yield TestClient(app)
    REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_first_report_lands_identically(client, transport):
    """§Initial Handshake: description, health, config, components and packages."""
    agent_uid = spec.uid(f"parity-first-{transport}")

    with session(client, transport) as agent:
        agent.send(spec.first_report(agent_uid, sequence_num=1))

    detail = client.get(f"/agent/{agent_uid.hex()}").json()
    assert detail["healthy"] is True
    assert detail["description"]["identifyingAttributes"][0]["key"] == "service.name"
    assert detail["components"]["receiver"][0]["id"] == "otlp"
    assert detail["available_components"]["components"]["receivers"]
    assert detail["package_statuses"]["packages"]["collector"]["agentHasVersion"] == "1.0.0"
    assert detail["effective_config"]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_health_change_lands_identically(client, transport):
    """§Component Health: unhealthy is a state change, not a lost report."""
    agent_uid = spec.uid(f"parity-health-{transport}")

    with session(client, transport) as agent:
        agent.send(spec.first_report(agent_uid, sequence_num=1))
        agent.send(
            spec.status_only(
                agent_uid, sequence_num=2, healthy=False, last_error="collector stopped"
            )
        )

    assert client.get(f"/agent/{agent_uid.hex()}").json()["healthy"] is False


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_hash_only_component_report_is_asked_about_identically(client, transport):
    """§AvailableComponents / #77: the ask is a reply flag, so it must be transport-free."""
    agent_uid = spec.uid(f"parity-components-{transport}")

    with session(client, transport) as agent:
        reply = agent.send(spec.hash_only_components(agent_uid, sequence_num=1))

    assert spec.asks_for_components(reply) is True
    assert client.get(f"/agent/{agent_uid.hex()}").json()[
        "last_requested_components_hash"
    ] is not None


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_full_component_list_lands_identically(client, transport):
    """§AvailableComponents: the components are stored *and* parsed for `used`."""
    agent_uid = spec.uid(f"parity-full-components-{transport}")

    with session(client, transport) as agent:
        agent.send(spec.first_report(agent_uid, sequence_num=1))
        reply = agent.send(spec.full_components(agent_uid, sequence_num=2))

    assert spec.asks_for_components(reply) is False
    receiver = client.get(f"/agent/{agent_uid.hex()}").json()["components"]["receiver"][0]
    assert receiver["id"] == "otlp"
    assert receiver["used"] is True


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_remote_config_status_lands_identically(client, transport):
    """§RemoteConfigStatus: status *and* hash, because reconciliation compares hashes."""
    agent_uid = spec.uid(f"parity-remote-config-{transport}")
    reported_hash = b"\x05" * 32

    with session(client, transport) as agent:
        agent.send(spec.first_report(agent_uid, sequence_num=1))
        agent.send(
            spec.remote_config_status(
                agent_uid, status="FAILED", config_hash=reported_hash, sequence_num=2
            )
        )

    detail = client.get(f"/agent/{agent_uid.hex()}").json()
    assert detail["remote_config_status"] == "RemoteConfigStatuses_FAILED"
    assert detail["last_remote_config_hash"] == reported_hash.hex()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_sequence_gap_is_flagged_identically(client, transport):
    """§Agent Status Compression: the repair flag cannot be HTTP-only."""
    agent_uid = spec.uid(f"parity-gap-{transport}")

    with session(client, transport) as agent:
        agent.send(spec.heartbeat(agent_uid, sequence_num=1))
        agent.send(spec.heartbeat(agent_uid, sequence_num=2))
        reply = agent.send(spec.heartbeat(agent_uid, sequence_num=9))

    assert spec.asks_for_full_state(reply) is True


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_disconnect_removes_the_agent_identically(client, transport):
    """§Agent Disconnect: gone is gone, on either transport."""
    agent_uid = spec.uid(f"parity-disconnect-{transport}")

    with session(client, transport) as agent:
        agent.send(spec.first_report(agent_uid, sequence_num=1))
        agent.send(spec.disconnect(agent_uid, sequence_num=2))

    assert client.get(f"/agent/{agent_uid.hex()}").status_code == 404


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_bare_heartbeat_keeps_the_state_identically(client, transport):
    """§Agent Status Compression: an omitted sub-message means unchanged."""
    agent_uid = spec.uid(f"parity-compression-{transport}")

    with session(client, transport) as agent:
        agent.send(spec.first_report(agent_uid, sequence_num=1))
        agent.send(spec.heartbeat(agent_uid, sequence_num=2))

    detail = client.get(f"/agent/{agent_uid.hex()}").json()
    assert detail["healthy"] is True
    assert detail["description"]["identifyingAttributes"]
    assert detail["package_statuses"]["packages"]["collector"]["agentHasVersion"] == "1.0.0"
