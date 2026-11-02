"""Tests for the opampctl CLI (issue #56).

- Live mode: commands run against the real FastAPI app in-process (TestClient
  transport), verifying CLI shapes mirror the API.
- Offline mode (--db): commands read a SQLite state file directly with no
  server.
"""

import base64
import json

import pytest
from typer.testing import CliRunner

from cli.main import app
from server.main import app as server_app
from server.state import AGENT_REGISTRY, AgentState, SQLiteAgentStore, SQLiteMetricsStore
from tests.asgi_transport import InProcessASGITransport

runner = CliRunner()

AVAILABLE_COMPONENTS = {
    "components": {
        "receivers": {"subComponentMap": {"otlp": {"metadata": [
            {"key": "code.namespace", "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/otlpreceiver v0.98.0"}},
        ]}}},
        "processors": {"subComponentMap": {"batch": {"metadata": [
            {"key": "code.namespace", "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector/processor/batchprocessor v0.98.0"}},
        ]}}},
        "exporters": {"subComponentMap": {"debug": {"metadata": [
            {"key": "code.namespace", "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector/exporter/debugexporter v0.98.0"}},
        ]}}},
    },
}

COLLECTOR_YAML = (
    b"service:\n  pipelines:\n    traces:\n"
    b"      receivers: [otlp]\n      processors: [batch]\n      exporters: [debug]\n"
)


def effective_config() -> str:
    return json.dumps({"configMap": {"configMap": {"": {
        "body": base64.b64encode(COLLECTOR_YAML).decode()}}}})


# Admin credential for live-ops tests: since #95 privileged endpoints are closed
# when nothing is configured, so any test touching one authenticates explicitly.
ADMIN_PW = "cli-pw-123456"


def register_agent(agent_id, with_components=False):
    state = AgentState(instance_uid=f"cli-test-{agent_id}".encode(), agent_id=agent_id)
    if with_components:
        state.available_components = AVAILABLE_COMPONENTS
        state.effective_config = effective_config()
    AGENT_REGISTRY.register(agent_id, state)
    return state


@pytest.fixture
def live(monkeypatch):
    """Route cli.main's client factory to the in-process app (with an admin credential)."""
    import cli.main as cli_main
    from client.opamp_client import OpampClient
    from server import admin_auth

    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")

    transport = InProcessASGITransport(server_app)

    def factory():
        return OpampClient(
            base_url="http://testserver",
            transport=transport,
            password=cli_main._settings.password,
            token=cli_main._settings.token,
        )

    monkeypatch.setattr(cli_main, "_client", factory)
    yield
    for agent_id in list(AGENT_REGISTRY._agents.keys()):
        AGENT_REGISTRY.remove(agent_id)


def run(*args):
    return runner.invoke(app, list(args))


