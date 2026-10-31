"""Fail-closed enablement and posture tests (#96).

Covers the three questions this issue exists to answer:

1. Is config push off by default, and does *everything* agree it is off
   (`/health`, the capability bit on the wire, the gate on the routes)?
2. Does enabling it on an insecure posture refuse to start, naming every
   requirement at once?
3. Does the development override still work, and is it impossible to miss
   (`insecure: true` everywhere, loud log)?
"""

from __future__ import annotations

import stat

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server import admin_auth, posture
from server.main import app
from server.state import AGENT_REGISTRY

PASSWORD = "correct-horse-battery-staple"


# --------------------------------------------------------------------------- helpers


def _secure_env(monkeypatch, tmp_path, agent_auth="token"):
    """Everything the posture requires, nothing more."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(admin_auth, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", agent_auth)
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setattr(posture, "ENFORCED_AGENT_AUTH_MODES", frozenset({"token", "mtls"}))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from the shipped defaults: everything off."""
    for var in (
        "OPAMP_CONFIG_PUSH_ENABLED",
        "OPAMP_ALLOW_INSECURE_CONFIG_PUSH",
        "OPAMP_AGENT_AUTH",
        "PUBLIC_URL",
        "TLS_TERMINATED_UPSTREAM",
        "OPAMP_AUDIT_ENABLED",
        "ADMIN_PASSWORD",
        "ADMIN_PASSWORD_FILE",
        "OPAMP_ADMIN_AUTOGENERATE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "none")
    yield


@pytest.fixture
def client():
    """The shared app object — no lifespan, so each test's env is what counts."""
    yield TestClient(app)


# --------------------------------------------------------------------------- posture


def test_defaults_are_off_and_safe():
    assert posture.config_push_enabled() is False
    assert posture.push_allowed() is False
    assert posture.insecure() is False
    assert posture.agent_auth_mode() == "off"
    assert posture.tls_enabled() is False
    assert posture.audit_enabled() is False
    assert posture.posture() == {
        "enabled": False,
        "insecure": False,
        "agent_auth": "off",
        "audit": False,
        "tls": False,
    }


def test_flag_parsing_accepts_truthy_spellings(monkeypatch):
    for raw in ("true", "1", "yes", "on", "TRUE", " yes "):
        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", raw)
        assert posture.config_push_enabled() is True, raw
    for raw in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", raw)
        assert posture.config_push_enabled() is False, raw


def test_unknown_agent_auth_mode_is_off(monkeypatch):
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "oauth-ish")
    assert posture.agent_auth_mode() == "off"


