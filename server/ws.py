"""OpAMP WebSocket transport (spec §WebSocket Transport, `specification.md:380`).

The socket layer is deliberately thin: framing lives in `server.framing`, message
handling lives in the FastAPI app (`_handle_agent_message`, shared with the HTTP
endpoint), and this module only owns connection lifecycle, per-connection
backpressure and the `instance_uid` ↔ connection registry.

One socket may carry several agents (spec §WebSocket Message Exchange), so the
registry is a many-to-many map, not a single "the agent on this socket".

**Unsolicited sends (#102).** `send_to_agent()` is the one way the server pushes a
frame it was not asked for. It changes nothing about the delivery *decision* — the
caller builds the frame from `server.remote_config`'s single decision function
(D5: "push now" is a latency optimisation over the same model, not a second
delivery path) — it only owns the transport facts:

* **Address by uid, never by socket.** A uid whose binding belongs to another
  principal is refused (#94), because a uid is not a routing hint.
* **Same backpressure as a reply.** Config frames ride the same bounded queue, so a
  slow reader cannot pin memory; a dropped frame is *not* a lost delivery, because
  the caller records delivery in an `on_sent` callback that only runs once the frame
  has actually been written.
* **Latest revision wins.** A newer config frame for the same uid marks the queued
  older one superseded: the writer skips it instead of sending two revisions in a
  row, and the superseded frame never counts as an attempt.
* **Config frames yield to replies.** A per-connection token bucket plus a global
  in-flight cap mean a 100-agent rollout cannot starve heartbeats on a shared
  socket: throttled frames return False and reconciliation re-offers them later.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from typing import Callable, Dict, FrozenSet, NamedTuple, Optional, Set

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
from server import agent_auth, tracing

CLOSE_PROTOCOL_ERROR = 1002
CLOSE_POLICY_VIOLATION = 1008
CLOSE_MESSAGE_TOO_BIG = 1009

# Bounded per-connection outbound queue: a slow reader must not pin memory.
DEFAULT_OUTBOUND_QUEUE_DEPTH = 32

# Config push budgets (#102). Per connection, because one wedged collector must not
# throttle the fleet; the global cap exists so N busy connections cannot turn a
# rollout into an unbounded number of unwritten frames held in memory.
DEFAULT_PUSH_RATE_PER_SECOND = 20.0
DEFAULT_PUSH_BURST = 20
DEFAULT_PUSH_MAX_CONCURRENT = 50
# How long a route will wait for the event loop to accept a push onto a live
# connection before giving up and reporting "queued" (the poll fallback).
DEFAULT_PUSH_WAIT_SECONDS = 2.0

# Delivery channels, used both by `remote_config.record_delivery()` and the metrics
# in this module so "how did it get there" has one vocabulary.
CHANNEL_PUSH = "push"
CHANNEL_REPLY = "reply"

KIND_REPLY = "reply"
KIND_CONFIG = "config"

# Why a push cannot be attempted. Stable strings because they end up in metrics,
# logs and API responses (`remote_config.DeliveryResult.reason`).
BLOCKER_OFFLINE = "offline"
BLOCKER_FOREIGN_PRINCIPAL = "foreign_principal"


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


def _env_number(name: str, default, cast=int):
    """Read a positive number from the environment, warning and defaulting."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using {default}")
        return default
    if value <= 0:
        logger.warning(f"{name}={raw!r} must be positive; using {default}")
        return default
    return value


def push_rate_per_second() -> float:
    return _env_number("CONFIG_PUSH_RATE_PER_SECOND", DEFAULT_PUSH_RATE_PER_SECOND, float)


def push_burst() -> int:
    return _env_number("CONFIG_PUSH_BURST", DEFAULT_PUSH_BURST, int)


def push_max_concurrent() -> int:
    return _env_number("CONFIG_PUSH_MAX_CONCURRENT", DEFAULT_PUSH_MAX_CONCURRENT, int)


def push_wait_seconds() -> float:
    return _env_number("CONFIG_PUSH_WAIT_SECONDS", DEFAULT_PUSH_WAIT_SECONDS, float)


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

