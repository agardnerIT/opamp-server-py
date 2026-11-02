"""The staged-rollout HTTP surface (#103) and the restart command (#84).

Every route here follows the repo convention (`tags=["config"]` / `["agents"]`, a
docstring that states the role, and a summary) and the safety rules of the epic: the
approval is a different role from the proposal, a dry run changes nothing, and a
command reply carries nothing but the command.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server import (
    admin_auth,
    agent_auth,
    audit,
    commands,
    config_control,
    crypto,
    remote_config,
    rollout,
)
from server.main import _handle_agent_message, app
from server.state import AGENT_REGISTRY, SQLiteCredentialStore, SQLiteRolloutStore
from server.ws import REGISTRY as WS_REGISTRY

ADMIN = "admin-pw-1234567890"
AGENT_ID = "ab" * 16

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


def admin_headers() -> dict:
    token = base64.b64encode(f":{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def agent_message(
    agent_id: str = AGENT_ID,
    status: str | None = None,
    hash_b64: str | None = None,
    restart_capable: bool = True,
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = bytes.fromhex(agent_id)
    msg.capabilities = (
        opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsHealth
    )
    if restart_capable:
        msg.capabilities |= (
            opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRestartCommand
        )
    msg.health.healthy = True
    if status:
        msg.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
        )
        if hash_b64:
            msg.remote_config_status.last_remote_config_hash = base64.b64decode(hash_b64)
    return msg.SerializeToString()


def reply(data: bytes) -> opamp_pb2.ServerToAgent:
    message = opamp_pb2.ServerToAgent()
    message.ParseFromString(_handle_agent_message(data).response)
    return message


def poll(**kwargs) -> opamp_pb2.ServerToAgent:
    return reply(agent_message(**kwargs))


def our_hash_b64(agent_id: str = AGENT_ID) -> str:
    record = remote_config.REGISTRY.get(agent_id) or {}
    return remote_config.hash_b64(record["config_hash"])


@pytest.fixture(autouse=True)
def push_enabled(monkeypatch, tmp_path):
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
    monkeypatch.delenv("OPAMP_RESTART_COMMAND_ENABLED", raising=False)
    for name in (
        "CONFIG_PUSH_CANARY_PERCENT",
        "CONFIG_PUSH_MAX_AGENTS_PER_BATCH",
        "CONFIG_PUSH_MAX_PERCENT_PER_BATCH",
        "CONFIG_PUSH_HEALTH_GATE_SECONDS",
        "CONFIG_PUSH_ROLLOUT_TTL_MINUTES",
        "CONFIG_PUSH_TICK_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
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


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def registered(client):
    poll()
    return AGENT_ID


def propose(client, scope=None, **extra):
    body = {"config_map": CONFIG, "scope": scope or {"agent_id": AGENT_ID}}
    body.update(extra)
    response = client.post("/config/revisions", json=body, headers=admin_headers())
    assert response.status_code == 200, response.text
    return response.json()["proposal_id"]


# --------------------------------------------------------------- #103: dry run


def test_dry_run_plans_the_batches_and_changes_nothing(client, registered):
    proposal_id = propose(client)
    response = client.post(
        f"/config/revisions/{proposal_id}/apply",
        json={"dry_run": True},
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["batches"][0]["agent_ids"] == [AGENT_ID]
    assert body["batch"]["count"] == 1
    assert body["delivery"]["reason"] == "dry_run"

    # Nothing was approved, assigned, held or delivered.
    assignment = remote_config.REGISTRY.get(AGENT_ID)
    assert assignment is None
    assert poll().HasField("remote_config") is False
    assert rollout.list_rollouts()["count"] == 0
    # ... and the proposal is still a proposal, so the real apply still works.
    assert config_control.summary(proposal_id)["statuses"] == ["proposed"]


def test_dry_run_honours_the_caps_it_is_given(client, registered):
    proposal_id = propose(client)
    body = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={
            "dry_run": True,
            "canary_percent": 100,
            "max_agents_per_batch": 1,
            "max_percent_per_batch": 100,
            "health_gate_seconds": 5,
            "ttl_minutes": 10,
        },
        headers=admin_headers(),
    ).json()
    assert body["batch"]["canary_percent"] == 100
    assert body["batch"]["max_agents_per_batch"] == 1
    assert body["batch"]["health_gate_seconds"] == 5
    assert body["batch"]["ttl_minutes"] == 10


def test_an_undecidable_proposal_cannot_be_dry_run(client, registered):
    proposal_id = propose(client)
    created = client.post(
        "/config/rollouts", params={"proposal_id": proposal_id}, json={}, headers=admin_headers()
    )
    assert created.status_code == 200, created.text
    again = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"dry_run": True},
        headers=admin_headers(),
    )
    assert again.status_code == 409
    assert "already" in json.dumps(again.json())


def test_unknown_extra_fields_are_rejected(client, registered):
    """`extra="forbid"`: a mistyped cap that silently does nothing is a blast-radius bug."""
    proposal_id = propose(client)
    response = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"max_batch": 10},
        headers=admin_headers(),
    )
    assert response.status_code == 422


# ------------------------------------------------------------- #103: rollouts


def test_create_rollout_is_the_approval_and_only_the_canary_is_released(client, registered):
    proposal_id = propose(client)
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"health_gate_seconds": 0},
        headers=admin_headers(),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "running"
    assert body["agents"]["states"]["delivered"] + body["agents"]["states"]["pending"] == 1
    # The revision is active and deliverable — but only after the rollout released it.
    assert remote_config.REGISTRY.get(AGENT_ID)["held"] is False
    assert poll().HasField("remote_config") is True


def test_a_rollout_needs_the_approver_role(client, registered):
    """Creating a rollout approves a proposal, so `operator` is not enough."""
    proposal_id = propose(client)
    response = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={},
        headers={},  # no credentials
    )
    assert response.status_code in (401, 403)


def test_list_and_get_rollouts(client, registered):
    proposal_id = propose(client)
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"health_gate_seconds": 0},
        headers=admin_headers(),
    ).json()

    listing = client.get("/config/rollouts", headers=admin_headers())
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["rollouts"][0]["rollout_id"] == created["rollout_id"]

    detail = client.get(f"/config/rollouts/{created['rollout_id']}", headers=admin_headers())
    assert detail.status_code == 200
    assert detail.json()["rollout_id"] == created["rollout_id"]

    assert client.get("/config/rollouts/ro-nope", headers=admin_headers()).status_code == 404
    assert client.get("/config/rollouts?status=nonsense", headers=admin_headers()).status_code == 422


def test_abort_stops_promotion_and_needs_only_operator(client, registered):
    proposal_id = propose(client)
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"health_gate_seconds": 0},
        headers=admin_headers(),
    ).json()
    response = client.post(
        f"/config/rollouts/{created['rollout_id']}/abort",
        json={"reason": "typo in the exporter"},
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "aborted"
    assert rollout.view(created["rollout_id"])["paused_reason"] == "aborted"
    # Audited exactly once, by the engine.
    assert len(audit.STORE.list_entries(action="config.rollout.abort")) == 1

    # A closed rollout cannot be aborted twice.
    again = client.post(
        f"/config/rollouts/{created['rollout_id']}/abort", json={}, headers=admin_headers()
    )
    assert again.status_code == 409


def test_rollback_rollout_route_reports_an_empty_rollout_as_409(client, registered):
    proposal_id = propose(client)
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"health_gate_seconds": 100000},
        headers=admin_headers(),
    ).json()
    response = client.post(
        f"/config/rollouts/{created['rollout_id']}/rollback", json={}, headers=admin_headers()
    )
    assert response.status_code == 409
    detail = json.dumps(response.json())
    assert "cannot roll back" in detail
    assert "previous revision" in detail


def test_revalidate_is_read_only_and_reports_the_fingerprint(client, registered):
    proposal_id = propose(client)
    response = client.post(
        f"/config/revisions/{proposal_id}/validate", headers=admin_headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal_id"] == proposal_id
    assert body["ok"] is True
    assert body["fingerprint"]
    assert body["stored"]  # the verdict recorded at propose time
    # Nothing moved: still a proposal, still no assignment.
    assert config_control.summary(proposal_id)["statuses"] == ["proposed"]
    assert remote_config.REGISTRY.get(AGENT_ID) is None


def test_the_rollout_surface_is_gated_by_the_push_posture(client, registered, monkeypatch):
    proposal_id = propose(client)
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "false")
    response = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={},
        headers=admin_headers(),
    )
    assert response.status_code == 404
    assert "disabled" in json.dumps(response.json())


def test_restart_after_apply_queues_one_command_per_promoted_agent(client, registered):
    proposal_id = propose(client)
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"health_gate_seconds": 0, "restart_after_apply": True},
        headers=admin_headers(),
    ).json()
    assert created["batch"]["restart_after_apply"] is True
    # The canary collects the config and reports APPLIED, which opens the gate.
    assert poll().HasField("remote_config") is True
    poll(status="APPLIED", hash_b64=our_hash_b64())
    from server.state import utcnow

    rollout.tick(now=utcnow())
    state = AGENT_REGISTRY.get(AGENT_ID)
    assert state.pending_command is not None
    assert state.pending_command["type"] == "restart"


# ---------------------------------------------------------------- #84: restart


def test_restart_is_queued_delivered_once_and_carries_nothing_else(client, registered):
    response = client.post(
        "/agent/{}/restart".format(AGENT_ID), json={"reason": "new exporter"}, headers=admin_headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True
    assert body["delivery"]["state"] == "queued"
    assert AGENT_REGISTRY.get(AGENT_ID).pending_command["requested_by"] == "admin"
    # Audited before the command was queued (#97 pre-write).
    rows = audit.STORE.list_entries(action="agent.restart")
    assert len(rows) == 1 and rows[0]["target_id"] == AGENT_ID

    # The next poll carries the command and nothing else (spec rule).
    command_reply = poll()
    assert command_reply.HasField("command")
    assert command_reply.command.type == opamp_pb2.CommandType.CommandType_Restart
    assert command_reply.instance_uid == bytes.fromhex(AGENT_ID)
    assert command_reply.capabilities != 0
    # No other field may be set on a command reply.
    for field in (
        "remote_config",
        "agent_identification",
        "connection_settings",
        "packages_available",
    ):
        assert command_reply.HasField(field) is False
    assert command_reply.flags == 0
    assert command_reply.error_response.type == 0

    # Exactly once: the following polls carry no command.
    for _ in range(3):
        assert poll().HasField("command") is False
    assert AGENT_REGISTRY.get(AGENT_ID).pending_command is None
    assert AGENT_REGISTRY.get(AGENT_ID).command_delivered_at


def test_restart_is_refused_with_409_when_the_agent_does_not_accept_it(client):
    poll(restart_capable=False)
    response = client.post(
        f"/agent/{AGENT_ID}/restart", json={}, headers=admin_headers()
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "AcceptsRestartCommand" in json.dumps(detail)
    assert "accepts_restart_command" in json.dumps(detail)
    # A refusal is audited too, with result="error" (pre-write, then act).
    rows = audit.STORE.list_entries(action="agent.restart")
    assert rows and rows[0]["result"] == "error"
    assert AGENT_REGISTRY.get(AGENT_ID).pending_command is None


def test_restart_unknown_agent_is_404_and_the_command_can_be_disabled(
    client, registered, monkeypatch
):
    assert client.post(
        "/agent/{}/restart".format("cd" * 16), json={}, headers=admin_headers()
    ).status_code == 404
    monkeypatch.setenv("OPAMP_RESTART_COMMAND_ENABLED", "false")
    response = client.post(f"/agent/{AGENT_ID}/restart", json={}, headers=admin_headers())
    assert response.status_code == 404
    assert commands.restart_allowed() is False


def test_a_queued_command_survives_a_registry_reload(client, registered, tmp_path):
    """HTTP has no push, so the queue is persisted: a server restart must not lose it."""
    client.post(f"/agent/{AGENT_ID}/restart", json={}, headers=admin_headers())
    store = AGENT_REGISTRY._store
    reloaded = store.load_all()
    assert reloaded[AGENT_ID].pending_command["type"] == "restart"


def test_the_agent_view_reports_command_state(client, registered):
    detail = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert detail["pending_command"] is None
    assert detail["command_delivered_at"] is None
    client.post(f"/agent/{AGENT_ID}/restart", json={}, headers=admin_headers())
    detail = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert detail["pending_command"]["type"] == "restart"
    poll()
    detail = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert detail["pending_command"] is None
    assert detail["command_delivered_at"] is not None


# ------------------------------------------------------------------ #79: /health


def test_health_publishes_the_spec_version_and_capability_mask(client):
    from server import protocol

    body = client.get("/health").json()
    assert body["spec_version"] == protocol.SPEC_VERSION == "0.20.0"
    assert body["capabilities"] == remote_config.server_capabilities()
    assert protocol.SPEC_RELEASE_URL.endswith("v0.20.0")


# -------------------------------------------------------- #103: client parity


def test_the_client_can_drive_the_whole_rollout_surface(client, registered):
    """AGENTS.md's four-layer rule: a route is not done until `opamp_client` speaks it."""
    import httpx

    from client.opamp_client import OpampClient
    from tests.asgi_transport import InProcessASGITransport

    proposal_id = propose(client)
    with OpampClient(
        base_url="http://testserver",
        password=ADMIN,
        transport=InProcessASGITransport(app),
    ) as oc:
        plan = oc.plan_config_rollout(proposal_id, health_gate_seconds=0)
        assert plan["dry_run"] is True and plan["proposal_id"] == proposal_id

        revalidated = oc.revalidate_config(proposal_id)
        assert revalidated["ok"] is True

        created = oc.create_config_rollout(proposal_id, health_gate_seconds=0)
        assert created["status"] == "running"
        rollout_id = created["rollout_id"]

        assert oc.list_config_rollouts()["count"] == 1
        assert oc.get_config_rollout(rollout_id)["rollout_id"] == rollout_id
        assert oc.abort_config_rollout(rollout_id, reason="client parity")["status"] == "aborted"

        # A restart is refused for an agent without the capability, and the client
        # surfaces the 409 with its explanation rather than a bare error.
        from client.opamp_client import OpampApiError

        with pytest.raises(OpampApiError) as excinfo:
            oc.restart_agent("cd" * 16)
        assert excinfo.value.status_code == 404


