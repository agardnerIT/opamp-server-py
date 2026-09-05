"""Tests for the MCP server (issue #58).

Tool functions are invoked directly (FastMCP's @tool decorator keeps the
underlying function callable), routed in-process to the real FastAPI app via
TestClient transport — so tool shapes can never drift from the API/CLI/client.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

import mcp_server.server as mcp_srv
from mcp_server.server import mcp
from server.main import app as server_app
from server.state import AGENT_REGISTRY, AgentState

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


@pytest.fixture
def wired(monkeypatch):
    """Route the MCP server's client factory to the in-process FastAPI app."""
    tc = TestClient(app=server_app)
    from client.opamp_client import OpampClient

    def factory():
        return OpampClient(base_url="http://testserver", transport=tc._transport)

    monkeypatch.setattr(mcp_srv, "_client", factory)
    yield
    for agent_id in list(AGENT_REGISTRY._agents.keys()):
        AGENT_REGISTRY.remove(agent_id)


def register_agent(agent_id, with_components=False):
    state = AgentState(instance_uid=f"mcp-test-{agent_id}".encode(), agent_id=agent_id)
    if with_components:
        state.available_components = AVAILABLE_COMPONENTS
        state.effective_config = effective_config()
    AGENT_REGISTRY.register(agent_id, state)


def call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    return json.loads(result)


# ------------------------------------------------------------------- registry

class TestRegistry:
    def test_all_tools_registered(self):
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        expected = {
            "health", "auth_status",
            "list_agents", "get_agent", "get_agent_metrics",
            "generate_manifest",
            "agent_report", "fleet_report", "heavy_collectors_report",
            "outdated_collectors_report",
            "get_compliance", "check_compliance", "compliance_summary",
            "list_policies", "validate_policies", "reload_policies",
            "get_alerts", "update_alerts", "test_alerts",
        }
        assert expected <= tools

    def test_server_metadata(self):
        assert mcp.name == "opamp-server"
        assert "OpAMP" in mcp.instructions


# -------------------------------------------------------------- tool behavior

class TestOpsTools:
    def test_health(self, wired):
        data = call(mcp_srv.health)
        assert data["status"] == "healthy"

    def test_auth_status(self, wired):
        data = call(mcp_srv.auth_status)
        assert "password_required" in data


class TestAgentTools:
    def test_list_agents(self, wired):
        register_agent("1111111111111111")
        data = call(mcp_srv.list_agents)
        assert data["count"] == 1

    def test_list_agents_filters(self, wired):
        register_agent("2222222222222222")
        data = call(mcp_srv.list_agents, healthy="true")
        assert data["count"] == 0
        data = call(mcp_srv.list_agents, healthy="unknown")
        assert data["count"] == 1
        data = call(mcp_srv.list_agents, attributes={"environment": "prod"})
        assert data["count"] == 0

    def test_get_agent(self, wired):
        register_agent("3333333333333333")
        data = call(mcp_srv.get_agent, "3333333333333333")
        assert data["id"] == "3333333333333333"

    def test_get_agent_not_found_is_structured(self, wired):
        data = call(mcp_srv.get_agent, "missing")
        assert data["error"] is True
        assert data["status_code"] == 404
        assert data["detail"] == "Agent not found"

    def test_get_agent_metrics(self, wired):
        register_agent("4444444444444444")
        data = call(mcp_srv.get_agent_metrics, "4444444444444444")
        # Empty dict when the agent has sent no metrics (same as the API).
        assert data == {}


class TestManifestTools:
    def test_generate_manifest(self, wired):
        register_agent("5555555555555555", with_components=True)
        data = call(mcp_srv.generate_manifest, "5555555555555555")
        assert data["collector_version"] == "1.0.0"
        assert "otlpreceiver" in data["manifest_yaml"]
        assert data["ocb_command"].startswith("ocb build")

    def test_generate_manifest_version(self, wired):
        register_agent("5555555555555555", with_components=True)
        data = call(mcp_srv.generate_manifest, "5555555555555555", version="2.3.4")
        assert data["collector_version"] == "2.3.4"

    def test_generate_manifest_no_components_structured(self, wired):
        register_agent("6666666666666666")
        data = call(mcp_srv.generate_manifest, "6666666666666666")
        assert data["error"] is True and data["status_code"] == 409


class TestReportTools:
    def test_fleet_report(self, wired):
        register_agent("7777777777777777", with_components=True)
        data = call(mcp_srv.fleet_report)
        assert data["agent_count"] == 1
        assert "# Agent Report" in data["report_markdown"]

    def test_agent_report(self, wired):
        register_agent("7777777777777777", with_components=True)
        data = call(mcp_srv.agent_report, "7777777777777777")
        assert "# Agent Report" in data["report_markdown"]

    def test_heavy_collectors_report(self, wired):
        data = call(mcp_srv.heavy_collectors_report, 0.9)
        assert data["threshold"] == 0.9

    def test_outdated_collectors_report(self, wired):
        data = call(mcp_srv.outdated_collectors_report, "0.100.0")
        assert data["version"] == "0.100.0"


class TestComplianceTools:
    def test_summary(self, wired):
        data = call(mcp_srv.compliance_summary)
        assert data["total"] == 0

    def test_policies_and_validate(self, wired):
        assert "policies" in call(mcp_srv.list_policies)
        assert "policies" in call(mcp_srv.validate_policies)

    def test_reload(self, wired):
        assert "success" in call(mcp_srv.reload_policies)

    def test_get_compliance_unknown_agent(self, wired):
        data = call(mcp_srv.get_compliance, "missing")
        assert data["error"] is True and data["status_code"] == 404


class TestAlertTools:
    def test_get_alerts(self, wired):
        data = call(mcp_srv.get_alerts)
        assert "config" in data and "events" in data

    def test_update_alerts_roundtrip(self, wired):
        current = call(mcp_srv.get_alerts)["config"]
        data = call(mcp_srv.update_alerts, current)
        assert "config" in data

    def test_test_alerts(self, wired):
        data = call(mcp_srv.test_alerts, "new_agent")
        assert "success" in data


class TestAdminAuth:
    def test_admin_tool_401_without_password(self, wired, monkeypatch):
        monkeypatch.setattr("server.main.ADMIN_PASSWORD", "pw123")
        data = call(mcp_srv.get_alerts)
        assert data["error"] is True and data["status_code"] == 401

    def test_admin_tool_ok_with_password(self, wired, monkeypatch):
        monkeypatch.setattr("server.main.ADMIN_PASSWORD", "pw123")
        from client.opamp_client import OpampClient

        tc = TestClient(app=server_app)
        monkeypatch.setattr(
            mcp_srv, "_client",
            lambda: OpampClient(base_url="http://testserver",
                                transport=tc._transport, password="pw123"),
        )
        data = call(mcp_srv.get_alerts)
        assert "config" in data


class TestConnectionError:
    def test_unreachable_server_structured(self, monkeypatch):
        from client.opamp_client import OpampClient

        def factory():
            return OpampClient(base_url="http://127.0.0.1:1", timeout=0.5)

        monkeypatch.setattr(mcp_srv, "_client", factory)
        data = call(mcp_srv.health)
        assert data["error"] is True
        assert "connection" in data
