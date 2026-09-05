"""Tests for the opamp_client shared HTTP client library (issue #54).

Two layers:
- Unit: URL normalization, env-var defaults, structured errors (MockTransport).
- Integration: every client method against the real FastAPI app via
  httpx.ASGITransport, so method shapes can never drift from the API.
"""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import client as client_pkg
from client import (
    DEFAULT_SERVER_URL,
    OpampApiError,
    OpampClient,
    OpampClientError,
    OpampConnectionError,
)
from client.opamp_client import _normalize_base_url
from server.main import app
from server.state import AGENT_REGISTRY, AgentState


# --- helpers -------------------------------------------------------------------

def make_client(**kwargs) -> OpampClient:
    """Client wired to the in-process FastAPI app (no network).

    FastAPI's TestClient transport is a sync httpx transport that dispatches
    to the ASGI app in-process, so OpampClient exercises the real endpoints.
    """
    tc = TestClient(app)
    kwargs.setdefault("transport", tc._transport)
    kwargs.setdefault("base_url", "http://testserver")
    return OpampClient(**kwargs)
    kwargs.setdefault("base_url", "http://testserver")
    return OpampClient(**kwargs)


def register_agent(agent_id, components=None, used=True):
    state = AgentState(instance_uid=f"client-test-{agent_id}".encode(), agent_id=agent_id)
    if components is not None:
        state.available_components = components
        state.effective_config = json.dumps(
            {"configMap": {"configMap": {"": {"body": base64.b64encode(
                b"service:\n  pipelines:\n    traces:\n      receivers: [otlp]\n      processors: [batch]\n      exporters: [debug]\n"
            ).decode()}}}}
        )
    AGENT_REGISTRY.register(agent_id, state)
    return agent_id


# Minimal available_components (JSON shape after MessageToDict) with used
# components (otlp/batch/debug) marked via effective_config above.
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


@pytest.fixture
def oc():
    c = make_client()
    yield c
    c.close()
    for agent_id in list(AGENT_REGISTRY._agents.keys()):
        AGENT_REGISTRY.remove(agent_id)


# --- unit: URL + env handling ---------------------------------------------------

class TestUrlHandling:
    def test_default_url(self, monkeypatch):
        monkeypatch.delenv("OPAMP_SERVER_URL", raising=False)
        assert _normalize_base_url("") == DEFAULT_SERVER_URL == "http://localhost:4320"

    def test_env_var_used_when_no_arg(self, monkeypatch):
        monkeypatch.setenv("OPAMP_SERVER_URL", "https://opamp.example.com:9999")
        c = OpampClient()
        assert c.base_url == "https://opamp.example.com:9999"
        c.close()

    def test_arg_beats_env(self, monkeypatch):
        monkeypatch.setenv("OPAMP_SERVER_URL", "https://from-env.example.com")
        c = OpampClient(base_url="http://from-arg.example.com")
        assert c.base_url == "http://from-arg.example.com"
        c.close()

    def test_strips_trailing_slash(self):
        assert _normalize_base_url("http://s.example.com/") == "http://s.example.com"

    def test_adds_scheme_when_missing(self):
        assert _normalize_base_url("opamp.example.com") == "http://opamp.example.com"

    def test_rejects_bad_scheme(self):
        with pytest.raises(OpampClientError):
            _normalize_base_url("ftp://opamp.example.com")


class TestAuth:
    def test_password_from_env_sets_basic_header(self, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "sekrit")
        c = OpampClient(base_url="http://t")
        expected = "Basic " + base64.b64encode(b":sekrit").decode()
        assert c._client.headers["Authorization"] == expected
        c.close()

    def test_no_password_no_header(self, monkeypatch):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        c = OpampClient(base_url="http://t")
        assert "Authorization" not in c._client.headers
        c.close()


