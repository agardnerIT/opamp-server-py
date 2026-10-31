"""OpAMP WebSocket transport (spec §WebSocket Transport, `specification.md:380`).

The socket layer is deliberately thin: framing lives in `server.framing`, message
handling lives in the FastAPI app (`_handle_agent_message`, shared with the HTTP
endpoint), and this module only owns connection lifecycle, per-connection
backpressure and the `instance_uid` ↔ connection registry.

One socket may carry several agents (spec §WebSocket Message Exchange), so the
registry is a many-to-many map, not a single "the agent on this socket".
"""

from __future__ import annotations

import asyncio
import itertools
import os
from typing import Callable, Dict, FrozenSet, NamedTuple, Set

import prometheus_client as prom_client
from fastapi import WebSocket
from loguru import logger
from starlette.websockets import WebSocketDisconnect

from server.framing import (
    MalformedFrame,
    MessageTooBig,
    decode_message,
    encode_message,
    message_limit,
)
from server import agent_auth

CLOSE_PROTOCOL_ERROR = 1002
CLOSE_POLICY_VIOLATION = 1008
CLOSE_MESSAGE_TOO_BIG = 1009

# Bounded per-connection outbound queue: a slow reader must not pin memory.
DEFAULT_OUTBOUND_QUEUE_DEPTH = 32


def outbound_queue_depth() -> int:
    """Per-connection send queue depth from `OPAMP_WS_OUTBOUND_QUEUE`.

    Invalid or non-positive values fall back to the default rather than raising
    at import time, matching `framing.message_limit()`: a config typo should not
    stop the server from booting.
    """
    raw = os.environ.get("OPAMP_WS_OUTBOUND_QUEUE")
    if not raw:
        return DEFAULT_OUTBOUND_QUEUE_DEPTH
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OUTBOUND_QUEUE_DEPTH
    return value if value > 0 else DEFAULT_OUTBOUND_QUEUE_DEPTH


def origin_allowed(origin: str | None, allowed: list) -> bool:
    """May a WebSocket upgrade with this `Origin` header proceed?

    `CORSMiddleware` does not apply to WebSocket upgrades, so without this check
    setting a restrictive `CORS_ORIGINS` would still leave a page on any origin
    able to open the socket. Agents are not browsers and send no `Origin` header
    at all, so a missing header is allowed; a present one must match
    `CORS_ORIGINS` unless that is `*` (the default).
    """
    if origin is None or "*" in allowed:
        return True
    return origin in allowed

OPAMP_WS_CONNECTIONS = prom_client.Gauge(
    "opamp_ws_connections",
    "Number of open OpAMP WebSocket connections",
)

OPAMP_WS_MESSAGES = prom_client.Counter(
    "opamp_ws_messages_total",
    "OpAMP WebSocket messages by direction and outcome",
    ["direction", "outcome"],
)

OPAMP_WS_OUTBOUND_DROPPED = prom_client.Counter(
    "opamp_ws_outbound_dropped_total",
    "Outbound ServerToAgent messages dropped because the connection queue was full",
)


class ProcessedMessage(NamedTuple):
    """What the shared message handler returns for one AgentToServer message."""

    response: bytes
    instance_uid: bytes = b""
    disconnected: bool = False


def ws_enabled() -> bool:
    """Read `OPAMP_WS_ENABLED` at call time so behaviour is testable.

    Default false: the server stays HTTP-only, which is what every existing
    collector expects. The route still exists and refuses the handshake.
    """
    return os.environ.get("OPAMP_WS_ENABLED", "false").lower() in ("true", "1", "yes")


class ConnectionRegistry:
    """Tracks which `instance_uid`s are bound to which live socket, and by whom.

    The principal is the authenticated agent id from the credential (#94) — `None`
    in `off` mode, where identity is whatever the message claims. Binding is
    per-uid because one socket may carry several agents; the principal lets the
    message handler refuse a takeover instead of silently moving the binding.
    """

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._by_uid: Dict[bytes, int] = {}
        self._principal_by_uid: Dict[bytes, str | None] = {}
        self._by_conn: Dict[int, Set[bytes]] = {}

    def new_id(self) -> int:
        return next(self._ids)

    def bind(self, conn_id: int, instance_uid: bytes, principal: str | None = None) -> None:
        previous = self._by_uid.get(instance_uid)
        if previous is not None and previous != conn_id:
            # Spec §Duplicate WebSocket Connections: MAY disconnect. Duplicate-uid
            # semantics for *legitimate* agents belong to #75; a binding may only
            # move here when the principal allows it, which the message handler
            # has already checked (#94).
            logger.warning(
                f"instance_uid {instance_uid.hex()} moved from connection "
                f"{previous} to {conn_id}"
            )
            self._by_conn.get(previous, set()).discard(instance_uid)
        self._by_uid[instance_uid] = conn_id
        self._principal_by_uid[instance_uid] = principal
        self._by_conn.setdefault(conn_id, set()).add(instance_uid)

    def principal_for(self, instance_uid: bytes) -> str | None:
        """Who holds the current binding (`None` = unauthenticated binding)."""
        return self._principal_by_uid.get(instance_uid)

    def unbind(self, conn_id: int, instance_uid: bytes) -> None:
        self._by_conn.get(conn_id, set()).discard(instance_uid)
        if self._by_uid.get(instance_uid) == conn_id:
            del self._by_uid[instance_uid]
            self._principal_by_uid.pop(instance_uid, None)

    def unbind_connection(self, conn_id: int) -> FrozenSet[bytes]:
        uids = frozenset(self._by_conn.pop(conn_id, set()))
        for uid in uids:
            if self._by_uid.get(uid) == conn_id:
                del self._by_uid[uid]
                self._principal_by_uid.pop(uid, None)
        return uids

    def connection_for(self, instance_uid: bytes) -> int | None:
        return self._by_uid.get(instance_uid)

    def agents_on(self, conn_id: int) -> FrozenSet[bytes]:
        return frozenset(self._by_conn.get(conn_id, ()))

    def reset(self) -> None:
        self._by_uid.clear()
        self._principal_by_uid.clear()
        self._by_conn.clear()


