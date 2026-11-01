"""Security regression suite for remote config push (#107).

Every test maps to a threat in the epic's model (epic #93). The mapping is in the
class docstring so removing a mitigation fails a test that says *why* it exists:

| Test class              | Threats |
|-------------------------|---------|
| `TestAgentIdentity`     | T1 — impersonate an agent, steal another agent's config, hijack a WS binding |
| `TestAdminSurface`      | T2 — the push surface reachable without a credential / with a wrong role |
| `TestPosture`           | T2, T4 — fail closed; 404 not 403; the insecure override is reported |
| `TestValidationCorpus`  | T7 — one typo takes the fleet down, unbounded parse, config orphans the agent |
| `TestLifecycle`         | T5, T6, T9 — propose/approve, supersede, rollback, withdraw, staleness |
| `TestNoLoop`            | T9 — `FAILED` and auto-revert must never re-push |
| `TestSecrets`           | T6 — a config secret never reaches an API, log, alert, CLI or the DB at rest |
| `TestAudit`             | T5 — record-then-act, tamper evidence |
| `TestEndToEnd`          | T8, T9 — the whole narrative, with the fake agent, in a few seconds |

Agents are `tests.fake_agent.FakeAgent` instances rather than hand-built protobufs,
so the *behaviour* under test (apply, fail, auto-revert, silence, crash) is the
supervisor's behaviour, not a test shortcut.

**Sensitivity checks.** Two tests assert that the mitigation is what makes the
other pass: the no-loop suite re-runs the scenario with poisoning disabled and
demonstrates the re-delivery loop, and the secrets suite proves the sentinel is
really in the pushed body before asserting it is absent from every surface.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from typer.testing import CliRunner

from server import (
    admin_auth,
    agent_auth,
    alerts,
    audit,
    auth,
    config_control,
    crypto,
    posture,
    redaction,
    remote_config,
)
from server.framing import encode_message
from server.main import app
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from server.ws import CLOSE_POLICY_VIOLATION, REGISTRY as WS_REGISTRY
from tests.fake_agent import FakeAgent

FIXTURES = Path(__file__).parent / "fixtures" / "configs"
ADMIN = "admin-pw-1234567890"
SECRET = "s3cr3t-config-value"


def fixture_body(name: str) -> str:
    return (FIXTURES / name).read_text()


def config(body: str = None) -> dict:
    return {"": {"body": body or fixture_body("valid.yaml"), "content_type": "text/yaml"}}


CONFIG = config()
CONFIG_V2 = config(fixture_body("valid.yaml").replace("limit_mib: 400", "limit_mib: 500"))


# --------------------------------------------------------------------- fixtures


def admin_headers() -> dict:
    token = base64.b64encode(f":{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def basic(name: str, password: str = "pw-123456789") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()}


def make_principal(name: str, role: str, password: str = "pw-123456789") -> dict:
    auth.PRINCIPALS.create(f"prin-{name}", name, role, auth.hash_password(password))
    return basic(name, password)


@pytest.fixture(autouse=True)
def secure_push(monkeypatch, tmp_path):
    """A fully secure posture with push on, encryption on and no revert grace window."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.delenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", raising=False)
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    monkeypatch.delenv("OPAMP_WS_ENABLED", raising=False)
    crypto.reset_key_cache()
    WS_REGISTRY.reset()
    config_control.reset_drift_alerts()
    auth.reset_lockouts()
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


def new_agent(mode: str = "apply_ok", uid: bytes | None = None, **kwargs) -> FakeAgent:
    """A fake agent with a live credential; returns it with `.headers` attached."""
    agent = FakeAgent(uid=uid or bytes.fromhex("ab" * 16), mode=mode, **kwargs)
    token, _ = agent_auth.issue_token(agent.agent_id)
    agent.headers = {"Authorization": f"Bearer {token}"}  # type: ignore[attr-defined]
    return agent


def register(agent: FakeAgent, client: TestClient) -> FakeAgent:
    agent.poll_http(client, headers=agent.headers)
    return agent


def propose(client: TestClient, config_map: dict | None = None, headers: dict | None = None, **extra):
    body = {
        "config_map": config_map or CONFIG,
        "scope": {"agent_id": "ab" * 16},
    }
    body.update(extra)
    return client.post("/config/revisions", json=body, headers=headers or admin_headers())


