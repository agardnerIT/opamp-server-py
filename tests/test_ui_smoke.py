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
REMOTE_CONFIG_PAGE = REPO / "ui" / "pages" / "5_Remote_Config.py"
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
        # #104 knobs
        self.push_allowed = True
        self.validation_fails = False
        self.rollout_refused = False
        self.agents = []
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
        # `admin_request()` goes through `.request()` for every verb, so the pages that
        # use it (Remote Config) need the agent listing here, not only in `.get()`.
        if method == "GET" and url.endswith("/agents"):
            return FakeResponse(200, {"agents": self.agents, "count": len(self.agents)})
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
        # #104: the Remote Config page. Deliberately dumb, like the rest of the fake.
        if url.endswith("/config/posture"):
            return FakeResponse(
                200,
                {
                    "allowed": self.push_allowed,
                    "config_push": {"enabled": self.push_allowed, "insecure": False},
                    "requirements": {
                        "admin_auth": {"met": True, "detail": "configured"},
                        "agent_auth": {"met": self.push_allowed, "detail": "token"},
                    },
                    "unmet": [] if self.push_allowed else ["agent_auth"],
                },
            )
        if url.endswith("/config/validate"):
            return FakeResponse(
                200,
                {
                    "valid": not self.validation_fails,
                    "objects": 1,
                    "size_bytes": 42,
                    "config_hash": "a" * 64,
                    "details": [{"path": "service.pipelines", "message": "no exporter",
                                 "severity": "error"}] if self.validation_fails else [],
                    "warnings": [],
                    "policy": {"status": "skipped"},
                },
            )
        if "/config/revisions" in url and method == "POST":
            return FakeResponse(200, {"proposal_id": "prop-1", "agents": 1,
                                      "config_hash": "a" * 64, "status": "proposed"})
        if "/config/revisions" in url:
            return FakeResponse(200, {"revisions": [], "count": 0, "statuses": []})
        if "/config/rollouts" in url and method == "POST":
            if self.rollout_refused:
                return FakeResponse(409, {"detail": {"message": "already active"}})
            if "dry_run" in json.dumps(kwargs.get("json") or {}):
                return FakeResponse(
                    200,
                    {"dry_run": True, "proposal_id": "prop-1", "config_hash": "a" * 64,
                     "agents": 1,
                     "batch": {"count": 2, "max_agents_per_batch": 25,
                               "health_gate_seconds": 90, "size": 1, "canary_percent": 5},
                     "batches": [
                         {"batch": 0, "agents": 1, "agent_ids": ["ab" * 16]},
                         {"batch": 1, "agents": 1, "agent_ids": ["cd" * 16]},
                     ]},
                )
            return FakeResponse(
                200,
                {"rollout_id": "ro-1", "status": "running", "agents": {"states": {}},
                 "batches": []},
            )
        if "/config/rollouts/" in url:
            return FakeResponse(
                200,
                {
                    "rollout_id": "ro-1",
                    "status": "paused",
                    "paused_reason": "auto_reverted",
                    "pause_detail": "agent cd… reverted",
                    "batch": {"current": 1, "count": 3, "health_gate_seconds": 90,
                              "started_at": "2026-11-02T00:00:00+00:00"},
                    "agents": {"total": 3, "states": {"applied": 1, "reverted": 1,
                                                      "pending": 1}},
                    "batches": [
                        {"batch": 0, "agents": 1, "status": "applied",
                         "states": {"applied": 1}, "agent_ids": ["ab" * 16]},
                        {"batch": 1, "agents": 1, "status": "paused",
                         "states": {"reverted": 1}, "agent_ids": ["cd" * 16]},
                    ],
                },
            )
        if "/config/rollouts" in url:
            return FakeResponse(200, {"rollouts": [], "count": 0, "statuses": []})
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
            return FakeResponse(200, {"agents": self.agents, "count": len(self.agents)})
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


def fake_agent(agent_id=None, healthy=True, capabilities=0x2):
    """One `GET /agents` row: the minimum the Remote Config page reads."""
    return {
        "id": agent_id or ("ab" * 16),
        "healthy": healthy,
        "capabilities": capabilities,
        "remote_config_status": "APPLIED",
        "description": {
            "non_identifying_attributes": {"deployment.environment": "production"}
        },
    }


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
    for kind in (
        "markdown",
        "caption",
        "info",
        "error",
        "warning",
        "success",
        # #104: the Remote Config flow is addressed by heading ("1. Target"), so a
        # helper that ignores headings cannot assert the page's shape.
        "header",
        "subheader",
        "title",
    ):
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