REGISTRY = ConnectionRegistry()


async def _close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # already closed / transport gone
        pass


def _enqueue(queue: asyncio.Queue, item: bytes) -> bool:
    """Queue a frame, dropping the oldest when full. Returns False if dropped."""
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        pass

    try:
        queue.get_nowait()
        queue.task_done()
    except asyncio.QueueEmpty:
        pass

    OPAMP_WS_OUTBOUND_DROPPED.inc()
    logger.warning("OpAMP WebSocket outbound queue full; dropped oldest message")
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        OPAMP_WS_OUTBOUND_DROPPED.inc()
        return False


async def _writer(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Single writer task per connection, so send order matches receive order."""
    while True:
        item = await queue.get()
        try:
            await websocket.send_bytes(item)
            OPAMP_WS_MESSAGES.labels(direction="sent", outcome="ok").inc()
        except Exception:
            logger.debug("OpAMP WebSocket write failed; connection is going away")
            return


async def serve(
    websocket: WebSocket,
    process: Callable[..., ProcessedMessage],
    registry: ConnectionRegistry | None = None,
    principal: object | None = None,
) -> None:
    """Serve one accepted WebSocket connection until it closes.

    `process` is the shared AgentToServer handler, called as
    `process(payload, principal)`. Importing the FastAPI app here would create a
    cycle, so it is injected. `principal` is the authenticated identity from the
    handshake (#94); it is `None` in `off` mode, where behaviour is unchanged.
    """
    registry = REGISTRY if registry is None else registry
    principal_id = getattr(principal, "agent_id", None)
    conn_id = registry.new_id()
    queue: asyncio.Queue = asyncio.Queue(maxsize=outbound_queue_depth())
    writer = asyncio.create_task(_writer(websocket, queue))
    OPAMP_WS_CONNECTIONS.inc()
    logger.info(f"OpAMP WebSocket connection {conn_id} opened")

    try:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                break

            if message["type"] == "websocket.disconnect":
                break

            frame = message.get("bytes")
            if frame is None:
                # OpAMP is binary-only; a text frame is a protocol error.
                logger.warning(f"Connection {conn_id}: non-binary WebSocket frame")
                OPAMP_WS_MESSAGES.labels(direction="received", outcome="malformed").inc()
                await _close(websocket, CLOSE_PROTOCOL_ERROR)
                break

            try:
                payload = decode_message(frame, max_bytes=message_limit())
            except MessageTooBig as exc:
                logger.warning(f"Connection {conn_id}: {exc}")
                OPAMP_WS_MESSAGES.labels(direction="received", outcome="too_big").inc()
                await _close(websocket, CLOSE_MESSAGE_TOO_BIG)
                break
            except MalformedFrame as exc:
                logger.warning(f"Connection {conn_id}: malformed frame: {exc}")
                OPAMP_WS_MESSAGES.labels(direction="received", outcome="malformed").inc()
                await _close(websocket, CLOSE_PROTOCOL_ERROR)
                break

            OPAMP_WS_MESSAGES.labels(direction="received", outcome="ok").inc()
            try:
                result = process(payload, principal)
            except agent_auth.AgentAuthError as exc:
                # #94: identity, not the message body, decides who may speak. The
                # handler refused before applying anything, so state is untouched.
                logger.warning(
                    f"Connection {conn_id}: refused message ({exc.reason}): {exc.detail}"
                )
                OPAMP_WS_MESSAGES.labels(direction="received", outcome="unauthorized").inc()
                agent_auth.record_failure(
                    exc.reason,
                    principal=exc.principal,
                    claimed=exc.claimed,
                    transport="ws",
                    client=websocket.client.host if websocket.client else "",
                )
                await _close(websocket, CLOSE_POLICY_VIOLATION)
                break

            if result.instance_uid:
                if result.disconnected:
                    registry.unbind(conn_id, result.instance_uid)
                else:
                    registry.bind(conn_id, result.instance_uid, principal_id)

            try:
                frame_out = encode_message(result.response, max_bytes=message_limit())
            except MessageTooBig as exc:
                # Spec: MUST NOT send an oversized ServerToAgent.
                logger.error(f"Connection {conn_id}: refusing to send response: {exc}")
                OPAMP_WS_MESSAGES.labels(direction="sent", outcome="too_big").inc()
                continue

            _enqueue(queue, frame_out)

    except Exception:
        logger.exception(f"OpAMP WebSocket connection {conn_id} failed")
    finally:
        writer.cancel()
        try:
            await writer
        except (asyncio.CancelledError, Exception):
            pass
        registry.unbind_connection(conn_id)
        OPAMP_WS_CONNECTIONS.dec()
        logger.info(f"OpAMP WebSocket connection {conn_id} closed")