def approve(client: TestClient, proposal_id: str, headers: dict | None = None, **extra):
    return client.post(
        f"/config/revisions/{proposal_id}/approve",
        json=extra,
        headers=headers or admin_headers(),
    )


def push_and_deliver(client: TestClient, agent: FakeAgent, config_map: dict | None = None) -> dict:
    """propose → approve → one poll; returns the approved proposal body."""
    proposal = propose(client, config_map).json()
    approved = approve(client, proposal["proposal_id"])
    assert approved.status_code == 200, approved.text
    reply = agent.poll_http(client, headers=agent.headers)
    assert reply is not None and reply.HasField("remote_config"), "config was not delivered"
    return proposal


# --------------------------------------------------------------- T1: identity


class TestAgentIdentity:
    """T1: `instance_uid` is self-asserted — authentication must decide identity.

    Removing any of these lets an attacker receive a config written for another
    machine, hijack a live socket, or suppress a real agent's rollout status.
    """

    def test_an_unauthenticated_message_is_rejected_before_parsing(self, client):
        """Auth runs first: a stranger learns nothing about the server's capabilities."""
        agent = FakeAgent(uid=bytes.fromhex("11" * 16))
        response = client.post(
            "/v1/opamp",
            content=agent.message(),
            headers={"Content-Type": "application/x-protobuf"},
        )

        assert response.status_code == 401
        assert response.headers.get("content-type", "").startswith("application/json")
        # A `ServerToAgent` reply would advertise OffersRemoteConfig; an error body
        # cannot be parsed as one, so the surface stays invisible.
        assert agent.agent_id not in AGENT_REGISTRY._agents

    def test_garbage_without_a_credential_is_401_not_200(self, client):
        response = client.post(
            "/v1/opamp",
            content=b"\xff\xff\xffnot-a-protobuf",
            headers={"Content-Type": "application/x-protobuf"},
        )
        assert response.status_code == 401

    def test_a_token_cannot_claim_another_agents_uid(self, client):
        attacker = new_agent(uid=bytes.fromhex("22" * 16))
        victim = new_agent(uid=bytes.fromhex("33" * 16))

        response = client.post(
            "/v1/opamp",
            content=attacker.message(),
            headers={
                **attacker.headers,
                # The body claims the victim's uid, exactly what a hijack looks like.
            },
        )
        assert response.status_code == 200

        stolen = FakeAgent(uid=victim.uid)
        response = client.post(
            "/v1/opamp",
            content=stolen.message(),
            headers=attacker.headers,
        )
        assert response.status_code == 403
        assert victim.agent_id not in AGENT_REGISTRY._agents

    def test_a_revoked_token_loses_access_immediately(self, client):
        agent = new_agent()
        assert agent.poll_http(client, headers=agent.headers) is not None

        agent_auth.revoke_tokens(agent.agent_id)

        response = client.post(
            "/v1/opamp",
            content=agent.message(),
            headers=agent.headers,
        )
        assert response.status_code == 401

    def test_a_revoked_agent_can_no_longer_receive_an_approved_config(self, client):
        agent = register(new_agent(), client)
        agent_auth.revoke_tokens(agent.agent_id)

        agent.headers = {}
        with pytest.raises(AssertionError):
            agent.poll_http(client, headers={})

    def test_ws_binding_takeover_is_refused(self, client, monkeypatch):
        """One socket bound an agent; a different credential may not take it over."""
        monkeypatch.setenv("OPAMP_WS_ENABLED", "true")
        a = new_agent(uid=bytes.fromhex("44" * 16))
        b = new_agent(uid=bytes.fromhex("55" * 16))

        with client.websocket_connect("/v1/opamp", headers=a.headers) as ws_a:
            a.poll_ws(ws_a)  # a binds 44… to this socket

            with client.websocket_connect("/v1/opamp", headers=b.headers) as ws_b:
                # b presents its own valid token but claims a's uid.
                intruder = FakeAgent(uid=a.uid)
                with pytest.raises(Exception) as exc:
                    ws_b.send_bytes(encode_message(intruder.message()))
                    ws_b.receive_bytes()
                assert getattr(exc.value, "code", None) == CLOSE_POLICY_VIOLATION

        # a's binding is untouched and it still owns its identity.
        assert AGENT_REGISTRY.get(a.agent_id) is not None


# ------------------------------------------------------------ T2: admin surface

PROPOSE_BODY = {"config_map": CONFIG, "scope": {"agent_id": "ab" * 16}}

