"""Append-only, hash-chained audit log (#97).

What this file is guarding, in the issue's own terms:

- **Every privileged action produces exactly one entry, naming the principal.**
  One row for a successful action — a second (`result="error"`) only when the
  action itself fails after being recorded.
- **Fail closed.** If the entry cannot be written, the privileged action does not
  happen. That is the difference between an audit log and a log file, and it is
  tested by breaking the store and asserting nothing was minted.
- **No bodies, no secrets.** Hashes, sizes, counts and verdict codes only; fields
  that look like credentials are dropped at the seam, so a caller that passes a
  token by accident cannot store it.
- **Tamper-evident.** Editing, deleting or reordering a row breaks `verify()`, and
  a retention prune is the one legitimate gap — it records what it cut.
- **Denials never 500.** `audit_event` is best effort with a circuit breaker; an
  agent auth failure that crashes the OpAMP endpoint because the disk is full
  would be an outage caused by bookkeeping.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from server import admin_auth, agent_auth, audit, auth
from server.audit import AuditError, audit_event, audit_required
from server.main import app
from server.state import AUDIT_GENESIS, SQLiteAuditStore, utcnow

PASSWORD = "correct-horse-battery-staple"
AGENT = "4f3c2b1a" * 4  # 32 hex chars = a 16-byte instance UID


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.delenv("OPAMP_AUDIT_ENABLED", raising=False)
    monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
    audit.reset_circuit_breaker()
    audit.reset_prune_timer()
    yield
    audit.reset_circuit_breaker()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def raw_client():
    """Client that returns 500s instead of raising, for fail-closed assertions."""
    return TestClient(app, raise_server_exceptions=False)


def admin_auth_headers() -> dict:
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f":{PASSWORD}".encode()).decode()}


def rows(**filters):
    return audit.STORE.list_entries(**filters)


def break_store(monkeypatch):
    """Make every append fail, as a full disk or a read-only mount would."""

    def explode(entry):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(audit.STORE, "append", explode)


# ----------------------------------------------------------------- the chain


class TestChain:
    def test_entries_chain_and_verify(self):
        for index in range(3):
            audit_event("test.event", principal="alice", index=index)
        stored = rows()
        assert len(stored) == 3
        # Newest first from list_entries; the chain is the other way round.
        chain = list(reversed(stored))
        assert chain[0]["prev_hash"] == AUDIT_GENESIS
        for previous, current in zip(chain, chain[1:]):
            assert current["prev_hash"] == previous["entry_hash"]

        report = audit.STORE.verify()
        assert report["ok"] is True
        assert report["entries"] == 3 and report["checked"] == 3
        assert report["anchored_by_checkpoint"] is False

    def test_edited_row_is_detected(self):
        for index in range(3):
            audit_event("test.event", principal="alice", index=index)
        middle = rows()[1]["id"]
        with sqlite3.connect(audit.STORE.db_path) as conn:
            conn.execute(
                "UPDATE audit_log SET result='denied' WHERE id=?", (middle,)
            )
        report = audit.STORE.verify()
        assert report["ok"] is False
        assert report["broken_at"] == middle
        assert "edited" in report["reason"]

    def test_deleted_middle_row_is_detected(self):
        for index in range(3):
            audit_event("test.event", principal="alice", index=index)
        middle = rows()[1]["id"]
        with sqlite3.connect(audit.STORE.db_path) as conn:
            conn.execute("DELETE FROM audit_log WHERE id=?", (middle,))
        report = audit.STORE.verify()
        assert report["ok"] is False
        assert "removed or reordered" in report["reason"]

    def test_deleted_first_row_without_checkpoint_is_detected(self):
        for index in range(3):
            audit_event("test.event", principal="alice", index=index)
        first = rows()[-1]["id"]
        with sqlite3.connect(audit.STORE.db_path) as conn:
            conn.execute("DELETE FROM audit_log WHERE id=?", (first,))
        report = audit.STORE.verify()
        assert report["ok"] is False
        assert "no checkpoint accounts for the gap" in report["reason"]

    def test_retention_prune_keeps_the_chain_verifiable(self):
        # Two rows well past retention, one inside it.
        audit.STORE.append(
            {
                "ts": "2020-01-01T00:00:00+00:00",
                "actor": "alice",
                "actor_kind": "human",
                "role": "admin",
                "action": "test.old",
                "result": "ok",
            }
        )
        audit.STORE.append(
            {
                "ts": "2020-01-02T00:00:00+00:00",
                "actor": "alice",
                "actor_kind": "human",
                "role": "admin",
                "action": "test.old",
                "result": "ok",
            }
        )
        audit_event("test.new", principal="alice")

        pruned = audit.STORE.prune("2021-01-01T00:00:00+00:00")
        assert pruned == 2

        report = audit.STORE.verify()
        assert report["ok"] is True
        assert report["anchored_by_checkpoint"] is True
        # The checkpoint is retained and records exactly what was cut.
        checkpoint = [row for row in rows() if row["action"] == "audit.checkpoint"]
        assert len(checkpoint) == 1
        detail = json.loads(checkpoint[0]["detail_json"])
        assert detail["pruned_rows"] == 2
        assert detail["pruned_through_hash"]

    def test_prune_never_takes_the_checkpoint_with_it(self):
        audit.STORE.append(
            {
                "ts": "2020-01-01T00:00:00+00:00",
                "actor": "alice",
                "actor_kind": "human",
                "role": "admin",
                "action": "test.old",
                "result": "ok",
            }
        )
        audit.STORE.prune("2021-01-01T00:00:00+00:00")
        # A second prune of the same window must not delete the first checkpoint.
        assert audit.STORE.prune("2021-01-01T00:00:00+00:00") == 0
        assert any(row["action"] == "audit.checkpoint" for row in rows())

    def test_maybe_prune_honours_retention_and_the_hourly_gate(self, monkeypatch):
        audit.STORE.append(
            {
                "ts": "2020-01-01T00:00:00+00:00",
                "actor": "alice",
                "actor_kind": "human",
                "role": "admin",
                "action": "test.old",
                "result": "ok",
            }
        )
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
        assert audit.maybe_prune() == 1
        # Second call inside the hour is a no-op, not another prune attempt.
        assert audit.maybe_prune() == 0

    def test_retention_zero_keeps_everything(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "0")
        audit.STORE.append(
            {
                "ts": "2020-01-01T00:00:00+00:00",
                "actor": "alice",
                "actor_kind": "human",
                "role": "admin",
                "action": "test.old",
                "result": "ok",
            }
        )
        assert audit.maybe_prune() == 0
        assert len(rows()) == 1


# --------------------------------------------------------------- classification


class TestClassification:
    def test_actor_kind_and_target_are_mapped_from_the_call_site(self):
        audit_event(
            "config.apply",
            principal="alice",
            role="operator",
            result="ok",
            target_kind="agent",
            target_id=AGENT,
            revision_id="rev_1",
            config_hash="abc123",
            extra="kept",
        )
        row = rows()[0]
        assert (row["actor"], row["actor_kind"], row["role"]) == (
            "alice",
            "human",
            "operator",
        )
        assert (row["action"], row["result"]) == ("config.apply", "ok")
        assert (row["target_kind"], row["target_id"]) == ("agent", AGENT)
        assert (row["revision_id"], row["config_hash"]) == ("rev_1", "abc123")
        assert json.loads(row["detail_json"])["extra"] == "kept"

    def test_agent_events_are_labelled_as_agents(self):
        audit_event("agent_auth.denied", result="denied", principal=AGENT, reason="unknown")
        row = rows()[0]
        assert row["actor"] == f"agent:{AGENT}"
        assert row["actor_kind"] == "agent"

    def test_missing_principal_is_anonymous_not_a_crash(self):
        audit_event("admin.auth.denied", result="denied", reason="missing")
        row = rows()[0]
        assert (row["actor"], row["actor_kind"]) == ("anonymous", "human")

    def test_secrets_never_reach_the_row(self):
        audit_event(
            "admin.login.ok",
            principal="alice",
            password="hunter2",
            token="opamp_admin_leak",
            secret_hash="deadbeef",
            nested={"api_token": "nope", "safe": "yes"},
        )
        dumped = json.dumps(rows()[0], default=str)
        assert "hunter2" not in dumped
        assert "opamp_admin_leak" not in dumped
        assert "deadbeef" not in dumped
        assert "nope" not in dumped
        detail = json.loads(rows()[0]["detail_json"])
        assert detail["nested"] == {"safe": "yes"}

    def test_detail_is_size_capped(self):
        audit_event("test.event", principal="alice", blob="x" * 20000)
        detail = rows()[0]["detail_json"]
        assert len(detail) <= audit.DETAIL_MAX_CHARS
        assert json.loads(detail)["truncated"] is True


# ------------------------------------------------------------- fail closed / open


class TestFailureModes:
    def test_audit_event_never_raises(self, monkeypatch):
        break_store(monkeypatch)
        assert audit_event("admin.login.denied", principal="alice") is None

    def test_audit_required_raises(self, monkeypatch):
        break_store(monkeypatch)
        with pytest.raises(AuditError):
            audit_required("config.apply", principal="alice")

    def test_breaker_stops_the_flood_but_keeps_logging(self, monkeypatch):
        break_store(monkeypatch)
        for _ in range(audit.BREAKER_THRESHOLD):
            audit_event("agent_auth.denied", principal=AGENT)
        assert audit.available() is False
        state = audit.breaker_state()
        assert state["tripped"] is True and state["failures"] >= audit.BREAKER_THRESHOLD
        # Still recorded as a line, just not attempted against the dead store.
        assert audit_event("agent_auth.denied", principal=AGENT) is None

        audit.reset_circuit_breaker()
        assert audit.available() is True

    def test_audit_required_ignores_the_breaker(self, monkeypatch):
        # A privileged action must fail rather than proceed unrecorded, even while
        # the best-effort path is in back-off.
        break_store(monkeypatch)
        for _ in range(audit.BREAKER_THRESHOLD + 1):
            audit_event("agent_auth.denied", principal=AGENT)
        assert audit.available() is False
        with pytest.raises(AuditError):
            audit_required("config.apply", principal="alice")

    def test_disabled_flag_refuses_privileged_actions(self, monkeypatch):
        monkeypatch.setenv("OPAMP_AUDIT_ENABLED", "false")
        assert audit.enabled() is False
        assert audit.available() is False
        with pytest.raises(AuditError):
            audit_required("config.apply", principal="alice")
        # Denials still work: the line is the fallback.
        assert audit_event("admin.login.denied", principal="alice") is None

    def test_require_audit_dependency_answers_503(self, monkeypatch):
        monkeypatch.setenv("OPAMP_AUDIT_ENABLED", "false")
        probe = FastAPI()

        @probe.get("/privileged")
        def privileged(_: None = Depends(auth.require_audit("config.apply"))):
            return {"ok": True}

        response = TestClient(probe).get("/privileged")
        assert response.status_code == 503
        assert "disabled" in response.json()["detail"]


# ------------------------------------------------------------------- privileged


class TestPrivilegedActions:
    def test_agent_token_issue_is_audited_once_with_the_principal(self, client):
        response = client.post(
            f"/agents/{AGENT}/token", headers=admin_auth_headers(), json={"label": "prod"}
        )
        assert response.status_code == 201
        entries = rows(action="agent.credential.issued")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["actor"] == "admin"
        assert entry["result"] == "ok"
        assert (entry["target_kind"], entry["target_id"]) == ("agent", AGENT)
        assert json.loads(entry["detail_json"])["label"] == "prod"
        # The token itself is never recorded, in any form.
        assert response.json()["token"] not in json.dumps(entries, default=str)

    def test_agent_token_revoke_is_audited(self, client):
        client.post(f"/agents/{AGENT}/token", headers=admin_auth_headers())
        assert client.delete(f"/agents/{AGENT}/token", headers=admin_auth_headers()).status_code == 200
        assert len(rows(action="agent.credential.revoked")) == 1

    def test_token_mint_fails_closed_when_the_log_is_unwritable(
        self, raw_client, monkeypatch
    ):
        break_store(monkeypatch)
        response = raw_client.post(
            f"/agents/{AGENT}/token", headers=admin_auth_headers()
        )
        assert response.status_code == 500
        # The acceptance criterion that matters: no credential was created.
        assert agent_auth.CREDENTIALS.active(AGENT) == []

    def test_principal_create_and_revoke_are_audited(self, client):
        created = client.post(
            "/auth/principals",
            headers=admin_auth_headers(),
            json={"name": "alice", "role": "approver", "password": "s3cret-pass"},
        )
        assert created.status_code == 201
        principal_id = created.json()["principal_id"]
        assert client.delete(
            f"/auth/principals/{principal_id}", headers=admin_auth_headers()
        ).status_code == 200

        created_rows = rows(action="admin.principal.created")
        revoked_rows = rows(action="admin.principal.revoked")
        assert len(created_rows) == 1 and len(revoked_rows) == 1
        assert created_rows[0]["actor"] == "alice"
        assert created_rows[0]["role"] == "approver"
        assert created_rows[0]["target_id"] == principal_id
        assert revoked_rows[0]["target_id"] == principal_id
        assert "s3cret-pass" not in json.dumps(created_rows + revoked_rows, default=str)

    def test_login_success_and_failure_are_audited(self, client):
        assert client.post(
            "/auth/session", json={"name": "admin", "password": PASSWORD}
        ).status_code == 201
        assert client.post(
            "/auth/session", json={"name": "admin", "password": "wrong"}
        ).status_code == 401
        ok = rows(action="admin.login.ok")
        denied = rows(action="admin.login.denied")
        assert len(ok) == 1 and ok[0]["result"] == "ok"
        assert len(denied) == 1 and denied[0]["result"] == "denied"
        # With only the shared password configured there is no principal row, so a
        # wrong secret is "unknown principal" — the reason is what the runbook keys
        # off, and the response body still says nothing either way.
        assert json.loads(denied[0]["detail_json"])["reason"] in (
            "bad_secret",
            "unknown_principal",
        )

    def test_logout_and_session_revoke_are_audited(self, client):
        token = client.post(
            "/auth/session", json={"name": "admin", "password": PASSWORD}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        session_id = client.get("/auth/sessions", headers=headers).json()["sessions"][0]["session_id"]
        assert client.delete(f"/auth/sessions/{session_id}", headers=headers).status_code == 200
        assert len(rows(action="admin.session.revoked")) == 1

        second = client.post(
            "/auth/session", json={"name": "admin", "password": PASSWORD}
        ).json()["token"]
        assert client.delete(
            "/auth/session", headers={"Authorization": f"Bearer {second}"}
        ).status_code == 200
        logout = rows(action="admin.logout")
        assert len(logout) == 1 and logout[0]["result"] == "ok"

    def test_agent_auth_failure_is_audited_without_raising(self):
        agent_auth.record_failure(
            "unknown", claimed=AGENT, transport="http", client="127.0.0.1"
        )
        entry = rows(action="agent_auth.denied")[0]
        # An unauthenticated caller has no identity to name: `anonymous`, with the
        # uid they *claimed* kept as unverified detail. A `uid_mismatch` is
        # different — there the credential did authenticate, so actor = agent:<id>.
        assert entry["actor"] == "anonymous"
        assert entry["actor_kind"] == "agent"
        assert entry["result"] == "denied"
        assert json.loads(entry["detail_json"])["claimed"] == AGENT


# ------------------------------------------------------------------------ API


class TestApi:
    def test_requires_admin(self, client):
        assert client.get("/audit").status_code == 401
        auth.create_principal("bob", "operator", "bob-password")
        token = client.post(
            "/auth/session", json={"name": "bob", "password": "bob-password"}
        ).json()["token"]
        assert client.get(
            "/audit", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 403

    def test_lists_newest_first_with_filters_and_pagination(self, client):
        for index in range(3):
            audit_event("config.propose", principal="alice", index=index)
        audit_event("admin.login.ok", principal="alice")

        body = client.get("/audit", headers=admin_auth_headers()).json()
        assert body["total"] == 4 and body["count"] == 4
        assert body["entries"][0]["action"] == "admin.login.ok"

        proposed = client.get(
            "/audit", headers=admin_auth_headers(), params={"action": "config."}
        ).json()
        assert proposed["total"] == 3

        page = client.get(
            "/audit", headers=admin_auth_headers(), params={"limit": 2, "offset": 2}
        ).json()
        assert page["count"] == 2 and page["total"] == 4
        assert page["limit"] == 2 and page["offset"] == 2
        assert page["filters"]["action"] is None

        by_actor = client.get(
            "/audit", headers=admin_auth_headers(), params={"actor": "nobody"}
        ).json()
        assert by_actor["total"] == 0

    def test_bad_timestamp_is_422_not_silently_ignored(self, client):
        response = client.get(
            "/audit", headers=admin_auth_headers(), params={"since": "yesterday"}
        )
        assert response.status_code == 422
        assert "invalid timestamp" in response.json()["detail"]

    def test_verify_reports_a_clean_chain(self, client):
        audit_event("config.propose", principal="alice")
        body = client.get("/audit/verify", headers=admin_auth_headers()).json()
        assert body["ok"] is True
        assert body["entries"] == 1
        assert body["breaker"]["tripped"] is False

    def test_export_jsonl(self, client):
        audit_event("config.propose", principal="alice")
        audit_event("config.approve", principal="bob")
        response = client.get(
            "/audit/export", headers=admin_auth_headers(), params={"format": "jsonl"}
        )
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        lines = [json.loads(line) for line in response.text.strip().splitlines()]
        assert [line["action"] for line in lines] == ["config.propose", "config.approve"]

    def test_export_csv(self, client):
        audit_event("config.propose", principal="alice")
        response = client.get(
            "/audit/export", headers=admin_auth_headers(), params={"format": "csv"}
        )
        assert response.status_code == 200
        parsed = list(csv.DictReader(io.StringIO(response.text)))
        assert parsed[0]["action"] == "config.propose"
        assert parsed[0]["actor"] == "alice"

    def test_export_rejects_unknown_format(self, client):
        assert client.get(
            "/audit/export", headers=admin_auth_headers(), params={"format": "xml"}
        ).status_code == 422

    def test_disabled_log_is_visible_not_hidden(self, client, monkeypatch):
        monkeypatch.setenv("OPAMP_AUDIT_ENABLED", "false")
        body = client.get("/audit", headers=admin_auth_headers()).json()
        assert body["enabled"] is False
        assert body["entries"] == []


# ------------------------------------------------------------------- posture


class TestPosture:
    def test_audit_is_a_config_push_requirement_now(self, monkeypatch):
        from server import posture

        assert posture.requirements()["audit"]["met"] is True
        monkeypatch.setenv("OPAMP_AUDIT_ENABLED", "false")
        assert posture.requirements()["audit"]["met"] is False
        assert any(
            "audit log is not available" in item for item in posture.unmet_requirements()
        )


def test_store_is_shareable_between_instances(tmp_path):
    """Two store objects over one file keep one chain (CLI + server, or workers)."""
    path = tmp_path / "shared.db"
    first, second = SQLiteAuditStore(path), SQLiteAuditStore(path)
    entry = {
        "ts": utcnow().isoformat(),
        "actor": "alice",
        "actor_kind": "human",
        "role": "admin",
        "action": "test.event",
        "result": "ok",
    }
    first.append(entry)
    second.append(entry)
    assert second.verify()["ok"] is True


# ------------------------------------------------- every write route decided (#97)


# Each mutating route is either audited (the action name it must emit) or listed
# here with the reason it is not. Adding a write route without deciding fails
# `test_every_write_route_is_audited_or_explained` — that is the whole point of the
# inventory: #97's AC is "a new endpoint cannot forget".
AUDITED_ROUTES = {
    ("POST", "/auth/session"): "admin.login.ok",  # + admin.login.denied on failure
    ("DELETE", "/auth/session"): "admin.logout",
    ("DELETE", "/auth/sessions/{session_id}"): "admin.session.revoked",
    ("POST", "/auth/principals"): "admin.principal.created",
    ("DELETE", "/auth/principals/{principal_id}"): "admin.principal.revoked",
    ("POST", "/agents/{agent_id}/token"): "agent.credential.issued",
    ("DELETE", "/agents/{agent_id}/token"): "agent.credential.revoked",
    ("POST", "/compliance/check/{agent_id}"): "compliance.checked",
    ("POST", "/compliance/reload"): "compliance.policies_reloaded",
    ("PUT", "/alerts"): "alerts.updated",
    ("POST", "/alerts/test"): "alerts.test",
    # #81: config push is the most privileged mutation in the server, so each of
    # these records before it acts (pre-write, then act — #97).
    ("PUT", "/agent/{agent_id}/config"): "config.push",
    ("DELETE", "/agent/{agent_id}/config"): "config.withdraw",
    ("POST", "/config/apply"): "config.apply",
    # #99: the control plane. Propose records the intent (and its verdict), approve
    # records the human decision before anything is delivered, reject/reactivate
    # keep the record complete, and rollback names the parent it returns to.
    ("POST", "/config/revisions"): "config.propose",
    ("POST", "/config/revisions/{proposal_id}/approve"): "config.approve",
    ("POST", "/config/revisions/{proposal_id}/reject"): "config.reject",
    ("POST", "/config/revisions/{proposal_id}/reactivate"): "config.reactivate",
    ("POST", "/agent/{agent_id}/config/rollback"): "config.rollback",
    # #102: "deliver now" is re-delivery of an already-approved revision, so it is an
    # operator action rather than an approval — but it is still a privileged action
    # against a live machine, and a refusal on principal grounds adds a second row
    # with `result="error"` (pre-write, then act — #97).
    ("POST", "/agent/{agent_id}/config/deliver"): "config.deliver",
    # #103: the staged rollout surface. Creating a rollout *is* the approval, so it
    # records the decision (and its batch plan) before anything is delivered; abort and
    # rollback are recorded by the engine/route respectively, each exactly once.
    ("POST", "/config/rollouts"): "config.rollout.create",
    ("POST", "/config/revisions/{proposal_id}/apply"): "config.rollout.create",
    ("POST", "/config/rollouts/{rollout_id}/abort"): "config.rollout.abort",
    ("POST", "/config/rollouts/{rollout_id}/rollback"): "config.rollout.rollback",
    # #84: a restart is a privileged action against a live machine; the queue write
    # happens after the row, and a refusal records `result="error"`.
    ("POST", "/agent/{agent_id}/restart"): "agent.restart",
    # #111: groups change the blast radius of every future push that names them, so
    # every mutation is recorded — `group.update` carries the definition diff, because
    # "changed" is not something an auditor can act on.
    ("POST", "/groups"): "group.create",
    ("PUT", "/groups/{group_id}"): "group.update",
    ("DELETE", "/groups/{group_id}"): "group.archive",
    ("POST", "/groups/{group_id}/restore"): "group.restore",
    # #46: both are admin actions. The check spends an outbound request; the
    # setting changes whether the server will ever spend one again.
    ("POST", "/updates/check"): "updates.check",
    ("PUT", "/updates/settings"): "updates.settings",
}

EXPLAINED_ROUTES = {
    # Agent protocol: every message already writes agent state, and audit rows per
    # heartbeat would be the #109 write amplification all over again. Auth failures
    # on this path are audited (`agent_auth.denied`); status changes are not.
    ("POST", "/v1/opamp"): "agent protocol: state transitions are the agent row itself",
    # Ingest: same argument, and it is unauthenticated telemetry, not a privileged
    # decision. Rate/volume is the opposite of what an audit log is for.
    ("POST", "/v1/metrics"): "telemetry ingest, not a privileged decision",
    # Generates YAML for download; touches no server or agent state.
    ("POST", "/agent/{agent_id}/manifest"): "pure function, no state change",
    # #81: a validator. It stores nothing and changes nothing — an AI agent asking
    # "would this config be accepted?" must not fill the audit log.
    ("POST", "/config/validate"): "pure validation, no state change",
    # #103: re-running the pipeline reads a revision and parses it; it stores and moves
    # nothing, so it is the same class of question as /config/validate.
    ("POST", "/config/revisions/{proposal_id}/validate"): "pure validation, no state change",
    # #111: a preview answers "who would this hit?" and stores no decision. It does
    # record a *resolution* (the baseline the next diff compares against), which is
    # membership history, not an action against a machine or a config.
    ("POST", "/groups/{group_id}/preview"): "read-only preview; records a resolution only",
}


def mutating_routes():
    from server.main import app as fastapi_app

    found = set()
    for route in fastapi_app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods & {"POST", "PUT", "DELETE", "PATCH"}:
            found.add((method, getattr(route, "path", "")))
    return found


def test_every_write_route_is_audited_or_explained():
    decided = set(AUDITED_ROUTES) | set(EXPLAINED_ROUTES)
    undeclared = mutating_routes() - decided
    assert not undeclared, (
        "new mutating route(s) without an audit decision: "
        f"{sorted(undeclared)} — add them to AUDITED_ROUTES (and emit the entry) or "
        "to EXPLAINED_ROUTES with the reason"
    )
    stale = decided - mutating_routes()
    assert not stale, f"audit inventory names routes that no longer exist: {sorted(stale)}"


class TestRemainingWriteRoutes:
    """Behavioural proof for the routes whose audit entry is not covered above."""

    def test_alerts_update_and_test(self, client):
        response = client.put(
            "/alerts",
            headers=admin_auth_headers(),
            json={"enabled": True, "webhook_url": "https://example.invalid/hook"},
        )
        assert response.status_code == 200
        assert client.post(
            "/alerts/test", headers=admin_auth_headers(), json={"event_type": "new_agent"}
        ).status_code == 200

        update = rows(action="alerts.updated")[0]
        assert update["target_id"] == "alerts"
        detail = json.loads(update["detail_json"])
        # Keys, never values: the webhook URL is a credential in practice.
        assert "webhook_url" in detail["fields"]
        assert "example.invalid" not in json.dumps(update, default=str)
        assert rows(action="alerts.test")[0]["target_id"] == "new_agent"

    def _register(self, agent_id):
        from server import main as main_module
        from server.state import AgentState

        main_module.AGENT_REGISTRY.register(
            agent_id, AgentState(instance_uid=bytes.fromhex(agent_id), agent_id=agent_id)
        )
        return main_module

    def test_compliance_check_records_only_a_real_evaluation(self, client, monkeypatch):
        agent_id = "ef" * 16
        main_module = self._register(agent_id)
        try:
            # OPA off: a 503 is a validation failure, so nothing is recorded — the
            # entry must describe something that actually happened.
            monkeypatch.setattr(main_module, "OPA_ENABLED", False)
            response = client.post(
                f"/compliance/check/{agent_id}", headers=admin_auth_headers()
            )
            assert response.status_code == 503
            assert rows(action="compliance.checked") == []

            monkeypatch.setattr(main_module, "OPA_ENABLED", True)
            monkeypatch.setattr(
                main_module, "evaluate_agent_compliance", lambda agent: {"compliant": True}
            )
            assert client.post(
                f"/compliance/check/{agent_id}", headers=admin_auth_headers()
            ).status_code == 200
            assert rows(action="compliance.checked")[0]["target_id"] == agent_id
        finally:
            main_module.AGENT_REGISTRY.remove(agent_id)

    def test_compliance_reload_is_audited(self, client, monkeypatch):
        from server import main as main_module

        monkeypatch.setattr(main_module, "OPA_ENABLED", False)
        response = client.post("/compliance/reload", headers=admin_auth_headers())
        assert response.status_code == 200
        assert response.json()["success"] is False
        # OPA off is a no-op, not a reload: no entry.
        assert rows(action="compliance.policies_reloaded") == []

        monkeypatch.setattr(main_module, "OPA_ENABLED", True)
        from server import opa_client

        monkeypatch.setattr(opa_client, "get_opa_client", lambda: None)
        assert client.post(
            "/compliance/reload", headers=admin_auth_headers()
        ).status_code == 200
        assert len(rows(action="compliance.policies_reloaded")) == 1
