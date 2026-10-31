"""Admin auth hardening: principals, roles, sessions and lockout (#95).

Every acceptance criterion in the issue maps to a test here:

- the shared `ADMIN_PASSWORD` still works, as a single synthesised `admin`
  principal, with a deprecation warning (one code path, not two)
- a named principal logs in with its own password and gets its own role
- sessions: opaque token, SHA-256 at rest, absolute TTL + idle timeout, revocable,
  logout, "revoke that device"
- lockout per (identity, client IP) with exponential backoff and `Retry-After`;
  `X-Forwarded-For` is ignored unless `TRUST_PROXY_HEADERS=true`
- every privileged route enforces its role (matrix test, all routes at once)
- read-only mode (no credential) is read-open and **write-closed**
- secrets never reach a response, a listing, an audit line or an error body
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from server import admin_auth, auth
from server.audit import audit_event
from server.main import app
from server.state import (
    SQLitePrincipalStore,
    SQLiteSessionStore,
    utcnow,
)

LEGACY = "legacy-pw-1234567890"


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Isolated principal/session DBs, no legacy secret, no leftover lockouts."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "none")
    monkeypatch.setattr(auth, "PRINCIPALS", SQLitePrincipalStore(tmp_path / "pr.db"))
    monkeypatch.setattr(auth, "SESSIONS", SQLiteSessionStore(tmp_path / "pr.db"))
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("ADMIN_LOGIN_MAX_ATTEMPTS", raising=False)
    auth.reset_lockouts()
    yield
    auth.reset_lockouts()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def legacy(monkeypatch):
    """Only the shared password configured (what every existing deployment has)."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", LEGACY)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    return LEGACY


@pytest.fixture
def admin_principal(client, legacy):
    """`root` (admin role) and its Basic header, created through the API."""
    created = client.post(
        "/auth/principals",
        json={"name": "root", "role": "admin", "password": "root-pw-123456"},
        headers=basic("", legacy),
    )
    assert created.status_code == 201, created.text
    return created.json(), basic("root", "root-pw-123456")