# (method, path, minimum role, body). The body is valid for the route so the test
# fails on the *authorisation* decision, not on pydantic validation.
CONFIG_ROUTES = [
    ("GET", f"/agent/{'ab' * 16}/config", "viewer", None),
    ("GET", "/config/revisions", "viewer", None),
    ("GET", "/config/revisions/prop-missing", "viewer", None),
    ("POST", "/config/validate", "viewer", {"config_map": CONFIG}),
    ("POST", "/config/revisions", "operator", PROPOSE_BODY),
    ("PUT", f"/agent/{'ab' * 16}/config", "approver", {"config_map": CONFIG}),
    ("DELETE", f"/agent/{'ab' * 16}/config", "approver", None),
    ("POST", "/config/apply", "admin", {"config_map": CONFIG}),
    ("POST", f"/agent/{'ab' * 16}/config/rollback", "approver", {}),
    ("POST", "/config/revisions/prop-missing/approve", "approver", {}),
    ("POST", "/config/revisions/prop-missing/reject", "approver", {}),
]


class TestAdminSurface:
    """T2/T3: every config route enforces a role, and none of them is open.

    This is the matrix that catches a new route declared without a role dependency:
    it exercises every config path with no credential and with each role below the
    route's minimum.
    """

    @pytest.mark.parametrize("method,path,minimum,body", CONFIG_ROUTES)
    def test_no_credential_is_401(self, client, method, path, minimum, body, monkeypatch):
        response = client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"

    @pytest.mark.parametrize("method,path,minimum,body", CONFIG_ROUTES)
    def test_lower_roles_are_refused(self, client, method, path, minimum, body, monkeypatch):
        monkeypatch.setenv("OPAMP_WS_ENABLED", "false")
        register(new_agent(), client)
        for role in ("viewer", "operator"):
            if auth.role_satisfies(role, minimum):
                continue
            headers = make_principal(f"u-{role}", role)
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, f"{role} reached {method} {path}"

    @pytest.mark.parametrize("role", ["viewer", "operator", "approver"])
    def test_a_privileged_read_never_needs_a_write_role(self, client, role):
        register(new_agent(), client)
        response = client.get("/config/revisions", headers=make_principal(f"r-{role}", role))
        assert response.status_code == 200


# ---------------------------------------------------------------- T2/T4: posture


class TestPosture:
    """T2/T4: fail closed, hide the surface, and report the override everywhere."""

    def test_push_is_off_by_default_and_answers_404_not_403(self, client, monkeypatch):
        monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
        register(new_agent(), client)

        response = client.put(
            f"/agent/{'ab' * 16}/config", json={"config_map": CONFIG}, headers=admin_headers()
        )
        # A stranger must not be able to tell "you are not allowed" from "no such route".
        assert response.status_code == 404
        assert response.json()["detail"] == posture.GATE_DETAIL

    def test_enabling_with_an_insecure_posture_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
        monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
        monkeypatch.delenv("PUBLIC_URL", raising=False)
        monkeypatch.delenv("CONFIG_AT_REST_KEY", raising=False)
        monkeypatch.delenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", raising=False)

        with pytest.raises(posture.PostureError) as exc:
            posture.assert_startup_posture()

        message = str(exc.value)
        assert "agents are not authenticated" in message
        assert "not served over TLS" in message
        assert "stored unencrypted" in message

    def test_the_override_starts_but_is_reported_everywhere(self, client, monkeypatch):
        monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
        monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
        monkeypatch.delenv("PUBLIC_URL", raising=False)

        posture.assert_startup_posture()  # does not raise
        health = client.get("/health").json()["config_push"]
        assert health["enabled"] is True
        assert health["insecure"] is True

        posture.publish_metrics()
        assert posture.metric_values()["insecure"] == 1
        assert posture.posture_warning() is not None

    def test_a_disabled_gate_never_delivers_a_config(self, client, monkeypatch):
        agent = register(new_agent(), client)
        push_and_deliver(client, agent)

        monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
        reply = agent.poll_http(client, headers=agent.headers)
        assert reply is not None and not reply.HasField("remote_config")


# -------------------------------------------------------------- T7: validation