@pytest.mark.parametrize(
    "page",
    [
        "ui/app.py",
        "ui/pages/1_Agents.py",
        "ui/pages/2_Reports.py",
        "ui/pages/4_Help.py",
        # #104: the page that can take down a fleet must at least render in both
        # postures — an unsigned visit and a signed-in one lead to different code.
        "ui/pages/5_Remote_Config.py",
    ],
)
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


# ------------------------------------------------- #104 Remote Config page


def signed_in(path, role="approver", **state):
    app = AppTest.from_file(str(path), default_timeout=30)
    app.session_state["admin_session"] = ui_session.AdminSession.from_login(
        {
            "token": TOKEN,
            "expires_at": "2030-01-01T00:00:00+00:00",
            "principal": {"name": "alice", "role": role},
        }
    )
    for key, value in state.items():
        app.session_state[key] = value
    return app


def test_remote_config_page_asks_for_sign_in_before_showing_the_flow(server):
    app = run(REMOTE_CONFIG_PAGE, server)
    assert not app.exception, app.exception
    # The whole flow sits behind the shared login form: no selector, no editor, no
    # approve button before a session exists.
    assert "Sign in" in [button.label for button in app.button]
    assert "1. Target" not in texts(app)


def test_remote_config_page_renders_the_flow_and_the_blast_radius(server):
    server.agents = [fake_agent()]
    app = signed_in(REMOTE_CONFIG_PAGE).run()
    assert not app.exception, app.exception
    body = texts(app)
    for heading in ("1. Target", "2. Edit", "3. Validate", "4. Propose", "7. History"):
        assert heading in body, heading
    # The counts come from `GET /agents` and are the point of step 1.
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Agents matched"] == "1"
    assert metrics["Accepts config"] == "1"
    assert "1 × production" in body  # the blast-radius breakdown


def test_an_operator_can_plan_but_not_approve(server):
    server.agents = [fake_agent()]
    app = signed_in(REMOTE_CONFIG_PAGE, role="operator", rc_body="exporters:\n  debug: {}\n").run()
    # The approve button only exists once there is a plan (it is the plan's action).
    [button for button in app.button if (button.label or "") == "Validate"][0].click().run()
    [button for button in app.button if (button.label or "") == "Propose revision"][0].click().run()
    [button for button in app.button
     if (button.label or "") == "Plan the rollout (dry run)"][0].click().run()
    assert not app.exception, app.exception
    approve = [button for button in app.button if (button.label or "") == "Approve & roll out"]
    assert approve and approve[0].disabled is True
    assert "needs the **approver** role" in texts(app)


def test_validate_then_propose_then_plan_is_one_call_each(server):
    server.agents = [fake_agent()]
    app = signed_in(
        REMOTE_CONFIG_PAGE, rc_body="exporters:\n  debug: {}\n"
    ).run()
    assert not app.exception, app.exception

    [button for button in app.button if (button.label or "") == "Validate"][0].click().run()
    assert not app.exception, app.exception
    assert "Valid" in texts(app)
    assert "POST" in server.methods_for("/config/validate")

    [button for button in app.button if (button.label or "") == "Propose revision"][0].click().run()
    assert not app.exception, app.exception
    assert "prop-1" in texts(app)
    assert "POST" in server.methods_for("/config/revisions")

    [button for button in app.button if (button.label or "") == "Plan the rollout (dry run)"][0].click().run()
    assert not app.exception, app.exception
    assert "Rollout plan (nothing has been delivered)" in texts(app)
    assert "POST" in server.methods_for("/config/rollouts")
    # The plan is displayed, not just fetched: the batches are the operator's review.
    assert "2" in [metric.value for metric in app.metric]


def test_a_paused_rollout_shows_its_reason_not_just_paused(server):
    app = signed_in(REMOTE_CONFIG_PAGE, rc_rollout_id="ro-1").run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "auto_reverted" in body
    assert "last working config" in body  # the per-reason explanation
    assert "Nothing is retried and nothing is rolled back automatically" in body


def test_a_redacted_body_is_refused_before_it_can_be_pushed(server):
    app = signed_in(
        REMOTE_CONFIG_PAGE,
        rc_body="exporters:\n  otlp:\n    headers:\n      api-key: <redacted>\n",
    ).run()
    assert not app.exception, app.exception
    assert "would wipe every credential" in texts(app)


def test_a_disabled_push_posture_explains_itself(server):
    server.push_allowed = False
    app = signed_in(REMOTE_CONFIG_PAGE).run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "Config push is **disabled**" in body or "blocked by the security posture" in body


def test_a_failed_validation_lists_the_yaml_paths(server):
    server.validation_fails = True
    app = signed_in(REMOTE_CONFIG_PAGE, rc_body="service: {}\n").run()
    [button for button in app.button if (button.label or "") == "Validate"][0].click().run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "Validation failed" in body
    assert "service.pipelines" in body