# #102: the push path is the only way the server starts a conversation, so its
# outcomes are the ones an operator asks about ("did the rollout reach the fleet?").
OPAMP_WS_PUSH_SENT = prom_client.Counter(
    "opamp_ws_push_sent_total",
    "Unsolicited ServerToAgent frames by outcome",
    ["outcome"],
)

# "Is the queue full" must be answerable without reading logs, and it is the
# leading indicator of a rollout that is being throttled rather than delivered.
OPAMP_WS_CONFIG_PENDING = prom_client.Gauge(
    "opamp_ws_config_pending",
    "Config frames queued for a live connection but not yet written",
)


class ProcessedMessage(NamedTuple):
    """What the shared message handler returns for one AgentToServer message."""

    response: bytes
    instance_uid: bytes = b""
    disconnected: bool = False
    # #102: an optional follow-up frame to send *after* the reply. A promotion
    # decided while handling this message must not race the reply (the agent would
    # see the config before the message it answered), and a push is asynchronous,
    # so the handler hands the socket layer a callable instead of sending itself.
    # Returning None means "nothing to send". Ignored on the HTTP transport, where
    # the offer simply rides the next poll — same outcome, later timing.
    post_reply: Optional[Callable[[], Optional[bytes]]] = None


def ws_enabled() -> bool:
    """Read `OPAMP_WS_ENABLED` at call time so behaviour is testable.

    Default false: the server stays HTTP-only, which is what every existing
    collector expects. The route still exists and refuses the handshake.
    """
    return os.environ.get("OPAMP_WS_ENABLED", "false").lower() in ("true", "1", "yes")


class TokenBucket:
    """Per-connection send budget for config frames (#102).

    Deliberately not a queue and not a sleep: `take()` either grants a token or
    refuses, and a refused config frame becomes "delivered on the next status
    report" — the same state the agent would be in without a socket at all. Blocking
    a rollout to wait for one slow reader would starve heartbeats on the same queue.
    """

    __slots__ = ("rate", "capacity", "tokens", "updated", "clock")

    def __init__(self, rate: float, capacity: int, clock: Callable[[], float] = time.monotonic):
        self.rate = float(rate)
        self.capacity = float(max(1, capacity))
        self.tokens = self.capacity
        self.updated = clock()
        self.clock = clock

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


class Outbound:
    """One queued frame and what to do when (or instead of) writing it."""

    __slots__ = ("payload", "kind", "uid", "on_sent", "superseded", "settled")

    def __init__(
        self,
        payload: bytes,
        kind: str = KIND_REPLY,
        uid: Optional[bytes] = None,
        on_sent: Optional[Callable[[], None]] = None,
    ) -> None:
        self.payload = payload
        self.kind = kind
        self.uid = uid
        self.on_sent = on_sent
        # Set when a newer revision is queued for the same uid: the writer skips it.
        self.superseded = False
        # Set exactly once when the item leaves the queue (written, dropped, ...).
        self.settled = False


class Connection:
    """A live socket as the push path sees it.

    Handed out by the registry as an opaque snapshot: callers read `closed` and
    queue onto it, they never mutate the registry through it. `config_slots` is
    only ever touched from the event loop, so it needs no lock.
    """

    __slots__ = ("conn_id", "queue", "loop", "principal_id", "bucket", "closed", "config_slots")

    def __init__(
        self,
        conn_id: int,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        principal_id: Optional[str] = None,
    ) -> None:
        self.conn_id = conn_id
        self.queue = queue
        self.loop = loop
        self.principal_id = principal_id
        self.bucket = TokenBucket(push_rate_per_second(), push_burst())
        self.closed = False
        self.config_slots: Dict[bytes, Outbound] = {}