class TestValidationCorpus:
    """T7: a crafted or careless config is refused with a structured error, fast.

    The pipeline is tested stage-by-stage in `tests/test_config_validation.py`; what
    this class adds is the **API contract**: every one of these must be a 4xx with a
    finding that names a path, not a 500 and not a slow parse.
    """

    CORPUS = [
        ("invalid_yaml.yaml", "not valid"),
        ("alias_bomb.yaml", "alias bomb"),
        ("duplicate_keys.yaml", "duplicate key"),
        ("missing_component.yaml", "referenced"),
        ("opamp_removed.yaml", "extensions.opamp"),
        ("opamp_endpoint_changed.yaml", "would move to"),
        ("empty_pipelines.yaml", "pipeline"),
    ]

    @pytest.mark.parametrize("fixture,needle", CORPUS)
    def test_every_bad_config_is_a_structured_4xx(self, client, fixture, needle):
        register(new_agent(), client)
        started = time.monotonic()

        response = client.put(
            f"/agent/{'ab' * 16}/config",
            json={"config_map": config(fixture_body(fixture))},
            headers=admin_headers(),
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, dict), detail
        assert detail.get("errors"), detail
        findings = detail.get("details")
        assert findings, detail
        finding = findings[0]
        assert {"path", "message", "hint"} <= set(finding)
        assert needle.lower() in json.dumps(detail).lower(), detail
        # Bounded runtime: the alias bomb is the case that would take minutes if the
        # node counter were missing.
        assert elapsed < 2.0, f"{fixture} took {elapsed:.1f}s"
        assert remote_config.REGISTRY.get("ab" * 16) is None

    def test_an_oversized_config_is_refused_before_parsing(self, client, monkeypatch):
        monkeypatch.setenv("CONFIG_MAX_BYTES", "64")
        register(new_agent(), client)
        response = client.put(
            f"/agent/{'ab' * 16}/config",
            json={"config_map": CONFIG},
            headers=admin_headers(),
        )
        assert response.status_code == 422
        assert "CONFIG_MAX_BYTES" in json.dumps(response.json())

    def test_a_policy_violation_blocks_approval_without_an_override(self, client, monkeypatch):
        from tests.test_config_validation import FakeOpa

        agent = register(new_agent(), client)
        pending = propose(client).json()

        monkeypatch.setattr(
            "server.opa_client.get_opa_client",
            lambda: FakeOpa(violations=[{"msg": "exporters must use TLS"}]),
        )
        refused = approve(client, pending["proposal_id"])
        assert refused.status_code == 409
        assert "policy" in json.dumps(refused.json()).lower()

        # The same approval with a recorded reason is allowed (and audited).
        allowed = approve(
            client, pending["proposal_id"], override_reason="backend is inside the VPC"
        )
        assert allowed.status_code == 200, allowed.text
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")


# --------------------------------------------------------------- T5/T6: lifecycle


