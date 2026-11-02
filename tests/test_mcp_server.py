"""Tests for the MCP server (issue #58).

Tool functions are invoked directly (FastMCP's @tool decorator keeps the
underlying function callable), routed in-process to the real FastAPI app via
TestClient transport — so tool shapes can never drift from the API/CLI/client.
"""

import base64
import json

import pytest

import mcp_server.server as mcp_srv
from mcp_server.server import mcp
from server.main import app as server_app
from server.state import AGENT_REGISTRY, AgentState
from tests.asgi_transport import InProcessASGITransport

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
    """Route the MCP server's client factory to the in-process FastAPI app.

    Also configures an admin credential: since #95 privileged endpoints are closed
    in read-only mode, and these tests exercise the tool shapes, not the auth mode.
    """
    from client.opamp_client import OpampClient
    from server import admin_auth

    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "mcp-pw-123456")
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    transport = InProcessASGITransport(server_app)

    def factory():
        return OpampClient(
            base_url="http://testserver", transport=transport, password="mcp-pw-123456"
        )

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
            "health", "auth_status", "config_posture",
            "list_agents", "get_agent", "get_agent_metrics",
            "generate_manifest",
            "agent_report", "fleet_report", "heavy_collectors_report",
            "outdated_collectors_report",
            "get_compliance", "check_compliance", "compliance_summary",
            "list_policies", "validate_policies", "reload_policies",
            "get_alerts", "update_alerts", "test_alerts",
            "audit_log", "audit_verify", "audit_export",
            # #103: rollouts are readable from MCP, never creatable/abortable here.
            "list_config_rollouts", "get_config_rollout",
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

    def test_config_posture(self, wired):
        data = call(mcp_srv.config_posture)
        assert data["config_push"]["enabled"] is False
        assert "requirements" in data and "unmet" in data


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
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "pw123")
        data = call(mcp_srv.get_alerts)
        assert data["error"] is True and data["status_code"] == 401

    def test_admin_tool_ok_with_password(self, wired, monkeypatch):
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "pw123")
        from client.opamp_client import OpampClient

        transport = InProcessASGITransport(server_app)
        monkeypatch.setattr(
            mcp_srv, "_client",
            lambda: OpampClient(base_url="http://testserver",
                                transport=transport, password="pw123"),
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


# ------------------------------------------------------- identity tools (#95)

class TestAuthTools:
    def test_auth_status_reports_the_modes(self, wired):
        data = call(mcp_srv.auth_status)
        assert data["auth_mode"] in ("password", "principals", "disabled")
        assert data["roles"] == ["viewer", "operator", "approver", "admin"]

    def test_whoami_names_the_role(self, wired):
        data = call(mcp_srv.whoami)
        assert data["authenticated"] is True
        assert data["role"] == "admin"
        assert data["method"] == "legacy"

    def test_identity_admin_is_not_exposed_to_mcp(self):
        """Logins, principals and sessions must never be AI-callable tools."""
        tools = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert not tools & {
            "login",
            "logout",
            "create_principal",
            "revoke_principal",
            "list_principals",
            "list_sessions",
            "revoke_session",
        }
        assert {"auth_status", "whoami"} <= tools


# ------------------------------------------------ credential tools (#94, #106)

class TestCredentialTools:
    """Write tools are absent unless OPAMP_MCP_ALLOW_WRITES=true."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch, tmp_path):
        from server import admin_auth, agent_auth
        from server.state import SQLiteCredentialStore

        monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "mcp-pw-123456")
        monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
        monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))

    def _wire(self, monkeypatch):
        from client.opamp_client import OpampClient

        transport = InProcessASGITransport(server_app)
        monkeypatch.setattr(
            mcp_srv, "_client",
            lambda: OpampClient(base_url="http://testserver", transport=transport,
                                password="mcp-pw-123456"),
        )

    def test_absent_by_default(self):
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        assert "create_agent_token" not in tools
        assert "revoke_agent_token" not in tools

    def test_register_when_writes_allowed(self, monkeypatch):
        monkeypatch.setenv("OPAMP_MCP_ALLOW_WRITES", "true")
        mcp_srv._register_write_tools()
        try:
            tools = {t.name for t in mcp._tool_manager.list_tools()}
            assert {"create_agent_token", "revoke_agent_token"} <= tools

            self._wire(monkeypatch)
            created = call(mcp_srv.create_agent_token, "abcd" * 8, label="mcp")
            assert created["token"].startswith("opamp_agent_")
            assert call(mcp_srv.revoke_agent_token, "abcd" * 8)["revoked"] == 1
        finally:
            for name in ("create_agent_token", "revoke_agent_token", "propose_config"):
                try:
                    mcp._tool_manager.remove_tool(name)
                except Exception:
                    pass

    def test_disabled_flag_keeps_tools_absent(self, monkeypatch):
        monkeypatch.setenv("OPAMP_MCP_ALLOW_WRITES", "false")
        mcp_srv._register_write_tools()
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        assert "create_agent_token" not in tools


# ---------------------------------------------------------------- audit (#97)

class TestAuditTools:
    def test_log_and_verify(self, wired):
        from server.audit import audit_event

        audit_event("config.propose", principal="alice", role="operator")

        data = call(mcp_srv.audit_log, action="config.")
        assert data["total"] >= 1
        assert data["entries"][0]["action"] == "config.propose"

        assert call(mcp_srv.audit_verify)["ok"] is True

    def test_export_returns_parseable_content(self, wired):
        from server.audit import audit_event

        audit_event("config.propose", principal="alice")
        data = call(mcp_srv.audit_export, format="jsonl")
        assert data["format"] == "jsonl"
        assert json.loads(data["content"].strip().splitlines()[0])["actor"] == "alice"

    def test_tools_are_read_only_not_write_gated(self):
        # #97/#106: reading the audit log is never a write tool, so it is always
        # present — an AI agent must be able to answer "who did this".
        assert not mcp_srv.writes_enabled()
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        assert {"audit_log", "audit_verify", "audit_export"} <= tools


# ---------------------------------------------------- remote config (#81, D9)

CONFIG_MAP = {"": {"body": "receivers:\n  otlp:\n", "content_type": "text/yaml"}}


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


class TestRemoteConfigTools:
    def test_validate_is_available_and_side_effect_free(self, wired):
        assert call(mcp_srv.validate_config, CONFIG_MAP)["valid"] is True
        assert call(mcp_srv.validate_config, {"": {"body": "a: [b", "content_type": "text/yaml"}})[
            "valid"
        ] is False

    def test_agent_config_is_redacted(self, wired, push_env):
        agent_id = "ab" * 16
        register_agent(agent_id)
        from server import remote_config

        remote_config.REGISTRY.push(
            agent_id,
            {"": {"body": "exporters:\n  x:\n    password: hunter2\n", "content_type": "text/yaml"}},
            "admin",
        )
        data = call(mcp_srv.agent_config, agent_id)
        assert data["desired"]["redacted"] is True
        assert "hunter2" not in json.dumps(data)

    def test_no_write_or_apply_tool_is_ever_exposed(self):
        """D9/#106: MCP may propose and validate, never apply, approve or roll back."""
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        forbidden = {
            "set_agent_config",
            "push_config",
            "apply_config",
            "apply_agent_config",
            "delete_agent_config",
            "withdraw_config",
            "approve_config",
            "reject_config",
            "reactivate_config",
            "rollback_config",
            "rollback_agent_config",
            # #101/#103: creating a rollout IS the approval, and abort/rollback change
            # what the fleet runs — never tools at any setting (D9).
            "create_config_rollout",
            "plan_config_rollout",
            "abort_config_rollout",
            "rollback_config_rollout",
            "restart_agent",
        }
        assert not (names & forbidden)
        assert {"agent_config", "validate_config", "list_config_revisions"} <= names

    def test_propose_config_is_a_write_gated_tool(self, wired, push_env, monkeypatch):
        """#99/D9: an MCP client may propose (a draft), but only with writes enabled."""
        monkeypatch.delenv("OPAMP_MCP_ALLOW_WRITES", raising=False)
        mcp_srv._register_write_tools()
        assert not mcp_srv.writes_enabled()
        assert "propose_config" not in {
            tool.name for tool in mcp._tool_manager.list_tools()
        }

        monkeypatch.setenv("OPAMP_MCP_ALLOW_WRITES", "true")
        try:
            mcp_srv._register_write_tools()
            assert "propose_config" in {tool.name for tool in mcp._tool_manager.list_tools()}
            agent_id = "cd" * 16
            register_agent(agent_id)
            proposed = call(
                mcp_srv.propose_config,
                CONFIG_MAP,
                {"agent_id": agent_id},
            )
            assert proposed["status"] == "proposed"
        finally:
            for name in ("propose_config", "create_agent_token", "revoke_agent_token"):
                try:
                    mcp._tool_manager.remove_tool(name)
                except Exception:
                    pass

    def test_validate_can_target_one_agent(self, wired, push_env):
        agent_id = "ef" * 16
        register_agent(agent_id)
        verdict = call(mcp_srv.validate_config, CONFIG_MAP, agent_id)
        assert verdict["valid"] is True
