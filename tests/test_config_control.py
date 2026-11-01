"""The config control plane (#99): revisions, approval, rollback, poisoning, drift.

Each test here corresponds to an acceptance criterion on the issue, because those
criteria are the only reason the lifecycle exists:

* propose → approve → delivered → `APPLIED` with a matching hash;
* approving a second revision supersedes the first and keeps it rollback-able;
* rollback reaches `APPLIED` and records the chain;
* an agent reporting its last-working hash after a failed push is classified as
  auto-reverted, the revision is poisoned, and **no further frames are sent** (asserted
  over five polls);
* stale-agent cleanup keeps the assignment, and a new uid inherits nothing;
* a selector resolves at approve time and does not capture later agents unless the
  operator opted in;
* drift is visible per agent and fires exactly one alert;
* a restart mid-rollout resumes from persisted state without double-delivering.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from proto import anyvalue_pb2, opamp_pb2
from server import (
    admin_auth,
    agent_auth,
    alerts,
    config_control,
    crypto,
    remote_config,
)
from server.main import _handle_agent_message, app
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from server.ws import REGISTRY as WS_REGISTRY

ADMIN = "admin-pw-1234567890"
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


def admin_headers() -> dict:
    token = base64.b64encode(f":{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def agent_message(
    uid: bytes = UID,
    status: str | None = None,
    reported_hash_b64: str | None = None,
    error: str = "",
    healthy: bool = True,
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = (
        opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsEffectiveConfig
    )
    msg.health.healthy = healthy
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


def reply(data: bytes) -> opamp_pb2.ServerToAgent:
    message = opamp_pb2.ServerToAgent()
    message.ParseFromString(_handle_agent_message(data).response)
    return message


def poll(agent_id: str, status: str = "APPLIED", hash_b64: str | None = None) -> bool:
    """One agent poll; returns True when the reply carries a config."""
    uid = bytes.fromhex(agent_id)
    return reply(agent_message(uid=uid, status=status, reported_hash_b64=hash_b64)).HasField(
        "remote_config"
    )


@pytest.fixture(autouse=True)
def push_enabled(monkeypatch, tmp_path):
    """A fully secure posture with push on (#96), encryption (#98) and no grace window."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    crypto.reset_key_cache()
    WS_REGISTRY.reset()
    config_control.reset_drift_alerts()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    WS_REGISTRY.reset()
    config_control.reset_drift_alerts()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    crypto.reset_key_cache()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def registered():
    reply(agent_message())
    return AGENT_ID


def propose(client, *, config_map=None, scope=None, **extra):
    body = {"config_map": config_map or CONFIG, "scope": scope or {"agent_id": AGENT_ID}}
    body.update(extra)
    return client.post("/config/revisions", json=body, headers=admin_headers())


def approve(client, proposal_id, **extra):
    return client.post(
        f"/config/revisions/{proposal_id}/approve",
        json=extra,
        headers=admin_headers(),
    )


# ------------------------------------------------------------------ happy path


def test_propose_delivers_nothing_and_approve_delivers_once(client, registered):
    proposed = propose(client)
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["status"] == "proposed"
    assert body["agents"] == 1
    proposal_id = body["proposal_id"]

    # Proposal is a draft: the agent's next poll carries nothing.
    assert poll(AGENT_ID) is False
    assert remote_config.REGISTRY.get(AGENT_ID) is None

    approved = approve(client, proposal_id)
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_by"] == "admin"
    revision_id = approved.json()["revisions"][0]["revision_id"]

    first = reply(agent_message())
    assert first.HasField("remote_config")
    assert first.remote_config.config_hash == bytes.fromhex(body["config_hash"])

    # The agent applies it and echoes our hash: no second frame, ever.
    assert poll(AGENT_ID, "APPLIED", remote_config.hash_b64(body["config_hash"])) is False

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["desired"]["status"] == "active"
    assert detail["desired"]["revision_id"] == revision_id
    assert detail["desired"]["deliverable"] is True
    assert detail["assignment"]["attempts"] == 1
    assert detail["assignment"]["last_status"] == "APPLIED"
    assert detail["drift"]["drift"] is False


def test_approving_a_second_revision_supersedes_the_first_and_keeps_it(client, registered):
    first = propose(client).json()
    approve(client, first["proposal_id"])

    second = propose(client, config_map=CONFIG_V2).json()
    approve(client, second["proposal_id"])

    history = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()["history"]
    by_revision = {row["revision_id"]: row for row in history}
    first_revision = first["revisions"][0]["revision_id"]
    assert by_revision[first_revision]["status"] == "superseded"
    assert by_revision[second["revisions"][0]["revision_id"]]["status"] == "active"

    # The superseded revision is still rollback-able.
    rolled = client.post(
        f"/agent/{AGENT_ID}/config/rollback", json={}, headers=admin_headers()
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["config_hash"] == first["config_hash"]
    assert rolled.json()["parent_revision_id"] == first_revision


def test_rollback_reaches_applied_and_records_the_chain(client, registered):
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    first_hash = pending["config_hash"]

    # A second revision becomes the new state, then rollback returns to the first.
    second = propose(client, config_map=CONFIG_V2).json()
    approve(client, second["proposal_id"])
    assert poll(AGENT_ID, "APPLIED", remote_config.hash_b64(first_hash))  # new config sent

    rolled = client.post(
        f"/agent/{AGENT_ID}/config/rollback", json={"note": "v2 broke tracing"},
        headers=admin_headers(),
    ).json()

    frame = reply(agent_message())
    assert frame.HasField("remote_config")
    assert bytes(frame.remote_config.config_hash).hex() == rolled["config_hash"]

    # Acknowledging the rollback ends delivery and the agent reports APPLIED.
    assert poll(AGENT_ID, "APPLIED", remote_config.hash_b64(rolled["config_hash"])) is False

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["desired"]["parent_revision_id"] == rolled["parent_revision_id"]
    assert detail["desired"]["note"] == "v2 broke tracing"
    statuses = {row["revision_id"]: row["status"] for row in detail["history"]}
    assert statuses[rolled["revision_id"]] == "active"
    assert "rolled_back" in statuses.values()


def test_a_new_uid_never_inherits_an_assignment(client, registered):
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    assert poll(AGENT_ID)

    # A cloned VM / reissued uid is a *different* agent: nothing is assigned to it.
    other = client.get(f"/agent/{OTHER_ID}/config", headers=admin_headers())
    assert other.status_code == 404
    assert remote_config.REGISTRY.get(OTHER_ID) is None
    assert reply(agent_message(uid=OTHER_UID)).HasField("remote_config") is False


def test_stale_cleanup_keeps_the_assignment_and_the_agent_gets_it_back(client, registered):
    from server.main import cleanup_stale_agents

    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    delivered = reply(agent_message())
    delivered_hash = bytes(delivered.remote_config.config_hash).hex()

    # Force the agent stale: the registry drops it, the assignment must not.
    AGENT_REGISTRY.get(AGENT_ID).last_heartbeat = AGENT_REGISTRY.get(
        AGENT_ID
    ).last_heartbeat - __import__("datetime").timedelta(seconds=3600)
    cleanup_stale_agents()
    assert AGENT_REGISTRY.get(AGENT_ID) is None
    assert remote_config.REGISTRY.get(AGENT_ID) is not None

    # It comes back and still reports the old hash, so it is offered the config again
    # (the delivery record proves the assignment survived the restart path).
    reply(agent_message())
    again = reply(agent_message(status="APPLIED", reported_hash_b64=remote_config.hash_b64("00" * 32)))
    assert again.HasField("remote_config")
    assert bytes(again.remote_config.config_hash).hex() == delivered_hash


# ---------------------------------------------------------------- poisoning (T9)


def test_failed_revision_is_poisoned_and_never_retried(client, registered):
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    assert reply(agent_message()).HasField("remote_config")

    failed = reply(
        agent_message(
            status="FAILED",
            reported_hash_b64=remote_config.hash_b64(pending["config_hash"]),
            error="collector refused it",
        )
    )
    assert not failed.HasField("remote_config")

    # Five more polls: nothing is ever sent again.
    for _ in range(5):
        assert poll(AGENT_ID) is False

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["desired"]["status"] == "poisoned"
    assert detail["desired"]["deliverable"] is False
    assert detail["assignment"]["poisoned"] is True


def test_an_auto_revert_is_detected_poisoned_and_stops_all_delivery(client, registered):
    """T9: the supervisor restores its last working config and reports *that* hash.

    A naive hash comparison then pushes the broken config forever. The server detects
    the revert, poisons the revision and sends nothing more — asserted over five polls.
    """
    good = propose(client).json()
    approve(client, good["proposal_id"])
    assert reply(agent_message()).HasField("remote_config")
    assert poll(AGENT_ID, "APPLIED", remote_config.hash_b64(good["config_hash"])) is False

    bad = propose(client, config_map=CONFIG_V2).json()
    approve(client, bad["proposal_id"])
    assert reply(agent_message()).HasField("remote_config")  # v2 goes out

    # The agent keeps reporting the *old* hash: the supervisor reverted.
    frames = 0
    for _ in range(5):
        if poll(AGENT_ID, "APPLIED", remote_config.hash_b64(good["config_hash"])):
            frames += 1
    assert frames <= 1, "the server must stop re-pushing a reverted config"

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["desired"]["status"] == "poisoned"
    assert "revert" in (detail["desired"]["failure_detail"] or "").lower()

    # And it stays quiet afterwards, even on a hash mismatch.
    assert poll(AGENT_ID) is False


def test_a_poisoned_revision_only_returns_by_explicit_reactivation(client, registered):
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    reply(agent_message())
    reply(
        agent_message(
            status="FAILED",
            reported_hash_b64=remote_config.hash_b64(pending["config_hash"]),
            error="boom",
        )
    )
    assert poll(AGENT_ID) is False

    reactivated = client.post(
        f"/config/revisions/{pending['proposal_id']}/reactivate",
        json={"reason": "the missing component is installed now"},
        headers=admin_headers(),
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reply(agent_message()).HasField("remote_config")


def test_only_an_approver_can_reactivate(client, registered):
    from server import auth

    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    reply(agent_message())
    reply(
        agent_message(
            status="FAILED",
            reported_hash_b64=remote_config.hash_b64(pending["config_hash"]),
        )
    )
    auth.PRINCIPALS.create("p-op", "op2", "operator", auth.hash_password("pw-operator-1234"))
    denied = client.post(
        f"/config/revisions/{pending['proposal_id']}/reactivate",
        json={},
        auth=("op2", "pw-operator-1234"),
    )
    assert denied.status_code == 403


def test_the_delivery_budget_stops_an_agent_that_never_acknowledges(client, registered, monkeypatch):
    monkeypatch.setenv("CONFIG_MAX_DELIVERY_ATTEMPTS", "2")
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    assert reply(agent_message()).HasField("remote_config")
    assert reply(agent_message()).HasField("remote_config")
    assert reply(agent_message()).HasField("remote_config") is False

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["assignment"]["unconfirmed"] is True
    assert detail["assignment"]["attempts"] == 2


def test_a_steady_state_acknowledgement_does_no_desired_config_writes(
    client, registered, monkeypatch
):
    """#109: an agent that keeps reporting the same APPLIED must not write per poll."""
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    assert reply(agent_message()).HasField("remote_config")

    writes = []
    store = remote_config.REGISTRY.store
    original = store.note_report
    monkeypatch.setattr(
        store,
        "note_report",
        lambda *args, **kwargs: writes.append(args) or original(*args, **kwargs),
    )

    acknowledged = remote_config.hash_b64(pending["config_hash"])
    for _ in range(5):
        poll(AGENT_ID, "APPLIED", acknowledged)

    # The first report changes `last_status` (one write); the repeats change nothing.
    assert len(writes) == 1


# ------------------------------------------------------------------- selectors


def _describe(client, uid: bytes, env: str = "prod"):
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.agent_description.identifying_attributes.add(
        key="service.name",
        value=anyvalue_pb2.AnyValue(string_value=f"collector-{env}"),
    )
    msg.agent_description.non_identifying_attributes.add(
        key="environment", value=anyvalue_pb2.AnyValue(string_value=env)
    )
    _handle_agent_message(msg.SerializeToString())


def test_selector_resolves_at_approve_time_and_does_not_capture_later_agents(client):
    _describe(client, UID, "prod")
    selected = propose(client, scope={"filter": {"environment": "prod"}})
    assert selected.status_code == 200, selected.text
    assert selected.json()["agents"] == 1

    # An agent that matches the filter *joins after* the proposal was created: the
    # selector is re-resolved, but a widened blast radius needs an explicit
    # acknowledgement from the approver.
    _describe(client, OTHER_UID, "prod")
    refused = approve(client, selected.json()["proposal_id"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["new_agents"] == [OTHER_ID]

    approved = approve(
        client, selected.json()["proposal_id"], confirm_scope_change=True
    ).json()
    assert approved["agents"] == 2, "the selector must be re-resolved at approve time"
    assert approved["added"] == [OTHER_ID]

    _describe(client, bytes.fromhex("ef" * 16), "prod")
    late_id = "ef" * 16
    assert remote_config.REGISTRY.get(late_id) is None
    late = client.get(f"/agent/{late_id}/config", headers=admin_headers()).json()
    assert late["desired"] is None, "a later match must not inherit the config"
    assert late["drift"]["drift"] is None


def test_include_future_opts_in_to_later_matches(client, monkeypatch):
    _describe(client, UID, "prod")
    proposal = propose(
        client, scope={"filter": {"environment": "prod"}, "include_future": True}
    ).json()
    approve(client, proposal["proposal_id"])

    late_uid = bytes.fromhex("ef" * 16)
    late_id = "ef" * 16
    _describe(client, late_uid, "prod")
    assert remote_config.REGISTRY.get(late_id) is None

    assert config_control.reconcile_future(AGENT_REGISTRY.list_all()) == 1
    assert remote_config.REGISTRY.get(late_id) is not None
    assert reply(agent_message(uid=late_uid)).HasField("remote_config")

    # Idempotent: a second sweep attaches nothing.
    assert config_control.reconcile_future(AGENT_REGISTRY.list_all()) == 0


def test_a_selector_that_matches_nothing_is_refused(client):
    _describe(client, UID, "prod")
    response = propose(client, scope={"filter": {"environment": "staging"}})
    assert response.status_code == 404
    assert "no agents matched" in response.json()["detail"]


def test_a_missing_scope_is_never_the_whole_fleet(client, registered):
    response = client.post(
        "/config/revisions", json={"config_map": CONFIG}, headers=admin_headers()
    )
    assert response.status_code == 422  # pydantic: scope is required
    response = client.post(
        "/config/revisions", json={"config_map": CONFIG, "scope": {}}, headers=admin_headers()
    )
    assert response.status_code == 400
    assert "never" in response.json()["detail"]


# ---------------------------------------------------------------------- drift


def test_drift_is_visible_per_agent_and_alerts_once(client, registered, monkeypatch):
    sent = []
    monkeypatch.setattr(
        alerts, "send_alert", lambda message, event_type="new_agent": sent.append(event_type)
        or (True, "ok")
    )

    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    reply(agent_message())

    # The agent reports an effective config that does not contain what we pushed.
    reporting = opamp_pb2.AgentToServer()
    reporting.instance_uid = UID
    reporting.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    reporting.effective_config.config_map.config_map[""].body = json.dumps(
        {"exporters": {"debug": {}}, "service": {"pipelines": {"traces": {}}}}
    ).encode()
    reply(reporting.SerializeToString())

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["drift"]["drift"] is True
    assert any("memory_limiter" in path or "service" in path for path in detail["drift"]["missing"])

    drifting = config_control.sweep_drift(AGENT_REGISTRY.list_all())
    assert len(drifting) == 1
    config_control.sweep_drift(AGENT_REGISTRY.list_all())
    assert sent.count("config_drift") == 1, "one alert per (agent, revision), not per sweep"


def test_no_drift_alert_when_the_agent_is_running_the_config(client, registered, monkeypatch):
    sent = []
    monkeypatch.setattr(
        alerts, "send_alert", lambda message, event_type="new_agent": sent.append(event_type)
        or (True, "ok")
    )
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    reply(
        agent_message(
            status="APPLIED", reported_hash_b64=remote_config.hash_b64(pending["config_hash"])
        )
    )
    assert config_control.sweep_drift(AGENT_REGISTRY.list_all()) == []
    assert "config_drift" not in sent


# -------------------------------------------------------------------- lifecycle


def test_a_restart_resumes_without_double_delivering(client, registered):
    from server.state import SQLiteDesiredConfigStore

    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    first = reply(agent_message())
    assert first.HasField("remote_config")

    # Simulate a server restart: a fresh registry over the same database.
    db_path = remote_config.REGISTRY.store.db_path
    remote_config.REGISTRY.reload()
    assert remote_config.REGISTRY.get(AGENT_ID)["delivered_hash"] == pending["config_hash"]

    # The agent never acknowledged, so one more delivery is legitimate (idempotent
    # desired state) — what must not happen is a restart looking like a fresh push
    # *after* the agent already applied it.
    reply(agent_message())
    assert poll(AGENT_ID, "APPLIED", remote_config.hash_b64(pending["config_hash"])) is False
    assert remote_config.DesiredConfigRegistry(SQLiteDesiredConfigStore(db_path)).get(AGENT_ID)


def test_only_proposed_revisions_can_be_rejected(client, registered):
    pending = propose(client).json()
    rejected = client.post(
        f"/config/revisions/{pending['proposal_id']}/reject",
        json={"reason": "wrong tenant"},
        headers=admin_headers(),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert poll(AGENT_ID) is False

    # A rejected proposal cannot be approved afterwards.
    assert approve(client, pending["proposal_id"]).status_code == 409


def test_reject_only_touches_the_undecided_revisions(client):
    """A partly-decided proposal (a selector that shrank) can still be cleaned up."""
    _describe(client, UID, "prod")
    proposal = propose(client, scope={"filter": {"environment": "prod"}}).json()
    stale_revision = proposal["revisions"][0]["revision_id"]

    # The original target stops matching; another agent takes its place.
    _describe(client, UID, "staging")
    _describe(client, OTHER_UID, "prod")
    approved = approve(
        client, proposal["proposal_id"], confirm_scope_change=True
    ).json()
    assert approved["agents"] == 1
    assert approved["dropped"] == [AGENT_ID]
    fresh_revision = approved["revisions"][0]["revision_id"]

    rejected = client.post(
        f"/config/revisions/{proposal['proposal_id']}/reject",
        json={"reason": "the old target left the selector"},
        headers=admin_headers(),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["revisions"] == [stale_revision]
    assert rejected.json()["skipped"] == [fresh_revision]

    statuses = {
        row["revision_id"]: row["status"]
        for row in client.get(
            f"/config/revisions/{proposal['proposal_id']}", headers=admin_headers()
        ).json()["revisions"]
    }
    assert statuses[stale_revision] == "rejected"
    assert statuses[fresh_revision] == "active"

    # Nothing left to reject the second time.
    again = client.post(
        f"/config/revisions/{proposal['proposal_id']}/reject",
        json={},
        headers=admin_headers(),
    )
    assert again.status_code == 409


def test_an_approved_revision_cannot_be_approved_twice(client, registered):
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    again = approve(client, pending["proposal_id"])
    assert again.status_code == 409
    assert "already been decided" in again.json()["detail"]["message"]


def test_proposing_a_broken_config_is_refused_and_stores_nothing(client, registered):
    broken = {"": {"body": "receivers: [unclosed", "content_type": "text/yaml"}}
    response = propose(client, config_map=broken)
    assert response.status_code == 422
    assert remote_config.REGISTRY.get(AGENT_ID) is None
    assert remote_config.REGISTRY.store.list_revisions() == []


def test_proposing_a_broken_opamp_invariant_is_refused(client, registered):
    no_opamp = {
        "": {
            "body": CONFIG[""]["body"].replace("  extensions: [opamp]\n", "")
            .replace("extensions:\n  opamp:\n    server:\n      ws:\n        endpoint: wss://opamp.example.com/v1/opamp\n", ""),
            "content_type": "text/yaml",
        }
    }
    response = propose(client, config_map=no_opamp)
    assert response.status_code == 422
    assert "extension is missing" in json.dumps(response.json())


def test_policy_failure_blocks_approval_unless_an_approver_overrides(client, registered, monkeypatch):
    from tests.test_config_validation import FakeOpa

    pending = propose(client).json()

    monkeypatch.setattr(
        "server.opa_client.get_opa_client",
        lambda: FakeOpa(violations=[{"msg": "exporters must use TLS"}]),
    )
    refused = approve(client, pending["proposal_id"])
    assert refused.status_code == 409
    assert "policy" in json.dumps(refused.json())

    overridden = approve(
        client, pending["proposal_id"], override_reason="backend is inside the VPC"
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["override_reason"] == "backend is inside the VPC"
    rows = client.get("/audit?action=config.override", headers=admin_headers()).json()
    assert rows["entries"], "an override must be audited"


def test_revalidation_refuses_an_approval_that_no_longer_validates(client, registered, monkeypatch):
    pending = propose(client).json()

    # The server's own endpoint moved after the proposal was made: the revision would
    # now point the fleet's control channel somewhere else.
    monkeypatch.setenv("PUBLIC_URL", "https://new.example.com")
    response = approve(client, pending["proposal_id"])
    assert response.status_code == 409
    assert "re-validate" in json.dumps(response.json())
    assert poll(AGENT_ID) is False


def test_revisions_listing_is_metadata_only(client, registered):
    pending = propose(client).json()
    listing = client.get("/config/revisions", headers=admin_headers()).json()
    assert listing["count"] == 1
    row = listing["revisions"][0]
    assert row["status"] == "proposed"
    assert "config_map" not in row
    assert "verbosity" not in json.dumps(row)
    assert listing["statuses"]

    proposal = client.get(
        f"/config/revisions/{pending['proposal_id']}", headers=admin_headers()
    ).json()
    assert proposal["status"] == "proposed"
    assert proposal["revisions"][0]["delivery"]["attempts"] == 0


def test_bad_status_filter_is_422(client, registered):
    response = client.get("/config/revisions?status=nope", headers=admin_headers())
    assert response.status_code == 422


def test_the_control_plane_is_gated_by_the_push_posture(client, registered, monkeypatch):
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED")
    response = propose(client)
    assert response.status_code == 404
    assert response.json()["detail"] == "config push is disabled"


def test_the_direct_push_path_also_keeps_one_active_revision(client, registered):
    """Rule 3 is enforced by the store, so no path can leave two active revisions."""
    first = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    ).json()
    second = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG_V2}, headers=admin_headers()
    ).json()

    history = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()["history"]
    statuses = {row["revision_id"]: row["status"] for row in history}
    assert statuses[first["revision_id"]] == "superseded"
    assert statuses[second["revision_id"]] == "active"


def test_an_older_database_is_migrated_in_place(tmp_path):
    """A database created before #99 gains the lifecycle columns, not a reset.

    Existing revisions become `active` (that is what they were) and the assignment
    columns default to "never delivered", so an upgrade does not lose the fleet's
    desired state.
    """
    import sqlite3

    from server.state import SQLiteDesiredConfigStore

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE config_revisions (revision_id TEXT PRIMARY KEY, "
            "agent_id TEXT NOT NULL, config_blob TEXT, config_hash TEXT NOT NULL, "
            "author TEXT NOT NULL, created_at TEXT NOT NULL, "
            "withdrawn INTEGER NOT NULL DEFAULT 0, failed_at TEXT, "
            "failure_detail TEXT, body_pruned_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE desired_configs (agent_id TEXT PRIMARY KEY, "
            "revision_id TEXT NOT NULL, previous_revision_id TEXT, "
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO config_revisions (revision_id, agent_id, config_hash, author, "
            "created_at, config_blob) VALUES ('rev-old', ?, ?, 'alice', "
            "'2026-01-01T00:00:00+00:00', NULL)",
            (AGENT_ID, "00" * 32),
        )
        conn.execute(
            "INSERT INTO desired_configs (agent_id, revision_id, updated_at) "
            "VALUES (?, 'rev-old', '2026-01-01T00:00:00+00:00')",
            (AGENT_ID,),
        )

    store = SQLiteDesiredConfigStore(db)
    current = store.current(AGENT_ID)
    assert current["status"] == "active"
    assert current["proposal_id"] is None
    assert current["attempts"] == 0
    assert current["poisoned"] is False
    assert current["delivered_hash"] is None


def test_audit_records_every_lifecycle_decision(client, registered):
    pending = propose(client).json()
    approve(client, pending["proposal_id"])
    second = propose(client, config_map=CONFIG_V2).json()
    approve(client, second["proposal_id"])
    rolled = client.post(
        f"/agent/{AGENT_ID}/config/rollback", json={}, headers=admin_headers()
    )
    assert rolled.status_code == 200, rolled.text
    client.post(
        f"/config/revisions/{pending['proposal_id']}/reject",
        json={},
        headers=admin_headers(),
    )
    actions = {
        row["action"]
        for row in client.get("/audit?limit=50", headers=admin_headers()).json()["entries"]
    }
    assert {"config.propose", "config.approve", "config.rollback"} <= actions


def test_a_role_on_a_config_object_reaches_the_agent(client, registered):
    """`AgentConfigObject.role` (v0.20.0, #66/#68) survives the whole push path.

    The realistic shape: the expanded config under `""` and the raw one under
    `"raw"`, each tagged with its role. Dropping the role anywhere between validate
    and deliver would report success and hand the agent a different object.
    """
    config_map = {
        "": {**CONFIG[""], "role": "expanded"},
        "raw": {"body": "receivers: [otlp]\n", "content_type": "text/yaml", "role": "raw"},
    }

    proposed = propose(client, config_map=config_map)
    assert proposed.status_code == 200, proposed.text
    approved = approve(client, proposed.json()["proposal_id"])
    assert approved.status_code == 200, approved.text

    delivered = reply(agent_message())
    assert delivered.HasField("remote_config")
    objects = delivered.remote_config.config.config_map
    assert objects[""].role == "expanded"
    assert objects["raw"].role == "raw"
    assert objects["raw"].body == b"receivers: [otlp]\n"

    # The hash covers the role, so the agent echoing it back settles the revision.
    reported = base64.b64encode(delivered.remote_config.config_hash).decode()
    assert poll(AGENT_ID, status="APPLIED", hash_b64=reported) is False

    view = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert view["desired"]["config_map"]["raw"]["role"] == "raw"
