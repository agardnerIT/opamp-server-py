"""Stale-agent expiry follows the agent's own cadence (#69).

The old rule was `now - last_heartbeat > 60s`, which is shorter than a
`polling_interval` an operator is free to set — so a slow-but-healthy collector was
deleted and re-registered on every cycle, firing `stale_agent` + `new_agent` alerts
each time and losing its persisted metrics.

Every test drives a controllable clock: nothing sleeps, and nothing depends on how
fast the machine is.
"""

from datetime import datetime, timedelta, timezone

import pytest

import server.main as server_main
import server.state as server_state
from server.state import AGENT_REGISTRY, AgentState

FLOOR = 180.0  # AGENT_TIMEOUT_SECONDS default
MARGIN = 3.0
FIRST_CONTACT = 900.0


@pytest.fixture
def agent(monkeypatch):
    """One connected agent, isolated from the rest of the registry."""
    monkeypatch.setattr(server_main, "_METRICS_STORE", _NullStore())
    monkeypatch.setattr(server_main, "send_stale_agent_alert", _RecordingAlert())
    monkeypatch.setattr(server_main, "send_new_agent_alert", lambda agent_id: True)
    monkeypatch.setattr(server_main, "AGENT_TIMEOUT_SECONDS", FLOOR)
    monkeypatch.setattr(server_main, "AGENT_TIMEOUT_INTERVAL_MARGIN", MARGIN)
    monkeypatch.setattr(server_main, "AGENT_FIRST_CONTACT_SECONDS", FIRST_CONTACT)

    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = AgentState(
        instance_uid=bytes.fromhex("00" * 15 + "01"),
        agent_id="00" * 15 + "01",
        connected_at=started,
        last_heartbeat=started,
    )
    AGENT_REGISTRY.register(state.agent_id, state, persist=False)
    try:
        yield state, started
    finally:
        AGENT_REGISTRY.remove(state.agent_id)
        _RecordingAlert.sent.clear()


class _NullStore:
    """The metrics store is a SQLite write; these tests are about the clock."""

    def delete(self, agent_id):  # noqa: ARG002 - interface only
        return None

    def upsert(self, agent_id, entry):  # noqa: ARG002 - interface only
        return None

    def get(self, agent_id):  # noqa: ARG002 - interface only
        return None


class _RecordingAlert:
    sent: list = []

    def __call__(self, agent_id):
        _RecordingAlert.sent.append(agent_id)
        return True, "queued"


def _report_every(state, start, interval, count, offset=0):
    """Feed `count` reports at `interval` seconds; return the time of the last one."""
    now = start
    for _ in range(count):
        now += timedelta(seconds=interval)
        state.record_report(now)
    return now + timedelta(seconds=offset)


def test_120s_poller_stays_connected_for_ten_minutes(agent):
    """The bug: at 120s polling the fixed 60s timeout deleted the agent every cycle."""
    state, start = agent
    now = start
    for _ in range(5):  # 10 minutes of a collector polling every 120s
        now += timedelta(seconds=120)
        state.record_report(now)
        assert server_main.cleanup_stale_agents(now=now) == []
    assert AGENT_REGISTRY.get(state.agent_id) is state
    assert _RecordingAlert.sent == []


def test_silent_agent_is_removed_after_about_three_intervals(agent):
    state, start = agent
    last = _report_every(state, start, interval=120, count=4)
    assert state.report_interval == pytest.approx(120, abs=1)

    # Two missed polls is still a blip, not a death: nothing happens yet.
    assert server_main.cleanup_stale_agents(now=last + timedelta(seconds=240)) == []
    assert AGENT_REGISTRY.get(state.agent_id) is not None

    # The third is the boundary: silence > 3 x 120s.
    assert server_main.cleanup_stale_agents(now=last + timedelta(seconds=361)) == [
        state.agent_id
    ]
    assert AGENT_REGISTRY.get(state.agent_id) is None
    assert _RecordingAlert.sent == [state.agent_id]


def test_slow_but_healthy_agent_never_alerts(agent):
    """A 10-minute poller must never be treated as gone, however many cycles pass."""
    state, start = agent
    now = start
    for _ in range(12):  # two hours at one report per 10 minutes
        now += timedelta(seconds=600)
        state.record_report(now)
        assert server_main.cleanup_stale_agents(now=now) == []
        # ...including the whole gap before each report, sampled every 15s.
        for tick in range(1, 40):
            between = now - timedelta(seconds=600) + timedelta(seconds=tick * 15)
            assert server_main.cleanup_stale_agents(now=between) == []

    assert _RecordingAlert.sent == []
    assert AGENT_REGISTRY.get(state.agent_id) is state


def test_first_contact_grace_covers_the_first_gap(agent):
    """Before one full gap is seen, a silent agent gets the first-contact window."""
    state, start = agent
    assert state.report_samples == 0
    assert state.timeout_seconds(FLOOR, MARGIN, FIRST_CONTACT) == FIRST_CONTACT

    # Silent from registration: not removed at 899s, removed at 901s.
    assert server_main.cleanup_stale_agents(now=start + timedelta(seconds=899)) == []
    assert server_main.cleanup_stale_agents(now=start + timedelta(seconds=901)) == [
        state.agent_id
    ]


