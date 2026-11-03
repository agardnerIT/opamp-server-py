"""The WebSocket half of the spec index (#90, #91).

`test_spec_conformance.py` drives HTTP. The spec's claim is that the transports are
interchangeable, so the same clauses have to be indexable on a socket too — otherwise
"both transports behave the same" rests on one parity test someone wrote once.

Same rules as the HTTP index: one thin case per clause, named after the requirement,
naming the suite that owns the detail.
"""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from server import framing
from server.state import AGENT_REGISTRY
from server.ws import (
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_POLICY_VIOLATION,
    CLOSE_PROTOCOL_ERROR,
)
from tests.conformance import harness as spec
from tests.conformance.harness import expect_reply, opamp_send
from tests.ws_client import ws_session

@pytest.fixture(autouse=True)
def ws_transport(monkeypatch):
    """Every case here speaks the socket transport, with the default agent posture."""
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_MAX_MESSAGE_BYTES", raising=False)


#: The state a transport must not change. Volatile fields (timestamps, the cadence
#: estimate) are excluded because they are about *when*, not *what*.
COMPARED_FIELDS = (
    "healthy",
    "description",
    "effective_config",
    "available_components",
    "package_statuses",
    "remote_config_status",
)


def test_a_message_produces_the_same_state_over_either_transport(client):
    """§WebSocket Transport: an agent may use either transport, so state must match.

    A whole-reply comparison would be wrong (the transports differ in framing, not in
    meaning); a whole-*state* comparison is exactly the requirement.

    Parity detail: `tests/test_ws_parity.py`.
    """
    http_uid = spec.uid("ws-index-http")
    ws_uid = spec.uid("ws-index-ws")

    expect_reply(opamp_send(client, spec.first_report(http_uid, sequence_num=1)))
    with ws_session(client) as ws:
        ws.send(spec.first_report(ws_uid, sequence_num=1))
        ws.recv()

    over_http = client.get(f"/agent/{http_uid.hex()}").json()
    over_ws = client.get(f"/agent/{ws_uid.hex()}").json()
    assert {field: over_http[field] for field in COMPARED_FIELDS} == {
        field: over_ws[field] for field in COMPARED_FIELDS
    }


def test_a_frame_over_the_size_limit_closes_1009(client, monkeypatch):
    """§WebSocket Message Size Limits: oversize is a 1009 close, never a dispatch.

    Detail: `tests/test_ws.py`, `tests/test_ws_lifecycle.py`.
    """
    message = spec.first_report(spec.uid("ws-too-big"))
    frame = framing.encode_message(message.SerializeToString())
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", str(len(frame) - 1))

    with ws_session(client) as ws:
        ws.send_raw(frame)
        ws.expect_close(CLOSE_MESSAGE_TOO_BIG)

    assert AGENT_REGISTRY.get(spec.uid("ws-too-big").hex()) is None


def test_a_malformed_frame_gets_a_bad_request_then_closes_1002(client):
    """§Closing Connection + §ServerErrorResponse: readable error first, then 1002.

    Detail: `tests/test_protocol_validation.py` (WS class), `tests/test_ws.py`.
    """
    with ws_session(client) as ws:
        ws.send_unframed(b"\x00\x00")
        error = ws.recv()
        assert error.error_response.error_message
        ws.expect_close(CLOSE_PROTOCOL_ERROR)


def test_a_refused_handshake_closes_1008(client, monkeypatch):
    """§WebSocket Transport: with the transport switched off the upgrade is refused.

    Detail: `tests/test_ws.py`.
    """
    monkeypatch.setenv("OPAMP_WS_ENABLED", "false")

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/v1/opamp"):
            pass

    assert exc.value.code == CLOSE_POLICY_VIOLATION
