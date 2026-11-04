"""WebSocket transport tests (#89, #90): framing, registry, lifecycle, parity."""

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from proto import opamp_pb2
from server.framing import decode_message, encode_message
from tests.uids import uid
from server.ws import (
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_POLICY_VIOLATION,
    CLOSE_PROTOCOL_ERROR,
    DEFAULT_OUTBOUND_QUEUE_DEPTH,
    REGISTRY,
    ConnectionRegistry,
    _enqueue,
    origin_allowed,
    outbound_queue_depth,
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


class TestQueueDepthConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("OPAMP_WS_OUTBOUND_QUEUE", raising=False)
        assert outbound_queue_depth() == DEFAULT_OUTBOUND_QUEUE_DEPTH

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OPAMP_WS_OUTBOUND_QUEUE", "4")
        assert outbound_queue_depth() == 4

    @pytest.mark.parametrize("raw", ["", "nope", "0", "-2"])
    def test_invalid_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv("OPAMP_WS_OUTBOUND_QUEUE", raw)
        assert outbound_queue_depth() == DEFAULT_OUTBOUND_QUEUE_DEPTH


class TestOriginCheck:
    """WebSocket upgrades bypass CORSMiddleware, so the transport checks Origin."""

    def test_agent_without_origin_is_allowed(self):
        assert origin_allowed(None, ["https://ui.example"]) is True

    def test_wildcard_allows_any_origin(self):
        assert origin_allowed("http://evil.example", ["*"]) is True

    def test_listed_origin_is_allowed(self):
        assert origin_allowed("https://ui.example", ["https://ui.example"]) is True

    def test_unlisted_origin_is_refused(self):
        assert origin_allowed("http://evil.example", ["https://ui.example"]) is False

    def test_restrictive_cors_refuses_cross_origin_handshake(self, client, ws_on, monkeypatch):
        monkeypatch.setattr("server.main.CORS_ORIGINS", ["https://ui.example"])
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/v1/opamp", headers={"origin": "http://evil.example"}
            ):
                pass
        assert exc.value.code == CLOSE_POLICY_VIOLATION

    def test_restrictive_cors_still_allows_agent_without_origin(self, client, ws_on, monkeypatch):
        monkeypatch.setattr("server.main.CORS_ORIGINS", ["https://ui.example"])
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(uid("ws-no-origin"))))
            assert recv(ws).instance_uid == uid("ws-no-origin")


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
        monkeypatch.setenv("OPAMP_WS_ENABLED", "false")
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/opamp"):
                pass
        assert exc.value.code == CLOSE_POLICY_VIOLATION

    def test_enabled_by_default(self, client, monkeypatch):
        """No server-side opt-in: an agent that asks for the upgrade gets one.

        This is the whole point of the default. An operator should not have to
        learn about a flag before a WebSocket client can connect.
        """
        monkeypatch.delenv("OPAMP_WS_ENABLED", raising=False)

        with client.websocket_connect("/v1/opamp") as ws:
            assert ws is not None

    def test_enabled_accepts(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(uid("ws-accept"))))
            assert recv(ws).instance_uid == uid("ws-accept")


class TestStatusReporting:
    def test_first_message_registers_and_requests_full_state(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(uid("ws-first"))))
            response = recv(ws)

        assert response.instance_uid == uid("ws-first")
        assert response.flags & opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState
        agents = client.get("/agents").json()
        assert agents["count"] == 1
        assert agents["agents"][0]["healthy"] is True

    def test_multiplexes_two_agents_on_one_socket(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(uid("ws-mux-a"))))
            first = recv(ws)
            ws.send_bytes(frame(agent_message(uid("ws-mux-b"), healthy=False)))
            second = recv(ws)

        assert first.instance_uid == uid("ws-mux-a")
        assert second.instance_uid == uid("ws-mux-b")
        assert REGISTRY.connection_for(uid("ws-mux-a")) == REGISTRY.connection_for(
            uid("ws-mux-b")
        )
        assert client.get("/agents").json()["count"] == 2

    def test_agent_disconnect_removes_and_unbinds(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(agent_message(uid("ws-disconnect"))))
            recv(ws)
            ws.send_bytes(frame(agent_message(uid("ws-disconnect"), disconnect=True)))
            recv(ws)

        assert client.get("/agents").json()["count"] == 0
        assert REGISTRY.connection_for(uid("ws-disconnect")) is None


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
        oversized = frame(agent_message(uid("ws-big")))
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
        agent_uid = uid(f"ws-parity-{transport}")
        payload = agent_message(agent_uid, healthy=True).SerializeToString()

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

        agent = client.get(f"/agent/{agent_uid.hex()}").json()
        assert agent["healthy"] is True

    @pytest.mark.parametrize("transport", ["http", "ws"])
    def test_disconnect_removes_agent(self, client, ws_on, transport):
        agent_uid = uid(f"ws-parity-disconnect-{transport}")
        if transport == "http":
            client.post("/v1/opamp", content=agent_message(agent_uid).SerializeToString())
            client.post(
                "/v1/opamp",
                content=agent_message(agent_uid, disconnect=True).SerializeToString(),
            )
        else:
            with client.websocket_connect("/v1/opamp") as ws:
                ws.send_bytes(frame(agent_message(agent_uid)))
                recv(ws)
                ws.send_bytes(frame(agent_message(agent_uid, disconnect=True)))
                recv(ws)

        assert client.get("/agents").json()["count"] == 0