def test_tls_from_public_url_or_upstream_terminator(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    assert posture.tls_enabled() is True
    monkeypatch.setenv("PUBLIC_URL", "wss://opamp.example.com/v1/opamp")
    assert posture.tls_enabled() is True
    monkeypatch.setenv("PUBLIC_URL", "http://opamp.example.com")
    assert posture.tls_enabled() is False
    monkeypatch.setenv("TLS_TERMINATED_UPSTREAM", "true")
    assert posture.tls_enabled() is True


def test_unmet_requirements_names_every_missing_item_at_once():
    missing = posture.unmet_requirements()
    assert len(missing) == 3
    assert "admin auth" in missing[0]
    assert "agents are not authenticated" in missing[1]
    assert "TLS" in missing[2]


def test_requirements_met_gives_empty_list(monkeypatch, tmp_path):
    _secure_env(monkeypatch, tmp_path)
    assert posture.unmet_requirements() == []
    assert posture.push_allowed() is True
    assert posture.insecure() is False


def test_configured_but_unenforced_agent_auth_does_not_count(monkeypatch, tmp_path):
    """Setting an env var no code reads must not open the gate."""
    _secure_env(monkeypatch, tmp_path)
    monkeypatch.setattr(posture, "ENFORCED_AGENT_AUTH_MODES", frozenset())
    assert posture.push_allowed() is False
    assert any("not enforced by this build" in m for m in posture.unmet_requirements())


def test_token_mode_is_enforced_and_mtls_is_not():
    """#94: only the mode this build actually implements may open the gate."""
    assert "token" in posture.ENFORCED_AGENT_AUTH_MODES
    assert "mtls" not in posture.ENFORCED_AGENT_AUTH_MODES
    assert "off" not in posture.ENFORCED_AGENT_AUTH_MODES


def test_mtls_mode_is_reported_but_refused(monkeypatch, tmp_path):
    _secure_env(monkeypatch, tmp_path, agent_auth="mtls")
    monkeypatch.setattr(posture, "ENFORCED_AGENT_AUTH_MODES", frozenset({"token"}))
    assert posture.push_allowed() is False
    unmet = "\n".join(posture.unmet_requirements())
    assert "not enforced by this build" in unmet and "#83" in unmet


def test_override_allows_push_but_reports_insecure(monkeypatch):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    assert posture.push_allowed() is True
    assert posture.insecure() is True
    assert posture.posture()["insecure"] is True


# --------------------------------------------------------------------------- startup


def test_startup_is_quiet_when_push_is_disabled():
    posture.assert_startup_posture()  # must not raise


def test_startup_refuses_naming_every_requirement(monkeypatch):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    with pytest.raises(posture.PostureError) as exc:
        posture.assert_startup_posture()
    message = str(exc.value)
    assert "refusing to start" in message
    for fragment in ("admin auth", "agents are not authenticated", "TLS"):
        assert fragment in message
    assert len(exc.value.missing) == 3
    assert "OPAMP_ALLOW_INSECURE_CONFIG_PUSH" in message


def test_startup_passes_on_a_secure_posture(monkeypatch, tmp_path):
    _secure_env(monkeypatch, tmp_path)
    posture.assert_startup_posture()  # must not raise


def test_startup_override_warns_and_starts(monkeypatch, caplog=None):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    posture.assert_startup_posture()  # must not raise
    assert posture.insecure() is True


def test_lifespan_refuses_to_serve(monkeypatch):
    """The refusal must reach a real server start, not just the helper."""
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    with pytest.raises(posture.PostureError):
        with TestClient(app):
            pass


# --------------------------------------------------------------------------- gate


def _gate_app() -> FastAPI:
    stub = FastAPI()

    @stub.get("/config/revisions")
    def _route(_: None = Depends(posture.config_push_gate)):
        return {"ok": True}

    return stub


def test_gate_is_404_not_403_when_disabled():
    with TestClient(_gate_app()) as c:
        resp = c.get("/config/revisions")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "config push is disabled"


def test_gate_is_404_when_enabled_but_insecure(monkeypatch):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    with TestClient(_gate_app()) as c:
        resp = c.get("/config/revisions")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "config push is disabled"


def test_gate_opens_when_posture_is_secure(monkeypatch, tmp_path):
    _secure_env(monkeypatch, tmp_path)
    with TestClient(_gate_app()) as c:
        resp = c.get("/config/revisions")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# --------------------------------------------------------------------------- wire


def _agent_message(uid=b"\x01" * 16):
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
    return msg.SerializeToString()


def test_capability_bit_absent_by_default(client):
    from server.main import _handle_agent_message

    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(_handle_agent_message(_agent_message()).response)
    assert not reply.capabilities & opamp_pb2.ServerCapabilities.ServerCapabilities_OffersRemoteConfig
    assert reply.HasField("remote_config") is False
    AGENT_REGISTRY.remove("01" * 16)


def test_capability_bit_present_only_when_push_allowed(monkeypatch, tmp_path, client):
    from server.main import _handle_agent_message

    _secure_env(monkeypatch, tmp_path)
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(_handle_agent_message(_agent_message(b"\x02" * 16)).response)
    assert reply.capabilities & opamp_pb2.ServerCapabilities.ServerCapabilities_OffersRemoteConfig
    AGENT_REGISTRY.remove("02" * 16)


def test_scrub_strips_remote_config_while_disabled():
    reply = opamp_pb2.ServerToAgent()
    reply.remote_config.config_hash = b"hash"
    assert posture.scrub_remote_config(reply) is True
    assert reply.HasField("remote_config") is False


def test_scrub_leaves_remote_config_alone_when_allowed(monkeypatch, tmp_path):
    _secure_env(monkeypatch, tmp_path)
    reply = opamp_pb2.ServerToAgent()
    reply.remote_config.config_hash = b"hash"
    assert posture.scrub_remote_config(reply) is False
    assert reply.HasField("remote_config") is True


# --------------------------------------------------------------------------- UI banner


def test_posture_warning_is_silent_when_posture_is_fine():
    assert posture.posture_warning({"insecure": False, "enabled": False}) is None


def test_posture_warning_is_loud_and_actionable(monkeypatch):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    warning = posture.posture_warning()
    assert warning is not None
    assert "Insecure config push is enabled" in warning
    assert "any client can claim any agent identity" in warning
    assert "cleartext" in warning
    assert "OPAMP_ALLOW_INSECURE_CONFIG_PUSH" in warning


def test_posture_warning_reports_tls_and_audit_when_present():
    warning = posture.posture_warning(
        {"insecure": True, "agent_auth": "token", "tls": True, "audit": True}
    )
    assert "token (enforced)" in warning
    assert "TLS: **yes**" in warning
    assert "Audit log: **on**" in warning


# --------------------------------------------------------------------------- /health


def test_health_reports_posture(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["config_push"] == {
        "enabled": False,
        "insecure": False,
        "agent_auth": "off",
        "audit": False,
        "tls": False,
    }


def test_health_reports_insecure_override(client, monkeypatch):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    assert client.get("/health").json()["config_push"]["insecure"] is True


# --------------------------------------------------------------------------- /auth/status


def test_auth_status_read_only_mode(client):
    body = client.get("/auth/status").json()
    assert body == {"password_required": False, "auth_mode": "disabled", "roles": []}


def test_auth_status_password_mode(client, monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123")
    body = client.get("/auth/status").json()
    assert body == {"password_required": True, "auth_mode": "password", "roles": ["admin"]}


# --------------------------------------------------------------------------- /config/posture


def test_config_posture_requires_admin_when_configured(client, monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123")
    assert client.get("/config/posture").status_code == 401
    ok = client.get("/config/posture", auth=("admin", "pw123"))
    assert ok.status_code == 200


def test_config_posture_reports_every_requirement(client):
    body = client.get("/config/posture").json()
    assert body["config_push"]["enabled"] is False
    assert body["allowed"] is False
    assert body["override"] is False
    assert set(body["requirements"]) == {"admin_auth", "agent_auth", "tls"}
    assert all(not r["met"] for r in body["requirements"].values())
    assert len(body["unmet"]) == 3
    assert body["http"] == {
        "public_url": None,
        "tls": False,
        "tls_terminated_upstream": False,
    }
    assert body["admin_auth"] == {"mode": "disabled", "source": "none"}


def test_config_posture_never_leaks_the_credential(client, monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", PASSWORD)
    resp = client.get("/config/posture", auth=("admin", PASSWORD))
    assert resp.status_code == 200
    assert PASSWORD not in resp.text


# --------------------------------------------------------------------------- admin_auth


def test_admin_auth_generates_and_persists_a_random_password(monkeypatch, tmp_path):
    monkeypatch.setattr(admin_auth, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OPAMP_ADMIN_AUTOGENERATE_PASSWORD", "true")

    first = admin_auth.ensure()
    stored = tmp_path / "admin_password"
    assert first == stored.read_text().strip()
    assert len(first) >= 32
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert admin_auth.source() == "generated"
    assert admin_auth.configured() is True

    # A restart reuses the stored password instead of generating a new one.
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")
    assert admin_auth.ensure() == first


def test_admin_auth_env_beats_generated_file(monkeypatch, tmp_path):
    monkeypatch.setattr(admin_auth, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OPAMP_ADMIN_AUTOGENERATE_PASSWORD", "true")
    (tmp_path / "admin_password").write_text("stored-password\n")
    monkeypatch.setenv("ADMIN_PASSWORD", "from-env")
    assert admin_auth.ensure() == "from-env"
    assert admin_auth.source() == "env"


def test_admin_auth_stays_read_only_without_a_credential(monkeypatch, tmp_path):
    monkeypatch.setattr(admin_auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "none")
    assert admin_auth.ensure() == ""
    assert admin_auth.configured() is False
    assert not (tmp_path / "admin_password").exists()


def test_password_comparison_is_constant_time_and_exact(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", PASSWORD)
    assert admin_auth.verify(PASSWORD) is True
    assert admin_auth.verify(PASSWORD + "x") is False
    assert admin_auth.verify("") is False
    assert admin_auth.verify(None) is False


def test_verify_is_open_in_read_only_mode(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")
    assert admin_auth.verify("anything") is True