def parse(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ------------------------------------------------------------------ live mode

class TestLiveOps:
    def test_health(self, live):
        data = parse(run("health"))
        assert data["status"] == "healthy"

    def test_auth_status(self, live):
        data = parse(run("auth", "status"))
        assert "password_required" in data

    def test_config_posture(self, live):
        data = parse(run("--password", ADMIN_PW, "config", "posture"))
        assert data["config_push"]["enabled"] is False
        assert data["override"] is False
        # admin auth is met (the fixture configures it); agent auth, TLS and the
        # config encryption key (#98) are not.
        assert "unmet" in data and len(data["unmet"]) == 3

    def test_agents_list_and_get(self, live):
        register_agent("1111111111111111")
        data = parse(run("agents", "list"))
        assert data["count"] == 1
        data = parse(run("agents", "get", "1111111111111111"))
        assert data["id"] == "1111111111111111"
        assert "metrics" in data

    def test_agents_list_filters(self, live):
        register_agent("2222222222222222")
        data = parse(run("agents", "list", "--healthy", "true"))
        assert data["count"] == 0
        data = parse(run("agents", "list", "--healthy", "unknown"))
        assert data["count"] == 1
        data = parse(run("agents", "list", "--attr", "environment=prod"))
        assert data["count"] == 0

    def test_agents_manifest_raw(self, live):
        register_agent("3333333333333333", with_components=True)
        result = run("agents", "manifest", "3333333333333333", "--raw")
        assert result.exit_code == 0, result.output
        assert "dist:" in result.output
        assert "otlpreceiver" in result.output

    def test_agents_manifest_json(self, live):
        register_agent("3333333333333333", with_components=True)
        data = parse(run("agents", "manifest", "3333333333333333"))
        assert data["collector_version"] == "1.0.0"
        assert "manifest_yaml" in data and "ocb_command" in data

    def test_reports_fleet_raw_and_json(self, live):
        register_agent("4444444444444444", with_components=True)
        raw = run("reports", "fleet", "--raw")
        assert raw.exit_code == 0
        assert "# Agent Report" in raw.output
        data = parse(run("reports", "fleet"))
        assert data["agent_count"] == 1

    def test_reports_heavy_and_outdated(self, live):
        data = parse(run("reports", "heavy", "--threshold", "0.9"))
        assert data["threshold"] == 0.9
        data = parse(run("reports", "outdated", "--version", "0.100.0"))
        assert data["version"] == "0.100.0"

    def test_agents_report_raw(self, live):
        register_agent("5555555555555555", with_components=True)
        result = run("agents", "report", "5555555555555555", "--raw")
        assert result.exit_code == 0, result.output
        assert "# Agent Report" in result.output

    def test_compliance_summary(self, live):
        data = parse(run("compliance", "summary"))
        assert "total" in data and "not_evaluated" in data

    def test_alerts_get_and_test(self, live):
        # `GET /alerts` needs viewer, `POST /alerts/test` needs operator (#95).
        data = parse(run("--password", ADMIN_PW, "alerts", "get"))
        assert "config" in data
        data = parse(run("--password", ADMIN_PW, "alerts", "test", "--event-type", "new_agent"))
        assert "success" in data

    def test_alerts_set_from_file(self, live, tmp_path):
        current = parse(run("--password", ADMIN_PW, "alerts", "get"))["config"]
        cfg = tmp_path / "alerts.json"
        cfg.write_text(json.dumps(current))
        data = parse(run("--password", ADMIN_PW, "alerts", "set", f"@{cfg}"))
        assert "config" in data

    def test_pretty_flag(self, live):
        result = run("--pretty", "health")
        assert result.exit_code == 0
        assert "\n  " in result.output  # indented

    def test_server_error_structured_to_stderr(self, live):
        result = run("agents", "get", "missing-agent")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"].startswith("HTTP 404")
        assert payload["response"]["detail"] == "Agent not found"


class TestLiveAdmin:
    def test_admin_401_without_password(self, live, monkeypatch):
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "pw123")
        result = run("alerts", "get")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"].startswith("HTTP 401")

    def test_admin_ok_with_password(self, live, monkeypatch):
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "pw123")
        result = run("--password", "pw123", "alerts", "get")
        assert result.exit_code == 0, result.output

    def test_whoami_and_auth_status(self, live):
        data = parse(run("--password", ADMIN_PW, "auth", "whoami"))
        assert data["role"] == "admin" and data["method"] in ("legacy", "password")
        status = parse(run("auth", "status"))  # unauthenticated: no credential needed
        assert status["auth_mode"] in ("password", "principals")

    def test_login_token_roundtrip(self, live):
        """`auth login --raw` -> `--token` is the documented session flow."""
        login = run("--password", ADMIN_PW, "auth", "login", "--raw")
        assert login.exit_code == 0, login.output
        token = login.output.strip()
        assert token.startswith("opamp_admin_")
        me = parse(run("--token", token, "auth", "whoami"))
        assert me["method"] == "session" and me["authenticated"] is True
        out = parse(run("--token", token, "auth", "logout"))
        assert out["logged_out"] is True

    def test_principal_create_list_revoke(self, live):
        created = parse(
            run(
                "--password", ADMIN_PW, "auth", "principals", "create", "ops-bot",
                "--role", "operator", "--password", "ops-bot-pw-123",
            )
        )
        assert created["role"] == "operator"
        listed = parse(run("--password", ADMIN_PW, "auth", "principals", "list"))
        assert any(p["name"] == "ops-bot" for p in listed["principals"])
        revoked = parse(
            run("--password", ADMIN_PW, "auth", "principals", "revoke", created["principal_id"])
        )
        assert revoked["revoked"] is True

    def test_principal_create_bad_role_is_structured_error(self, live):
        result = run(
            "--password", ADMIN_PW, "auth", "principals", "create", "x",
            "--role", "root", "--password", "pw-123456789",
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "HTTP 422"

    def test_sessions_list(self, live):
        run("--password", ADMIN_PW, "auth", "login", "--raw")
        data = parse(run("--password", ADMIN_PW, "auth", "sessions", "list"))
        assert any(s["principal"] for s in data["sessions"])


# ---------------------------------------------------------------- offline mode

@pytest.fixture
def offline_db(tmp_path):
    """A SQLite state file with two agents and a metrics snapshot."""
    db = tmp_path / "opamp.db"
    store = SQLiteAgentStore(db)
    store.upsert(AgentState(
        instance_uid=b"aaaa1111aaaa1111",
        agent_id="aaaa1111aaaa1111",
        available_components=AVAILABLE_COMPONENTS,
        effective_config=effective_config(),
        remote_config_status="APPLIED",
    ))
    store.upsert(AgentState(
        instance_uid=b"bbbb2222bbbb2222",
        agent_id="bbbb2222bbbb2222",
        description={"identifyingAttributes": {}, "nonIdentifyingAttributes": {}},
    ))
    SQLiteMetricsStore(db).upsert(
        "aaaa1111aaaa1111",
        {"metrics": {"otelcol_receiver_accepted_metric_points": 42},
         "updated_at": "2026-01-01T00:00:00Z"},
    )
    return db


class TestOffline:
    def test_agents_list(self, offline_db):
        data = parse(run("--db", str(offline_db), "agents", "list"))
        assert data["count"] == 2

    def test_agents_list_filter(self, offline_db):
        data = parse(run("--db", str(offline_db), "agents", "list", "--status", "APPLIED"))
        assert data["count"] == 1 and data["agents"][0]["remote_config_status"] == "APPLIED"

    def test_agents_get_with_metrics(self, offline_db):
        data = parse(run("--db", str(offline_db), "agents", "get", "aaaa1111aaaa1111"))
        # Same shape as the API: data['metrics'] is the persisted entry.
        assert data["metrics"]["metrics"]["otelcol_receiver_accepted_metric_points"] == 42

    def test_agents_get_missing(self, offline_db):
        result = run("--db", str(offline_db), "agents", "get", "nope")
        assert result.exit_code == 1
        assert "not found" in json.loads(result.output)["error"]

    def test_agents_metrics(self, offline_db):
        data = parse(run("--db", str(offline_db), "agents", "metrics", "aaaa1111aaaa1111"))
        assert data["metrics"]["otelcol_receiver_accepted_metric_points"] == 42
        assert "updated_at" in data

    def test_agents_manifest_raw(self, offline_db):
        result = run("--db", str(offline_db), "agents", "manifest", "aaaa1111aaaa1111", "--raw")
        assert result.exit_code == 0, result.output
        assert "dist:" in result.output and "otlpreceiver" in result.output

    def test_agents_manifest_no_components(self, offline_db):
        result = run("--db", str(offline_db), "agents", "manifest", "bbbb2222bbbb2222")
        assert result.exit_code == 1
        assert "no components in use" in json.loads(result.output)["error"]

    def test_agents_manifest_bad_version(self, offline_db):
        result = run("--db", str(offline_db), "agents", "manifest", "aaaa1111aaaa1111", "--version", "bad version")
        assert result.exit_code == 1
        assert "semver" in json.loads(result.output)["error"]

    def test_reports_fleet(self, offline_db):
        data = parse(run("--db", str(offline_db), "reports", "fleet"))
        assert data["agent_count"] == 2
        result = run("--db", str(offline_db), "reports", "fleet", "--raw")
        assert "# Agent Report" in result.output

    def test_reports_heavy_outdated(self, offline_db):
        data = parse(run("--db", str(offline_db), "reports", "heavy"))
        assert data["threshold"] == 0.5
        data = parse(run("--db", str(offline_db), "reports", "outdated", "--version", "0.100.0"))
        assert data["version"] == "0.100.0"

    def test_agents_report(self, offline_db):
        result = run("--db", str(offline_db), "agents", "report", "aaaa1111aaaa1111", "--raw")
        assert result.exit_code == 0, result.output

    def test_live_command_blocked_offline(self, offline_db):
        result = run("--db", str(offline_db), "health")
        assert result.exit_code == 1
        assert "--db" in json.loads(result.output)["error"]

    def test_compliance_blocked_offline(self, offline_db):
        result = run("--db", str(offline_db), "compliance", "summary")
        assert result.exit_code == 1

    def test_alerts_blocked_offline(self, offline_db):
        result = run("--db", str(offline_db), "alerts", "get")
        assert result.exit_code == 1

    def test_empty_db_fails_cleanly(self, tmp_path):
        db = tmp_path / "empty.db"
        SQLiteAgentStore(db)  # init empty file
        result = run("--db", str(db), "agents", "list")
        assert result.exit_code == 1
        assert "No agents found" in json.loads(result.output)["error"]


# ------------------------------------------------------------ agent credentials

class TestTokenCommands:
    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch, tmp_path):
        from server import admin_auth, agent_auth
        from server.state import SQLiteCredentialStore

        monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "cli-pw-123456")
        monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
        monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))

    def test_create_prints_token_and_snippet(self, live):
        data = parse(run("--password", "cli-pw-123456", "agents", "token", "create",
                         "abcd" * 8, "--label", "host-1"))
        assert data["token"].startswith("opamp_agent_")
        assert data["label"] == "host-1"
        assert "collector_snippet" in data and "Authorization" in data["collector_snippet"]

    def test_create_raw_prints_only_the_token(self, live):
        result = run("--password", "cli-pw-123456", "agents", "token", "create",
                     "abce" * 8, "--raw")
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("opamp_agent_")
        assert "{" not in result.output

    def test_revoke_is_idempotent(self, live):
        run("--password", "cli-pw-123456", "agents", "token", "create", "abcf" * 8)
        assert parse(run("--password", "cli-pw-123456", "agents", "token", "revoke",
                         "abcf" * 8))["revoked"] == 1
        assert parse(run("--password", "cli-pw-123456", "agents", "token", "revoke",
                         "abcf" * 8))["revoked"] == 0

    def test_requires_admin(self, live, monkeypatch):
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "cli-pw-123456")
        result = run("agents", "token", "create", "abcd" * 8)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"].startswith("HTTP 401")