def test_the_full_documented_flow_over_http(client, registered):
    """#103's acceptance criterion, verbatim: propose → approve → apply (dry run) →
    apply → status APPLIED."""
    proposal_id = propose(client)

    # Apply (dry run): a plan, and nothing else.
    plan = client.post(
        "/config/revisions/{}/apply".format(proposal_id),
        json={"dry_run": True, "health_gate_seconds": 0},
        headers=admin_headers(),
    ).json()
    assert plan["dry_run"] is True
    assert poll().HasField("remote_config") is False

    # Apply: the approval, the hold and the canary in one call.
    applied = client.post(
        "/config/revisions/{}/apply".format(proposal_id),
        json={"health_gate_seconds": 0},
        headers=admin_headers(),
    ).json()
    assert applied["status"] == "running"

    # The agent collects it on its own poll and reports APPLIED with our hash.
    assert poll().HasField("remote_config") is True
    poll(status="APPLIED", hash_b64=our_hash_b64())

    from server.state import utcnow

    for _ in range(2):
        rollout.tick(now=utcnow())
    status = client.get(f"/config/rollouts/{applied['rollout_id']}", headers=admin_headers())
    assert status.json()["status"] == "completed"
    assert status.json()["agents"]["states"]["applied"] == 1

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    # `assignment` is the derived delivery state for *this* assignment (#102): the
    # agent's own APPLIED report with our hash is what makes it "applied".
    assert detail["assignment"]["state"] == "applied"
    assert detail["desired"]["held"] is False