class ConnectionRegistry:
    """Tracks which `instance_uid`s are bound to which live socket, and by whom.

    The principal is the authenticated agent id from the credential (#94) — `None`
    in `off` mode, where identity is whatever the message claims. Binding is
    per-uid because one socket may carry several agents; the principal lets the
    message handler refuse a takeover instead of silently moving the binding.

    Two callers touch this from different threads: the serve task (bind/unbind/open/
    close) and a route's worker thread (`send_to_agent` resolving a uid). Reads are
    single-key lookups returning an immutable-ish `Connection` snapshot, so they
    cannot tear; the multi-key mutations take a `threading.Lock`. A `threading` lock
    rather than an `asyncio` one, precisely because the second caller is not the
    event loop.
    """

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._by_uid: Dict[bytes, int] = {}
        self._principal_by_uid: Dict[bytes, str | None] = {}
        self._by_conn: Dict[int, Set[bytes]] = {}
        self._conns: Dict[int, Connection] = {}

    def new_id(self) -> int:
        return next(self._ids)

    # -- connection lifecycle ------------------------------------------------

    def open(
        self,
        conn_id: int,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        principal_id: Optional[str] = None,
    ) -> Connection:
        """Register a socket's send queue so `send_to_agent` can reach it."""
        connection = Connection(conn_id, queue, loop, principal_id)
        with self._lock:
            self._conns[conn_id] = connection
        return connection

    def close(self, conn_id: int) -> None:
        """Mark a socket closed and forget its handle, so pushes stop immediately."""
        with self._lock:
            connection = self._conns.pop(conn_id, None)
        if connection is not None:
            connection.closed = True

    def handle(self, conn_id: int) -> Optional[Connection]:
        with self._lock:
            return self._conns.get(conn_id)

    def handle_for(self, instance_uid: bytes) -> Optional[Connection]:
        """The live connection a uid is bound to, as one atomic snapshot."""
        with self._lock:
            conn_id = self._by_uid.get(instance_uid)
            if conn_id is None:
                return None
            return self._conns.get(conn_id)

    # -- bindings ------------------------------------------------------------

    def bind(self, conn_id: int, instance_uid: bytes, principal: str | None = None) -> None:
        with self._lock:
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
        with self._lock:
            return self._principal_by_uid.get(instance_uid)

    def unbind(self, conn_id: int, instance_uid: bytes) -> None:
        with self._lock:
            self._by_conn.get(conn_id, set()).discard(instance_uid)
            if self._by_uid.get(instance_uid) == conn_id:
                del self._by_uid[instance_uid]
                self._principal_by_uid.pop(instance_uid, None)

    def unbind_connection(self, conn_id: int) -> FrozenSet[bytes]:
        with self._lock:
            uids = frozenset(self._by_conn.pop(conn_id, set()))
            for uid in uids:
                if self._by_uid.get(uid) == conn_id:
                    del self._by_uid[uid]
                    self._principal_by_uid.pop(uid, None)
            return uids

    def connection_for(self, instance_uid: bytes) -> int | None:
        with self._lock:
            return self._by_uid.get(instance_uid)

    def agents_on(self, conn_id: int) -> FrozenSet[bytes]:
        with self._lock:
            return frozenset(self._by_conn.get(conn_id, ()))

    def reset(self) -> None:
        """Drop every connection and binding, and zero the push accounting.

        Used by tests and by nothing else, but the in-flight counter lives outside the
        registry, so leaving it behind would leak a queue-depth gauge across runs.
        """
        with self._lock:
            for connection in self._conns.values():
                connection.closed = True
            self._conns.clear()
            self._by_uid.clear()
            self._principal_by_uid.clear()
            self._by_conn.clear()
        _reset_push_accounting()


REGISTRY = ConnectionRegistry()


# --------------------------------------------------------------- push accounting

# Config frames queued but not yet written, across every connection. Only touched
# from the event loop (enqueue happens there), so a plain int is enough.
_config_in_flight = 0


def config_in_flight() -> int:
    return _config_in_flight


def _reset_push_accounting() -> None:
    global _config_in_flight
    _config_in_flight = 0
    OPAMP_WS_CONFIG_PENDING.set(0)


def _settle(item: object, outcome: str) -> None:
    """Account for one config frame leaving the queue. Idempotent by design."""
    global _config_in_flight
    if not isinstance(item, Outbound) or item.kind != KIND_CONFIG or item.settled:
        return
    item.settled = True
    _config_in_flight = max(0, _config_in_flight - 1)
    OPAMP_WS_CONFIG_PENDING.set(_config_in_flight)
    OPAMP_WS_PUSH_SENT.labels(outcome=outcome).inc()


def _clear_slot(connection: Connection, item: object) -> None:
    """Forget a config frame so a later push does not try to supersede it."""
    if not isinstance(item, Outbound) or item.uid is None:
        return
    if connection.config_slots.get(item.uid) is item:
        connection.config_slots.pop(item.uid, None)