class TestOfflineCredentials:
    def test_agents_get_reports_credential_state_offline(self, offline_db):
        """Operators watching a cutover are often doing it with the server down."""
        from server.agent_auth import hash_token
        from server.state import SQLiteCredentialStore

        SQLiteCredentialStore(offline_db).add(hash_token("opamp_agent_x"), "aaaa1111aaaa1111")
        data = parse(run("--db", str(offline_db), "agents", "get", "aaaa1111aaaa1111"))
        assert data["credential"]["enrolled"] is True
        assert data["credential"]["active"] == 1

        listed = parse(run("--db", str(offline_db), "agents", "list"))
        assert all("credential" in agent for agent in listed["agents"])


# ------------------------------------------------------------------- audit (#97)

class TestAudit:
    def test_list_verify_export(self, live):
        from server.audit import audit_event

        audit_event("config.propose", principal="alice", role="operator")

        data = parse(run("--password", ADMIN_PW, "audit", "list", "--action", "config."))
        assert data["total"] >= 1
        assert data["entries"][0]["action"] == "config.propose"
        assert data["entries"][0]["actor"] == "alice"

        report = parse(run("--password", ADMIN_PW, "audit", "verify"))
        assert report["ok"] is True

    def test_export_writes_a_file(self, live, tmp_path):
        from server.audit import audit_event

        audit_event("config.propose", principal="alice")
        target = tmp_path / "audit.jsonl"
        data = parse(
            run(
                "--password", ADMIN_PW,
                "audit", "export", "--format", "jsonl", "--output", str(target),
            )
        )
        assert data["bytes"] > 0
        assert json.loads(target.read_text().strip().splitlines()[0])["actor"] == "alice"

    def test_export_prints_raw_to_stdout(self, live):
        from server.audit import audit_event

        audit_event("config.propose", principal="alice")
        result = run("--password", ADMIN_PW, "audit", "export", "--format", "csv")
        assert result.exit_code == 0
        assert result.output.splitlines()[0].startswith("id,ts,actor")

    def test_unknown_format_is_refused(self, live):
        result = run("--password", ADMIN_PW, "audit", "export", "--format", "xml")
        assert result.exit_code == 1
        assert "jsonl or csv" in result.output

    def test_verify_exits_nonzero_on_a_broken_chain(self, live):
        import sqlite3

        from server import audit

        audit.audit_event("config.propose", principal="alice")
        with sqlite3.connect(audit.STORE.db_path) as conn:
            conn.execute("UPDATE audit_log SET result='denied'")
        result = run("--password", ADMIN_PW, "audit", "verify")
        assert result.exit_code == 1
        assert '"ok": false' in result.output