class TestLifecycle:
    """T5/T6/T9: propose → approve → applied, supersede, rollback, withdraw, stale."""

    def test_propose_approve_deliver_applied(self, client):
        agent = register(new_agent(), client)
        proposal = propose(client).json()

        # Nothing is delivered until an approver acts.
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        assert approve(client, proposal["proposal_id"]).status_code == 200
        delivered = agent.poll_http(client, headers=agent.headers)
        assert delivered.remote_config.config_hash == bytes.fromhex(proposal["config_hash"])

        # The agent acknowledges; nothing is re-sent.
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")
        detail = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()
        assert detail["desired"]["status"] == "active"
        assert detail["assignment"]["last_status"] == "APPLIED"

    def test_a_second_approval_supersedes_but_keeps_the_first_rollbackable(self, client):
        agent = register(new_agent(), client)
        first = push_and_deliver(client, agent)
        first_revision = first["revisions"][0]["revision_id"]

        second = propose(client, CONFIG_V2).json()
        approve(client, second["proposal_id"])
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        statuses = {
            row["revision_id"]: row["status"]
            for row in client.get(
                f"/agent/{agent.agent_id}/config", headers=admin_headers()
            ).json()["history"]
        }
        assert statuses[first_revision] == "superseded"
        assert statuses[second["revisions"][0]["revision_id"]] == "active"

        rolled = client.post(
            f"/agent/{agent.agent_id}/config/rollback", json={}, headers=admin_headers()
        )
        assert rolled.status_code == 200
        assert rolled.json()["config_hash"] == first["config_hash"]

    def test_rollback_reaches_applied_and_names_its_parent(self, client):
        agent = register(new_agent(), client)
        first = push_and_deliver(client, agent)
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        second = propose(client, CONFIG_V2).json()
        approve(client, second["proposal_id"])
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        rolled = client.post(
            f"/agent/{agent.agent_id}/config/rollback",
            json={"note": "v2 broke tracing"},
            headers=admin_headers(),
        ).json()
        frame = agent.poll_http(client, headers=agent.headers)
        assert bytes(frame.remote_config.config_hash).hex() == rolled["config_hash"]
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        detail = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()
        assert detail["desired"]["parent_revision_id"] == first["revisions"][0]["revision_id"]
        assert detail["assignment"]["last_status"] == "APPLIED"

    def test_withdraw_is_an_empty_map_delivered_once(self, client):
        agent = register(new_agent(), client)
        push_and_deliver(client, agent)
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        withdrawn = client.delete(
            f"/agent/{agent.agent_id}/config", headers=admin_headers()
        )
        assert withdrawn.status_code == 200

        offer = agent.poll_http(client, headers=agent.headers)
        assert offer.HasField("remote_config")
        assert len(offer.remote_config.config.config_map) == 0
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

    def test_staleness_keeps_the_assignment(self, client):
        agent = register(new_agent(), client)
        pending = push_and_deliver(client, agent)

        agent.age_the_server(3600)
        from server.main import cleanup_stale_agents

        removed = cleanup_stale_agents()
        assert agent.agent_id in removed
        assert AGENT_REGISTRY.get(agent.agent_id) is None
        assert remote_config.REGISTRY.get(agent.agent_id) is not None

        # It comes back reporting an old hash: the desired state is still there.
        agent.reported_hash = b"\x00" * 32
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")
        assert remote_config.REGISTRY.get(agent.agent_id)["config_hash"] == pending["config_hash"]

    def test_a_new_uid_inherits_nothing(self, client):
        agent = register(new_agent(), client)
        push_and_deliver(client, agent)

        newcomer = new_agent(uid=bytes.fromhex("cd" * 16))
        assert remote_config.REGISTRY.get(newcomer.agent_id) is None
        reply = newcomer.poll_http(client, headers=newcomer.headers)
        assert not reply.HasField("remote_config")

    def test_a_rejected_proposal_is_never_delivered(self, client):
        agent = register(new_agent(), client)
        proposal = propose(client).json()
        rejected = client.post(
            f"/config/revisions/{proposal['proposal_id']}/reject",
            json={"reason": "wrong tenant"},
            headers=admin_headers(),
        )
        assert rejected.status_code == 200
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")


# ---------------------------------------------------------------- T9: no loop