async def _close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # already closed / transport gone
        pass


def _enqueue(queue: asyncio.Queue, item) -> bool:
    """Queue a frame, dropping the oldest when full. Returns False if dropped."""
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        pass

    evicted = None
    try:
        evicted = queue.get_nowait()
        queue.task_done()
    except asyncio.QueueEmpty:
        pass
    if evicted is not None:
        # A config frame evicted here was never written, so it must not count as a
        # delivery attempt — reconciliation re-offers it on the next report (#102).
        _settle(evicted, "dropped")

    OPAMP_WS_OUTBOUND_DROPPED.inc()
    logger.warning("OpAMP WebSocket outbound queue full; dropped oldest message")
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        OPAMP_WS_OUTBOUND_DROPPED.inc()
        _settle(item, "dropped")
        return False


async def _writer(websocket: WebSocket, connection: Connection) -> None:
    """Single writer task per connection, so send order matches receive order."""
    queue = connection.queue
    while True:
        item = await queue.get()
        if isinstance(item, Outbound) and item.superseded:
            # A newer revision was queued for this uid in the same tick: skip, and
            # do not call `on_sent` — this frame was never on the wire.
            _settle(item, "superseded")
            _clear_slot(connection, item)
            continue
        try:
            await websocket.send_bytes(item.payload if isinstance(item, Outbound) else item)
            OPAMP_WS_MESSAGES.labels(direction="sent", outcome="ok").inc()
        except Exception:
            logger.debug("OpAMP WebSocket write failed; connection is going away")
            _settle(item, "closed")
            _clear_slot(connection, item)
            return

        if isinstance(item, Outbound):
            _clear_slot(connection, item)
            _settle(item, "ok")
            if item.on_sent is not None:
                try:
                    # Runs *after* the write, which is what makes "delivered" a fact
                    # rather than an intention (a queued frame proves nothing).
                    item.on_sent()
                except Exception:  # pragma: no cover - defensive
                    logger.bind(event="opamp.ws.on_sent_failed").warning(
                        "post-write delivery hook failed", exc_info=True
                    )


def _drain(connection: Connection, outcome: str) -> int:
    """Settle everything the writer never reached, so the gauges return to zero."""
    drained = 0
    while True:
        try:
            item = connection.queue.get_nowait()
        except asyncio.QueueEmpty:
            return drained
        drained += 1
        _settle(item, outcome)
        _clear_slot(connection, item)


