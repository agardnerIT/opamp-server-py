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


def register_agent(agent_id, with_components=False):
    state = AgentState(instance_uid=f"cli-test-{agent_id}".encode(), agent_id=agent_id)
    if with_components:
        state.available_components = AVAILABLE_COMPONENTS
        state.effective_config = effective_config()
    AGENT_REGISTRY.register(agent_id, state)
    return state


@pytest.fixture
def live(monkeypatch):
    """Route cli.main's client factory to the in-process app."""
    import cli.main as cli_main
    from client.opamp_client import OpampClient

    transport = InProcessASGITransport(server_app)

    def factory():
        return OpampClient(
            base_url="http://testserver",
            transport=transport,
            password=cli_main._settings.password,
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
        data = parse(run("alerts", "get"))
        assert "config" in data
        data = parse(run("alerts", "test", "--event-type", "new_agent"))
        assert "success" in data

    def test_alerts_set_from_file(self, live, tmp_path):
        current = parse(run("alerts", "get"))["config"]
        cfg = tmp_path / "alerts.json"
        cfg.write_text(json.dumps(current))
        data = parse(run("alerts", "set", f"@{cfg}"))
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
        monkeypatch.setattr("server.main.ADMIN_PASSWORD", "pw123")
        result = run("alerts", "get")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"].startswith("HTTP 401")

    def test_admin_ok_with_password(self, live, monkeypatch):
        monkeypatch.setattr("server.main.ADMIN_PASSWORD", "pw123")
        result = run("--password", "pw123", "alerts", "get")
        assert result.exit_code == 0, result.output


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
