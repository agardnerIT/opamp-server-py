"""Overload shedding (#78): 503 + `Retry-After`, and the WebSocket equivalent.

Spec §Throttling says the server MAY refuse work with 503/429 and a `Retry-After`
(the minimum recommended interval is 30 s), and on the WebSocket transport MAY answer
`ServerErrorResponse{type: UNAVAILABLE, retry_info{retry_after_nanoseconds}}`.
opamp-go already implements both halves, so these tests pin the server's half:

* disabled by default — nothing changes for anyone who has not opted in;
* overloaded → 503 with `Retry-After` ≥ 30, and **no** state change;
* `/health`, `/metrics` and admin routes stay 200 while agents are being refused;
* an agent that is being throttled is not reaped as stale (#69);
* the gate releases as soon as the lag does — no sticky grace period;
* the WebSocket path refuses the *upgrade* with 503 + `Retry-After` (not an
  in-band refusal: the reference client turns that into a reconnect storm — see
  `test_an_open_socket_is_not_cut_off_mid_stream`);
* both paths are counted.

The overload signal is event-loop lag, which the sampler measures. Every test that
needs overload calls `throttle.record_sample()` rather than trying to make a machine
slow: the measurement is one number and the gate is a pure function of it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import prometheus_client as prom_client
import pytest
from starlette.testclient import WebSocketDenialResponse

from proto import opamp_pb2
from server import throttle
from server.framing import decode_message, encode_message
from tests.support import anonymous
from server.state import AGENT_REGISTRY, AgentState
from tests.conformance.harness import opamp_send

UID = bytes.fromhex("78" * 16)


def message(uid: bytes = UID) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = True
    return msg.SerializeToString()


def frame(body: bytes) -> bytes:
    return encode_message(body)


def sample(name: str, labels: dict) -> float:
    value = prom_client.REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


@pytest.fixture(autouse=True)
def clean_throttle(monkeypatch):
    """Each test starts from a cold, disabled gate with no leaked lag."""
    for name in (
        "OPAMP_THROTTLE_ENABLED",
        "OPAMP_THROTTLE_MAX_LAG_SECONDS",
        "OPAMP_THROTTLE_RETRY_AFTER_SECONDS",
        "OPAMP_THROTTLE_REAPER_GRACE_SECONDS",
        "OPAMP_AGENT_AUTH",
        "OPAMP_WS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture
def throttling(monkeypatch):
    """The gate is on and the loop is measurably behind."""
    monkeypatch.setenv("OPAMP_THROTTLE_ENABLED", "true")
    throttle.record_sample(5.0)
    return throttle


# ------------------------------------------------------------------ default off


def test_disabled_by_default_accepts_everything(client):
    """An operator who never opted in must not see a single 503, however slow."""
    throttle.record_sample(60.0)  # a minute behind; irrelevant while disabled
    assert throttle.should_shed() is False
    response = opamp_send(client, message())
    assert response.status_code == 200
    assert AGENT_REGISTRY.get(UID.hex()) is not None
    assert client.get("/health").json()["throttle"]["enabled"] is False


def test_defaults_are_off_thirty_seconds_and_one_second_of_lag(monkeypatch):
    assert throttle.enabled() is False
    assert throttle.retry_after_seconds() == 30
    assert throttle.max_lag_seconds() == 1.0


# ------------------------------------------------------------------- HTTP 503


def test_overload_returns_503_with_retry_after_and_changes_nothing(client, throttling):
    before = sample("opamp_throttled_requests_total", {"transport": "http"})

    response = opamp_send(client, message())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    # No `ServerToAgent`: there is nothing to say in-band on plain HTTP, and a
    # half-message would invite a client to parse it as an ack.
    assert response.content == b""
    # Nothing was registered, so a throttled agent is not a new agent and does not
    # fire `new_agent`; its heartbeat simply did not advance this poll.
    assert AGENT_REGISTRY.count == 0
    assert sample("opamp_throttled_requests_total", {"transport": "http"}) == before + 1


def test_retry_after_is_never_below_the_spec_minimum(monkeypatch):
    """The spec recommends 30 s; a smaller configured value is raised, not honoured."""
    monkeypatch.setenv("OPAMP_THROTTLE_RETRY_AFTER_SECONDS", "5")
    assert throttle.retry_after_seconds() == 30

    monkeypatch.setenv("OPAMP_THROTTLE_RETRY_AFTER_SECONDS", "120")
    assert throttle.retry_after_seconds() == 120
    assert throttle.headers() == {"Retry-After": "120"}


def test_retry_after_header_follows_configuration(client, monkeypatch):
    monkeypatch.setenv("OPAMP_THROTTLE_ENABLED", "true")
    monkeypatch.setenv("OPAMP_THROTTLE_RETRY_AFTER_SECONDS", "90")
    throttle.record_sample(5.0)
    assert opamp_send(client, message()).headers["Retry-After"] == "90"


def test_threshold_is_configurable(client, monkeypatch):
    monkeypatch.setenv("OPAMP_THROTTLE_ENABLED", "true")
    monkeypatch.setenv("OPAMP_THROTTLE_MAX_LAG_SECONDS", "2")
    throttle.record_sample(1.5)
    assert opamp_send(client, message()).status_code == 200
    throttle.record_sample(2.5)
    assert opamp_send(client, message()).status_code == 503


def test_gate_releases_the_moment_the_loop_catches_up(client, throttling):
    """A sticky gate would turn `Retry-After` into a self-inflicted outage."""
    assert opamp_send(client, message()).status_code == 503

    throttle.record_sample(0.0)

    assert throttle.should_shed() is False
    assert opamp_send(client, message()).status_code == 200


def test_monitoring_and_admin_routes_are_never_throttled(client, throttling):
    """Throttling your own monitoring turns a slow server into a blind one."""
    assert opamp_send(client, message()).status_code == 503

    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/agents").status_code == 200
    assert client.get("/auth/status").status_code == 200

    health = client.get("/health").json()["throttle"]
    assert health["enabled"] is True
    assert health["shedding"] is True
    assert health["lag_seconds"] >= 1.0


def test_refusal_happens_before_the_body_is_read(client, throttling, monkeypatch):
    """A shedding server must not decompress and parse the message it refuses."""
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "16")
    response = opamp_send(client, message())
    assert response.status_code == 503  # not 413: the body was never read


def test_identity_is_checked_before_load(client, throttling, monkeypatch):
    """401 stays 401 under overload: a bad credential is not a capacity problem."""
    response = opamp_send(client, message(), headers=anonymous())
    assert response.status_code == 401
    assert "Retry-After" not in response.headers


# ------------------------------------------------------- interaction with #69


class _NullStore:
    def delete(self, agent_id):  # noqa: ARG002 - interface only
        return None

    def upsert(self, agent_id, entry):  # noqa: ARG002
        return None

    def get(self, agent_id):  # noqa: ARG002
        return None


def _stale_agent(monkeypatch, name: str = "overload-reaper-agent") -> AgentState:
    import server.main as server_main

    monkeypatch.setattr(server_main, "_METRICS_STORE", _NullStore())
    monkeypatch.setattr(server_main, "send_stale_agent_alert", lambda agent_id: True)
    # Long enough to be past even a generous learned timeout.
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    state = AgentState(
        instance_uid=bytes.fromhex("78" * 15 + "01"),
        agent_id=bytes.fromhex("78" * 15 + "01").hex(),
        connected_at=started,
        last_heartbeat=started,
    )
    AGENT_REGISTRY.register(state.agent_id, state, persist=False)
    return state


def test_reaper_stands_down_while_throttling(client, throttling, monkeypatch):
    """#69: an agent told to retry in 30 s is silent on purpose, not dead."""
    import server.main as server_main

    state = _stale_agent(monkeypatch)
    before = sample("opamp_stale_reaper_deferrals_total", {})

    assert server_main.cleanup_stale_agents() == []
    assert AGENT_REGISTRY.get(state.agent_id) is state
    assert sample("opamp_stale_reaper_deferrals_total", {}) == before + 1

    AGENT_REGISTRY.remove(state.agent_id)