async def send_to_agent(
    instance_uid: bytes,
    payload: bytes,
    *,
    kind: str = "frame",
    on_sent: Optional[Callable[[], None]] = None,
) -> bool:
    """Enqueue an unsolicited ServerToAgent frame onto the uid's connection.

    Returns False — never raises, never buffers — when there is nowhere to put it:
    no live connection, a connection that is closing, a uid bound to a different
    principal (#94), or a config frame over the rate limit. The caller's answer to
    False is always the same: the desired state is already stored, so the agent
    receives it on its next status report (#81).

    `on_sent` runs on the event loop **after** the frame is written, which is where
    delivery bookkeeping belongs: `remote_config.offer_for()` can record at build
    time because the offer rides the same response body, but a queued push proves
    nothing until it is on the wire. It runs inline, so keep it to one small write —
    that is what `record_delivery()` is; do not put a network call there.
    """
    global _config_in_flight

    connection = REGISTRY.handle_for(instance_uid)
    blocker = push_blocker(instance_uid, connection)
    if blocker == BLOCKER_OFFLINE:
        OPAMP_WS_PUSH_SENT.labels(outcome="offline").inc()
        return False
    if blocker == BLOCKER_FOREIGN_PRINCIPAL:
        # Defence in depth (#94): the binding is what authorises the send, so a uid
        # that moved to another principal's socket is refused rather than handed a
        # config that was approved for a different credential's agent.
        OPAMP_WS_PUSH_SENT.labels(outcome="refused").inc()
        logger.bind(
            event="opamp.ws.push_refused",
            conn_id=getattr(connection, "conn_id", None),
            agent_id=instance_uid.hex(),
        ).warning(
            f"Refusing push to {instance_uid.hex()}: the uid is bound to a different "
            "principal than the connection that holds it"
        )
        return False

    if kind == KIND_CONFIG:
        if _config_in_flight >= push_max_concurrent():
            OPAMP_WS_PUSH_SENT.labels(outcome="throttled").inc()
            logger.bind(
                event="opamp.ws.push_throttled", reason="max_concurrent",
                agent_id=instance_uid.hex(),
            ).warning(
                f"Config push to {instance_uid.hex()} throttled: "
                f"{_config_in_flight} frames in flight (CONFIG_PUSH_MAX_CONCURRENT="
                f"{push_max_concurrent()}); it will be delivered on the next report"
            )
            return False
        if not connection.bucket.take():
            OPAMP_WS_PUSH_SENT.labels(outcome="throttled").inc()
            logger.bind(
                event="opamp.ws.push_throttled", reason="rate", agent_id=instance_uid.hex(),
                conn_id=connection.conn_id,
            ).warning(
                f"Config push to {instance_uid.hex()} throttled: connection "
                f"{connection.conn_id} is at {connection.bucket.rate:g} frames/s"
            )
            return False

    item = Outbound(
        payload,
        kind=kind,
        uid=instance_uid if kind == KIND_CONFIG else None,
        on_sent=on_sent,
    )
    if kind == KIND_CONFIG:
        previous = connection.config_slots.get(instance_uid)
        if previous is not None:
            # Latest revision wins: the older frame is skipped by the writer and its
            # `on_sent` never fires, so it never consumes a delivery attempt.
            previous.superseded = True
        connection.config_slots[instance_uid] = item
        _config_in_flight += 1
        OPAMP_WS_CONFIG_PENDING.set(_config_in_flight)

    return _enqueue(connection.queue, item)


def push_blocker(
    instance_uid: bytes, connection: Optional[Connection] = None
) -> Optional[str]:
    """Why a push to this uid would be refused, or None if it may be attempted.

    Both entry points ask this *before* building a coroutine (or a frame), so a dead
    or mis-bound socket costs a dict lookup instead of a thread hop, and `offline`
    and `refused` cannot drift between them.
    """
    if connection is None:
        connection = REGISTRY.handle_for(instance_uid)
    if connection is None or connection.closed:
        return BLOCKER_OFFLINE
    if REGISTRY.principal_for(instance_uid) != connection.principal_id:
        return BLOCKER_FOREIGN_PRINCIPAL
    return None