def basic(name: str, password: str) -> dict:
    token = base64.b64encode(f"{name}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_principal(client, headers, name: str, role: str, password: str = "pw-123456789") -> dict:
    response = client.post(
        "/auth/principals",
        json={"name": name, "role": role, "password": password},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client, name: str = "", password: str = "") -> dict:
    response = client.post("/auth/session", json={"name": name, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def failures(reason: str) -> float:
    return auth.PROM_ADMIN_AUTH_FAILURES.labels(reason=reason)._value.get()


# --------------------------------------------------------------- the primitives


class TestPrimitives:
    def test_password_hash_is_scrypt_and_self_describing(self):
        stored = auth.hash_password("hunter2hunter2")
        scheme, n, r, p, salt, digest = stored.split("$")
        assert scheme == "scrypt" and int(n) == auth.SCRYPT_N and int(r) == auth.SCRYPT_R
        assert int(p) == auth.SCRYPT_P and salt and digest
        assert auth.verify_password("hunter2hunter2", stored)
        assert not auth.verify_password("hunter2hunter3", stored)

    def test_each_hash_has_a_fresh_salt(self):
        first = auth.hash_password("same-password")
        second = auth.hash_password("same-password")
        assert first != second
        assert auth.verify_password("same-password", first)
        assert auth.verify_password("same-password", second)

    def test_verify_handles_garbage_without_raising(self):
        for garbage in ("", "not-a-hash", "scrypt$x$y$z$a$b", "bcrypt$1$2$3$a$b"):
            assert auth.verify_password("x", garbage) is False

    def test_roles_are_ordered(self):
        assert auth.role_satisfies("admin", "approver")
        assert auth.role_satisfies("approver", "operator")
        assert auth.role_satisfies("operator", "viewer")
        assert not auth.role_satisfies("operator", "approver")
        assert not auth.role_satisfies("viewer", "operator")
        assert not auth.role_satisfies("nonsense", "viewer")

    def test_normalize_role_rejects_unknown(self):
        assert auth.normalize_role(" APPROVER ") == "approver"
        with pytest.raises(ValueError):
            auth.normalize_role("superuser")

    def test_session_tokens_are_stored_only_as_hashes(self, client, legacy):
        session = login(client, "", legacy)
        token = session["token"]
        assert token.startswith(auth.SESSION_PREFIX)
        rows = auth.SESSIONS.list_for_principal("legacy")
        assert rows and token not in json.dumps(rows, default=str)
        assert auth.hash_session(token) == rows[0]["session_hash"]

    def test_audit_drops_fields_that_look_like_secrets(self):
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
        try:
            audit_event(
                "test.event",
                kind="test",
                principal="alice",
                token="opamp_admin_leak",
                password="hunter2",
                secret_hash="deadbeef",
            )
        finally:
            logger.remove(sink)
        assert lines, "audit_event wrote nothing"
        payload = lines[0].split("AUDIT ", 1)[1]
        assert "opamp_admin_leak" not in payload and "hunter2" not in payload
        assert "deadbeef" not in payload
        assert json.loads(payload)["principal"] == "alice"


# --------------------------------------------------------------------- login


class TestLegacyPassword:
    def test_any_name_works_with_the_shared_password(self, client, legacy):
        for name in ("", "admin", "whatever"):
            session = login(client, name, legacy)
            assert session["principal"] == {"name": "admin", "role": "admin"}

    def test_wrong_password_is_401_and_says_nothing(self, client, legacy):
        response = client.post("/auth/session", json={"name": "admin", "password": "nope"})
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid credentials"

    def test_basic_auth_still_works_for_scripts(self, client, legacy):
        assert client.get("/auth/me", headers=basic("", legacy)).json()["role"] == "admin"

    def test_legacy_sessions_die_with_the_password(self, client, legacy, monkeypatch):
        # Losing the credential must not leave a live session behind: the server
        # drops to read-only mode (anonymous viewer), and every write is refused.
        token = login(client, "", legacy)["token"]
        assert client.get("/auth/me", headers=bearer(token)).json()["role"] == "admin"
        monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")
        me = client.get("/auth/me", headers=bearer(token)).json()
        assert me["method"] == "anonymous" and me["authenticated"] is False
        write = client.put("/alerts", json={"events": {}}, headers=bearer(token))
        assert write.status_code == 403 and "read-only" in write.json()["detail"]

    def test_deprecation_warning_is_emitted(self, legacy):
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
        try:
            auth.warn_if_legacy()
        finally:
            logger.remove(sink)
        assert any("shared ADMIN_PASSWORD" in line for line in lines)


class TestPrincipals:
    def test_login_with_a_named_principal(self, client, admin_principal):
        _, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        session = login(client, "riley", "pw-123456789")
        assert session["principal"] == {"name": "riley", "role": "operator"}
        me = client.get("/auth/me", headers=bearer(session["token"])).json()
        assert me["name"] == "riley" and me["role"] == "operator" and me["method"] == "session"

    def test_unknown_principal_and_bad_secret_are_indistinguishable(self, client, admin_principal):
        _, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        unknown = client.post("/auth/session", json={"name": "ghost", "password": "pw-123456789"})
        bad = client.post("/auth/session", json={"name": "riley", "password": "wrong-password"})
        assert unknown.status_code == bad.status_code == 401
        assert unknown.json() == bad.json()

    def test_revoked_principal_cannot_log_in(self, client, admin_principal):
        _, headers = admin_principal
        record = make_principal(client, headers, "riley", "operator")
        assert client.delete(f"/auth/principals/{record['principal_id']}", headers=headers).status_code == 200
        response = client.post("/auth/session", json={"name": "riley", "password": "pw-123456789"})
        assert response.status_code == 403

    def test_expired_principal_cannot_log_in(self, client, admin_principal):
        _, headers = admin_principal
        past = (utcnow() - timedelta(hours=1)).isoformat()
        client.post(
            "/auth/principals",
            json={"name": "temp", "role": "operator", "password": "pw-123456789", "expires_at": past},
            headers=headers,
        )
        response = client.post("/auth/session", json={"name": "temp", "password": "pw-123456789"})
        assert response.status_code == 403

    def test_create_validates_role_name_and_password(self, client, admin_principal):
        _, headers = admin_principal
        bad_role = client.post(
            "/auth/principals",
            json={"name": "x", "role": "superuser", "password": "pw-123456789"},
            headers=headers,
        )
        assert bad_role.status_code == 422
        short = client.post(
            "/auth/principals",
            json={"name": "x", "role": "viewer", "password": "short"},
            headers=headers,
        )
        assert short.status_code == 422
        blank = client.post(
            "/auth/principals",
            json={"name": "  ", "role": "viewer", "password": "pw-123456789"},
            headers=headers,
        )
        assert blank.status_code == 422
        colon = client.post(
            "/auth/principals",
            json={"name": "a:b", "role": "viewer", "password": "pw-123456789"},
            headers=headers,
        )
        assert colon.status_code == 422  # ':' would break HTTP Basic

    def test_duplicate_name_is_409_even_after_revocation(self, client, admin_principal):
        _, headers = admin_principal
        record = make_principal(client, headers, "riley", "operator")
        client.delete(f"/auth/principals/{record['principal_id']}", headers=headers)
        again = client.post(
            "/auth/principals",
            json={"name": "riley", "role": "admin", "password": "pw-123456789"},
            headers=headers,
        )
        assert again.status_code == 409

    def test_principal_listings_never_contain_secrets(self, client, admin_principal):
        _, headers = admin_principal
        body = client.get("/auth/principals", headers=headers).text
        assert "secret_hash" not in body and "scrypt" not in body
        assert "root-pw-123456" not in body

    def test_revoke_closes_the_principals_sessions(self, client, admin_principal):
        _, headers = admin_principal
        record = make_principal(client, headers, "riley", "admin")
        token = login(client, "riley", "pw-123456789")["token"]
        assert client.get("/auth/me", headers=bearer(token)).status_code == 200
        revoked = client.delete(f"/auth/principals/{record['principal_id']}", headers=headers)
        assert revoked.json()["sessions_revoked"] == 1
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401

    def test_revoke_unknown_principal_is_404(self, client, admin_principal):
        _, headers = admin_principal
        assert client.delete("/auth/principals/prin_nope", headers=headers).status_code == 404


# ------------------------------------------------------------------- sessions


class TestSessions:
    def test_list_and_revoke_a_session(self, client, admin_principal):
        record, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        token = login(client, "riley", "pw-123456789")["token"]

        sessions = client.get("/auth/sessions", headers=headers).json()["sessions"]
        assert any(session["principal"] == "riley" for session in sessions)
        assert all("session_hash" not in str(session) for session in sessions)

        target = next(session for session in sessions if session["principal"] == "riley")
        revoked = client.delete(f"/auth/sessions/{target['session_id']}", headers=headers)
        assert revoked.status_code == 200 and revoked.json()["revoked"] is True
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401

    def test_revoke_unknown_session_is_404(self, client, admin_principal):
        _, headers = admin_principal
        assert client.delete("/auth/sessions/abcdef123456", headers=headers).status_code == 404

    def test_short_session_id_is_422(self, client, admin_principal):
        _, headers = admin_principal
        assert client.delete("/auth/sessions/abc", headers=headers).status_code == 422

    def test_logout_revokes_own_session(self, client, admin_principal):
        _, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        token = login(client, "riley", "pw-123456789")["token"]
        assert client.delete("/auth/session", headers=bearer(token)).json()["logged_out"] is True
        assert client.delete("/auth/session", headers=bearer(token)).status_code == 401
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401

    def test_legacy_sessions_are_visible_and_revocable(self, client, legacy):
        """A session opened with the shared password must not be invisible to admin."""
        token = login(client, "", legacy)["token"]
        sessions = client.get("/auth/sessions", headers=basic("", legacy)).json()["sessions"]
        legacy_views = [s for s in sessions if s["principal_id"] == "legacy"]
        assert legacy_views and legacy_views[0]["principal"] == "admin (shared password)"
        revoked = client.delete(
            f"/auth/sessions/{legacy_views[0]['session_id']}", headers=basic("", legacy)
        )
        assert revoked.json()["revoked"] is True
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401

    def test_logout_with_basic_has_nothing_to_revoke(self, client, legacy):
        assert client.delete("/auth/session", headers=basic("", legacy)).status_code == 400

    def test_absolute_ttl_expires_the_session(self, client, admin_principal, monkeypatch):
        _, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        token = login(client, "riley", "pw-123456789")["token"]
        _rewind_session(auth.hash_session(token), expires_at=utcnow() - timedelta(minutes=1))
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401
        assert failures(auth.REASON_SESSION_EXPIRED) >= 1

    def test_idle_timeout_expires_the_session(self, client, admin_principal, monkeypatch):
        _, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        token = login(client, "riley", "pw-123456789")["token"]
        _rewind_session(auth.hash_session(token), last_seen_at=utcnow() - timedelta(hours=1))
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401

    def test_idle_touch_keeps_a_used_session_alive(self, client, admin_principal):
        _, headers = admin_principal
        make_principal(client, headers, "riley", "operator")
        token = login(client, "riley", "pw-123456789")["token"]
        _rewind_session(auth.hash_session(token), last_seen_at=utcnow() - timedelta(minutes=5))
        assert client.get("/auth/me", headers=bearer(token)).status_code == 200
        # The refresh is throttled to one write a minute, so the row must have moved.
        row = auth.SESSIONS.get(auth.hash_session(token))
        assert (utcnow() - utcnow().fromisoformat(row["last_seen_at"])).total_seconds() < 5

    def test_ttl_and_idle_are_configurable(self, monkeypatch):
        monkeypatch.setenv("ADMIN_SESSION_TTL_SECONDS", "120")
        monkeypatch.setenv("ADMIN_SESSION_IDLE_SECONDS", "30")
        assert auth.session_ttl_seconds() == 120
        assert auth.session_idle_seconds() == 30
        monkeypatch.setenv("ADMIN_SESSION_TTL_SECONDS", "not-a-number")
        assert auth.session_ttl_seconds() == 3600


def _rewind_session(session_hash: str, **fields) -> None:
    """Move a session's timestamps into the past to simulate the clock moving."""
    with sqlite3.connect(auth.SESSIONS.db_path) as conn:
        for column, value in fields.items():
            conn.execute(
                f"UPDATE admin_sessions SET {column}=? WHERE session_hash=?",
                (value.isoformat(), session_hash),
            )


# -------------------------------------------------------------------- lockout


class TestLockout:
    def test_repeated_failures_lock_the_identity_out(self, client, legacy, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "3")
        monkeypatch.setenv("ADMIN_LOGIN_BACKOFF_SECONDS", "60")
        auth.reset_lockouts()
        for _ in range(3):
            assert client.post("/auth/session", json={"name": "admin", "password": "nope"}).status_code == 401
        blocked = client.post("/auth/session", json={"name": "admin", "password": "nope"})
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        assert failures(auth.REASON_LOCKED_OUT) >= 1

    def test_lockout_survives_a_correct_password_until_it_expires(self, client, legacy, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "2")
        auth.reset_lockouts()
        for _ in range(2):
            client.post("/auth/session", json={"name": "admin", "password": "nope"})
        assert client.post("/auth/session", json={"name": "admin", "password": legacy}).status_code == 429

    def test_a_successful_login_clears_the_counter(self, client, legacy, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "3")
        auth.reset_lockouts()
        for _ in range(2):
            client.post("/auth/session", json={"name": "admin", "password": "nope"})
        assert client.post("/auth/session", json={"name": "admin", "password": legacy}).status_code == 201
        # A fresh budget: two more failures still cannot lock it out.
        for _ in range(2):
            client.post("/auth/session", json={"name": "admin", "password": "nope"})
        assert client.post("/auth/session", json={"name": "admin", "password": legacy}).status_code == 201

    def test_backoff_grows(self, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("ADMIN_LOGIN_BACKOFF_SECONDS", "10")
        auth.reset_lockouts()
        first = auth.record_failure("k", auth.REASON_BAD_SECRET)
        second = auth.record_failure("k", auth.REASON_BAD_SECRET)
        third = auth.record_failure("k", auth.REASON_BAD_SECRET)
        assert (first, second, third) == (10, 20, 40)

    def test_backoff_is_capped(self, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("ADMIN_LOGIN_BACKOFF_SECONDS", "60")
        monkeypatch.setenv("ADMIN_LOGIN_MAX_BACKOFF_SECONDS", "120")
        auth.reset_lockouts()
        for _ in range(20):
            wait = auth.record_failure("k", auth.REASON_BAD_SECRET)
        assert wait == 120

    def test_lockout_is_per_identity(self, client, legacy, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "2")
        auth.reset_lockouts()
        for _ in range(2):
            client.post("/auth/session", json={"name": "admin", "password": "nope"})
        # A different identity from the same client is not locked out.
        assert client.post("/auth/session", json={"name": "someone", "password": legacy}).status_code == 201

    def test_x_forwarded_for_is_ignored_by_default(self, client, legacy, monkeypatch):
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "2")
        auth.reset_lockouts()
        for port in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            client.post(
                "/auth/session",
                json={"name": "admin", "password": "nope"},
                headers={"X-Forwarded-For": port},
            )
        # Same socket peer for all three, so the counter accumulates and locks.
        blocked = client.post("/auth/session", json={"name": "admin", "password": "nope"})
        assert blocked.status_code == 429

    def test_x_forwarded_for_is_honoured_when_trusted(self, client, legacy, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "2")
        auth.reset_lockouts()
        for _ in range(2):
            client.post(
                "/auth/session",
                json={"name": "admin", "password": "nope"},
                headers={"X-Forwarded-For": "9.9.9.9"},
            )
        blocked = client.post(
            "/auth/session",
            json={"name": "admin", "password": "nope"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        assert blocked.status_code == 429
        # ...and a different forwarded client keeps its own budget.
        allowed = client.post(
            "/auth/session",
            json={"name": "admin", "password": legacy},
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        assert allowed.status_code == 201

    def test_basic_auth_attempts_are_locked_out_too(self, client, legacy, monkeypatch):
        """/auth/verify must not be a free credential oracle."""
        monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "3")
        auth.reset_lockouts()
        for _ in range(3):
            assert client.get("/auth/verify", headers=basic("admin", "wrong")).status_code == 401
        blocked = client.get("/auth/verify", headers=basic("admin", "wrong"))
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        # Even the right password waits: that is what a lockout means.
        assert client.get("/auth/verify", headers=basic("admin", legacy)).status_code == 429

    def test_a_session_token_is_not_locked_out(self, client, admin_principal):
        """A 256-bit token cannot be guessed, so it must not consume lockout budget."""
        _, headers = admin_principal
        auth.reset_lockouts()
        for _ in range(10):
            assert client.get("/auth/me", headers=bearer("opamp_admin_nope")).status_code == 401
        assert client.get("/auth/me", headers=headers).status_code == 200

    def test_lockout_map_stays_bounded(self, monkeypatch):
        monkeypatch.setattr(auth, "MAX_LOCKOUT_ENTRIES", 10)
        auth.reset_lockouts()
        for index in range(50):
            auth.record_failure(f"k{index}", auth.REASON_BAD_SECRET)
        assert len(auth._FAILURES) <= 10


# ---------------------------------------------------------------------- roles


# (method, path, minimum role, body) for every privileged route. `body` keeps the
# request cheap: these only assert the *gate*, not the endpoint's own semantics.
PRIVILEGED_ROUTES = [
    ("GET", "/auth/me", "viewer", None),
    ("GET", "/auth/sessions", "admin", None),
    ("DELETE", "/auth/sessions/abcdef123456", "admin", None),
    ("GET", "/auth/principals", "admin", None),
    ("POST", "/auth/principals", "admin", {"name": "n", "role": "viewer", "password": "pw-123456789"}),
    ("DELETE", "/auth/principals/prin_nope", "admin", None),
    ("GET", "/config/posture", "viewer", None),
    ("GET", "/alerts", "viewer", None),
    ("PUT", "/alerts", "operator", {"events": {}}),
    ("POST", "/alerts/test", "operator", {"event_type": "new_agent"}),
    ("POST", "/compliance/reload", "admin", {}),
    ("POST", "/compliance/check/" + "ab" * 16, "operator", {}),
    ("POST", "/agents/" + "ab" * 16 + "/token", "admin", {}),
    ("DELETE", "/agents/" + "ab" * 16 + "/token", "admin", None),
]


class TestRoleMatrix:
    @pytest.mark.parametrize("method,path,minimum,body", PRIVILEGED_ROUTES)
    def test_no_credential_is_401(self, client, legacy, method, path, minimum, body):
        response = client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        assert "www-authenticate" in {key.lower() for key in response.headers}

    @pytest.mark.parametrize("method,path,minimum,body", PRIVILEGED_ROUTES)
    @pytest.mark.parametrize("role", ["viewer", "operator"])
    def test_lower_roles_are_403_unless_allowed(
        self, client, admin_principal, method, path, minimum, body, role
    ):
        _, admin_headers = admin_principal
        make_principal(client, admin_headers, f"user-{role}", role)
        headers = basic(f"user-{role}", "pw-123456789")
        response = client.request(method, path, json=body, headers=headers)
        if auth.role_satisfies(role, minimum):
            assert response.status_code != 403, f"{role} should reach {method} {path}"
        else:
            assert response.status_code == 403, f"{role} reached {method} {path}"
            assert "requires" in response.json()["detail"]
            assert failures(auth.REASON_ROLE) >= 1

    def test_approver_can_do_everything_an_operator_can(self, client, admin_principal):
        _, headers = admin_principal
        make_principal(client, headers, "ava", "approver")
        approver = basic("ava", "pw-123456789")
        assert client.get("/alerts", headers=approver).status_code == 200
        assert client.put("/alerts", json={"events": {}}, headers=approver).status_code == 200
        # ...but identity administration stays with admin.
        assert client.get("/auth/principals", headers=approver).status_code == 403

    def test_require_admin_alias_still_answers(self, client, admin_principal):
        _, headers = admin_principal
        request = _fake_request(headers)
        assert auth.require_admin(request) is True


def _fake_request(headers: dict):
    """Minimal Request with headers and no client (for direct helper calls)."""

    class _Request:
        def __init__(self, headers):
            self.headers = headers
            self.client = None

    return _Request(headers)


# ------------------------------------------------------------- read-only mode


class TestReadOnlyMode:
    def test_reads_are_open(self, client):
        assert client.get("/auth/status").json()["password_required"] is False
        assert client.get("/alerts").status_code == 200
        assert client.get("/config/posture").status_code == 200
        me = client.get("/auth/me").json()
        assert me["method"] == "anonymous" and me["authenticated"] is False

    def test_writes_are_refused_with_an_actionable_message(self, client):
        for method, path, body in (
            ("PUT", "/alerts", {"events": {}}),
            ("POST", "/alerts/test", {"event_type": "new_agent"}),
            ("POST", "/agents/" + "ab" * 16 + "/token", {}),
        ):
            response = client.request(method, path, json=body)
            assert response.status_code == 403
            assert "read-only" in response.json()["detail"]

    def test_warning_is_emitted(self):
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
        try:
            auth.warn_if_read_only()
        finally:
            logger.remove(sink)
        assert any("read-only mode" in line for line in lines)


# --------------------------------------------------------------- config push gate


class TestPostureGate:
    def test_a_principal_satisfies_the_admin_requirement(self, client, admin_principal):
        """No ADMIN_PASSWORD, only principals: the push gate can still open (#95/#96)."""
        from server import posture

        requirements = posture.requirements()
        assert requirements["admin_auth"]["met"] is True

    def test_read_only_mode_does_not_satisfy_it(self, client):
        from server import posture

        assert posture.requirements()["admin_auth"]["met"] is False

    def test_principal_only_posture_opens_push_without_a_shared_password(
        self, client, admin_principal, monkeypatch
    ):
        from server import posture

        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
        monkeypatch.setenv("TLS_TERMINATED_UPSTREAM", "true")
        assert posture.push_allowed() is True


# ------------------------------------------------------------------ redaction


SECRET_URL = "https://hooks.slack.com/services/T000/B000/supersecrettoken"


class TestAlertRedaction:
    def test_get_redacts_the_webhook_url_and_headers(self, client, admin_principal):
        _, headers = admin_principal
        client.put(
            "/alerts",
            json={
                "events": {
                    "new_agent": {
                        "enabled": True,
                        "webhook_url": SECRET_URL,
                        "headers": '{"Authorization": "Bearer abc123"}',
                    }
                }
            },
            headers=headers,
        )
        body = client.get("/alerts", headers=headers).json()["config"]
        event = body["events"]["new_agent"]
        assert "supersecrettoken" not in json.dumps(body)
        assert "Bearer abc123" not in json.dumps(body)
        assert event["webhook_url"].startswith("https://hooks.slack.com/")
        assert event["webhook_url"].endswith(auth_redacted())
        assert event["headers"] == auth_redacted()
        assert event["enabled"] is True  # non-secret fields survive

    def test_put_with_the_sentinel_keeps_the_stored_secret(self, client, admin_principal):
        _, headers = admin_principal
        client.put(
            "/alerts",
            json={"events": {"new_agent": {"webhook_url": SECRET_URL}}},
            headers=headers,
        )
        fetched = client.get("/alerts", headers=headers).json()["config"]
        client.put("/alerts", json=fetched, headers=headers)  # a UI round-trip
        from server.alerts import ALERT_CONFIG

        assert ALERT_CONFIG["events"]["new_agent"]["webhook_url"] == SECRET_URL

    def test_put_with_a_new_url_replaces_it(self, client, admin_principal):
        _, headers = admin_principal
        client.put("/alerts", json={"events": {"new_agent": {"webhook_url": SECRET_URL}}}, headers=headers)
        new = "https://example.com/other-token"
        client.put("/alerts", json={"events": {"new_agent": {"webhook_url": new}}}, headers=headers)
        from server.alerts import ALERT_CONFIG

        assert ALERT_CONFIG["events"]["new_agent"]["webhook_url"] == new

    def test_sentinel_with_nothing_stored_is_dropped(self, client, admin_principal):
        _, headers = admin_principal
        from server.alerts import ALERT_CONFIG

        ALERT_CONFIG["events"]["new_agent"]["webhook_url"] = ""
        client.put(
            "/alerts",
            json={"events": {"new_agent": {"webhook_url": auth_redacted()}}},
            headers=headers,
        )
        assert ALERT_CONFIG["events"]["new_agent"]["webhook_url"] == ""

    def test_redact_url_keeps_scheme_and_host_only(self):
        from server.alerts import REDACTED, redact_url

        assert redact_url(SECRET_URL) == f"https://hooks.slack.com/{REDACTED}"
        assert redact_url("https://example.com") == "https://example.com"
        assert redact_url("") == ""


def auth_redacted() -> str:
    from server.alerts import REDACTED

    return REDACTED
