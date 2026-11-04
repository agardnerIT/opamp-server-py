"""Staged rollout engine (#101): blast-radius caps, health gate, auto-pause, abort.

One test per acceptance criterion on the issue, plus the pure-function tests that make
those criteria assertable without sleeping: `plan_batches()` and `batch_gate()` take
their settings, their agent facts and their clock, so "the gate never opened" is a
statement about values rather than about wall time.
"""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest

from proto import opamp_pb2
from server import (
    admin_auth,
    agent_auth,
    audit,
    config_control,
    crypto,
    remote_config,
    rollout,
)
from tests.conformance.harness import handle_message as reply
from server.state import AGENT_REGISTRY, SQLiteCredentialStore, SQLiteRolloutStore, utcnow
from server.ws import REGISTRY as WS_REGISTRY
from tests.support import ADMIN, admin_headers


CONFIG = {
    "": {
        "body": (
            "receivers:\n"
            "  otlp:\n"
            "    protocols:\n"
            "      grpc:\n"
            "processors:\n"
            "  memory_limiter:\n"
            "    check_interval: 1s\n"
            "  batch: {}\n"
            "exporters:\n"
            "  debug: {}\n"
            "service:\n"
            "  extensions: [opamp]\n"
            "  telemetry:\n"
            "    logs:\n"
            "      level: info\n"
            "  pipelines:\n"
            "    traces:\n"
            "      receivers: [otlp]\n"
            "      processors: [memory_limiter, batch]\n"
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
        "body": CONFIG[""]["body"].replace("debug: {}", "debug:\n    verbosity: detailed"),
        "content_type": "text/yaml",
    }
}


def uid_for(index: int) -> bytes:
    return bytes.fromhex(f"{index:032x}")


def agent_id_for(index: int) -> str:
    return uid_for(index).hex()


def agent_message(
    agent_id: str,
    status: str | None = None,
    reported_hash_b64: str | None = None,
    error: str = "",
    healthy: bool = True,
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = bytes.fromhex(agent_id)
    msg.capabilities = (
        opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsHealth
    )
    msg.health.healthy = healthy
    if not healthy:
        # proto3 drops a false scalar from MessageToDict, so an unhealthy report needs
        # a second field or the server never sees a `health` block at all.
        msg.health.last_error = error or "unavailable"
    if status:
        msg.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
        )
        if reported_hash_b64:
            msg.remote_config_status.last_remote_config_hash = base64.b64decode(
                reported_hash_b64
            )
        msg.remote_config_status.error_message = error
    return msg.SerializeToString()


def poll(
    agent_id: str,
    status: str | None = None,
    hash_b64: str | None = None,
    healthy: bool = True,
    error: str = "",
) -> bool:
    """One agent poll; True when the reply carries a config."""
    return reply(
        agent_message(
            agent_id,
            status=status,
            reported_hash_b64=hash_b64,
            healthy=healthy,
            error=error,
        )
    ).HasField("remote_config")


def our_hash_b64(agent_id: str) -> str:
    record = remote_config.REGISTRY.get(agent_id) or {}
    return remote_config.hash_b64(record["config_hash"])


@pytest.fixture(autouse=True)
def push_enabled(monkeypatch, tmp_path):
    """A fully secure posture with push on (#96), encryption (#98) and no grace window."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setattr(rollout, "STORE", SQLiteRolloutStore(tmp_path / "rollouts.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    monk = monkeypatch
    for name in (
        "CONFIG_PUSH_CANARY_PERCENT",
        "CONFIG_PUSH_MAX_AGENTS_PER_BATCH",
        "CONFIG_PUSH_MAX_PERCENT_PER_BATCH",
        "CONFIG_PUSH_HEALTH_GATE_SECONDS",
        "CONFIG_PUSH_ROLLOUT_TTL_MINUTES",
        "CONFIG_PUSH_TICK_SECONDS",
    ):
        monk.delenv(name, raising=False)
    crypto.reset_key_cache()
    WS_REGISTRY.reset()
    config_control.reset_drift_alerts()
    rollout.reset_counters()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    WS_REGISTRY.reset()
    config_control.reset_drift_alerts()
    rollout.reset_counters()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    crypto.reset_key_cache()


# ---------------------------------------------------------------- pure functions


def test_batches_are_deterministic_and_bounded_by_both_caps():
    """Same targets → same batches; canary first; both caps honoured."""
    agents = [agent_id_for(index) for index in range(500)]
    first = rollout.plan_batches(
        agents,
        canary_percent_value=5.0,
        max_agents_per_batch_value=25,
        max_percent_per_batch_value=25.0,
    )
    shuffled = list(reversed(agents))
    second = rollout.plan_batches(
        shuffled,
        canary_percent_value=5.0,
        max_agents_per_batch_value=25,
        max_percent_per_batch_value=25.0,
    )
    assert first == second
    assert first[0] == sorted(agents)[:25]  # 5% of 500, sorted
    assert all(len(batch) <= 25 for batch in first)
    assert all(len(batch) <= 125 for batch in first)  # 25% of 500
    assert sum(len(batch) for batch in first) == 500
    # No agent appears twice: a rollout must not re-deliver by planning badly.
    assert len({agent for batch in first for agent in batch}) == 500


def test_absolute_cap_beats_relative_cap_on_a_big_fleet():
    batches = rollout.plan_batches(
        [agent_id_for(index) for index in range(100)],
        canary_percent_value=5.0,
        max_agents_per_batch_value=10,
        max_percent_per_batch_value=50.0,
    )
    assert batches[0] == sorted(agent_id_for(i) for i in range(100))[:5]
    assert all(len(batch) <= 10 for batch in batches)


def test_one_agent_always_produces_a_promotable_canary():
    """A 1% canary of a 3-agent fleet rounds up to one agent, and never to zero."""
    batches = rollout.plan_batches(
        [agent_id_for(index) for index in range(3)],
        canary_percent_value=1.0,
        max_agents_per_batch_value=25,
        max_percent_per_batch_value=25.0,
    )
    assert batches[0] and len(batches[0]) == 1
    assert sum(len(batch) for batch in batches) == 3


def test_the_gate_requires_applied_with_our_hash_not_naive_equality():
    """`APPLIED` with somebody else's hash is not evidence, and neither is ours with
    a different status."""
    ours = "a" * 64
    theirs = "b" * 64
    applied_ours = rollout.AgentGate(
        agent_id="x",
        expected_hash=ours,
        delivered=True,
        reported_status="APPLIED",
        reported_hash=ours,
        healthy=True,
    )
    assert applied_ours.applied is True
    verdict = rollout.batch_gate([applied_ours], gate_seconds=0)
    assert verdict.ok is True and verdict.reason == rollout.GATE_OK

    wrong_hash = rollout.AgentGate(
        agent_id="x",
        expected_hash=ours,
        delivered=True,
        reported_status="APPLIED",
        reported_hash=theirs,
        healthy=True,
    )
    assert wrong_hash.applied is False
    verdict = rollout.batch_gate([wrong_hash], gate_seconds=0)
    assert verdict.ok is False and verdict.reason == rollout.GATE_AWAITING_REPORT

    wrong_status = rollout.AgentGate(
        agent_id="x",
        expected_hash=ours,
        delivered=True,
        reported_status="APPLYING",
        reported_hash=ours,
        healthy=True,
    )
    assert rollout.batch_gate([wrong_status], gate_seconds=0).ok is False


def test_the_health_window_is_the_last_thing_to_open():
    now = utcnow()
    gate = rollout.AgentGate(
        agent_id="x",
        expected_hash="a" * 64,
        delivered=True,
        reported_status="APPLIED",
        reported_hash="a" * 64,
        healthy=True,
    )
    waiting = rollout.batch_gate(
        [gate], gate_seconds=90, last_delivery=now - timedelta(seconds=10), now=now
    )
    assert waiting.ok is False and waiting.reason == rollout.GATE_HEALTH_WINDOW
    opened = rollout.batch_gate(
        [gate], gate_seconds=90, last_delivery=now - timedelta(seconds=91), now=now
    )
    assert opened.ok is True


def test_a_config_conflict_pauses_the_batch_even_though_health_is_true():
    """#135: APPLIED, healthy=true, and the fleet is still not running our config.

    The agent's own `ComponentHealth.status` was `config_conflict` while `healthy`
    stayed true (the agent is fine; the *config* is ambiguous, because the daemon
    merges a second source scheduling the same query names). Checking only
    `healthy is False` let a rollout complete on exactly that state.
    """
    gate = rollout.AgentGate(
        agent_id="x",
        expected_hash="a" * 64,
        delivered=True,
        reported_status="APPLIED",
        reported_hash="a" * 64,
        healthy=True,
        health_status="config_conflict",
        health_error="the same query name is scheduled more than once: battery_stats",
    )
    assert gate.applied is True, "this is not a delivery failure: the config did apply"

    verdict = rollout.batch_gate([gate], gate_seconds=0)
    assert verdict.pause is True
    assert verdict.reason == rollout.PAUSE_CONFIG_CONFLICT
    assert verdict.blocking == ("x",)
    assert "battery_stats" in verdict.detail, "the agent's own words are the useful part"


def test_a_healthy_status_that_is_not_a_conflict_still_promotes():
    """Only the one status pauses: `ok` (and anything else the agent invents) does not."""
    gate = rollout.AgentGate(
        agent_id="x",
        expected_hash="a" * 64,
        delivered=True,
        reported_status="APPLIED",
        reported_hash="a" * 64,
        healthy=True,
        health_status="ok",
    )
    assert rollout.batch_gate([gate], gate_seconds=0).ok is True


def test_facts_for_reads_the_reported_health_status():
    """The gate's input comes from the stored health report, not from `healthy` alone."""

    class State:
        agent_id = "x"
        healthy = True
        remote_config_status = "RemoteConfigStatuses_APPLIED"
        health = {"status": "config_conflict", "lastError": "duplicate query names"}

        def is_stale(self, now):
            return False

    gate = rollout.facts_for(
        {"agent_id": "x", "config_hash": "a" * 64, "revision_id": "r-1"}, State(), None
    )
    assert gate.healthy is True
    assert gate.health_status == "config_conflict"
    assert gate.health_error == "duplicate query names"


def test_an_unknown_health_report_does_not_block_the_gate():
    """Health reporting is optional in the spec; blocking on its absence would make
    every collector without `ReportsHealth` un-rolloutable. Only `healthy=false` stops
    a batch."""
    gate = rollout.AgentGate(
        agent_id="x",
        expected_hash="a" * 64,
        delivered=True,
        reported_status="APPLIED",
        reported_hash="a" * 64,
        healthy=None,
    )
    assert rollout.batch_gate([gate], gate_seconds=0).ok is True


# ------------------------------------------------------------------- acceptance


def _propose(client, agent_ids, config_map=None):
    response = client.post(
        "/config/revisions",
        json={"config_map": config_map or CONFIG_V2, "scope": {"agents": agent_ids}},
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal_id"]


def _create(proposal_id, **kwargs):
    """Create a rollout with a test-friendly step: the percent cap would otherwise make
    a 3-agent fleet promote one agent per batch (correct, but 20 tests of waiting)."""
    kwargs.setdefault("approver", "admin")
    kwargs.setdefault("gate_seconds", 0)
    kwargs.setdefault("max_percent_per_batch_value", 100.0)
    return rollout.create_rollout(proposal_id, **kwargs)

def _register(client, count=1, start=1):
    """Register `count` agents through the real message path and return their ids."""
    ids = []
    for index in range(start, start + count):
        agent_id = agent_id_for(index)
        poll(agent_id)  # registration poll, no status
        ids.append(agent_id)
    return ids


def test_canary_then_batch_two_after_the_gate_opens(client):
    """Acceptance: one canary applies, health is green, the gate opens, batch 2 goes."""
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id, now=utcnow())
    assert created["status"] == "running"
    assert created["batch"]["count"] == 2
    assert created["batches"][0]["agents"] == 1

    canary = created["batches"][0]["agent_ids"][0]
    rest = created["batches"][1]["agent_ids"]
    assert len(rest) == 2

    # A held agent is never handed the config, even when it polls: approval assigned
    # it, the rollout has not released it (#101 rule 1).
    for agent_id in rest:
        assert poll(agent_id) is False
        assert remote_config.REGISTRY.get(agent_id)["held"] is True

    # The canary collects the config on its own poll, then reports APPLIED with it.
    assert poll(canary) is True
    hash_b64 = our_hash_b64(canary)
    poll(canary, status="APPLIED", hash_b64=hash_b64)

    advanced = rollout.tick(now=utcnow())
    assert advanced["events"][0]["action"] == "promoted"
    assert advanced["events"][0]["batch"] == 1

    # Batch 2 is now released and applies in turn; then the rollout completes.
    for agent_id in rest:
        assert poll(agent_id) is True
        poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
    final = rollout.tick(now=utcnow())
    assert final["events"][0]["action"] in ("promoted", "completed")
    view = rollout.view(created["rollout_id"])
    if view["status"] == "running":
        final = rollout.tick(now=utcnow())
        view = rollout.view(created["rollout_id"])
    assert view["status"] == "completed"
    assert view["agents"]["states"]["applied"] == 3
    # Every hold has been released by the time the rollout is complete.
    for agent_id in agent_ids:
        assert remote_config.REGISTRY.get(agent_id)["held"] is False


def test_a_failed_canary_pauses_the_rollout_and_poisons_the_revision(client):
    """Acceptance: FAILED → pause, no further delivery, revision poisoned, no retry."""
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    canary = created["batches"][0]["agent_ids"][0]
    rest = created["batches"][1]["agent_ids"]

    assert poll(canary) is True
    poll(canary, status="FAILED", hash_b64=our_hash_b64(canary), error="boom")

    paused = rollout.tick(now=utcnow())
    assert paused["events"][0]["action"] == "paused"
    assert paused["events"][0]["reason"] == rollout.PAUSE_FAILED

    view = rollout.view(created["rollout_id"])
    assert view["status"] == "paused"
    assert view["paused_reason"] == "failed"
    assert "boom" in (view["pause_detail"] or "") or view["pause_detail"]
    assert view["agents"]["states"]["failed"] >= 1

    # The revision is poisoned and the assignment is undeliverable (D6).
    record = remote_config.REGISTRY.store.current(canary)
    assert record["status"] == "poisoned" or record["poisoned"]
    assert remote_config.deliverable(remote_config.REGISTRY.get(canary)) is False

    # Nothing else moves, over several ticks: a pause is not a retry timer, and the
    # un-promoted agents stay held.
    for agent_id in rest:
        assert remote_config.REGISTRY.get(agent_id)["held"] is True
        assert poll(agent_id) is False
    for _ in range(3):
        again = rollout.tick(now=utcnow())
        assert all(event["action"] != "promoted" for event in again["events"])
    for agent_id in rest:
        assert poll(agent_id) is False


def test_a_silent_canary_never_opens_the_gate(client):
    """Acceptance: stale/dead after delivery → distinct pause reason, gate never opens."""
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    canary = created["batches"][0]["agent_ids"][0]
    assert poll(canary) is True

    # The agent is delivered to and then stops reporting: no APPLIED, no heartbeat.
    state = AGENT_REGISTRY.get(canary)
    state.last_heartbeat = utcnow() - timedelta(hours=2)
    paused = rollout.tick(now=utcnow())
    assert paused["events"][0]["action"] == "paused"
    assert paused["events"][0]["reason"] == rollout.PAUSE_SILENT
    assert rollout.view(created["rollout_id"])["status"] == "paused"
    assert rollout.view(created["rollout_id"])["paused_reason"] == "agent_went_silent"


def test_a_silent_agent_that_vanished_from_the_registry_also_pauses(client):
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    canary = created["batches"][0]["agent_ids"][0]
    assert poll(canary) is True
    AGENT_REGISTRY.remove(canary)  # the stale reaper got there first

    paused = rollout.tick(now=utcnow())
    assert paused["events"][0]["action"] == "paused"
    assert paused["events"][0]["reason"] == rollout.PAUSE_SILENT


def test_an_unhealthy_agent_pauses_the_batch(client):
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    canary = created["batches"][0]["agent_ids"][0]
    assert poll(canary) is True
    poll(canary, status="APPLIED", hash_b64=our_hash_b64(canary), healthy=False)

    paused = rollout.tick(now=utcnow())
    assert paused["events"][0]["action"] == "paused"
    assert paused["events"][0]["reason"] == rollout.PAUSE_UNHEALTHY


def test_an_auto_revert_pauses_and_poisons_without_looping(client):
    """Acceptance: the agent reports its last-working hash → pause + poison, no loop."""
    agent_ids = _register(client, 3)
    # Give the fleet a known-good revision first, so "last working" has a hash to be.
    first = _propose(client, agent_ids, config_map=CONFIG)
    first_rollout = _create(first)
    for agent_id in agent_ids:
        poll(agent_id)
        poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
    rollout.tick(now=utcnow())
    rollout.tick(now=utcnow())
    assert rollout.view(first_rollout["rollout_id"])["status"] in ("completed", "running")
    last_working = {
        agent_id: our_hash_b64(agent_id) for agent_id in agent_ids
    }

    second = _propose(client, agent_ids, config_map=CONFIG_V2)
    created = _create(second)
    canary = created["batches"][0]["agent_ids"][0]

    # Delivered twice (the agent keeps reporting no hash, so reconciliation re-offers
    # it — which is what makes the supervisor's revert look like a stale report at
    # first), then it reports the *old* hash with APPLIED: an auto-revert (T9).
    assert poll(canary) is True
    assert poll(canary) is True
    poll(canary, status="APPLIED", hash_b64=last_working[canary])

    record = remote_config.REGISTRY.store.current(canary)
    assert record["poisoned"] or record["status"] == "poisoned"

    paused = rollout.tick(now=utcnow())
    assert paused["events"][0]["action"] == "paused"
    assert paused["events"][0]["reason"] == rollout.PAUSE_AUTO_REVERTED

    # No loop: the revision stays undeliverable and no frame is re-offered, over time.
    for _ in range(5):
        assert poll(canary) is False
        rollout.tick(now=utcnow())
    assert poll(canary) is False
    assert rollout.view(created["rollout_id"])["status"] == "paused"


def test_a_500_agent_rollout_never_exceeds_the_caps(client):
    """Acceptance: caps are honoured end to end, not only in the planner."""
    agent_ids = [agent_id_for(index) for index in range(1, 501)]
    for agent_id in agent_ids:
        poll(agent_id)
    proposal_id = _propose(client, agent_ids)
    # Production defaults, not the test-friendly ones: this is the acceptance criterion.
    created = rollout.create_rollout(proposal_id, approver="admin", gate_seconds=0)

    assert created["agents"]["total"] == 500
    assert created["batch"]["count"] == 20  # 25 canary + 19 × 25
    assert created["batch"]["max_agents_per_batch"] == 25
    assert created["batch"]["max_percent_per_batch"] == 25.0
    assert all(batch["agents"] <= 25 for batch in created["batches"])
    # Exactly one batch is in flight; everything else is held.
    assert created["batches"][0]["status"] == "in_flight"
    assert all(batch["status"] == "pending" for batch in created["batches"][1:])
    held = sum(
        1
        for agent_id in agent_ids
        if (remote_config.REGISTRY.get(agent_id) or {}).get("held")
    )
    assert held == 475


def test_abort_stops_promotion_within_one_tick_and_leaves_applied_agents_alone(client):
    """Acceptance: abort stops promotion; already-applied agents are untouched."""
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    canary = created["batches"][0]["agent_ids"][0]
    rest = created["batches"][1]["agent_ids"]

    assert poll(canary) is True
    poll(canary, status="APPLIED", hash_b64=our_hash_b64(canary))
    rollout.tick(now=utcnow())  # batch 2 promoted

    aborted = rollout.abort(created["rollout_id"], reason="stop")
    assert aborted["status"] == "aborted"
    # The canary keeps running the config it already applied: abort is not a recall.
    applied_revision = remote_config.REGISTRY.get(canary)["revision_id"]
    assert aborted["agents"]["states"]["applied"] >= 1

    ticked = rollout.tick(now=utcnow())
    assert all(event["action"] != "promoted" for event in ticked["events"])
    assert remote_config.REGISTRY.get(canary)["revision_id"] == applied_revision
    assert rollout.view(created["rollout_id"])["status"] == "aborted"
    # A rollout in flight at abort time keeps its un-promoted agents held; nothing
    # else was ever delivered.
    assert rollout.view(created["rollout_id"])["agents"]["states"]["applied"] >= 1
    assert all(remote_config.REGISTRY.get(agent_id) is not None for agent_id in rest)

    # Third batch agents (if any) were never released.
    for batch in aborted["batches"]:
        if batch["status"] == "pending":
            for agent_id in batch["agent_ids"]:
                assert remote_config.REGISTRY.get(agent_id)["held"] is True


def test_a_ttl_expired_rollout_is_cancelled_and_audited(client):
    """Acceptance: TTL cancels a stalled rollout and audits the reason."""
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    started = utcnow()
    created = _create(
        proposal_id,
        gate_seconds=100000,  # the gate can never open in this test
        ttl_minutes_value=1,
        now=started,
    )
    result = rollout.tick(now=started + timedelta(minutes=2))
    assert any(event["action"] == "cancelled" for event in result["events"])
    view = rollout.view(created["rollout_id"])
    assert view["status"] == "cancelled"
    assert view["paused_reason"] == "ttl_expired"

    rows = audit.STORE.list_entries(action="config.rollout.cancelled")
    assert rows and "ttl_expired" in (rows[0].get("detail_json") or "")
    # The un-promoted agents are still held: an expired approval must not fire later.
    held = [
        agent_id
        for agent_id in agent_ids
        if (remote_config.REGISTRY.get(agent_id) or {}).get("held")
    ]
    un_promoted = [
        agent_id
        for batch in view["batches"][1:]
        for agent_id in batch["agent_ids"]
    ]
    assert un_promoted and held == un_promoted


def test_a_rollback_rollout_reaches_applied_on_every_previously_applied_agent(client):
    """Acceptance: rollback runs as its own rollout with the same gating."""
    agent_ids = _register(client, 2)
    first = _propose(client, agent_ids, config_map=CONFIG)
    _create(first)
    good_hashes = {}
    for agent_id in agent_ids:
        poll(agent_id)
        poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
        good_hashes[agent_id] = our_hash_b64(agent_id)
    rollout.tick(now=utcnow())
    rollout.tick(now=utcnow())

    second = _propose(client, agent_ids, config_map=CONFIG_V2)
    rolled = _create(second)
    for agent_id in agent_ids:
        poll(agent_id)
        poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
    rollout.tick(now=utcnow())
    view = rollout.view(rolled["rollout_id"])
    while view["status"] == "running":
        view = rollout.view(rolled["rollout_id"])
        for agent_id in agent_ids:
            poll(agent_id)
            poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
        rollout.tick(now=utcnow())
        view = rollout.view(rolled["rollout_id"])
    assert view["status"] == "completed"

    back = rollout.create_rollback_rollout(
        rolled["rollout_id"], approver="admin", gate_seconds=0
    )
    assert back["kind"] == "rollback"
    assert back["agents"]["total"] == 2

    for _ in range(4):
        for agent_id in agent_ids:
            poll(agent_id)
            poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
        rollout.tick(now=utcnow())
        if rollout.view(back["rollout_id"])["status"] == "completed":
            break
    final = rollout.view(back["rollout_id"])
    assert final["status"] == "completed"
    assert final["agents"]["states"]["applied"] == 2
    # Every agent is running the previous revision again (the *old* hash), and the
    # rollback revision names its parent so the chain is answerable.
    for agent_id in agent_ids:
        record = remote_config.REGISTRY.get(agent_id)
        assert remote_config.hash_b64(record["config_hash"]) == good_hashes[agent_id]
        assert record["parent_revision_id"]


def test_a_rollout_is_audited_at_pause_and_completion(client):
    agent_ids = _register(client, 1)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    agent_id = created["batches"][0]["agent_ids"][0]
    poll(agent_id)
    poll(agent_id, status="APPLIED", hash_b64=our_hash_b64(agent_id))
    rollout.tick(now=utcnow())
    assert audit.STORE.list_entries(action="config.rollout.completed")
    assert rollout.view(created["rollout_id"])["status"] == "completed"


def test_the_engine_does_nothing_when_push_is_disabled(client, monkeypatch):
    agent_ids = _register(client, 1)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "false")
    assert rollout.tick(now=utcnow())["skipped"] == "push_disabled"
    assert rollout.view(created["rollout_id"])["status"] == "running"


