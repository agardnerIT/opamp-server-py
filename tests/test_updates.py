"""#46: the in-product update check.

Pure unit tests with a fake HTTP client and a temporary state directory. No test
in this file may reach the network; the app-level suite sets UPDATE_CHECK=off.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from server import admin_auth, updates
from server.main import app
from tests.asgi_transport import InProcessASGITransport


@pytest.fixture(autouse=True)
def isolated_update_state(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_CHECK", "on")
    monkeypatch.setenv("UPDATE_CHECK_INTERVAL_HOURS", "24")
    monkeypatch.setenv("UPDATE_CHECK_JITTER", "0")
    monkeypatch.setattr(updates, "STATE_PATH", tmp_path / "update-check.json")
    monkeypatch.setattr(updates, "SETTINGS_PATH", tmp_path / "update-settings.json")


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content if content is not None else b"{}"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _ok(tag="v0.2.0", **extra):
    return FakeResponse(200, {"tag_name": tag, "draft": False, "prerelease": False, **extra})


def test_semver_and_newer():
    assert updates.parse_semver("v1.2.3") == (1, 2, 3, "")
    assert updates.parse_semver("1.2.3-rc.1") == (1, 2, 3, "rc.1")
    assert updates.parse_semver("latest") is None
    assert updates.is_newer("v0.2.0", "0.1.0") is True
    assert updates.is_newer("v0.1.0", "0.1.0") is False
    assert updates.is_newer("v0.0.5-test", "0.1.0") is False
    assert updates.is_newer("v0.2.0-rc.1", "0.2.0") is False
    assert updates.is_newer("v0.2.0", "0.2.0-rc.1") is True


def test_check_sends_only_the_anonymous_request_and_saves_state():
    client = FakeClient(_ok())
    state = updates.check_once(client=client, now=NOW)

    assert client.calls == [
        (
            updates.GITHUB_API,
            {
                "User-Agent": updates.USER_AGENT,
                "Accept": "application/vnd.github+json",
            },
        )
    ]
    assert "0.1.0" not in str(client.calls)
    assert state["latest_tag"] == "v0.2.0"
    assert state["update_available"] is True
    assert state["checked_at"] == NOW.isoformat()
    assert updates.load_state()["latest_tag"] == "v0.2.0"


def test_older_and_prerelease_latest_do_not_offer_an_update():
    assert updates.check_once(client=FakeClient(_ok("v0.0.5-test")), now=NOW, force=True)["update_available"] is False
    assert updates.check_once(client=FakeClient(_ok("v0.2.0", prerelease=True)), now=NOW, force=True)["update_available"] is False


def test_404_means_no_stable_release_not_an_error():
    state = updates.check_once(client=FakeClient(FakeResponse(404)), now=NOW)
    assert state["latest_tag"] is None
    assert state["error"] is None


def test_failure_keeps_the_last_known_tag_and_retries_in_an_hour():
    updates.check_once(client=FakeClient(_ok()), now=NOW)
    later = NOW + timedelta(hours=2)
    state = updates.check_once(client=FakeClient(FakeResponse(500)), now=later)
    assert state["latest_tag"] == "v0.2.0"
    assert state["error"] == "http:500"
    assert state["next_check_at"] == (later + timedelta(hours=1)).isoformat()


def test_a_second_check_inside_a_minute_is_refused_unless_forced():
    updates.check_once(client=FakeClient(_ok()), now=NOW)
    with pytest.raises(updates.TooSoon):
        updates.check_once(client=FakeClient(_ok()), now=NOW + timedelta(seconds=30))
    state = updates.check_once(
        client=FakeClient(_ok("v0.3.0")),
        now=NOW + timedelta(seconds=30),
        force=True,
    )
    assert state["latest_tag"] == "v0.3.0"


def test_deployer_off_wins_and_admin_can_only_override_a_deployer_on(monkeypatch):
    assert updates.enabled() is True

    updates.set_admin_override(False)
    assert updates.admin_override() is False
    assert updates.enabled() is False
    assert updates.check_once(client=FakeClient(_ok()), now=NOW)["enabled"] is False

    updates.set_admin_override(True)
    assert updates.enabled() is True

    monkeypatch.setenv("UPDATE_CHECK", "off")
    assert updates.deployment_enabled() is False
    assert updates.enabled() is False
    assert updates.check_once(client=FakeClient(_ok()), now=NOW)["enabled"] is False

    updates.set_admin_override(None)
    assert updates.admin_override() is None


def test_disabled_check_does_not_call_github(monkeypatch):
    monkeypatch.setenv("UPDATE_CHECK", "off")
    client = FakeClient(_ok())
    state = updates.check_once(client=client, now=NOW)
    assert client.calls == []
    assert state["enabled"] is False
    assert state["source"] == "disabled"


def test_due_and_seconds_until_check_use_the_persisted_next_time():
    assert updates.due(now=NOW) is True
    assert updates.seconds_until_check(now=NOW) == 60.0

    updates.check_once(client=FakeClient(_ok()), now=NOW)
    assert updates.due(now=NOW + timedelta(hours=1)) is False
    remaining = updates.seconds_until_check(now=NOW + timedelta(hours=23))
    assert 3590 <= remaining <= 3610


def test_state_does_not_expose_a_feed_or_an_error_body():
    state = updates.state_dict()
    assert "feed" not in state
    assert "url" not in state
    assert set(state) >= {
        "current_version",
        "latest_tag",
        "update_available",
        "enabled",
        "deployment_enabled",
        "admin_override",
        "releases_url",
    }


# --------------------------------------------------------------- API surface


def _api(password="pw123456"):
    headers = {}
    if password:
        basic = base64.b64encode(f":{password}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    return httpx.Client(
        base_url="http://testserver",
        transport=InProcessASGITransport(app),
        headers=headers,
    )


def test_get_updates_is_unauthenticated_and_never_checks(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123456")

    def boom():
        raise AssertionError("GET /updates must not perform network I/O")

    monkeypatch.setattr(updates, "check_once", boom)
    with _api(password="") as client:
        response = client.get("/updates")
    assert response.status_code == 200
    assert response.json()["current_version"] == updates.__version__


def test_check_and_settings_require_admin(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123456")
    with _api(password="") as client:
        assert client.post("/updates/check").status_code in (401, 403)
        assert client.put("/updates/settings", json={"enabled": False}).status_code in (401, 403)


def test_admin_can_run_a_check(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123456")
    monkeypatch.setattr(updates, "check_once", lambda: updates.state_dict())
    with _api() as client:
        response = client.post("/updates/check")
    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_admin_can_toggle_and_clear_the_override(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123456")
    with _api() as client:
        off = client.put("/updates/settings", json={"enabled": False})
        assert off.status_code == 200
        assert off.json()["enabled"] is False
        assert off.json()["admin_override"] is False

        on = client.put("/updates/settings", json={"enabled": True})
        assert on.status_code == 200
        assert on.json()["enabled"] is True

        cleared = client.put("/updates/settings", json={"enabled": None})
        assert cleared.status_code == 200
        assert cleared.json()["admin_override"] is None


def test_check_is_refused_when_deployment_disables_it(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw123456")
    monkeypatch.setenv("UPDATE_CHECK", "off")
    with _api() as client:
        response = client.post("/updates/check")
    assert response.status_code == 409
