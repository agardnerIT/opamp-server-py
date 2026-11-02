"""#105: render the real Streamlit pages in-process, with the HTTP layer faked.

The grep tests in `test_ui_pages.py` prove *absence*; this file proves the pages
still **run**. It matters because the pages were untested, so a renamed helper or a
mistyped `st.*` call is invisible until someone loads the UI — and it already paid
for itself: it caught the "Extend" button consuming its own re-auth intent on first
render, which made re-authentication a silent no-op.

`streamlit` is an optional extra (`requirements-ui.txt`), which CI installs and a
minimal dev environment may not, so the whole module skips when it is missing. The
fakes are deliberately dumb: they exist to exercise the UI's branching, not to model
the server (that is `tests/test_auth.py`'s job).
"""

from __future__ import annotations

import json

import pytest
import requests

AppTest = pytest.importorskip(
    "streamlit.testing.v1", reason="streamlit is the optional [ui] extra"
).AppTest

from pathlib import Path

from ui import session as ui_session

REPO = Path(__file__).resolve().parent.parent
ADMIN_PAGE = REPO / "ui" / "pages" / "3_Admin.py"
HELP_PAGE = REPO / "ui" / "pages" / "4_Help.py"
TOKEN = "opamp_admin_smoketoken"


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload) if self._payload is not None else ""

    @property
    def ok(self):
        return 200 <= self.status_code < 300