# ------------------------------------------------------ offline audit (#97, --db)

class TestOfflineAudit:
    """`--db` reads the log straight from the file — what you need when the
    server itself is the casualty (or when a third party hands you the database)."""

    @pytest.fixture
    def audit_db(self, offline_db):
        from server.state import SQLiteAuditStore

        store = SQLiteAuditStore(offline_db)
        store.append(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "actor": "alice",
                "actor_kind": "human",
                "role": "operator",
                "action": "config.propose",
                "target_kind": "agent",
                "target_id": "aaaa1111aaaa1111",
                "result": "ok",
                "detail_json": None,
            }
        )
        store.append(
            {
                "ts": "2026-01-02T00:00:00+00:00",
                "actor": "bob",
                "actor_kind": "human",
                "role": "approver",
                "action": "config.approve",
                "result": "ok",
                "detail_json": None,
            }
        )
        return offline_db

    def test_list(self, audit_db):
        data = parse(run("--db", str(audit_db), "audit", "list"))
        assert data["total"] == 2
        assert data["entries"][0]["actor"] == "bob"  # newest first
        assert data["source"] == str(audit_db)

    def test_list_filter(self, audit_db):
        data = parse(
            run("--db", str(audit_db), "audit", "list", "--action", "config.approve")
        )
        assert data["total"] == 1 and data["entries"][0]["action"] == "config.approve"

    def test_verify(self, audit_db):
        data = parse(run("--db", str(audit_db), "audit", "verify"))
        assert data["ok"] is True and data["entries"] == 2

    def test_verify_exits_nonzero_when_tampered(self, audit_db):
        import sqlite3

        with sqlite3.connect(audit_db) as conn:
            conn.execute("UPDATE audit_log SET actor='mallory'")
        result = run("--db", str(audit_db), "audit", "verify")
        assert result.exit_code == 1
        assert json.loads(result.output)["ok"] is False

    def test_export(self, audit_db, tmp_path):
        target = tmp_path / "offline.jsonl"
        data = parse(
            run(
                "--db", str(audit_db),
                "audit", "export", "-f", "jsonl", "-o", str(target),
            )
        )
        assert data["bytes"] > 0
        lines = target.read_text().strip().splitlines()
        # Oldest first, so a SIEM ingests it in the order it happened.
        assert json.loads(lines[0])["action"] == "config.propose"
        assert json.loads(lines[1])["action"] == "config.approve"