def test_one_sample_is_not_enough_to_trust(agent):
    """A restart measures its first gap from a persisted heartbeat, not a real poll."""
    state, start = agent
    state.record_report(start + timedelta(seconds=90))
    assert state.report_samples < 2
    assert state.timeout_seconds(FLOOR, MARGIN, FIRST_CONTACT) == FIRST_CONTACT


def test_floor_protects_fast_agents_and_survives_jitter(agent):
    """An agent reporting every 5s is still given the floor, not 15s."""
    state, start = agent
    now = _report_every(state, start, interval=5, count=10)
    assert state.timeout_seconds(FLOOR, MARGIN, FIRST_CONTACT) == FLOOR
    assert server_main.cleanup_stale_agents(now=now + timedelta(seconds=179)) == []
    assert server_main.cleanup_stale_agents(now=now + timedelta(seconds=181)) == [
        state.agent_id
    ]


def test_two_messages_in_one_batch_are_not_a_cadence(agent):
    """A gap under a second is an artefact, not a polling interval."""
    state, start = agent
    state.record_report(start + timedelta(milliseconds=200))
    state.record_report(start + timedelta(milliseconds=400))
    assert state.report_samples == 0
    assert state.report_interval is None


def test_estimate_is_smoothed_not_replaced_by_one_outlier(agent):
    """A single long gap (a paused VM, say) must not hand the agent a huge window."""
    state, start = agent
    now = _report_every(state, start, interval=60, count=5)
    assert state.report_interval == pytest.approx(60, abs=1)

    state.record_report(now + timedelta(seconds=600))  # one very late report
    # 0.3 weight: 60 + 0.3 x (600 - 60) = 222, not 600.
    assert state.report_interval == pytest.approx(222, abs=2)
    assert state.timeout_seconds(FLOOR, MARGIN, FIRST_CONTACT) == pytest.approx(666, abs=6)


def test_message_path_records_the_report(agent):
    """`_apply_status_report` must keep the heartbeat moving on every message."""
    state, start = agent
    monkey = pytest.MonkeyPatch()
    try:
        fixed = start + timedelta(seconds=42)
        # `record_report` reads the clock from `server.state`, not `server.main`.
        monkey.setattr(server_state, "utcnow", lambda: fixed)
        server_main._apply_status_report(state.agent_id, {"health": {"healthy": True}})
    finally:
        monkey.undo()
    assert state.last_heartbeat == start + timedelta(seconds=42)
    assert state.healthy is True


def test_server_side_mutation_does_not_count_as_liveness(agent):
    """A compliance check on a dead agent must not keep it on the dashboard."""
    state, start = agent
    assert AGENT_REGISTRY.update(state.agent_id, persist=False, compliance={"ok": True})
    assert state.last_heartbeat == start  # unmoved by a server-side write


def test_removal_still_drops_metrics_and_fires_one_alert(agent, monkeypatch):
    state, start = agent
    deleted = []
    monkeypatch.setattr(server_main, "_METRICS_STORE", type(
        "S", (), {"delete": lambda self, agent_id: deleted.append(agent_id)}
    )())
    server_main.AGENT_METRICS[state.agent_id] = {"requests": 1}

    last = _report_every(state, start, interval=120, count=4)
    server_main.cleanup_stale_agents(now=last + timedelta(seconds=400))

    assert deleted == [state.agent_id]
    assert state.agent_id not in server_main.AGENT_METRICS
    assert _RecordingAlert.sent == [state.agent_id]


def test_api_exposes_the_timeout_that_applies(agent):
    """The number a human needs: 'why is this agent still/not listed?'."""
    from fastapi.testclient import TestClient

    state, start = agent
    _report_every(state, start, interval=300, count=3)
    detail = TestClient(server_main.app).get(f"/agent/{state.agent_id}").json()

    assert detail["report_interval_seconds"] == pytest.approx(300, abs=1)
    assert detail["stale_timeout_seconds"] == pytest.approx(900, abs=5)


def test_real_messages_drive_the_cadence_end_to_end():
    """The learning must happen on the wire, not only when a test calls it directly."""
    from fastapi.testclient import TestClient

    uid = bytes.fromhex("00112233445566778899aabbccddef01")

    def message():
        from proto import opamp_pb2

        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = uid
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        msg.health.healthy = True
        return msg.SerializeToString()

    client = TestClient(server_main.app)
    alerts = _RecordingAlert()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(server_main, "send_stale_agent_alert", alerts)
        monkey.setattr(server_main, "send_new_agent_alert", lambda agent_id: True)

        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = {"now": started}
        monkey.setattr(server_state, "utcnow", lambda: clock["now"])

        client.post("/v1/opamp", content=message(), headers={"Content-Type": "application/x-protobuf"})
        agent_id = next(a.agent_id for a in AGENT_REGISTRY.list_all() if a.instance_uid == uid)

        for _ in range(4):  # four polls, 120s apart
            clock["now"] += timedelta(seconds=120)
            client.post("/v1/opamp", content=message(), headers={"Content-Type": "application/x-protobuf"})
            assert server_main.cleanup_stale_agents(now=clock["now"]) == []

        state = AGENT_REGISTRY.get(agent_id)
        assert state.report_interval == pytest.approx(120, abs=1)
        assert alerts.sent == []
    finally:
        monkey.undo()
        for existing in list(AGENT_REGISTRY._agents.keys()):
            AGENT_REGISTRY.remove(existing)
        _RecordingAlert.sent.clear()