class FakeServer:
    """Records every call and answers just enough to drive the pages."""

    def __init__(self, role="approver", auth_mode="principals"):
        self.role = role
        self.auth_mode = auth_mode
        self.login_status = 201
        self.login_headers = {}
        self.token_valid = True
        self.calls = []

    # -- assertions helpers -----------------------------------------------
    def methods_for(self, fragment):
        return [method for method, url in self.calls if fragment in url]

    # -- transport --------------------------------------------------------
    def _token_ok(self, headers):
        return self.token_valid and (headers or {}).get("Authorization") == f"Bearer {TOKEN}"

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url))
        authed = self._token_ok(headers)
        if "/alerts" in url:
            if not authed:
                return FakeResponse(401, {"detail": "invalid credentials"})
            if method == "PUT":
                return FakeResponse(200, {"config": {"events": {}}})
            return FakeResponse(
                200,
                {"types": ["webhook"], "events": ["new_agent"], "config": {"events": {"new_agent": {}}}},
            )
        if url.endswith("/auth/sessions"):
            if not authed:
                return FakeResponse(401, {"detail": "invalid credentials"})
            return FakeResponse(
                200,
                {
                    "sessions": [
                        {
                            "session_id": "aaaaaa111111",
                            "principal": "alice",
                            "principal_id": "prin_1",
                            "expired": False,
                        }
                    ]
                },
            )
        if "/compliance/reload" in url:
            return FakeResponse(200 if authed else 401, {"success": authed})
        if "/compliance/check/" in url:
            return FakeResponse(200 if authed else 401, {"compliant": authed})
        return FakeResponse(404, {"detail": "not found"})

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(("GET", url))
        if url.endswith("/auth/status"):
            return FakeResponse(
                200,
                {
                    "password_required": True,
                    "auth_mode": self.auth_mode,
                    "roles": list(ui_session.ROLES),
                    "sessions": True,
                    "principals": 1,
                    "legacy_password": self.auth_mode == "password",
                },
            )
        if url.endswith("/health"):
            return FakeResponse(200, {"opa_enabled": True, "config_push": {}})
        if url.endswith("/agents"):
            return FakeResponse(200, {"agents": [], "count": 0})
        if url.endswith("/compliance/validate"):
            return FakeResponse(200, {"opa_enabled": True, "policies": []})
        return self.request("GET", url, headers=headers)

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.calls.append(("POST", url))
        if url.endswith("/auth/session"):
            if self.login_status != 201:
                return FakeResponse(
                    self.login_status, {"detail": "too many failed attempts"}, self.login_headers
                )
            return FakeResponse(
                201,
                {
                    "token": TOKEN,
                    "token_type": "bearer",
                    "expires_at": "2030-01-01T00:00:00+00:00",
                    "idle_timeout_seconds": 900,
                    "ttl_seconds": 3600,
                    "principal": {"name": "alice", "role": self.role},
                },
            )
        return self.request("POST", url, headers=headers)

    def delete(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(("DELETE", url))
        if "/auth/sessions/" in url:
            return FakeResponse(200, {"session_id": url.rsplit("/", 1)[-1], "revoked": True})
        return FakeResponse(200, {"logged_out": True})


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    for name in ("get", "post", "delete", "request"):
        monkeypatch.setattr(requests, name, getattr(fake, name))
    # `requests.put()` is only used through `admin_request`, which goes via `.request`.
    monkeypatch.setattr(requests, "put", fake.request.__get__(fake))
    return fake


def run(path, server):
    app = AppTest.from_file(str(path), default_timeout=30)
    app.run()
    return app


def sign_in(app, attempts=0, name="alice", password="correct-horse"):
    if name is not None and any(field.label == "Name" for field in app.text_input):
        app.text_input(key=f"admin_login_name_{attempts}").input(name)
    app.text_input(key=f"admin_login_password_{attempts}").input(password)
    [button for button in app.button if (button.label or "") == "Sign in"][0].click().run()
    return app


def texts(app):
    out = []
    for kind in ("markdown", "caption", "info", "error", "warning", "success"):
        for element in getattr(app, kind):
            out.append(str(getattr(element, "value", "")))
    return "\n".join(out)


def session_state(app):
    """The keys currently in session state (the proxy is iterable)."""
    return set(app.session_state)


# ------------------------------------------------------------------- the page


def test_unsigned_admin_page_renders_the_shared_login_form(server):
    app = run(ADMIN_PAGE, server)
    assert not app.exception, app.exception
    assert "Admin sign-in required" in texts(app)
    assert any(field.label == "Name" for field in app.text_input)
    assert any(field.label == "Password" for field in app.text_input)
    # Nothing privileged was attempted.
    assert server.methods_for("/alerts") == []


def test_login_failure_message_survives_the_rerun(server):
    app = run(ADMIN_PAGE, server)
    server.login_status = 401
    sign_in(app)
    assert not app.exception, app.exception
    assert "Invalid credentials" in texts(app)


def test_lockout_shows_the_retry_delay(server):
    app = run(ADMIN_PAGE, server)
    server.login_status = 429
    server.login_headers = {"Retry-After": "300"}
    sign_in(app)
    assert not app.exception, app.exception
    assert "Try again in 5 minutes" in texts(app)


def test_successful_login_keeps_a_token_and_never_a_password(server):
    app = run(ADMIN_PAGE, server)
    sign_in(app)

    assert not app.exception, app.exception
    assert "Admin Panel" in texts(app)
    assert "admin_session" in session_state(app)
    assert "admin_password" not in session_state(app)
    assert not any("password" in key for key in session_state(app))
    session = app.session_state["admin_session"]
    assert isinstance(session, ui_session.AdminSession)
    assert session.token == TOKEN
    # Every privileged call carried the bearer token, never HTTP Basic.
    assert all("Basic" not in str(call) for call in server.calls)


def test_expired_session_returns_to_the_login_form_with_a_sentence(server):
    app = run(ADMIN_PAGE, server)
    sign_in(app)
    server.token_valid = False
    app.run()

    assert not app.exception, app.exception
    assert "Admin sign-in required" in texts(app)
    assert "no longer valid" in texts(app)
    assert "admin_session" not in session_state(app)


def test_admin_only_actions_are_hidden_for_an_approver(server):
    server.role = "approver"
    app = run(ADMIN_PAGE, server)
    sign_in(app)
    labels = [tab.label for tab in app.tabs]
    assert "Sessions" not in labels
    assert server.methods_for("/auth/sessions") == []


def test_admin_sees_the_sessions_tab_and_can_list_them(server):
    server.role = "admin"
    app = run(ADMIN_PAGE, server)
    sign_in(app)
    assert "Sessions" in [tab.label for tab in app.tabs]
    assert "GET" in server.methods_for("/auth/sessions")


def test_read_only_server_explains_itself_instead_of_asking_for_a_password(server, monkeypatch):
    app = AppTest.from_file(str(ADMIN_PAGE), default_timeout=30)
    # A server with no credential answers `password_required: false`.
    original = server.get

    def read_only_get(url, **kwargs):
        if url.endswith("/auth/status"):
            return FakeResponse(
                200,
                {
                    "password_required": False,
                    "auth_mode": "disabled",
                    "roles": [],
                    "sessions": False,
                    "principals": 0,
                    "legacy_password": False,
                },
            )
        return original(url, **kwargs)

    monkeypatch.setattr(requests, "get", read_only_get)
    app.run()
    assert not app.exception, app.exception
    assert "read-only" in texts(app)
    assert "Admin sign-in required" not in texts(app)


def test_extend_is_an_audited_reauth_that_revokes_the_old_token(server):
    app = run(ADMIN_PAGE, server)
    sign_in(app)
    before = len(server.calls)

    [button for button in app.button if (button.label or "") == "Extend"][0].click().run()
    assert "audited re-auth" in texts(app)
    assert "admin_session" in session_state(app), "the old session must survive until re-auth"

    sign_in(app, attempts=1)
    assert not app.exception, app.exception
    assert "Admin Panel" in texts(app)
    during = server.calls[before:]
    assert any(method == "POST" and url.endswith("/auth/session") for method, url in during), (
        "the re-auth must mint a new token"
    )
    assert any(method == "DELETE" and url.endswith("/auth/session") for method, url in during), (
        "the superseded token must be revoked"
    )


def test_extend_can_be_cancelled(server):
    app = run(ADMIN_PAGE, server)
    sign_in(app)
    [button for button in app.button if (button.label or "") == "Extend"][0].click().run()
    [button for button in app.button if (button.label or "") == "Cancel"][0].click().run()
    assert not app.exception, app.exception
    assert "Admin Panel" in texts(app)


def test_sign_out_everywhere_revokes_sessions_then_drops_the_local_one(server):
    server.role = "admin"
    app = AppTest.from_file(str(HELP_PAGE), default_timeout=30)
    app.session_state["admin_session"] = ui_session.AdminSession.from_login(
        {
            "token": TOKEN,
            "expires_at": "2030-01-01T00:00:00+00:00",
            "principal": {"name": "alice", "role": "admin"},
        }
    )
    app.run()

    [button for button in app.button if (button.label or "") == "Sign out everywhere"][0].click().run()
    assert not app.exception, app.exception
    assert "DELETE" in server.methods_for("/auth/sessions/")
    assert any(
        method == "DELETE" and url.endswith("/auth/session") for method, url in server.calls
    )
    assert "admin_session" not in session_state(app)
    assert "Signed out" in texts(app)


def test_a_viewer_is_not_offered_sign_out_everywhere(server):
    app = AppTest.from_file(str(HELP_PAGE), default_timeout=30)
    app.session_state["admin_session"] = ui_session.AdminSession.from_login(
        {
            "token": TOKEN,
            "expires_at": "2030-01-01T00:00:00+00:00",
            "principal": {"name": "alice", "role": "viewer"},
        }
    )
    app.run()
    labels = [button.label for button in app.button]
    assert "Sign out" in labels
    assert "Sign out everywhere" not in labels


@pytest.mark.parametrize("page", ["ui/app.py", "ui/pages/1_Agents.py", "ui/pages/2_Reports.py", "ui/pages/4_Help.py"])
def test_every_page_renders(server, page):
    app = run(REPO / page, server)
    assert not app.exception, app.exception


def test_sidebar_labels_the_role_and_the_expiry(server):
    app = AppTest.from_file(str(HELP_PAGE), default_timeout=30)
    app.session_state["admin_session"] = ui_session.AdminSession.from_login(
        {
            "token": TOKEN,
            "expires_at": "2030-01-01T00:00:00+00:00",
            "principal": {"name": "alice", "role": "approver"},
        }
    )
    app.run()
    body = texts(app)
    assert "alice" in body and "approver" in body
    assert "expires in" in body