# ------------------------------------------------------- remote config (#81)

CONFIG_FILE_BODY = "receivers:\n  otlp:\n    protocols:\n      grpc:\n"


@pytest.fixture
def push_env(monkeypatch):
    from server import crypto

    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    crypto.reset_key_cache()
    yield
    crypto.reset_key_cache()


class TestConfigCommands:
    def test_validate_reports_a_hash(self, live, tmp_path):
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)
        data = parse(run("--password", ADMIN_PW, "config", "validate", "--file", str(path)))
        assert data["valid"] is True and len(data["config_hash"]) == 64

    def test_validate_invalid_still_exits_zero(self, live, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text("receivers: [broken")
        data = parse(run("--password", ADMIN_PW, "config", "validate", "--file", str(path)))
        assert data["valid"] is False and data["errors"]

    def test_set_get_withdraw(self, live, push_env, tmp_path):
        register_agent("ab" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)

        pushed = parse(
            run("--password", ADMIN_PW, "agents", "config", "set", "ab" * 16,
                "--file", str(path), "--note", "cli test")
        )
        assert pushed["revision_id"].startswith("rev-")

        got = parse(run("--password", ADMIN_PW, "agents", "config", "get", "ab" * 16))
        assert got["desired"]["revision_id"] == pushed["revision_id"]
        assert got["effective"]["redacted"] is False

        withdrawn = parse(
            run("--password", ADMIN_PW, "agents", "config", "withdraw", "ab" * 16)
        )
        assert withdrawn["withdrawn"] is True

    def test_set_carries_role(self, live, push_env, tmp_path):
        """`--role` reaches the stored revision (v0.20.0 AgentConfigObject.role, #68)."""
        register_agent("ef" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)

        parse(run("--password", ADMIN_PW, "agents", "config", "set", "ef" * 16,
                  "--file", str(path), "--role", "raw"))
        got = parse(run("--password", ADMIN_PW, "agents", "config", "get", "ef" * 16))
        assert got["desired"]["config_map"][""]["role"] == "raw"

        # It changes the canonical hash, so a role is part of what was approved.
        with_role = parse(run("--password", ADMIN_PW, "config", "validate",
                              "--file", str(path), "--role", "raw"))
        without = parse(run("--password", ADMIN_PW, "config", "validate",
                            "--file", str(path)))
        assert with_role["config_hash"] != without["config_hash"]

    def test_set_is_404_when_push_is_disabled(self, live, tmp_path):
        register_agent("cd" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)
        result = run("--password", ADMIN_PW, "agents", "config", "set", "cd" * 16,
                     "--file", str(path))
        assert result.exit_code == 1
        assert "404" in json.loads(result.output)["error"]

    def test_apply_needs_confirm(self, live, push_env, tmp_path):
        register_agent("ef" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)
        refused = run("--password", ADMIN_PW, "config", "apply", "--file", str(path),
                      "--agent", "ef" * 16)
        assert refused.exit_code == 1

        ok = parse(
            run("--password", ADMIN_PW, "config", "apply", "--file", str(path),
                "--agent", "ef" * 16, "--confirm")
        )
        assert ok["succeeded"] == 1

    def test_cli_propose_approve_rollback(self, live, push_env, tmp_path):
        register_agent("11" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)

        proposed = parse(
            run("--password", ADMIN_PW, "config", "propose", "--file", str(path),
                "--agent", "11" * 16, "--note", "cli propose")
        )
        assert proposed["status"] == "proposed"
        proposal_id = proposed["proposal_id"]

        listing = parse(run("--password", ADMIN_PW, "config", "revisions",
                            "--agent", "11" * 16))
        assert listing["count"] == 1 and listing["revisions"][0]["status"] == "proposed"

        shown = parse(run("--password", ADMIN_PW, "config", "show", proposal_id))
        assert shown["proposal_id"] == proposal_id
        assert "config_map" not in json.dumps(shown)

        approved = parse(
            run("--password", ADMIN_PW, "config", "approve", proposal_id, "--note", "ok")
        )
        assert approved["status"] == "active"

        # A second revision, then rollback to the first.
        parse(run("--password", ADMIN_PW, "agents", "config", "set", "11" * 16,
                  "--file", str(path)))
        rolled = parse(
            run("--password", ADMIN_PW, "agents", "config", "rollback", "11" * 16,
                "--note", "undo")
        )
        assert rolled["parent_revision_id"]

        # #102: deliver-now is re-delivery of the active revision. The fixture's
        # agent does not advertise `AcceptsRemoteConfig`, so the honest answer is
        # "unchanged" naming that reason — never a claim that it was delivered.
        delivered = parse(
            run("--password", ADMIN_PW, "agents", "config", "deliver", "11" * 16)
        )
        assert delivered["delivery"]["state"] == "unchanged"
        assert delivered["delivery"]["reason"] == "agent_does_not_accept"

    def test_cli_reject_then_approve_is_409(self, live, push_env, tmp_path):
        register_agent("22" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)
        proposal = parse(
            run("--password", ADMIN_PW, "config", "propose", "--file", str(path),
                "--agent", "22" * 16)
        )
        rejected = parse(
            run("--password", ADMIN_PW, "config", "reject", proposal["proposal_id"],
                "--reason", "wrong tenant")
        )
        assert rejected["status"] == "rejected"
        result = run("--password", ADMIN_PW, "config", "approve", proposal["proposal_id"])
        assert result.exit_code == 1
        assert "409" in json.loads(result.output)["error"]

    def test_cli_propose_needs_a_scope(self, live, push_env, tmp_path):
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)
        result = run("--password", ADMIN_PW, "config", "propose", "--file", str(path))
        assert result.exit_code == 1
        assert "exactly one scope" in json.loads(result.output)["error"]

    def test_offline_revisions_and_show(self, live, push_env, offline_db, tmp_path):
        from server.state import SQLiteDesiredConfigStore

        register_agent("33" * 16)
        path = tmp_path / "collector.yaml"
        path.write_text(CONFIG_FILE_BODY)
        # Store a revision directly in the offline database the CLI reads.
        from server import remote_config

        store = SQLiteDesiredConfigStore(offline_db)
        remote_config.DesiredConfigRegistry(store).push(
            "33" * 16,
            {"": {"body": CONFIG_FILE_BODY, "content_type": "text/yaml"}},
            "alice",
            proposal_id="prop-offline-1",
        )
        listing = parse(run("--db", str(offline_db), "config", "revisions"))
        assert listing["count"] >= 1
        assert "offline" in listing["note"]
        shown = parse(run("--db", str(offline_db), "config", "show", "prop-offline-1"))
        assert shown["proposal_id"] == "prop-offline-1"

    def test_offline_get_is_always_redacted(self, offline_db):
        data = parse(run("--db", str(offline_db), "agents", "config", "get", "aaaa1111aaaa1111"))
        assert data["effective"]["redacted"] is True or data["effective"]["config"] is not None
        assert "never revealed" in data["note"]


