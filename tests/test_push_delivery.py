"""Unsolicited server→agent push (#102): delivery semantics for both transports.

The rule this file exists to protect is one sentence from the issue: **a push is a
latency optimisation over the same decision, never a second delivery path** (D5). So
these tests are written against the acceptance criteria rather than the internals:

* a connected agent with no pending message receives an unsolicited config frame, and
  the frame advertises exactly what a reply advertises;
* an offline uid returns False from `send_to_agent()` with no exception and no
  buffering, the API says `queued`, and the config arrives on the next poll;
* two pushes to one socket deliver the newest revision only (the older frame is
  superseded, not sent twice);
* a full outbound queue is observable, does **not** record a delivery, and the config
  is re-offered by reconciliation on the next report;
* pushing to a uid bound to a different principal is refused and audited;
* heartbeats are not starved during a large rollout;
* `delivered` never becomes `applied` without the agent's own word.

The transport-level tests drive the real `serve()` writer with a fake socket, because
the interesting behaviour (supersede, queue-full, write-then-record) only exists once
there is a writer task. The API-level tests use `TestClient`'s real WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading

import prometheus_client as prom_client
import pytest

from proto import opamp_pb2
from server import admin_auth, agent_auth, crypto, remote_config, ws
from server.framing import decode_message, encode_message
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from server.ws import REGISTRY, ProcessedMessage
from tests.support import ADMIN, admin_headers, bearer

UID = bytes.fromhex("ab" * 16)
AGENT_ID = "ab" * 16
OTHER_UID = bytes.fromhex("cd" * 16)
OTHER_ID = "cd" * 16

CONFIG = {
    "": {
        "body": (
            "receivers:\n"
            "  otlp:\n"
            "    protocols:\n"
            "      grpc:\n"
            "exporters:\n"
            "  debug: {}\n"
            "service:\n"
            "  extensions: [opamp]\n"
            "  pipelines:\n"
            "    traces:\n"
            "      receivers: [otlp]\n"
            "      exporters: [debug]\n"
            "extensions:\n"
            "  opamp:\n"
            "    server:\n"
            "      ws:\n"
            "        endpoint: wss://opamp.example.com/v1/opamp\n"
        ),
        "content_type": "text/yaml",
    }
}

CONFIG_V2 = {
    "": {
        "body": CONFIG[""]["body"].replace("debug: {}", "debug:\n    verbosity: basic"),
        "content_type": "text/yaml",
    }
}


# ------------------------------------------------------------------- helpers


def agent_message(
    uid: bytes = UID,
    status: str | None = None,
    reported_hash_b64: str | None = None,
    capabilities: int | None = None,
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = (
        opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        if capabilities is None
        else capabilities
    )
    msg.health.healthy = True
    if status:
        msg.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
        )
        if reported_hash_b64:
            msg.remote_config_status.last_remote_config_hash = base64.b64decode(
                reported_hash_b64
            )
    return msg.SerializeToString()


def frame(msg_bytes: bytes) -> bytes:
    return encode_message(msg_bytes)


def recv(socket) -> opamp_pb2.ServerToAgent:
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(decode_message(socket.receive_bytes()))
    return reply


def counter(name: str, **labels) -> float:
    return prom_client.REGISTRY.get_sample_value(name, labels) or 0.0


def poll(client, token: str | None = None, uid: bytes = UID, **kwargs):
    """One HTTP status report, authenticated like a real agent (#94)."""
    response = client.post(
        "/v1/opamp",
        content=agent_message(uid=uid, **kwargs),
        headers=bearer(token) if token else {},
    )
    assert response.status_code == 200, response.text
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(response.content)
    return reply


@pytest.fixture(autouse=True)
def push_enabled(monkeypatch, tmp_path):
    """A secure posture with push on (#96), a real credential store (#94), no grace."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.delenv("CONFIG_PUSH_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("CONFIG_PUSH_RATE_PER_SECOND", raising=False)
    monkeypatch.delenv("CONFIG_PUSH_BURST", raising=False)
    crypto.reset_key_cache()
    REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    crypto.reset_key_cache()


@pytest.fixture
def token(client) -> str:
    return client.post(
        f"/agents/{AGENT_ID}/token", headers=admin_headers(), json={}
    ).json()["token"]


def push(client, config_map=None):
    return client.put(
        f"/agent/{AGENT_ID}/config",
        json={"config_map": config_map or CONFIG},
        headers=admin_headers(),
    )


# ------------------------------------------------- API level: unsolicited delivery


class TestUnsolicitedSend:
    def test_connected_agent_receives_a_push_with_no_pending_message(self, client, token):
        """The AC: an idle agent gets a config frame it did not ask for."""
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as socket:
            socket.send_bytes(frame(agent_message()))
            reply = recv(socket)
            assert not reply.HasField("remote_config")

            response = push(client)
            assert response.status_code == 200, response.text
            delivery = response.json()["delivery"]
            assert delivery["state"] == "delivered"
            assert delivery["channel"] == "push"

            # No `send_bytes` in between: this frame answers nothing.
            pushed = recv(socket)
            assert pushed.instance_uid == UID
            assert pushed.HasField("remote_config")
            assert bytes(pushed.remote_config.config_hash).hex() == response.json()["config_hash"]

        assignment = remote_config.REGISTRY.delivery_record(AGENT_ID)
        assert assignment["channel"] == "push"
        assert assignment["attempts"] == 1
        assert assignment["state"] == "delivered"
        assert assignment["last_status"] is None  # never inferred

    def test_push_frame_advertises_what_a_reply_advertises(self, client, token):
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as socket:
            socket.send_bytes(frame(agent_message()))
            reply = recv(socket)
            push(client)
            pushed = recv(socket)

        expected = remote_config.server_capabilities()
        assert reply.capabilities == expected
        assert pushed.capabilities == expected
        # A push answers nothing, so it must not request a full state report.
        assert not pushed.flags & opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState

    def test_offline_agent_is_queued_and_delivered_on_the_next_poll(self, client, token):
        """HTTP-only agents keep working: `queued` is a promise, not a failure."""
        poll(client, token)  # registers the agent over HTTP
        response = push(client)
        assert response.status_code == 200
        delivery = response.json()["delivery"]
        assert delivery["state"] == "queued"
        assert delivery["channel"] is None
        assert "next status report" in delivery["detail"] or "polling interval" in delivery["detail"]

        assignment = remote_config.REGISTRY.delivery_record(AGENT_ID)
        assert assignment["state"] == "queued"
        assert assignment["delivered_hash"] is None

        # Reconciliation: the desired state is what delivers, not the failed push.
        reported = poll(client, token)
        assert reported.HasField("remote_config")
        assignment = remote_config.REGISTRY.delivery_record(AGENT_ID)
        assert assignment["channel"] == "reply"
        assert assignment["state"] == "delivered"

    def test_deliver_now_is_idempotent_once_the_agent_has_it(self, client, token):
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as socket:
            socket.send_bytes(frame(agent_message()))
            recv(socket)
            pushed = push(client).json()
            recv(socket)

            # The agent acknowledges with our hash, then someone clicks "deliver now".
            poll(
                client,
                token,
                status="APPLIED",
                reported_hash_b64=remote_config.hash_b64(pushed["config_hash"]),
            )
            again = client.post(
                f"/agent/{AGENT_ID}/config/deliver", headers=admin_headers()
            )
        assert again.status_code == 200
        delivery = again.json()["delivery"]
        assert delivery["state"] == "unchanged"
        assert delivery["reason"] == "reported_hash_matches"
        assert remote_config.REGISTRY.delivery_record(AGENT_ID)["attempts"] == 1

    def test_delivery_state_walks_queued_delivered_applied(self, client, token):
        poll(client, token)
        pushed = push(client).json()
        detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
        assert detail["assignment"]["state"] == "queued"

        delivered = poll(client, token)
        assert delivered.HasField("remote_config")
        detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
        assert detail["assignment"]["state"] == "delivered"
        assert detail["assignment"]["channel"] == "reply"

        poll(
            client,
            token,
            status="APPLIED",
            reported_hash_b64=remote_config.hash_b64(pushed["config_hash"]),
        )
        detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
        assert detail["assignment"]["state"] == "applied"

    def test_an_unknown_agent_is_404_and_a_bad_uid_422(self, client):
        assert (
            client.post(f"/agent/{OTHER_ID}/config/deliver", headers=admin_headers()).status_code
            == 404
        )
        assert (
            client.post("/agent/not-a-uid/config/deliver", headers=admin_headers()).status_code
            == 422
        )

    def test_deliver_requires_operator_and_push_posture(self, client, token, monkeypatch):
        poll(client, token)
        # No credential at all: 401 (a `viewer` would be 403 — #95).
        assert client.post(f"/agent/{AGENT_ID}/config/deliver").status_code == 401
        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "false")
        assert (
            client.post(f"/agent/{AGENT_ID}/config/deliver", headers=admin_headers()).status_code
            == 404
        )


    def test_proposal_view_reports_the_same_delivery_state(self, client, token):
        """The listing uses an authoritative read, but the same derived vocabulary."""
        poll(client, token)
        proposal = client.post(
            "/config/revisions",
            json={"config_map": CONFIG, "scope": {"agent_id": AGENT_ID}},
            headers=admin_headers(),
        ).json()
        client.post(
            f"/config/revisions/{proposal['proposal_id']}/approve",
            json={},
            headers=admin_headers(),
        )
        view = client.get(
            f"/config/revisions/{proposal['proposal_id']}", headers=admin_headers()
        ).json()["revisions"][0]["delivery"]
        assert view["current"] is True
        assert view["state"] == "queued"

        poll(client, token)  # the next report carries it
        view = client.get(
            f"/config/revisions/{proposal['proposal_id']}", headers=admin_headers()
        ).json()["revisions"][0]["delivery"]
        assert view["state"] == "delivered"
        assert view["channel"] == "reply"

    def test_fleet_apply_reports_a_partial_delivery_honestly(self, client, token):
        """A fleet push that reached half the fleet must not read as success."""
        other_id = "cd" * 16
        other_token = client.post(
            f"/agents/{other_id}/token", headers=admin_headers(), json={}
        ).json()["token"]
        poll(client, other_token, uid=OTHER_UID)  # HTTP-only agent

        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as socket:
            socket.send_bytes(frame(agent_message()))
            recv(socket)
            response = client.post(
                "/config/apply",
                json={
                    "config_map": CONFIG,
                    "agents": [AGENT_ID, other_id],
                    "confirm": True,
                },
                headers=admin_headers(),
            )
            assert response.status_code == 200, response.text
            pushed = recv(socket)

        assert pushed.HasField("remote_config")
        delivery = response.json()["delivery"]
        assert delivery["state"] == "partial"
        assert delivery["agents"] == {"delivered": 1, "queued": 1, "unchanged": 0}
        assert delivery["channel"] == "push"


class TestPrincipalBinding:
    def test_push_to_a_uid_bound_to_another_principal_is_refused_and_audited(
        self, client, token
    ):
        """#94/#102: the binding authorises the send, so a moved uid is not delivered to."""
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as socket:
            socket.send_bytes(frame(agent_message()))
            recv(socket)
            conn_id = REGISTRY.connection_for(UID)
            # Simulate a binding that moved (a stale entry, or an attempt to receive
            # another credential's config): refused, and the agent still gets its
            # config on its own next report.
            REGISTRY.bind(conn_id, UID, "someone-else")

            response = push(client)
            assert response.status_code == 200
            delivery = response.json()["delivery"]
            assert delivery["state"] == "queued"
            assert delivery["reason"] == "foreign_principal"
            assert remote_config.REGISTRY.delivery_record(AGENT_ID)["delivered_hash"] is None

        audit = client.get(
            "/audit", params={"action": "config.deliver"}, headers=admin_headers()
        ).json()
        refused = [
            entry
            for entry in audit["entries"]
            if entry.get("action") == "config.deliver" and entry.get("result") == "error"
        ]
        assert refused, "a refused delivery must be audited as an error"
        assert refused[0]["actor"] == "admin"
        assert "different principal" in json.dumps(refused[0]["detail_json"])


# --------------------------------------------- transport harness: the real writer


class FakeSocket:
    """Just enough of `starlette.WebSocket` for `serve()`.

    `gate` blocks the writer mid-send, which is how a test makes the outbound queue
    fill deterministically instead of hoping the scheduler cooperates.
    """

    def __init__(self, gate: asyncio.Event | None = None):
        self.sent: list[bytes] = []
        self.gate = gate
        self.client = None
        self._inbound: asyncio.Queue = asyncio.Queue()

    async def receive(self):
        item = await self._inbound.get()
        if item is None:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.message", "bytes": item}

    async def send_bytes(self, data: bytes) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - unused
        self._inbound.put_nowait(None)

    def send(self, frame: bytes) -> None:
        self._inbound.put_nowait(frame)

    def disconnect(self) -> None:
        self._inbound.put_nowait(None)


def bind_uid_process(replies: list[ProcessedMessage] | None = None):
    """A `process` stand-in: replies are empty frames and bind the uid to the socket."""

    def process(payload, principal=None, transport="http"):
        response = opamp_pb2.ServerToAgent()
        response.instance_uid = UID
        return ProcessedMessage(
            response.SerializeToString(),
            instance_uid=UID,
            post_reply=(lambda: replies.pop(0)) if replies else None,
        )

    return process


async def _settle(iterations: int = 40) -> None:
    """Let the serve task and the writer run (no sleeps, no flakiness)."""
    for _ in range(iterations):
        await asyncio.sleep(0)


class TestWriterAndQueue:
    def test_second_push_supersedes_the_queued_first(self):
        async def scenario():
            REGISTRY.reset()
            before = counter("opamp_ws_push_sent_total", outcome="superseded")
            socket = FakeSocket(gate=asyncio.Event())
            task = asyncio.create_task(ws.serve(socket, bind_uid_process(), registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()

            # Both land in the same tick, before the (blocked) writer can run.
            assert await ws.send_to_agent(UID, b"rev-1", kind=ws.KIND_CONFIG)
            assert await ws.send_to_agent(UID, b"rev-2", kind=ws.KIND_CONFIG)

            socket.gate.set()
            await _settle(80)
            socket.disconnect()
            await task
            return socket.sent, counter("opamp_ws_push_sent_total", outcome="superseded") - before

        sent, superseded = asyncio.run(scenario())
        assert superseded == 1
        # The reply to the agent's message, then the newest revision only.
        assert len(sent) == 2
        assert sent[1] == b"rev-2"

    def test_full_queue_is_observable_and_never_records_a_delivery(self, monkeypatch):
        # A big bucket so the *queue* is what refuses, and a small queue so it is
        # cheap to fill: the point is backpressure, not the rate limiter.
        monkeypatch.setenv("CONFIG_PUSH_BURST", "1000")
        monkeypatch.setenv("OPAMP_WS_OUTBOUND_QUEUE", "4")

        async def scenario():
            REGISTRY.reset()
            dropped_before = counter("opamp_ws_outbound_dropped_total")
            recorded: list[str] = []
            socket = FakeSocket(gate=asyncio.Event())
            task = asyncio.create_task(ws.serve(socket, bind_uid_process(), registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()

            connection = REGISTRY.handle_for(UID)
            # Depth 32 by default: block the writer, then overflow the queue.
            accepted, refused = 0, 0
            for index in range(40):
                ok = await ws.send_to_agent(
                    UID, f"rev-{index}".encode(), kind=ws.KIND_CONFIG,
                    on_sent=lambda: recorded.append("sent"),
                )
                accepted += ok
                refused += not ok
            depth = connection.queue.qsize()
            assert ws.config_in_flight() <= ws.outbound_queue_depth() + 1

            socket.gate.set()
            await _settle(200)
            socket.disconnect()
            await task
            return (
                accepted, refused, ws.config_in_flight(), depth, dropped_before, recorded,
                socket.sent,
            )

        accepted, refused, in_flight, depth, dropped_before, recorded, sent = asyncio.run(
            scenario()
        )
        # Backpressure is drop-oldest, not refusal: a full queue is bounded memory and
        # a counted drop, and every settled frame returns its in-flight slot.
        assert accepted == 40
        assert depth <= ws.outbound_queue_depth()
        assert in_flight == 0, "every accepted frame must be settled"
        assert counter("opamp_ws_outbound_dropped_total") > dropped_before
        # Latest revision wins: of the 40 accepted frames only the newest was written,
        # so exactly one delivery was recorded.
        assert recorded == ["sent"]
        assert sent[-1].startswith(b"rev-")

    def test_a_dropped_config_frame_does_not_consume_a_delivery_attempt(self, client, token):
        """The AC: the queue is bounded, and reconciliation — not the push — delivers.

        The property that matters is `delivered_at` staying NULL, because that is what
        makes the next status report offer the config again. A frame that was queued
        and then dropped (or superseded) proves nothing, so it must not count.
        """
        poll(client, token)
        record = remote_config.REGISTRY.push(
            AGENT_ID, CONFIG, "test"
        )
        assert remote_config.REGISTRY.delivery_record(AGENT_ID)["attempts"] == 0

        async def scenario():
            REGISTRY.reset()
            connection = REGISTRY.open(7, asyncio.Queue(maxsize=1), asyncio.get_running_loop())
            REGISTRY.bind(7, UID, None)
            connection.queue.put_nowait(ws.Outbound(b"occupies-the-only-slot"))
            recorded: list[str] = []
            first = await ws.send_to_agent(
                UID, b"config-a", kind=ws.KIND_CONFIG, on_sent=lambda: recorded.append("a")
            )
            second = await ws.send_to_agent(
                UID, b"config-b", kind=ws.KIND_CONFIG, on_sent=lambda: recorded.append("b")
            )
            # Nothing is written: drain it the way a closing socket would.
            from server.ws import _drain

            _drain(connection, "closed")
            return first, second, recorded, ws.config_in_flight()

        first, second, recorded, in_flight = asyncio.run(scenario())
        assert (first, second) == (True, True)
        assert recorded == [], "a frame that was never written recorded a delivery"
        assert in_flight == 0

        assignment = remote_config.REGISTRY.delivery_record(AGENT_ID)
        assert assignment["attempts"] == 0
        assert assignment["delivered_hash"] is None
        assert assignment["state"] == "queued"

        # Reconciliation: the agent's next report carries it, and *that* is a delivery.
        offer = remote_config.offer_for(
            AGENT_ID,
            opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig,
            {},
        )
        assert offer is not None
        assert bytes(offer.config_hash).hex() == record["config_hash"]
        assignment = remote_config.REGISTRY.delivery_record(AGENT_ID)
        assert assignment["attempts"] == 1
        assert assignment["channel"] == "reply"

    def test_post_reply_hook_frames_arrive_after_the_reply(self):
        """#102: a config frame triggered by a message must not overtake the reply."""
        follow_up = opamp_pb2.ServerToAgent()
        follow_up.instance_uid = UID
        follow_up.remote_config.config_hash = b"\x01\x02"

        async def scenario():
            REGISTRY.reset()
            process = bind_uid_process(replies=[follow_up.SerializeToString()])
            socket = FakeSocket()
            task = asyncio.create_task(ws.serve(socket, process, registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle(60)
            socket.disconnect()
            await task
            return socket.sent

        sent = asyncio.run(scenario())
        assert len(sent) == 2
        first = opamp_pb2.ServerToAgent()
        first.ParseFromString(decode_message(sent[0]))
        assert not first.HasField("remote_config")
        second = opamp_pb2.ServerToAgent()
        second.ParseFromString(decode_message(sent[1]))
        assert second.HasField("remote_config")

    def test_a_raising_post_reply_does_not_kill_the_socket(self):
        """A rollout rule must never be able to take a live agent offline."""

        def explode():
            raise RuntimeError("rollout rule blew up")

        def process(payload, principal=None, transport="http"):
            response = opamp_pb2.ServerToAgent()
            response.instance_uid = UID
            return ProcessedMessage(
                response.SerializeToString(), instance_uid=UID, post_reply=explode
            )

        async def scenario():
            REGISTRY.reset()
            socket = FakeSocket()
            task = asyncio.create_task(ws.serve(socket, process, registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()
            alive = REGISTRY.handle_for(UID) is not None
            socket.send(frame(agent_message()))  # the socket still works
            await _settle()
            socket.disconnect()
            await task
            return alive, len(socket.sent)

        alive, sent = asyncio.run(scenario())
        assert alive is True
        assert sent == 2

    def test_closed_connection_returns_false_and_buffers_nothing(self):
        async def scenario():
            REGISTRY.reset()
            socket = FakeSocket()
            task = asyncio.create_task(ws.serve(socket, bind_uid_process(), registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()
            connection = REGISTRY.handle_for(UID)
            socket.disconnect()
            await task
            return (
                await ws.send_to_agent(UID, b"frame", kind=ws.KIND_CONFIG),
                connection.queue.qsize(),
                ws.config_in_flight(),
            )

        sent, depth, in_flight = asyncio.run(scenario())
        assert sent is False
        assert depth == 0
        assert in_flight == 0

    def test_heartbeats_are_not_starved_during_a_large_rollout(self, monkeypatch):
        """100 config pushes on one shared socket must not delay a heartbeat's reply.

        Two guarantees, both enforced by the design rather than by luck: the token
        bucket caps how many config frames one connection can accept (so a rollout
        cannot flood a single queue), and a reply is never throttled — when the queue
        is full it evicts the *oldest* frame, which is a config frame that
        reconciliation will re-send, never the reply the agent is waiting for.
        """
        monkeypatch.setenv("CONFIG_PUSH_BURST", "20")
        monkeypatch.setenv("CONFIG_PUSH_RATE_PER_SECOND", "20")
        rollout = [bytes.fromhex(f"{index:032x}") for index in range(1, 101)]

        async def scenario():
            REGISTRY.reset()

            def process(payload, principal=None, transport="http"):
                response = opamp_pb2.ServerToAgent()
                response.instance_uid = UID
                return ProcessedMessage(response.SerializeToString(), instance_uid=UID)

            socket = FakeSocket()
            task = asyncio.create_task(ws.serve(socket, process, registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()
            connection = REGISTRY.handle_for(UID)
            # One socket carrying a whole fleet's agents (spec §WebSocket Message
            # Exchange), which is exactly when starvation would bite.
            for uid in rollout:
                REGISTRY.bind(connection.conn_id, uid, None)

            accepted = 0
            for index, uid in enumerate(rollout):
                accepted += await ws.send_to_agent(
                    uid, f"rev-{index}".encode(), kind=ws.KIND_CONFIG
                )
            # The heartbeat arrives *after* the rollout was queued.
            socket.send(frame(agent_message()))
            await _settle(400)
            sent = list(socket.sent)
            in_flight = ws.config_in_flight()
            socket.disconnect()
            await task
            return accepted, sent, in_flight

        accepted, sent, in_flight = asyncio.run(scenario())
        assert accepted <= ws.push_burst(), "one connection must not accept an unbounded roll"
        assert in_flight == 0
        # The last frame is the reply to the heartbeat that arrived after the rollout.
        heartbeat_reply = opamp_pb2.ServerToAgent()
        heartbeat_reply.ParseFromString(decode_message(sent[-1]))
        assert heartbeat_reply.instance_uid == UID
        assert not heartbeat_reply.HasField("remote_config")


class TestThreadsafeEntryPoint:
    def test_send_from_another_thread_reaches_the_connection(self):
        async def scenario():
            REGISTRY.reset()
            socket = FakeSocket()
            task = asyncio.create_task(ws.serve(socket, bind_uid_process(), registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()

            result: list[bool] = []

            def worker():
                result.append(ws.send_to_agent_threadsafe(UID, b"frame", kind=ws.KIND_CONFIG))

            thread = threading.Thread(target=worker)
            thread.start()
            while thread.is_alive():
                await asyncio.sleep(0.01)
            thread.join()
            await _settle(60)
            socket.disconnect()
            await task
            return result, socket.sent

        result, sent = asyncio.run(scenario())
        assert result == [True]
        assert sent[-1] == b"frame"

    def test_calling_it_from_the_target_loop_is_an_error(self):
        async def scenario():
            REGISTRY.reset()
            socket = FakeSocket()
            task = asyncio.create_task(ws.serve(socket, bind_uid_process(), registry=REGISTRY))
            await _settle()
            socket.send(frame(agent_message()))
            await _settle()
            try:
                ws.send_to_agent_threadsafe(UID, b"frame", kind=ws.KIND_CONFIG)
                raised = False
            except RuntimeError:
                raised = True
            socket.disconnect()
            await task
            return raised

        assert asyncio.run(scenario()) is True


# ----------------------------------------------------------------- metrics


class TestMetrics:
    def test_push_outcomes_are_counted(self, client, token):
        before_ok = counter("opamp_ws_push_sent_total", outcome="ok")
        before_offline = counter("opamp_ws_push_sent_total", outcome="offline")

        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as socket:
            socket.send_bytes(frame(agent_message()))
            recv(socket)
            push(client)
            recv(socket)

        assert counter("opamp_ws_push_sent_total", outcome="ok") == before_ok + 1

        # Same agent, now offline: the second push counts as offline, not ok.
        push(client, config_map=CONFIG_V2)
        assert counter("opamp_ws_push_sent_total", outcome="offline") == before_offline + 1

    def test_delivery_latency_is_observed_when_the_agent_reports_applied(self, client, token):
        """delivered → APPLIED, measured from the write, recorded once per revision."""
        poll(client, token)                      # register
        pushed = push(client).json()             # queued: no live connection
        assert poll(client, token).HasField("remote_config")  # delivered by the reply
        reported = remote_config.hash_b64(pushed["config_hash"])

        def samples(channel: str) -> float:
            return counter(
                "opamp_config_delivery_latency_seconds_count", channel=channel
            )

        before = samples("reply")
        poll(client, token, status="APPLIED", reported_hash_b64=reported)
        assert samples("reply") == before + 1
        # A heartbeat repeating APPLIED must not observe it again.
        poll(client, token, status="APPLIED", reported_hash_b64=reported)
        assert samples("reply") == before + 1
