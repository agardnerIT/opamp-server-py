"""BAD_REQUEST handling and instance_uid validation (#73).

Acceptance criteria pinned here:

* an empty uid is refused with `BAD_REQUEST` and registers nothing;
* an unparseable body is refused with a human-readable `error_message`;
* `RequestInstanceUid` mints a 16-byte UUIDv7, and the agent is seen under it;
* non-16-byte but non-empty uids stay accepted (deliberate compatibility choice);
* with agent authentication the server refuses to mint an identity (D1);
* WebSocket gets the same answer: readable BAD_REQUEST, then close 1002.
"""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from proto import opamp_pb2
from server import agent_auth, protocol
from server.framing import decode_message, encode_message
from server.main import _handle_agent_message
from server.state import AGENT_REGISTRY
from tests.conformance.harness import opamp_send, parse_reply
from server.ws import CLOSE_PROTOCOL_ERROR, REGISTRY

BAD_REQUEST = opamp_pb2.ServerErrorResponseType_BadRequest
REQUEST_UID = opamp_pb2.AgentToServerFlags.AgentToServerFlags_RequestInstanceUid


def message(uid: bytes = b"", **kwargs) -> opamp_pb2.AgentToServer:
    msg = opamp_pb2.AgentToServer()
    if uid:
        msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    for field, value in kwargs.items():
        setattr(msg, field, value)
    return msg


def frame(msg: opamp_pb2.AgentToServer) -> bytes:
    return encode_message(msg.SerializeToString())


def assert_bad_request(response, *, contains: str = "") -> opamp_pb2.ServerToAgent:
    assert response.status_code == 400, response.text
    assert response.headers["content-type"] == "application/x-protobuf"
    reply = parse_reply(response)
    assert reply.error_response.type == BAD_REQUEST
    assert reply.error_response.error_message
    if contains:
        assert contains in reply.error_response.error_message
    # A protocol error must not advertise capabilities in the same message.
    assert reply.capabilities == 0
    assert not reply.HasField("remote_config")
    return reply


@pytest.fixture(autouse=True)
def open_agent_auth(monkeypatch):
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)


class TestBadRequest:
    def test_empty_uid_is_refused_and_registers_nothing(self, client):
        response = opamp_send(client, message().SerializeToString())

        reply = assert_bad_request(response, contains="instance_uid")
        assert reply.instance_uid == b""
        assert AGENT_REGISTRY.count == 0

    def test_unparseable_body_is_refused_with_a_readable_reason(self, client):
        # Field 1 (length-delimited) announces 255 bytes and then stops: protobuf
        # cannot parse it, unlike "almost anything is an unknown field" bodies.
        response = opamp_send(client, b"\x0a\xff")

        assert_bad_request(response, contains="malformed AgentToServer")
        assert AGENT_REGISTRY.count == 0

    def test_non_16_byte_uid_is_still_accepted(self, client):
        # Deliberate: readable ASCII ids are used by every harness and by existing
        # fleets. Only an empty uid is rejected (see server/protocol.py).
        uid = b"test_opamp_00123456"  # 19 bytes
        response = opamp_send(client, message(uid).SerializeToString())

        assert response.status_code == 200
        assert parse_reply(response).instance_uid == uid
        assert AGENT_REGISTRY.get(uid.hex()) is not None

    def test_valid_16_byte_uid_is_unchanged(self, client):
        uid = bytes.fromhex("ab" * 16)
        response = opamp_send(client, message(uid).SerializeToString())

        assert response.status_code == 200
        reply = parse_reply(response)
        assert reply.instance_uid == uid
        assert reply.capabilities > 0
        assert not reply.HasField("error_response")
        assert AGENT_REGISTRY.get(uid.hex()) is not None


class TestRequestInstanceUid:
    def test_mints_a_uuid7_and_registers_under_it(self, client):
        temporary = b"temporary-uid-01"
        response = opamp_send(
            client, message(temporary, flags=REQUEST_UID).SerializeToString()
        )

        assert response.status_code == 200
        reply = parse_reply(response)
        new_uid = reply.agent_identification.new_instance_uid
        assert len(new_uid) == 16
        assert new_uid[6] >> 4 == 7  # version nibble
        assert new_uid[8] >> 6 == 0b10  # RFC 4122 variant
        # The reply is addressed to the temporary uid the agent used in this message.
        assert reply.instance_uid == temporary
        # State lives under the minted id, not the throwaway.
        assert AGENT_REGISTRY.get(new_uid.hex()) is not None
        assert AGENT_REGISTRY.get(temporary.hex()) is None

    def test_minted_uid_is_usable_on_the_next_message(self, client):
        temporary = b"temporary-uid-02"
        first = opamp_send(client, message(temporary, flags=REQUEST_UID).SerializeToString())
        new_uid = parse_reply(first).agent_identification.new_instance_uid

        second = opamp_send(client, message(new_uid).SerializeToString())
        assert second.status_code == 200
        assert AGENT_REGISTRY.get(new_uid.hex()) is not None

    def test_temporary_record_is_cleaned_up(self, client):
        temporary = bytes.fromhex("cd" * 16)
        assert opamp_send(client, message(temporary).SerializeToString()).status_code == 200
        assert AGENT_REGISTRY.get(temporary.hex()) is not None

        response = opamp_send(client, message(temporary, flags=REQUEST_UID).SerializeToString())
        new_uid = parse_reply(response).agent_identification.new_instance_uid

        assert AGENT_REGISTRY.get(temporary.hex()) is None
        assert AGENT_REGISTRY.get(new_uid.hex()) is not None

    def test_refused_when_agent_auth_is_on(self, client, monkeypatch):
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
        uid = bytes.fromhex("33" * 16)
        token, _ = agent_auth.issue_token(uid.hex())

        response = opamp_send(
            client,
            message(uid, flags=REQUEST_UID).SerializeToString(),
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_bad_request(response, contains="RequestInstanceUid")
        assert AGENT_REGISTRY.get(uid.hex()) is None

    def test_direct_handler_refusal_uses_the_same_exception(self):
        uid = bytes.fromhex("44" * 16)
        principal = agent_auth.Principal(agent_id=uid.hex())
        body = message(uid, flags=REQUEST_UID).SerializeToString()

        with pytest.raises(protocol.BadRequest, match="RequestInstanceUid"):
            _handle_agent_message(body, principal)


class TestWebSocketBadRequest:
    def test_empty_uid_gets_error_frame_then_close_1002(self, client, ws_on):
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(message()))
            reply = opamp_pb2.ServerToAgent()
            reply.ParseFromString(decode_message(ws.receive_bytes()))
            assert reply.error_response.type == BAD_REQUEST
            assert reply.error_response.error_message
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()

        assert exc.value.code == CLOSE_PROTOCOL_ERROR
        assert AGENT_REGISTRY.count == 0

    def test_request_uid_binds_the_minted_identity(self, client, ws_on):
        temporary = b"temporary-ws-001"
        with client.websocket_connect("/v1/opamp") as ws:
            ws.send_bytes(frame(message(temporary, flags=REQUEST_UID)))
            reply = opamp_pb2.ServerToAgent()
            reply.ParseFromString(decode_message(ws.receive_bytes()))

            new_uid = reply.agent_identification.new_instance_uid
            assert len(new_uid) == 16
            assert REGISTRY.connection_for(new_uid) is not None
            assert REGISTRY.connection_for(temporary) is None
