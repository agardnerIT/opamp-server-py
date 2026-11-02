"""WebSocket connection lifecycle: reconnect, abrupt close, refused sends (#90).

The core transport behaviours (framing, 1009 oversize, 1002 malformed, queue
backpressure, newest-socket-wins) live in `test_ws.py`. This file covers the
*lifecycle* questions that #90 raised and nothing had pinned:

* **Reconnect.** A collector that restarts its socket must find its state intact, be
  asked for `ReportFullState` (the server cannot know what the previous socket
  delivered), and keep the same agent id.
* **Abrupt close is not a disconnect.** Dropping the socket without `AgentDisconnect`
  unbinds the uid but must leave the state alone: the stale sweep (#69) owns removal,
  and an agent that reconnects within its timeout must not look like a new agent.
* **A refused outbound send leaves the socket alive.** The spec says a server MUST
  NOT send an oversized `ServerToAgent`; "do not send" must not become "die and take
  the agent's other work with it".

Every connection here is in-process (Starlette `TestClient`), so the suite stays
socket-free and fast.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server import framing
from server.main import app
from server.state import AGENT_REGISTRY
from server.ws import OPAMP_WS_MESSAGES, REGISTRY
from tests.conformance.harness import uid
from tests.ws_client import ws_session

FULL_STATE = opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState
UID = uid("ws-lifecycle")
AGENT_ID = UID.hex()


def report(*, healthy: bool = True, sequence_num: int = 0) -> opamp_pb2.AgentToServer:
    """A first report with every field group, so "was the state kept?" is answerable."""
    message = opamp_pb2.AgentToServer()
    message.instance_uid = UID
    message.sequence_num = sequence_num
    message.capabilities = (
        opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsHealth
        | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsAvailableComponents
    )
    message.health.healthy = healthy
    attribute = message.agent_description.identifying_attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "lifecycle-collector"
    message.available_components.components["receivers"].sub_component_map["otlp"].SetInParent()
    message.package_statuses.packages["collector"].name = "collector"
    message.package_statuses.packages["collector"].agent_has_version = "1.0.0"
    return message


def heartbeat(sequence_num: int = 1) -> opamp_pb2.AgentToServer:
    message = opamp_pb2.AgentToServer()
    message.instance_uid = UID
    message.sequence_num = sequence_num
    return message


def sent_metric(outcome: str) -> float:
    return OPAMP_WS_MESSAGES.labels(direction="sent", outcome=outcome)._value.get()


@pytest.fixture
def client(monkeypatch):
    REGISTRY.reset()
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_MAX_MESSAGE_BYTES", raising=False)
    yield TestClient(app)
    REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


# --- reconnect --------------------------------------------------------------


def test_reconnect_keeps_the_agent_and_its_state(client):
    """A reconnect is not a re-registration: same id, nothing forgotten."""
    with ws_session(client) as ws:
        ws.send(report(sequence_num=1))
        ws.recv()

    with ws_session(client) as ws:
        ws.send(heartbeat(sequence_num=2))
        ws.recv()

    agent = client.get(f"/agent/{AGENT_ID}").json()
    assert agent["id"] == AGENT_ID
    assert agent["healthy"] is True
    assert agent["description"]["identifyingAttributes"][0]["key"] == "service.name"
    assert agent["package_statuses"]["packages"]["collector"]["agentHasVersion"] == "1.0.0"
    assert agent["available_components"]["components"]["receivers"]


def test_a_continuing_counter_across_a_reconnect_is_not_a_gap(client):
    """The sequence baseline is per *agent*, not per socket, so a clean reconnect is quiet.

    Asking for full state on every reconnect would be a request per reconnect for an
    agent that did nothing wrong — the thing #70's rate limit only bounds, not removes.
    """
    with ws_session(client) as ws:
        ws.send(heartbeat(sequence_num=1))
        ws.recv()

    with ws_session(client) as ws:
        ws.send(heartbeat(sequence_num=2))
        assert not ws.recv().flags & FULL_STATE


def test_a_restarted_counter_across_a_reconnect_is_a_gap(client):
    """A collector that restarts resets its counter, and that *is* status we missed."""
    with ws_session(client) as ws:
        ws.send(heartbeat(sequence_num=5))
        ws.recv()

    with ws_session(client) as ws:
        ws.send(heartbeat(sequence_num=1))
        assert ws.recv().flags & FULL_STATE


# --- abrupt close -----------------------------------------------------------


def test_an_abrupt_close_unbinds_the_uid_but_keeps_the_state(client):
    """No `AgentDisconnect` means "gone quiet", not "gone": removal belongs to #69."""
    with ws_session(client) as ws:
        ws.send(report(sequence_num=1))
        ws.recv()
    # Leaving the `with` block closes the socket without a disconnect message.

    assert REGISTRY.connection_for(UID) is None
    agent = client.get(f"/agent/{AGENT_ID}")
    assert agent.status_code == 200
    assert agent.json()["healthy"] is True
    assert AGENT_REGISTRY.get(AGENT_ID) is not None