class TestRolloutCommands:
    """#101/#103 layer parity: the rollout surface must be usable from `opampctl`."""

    @pytest.fixture(autouse=True)
    def push_env(self, monkeypatch):
        """The full config-push posture: without it every route is a deliberate 404."""
        from server import crypto

        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
        monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
        monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
        crypto.reset_key_cache()
        yield
        crypto.reset_key_cache()

    def _propose(self, live):
        import cli.main as cli_main  # noqa: F401

        with __import__("client.opamp_client", fromlist=["OpampClient"]).OpampClient(
            base_url="http://testserver",
            transport=InProcessASGITransport(server_app),
            password=ADMIN_PW,
        ) as oc:
            return oc.propose_config(
                {"": {"body": "extensions: {}\n", "content_type": "text/yaml"}},
                {"agent_id": "ab" * 16},
            )["proposal_id"]

    def test_rollout_plan_create_show_and_abort(self, live):
        proposal_id = self._propose(live)
        auth = ("--password", ADMIN_PW)

        planned = parse(run(*auth, "config", "rollout", "plan", proposal_id))
        assert planned["dry_run"] is True
        assert planned["batches"][0]["agent_ids"] == ["ab" * 16]

        created = parse(run(*auth, "config", "rollout", "create", proposal_id,
                            "--health-gate", "0"))
        assert created["status"] == "running"
        rollout_id = created["rollout_id"]

        listing = parse(run(*auth, "config", "rollouts"))
        assert listing["count"] == 1
        shown = parse(run(*auth, "config", "rollout", "show", rollout_id))
        assert shown["rollout_id"] == rollout_id

        aborted = parse(run(*auth, "config", "rollout", "abort", rollout_id, "--reason", "stop"))
        assert aborted["status"] == "aborted"

    def test_revalidate_and_restart_commands_exist(self, live):
        proposal_id = self._propose(live)
        body = parse(run("--password", ADMIN_PW, "config", "revalidate", proposal_id))
        assert body["proposal_id"] == proposal_id

        # No such agent: the CLI maps the API's 404 to a structured error.
        result = run("--password", ADMIN_PW, "agents", "config", "restart", "cd" * 16)
        assert result.exit_code == 1