class TestErrors:
    def test_api_error_structured(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "Agent not found"})

        c = OpampClient(base_url="http://t", transport=httpx.MockTransport(handler))
        with pytest.raises(OpampApiError) as exc:
            c.get_agent("nope")
        assert exc.value.status_code == 404
        assert exc.value.detail == "Agent not found"
        c.close()

    def test_api_error_non_json_body(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        c = OpampClient(base_url="http://t", transport=httpx.MockTransport(handler))
        with pytest.raises(OpampApiError) as exc:
            c.fleet_report()
        assert exc.value.status_code == 500
        assert exc.value.detail == "boom"
        c.close()

    def test_connection_error_wrapped(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        c = OpampClient(base_url="http://t", transport=httpx.MockTransport(handler))
        with pytest.raises(OpampConnectionError):
            c.health()
        c.close()

    def test_exception_hierarchy(self):
        assert issubclass(OpampApiError, OpampConnectionError.__mro__[1])
        assert issubclass(OpampConnectionError, client_pkg.OpampClientError)


# --- integration: every method against the real app ------------------------------

class TestOpsAndAuth:
    def test_health(self, oc):
        data = oc.health()
        assert data["status"] == "healthy"
        assert "agents_connected" in data and "opa_enabled" in data

    def test_auth_status(self, oc):
        data = oc.auth_status()
        assert "password_required" in data

    def test_auth_verify_ok_and_bad(self, monkeypatch):
        monkeypatch.setattr("server.main.ADMIN_PASSWORD", "pw123")
        c = make_client(password="pw123")
        try:
            assert c.auth_verify("pw123") == {"verified": True}
            assert c.auth_verify("wrong") == {"verified": False}
        finally:
            c.close()


class TestAgents:
    def test_list_agents_empty(self, oc):
        data = oc.list_agents()
        assert data["agents"] == [] and data["count"] == 0

    def test_list_agents_filters(self, oc):
        register_agent("aaaaaaaaaaaaaaaa")
        data = oc.list_agents(healthy="unknown")
        assert data["count"] == 1
        data = oc.list_agents(healthy="true")
        assert data["count"] == 0

    def test_list_agents_attribute_filter(self, oc):
        register_agent("bbbbbbbbbbbbbbbb")
        data = oc.list_agents(environment="prod")
        assert data["count"] == 0  # agent has no such attribute

    def test_get_agent_404(self, oc):
        with pytest.raises(OpampApiError) as exc:
            oc.get_agent("missing")
        assert exc.value.status_code == 404

    def test_get_agent_roundtrip(self, oc):
        register_agent("cccccccccccccccc")
        data = oc.get_agent("cccccccccccccccc")
        assert data["id"] == "cccccccccccccccc"
        assert "metrics" in data

    def test_get_agent_metrics(self, oc):
        register_agent("dddddddddddddddd")
        data = oc.get_agent_metrics("dddddddddddddddd")
        assert isinstance(data, dict)


class TestManifestAndReports:
    def test_generate_manifest(self, oc):
        register_agent("eeeeeeeeeeeeeeee", components=AVAILABLE_COMPONENTS)
        result = oc.generate_manifest("eeeeeeeeeeeeeeee")
        assert "manifest_yaml" in result and "ocb_command" in result
        assert result["collector_version"] == "1.0.0"

    def test_generate_manifest_custom_version(self, oc):
        register_agent("eeeeeeeeeeeeeeee", components=AVAILABLE_COMPONENTS)
        result = oc.generate_manifest("eeeeeeeeeeeeeeee", version="2.3.4")
        assert result["collector_version"] == "2.3.4"

    def test_generate_manifest_bad_version_422(self, oc):
        register_agent("eeeeeeeeeeeeeeee")
        with pytest.raises(OpampApiError) as exc:
            oc.generate_manifest("eeeeeeeeeeeeeeee", version="../etc/passwd")
        assert exc.value.status_code == 422

    def test_agent_report(self, oc):
        register_agent("ffffffffffffffff")
        data = oc.agent_report("ffffffffffffffff")
        assert "report_markdown" in data

    def test_fleet_report(self, oc):
        register_agent("ffffffffffffffff")
        data = oc.fleet_report()
        assert data["agent_count"] == 1
        assert "report_markdown" in data

    def test_heavy_collectors_report(self, oc):
        data = oc.heavy_collectors_report(threshold=0.8)
        assert data["threshold"] == 0.8

    def test_outdated_collectors_report(self, oc):
        data = oc.outdated_collectors_report(version="0.100.0")
        assert data["version"] == "0.100.0"


class TestCompliance:
    def test_summary(self, oc):
        data = oc.compliance_summary()
        assert data["total"] == 0 and "not_evaluated" in data

    def test_get_compliance_unknown_agent(self, oc):
        with pytest.raises(OpampApiError) as exc:
            oc.get_compliance("missing")
        assert exc.value.status_code == 404

    def test_list_policies(self, oc):
        data = oc.list_policies()
        assert "policies" in data and "opa_enabled" in data

    def test_validate_policies(self, oc):
        data = oc.validate_policies()
        assert "policies" in data and "opa_enabled" in data

    def test_reload_policies_admin(self, oc):
        data = oc.reload_policies()
        assert "success" in data


class TestAlerts:
    def test_get_alerts(self, oc):
        data = oc.get_alerts()
        assert "config" in data and "events" in data and "types" in data

    def test_update_alerts_roundtrip(self, oc):
        current = oc.get_alerts()["config"]
        result = oc.update_alerts(current)
        assert "config" in result

    def test_test_alerts(self, oc):
        data = oc.test_alerts(event_type="new_agent")
        assert "success" in data


class TestAdminAuthThroughClient:
    def test_admin_endpoints_require_password_when_set(self, monkeypatch):
        monkeypatch.setattr("server.main.ADMIN_PASSWORD", "pw123")
        # Right password -> 2xx; wrong/missing -> 401 structured error.
        good = make_client(password="pw123")
        bad = make_client(password="nope")
        try:
            good.get_alerts()
            with pytest.raises(OpampApiError) as exc:
                bad.get_alerts()
            assert exc.value.status_code == 401
        finally:
            good.close()
            bad.close()


class TestPackaging:
    def test_init_reexports(self):
        assert client_pkg.OpampClient is OpampClient
        assert client_pkg.OpampApiError is OpampApiError

    def test_json_everywhere(self, oc):
        """Every non-error response parses as JSON (dict), never raw text."""
        for method, args in [
            (oc.health, ()),
            (oc.auth_status, ()),
            (oc.list_agents, ()),
            (oc.compliance_summary, ()),
            (oc.list_policies, ()),
        ]:
            assert isinstance(method(*args), dict)
