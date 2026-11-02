"""An in-process OpAMP client for the WebSocket transport (#91).

The HTTP suites got `tests/conformance/harness.py`; the WebSocket suites had been
hand-rolling `encode_message`/`receive_bytes` in every test, which is how a test starts
asserting its own framing bug instead of the server's behaviour.

This client is deliberately thin and uses `server.framing` for both directions, so a
test exercises the same varint codec as production. It runs on Starlette's
`TestClient` socket in-process: no ports, no network, ~1 ms per connection.

    with ws_session(client) as ws:
        ws.send(status_report(uid))
        reply = ws.recv()
        assert reply.instance_uid == uid

`recv()` takes a timeout (default 2 s) and fails loudly with the close code if the
server closed instead of replying — a hung test that "looks like a timeout" is a day
of debugging for somebody.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proto import opamp_pb2
from server.framing import decode_message, encode_message

OPAMP_PATH = "/v1/opamp"
DEFAULT_TIMEOUT = 2.0


class OpampWSClient:
    """One OpAMP WebSocket connection, framed like the real thing."""

    def __init__(
        self,
        client: TestClient,
        *,
        headers: Optional[Dict[str, str]] = None,
        path: str = OPAMP_PATH,
        assert_echo_uid: bool = True,
    ):
        # Starlette calls `headers.setdefault(...)`, so an explicit dict is required;
        # an empty one leaves its `origin: http://testserver` default in place.
        self._session = client.websocket_connect(path, headers=dict(headers or {}))
        self._session.__enter__()
        self.assert_echo_uid = assert_echo_uid
        self._last_uid: bytes = b""

    # -- lifecycle -------------------------------------------------------

    def close(self, code: int = 1000) -> None:
        """Close from the client side (the server then unbinds on its way out)."""
        self._session.close(code)

    def __enter__(self) -> "OpampWSClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        try:
            self._session.close(1000)
        except Exception:  # already closed by the server
            pass
        try:
            self._session.__exit__(*exc_info)
        except Exception:  # pragma: no cover - teardown must never mask a failure
            pass

    # -- send ------------------------------------------------------------

    def send(self, message: opamp_pb2.AgentToServer) -> None:
        """Frame and send one `AgentToServer`, remembering its uid for the echo check."""
        self._last_uid = bytes(message.instance_uid)
        self.send_raw(encode_message(message.SerializeToString()))

    def send_raw(self, frame: bytes) -> None:
        """Send bytes as-is: the malformed-frame and oversize cases."""
        self._session.send_bytes(frame)

    def send_unframed(self, payload: bytes) -> None:
        """Send a payload with no OpAMP varint header (a protocol error by definition)."""
        self._session.send_bytes(payload)

    # -- receive ---------------------------------------------------------

    def recv(self, timeout: float = DEFAULT_TIMEOUT) -> opamp_pb2.ServerToAgent:
        """The next `ServerToAgent` reply, asserting it is addressed to the sender.

        One socket may carry several agents, so "who is this reply for?" is a real
        requirement (§WebSocket Message Exchange), not a detail.
        """
        reply = opamp_pb2.ServerToAgent()
        reply.ParseFromString(self.recv_raw(timeout))
        if (
            self.assert_echo_uid
            and self._last_uid
            and reply.instance_uid
            and reply.instance_uid != self._last_uid
        ):
            raise AssertionError(
                f"reply addressed to {reply.instance_uid.hex()}, "
                f"message came from {self._last_uid.hex()}"
            )
        return reply

    def recv_raw(self, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """The next frame's payload, unframed. Fails loudly on close instead of hanging."""
        box: Dict[str, Any] = {}

        def read() -> None:
            try:
                box["frame"] = self._session.receive_bytes()
            except BaseException as exc:  # closed, or the session died
                box["error"] = exc

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            raise AssertionError(f"no frame from the server within {timeout}s")
        if "error" in box:
            error = box["error"]
            if isinstance(error, WebSocketDisconnect):
                raise AssertionError(
                    f"connection closed with {error.code} instead of a reply"
                ) from error
            raise error
        return decode_message(box["frame"])

    def expect_close(self, code: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Assert the server closes the connection with `code`."""
        try:
            self.recv_raw(timeout)
        except AssertionError as exc:
            assert f"closed with {code}" in str(exc), str(exc)
            return
        raise AssertionError(f"expected close {code}, got a reply instead")


def ws_session(
    client: TestClient,
    *,
    headers: Optional[Dict[str, str]] = None,
    assert_echo_uid: bool = True,
) -> OpampWSClient:
    """Open a connection; use it as a context manager."""
    return OpampWSClient(client, headers=headers, assert_echo_uid=assert_echo_uid)