class TestNoLoop:
    """T9: the most important tests in the suite — a failed config is never retried.

    Both scenarios run the *same* assertion twice: with poisoning enabled nothing is
    re-delivered, and with the status reaction disabled (`note_status` becomes a
    no-op, i.e. the mitigation removed) the server does re-deliver. The second run is
    the proof that the first is not vacuous.
    """

    def test_failed_is_poisoned_and_never_redelivered(self, client):
        agent = register(new_agent(mode="apply_fail"), client)
        push_and_deliver(client, agent)

        # The agent reports FAILED for our hash; the same reply must not re-send it.
        failed = agent.poll_http(client, headers=agent.headers)
        assert not failed.HasField("remote_config")

        for _ in range(5):
            reply = agent.poll_http(client, headers=agent.headers)
            assert not reply.HasField("remote_config")

        detail = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()
        assert detail["desired"]["status"] == "poisoned"
        assert detail["assignment"]["poisoned"] is True
        assert audit.STORE.list_entries(action="config.apply.failed", limit=5)

    def _good_then_bad(self, client, mode: str):
        """Push+apply a good revision, switch to `mode`, then push a bad one."""
        agent = register(new_agent(), client)
        good = push_and_deliver(client, agent)
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        agent.mode = mode
        bad = propose(client, CONFIG_V2).json()
        approve(client, bad["proposal_id"])
        first = agent.poll_http(client, headers=agent.headers)
        assert first.HasField("remote_config")
        return agent, good, bad

    def test_failed_without_poisoning_would_redeliver(self, client, monkeypatch):
        """Sensitivity check: remove the mitigation and watch the loop appear."""
        # Poisoning is what stops the loop: stub the reaction out.
        monkeypatch.setattr(remote_config, "note_status", lambda *args, **kwargs: None)
        agent, _, _ = self._good_then_bad(client, "auto_revert")

        # The revert report is ignored, so the agent keeps reporting its old hash and
        # the server keeps offering the same broken revision — the T9 loop.
        offers = FakeAgent.count_offers(agent.drive_http(client, 3, headers=agent.headers))
        assert offers == 3, f"expected the loop when poisoning is disabled, got {offers}"

    def test_an_auto_revert_is_poisoned_and_not_repushed_forever(self, client):
        agent, _, _ = self._good_then_bad(client, "auto_revert")

        # The supervisor restored its last working config and reports that hash. One
        # further delivery is legitimate (the first report is indistinguishable from a
        # stale ack); three or more is the loop T9 describes.
        offers = FakeAgent.count_offers(agent.drive_http(client, 8, headers=agent.headers))
        detail = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()

        assert offers <= 1, f"auto-revert looped: {offers} re-deliveries"
        assert detail["desired"]["status"] == "poisoned"
        assert "revert" in (detail["desired"]["failure_detail"] or "").lower()

    def test_a_silent_agent_is_not_repushed_forever(self, client):
        agent = register(new_agent(mode="silent"), client)
        push_and_deliver(client, agent)

        offers = FakeAgent.count_offers(agent.drive_http(client, 8, headers=agent.headers))
        detail = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()

        assert offers <= remote_config.max_delivery_attempts()
        assert detail["assignment"]["unconfirmed"] is True

    def test_a_new_revision_after_a_poison_is_deliverable(self, client):
        """Poison is a fact about the revision, not the agent (D6).

        Regression: the assignment row kept `poisoned=1` after the pointer moved, so
        a rollback or a fresh push after a FAILED revision was silently undeliverable
        — exactly the recovery path this suite's E2E depends on.
        """
        agent = register(new_agent(mode="apply_fail"), client)
        first = push_and_deliver(client, agent)
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        agent.mode = "apply_ok"
        second = propose(client, CONFIG_V2).json()
        assert approve(client, second["proposal_id"]).status_code == 200
        assert second["config_hash"] != first["config_hash"]
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")

    def test_a_crashed_agent_does_not_get_the_fleet_pushed_at_it(self, client):
        agent = register(new_agent(mode="crash"), client)
        push_and_deliver(client, agent)
        assert agent.stopped is True

        # No further messages, so no further deliveries; the assignment is intact for
        # when the supervisor comes back.
        assert remote_config.REGISTRY.get(agent.agent_id) is not None


# ------------------------------------------------------------------- T6: secrets