def test_reaper_holds_off_through_the_grace_window_then_resumes(monkeypatch):
    """Refusals are remembered for a grace period, but not forever."""
    import server.main as server_main

    monkeypatch.setenv("OPAMP_THROTTLE_ENABLED", "true")
    monkeypatch.setenv("OPAMP_THROTTLE_REAPER_GRACE_SECONDS", "10")
    state = _stale_agent(monkeypatch)

    throttle.record_sample(0.0)  # caught up...
    throttle.mark_throttled(throttle.TRANSPORT_HTTP)  # ...but a 503 just went out
    assert throttle.should_shed() is False
    assert throttle.hold_reaper() is True  # the agent that got it is still backing off

    assert server_main.cleanup_stale_agents() == []
    assert AGENT_REGISTRY.get(state.agent_id) is state

    # Past the grace window the reaper is allowed to believe silence again.
    monkeypatch.setattr(
        throttle, "_last_overload_at", time.monotonic() - 11
    )
    assert throttle.hold_reaper() is False
    assert server_main.cleanup_stale_agents() == [state.agent_id]
    assert AGENT_REGISTRY.get(state.agent_id) is None


def test_reaper_is_unaffected_when_the_gate_is_off(monkeypatch):
    import server.main as server_main

    state = _stale_agent(monkeypatch)
    # A tripped threshold with the feature disabled is just a number.
    throttle.record_sample(60.0)
    assert throttle.hold_reaper() is False
    assert server_main.cleanup_stale_agents() == [state.agent_id]


