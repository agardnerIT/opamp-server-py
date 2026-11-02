"""#105: the Streamlit admin session helper — token login, roles, 401/403/429 handling.

`ui/session.py` is deliberately streamlit-free (same precedent as `ui/config_view.py`
from #68), so all of this runs without the UI dependency stack. Every assertion here
is about a rule the UI must not get wrong, because each one was a hole:

* the password leaves no trace — not in the session object, not in a `repr`, not in a
  log line (`AdminSession.__repr__` redacts the token too);
* the **role ladder agrees with the server** — a UI that quietly disagrees about who
  may approve is worse than no gating at all;
* **401 means "sign in again" and 403 does not** — conflating them logs a user out for
  clicking a button their role does not allow;
* **429 carries the retry delay** instead of an endless "invalid password" loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server import auth as server_auth
from ui import session as ui_session


class FakeResponse:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, status_code, payload=None, headers=None, json_raises=False):
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises
        self.headers = headers or {}

    def json(self):
        if self._json_raises or self._payload is None:
            raise ValueError("no json body")
        return self._payload


def poster(response=None, exc=None):
    calls = []

    def _post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        if exc is not None:
            raise exc
        return response

    _post.calls = calls
    return _post


LOGIN_OK = {
    "token": "opamp_admin_secrettoken",
    "token_type": "bearer",
    "expires_at": "2030-01-01T00:00:00+00:00",
    "idle_timeout_seconds": 900,
    "ttl_seconds": 3600,
    "principal": {"name": "alice", "role": "approver"},
}


# ------------------------------------------------------------------ role parity


def test_role_ladder_matches_the_server():
    """The UI mirrors `server.auth` on purpose (importing it would touch SQLite)."""
    assert ui_session.ROLE_LEVELS == server_auth.ROLE_LEVELS
    assert ui_session.ROLES == server_auth.ROLES


@pytest.mark.parametrize(
    "have,required,expected",
    [
        ("viewer", "viewer", True),
        ("viewer", "operator", False),
        ("operator", "viewer", True),
        ("operator", "approver", False),
        ("approver", "operator", True),
        ("approver", "admin", False),
        ("admin", "approver", True),
        ("admin", "admin", True),
        ("", "viewer", False),
        ("superuser", "viewer", False),
        ("operator", "nonexistent", False),
    ],
)
def test_role_satisfies_is_ordered(have, required, expected):
    assert ui_session.role_satisfies(have, required) is expected


def test_role_satisfies_agrees_with_server():
    for have in ui_session.ROLES:
        for required in ui_session.ROLES:
            assert ui_session.role_satisfies(have, required) == server_auth.role_satisfies(
                have, required
            ), (have, required)


def test_normalize_role_rejects_unknown_roles():
    assert ui_session.normalize_role(" ADMIN ") == "admin"
    with pytest.raises(ValueError):
        ui_session.normalize_role("root")


# --------------------------------------------------------------------- headers


def test_session_headers_are_bearer_only():
    assert ui_session.session_headers("abc123") == {"Authorization": "Bearer abc123"}


def test_session_headers_refuse_an_empty_token():
    with pytest.raises(ValueError):
        ui_session.session_headers("   ")


# --------------------------------------------------------------------- session


def test_from_login_reads_the_server_payload():
    session = ui_session.AdminSession.from_login(LOGIN_OK)
    assert session.token == "opamp_admin_secrettoken"
    assert (session.name, session.role) == ("alice", "approver")
    assert session.idle_timeout_seconds == 900
    assert session.expires_at == LOGIN_OK["expires_at"]


def test_from_login_rejects_a_payload_without_a_token():
    with pytest.raises(ValueError):
        ui_session.AdminSession.from_login({"principal": {"name": "a", "role": "viewer"}})


def test_from_login_rejects_an_unknown_role():
    payload = {"token": "t", "principal": {"name": "a", "role": "superuser"}}
    with pytest.raises(ValueError):
        ui_session.AdminSession.from_login(payload)


def test_from_login_rejects_a_non_object_payload():
    with pytest.raises(ValueError):
        ui_session.AdminSession.from_login(["not", "an", "object"])


def test_session_never_exposes_the_token_or_a_password():
    session = ui_session.AdminSession.from_login(LOGIN_OK)
    for text in (repr(session), str(session), f"{session!r}", repr([session])):
        assert "opamp_admin_secrettoken" not in text
        assert "redacted" in text
    # The dataclass has no field that could hold a password at all.
    from dataclasses import fields

    assert "password" not in {field.name for field in fields(ui_session.AdminSession)}


def test_can_and_label():
    session = ui_session.AdminSession.from_login(LOGIN_OK)
    assert session.can("operator") is True
    assert session.can("admin") is False
    assert session.label() == "alice · approver"


NOW = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_expiry_accounting():
    session = ui_session.AdminSession.from_login(
        {**LOGIN_OK, "expires_at": (NOW + timedelta(minutes=30)).isoformat()}
    )
    assert session.expires_in(NOW) == 1800
    assert session.expired(NOW) is False
    assert session.expired(NOW + timedelta(minutes=31)) is True
    assert session.expiry_label(NOW) == "expires in 30m 00s"
    assert session.expiry_label(NOW + timedelta(hours=2)) == "session expired"


def test_expiry_label_formats_hours_and_unknown_expiry():
    session = ui_session.AdminSession.from_login(
        {**LOGIN_OK, "expires_at": (NOW + timedelta(hours=2, minutes=5)).isoformat()}
    )
    assert session.expiry_label(NOW) == "expires in 2h 05m"
    assert ui_session.AdminSession.from_login({**LOGIN_OK, "expires_at": None}).expiry_label(
        NOW
    ) == "expiry unknown"


# ----------------------------------------------------------------------- login


def test_login_posts_the_credentials_once_and_returns_a_token():
    post = poster(FakeResponse(201, LOGIN_OK))
    session = ui_session.login("http://localhost:4320/", "alice", "hunter2", post=post)

    assert session.token == LOGIN_OK["token"]
    assert len(post.calls) == 1
    assert post.calls[0]["url"] == "http://localhost:4320/auth/session"
    assert post.calls[0]["json"] == {"name": "alice", "password": "hunter2"}
    # The password is an argument, never part of the resulting session.
    assert "hunter2" not in repr(session)


def test_login_accepts_200_as_well_as_201():
    assert ui_session.login("http://s", "a", "b", post=poster(FakeResponse(200, LOGIN_OK))).token


def test_login_401_is_invalid_credentials():
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login(
            "http://s", "alice", "wrong", post=poster(FakeResponse(401, {"detail": "invalid credentials"}))
        )
    assert caught.value.kind == ui_session.KIND_INVALID
    assert caught.value.status == 401
    # No hint about *which* half was wrong: the server does not leak that either.
    assert "alice" not in caught.value.message


def test_login_403_is_forbidden_and_keeps_the_server_detail():
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login(
            "http://s", "alice", "pw", post=poster(FakeResponse(403, {"detail": "principal is revoked"}))
        )
    assert caught.value.kind == ui_session.KIND_FORBIDDEN
    assert "revoked" in caught.value.message


def test_login_403_read_only_mode_is_recognised():
    detail = "admin auth is not configured: this deployment is read-only. Set ADMIN_PASSWORD"
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login("http://s", "a", "b", post=poster(FakeResponse(403, {"detail": detail})))
    assert caught.value.kind == ui_session.KIND_READ_ONLY


@pytest.mark.parametrize(
    "headers,payload,expected",
    [
        ({"Retry-After": "120"}, {}, 120),
        ({"retry-after": "45"}, {}, 45),
        ({}, {"retry_after": 90}, 90),
        ({"Retry-After": "not-a-number"}, {"retry_after": 60}, 60),
        ({"Retry-After": "garbage"}, {}, 30),
        ({}, {}, 30),
    ],
)
def test_login_429_reports_the_retry_delay(headers, payload, expected):
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login(
            "http://s", "a", "b", post=poster(FakeResponse(429, payload, headers=headers))
        )
    assert caught.value.kind == ui_session.KIND_LOCKED_OUT
    assert caught.value.retry_after == expected
    assert ui_session.format_delay(expected) in caught.value.human()


def test_login_5xx_is_unavailable():
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login("http://s", "a", "b", post=poster(FakeResponse(500, {"detail": "boom"})))
    assert caught.value.kind == ui_session.KIND_UNAVAILABLE
    assert "boom" in caught.value.message


def test_login_survives_a_non_json_error_body():
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login("http://s", "a", "b", post=poster(FakeResponse(502, json_raises=True)))
    assert caught.value.kind == ui_session.KIND_UNAVAILABLE


def test_login_success_with_an_unreadable_body_is_reported_not_crashed():
    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login("http://s", "a", "b", post=poster(FakeResponse(201, {"principal": {}})))
    assert caught.value.kind == ui_session.KIND_UNAVAILABLE


def test_login_transport_failure_is_network_not_a_stack_trace():
    import requests

    with pytest.raises(ui_session.AuthError) as caught:
        ui_session.login(
            "http://s", "a", "b", post=poster(exc=requests.ConnectionError("refused"))
        )
    assert caught.value.kind == ui_session.KIND_NETWORK
    assert "Could not reach the server" in caught.value.message


# ------------------------------------------------------------------- classify


def test_classify_success():
    for status in (200, 201, 204):
        outcome = ui_session.classify_response(status)
        assert outcome.ok is True and outcome.kind == "ok"


def test_classify_401_requires_a_new_login():
    outcome = ui_session.classify_response(401, {}, {"detail": "invalid credentials"})
    assert outcome.relogin is True
    assert outcome.kind == ui_session.KIND_INVALID


def test_classify_403_does_not_drop_the_session():
    """A role denial is not a dead token — logging the user out would be a bug."""
    outcome = ui_session.classify_response(
        403, {}, {"detail": "role 'viewer' cannot perform this action"}, required_role="approver"
    )
    assert outcome.relogin is False
    assert outcome.kind == ui_session.KIND_FORBIDDEN
    assert "viewer" in outcome.message


def test_classify_403_read_only_points_at_the_configuration():
    outcome = ui_session.classify_response(
        403, {}, {"detail": "admin auth is not configured: this deployment is read-only."}
    )
    assert outcome.kind == ui_session.KIND_READ_ONLY
    assert outcome.relogin is False
    assert "read-only" in outcome.message


def test_classify_429_carries_the_delay():
    outcome = ui_session.classify_response(429, {"Retry-After": "300"}, None)
    assert outcome.kind == ui_session.KIND_LOCKED_OUT
    assert outcome.retry_after == 300
    assert "5 minutes" in outcome.message


def test_classify_503_audit_unavailable_is_fail_closed_copy():
    outcome = ui_session.classify_response(
        503, {}, {"detail": "audit log unavailable: the log is not writable"}
    )
    assert outcome.kind == ui_session.KIND_AUDIT
    assert "audit log unavailable" in outcome.message


def test_classify_other_errors_keep_the_server_detail():
    outcome = ui_session.classify_response(422, {}, {"detail": [{"msg": "field required"}]})
    assert outcome.kind == ui_session.KIND_UNAVAILABLE
    assert outcome.message == "field required"


@pytest.mark.parametrize(
    "seconds,expected",
    [(1, "1 second"), (30, "30 seconds"), (60, "1 minute"), (90, "1m 30s"), (900, "15 minutes")],
)
def test_format_delay(seconds, expected):
    assert ui_session.format_delay(seconds) == expected


def test_retry_after_seconds_handles_http_date_and_missing_values():
    assert ui_session.retry_after_seconds({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, None) == 30
    assert ui_session.retry_after_seconds(None, {"retry_after": 0}) == 30
    assert ui_session.retry_after_seconds(None, None, default=5) == 5


# --------------------------------------------------------------------- logout


def test_logout_confirms_revocation():
    calls = []

    def _delete(url, headers=None, timeout=None):
        calls.append((url, headers))
        return FakeResponse(200, {"logged_out": True})

    assert ui_session.logout("http://s/", "tok", delete=_delete) is True
    assert calls == [("http://s/auth/session", {"Authorization": "Bearer tok"})]


def test_logout_is_best_effort():
    def _boom(url, headers=None, timeout=None):
        raise RuntimeError("unreachable")

    assert ui_session.logout("http://s", "tok", delete=_boom) is False
    assert ui_session.logout("http://s", "  ", delete=_boom) is False


# ---------------------------------------------------------- sign out everywhere


def _session(name="alice", role="admin", method="session"):
    return ui_session.AdminSession(token="t", name=name, role=role, method=method)


LISTING = {
    "sessions": [
        {"session_id": "aaaaaa111111", "principal": "alice", "principal_id": "prin_1"},
        {"session_id": "bbbbbb222222", "principal": "alice", "principal_id": "prin_1"},
        {"session_id": "cccccc333333", "principal": "bob", "principal_id": "prin_2"},
        {"session_id": "", "principal": "alice", "principal_id": "prin_1"},
        "garbage",
    ]
}


def test_sessions_of_matches_only_the_same_principal():
    ids = [record["session_id"] for record in ui_session.sessions_of(_session(), LISTING)]
    assert ids == ["aaaaaa111111", "bbbbbb222222"]


def test_sessions_of_matches_legacy_sessions_by_id_not_label():
    legacy = _session(name="admin", method="legacy")
    listing = {
        "sessions": [
            {"session_id": "dddddd444444", "principal": "admin (shared password)", "principal_id": "legacy"},
            {"session_id": "eeeeee555555", "principal": "alice", "principal_id": "prin_1"},
        ]
    }
    ids = [record["session_id"] for record in ui_session.sessions_of(legacy, listing)]
    assert ids == ["dddddd444444"]


def test_sessions_of_does_not_prefix_match_principals():
    listing = {"sessions": [{"session_id": "ffffff666666", "principal": "alice2"}]}
    assert ui_session.sessions_of(_session(name="alice"), listing) == []


def test_sessions_of_tolerates_an_empty_payload():
    assert ui_session.sessions_of(_session(), {}) == []
    assert ui_session.sessions_of(_session(), None) == []