def send_to_agent_threadsafe(
    instance_uid: bytes,
    payload: bytes,
    *,
    kind: str = "frame",
    on_sent: Optional[Callable[[], None]] = None,
    timeout: Optional[float] = None,
) -> bool:
    """`send_to_agent()` for callers that are not the event loop.

    Every push route is a sync `def`, so FastAPI runs it in a worker thread, and
    `asyncio.Queue.put_nowait` is not thread-safe. This hops onto the connection's
    own loop and waits (bounded) for the answer, which keeps "was it queued?" honest
    instead of fire-and-forget.

    Calling it *from* the loop of the connection it targets would deadlock, so that
    is a `RuntimeError` (await the coroutine instead). A closed loop, a timeout or a
    vanished connection all answer False, which the caller treats as "delivered on
    the next poll".
    """
    connection = REGISTRY.handle_for(instance_uid)
    blocker = push_blocker(instance_uid, connection)
    if blocker is not None:
        OPAMP_WS_PUSH_SENT.labels(
            outcome="refused" if blocker == BLOCKER_FOREIGN_PRINCIPAL else "offline"
        ).inc()
        return False
    if connection is None:  # pragma: no cover - push_blocker narrows this
        OPAMP_WS_PUSH_SENT.labels(outcome="offline").inc()
        return False

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is connection.loop:
        raise RuntimeError(
            "send_to_agent_threadsafe() cannot be called from the target connection's "
            "event loop; await send_to_agent() instead"
        )

    wait = push_wait_seconds() if timeout is None else timeout
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_to_agent(instance_uid, payload, kind=kind, on_sent=on_sent),
            connection.loop,
        )
        return bool(future.result(wait))
    except (TimeoutError, RuntimeError):
        # Loop closed or the frame could not be accepted in time. Not an error: the
        # stored desired state is the source of truth.
        OPAMP_WS_PUSH_SENT.labels(outcome="offline").inc()
        logger.bind(
            event="opamp.ws.push_unavailable", agent_id=instance_uid.hex(),
            conn_id=connection.conn_id,
        ).warning(
            f"Could not queue a push to {instance_uid.hex()} within {wait:g}s; "
            "it will be delivered on the next status report"
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive
        OPAMP_WS_PUSH_SENT.labels(outcome="offline").inc()
        logger.bind(event="opamp.ws.push_failed", agent_id=instance_uid.hex()).warning(
            f"Push to {instance_uid.hex()} failed: {exc}"
        )
        return False


async def serve(
    websocket: WebSocket,
    process: Callable[..., ProcessedMessage],
    registry: ConnectionRegistry | None = None,
    principal: object | None = None,
) -> None:
    """Serve one accepted WebSocket connection until it closes.

    `process` is the shared AgentToServer handler, called as
    `process(payload, principal, transport="ws")`. Importing the FastAPI app here
    would create a cycle, so it is injected. `principal` is the authenticated
    identity from the handshake (#94); it is `None` in `off` mode, where behaviour
    is unchanged.

    #108: one long-lived `opamp.ws.connection` span per socket (it is the only
    place that knows the close code and which agents were bound), and every log
    line inside carries `conn_id` via the loguru context. A heartbeat message is
    still DEBUG/message-level; connection open and close are INFO.

    #102: the queue is registered with the registry *before* the first frame is
    read, so an agent can be pushed to the instant it is bound, and it is unregistered
    (and drained) in the `finally` so a closed socket can never accumulate frames or
    leave the in-flight gauge non-zero.

    `registry` is injectable so a test can own the map, but the push path
    (`send_to_agent`, `deliver_now`) always resolves uids through the process-wide
    `REGISTRY`: an injected registry therefore serves frames normally and is simply
    not pushable. Production passes nothing, so the two are the same object.
    """
    registry = REGISTRY if registry is None else registry
    principal_id = getattr(principal, "agent_id", None)
    conn_id = registry.new_id()
    queue: asyncio.Queue = asyncio.Queue(maxsize=outbound_queue_depth())
    connection = registry.open(conn_id, queue, asyncio.get_running_loop(), principal_id)
    writer = asyncio.create_task(_writer(websocket, connection))
    OPAMP_WS_CONNECTIONS.inc()
    # WebSocket close code 1000 = normal closure, 1001 = going away.
    close_code = 1000
    started = time.monotonic()

    with tracing.span(
        "opamp.ws.connection", conn_id=conn_id, principal=principal_id, transport="ws"
    ) as ws_span:
        with logger.contextualize(conn_id=conn_id, transport="ws"):
            logger.bind(
                event="opamp.ws.connected", conn_id=conn_id, principal=principal_id
            ).info(f"OpAMP WebSocket connection {conn_id} opened")
            try:
                close_code = await _serve_frames(
                    websocket, process, registry, principal, principal_id, connection
                )
            except Exception:
                close_code = 1011
                logger.bind(
                    event="opamp.ws.connection_failed", conn_id=conn_id
                ).exception(f"OpAMP WebSocket connection {conn_id} failed")
            finally:
                registry.close(conn_id)
                writer.cancel()
                try:
                    await writer
                except (asyncio.CancelledError, Exception):
                    pass
                _drain(connection, "closed")
                bound = registry.unbind_connection(conn_id)
                OPAMP_WS_CONNECTIONS.dec()
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                tracing.set_span_attributes(
                    ws_span,
                    close_code=close_code,
                    agents=[uid.hex() for uid in bound],
                    duration_ms=duration_ms,
                )
                logger.bind(
                    event="opamp.ws.disconnected",
                    conn_id=conn_id,
                    close_code=close_code,
                    agents=len(bound),
                    duration_ms=duration_ms,
                ).info(
                    f"OpAMP WebSocket connection {conn_id} closed ({close_code})"
                )


async def _serve_frames(
    websocket: WebSocket,
    process: Callable[..., ProcessedMessage],
    registry: ConnectionRegistry,
    principal: object | None,
    principal_id: str | None,
    connection: Connection,
) -> int:
    """The receive loop; returns the close code it finished with."""
    conn_id = connection.conn_id
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect as exc:
            return getattr(exc, "code", None) or 1000

        if message["type"] == "websocket.disconnect":
            return message.get("code") or 1000

        frame = message.get("bytes")
        if frame is None:
            # OpAMP is binary-only; a text frame is a protocol error.
            logger.bind(
                event="opamp.ws.frame_rejected", conn_id=conn_id, reason="non_binary"
            ).warning(f"Connection {conn_id}: non-binary WebSocket frame")
            OPAMP_WS_MESSAGES.labels(direction="received", outcome="malformed").inc()
            await _close(websocket, CLOSE_PROTOCOL_ERROR)
            return CLOSE_PROTOCOL_ERROR

        try:
            payload = decode_message(frame, max_bytes=message_limit())
        except MessageTooBig as exc:
            logger.bind(
                event="opamp.ws.frame_rejected", conn_id=conn_id, reason="too_big"
            ).warning(f"Connection {conn_id}: {exc}")
            OPAMP_WS_MESSAGES.labels(direction="received", outcome="too_big").inc()
            await _close(websocket, CLOSE_MESSAGE_TOO_BIG)
            return CLOSE_MESSAGE_TOO_BIG
        except MalformedFrame as exc:
            logger.bind(
                event="opamp.ws.frame_rejected", conn_id=conn_id, reason="malformed"
            ).warning(f"Connection {conn_id}: malformed frame: {exc}")
            OPAMP_WS_MESSAGES.labels(direction="received", outcome="malformed").inc()
            await _close(websocket, CLOSE_PROTOCOL_ERROR)
            return CLOSE_PROTOCOL_ERROR

        OPAMP_WS_MESSAGES.labels(direction="received", outcome="ok").inc()
        try:
            # `transport` is observability only (#108): the shared handler tags its
            # log line and span with which transport carried the message.
            result = process(payload, principal, transport="ws")
        except agent_auth.AgentAuthError as exc:
            # #94: identity, not the message body, decides who may speak. The
            # handler refused before applying anything, so state is untouched.
            logger.bind(
                event="agent.auth.failed", conn_id=conn_id, reason=exc.reason
            ).warning(
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
            return CLOSE_POLICY_VIOLATION

        if result.instance_uid:
            if result.disconnected:
                registry.unbind(conn_id, result.instance_uid)
            else:
                registry.bind(conn_id, result.instance_uid, principal_id)

        try:
            frame_out = encode_message(result.response, max_bytes=message_limit())
        except MessageTooBig as exc:
            # Spec: MUST NOT send an oversized ServerToAgent.
            logger.bind(event="opamp.ws.send_refused", conn_id=conn_id).error(
                f"Connection {conn_id}: refusing to send response: {exc}"
            )
            OPAMP_WS_MESSAGES.labels(direction="sent", outcome="too_big").inc()
            continue

        # The reply is queued before the follow-up, so a `post_reply` config frame
        # can never overtake the message it was triggered by (#102).
        _enqueue(connection.queue, Outbound(frame_out))

        if result.post_reply is not None:
            await _run_post_reply(result, registry, connection)


async def _run_post_reply(
    result: ProcessedMessage,
    registry: ConnectionRegistry,
    connection: Connection,
) -> None:
    """Run a handler's `post_reply` hook and enqueue whatever it produces.

    A hook that raises is logged and swallowed: a rollout rule must never be able to
    kill a live agent's socket. The frame goes through `send_to_agent()`, so it obeys
    the same binding, posture and rate checks as any other push.
    """
    try:
        follow_up = result.post_reply()
    except Exception:
        logger.bind(
            event="opamp.ws.post_reply_failed", conn_id=connection.conn_id
        ).exception("post_reply hook failed; continuing")
        return
    if not follow_up:
        return
    uid = result.instance_uid or b""
    try:
        frame_out = encode_message(follow_up, max_bytes=message_limit())
    except MessageTooBig as exc:
        logger.bind(
            event="opamp.ws.post_reply_too_big", conn_id=connection.conn_id
        ).error(f"post_reply frame refused: {exc}")
        return
    await send_to_agent(uid, frame_out, kind=KIND_CONFIG)