def test_an_explicit_disconnect_removes_the_agent_immediately(client):
    """The mirror of the case above: `AgentDisconnect` *is* "gone"."""
    with ws_session(client) as ws:
        ws.send(report(sequence_num=1))
        ws.recv()
        message = heartbeat(sequence_num=2)
        message.agent_disconnect.SetInParent()
        ws.send(message)
        ws.recv()

    assert client.get(f"/agent/{AGENT_ID}").status_code == 404
    assert REGISTRY.connection_for(UID) is None


# --- refused sends ----------------------------------------------------------


class FakeWebSocket:
    """Just enough of `WebSocket` to drive `_serve_frames` without a socket."""

    client = None

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = None

    async def receive(self):
        if not self.frames:
            return {"type": "websocket.disconnect", "code": 1000}
        return self.frames.pop(0)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self, code: int = 1000):
        self.closed = code


def test_an_oversized_reply_is_refused_without_killing_the_socket(monkeypatch):
    """MUST NOT send an oversized `ServerToAgent`; MUST NOT drop the socket either.

    Driven at `_serve_frames` because the reply size is the handler's business: a real
    collector's reply is always small, so a socket-level test could only shrink the
    limit until the *request* was refused first. Here the reply is genuinely oversized
    and the frame on the wire is legal.
    """
    import asyncio

    from server.ws import ConnectionRegistry, ProcessedMessage, _serve_frames

    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "64")
    inbound = framing.encode_message(heartbeat(sequence_num=1).SerializeToString())
    assert len(inbound) <= 64, "the request must be legal for this case to mean anything"

    huge = opamp_pb2.ServerToAgent()
    huge.instance_uid = UID
    huge.custom_message.data = b"x" * 512  # 512 bytes of reply, limit 64

    def process(payload, principal, transport="ws"):
        return ProcessedMessage(huge.SerializeToString(), instance_uid=UID)

    registry = ConnectionRegistry()
    conn_id = registry.new_id()
    websocket = FakeWebSocket(
        [
            {"type": "websocket.receive", "bytes": inbound},
            # A second frame proves the loop kept serving after refusing the send.
            {"type": "websocket.receive", "bytes": inbound},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )

    async def run():
        queue = asyncio.Queue(maxsize=8)
        connection = registry.open(conn_id, queue, asyncio.get_running_loop(), None)
        return await _serve_frames(
            websocket, process, registry, None, None, connection
        )

    before = OPAMP_WS_MESSAGES.labels(direction="sent", outcome="too_big")._value.get()
    close_code = asyncio.run(run())

    assert websocket.sent == [], "an oversized ServerToAgent must never reach the wire"
    assert websocket.closed is None, "refusing a send must not close the connection"
    assert close_code == 1000
    assert (
        OPAMP_WS_MESSAGES.labels(direction="sent", outcome="too_big")._value.get()
        == before + 2
    )


def test_a_frame_over_the_limit_still_closes_1009(client, monkeypatch):
    """The receive side of the same limit is a close, not a silent drop."""
    from server.ws import CLOSE_MESSAGE_TOO_BIG

    message = report()
    frame = framing.encode_message(message.SerializeToString())
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", str(len(frame) - 1))

    with ws_session(client) as ws:
        ws.send_raw(frame)
        ws.expect_close(CLOSE_MESSAGE_TOO_BIG)


def test_a_malformed_frame_reply_precedes_the_close(client):
    """#73: a readable BAD_REQUEST frame, *then* 1002 — not just a bare close."""
    from server.ws import CLOSE_PROTOCOL_ERROR

    with ws_session(client) as ws:
        ws.send_unframed(b"\x00\x00")  # zero varint header + no payload
        error = ws.recv()
        assert error.error_response.type == (
            opamp_pb2.ServerErrorResponseType.ServerErrorResponseType_BadRequest
        )
        ws.expect_close(CLOSE_PROTOCOL_ERROR)