# ----------------------------------------------------------------- WebSocket


def test_websocket_upgrade_is_denied_with_503_and_retry_after(
    client, throttling, monkeypatch
):
    """503 on the handshake is the backoff a real WS client actually honours."""
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    before = sample("opamp_throttled_requests_total", {"transport": "ws"})

    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/v1/opamp"):
            pass  # pragma: no cover - the upgrade never completes

    assert denied.value.status_code == 503
    assert denied.value.headers["retry-after"] == "30"
    # Nothing was registered: a throttled agent is not a new agent.
    assert AGENT_REGISTRY.count == 0
    assert sample("opamp_throttled_requests_total", {"transport": "ws"}) == before + 1


def test_an_open_socket_is_not_cut_off_mid_stream(client, throttling, monkeypatch):
    """Overload is shed at the handshake, never at an established socket.

    Measured against `otelcol-contrib` 0.162 (opamp-go v0.24): an in-band
    `UNAVAILABLE` makes the client tear the socket down and reconnect immediately,
    ignoring `retry_info` — ~300 reconnects/s/agent, which turns a throttle into an
    amplifier. `Retry-After` on a refused upgrade *is* honoured (30.0 s apart). A
    socket that is already open therefore keeps being served; its next reconnect is
    what gets shed.
    """
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    throttle.record_sample(0.0)

    with client.websocket_connect("/v1/opamp") as ws:
        throttle.record_sample(60.0)  # overload arrives mid-stream
        assert throttle.should_shed() is True
        ws.send_bytes(frame(message()))
        response = opamp_pb2.ServerToAgent()
        response.ParseFromString(decode_message(ws.receive_bytes()))

    assert response.error_response.type == 0  # served, not refused
    assert AGENT_REGISTRY.get(UID.hex()) is not None


def test_websocket_is_served_normally_once_the_load_drops(
    client, throttling, monkeypatch
):
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
    throttle.record_sample(0.0)
    with client.websocket_connect("/v1/opamp") as ws:
        ws.send_bytes(frame(message()))
        response = opamp_pb2.ServerToAgent()
        response.ParseFromString(decode_message(ws.receive_bytes()))
    assert response.error_response.type == 0  # no error_response
    assert AGENT_REGISTRY.get(UID.hex()) is not None


# -------------------------------------------------------------------- sampler


def test_sampler_records_lag_samples(monkeypatch):
    """The real measurement loop, with `record_sample` captured instead of timed."""
    seen: list = []
    monkeypatch.setattr(throttle, "record_sample", seen.append)
    monkeypatch.setattr(throttle, "SAMPLE_INTERVAL_SECONDS", 0.01)

    async def run():
        throttle.start()
        await asyncio.sleep(0.1)
        throttle.stop()

    asyncio.run(run())

    assert len(seen) >= 3
    assert all(lag >= 0.0 for lag in seen)