def test_listing_and_unknown_rollouts_are_honest(client):
    agent_ids = _register(client, 1)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    listing = rollout.list_rollouts()
    assert listing["count"] == 1
    assert listing["rollouts"][0]["rollout_id"] == created["rollout_id"]
    assert listing["rollouts"][0]["states"]["delivered"] + listing["rollouts"][0]["states"]["pending"] >= 1

    with pytest.raises(rollout.RolloutError):
        rollout.view("ro-does-not-exist")
    with pytest.raises(rollout.RolloutError):
        rollout.abort("ro-does-not-exist")
    with pytest.raises(rollout.RolloutError):
        rollout.list_rollouts(status="nonsense")


def test_a_held_assignment_is_undeliverable_and_says_so(client):
    """The hold is visible in every vocabulary the API already uses (#99/#102), so a
    UI can say "held by a rollout" instead of showing a config that never arrives."""
    agent_ids = _register(client, 3)
    proposal_id = _propose(client, agent_ids)
    created = _create(proposal_id)
    held_agent = created["batches"][1]["agent_ids"][0]

    record = remote_config.REGISTRY.get(held_agent)
    assert record["held"] is True
    assert remote_config.deliverable(record) is False
    assert remote_config.delivery_view(record)["state"] == "held"
    decision = remote_config.decision_for(held_agent, capabilities=0xFFFF)
    assert decision.deliver is False
    assert decision.reason == "held_by_rollout"
    assert "batch is promoted" in remote_config.REASON_DETAILS["held_by_rollout"]
