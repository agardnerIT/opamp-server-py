"""WebSocket transport tests (#89, #90): framing, registry, lifecycle, parity."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proto import opamp_pb2
from server.framing import decode_message, encode_message
from server.main import app
from server.state import AGENT_REGISTRY
from server.ws import (
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_POLICY_VIOLATION,
    CLOSE_PROTOCOL_ERROR,
    REGISTRY,
    ConnectionRegistry,
    _enqueue,
)


def agent_message(uid: bytes, healthy: bool | None = True, disconnect: bool = False):
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    if healthy is not None:
        msg.health.healthy = healthy
    if disconnect:
        msg.agent_disconnect.SetInParent()
    return msg


def frame(msg) -> bytes:
    return encode_message(msg.SerializeToString())


def recv(ws) -> opamp_pb2.ServerToAgent:
    response = opamp_pb2.ServerToAgent()
    response.ParseFromString(decode_message(ws.receive_bytes()))
    return response


@pytest.fixture
def client():
    REGISTRY.reset()
    yield TestClient(app)
    REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


@pytest.fixture
def ws_on(monkeypatch):
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")


class TestConnectionRegistry:
    def test_bind_and_unbind(self):
        registry = ConnectionRegistry()
        registry.bind(1, b"a")
        assert registry.connection_for(b"a") == 1
        assert registry.agents_on(1) == frozenset({b"a"})
        registry.unbind(1, b"a")
        assert registry.connection_for(b"a") is None

    def test_one_socket_many_agents(self):
        registry = ConnectionRegistry()
        registry.bind(1, b"a")
        registry.bind(1, b"b")
        assert registry.agents_on(1) == frozenset({b"a", b"b"})

    def test_newest_socket_wins_duplicate_uid(self):
        registry = ConnectionRegistry()
        registry.bind(1, b"a")
        registry.bind(2, b"a")
        assert registry.connection_for(b"a") == 2
        assert registry.agents_on(1) == frozenset()

    def test_unbinding_old_socket_keeps_new_binding(self):
        registry = ConnectionRegistry()
        registry.bind(1, b"a")
        registry.bind(2, b"a")
        registry.unbind_connection(1)
        assert registry.connection_for(b"a") == 2

    def test_unbind_connection_returns_uids(self):
        registry = ConnectionRegistry()
        registry.bind(7, b"a")
        registry.bind(7, b"b")
        assert registry.unbind_connection(7) == frozenset({b"a", b"b"})
        assert registry.agents_on(7) == frozenset()


class TestOutboundQueue:
    def test_drops_oldest_when_full(self):
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)

        async def fill():
            assert _enqueue(queue, b"one")
            assert _enqueue(queue, b"two")
            assert _enqueue(queue, b"three")
            return [queue.get_nowait() for _ in range(2)]

        assert asyncio.run(fill()) == [b"two", b"three"]


class TestHandshake:
    def test_disabled_refuses(self, client, monkeypatch):
        monkeypatch.delenv("OPAMP_WS_ENABLED", raising=False)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/opamp"):
                pass
        assert exc.value.code == CLOSE_POLICY_VIOLATION

    def test_enabled_accepts(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(b"ws-accept-0001")))
            assert recv(ws).instance_uid == b"ws-accept-0001"


class TestStatusReporting:
    def test_first_message_registers_and_requests_full_state(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(b"ws-first-000001")))
            response = recv(ws)

        assert response.instance_uid == b"ws-first-000001"
        assert response.flags & opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState
        agents = client.get("/agents").json()
        assert agents["count"] == 1
        assert agents["agents"][0]["healthy"] is True

    def test_multiplexes_two_agents_on_one_socket(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(b"ws-mux-00000001")))
            first = recv(ws)
            ws.send_bytes(frame(agent_message(b"ws-mux-00000002", healthy=False)))
            second = recv(ws)

        assert first.instance_uid == b"ws-mux-00000001"
        assert second.instance_uid == b"ws-mux-00000002"
        assert REGISTRY.connection_for(b"ws-mux-00000001") == REGISTRY.connection_for(
            b"ws-mux-00000002"
        )
        assert client.get("/agents").json()["count"] == 2

    def test_agent_disconnect_removes_and_unbinds(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(b"ws-disc-0000001")))
            recv(ws)
            ws.send_bytes(frame(agent_message(b"ws-disc-0000001", disconnect=True)))
            recv(ws)

        assert client.get("/agents").json()["count"] == 0
        assert REGISTRY.connection_for(b"ws-disc-0000001") is None


class TestMalformedFrames:
    def test_non_zero_header_closes_1002(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(b"\x01\x0a\x00")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
        assert exc.value.code == CLOSE_PROTOCOL_ERROR

    def test_empty_frame_closes_1002(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(b"")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
        assert exc.value.code == CLOSE_PROTOCOL_ERROR

    def test_text_frame_closes_1002(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_text("not binary")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
        assert exc.value.code == CLOSE_PROTOCOL_ERROR

    def test_oversize_closes_1009(self, client, ws_on, monkeypatch):
        oversized = frame(agent_message(b"ws-big-000000001"))
        monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "16")
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(oversized)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
        assert exc.value.code == CLOSE_MESSAGE_TOO_BIG


class TestTransportParity:
    """The same message must produce the same agent state on both transports."""

    @pytest.mark.parametrize("transport", ["http", "ws"])
    def test_first_report_status_is_applied(self, client, ws_on, transport):
        uid = f"parity-{transport}-01".encode()
        payload = agent_message(uid, healthy=True).SerializeToString()

        if transport == "http":
            response = client.post(
                "/v1/opamp",
                content=payload,
                headers={"Content-Type": "application/x-protobuf"},
            )
            assert response.status_code == 200
        else:
            with client.websocket_connect("/v1/opamp") as ws:
                ws.send_bytes(encode_message(payload))
                recv(ws)

        agent = client.get(f"/agent/{uid.hex()}").json()
        assert agent["healthy"] is True

    @pytest.mark.parametrize("transport", ["http", "ws"])
    def test_disconnect_removes_agent(self, client, ws_on, transport):
        uid = f"parity-disc-{transport}".encode()
        if transport == "http":
            client.post("/v1/opamp", content=agent_message(uid).SerializeToString())
            client.post(
                "/v1/opamp",
                content=agent_message(uid, disconnect=True).SerializeToString(),
            )
        else:
            with client.websocket_connect("/v1/opamp") as ws:
                ws.send_bytes(frame(agent_message(uid)))
                recv(ws)
                ws.send_bytes(frame(agent_message(uid, disconnect=True)))
                recv(ws)

        assert client.get("/agents").json()["count"] == 0