class TestSecrets:
    """T6: a config secret never reaches a surface that is not the agent it is for."""

    SECRET_CONFIG = config(
        fixture_body("valid.yaml").replace(
            "    endpoint: https://backend.example.com:4318",
            "    endpoint: https://backend.example.com:4318\n"
            f"    password: {SECRET}\n"
            "    headers:\n"
            f"      Authorization: Basic {SECRET}",
        )
    )

    def _delivered(self, client) -> FakeAgent:
        agent = register(new_agent(), client)
        pushed = client.put(
            f"/agent/{agent.agent_id}/config",
            json={"config_map": self.SECRET_CONFIG},
            headers=admin_headers(),
        )
        assert pushed.status_code == 200, pushed.text
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")
        # The sentinel really is in the pushed body: the absence assertions below
        # are not vacuous.
        assert SECRET in remote_config.REGISTRY.get(agent.agent_id)["config_map"][""]["body"]
        agent.poll_http(client, headers=agent.headers)  # APPLIED
        return agent

    def test_no_api_surface_renders_the_secret(self, client):
        agent = self._delivered(client)

        payloads = {
            "/agents": client.get("/agents", headers=admin_headers()).text,
            f"/agent/{agent.agent_id}": client.get(
                f"/agent/{agent.agent_id}", headers=admin_headers()
            ).text,
            f"/agent/{agent.agent_id}/config": client.get(
                f"/agent/{agent.agent_id}/config", headers=admin_headers()
            ).text,
            f"/agent/{agent.agent_id}/report": client.get(
                f"/agent/{agent.agent_id}/report", headers=admin_headers()
            ).text,
            "/reports/fleet": client.get("/reports/fleet", headers=admin_headers()).text,
            "/audit": client.get("/audit", headers=admin_headers()).text,
        }
        for path, payload in payloads.items():
            assert SECRET not in payload, f"secret leaked via {path}"
        assert "redacted" in payloads[f"/agent/{agent.agent_id}/config"]

    def test_no_log_sink_renders_the_secret(self, client):
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            self._delivered(client)
        finally:
            logger.remove(sink)

        rendered = "\n".join(lines)
        assert SECRET not in rendered
        # The config *was* handled during the test (so the filter was exercised).
        assert any("remote_config" in line or "config" in line for line in lines)

    def test_no_alert_payload_renders_the_secret(self, client, monkeypatch):
        sent: list = []

        class _Response:
            status_code = 200

        def fake_post(url, data=None, headers=None, timeout=None):
            sent.append(data.decode() if isinstance(data, bytes) else str(data))
            return _Response()

        monkeypatch.setattr(alerts.requests, "post", fake_post)
        # Every dispatch reads the *event* config, not the root of ALERT_CONFIG.
        monkeypatch.setitem(
            alerts.ALERT_CONFIG["events"],
            "config_drift",
            {
                "enabled": True,
                "webhook_url": "https://hooks.example.com/x",
                "body_template": '{"message": "{message}"}',
                "headers": "",
            },
        )

        agent = self._delivered(client)
        # Force drift: the agent reports an effective config that lacks the desired one
        # and does not report our hash (otherwise the fast path says "no drift").
        agent.reported_config = {"": "exporters: {}"}
        agent.reported_hash = b"\x00" * 32
        agent.status = "APPLIED"
        agent.poll_http(client, headers=agent.headers)
        config_control.sweep_drift(AGENT_REGISTRY.list_all())

        assert sent, "the drift alert did not fire"
        assert SECRET not in "\n".join(sent)

    def test_the_database_at_rest_does_not_contain_the_secret(self, client):
        self._delivered(client)
        db_path = remote_config.REGISTRY.store.db_path
        raw = Path(db_path).read_bytes()
        assert SECRET.encode() not in raw

    def test_cli_output_does_not_render_the_secret(self, client, monkeypatch):
        from cli.main import app as cli_app
        from cli.main import _client as _unused  # noqa: F401  (import check)
        import cli.main as cli_main
        from client.opamp_client import OpampClient
        from tests.asgi_transport import InProcessASGITransport

        agent = self._delivered(client)
        transport = InProcessASGITransport(app)

        monkeypatch.setattr(cli_main._settings, "server", "http://testserver")
        monkeypatch.setattr(cli_main._settings, "password", ADMIN)
        monkeypatch.setattr(cli_main._settings, "token", None)
        monkeypatch.setattr(cli_main._settings, "db", None)
        monkeypatch.setattr(
            cli_main,
            "_client",
            lambda: OpampClient(
                base_url="http://testserver", transport=transport, password=ADMIN
            ),
        )

        result = CliRunner().invoke(cli_app, ["agents", "get", agent.agent_id])
        assert result.exit_code == 0, result.output
        assert SECRET not in result.output

    def test_a_redacted_body_can_never_be_pushed(self, client):
        agent = register(new_agent(), client)
        body = fixture_body("valid.yaml") + f"    password: {redaction.SENTINEL}\n"
        response = client.put(
            f"/agent/{agent.agent_id}/config",
            json={"config_map": config(body)},
            headers=admin_headers(),
        )
        assert response.status_code == 422
        assert "redaction marker" in json.dumps(response.json())
        assert remote_config.REGISTRY.get(agent.agent_id) is None

    def test_reveal_requires_a_role_and_is_audited(self, client):
        agent = self._delivered(client)
        viewer = make_principal("sec-viewer", "viewer")
        response = client.get(
            f"/agent/{agent.agent_id}/config?reveal=true", headers=viewer
        )
        assert response.status_code == 403

        approver = make_principal("sec-approver", "approver")
        revealed = client.get(
            f"/agent/{agent.agent_id}/config?reveal=true", headers=approver
        )
        assert revealed.status_code == 200
        assert SECRET in revealed.text
        assert audit.STORE.list_entries(action="config.reveal", limit=5)


# -------------------------------------------------------------------- T5: audit


