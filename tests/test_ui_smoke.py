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

import base64
import json

import pytest
import requests

AppTest = pytest.importorskip(
    "streamlit.testing.v1", reason="streamlit is the optional [ui] extra"
).AppTest

from pathlib import Path

from ui import session as ui_session

REPO = Path(__file__).resolve().parent.parent
AGENTS_PAGE = REPO / "ui" / "pages" / "1_Agents.py"
ADMIN_DIR = REPO / "ui" / "pages" / "admin"
ALERTS_PAGE = ADMIN_DIR / "alerts.py"
COMPLIANCE_PAGE = ADMIN_DIR / "compliance.py"
AGENT_TOKENS_PAGE = ADMIN_DIR / "agent_tokens.py"
SESSIONS_PAGE = ADMIN_DIR / "sessions.py"
UPDATES_PAGE = ADMIN_DIR / "updates.py"
RELEASE_EMAILS_PAGE = ADMIN_DIR / "release_emails.py"
#: Every admin subpage. The Admin page used to be one file with tabs; these are its
#: sections, one page each.
ADMIN_PAGES = [
    ALERTS_PAGE,
    COMPLIANCE_PAGE,
    AGENT_TOKENS_PAGE,
    SESSIONS_PAGE,
    UPDATES_PAGE,
    RELEASE_EMAILS_PAGE,
]
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
        # #111 knobs
        self.groups = []
        self.health_defined = []
        self.login_status = 201
        self.login_headers = {}
        self.token_valid = True
        # #124: what `/health` reports as `config_push.agent_auth`, which decides whether
        # the connect snippet carries a credential.
        self.agent_auth_mode = "off"
        # #126: the token surface. `minted` is what POST /agent-tokens returns, so a
        # test can assert the page shows *that* token and not a placeholder.
        self.agent_tokens = []
        self.minted_token = "opamp_agent_smoketoken"
        # The agent-detail view: what `/agent/{id}` reports as the agent's effective
        # config. `None` unless a test sets it, so the default page has none.
        self.effective_config = None
        self.calls = []
        # Request bodies, so a test can assert what the page *sent*, not only where (#131).
        self.bodies = []

    # -- assertions helpers -----------------------------------------------
    def methods_for(self, fragment):
        return [method for method, url in self.calls if fragment in url]

    def body_for(self, fragment):
        """The JSON body of the last request to a URL containing `fragment`."""
        for url, body in reversed(self.bodies):
            if fragment in url:
                return body
        raise AssertionError(
            f"no body recorded for {fragment!r}; recorded: {[url for url, _ in self.bodies]}"
        )

    # -- transport --------------------------------------------------------
    def _token_ok(self, headers):
        return self.token_valid and (headers or {}).get("Authorization") == f"Bearer {TOKEN}"

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url))
        if kwargs.get("json") is not None:
            self.bodies.append((url, kwargs["json"]))
        authed = self._token_ok(headers)
        # `admin_request()` goes through `.request()` for every verb, so the pages that
        # use it (Remote Config) need the agent listing here, not only in `.get()`.
        if method == "GET" and url.endswith("/agents"):
            return FakeResponse(200, {"agents": self.agents, "count": len(self.agents)})
        # The agent detail view, through the verb the Remote Config prefill uses (#131).
        if method == "GET" and "/agent/" in url:
            return self._agent_detail(url)
        # #126: fleet tokens.
        if url.endswith("/agent-tokens"):
            if method == "GET":
                return FakeResponse(
                    200, {"tokens": self.agent_tokens, "count": len(self.agent_tokens)}
                )
            if not authed:
                return FakeResponse(401, {"detail": "invalid credentials"})
            body = kwargs.get("json") or {}
            minted = {
                "token": self.minted_token,
                "token_id": "agtok_smoke",
                "label": body.get("label") or "",
                "created_at": "2026-01-01T00:00:00+00:00",
                "expires_at": None,
                "max_agents": body.get("max_agents"),
                "collector_snippet": "extensions:\n  opamp: ...",
                "note": "shown once",
            }
            self.agent_tokens.append(
                {
                    "token_id": minted["token_id"],
                    "label": minted["label"],
                    "agent_id": None,
                    "bound": False,
                    "created_at": minted["created_at"],
                    "expires_at": None,
                    "expired": False,
                    "max_agents": minted["max_agents"],
                    "agents_used": 0,
                    "last_seen_at": None,
                    "revoked_at": None,
                    "active": True,
                }
            )
            return FakeResponse(201, minted)
        if method == "DELETE" and "/agent-tokens/" in url:
            if not authed:
                return FakeResponse(401, {"detail": "invalid credentials"})
            token_id = url.rsplit("/", 1)[-1]
            return FakeResponse(200, {"token_id": token_id, "revoked": True})
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
        # #111: groups. The API shape matters here: the page reads `member_count`,
        # `sources` and `health_defined`, all resolved server-side.
        if url.endswith("/groups/attribute-keys"):
            return FakeResponse(
                200,
                {
                    "keys": [
                        {
                            "key": "environment",
                            "agent_count": 1,
                            "values": ["production"],
                            "value_count": 1,
                        }
                    ],
                    "count": 1,
                    "reserved": ["healthy", "remote_config_status", "status"],
                    "matching": "exact, case-insensitive equality",
                },
            )
        if method == "POST" and "/groups/" in url and url.endswith("/preview"):
            name = url.split("/groups/")[1].rsplit("/", 1)[0]
            return FakeResponse(
                200,
                {
                    "group_id": "grp-1",
                    "name": name,
                    "count": len(self.agents),
                    "members": [agent["id"] for agent in self.agents],
                    "previous_count": 3,
                    "added": [],
                    "dropped": ["ab" * 16],
                    "unchanged": [],
                    "sources": {
                        "definition": [agent["id"] for agent in self.agents],
                        "included": [],
                        "excluded": ["ab" * 16],
                    },
                    "unknown_pins": [],
                },
            )
        if method == "POST" and url.endswith("/restore"):
            return FakeResponse(200, {"group_id": "grp-1", "name": "prod"})
        if "/groups" in url:
            if method == "POST" and url.rstrip("/").endswith("/groups"):
                body = kwargs.get("json") or {}
                return FakeResponse(
                    200,
                    {
                        "group_id": "grp-2",
                        "name": body.get("name"),
                        "definition": body.get("definition") or {},
                        "member_count": 0,
                        "members": [],
                        "sources": {"definition": [], "included": [], "excluded": []},
                        "health_defined": [],
                    },
                )
            if method == "DELETE":
                return FakeResponse(200, {"group_id": "grp-1", "archived_at": "2026-01-01"})
            return FakeResponse(200, {"groups": self.groups, "count": len(self.groups)})
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
            return FakeResponse(
                200,
                {
                    "opa_enabled": True,
                    "config_push": {"agent_auth": self.agent_auth_mode},
                },
            )
        if url.endswith("/agents"):
            return FakeResponse(200, {"agents": self.agents, "count": len(self.agents)})
        if "/agent/" in url:
            return self._agent_detail(url)
        if url.endswith("/compliance/validate"):
            return FakeResponse(200, {"opa_enabled": True, "policies": []})
        return self.request("GET", url, headers=headers)

    def _agent_detail(self, url):
        """One agent's detail view. Only the fields the pages read are filled; the
        point is to render the effective-config panel, not to model the API."""
        agent_id = url.rsplit("/", 1)[-1].split("?")[0]
        detail = dict(fake_agent(agent_id))
        detail["effective_config"] = self.effective_config
        detail["metrics"] = {}
        detail["components"] = {}
        detail["capability_tags"] = []
        detail["last_heartbeat"] = "2026-01-01T00:00:00+00:00"
        return FakeResponse(200, detail)

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.calls.append(("POST", url))
        kwargs["json"] = json
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
        return self.request("POST", url, headers=headers, json=kwargs.get("json"))

    def delete(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(("DELETE", url))
        if "/auth/sessions/" in url:
            return FakeResponse(200, {"session_id": url.rsplit("/", 1)[-1], "revoked": True})
        return FakeResponse(200, {"logged_out": True})


def fake_agent(agent_id=None, healthy=True, capabilities=0x2):
    """One `GET /agents` row in the shape the **server** actually serialises.

    The description is camelCase lists of `{key, value: {stringValue}}` (#75), not a
    plain mapping: the mapping shape is what let the blast radius read "3 × unknown"
    against the real API while this fixture said "1 × production" (#104).
    """
    return {
        "id": agent_id or ("ab" * 16),
        "healthy": healthy,
        "capabilities": capabilities,
        "remote_config_status": "APPLIED",
        "description": {
            "identifyingAttributes": [
                {"key": "service.name", "value": {"stringValue": "otelcol"}},
            ],
            "nonIdentifyingAttributes": [
                {
                    "key": "deployment.environment",
                    "value": {"stringValue": "production"},
                }
            ],
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
    app = run(ALERTS_PAGE, server)
    assert not app.exception, app.exception
    assert "Admin sign-in required" in texts(app)
    assert any(field.label == "Name" for field in app.text_input)
    assert any(field.label == "Password" for field in app.text_input)
    # Nothing privileged was attempted.
    assert server.methods_for("/alerts") == []


def test_login_failure_message_survives_the_rerun(server):
    app = run(ALERTS_PAGE, server)
    server.login_status = 401
    sign_in(app)
    assert not app.exception, app.exception
    assert "Invalid credentials" in texts(app)


def test_lockout_shows_the_retry_delay(server):
    app = run(ALERTS_PAGE, server)
    server.login_status = 429
    server.login_headers = {"Retry-After": "300"}
    sign_in(app)
    assert not app.exception, app.exception
    assert "Try again in 5 minutes" in texts(app)


def test_successful_login_keeps_a_token_and_never_a_password(server):
    app = run(ALERTS_PAGE, server)
    sign_in(app)

    assert not app.exception, app.exception
    assert "Alerts" in texts(app)
    assert "admin_session" in session_state(app)
    assert "admin_password" not in session_state(app)
    assert not any("password" in key for key in session_state(app))
    session = app.session_state["admin_session"]
    assert isinstance(session, ui_session.AdminSession)
    assert session.token == TOKEN
    # Every privileged call carried the bearer token, never HTTP Basic.
    assert all("Basic" not in str(call) for call in server.calls)


def test_expired_session_returns_to_the_login_form_with_a_sentence(server):
    app = run(ALERTS_PAGE, server)
    sign_in(app)
    server.token_valid = False
    app.run()

    assert not app.exception, app.exception
    assert "Admin sign-in required" in texts(app)
    assert "no longer valid" in texts(app)
    assert "admin_session" not in session_state(app)


def test_admin_only_pages_refuse_a_lower_role(server):
    """The tab is gone, the gate is not: an approver cannot see session data."""
    server.role = "approver"
    app = run(SESSIONS_PAGE, server)
    sign_in(app)

    assert not app.exception, app.exception
    assert "needs" in texts(app) and "admin" in texts(app)
    assert server.methods_for("/auth/sessions") == [], (
        "the page must not call the API for a role the server would refuse"
    )


def test_admin_sees_the_sessions_page_and_can_list_them(server):
    server.role = "admin"
    app = run(SESSIONS_PAGE, server)
    sign_in(app)

    assert not app.exception, app.exception
    assert "Sessions" in texts(app)
    assert "GET" in server.methods_for("/auth/sessions")


def test_read_only_server_explains_itself_instead_of_asking_for_a_password(server, monkeypatch):
    app = AppTest.from_file(str(ALERTS_PAGE), default_timeout=30)
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
    app = run(ALERTS_PAGE, server)
    sign_in(app)
    before = len(server.calls)

    [button for button in app.button if (button.label or "") == "Extend"][0].click().run()
    assert "audited re-auth" in texts(app)
    assert "admin_session" in session_state(app), "the old session must survive until re-auth"

    sign_in(app, attempts=1)
    assert not app.exception, app.exception
    assert "Alerts" in texts(app)
    during = server.calls[before:]
    assert any(method == "POST" and url.endswith("/auth/session") for method, url in during), (
        "the re-auth must mint a new token"
    )
    assert any(method == "DELETE" and url.endswith("/auth/session") for method, url in during), (
        "the superseded token must be revoked"
    )


def test_extend_can_be_cancelled(server):
    app = run(ALERTS_PAGE, server)
    sign_in(app)
    [button for button in app.button if (button.label or "") == "Extend"][0].click().run()
    [button for button in app.button if (button.label or "") == "Cancel"][0].click().run()
    assert not app.exception, app.exception
    assert "Alerts" in texts(app)


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
        # The Admin sections: pages now, so each is a page that has to render.
        "ui/pages/admin/alerts.py",
        "ui/pages/admin/compliance.py",
        "ui/pages/admin/agent_tokens.py",
        "ui/pages/admin/sessions.py",
        "ui/pages/admin/updates.py",
        "ui/pages/admin/release_emails.py",
    ],
)
def test_every_page_renders(server, page):
    app = run(REPO / page, server)
    assert not app.exception, app.exception


def test_the_router_registers_every_page_and_leaves_none_orphaned():
    """The Admin sections are pages, so the navigation is the only way to reach them.

    A page file that is not registered is a page nobody can visit — exactly the bug
    the tabs had in reverse. Comparing the router's registrations to the directory
    catches both a missing entry and a stale one.
    """
    import re

    router = (REPO / "ui" / "app.py").read_text()
    registered = set(re.findall(r'page\(\s*"([^"]+)"', router))
    on_disk = {
        str(path.relative_to(REPO / "ui" / "pages"))
        for path in (REPO / "ui" / "pages").rglob("*.py")
        if "__pycache__" not in str(path)
    }

    assert registered == on_disk
    # The sections that used to be tabs are individual pages, not one Admin page.
    assert not (REPO / "ui" / "pages" / "3_Admin.py").exists()
    assert {"admin/alerts.py", "admin/agent_tokens.py"} <= registered


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


def token_row(**overrides):
    """One `GET /agent-tokens` row in the shape the server serialises (#126)."""
    row = {
        "token_id": "agtok_abc123",
        "label": "prod",
        "agent_id": None,
        "bound": False,
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": None,
        "expired": False,
        "max_agents": 15,
        "agents_used": 3,
        "last_seen_at": None,
        "revoked_at": None,
        "active": True,
    }
    row.update(overrides)
    return row


def test_the_token_page_shows_the_table_with_cap_and_expiry(server):
    """#126: "which token is about to expire / has hit its cap" is the whole point."""
    server.agent_tokens = [
        token_row(),
        token_row(token_id="agtok_full", label="old", agents_used=15),
        token_row(token_id="agtok_dead", label="leaked", revoked_at="2026-02-01T00:00:00+00:00"),
    ]
    app = signed_in(AGENT_TOKENS_PAGE, role="admin").run()
    assert not app.exception, app.exception
    assert "Agent tokens" in texts(app)

    table = app.dataframe[0].value
    states = list(table["State"])
    # A cap that is full and a token that was revoked are different problems, so the
    # table names them differently instead of making the reader compare numbers.
    assert states == ["active", "at cap", "revoked"]
    assert list(table["Agents"])[0] == "3 of 15"
    assert "never" in list(table["Expires"])[0]
    # The handle is shown (it is what revocation takes); the token is not, and cannot be.
    assert "agtok_full" in "\n".join(table["Token"])


def test_revoking_a_token_needs_a_confirmation_and_is_audited_by_the_server(server):
    server.agent_tokens = [token_row()]
    app = signed_in(AGENT_TOKENS_PAGE, role="admin").run()
    revoke = [button for button in app.button if (button.label or "") == "Revoke token"][0]
    assert revoke.disabled is True, "revoking blocks a fleet — it must not be one click"

    app.checkbox(key="admin_revoke_token_confirm").set_value(True).run()
    [button for button in app.button if (button.label or "") == "Revoke token"][0].click().run()
    assert not app.exception, app.exception
    # The page reruns after a revoke so the table reflects the new state; the proof is
    # the request, and the server's audit entry is what actually records it.
    assert any(
        method == "DELETE" and url.endswith("/agent-tokens/agtok_abc123")
        for method, url in server.calls
    )


def test_the_token_page_mints_a_replacement_after_a_revocation(server):
    """Revoking here must not leave the operator unable to mint a new one.

    The form used to exist only inside the empty-fleet branch of the Agents page, which
    is unreachable as soon as one agent has connected — so revoking your only token
    locked you out of the UI until you found `opampctl`. Then it moved into a collapsed
    expander inside a tab, which is the same failure with extra steps: you had to know
    to look. It is a page-level form now, visible on arrival.
    """
    server.agent_auth_mode = "token"
    server.agent_tokens = [token_row(token_id="agtok_dead", revoked_at="2026-02-01T00:00:00+00:00")]
    app = signed_in(AGENT_TOKENS_PAGE, role="admin").run()
    assert not app.exception, app.exception

    submit = [button for button in app.button if (button.label or "") == "Generate token"]
    assert submit, "the token page has no way to mint a token"
    # Nothing has to be expanded or selected first: the form is the page.
    assert "Mint a token" in texts(app)
    assert not [element for element in app.expander], (
        "the mint form must not be hidden behind a disclosure"
    )

    submit[0].click().run()
    assert not app.exception, app.exception

    shown = "\n".join(element.value for element in app.code)
    assert server.minted_token in shown, "the new token must be shown once, in full"
    assert "only time it is shown" in texts(app)
    assert any(
        method == "POST" and url.endswith("/agent-tokens") for method, url in server.calls
    ), "minting must go through the shared admin transport"


def test_the_token_page_does_not_offer_minting_when_no_token_is_required(server):
    """An open endpoint makes a token meaningless, and offering one would imply it is not."""
    server.agent_auth_mode = "off"
    app = signed_in(AGENT_TOKENS_PAGE, role="admin").run()
    assert not app.exception, app.exception

    assert not [
        button for button in app.button if (button.label or "") == "Generate token"
    ]
    assert "OPAMP_AGENT_AUTH=token" in texts(app)


def test_a_non_admin_cannot_see_the_token_page(server):
    """The server re-checks the role; the page is the courtesy, not the control."""
    app = signed_in(AGENT_TOKENS_PAGE, role="operator").run()

    assert not app.exception, app.exception
    assert "needs" in texts(app) and "admin" in texts(app)
    assert server.methods_for("/agent-tokens") == []


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


def test_approving_a_rollout_fills_the_live_progress_field(server):
    """A rollout started in the page is visible immediately (#104).

    The field is a keyed widget, so without the autofill it keeps the empty value it
    was first created with and the operator sees "Rollout started" above a blank
    progress box — the rollout exists, but the page silently does not show it.
    """
    server.agents = [fake_agent()]
    app = signed_in(REMOTE_CONFIG_PAGE, rc_body="exporters:\n  debug: {}\n").run()
    [button for button in app.button if (button.label or "") == "Validate"][0].click().run()
    [button for button in app.button if (button.label or "") == "Propose revision"][0].click().run()
    [button for button in app.button
     if (button.label or "") == "Plan the rollout (dry run)"][0].click().run()
    [button for button in app.button
     if (button.label or "") == "Approve & roll out"][0].click().run()

    assert not app.exception, app.exception
    assert "Rollout started" in texts(app)
    field = [widget for widget in app.text_input if widget.key == "rc_rollout_id"][0]
    assert field.value == "ro-1"
    # The live view rendered, not just the success message next to an empty field.
    assert "auto_reverted" in texts(app)


def planned_rollout(server, role="approver"):
    """Drive steps 1–4 to a dry-run plan, which is where the gate field appears."""
    server.agents = [fake_agent()]
    app = signed_in(
        REMOTE_CONFIG_PAGE, role=role, rc_body="exporters:\n  debug: {}\n"
    ).run()
    [button for button in app.button if (button.label or "") == "Validate"][0].click().run()
    [button for button in app.button
     if (button.label or "") == "Propose revision"][0].click().run()
    [button for button in app.button
     if (button.label or "") == "Plan the rollout (dry run)"][0].click().run()
    assert not app.exception, app.exception
    return app


def rollout_bodies(server):
    """`(body, is_dry_run)` for every POST /config/rollouts the page made."""
    return [
        (body, bool(body.get("dry_run")))
        for url, body in server.bodies
        if url.endswith("/config/rollouts")
    ]


def test_the_health_gate_box_starts_at_the_server_default(server):
    """`CONFIG_PUSH_HEALTH_GATE_SECONDS` seeds the field; it does not own it.

    The first plan carries no override, so the number the server reports *is* the
    configured default (90 in the fake, as in `server/rollout.py`). That reported value
    is what the box is filled from — the page has no other way to know it, and hardcoding
    90 here would rot the moment an operator changed the env var.
    """
    app = planned_rollout(server)
    field = [widget for widget in app.number_input if widget.key == "rc_health_gate"][0]
    assert field.value == 90
    # Not an override: the plan asked the server what the default is.
    body, dry_run = rollout_bodies(server)[-1]
    assert dry_run and "health_gate_seconds" not in body


def test_the_number_in_the_health_gate_box_is_what_the_rollout_gets(server):
    """Editing the box and approving must not fall back to the env default.

    The bug this pins: the create body used to carry only `note`/`restart_after_apply`,
    so the gate was whatever the server env said — a plan reviewed at 30s would silently
    run at 90s.
    """
    app = planned_rollout(server)
    app.number_input(key="rc_health_gate").set_value(30).run()
    assert not app.exception, app.exception
    # The plan above is now stale, and the page says so rather than letting the operator
    # approve against numbers that no longer match the box.
    assert "The plan above used 90s" in texts(app)

    [button for button in app.button
     if (button.label or "") == "Re-plan with this gate (dry run)"][0].click().run()
    assert not app.exception, app.exception
    body, dry_run = rollout_bodies(server)[-1]
    assert dry_run and body["health_gate_seconds"] == 30

    [button for button in app.button
     if (button.label or "") == "Approve & roll out"][0].click().run()
    assert not app.exception, app.exception
    body, dry_run = rollout_bodies(server)[-1]
    assert not dry_run
    assert body["health_gate_seconds"] == 30


def test_a_zero_health_gate_is_sent_rather_than_dropped(server):
    """`0` is a legal gate (a fast lab) and must survive the trip to the server.

    `gate or None` would turn "promote immediately" into "wait 90s", and a body built
    with `if gate:` would do the same. Only `is not None` is correct.
    """
    app = planned_rollout(server)
    app.number_input(key="rc_health_gate").set_value(0).run()
    [button for button in app.button
     if (button.label or "") == "Approve & roll out"][0].click().run()
    assert not app.exception, app.exception
    body, dry_run = rollout_bodies(server)[-1]
    assert not dry_run
    assert body["health_gate_seconds"] == 0


def test_the_health_gate_box_is_not_overwritten_by_a_re_plan(server):
    """Seeded once. A re-plan echoes a gate back, and that echo must not reseed.

    If the seeding keyed off "what the plan reported", the next plan would rewrite the
    box from its own echo — harmless here, but the same rule applied after a proposal
    change would quietly restore the env default over an operator's number.
    """
    app = planned_rollout(server)
    app.number_input(key="rc_health_gate").set_value(45).run()
    [button for button in app.button
     if (button.label or "") == "Re-plan with this gate (dry run)"][0].click().run()
    # The fake echoes a fixed 90 regardless; the box must still hold the operator's 45.
    field = [widget for widget in app.number_input if widget.key == "rc_health_gate"][0]
    assert field.value == 45
    assert "Re-plan to check it at 45s" in texts(app)


def test_description_attribute_reads_the_serialised_list_shape():
    """The blast radius must read the API's real shape, not a fixture's convenience."""
    from ui.shared import description_attribute

    description = fake_agent()["description"]
    assert description_attribute(description, "deployment.environment") == "production"
    assert description_attribute(description, "environment") is None
    assert description_attribute(None, "environment") is None
    # The flat mapping is still accepted (older snapshots, hand-written fixtures).
    assert (
        description_attribute(
            {"non_identifying_attributes": {"environment": "staging"}}, "environment"
        )
        == "staging"
    )


def test_a_paused_rollout_shows_its_reason_not_just_paused(server):
    app = signed_in(REMOTE_CONFIG_PAGE, rc_rollout_id="ro-1").run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "auto_reverted" in body
    assert "last working config" in body  # the per-reason explanation
    assert "Nothing is retried and nothing is rolled back automatically" in body


def test_prefill_loads_the_agent_config_decoded(server):
    """Prefill used to put the report's base64 in the box, so proposals pushed base64.

    `MessageToDict` renders every protobuf `bytes` body as base64, and the page read
    `configMap.configMap[*].body` verbatim (#131). What an operator edits has to be the
    document, and the content type has to follow it — a JSON body stored as `text/yaml`
    is a second, quieter bug (#118).
    """
    agent = fake_agent()
    server.agents = [agent]
    document = json.dumps(
        {
            "schedule": {
                "battery_stats": {
                    "query": "SELECT percent_remaining FROM battery;",
                    "interval": 32,
                }
            }
        }
    )
    server.effective_config = json.dumps(
        {
            "configMap": {
                "configMap": {
                    "": {
                        "body": base64.b64encode(document.encode()).decode(),
                        "contentType": "application/json",
                    }
                }
            }
        }
    )

    app = signed_in(REMOTE_CONFIG_PAGE, rc_prefill=agent["id"]).run()
    assert not app.exception, app.exception
    prefill = [
        button
        for button in app.button
        if (button.label or "") == "Prefill from this agent's effective config"
    ][0]
    prefill.click().run()
    assert not app.exception, app.exception

    body = app.session_state["rc_body"]
    assert "percent_remaining" in body
    assert body.strip().startswith("{")
    assert app.session_state["rc_content_type"] == "application/json"
    assert "Loaded object" in texts(app)


def test_the_object_name_the_operator_gives_is_the_key_that_is_proposed(server):
    """The `config_map` key is the name the agent looks its config up by.

    It was hardcoded to `""` here, so a config aimed at an osquery agent was
    misaddressed: the extension wants `osquery.json`, reported FAILED, poisoned the
    revision and paused the rollout at batch 0.
    """
    server.agents = [fake_agent()]
    app = signed_in(
        REMOTE_CONFIG_PAGE,
        rc_body='{"schedule": {}}',
        rc_object_key="osquery.json",
    ).run()
    assert not app.exception, app.exception

    [
        button
        for button in app.button
        if (button.label or "") == "Propose revision"
    ][0].click().run()
    assert not app.exception, app.exception

    config_map = server.body_for("/config/revisions")["config_map"]
    assert list(config_map) == ["osquery.json"]
    assert config_map["osquery.json"]["body"] == '{"schedule": {}}'

    # Validate sends the same keying, so the warning it returns is about the real one.
    [button for button in app.button if (button.label or "") == "Validate"][0].click().run()
    assert list(server.body_for("/config/validate")["config_map"]) == ["osquery.json"]


def test_prefill_carries_the_object_name_so_the_revision_matches_the_agent(server):
    """A prefilled body under a different key is still a mis-addressed push.

    The report already says which name the agent applied (`osquery.json`); the page
    read the body and dropped the name, so the proposal re-keyed it to `""`.
    """
    agent = fake_agent()
    server.agents = [agent]
    document = json.dumps({"schedule": {"battery_stats": {"query": "select 1;"}}})
    server.effective_config = json.dumps(
        {
            "configMap": {
                "configMap": {
                    "osquery.json": {
                        "body": base64.b64encode(document.encode()).decode(),
                        "contentType": "application/json",
                    }
                }
            }
        }
    )

    app = signed_in(REMOTE_CONFIG_PAGE, rc_prefill=agent["id"]).run()
    [
        button
        for button in app.button
        if (button.label or "") == "Prefill from this agent's effective config"
    ][0].click().run()
    assert not app.exception, app.exception
    assert app.session_state["rc_object_key"] == "osquery.json"

    [
        button
        for button in app.button
        if (button.label or "") == "Propose revision"
    ][0].click().run()
    assert list(server.body_for("/config/revisions")["config_map"]) == ["osquery.json"]


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


# --------------------------------------------------- #75: the re-issue banner


def test_a_reissued_uid_explains_itself():
    """The Agents page must be able to explain an id change, not just show one.

    Tested as a string function rather than through `AppTest`, because the detail view
    is addressed by a query param and `AppTest` has no way to set one. The page calls
    the same function, so the wording cannot drift from what is asserted here.
    """
    from ui.shared import reissue_banner_text

    assert reissue_banner_text(None, None) is None
    assert reissue_banner_text("", "host_identity") is None

    host = reissue_banner_text("ab" * 16, "host_identity")
    assert "ab" * 16 in host
    assert "different host identities" in host
    assert "opamp_duplicate_instance_uid_total" in host

    counter = reissue_banner_text("ab" * 16, "sequence_regression")
    assert "counter kept going backwards" in counter

    # An unknown reason is shown verbatim rather than swallowed.
    assert "something odd" in reissue_banner_text("ab" * 16, "something odd")


# --------------------------------------------------- empty fleet onboarding (#116)


def empty_agents_page(server):
    server.agents = []
    app = signed_in(REPO / "ui/pages/1_Agents.py")
    app.run()
    assert not app.exception, app.exception
    return app


def test_an_empty_fleet_offers_a_copy_paste_collector_config(server):
    """#116: the first screen a new user sees must contain the next step."""
    app = empty_agents_page(server)
    snippet = "\n".join(element.value for element in app.code)
    assert "extensions:" in snippet and "opamp:" in snippet
    assert "/v1/opamp" in snippet
    assert "service:" in snippet and "extensions: [opamp]" in snippet
    assert "No agents connected yet" in texts(app)
    # A download button means the file never has to be retyped from a screenshot.
    assert any(button.label == "Download config.yaml" for button in app.download_button)


def test_the_docker_toggle_rewrites_the_endpoint_for_a_containerised_collector(server):
    app = empty_agents_page(server)
    assert "localhost" in "\n".join(element.value for element in app.code)
    app.toggle(key="empty_fleet_in_container").set_value(True).run()
    assert not app.exception, app.exception
    snippet = "\n".join(element.value for element in app.code)
    assert "host.docker.internal" in snippet
    assert "host-gateway" in texts(app)


def test_the_guided_mint_gates_before_it_shows_a_form(server):
    """Sign-in must come *before* the form, not after the submit.

    Gating inside the submit branch dropped the intent: `require_admin` rendered the
    sign-in form and `st.stop()`ed the run, so the POST never happened and the operator
    came back to an empty form with their label and expiry reset. Click → sign in →
    nothing → click again is the loop this test exists to prevent.
    """
    server.agent_auth_mode = "token"
    server.agents = []
    server.role = "admin"  # minting a credential needs the admin role, not approver
    app = AppTest.from_file(str(AGENTS_PAGE), default_timeout=30)
    app.run()
    assert not app.exception, app.exception

    # The step is still explained while signed out — the context must not vanish with
    # the form — but a form the operator cannot submit is not offered at all.
    assert "Generate a token for this fleet" in texts(app)
    assert "Admin sign-in required" in texts(app)
    assert not [button for button in app.button if button.label == "Generate token"]

    sign_in(app)
    assert not app.exception, app.exception

    submit = [button for button in app.button if button.label == "Generate token"]
    assert submit, "signing in must reveal the form"
    submit[0].click().run()
    assert not app.exception, app.exception
    snippet = "\n".join(element.value for element in app.code)
    assert server.minted_token in snippet, "one click after signing in must mint the token"


def test_a_token_server_offers_to_mint_the_token_instead_of_printing_a_placeholder(server):
    """#124/#126: a token server 401s the unauthenticated shape, and the operator has
    no token to put in it — so the first screen has to produce one.

    #124 made the snippet honest about the mode; #126 made it *usable*, by minting the
    fleet token here. Printing `Bearer ${env:...}` at someone who has no token yet is
    the same dead end in a nicer font.
    """
    server.agent_auth_mode = "token"
    app = empty_agents_page(server)
    body = texts(app)
    assert "Generate a token for this fleet" in body
    assert "not tied to an agent id" in body
    # No snippet yet: there is nothing runnable to show until a token exists.
    assert not [code for code in app.code if "Authorization" in code.value]


def test_minting_shows_the_token_once_and_a_runnable_snippet(server):
    """The token is the whole point: the snippet must contain the one just minted."""
    server.agent_auth_mode = "token"
    server.agents = []
    app = signed_in(REPO / "ui/pages/1_Agents.py", role="admin")
    app.run()
    assert not app.exception, app.exception

    submit = next(button for button in app.button if button.label == "Generate token")
    submit.click().run()
    assert not app.exception, app.exception

    snippet = "\n".join(element.value for element in app.code)
    assert server.minted_token in snippet
    assert 'Authorization: "Bearer ' + server.minted_token + '"' in snippet
    body = texts(app)
    assert "only time it is shown" in body
    assert "agtok_smoke" in body
    # The form is gone once a token exists — a second mint would be a second secret.
    assert "Generate a token for this fleet" not in body
    # Minting went through the one admin transport, with a cap and an expiry by default
    # (the form's defaults are the safe ones, not "never"/"unlimited").
    assert any(method == "POST" and url.endswith("/agent-tokens") for method, url in server.calls)


def test_the_empty_fleet_snippet_claims_no_credential_on_an_open_server(server):
    server.agent_auth_mode = "off"
    app = empty_agents_page(server)
    snippet = "\n".join(element.value for element in app.code)
    assert "headers" not in snippet and "Authorization" not in snippet
    # Open is stated, not implied: the operator cannot accept a risk nobody named.
    assert "OPAMP_AGENT_AUTH=off" in texts(app)


def test_the_help_page_documents_the_same_credential_block(server):
    server.agent_auth_mode = "token"
    app = signed_in(HELP_PAGE)
    app.run()
    assert not app.exception, app.exception
    snippet = "\n".join(element.value for element in app.code)
    assert "OPAMP_AGENT_TOKEN" in snippet
    assert "requires an agent credential" in texts(app)


def test_an_unreachable_health_check_does_not_claim_the_endpoint_is_open(server, monkeypatch):
    """Unknown is its own answer: `off` would be a guess with a 401 at the end of it."""

    def offline(url, *args, **kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "get", offline)
    app = signed_in(HELP_PAGE)
    app.run()
    assert not app.exception, app.exception
    snippet = "\n".join(element.value for element in app.code)
    assert "Authorization" not in snippet
    assert "Could not read this server's agent-auth mode" in texts(app)


def test_an_unreachable_server_is_not_reported_as_a_configuration_problem(server, monkeypatch):
    server.agents = []

    def offline(url, *args, **kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "get", offline)
    app = signed_in(REPO / "ui/pages/1_Agents.py")
    app.run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "unreachable" in body
    assert "connection refused" in body
    assert not app.code, "a snippet would send a new user looking in the wrong place"


# ------------------------------------------------- collector groups in the UI (#111)


def group_row(**overrides):
    row = {
        "group_id": "grp-1",
        "name": "prod-emea",
        "definition": {"environment": "production"},
        "include": [],
        "exclude": ["ab" * 16],
        "member_count": 1,
        "members": ["cd" * 16],
        "sources": {"definition": ["cd" * 16], "included": [], "excluded": ["ab" * 16]},
        "health_defined": [],
        "unknown_pins": [],
        "archived_at": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "last_resolution": None,
        "membership_changes": 0,
    }
    row.update(overrides)
    return row


def test_the_target_step_can_target_a_saved_group(server):
    """#111: a group is a visible choice with its live count, not a hidden CLI flag."""
    server.agents = [fake_agent("cd" * 16)]
    server.groups = [group_row()]
    app = signed_in(REMOTE_CONFIG_PAGE)
    app.run()
    app.radio(key="rc_target_mode").set_value("Saved group").run()
    assert not app.exception, app.exception
    picker = app.selectbox(key="rc_group")
    # The count is in the option label: the operator sees the blast radius before
    # committing to a target, not after a push.
    assert picker.options == ["prod-emea — 1 member(s)"]
    body = texts(app)
    # The definition is shown, because "what does this name mean?" is the whole point.
    assert "production" in "\n".join(element.value for element in app.code)
    assert "1 pinned out" in body


def test_a_health_defined_group_is_called_out_and_not_offered_as_a_push(server):
    server.agents = [fake_agent("cd" * 16)]
    server.groups = [group_row(health_defined=["healthy"])]
    app = signed_in(REMOTE_CONFIG_PAGE)
    app.run()
    app.radio(key="rc_target_mode").set_value("Saved group").run()
    assert not app.exception, app.exception
    assert "defined on **healthy**" in texts(app)


def test_the_groups_section_lists_previews_and_creates_groups(server):
    server.agents = [fake_agent("cd" * 16)]
    server.groups = [group_row()]
    app = signed_in(REMOTE_CONFIG_PAGE)
    app.run()
    app.toggle(key="rc_manage_groups").set_value(True).run()
    assert not app.exception, app.exception
    labels = [column.label for column in app.selectbox]
    assert "Preview a group" in labels
    # The builder offers keys harvested from live agents, not a free-text box.
    assert "Definition attribute" in labels
    assert "Value" in labels

    [button for button in app.button if button.label == "Preview membership"][0].click().run()
    assert not app.exception, app.exception
    assert "Dropped" in texts(app)


# ------------------------------------- empty-fleet refresh and agent-detail links


def test_the_empty_fleet_refresh_picks_up_an_agent_that_has_since_connected(server):
    """The empty state is entered before the agent exists, so nothing re-runs it.

    Streamlit only re-runs on interaction and this screen has no inputs, so without the
    button "No agents connected yet" outlived the connection until a navigation.
    """
    app = empty_agents_page(server)
    assert "No agents connected yet" in texts(app)

    server.agents = [fake_agent()]
    refresh = next(button for button in app.button if button.key == "empty_fleet_refresh")
    refresh.click().run()

    assert not app.exception, app.exception
    assert "Agent List" in texts(app), "the refresh must re-read /agents"
    assert "No agents connected yet" not in texts(app)


def test_the_agent_table_link_cannot_mismatch_the_page_path(server):
    """The table hardcoded `/Agents` while the nav's `url_path` is `agents`.

    Streamlit matches paths case-sensitively and answered the miss with a "Page not
    found" dialog — over the detail that had rendered behind it. A query-only link
    stays on whatever the current route is, so there is no path to mismatch.
    """
    server.agents = [fake_agent()]
    app = run(AGENTS_PAGE, server)
    assert not app.exception, app.exception

    html = "\n".join(str(element.value) for element in app.markdown)
    assert 'href="?agent_id=' in html
    assert "/Agents?agent_id=" not in html
    assert "/agents?agent_id=" not in html


# --------------------------------------- the agent detail shows the running config


def test_the_agent_detail_shows_the_config_an_osquery_agent_actually_runs(server):
    """The panel claimed there was no readable config for a running osquery schedule.

    osquery reports `application/json`; the display path only accepted YAML, so an
    operator opening the agent saw "No config object ... readable as YAML" instead of
    the `SELECT` statements and intervals the agent was executing.
    """
    agent = fake_agent()
    server.agents = [agent]
    body = base64.b64encode(
        json.dumps(
            {
                "schedule": {
                    "battery_percentage": {
                        "query": "SELECT percent_remaining FROM battery;",
                        "interval": 30,
                    }
                }
            }
        ).encode()
    ).decode()
    server.effective_config = json.dumps(
        {
            "configMap": {
                "configMap": {
                    "osquery.osquery.conf.json": {
                        "body": body,
                        "contentType": "application/json",
                    }
                }
            }
        }
    )

    app = AppTest.from_file(str(AGENTS_PAGE), default_timeout=30)
    app.query_params["agent_id"] = agent["id"]
    app.run()
    assert not app.exception, app.exception

    app.toggle(key=f"show_config_{agent['id'][:8]}").set_value(True).run()
    assert not app.exception, app.exception

    shown = "\n".join(str(element.value) for element in app.code)
    assert "SELECT percent_remaining FROM battery;" in shown
    assert "interval: 30" in shown
    # Not rewritten into a collector config the agent never ran.
    assert "receivers:" not in shown
