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
from tests.asgi_transport import InProcessASGITransport


# --- helpers -------------------------------------------------------------------

def make_client(**kwargs) -> OpampClient:
    """Client wired to the in-process FastAPI app (no network)."""
    kwargs.setdefault("transport", InProcessASGITransport(app))
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
def oc(monkeypatch):
    # #95: with no admin credential the server is read-open / write-closed, so the
    # admin-surface tests authenticate. Behaviour in read-only mode has its own
    # tests in tests/test_auth.py.
    monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "client-pw-123456")
    c = make_client(password="client-pw-123456")
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
        # The `oc` fixture configures the legacy shared password (#95 read-only mode
        # would report auth_mode "disabled" with no roles).
        assert data["password_required"] is True
        assert data["auth_mode"] == "password"
        assert data["roles"] == ["viewer", "operator", "approver", "admin"]
        assert data["legacy_password"] is True

    def test_config_posture(self, oc):
        data = oc.config_posture()
        assert data["config_push"]["enabled"] is False
        assert data["allowed"] is False
        assert set(data["requirements"]) == {"admin_auth", "agent_auth", "tls"}
        assert data["requirements"]["admin_auth"]["met"] is True
        # admin auth is met here, so agent auth + TLS are the two remaining gaps.
        assert len(data["unmet"]) == 2

    def test_auth_verify_ok_and_bad(self, monkeypatch):
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "pw123")
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
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "pw123")
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


class TestIdentityAndSessions:
    """The #95 surface, through the shipped client (no HTTP shapes drift)."""

    def test_legacy_login_whoami_logout(self, oc):
        session = oc.login("", oc.password)
        assert session["token"].startswith("opamp_admin_")
        assert session["principal"]["role"] == "admin"

        scoped = make_client(token=session["token"])
        try:
            me = scoped.whoami()
            assert me["name"] == "admin" and me["method"] == "session"
            assert scoped.logout()["logged_out"] is True
            with pytest.raises(OpampApiError) as exc:
                scoped.whoami()
            assert exc.value.status_code == 401
        finally:
            scoped.close()

    def test_bearer_token_beats_the_basic_password(self, oc):
        session = oc.login("", oc.password)
        scoped = make_client(password="wrong-on-purpose", token=session["token"])
        try:
            assert scoped.whoami()["authenticated"] is True
        finally:
            scoped.close()

    def test_whoami_in_read_only_mode_is_anonymous(self, monkeypatch):
        monkeypatch.setattr("server.admin_auth.ADMIN_PASSWORD", "")
        anonymous = make_client()
        try:
            me = anonymous.whoami()
            assert me["method"] == "anonymous" and me["authenticated"] is False
        finally:
            anonymous.close()

    def test_principal_lifecycle(self, oc, monkeypatch):
        created = oc.create_principal("dana", "operator", "dana-pw-123456")
        assert created["role"] == "operator" and created["active"] is True
        assert "secret_hash" not in json.dumps(created)

        listed = oc.list_principals()
        assert [p["name"] for p in listed["principals"]] == ["dana"]
        assert listed["roles"] == ["viewer", "operator", "approver", "admin"]

        # The new principal can authenticate with Basic...
        scoped = make_client(password="dana-pw-123456")
        scoped._client.headers["Authorization"] = "Basic " + base64.b64encode(
            b"dana:dana-pw-123456"
        ).decode()
        try:
            assert scoped.whoami()["role"] == "operator"
            # ...but cannot mint identities.
            with pytest.raises(OpampApiError) as exc:
                scoped.create_principal("eve", "admin", "eve-pw-1234567")
            assert exc.value.status_code == 403
        finally:
            scoped.close()

        revoked = oc.revoke_principal(created["principal_id"])
        assert revoked["revoked"] is True

    def test_session_listing_and_revocation(self, oc):
        session = oc.login("", oc.password)
        sessions = oc.list_sessions()["sessions"]
        assert sessions, "the login above must be listed"
        target = sessions[0]
        assert "session_hash" not in json.dumps(sessions)
        revoked = oc.revoke_session(target["session_id"])
        assert revoked["revoked"] is True
        scoped = make_client(token=session["token"])
        try:
            with pytest.raises(OpampApiError) as exc:
                scoped.whoami()
            assert exc.value.status_code == 401
        finally:
            scoped.close()

    def test_bad_role_and_short_password_are_structured_errors(self, oc):
        with pytest.raises(OpampApiError) as bad_role:
            oc.create_principal("x", "root", "pw-123456789")
        assert bad_role.value.status_code == 422
        with pytest.raises(OpampApiError) as short:
            oc.create_principal("y", "viewer", "short")
        assert short.value.status_code == 422


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


# --- agent credentials (#94) ----------------------------------------------------

@pytest.fixture
def credentials(monkeypatch, tmp_path):
    """Isolated credential DB + an admin credential, so minting is allowed."""
    from server import admin_auth, agent_auth
    from server.state import SQLiteCredentialStore

    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "client-pw-123456")
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))


class TestAgentCredentials:
    def test_create_and_revoke(self, credentials):
        agent_id = "ab" * 16
        with make_client(password="client-pw-123456") as c:
            created = c.create_agent_token(agent_id, label="host-1")
            assert created["agent_id"] == agent_id
            assert created["token"].startswith("opamp_agent_")
            assert created["label"] == "host-1"
            assert "Authorization" in created["collector_snippet"]

            assert c.revoke_agent_token(agent_id) == {"agent_id": agent_id, "revoked": 1}
            assert c.revoke_agent_token(agent_id)["revoked"] == 0  # idempotent

    def test_requires_admin(self, credentials):
        with make_client() as c:  # no password
            with pytest.raises(OpampApiError) as exc:
                c.create_agent_token("cd" * 16)
            assert exc.value.status_code == 401

    def test_bad_uid_is_422(self, credentials):
        with make_client(password="client-pw-123456") as c:
            with pytest.raises(OpampApiError) as exc:
                c.create_agent_token("not-a-uid")
            assert exc.value.status_code == 422