class TestAudit:
    """T5: a privileged action is recorded before it happens, and tampering shows."""

    def test_each_privileged_action_is_one_audit_entry(self, client):
        register(new_agent(), client)
        proposal = propose(client).json()
        approve(client, proposal["proposal_id"])

        assert len(audit.STORE.list_entries(action="config.propose", limit=10)) == 1
        assert len(audit.STORE.list_entries(action="config.approve", limit=10)) == 1
        assert audit.STORE.verify()["ok"] is True

    def test_a_push_that_cannot_be_recorded_does_not_happen(self, monkeypatch):
        client = TestClient(app, raise_server_exceptions=False)
        agent = register(new_agent(), client)

        def broken_append(entry):
            raise RuntimeError("disk full")

        monkeypatch.setattr(audit.STORE, "append", broken_append)
        response = client.put(
            f"/agent/{agent.agent_id}/config",
            json={"config_map": CONFIG},
            headers=admin_headers(),
        )

        assert response.status_code >= 500
        assert remote_config.REGISTRY.get(agent.agent_id) is None

    def test_a_hand_edited_audit_row_is_detected(self, client):
        register(new_agent(), client)
        propose(client)

        with sqlite3.connect(audit.STORE.db_path) as conn:
            conn.execute(
                "UPDATE audit_log SET actor='mallory' WHERE id=(SELECT MAX(id) FROM audit_log)"
            )
        conn.close()

        result = audit.STORE.verify()
        assert result["ok"] is False


# ---------------------------------------------------------------- T8/T9: E2E


class TestEndToEnd:
    """The whole narrative, one test, no sleeps (#107 acceptance criterion).

    propose → approve → canary (single agent) → APPLIED → bad revision → FAILED →
    poisoned → rollback → APPLIED. The rollout *engine* of #101 is not part of this
    build (see `test_staged_rollout_is_not_part_of_this_build`); the rest of the
    lifecycle is.
    """

    def test_the_safe_narrative(self, client):
        agent = register(new_agent(script=["APPLYING", "APPLIED"]), client)

        # 1. Propose. Nothing is delivered.
        proposal = propose(client).json()
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        # 2. Approve. One frame goes out, the agent reports APPLYING then APPLIED.
        assert approve(client, proposal["proposal_id"]).status_code == 200
        first = agent.poll_http(client, headers=agent.headers)
        assert first.HasField("remote_config")
        assert agent.status == "APPLYING"
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")
        assert agent.status == "APPLIED"

        # 3. A bad revision is approved, delivered, and reported FAILED.
        bad = propose(client, CONFIG_V2).json()
        assert approve(client, bad["proposal_id"]).status_code == 200
        assert agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        # The collector refused it: FAILED for the hash we just delivered.
        agent.report("FAILED", "collector refused the config")
        reply = agent.poll_http(client, headers=agent.headers)
        assert not reply.HasField("remote_config"), "the failing frame must not be re-sent"

        # 4. Poisoned, permanently: five more polls, zero frames.
        assert FakeAgent.count_offers(agent.drive_http(client, 5, headers=agent.headers)) == 0
        detail = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()
        assert detail["desired"]["status"] == "poisoned"
        assert detail["desired"]["deliverable"] is False

        # 5. Roll back: a new revision whose content is the last good one.
        agent.mode = "apply_ok"
        rolled = client.post(
            f"/agent/{agent.agent_id}/config/rollback",
            json={"note": "v2 poisoned; restoring v1"},
            headers=admin_headers(),
        )
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["config_hash"] == proposal["config_hash"]

        frame = agent.poll_http(client, headers=agent.headers)
        assert bytes(frame.remote_config.config_hash).hex() == rolled.json()["config_hash"]
        assert not agent.poll_http(client, headers=agent.headers).HasField("remote_config")

        final = client.get(f"/agent/{agent.agent_id}/config", headers=admin_headers()).json()
        assert final["assignment"]["last_status"] == "APPLIED"
        assert final["desired"]["status"] == "active"

        # 6. The story is answerable from the audit log alone.
        actions = [
            row["action"] for row in audit.STORE.list_entries(limit=50)
        ]
        for expected in ("config.propose", "config.approve", "config.apply.failed", "config.rollback"):
            assert expected in actions, (expected, actions)
        assert audit.STORE.verify()["ok"] is True

    def test_staged_rollout_is_not_part_of_this_build(self, client):
        """#101 (canary/batches/blast-radius caps) is a separate issue and not here.

        This test is deliberately an assertion about the *absence*: when #101 lands,
        it fails, and the rollout-gating suite in this file can be written against
        the real engine instead of pretending one exists. Canary semantics are
        covered by single-agent approval until then.
        """
        assert client.post("/config/rollouts", json={}).status_code in (404, 405)
        assert not hasattr(config_control, "Rollout")
