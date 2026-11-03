"""In-product update check (#46).

One anonymous GET to GitHub's releases API, at most once per interval. The
current version is **never sent**: the comparison happens locally. No instance
ID, hostname, fleet data, cookies, `.netrc`, or proxy environment is used.

Disabling, in precedence order:

1. ``UPDATE_CHECK=off`` (the deployer; wins over everything)
2. the admin toggle, persisted in ``data/update-settings.json``
3. otherwise enabled

``GET /updates`` is a pure read and never performs network I/O. The background
task and the explicit admin ``POST /updates/check`` are the only callers of
:func:`check_once`.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from server.version import __version__

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
STATE_PATH = DATA_DIR / "update-check.json"
SETTINGS_PATH = DATA_DIR / "update-settings.json"

GITHUB_API = "https://api.github.com/repos/agardnerIT/opamp-server-py/releases/latest"
RELEASES_URL = "https://github.com/agardnerIT/opamp-server-py/releases"
USER_AGENT = "opamp-server-py-update-check"
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 64 * 1024
MIN_CHECK_INTERVAL_SECONDS = 60
FAILURE_RETRY_SECONDS = 3600
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")

_LOCK = threading.Lock()


class TooSoon(Exception):
    """A check ran less than :data:`MIN_CHECK_INTERVAL_SECONDS` ago."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"an update check ran recently; retry in {self.retry_after}s")


class _NoRelease(Exception):
    """GitHub has no published stable release yet (404)."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def deployment_enabled() -> bool:
    """The deployer's switch. ``off`` is final; there is no admin override."""
    return _env("UPDATE_CHECK", "on").lower() not in (
        "off",
        "false",
        "0",
        "no",
        "disabled",
    )


def admin_override() -> Optional[bool]:
    """The admin's persisted choice, or None when the deployer default applies."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("enabled"), bool):
        return data["enabled"]
    return None


def set_admin_override(value: Optional[bool]) -> None:
    """Persist the admin choice. ``None`` clears it back to the deployer default."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {} if value is None else {"enabled": bool(value)}
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def enabled() -> bool:
    if not deployment_enabled():
        return False
    override = admin_override()
    if override is not None:
        return override
    return True


def parse_semver(tag: str) -> Optional[tuple]:
    """``v1.2.3`` / ``1.2.3-rc.1`` -> a comparable tuple, or None."""
    match = SEMVER_RE.match((tag or "").strip())
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    return (int(major), int(minor), int(patch), prerelease or "")


def _prerelease_key(value: str) -> tuple:
    """A small SemVer prerelease ordering: numeric identifiers sort first."""
    parts = []
    for part in value.split("."):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    latest_version = parse_semver(latest)
    current_version = parse_semver(current)
    if not latest_version or not current_version:
        return False
    if latest_version[:3] != current_version[:3]:
        return latest_version[:3] > current_version[:3]
    latest_pre, current_pre = latest_version[3], current_version[3]
    if not latest_pre and current_pre:
        return True
    if latest_pre and not current_pre:
        return False
    return _prerelease_key(latest_pre) > _prerelease_key(current_pre)


def _interval_seconds() -> float:
    try:
        hours = float(_env("UPDATE_CHECK_INTERVAL_HOURS", str(DEFAULT_INTERVAL_HOURS)))
    except ValueError:
        hours = DEFAULT_INTERVAL_HOURS
    return max(1.0, hours) * 3600


def _jitter_seconds(interval: float) -> float:
    try:
        fraction = float(_env("UPDATE_CHECK_JITTER", "0.1"))
    except ValueError:
        fraction = 0.1
    fraction = min(max(fraction, 0.0), 0.5)
    return random.uniform(-fraction, fraction) * interval


def _timeout_seconds() -> float:
    try:
        value = float(_env("UPDATE_CHECK_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return min(max(value, 1.0), 30.0)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_state() -> Dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def state_dict() -> Dict[str, Any]:
    """The public shape of ``GET /updates``. Never includes the feed URL."""
    state = load_state()
    latest_tag = state.get("latest_tag")
    if not isinstance(latest_tag, str) or not latest_tag:
        latest_tag = None
    return {
        "current_version": __version__,
        "latest_version": latest_tag.lstrip("v") if latest_tag else None,
        "latest_tag": latest_tag,
        "update_available": bool(latest_tag and is_newer(latest_tag, __version__)),
        "checked_at": state.get("checked_at"),
        "next_check_at": state.get("next_check_at"),
        "enabled": enabled(),
        "deployment_enabled": deployment_enabled(),
        "admin_override": admin_override(),
        "source": "github" if enabled() else "disabled",
        "error": state.get("last_error"),
        "releases_url": RELEASES_URL,
    }


def due(now: Optional[datetime] = None) -> bool:
    if not enabled():
        return False
    state = load_state()
    next_check = _parse_iso(state.get("next_check_at"))
    return next_check is None or (now or _now()) >= next_check


def seconds_until_check(now: Optional[datetime] = None) -> float:
    """How long the loop should sleep. A disabled check wakes once a minute."""
    if not enabled():
        return 60.0
    state = load_state()
    next_check = _parse_iso(state.get("next_check_at"))
    if next_check is None:
        # First run: give the server time to finish starting up.
        return 60.0
    return max(1.0, (next_check - (now or _now())).total_seconds())


def _fetch_release(client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    owns_client = client is None
    if owns_client:
        kwargs: Dict[str, Any] = {
            "timeout": _timeout_seconds(),
            "follow_redirects": False,
            # No .netrc, no HTTP(S)_PROXY, no cookies. An operator who needs a
            # proxy sets UPDATE_HTTP_PROXY explicitly.
            "trust_env": False,
        }
        proxy = _env("UPDATE_HTTP_PROXY")
        if proxy:
            kwargs["proxy"] = proxy
        client = httpx.Client(**kwargs)

    try:
        response = client.get(
            GITHUB_API,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github+json",
            },
        )
        if response.status_code == 404:
            raise _NoRelease()
        if response.status_code != 200:
            error = RuntimeError(f"GitHub returned {response.status_code}")
            error.status_code = response.status_code  # type: ignore[attr-defined]
            raise error
        if len(response.content) > MAX_BODY_BYTES:
            raise ValueError("GitHub response was too large")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("GitHub response was not an object")
        return data
    finally:
        if owns_client:
            client.close()


def check_once(
    client: Optional[httpx.Client] = None,
    now: Optional[datetime] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Run one check, persist the result, and return the public state.

    A failure keeps the last known tag and schedules a retry in an hour. No
    response body, URL, or version is logged.
    """
    with _LOCK:
        moment = now or _now()
        if not enabled():
            return state_dict()

        state = load_state()
        if not force:
            last = _parse_iso(state.get("checked_at"))
            if last is not None:
                elapsed = (moment - last).total_seconds()
                if elapsed < MIN_CHECK_INTERVAL_SECONDS:
                    raise TooSoon(MIN_CHECK_INTERVAL_SECONDS - elapsed)

        interval = _interval_seconds()
        try:
            data = _fetch_release(client)
            tag = data.get("tag_name")
            if data.get("draft") is True or data.get("prerelease") is True:
                tag = None
            if tag is not None and (
                not isinstance(tag, str) or parse_semver(tag) is None
            ):
                raise ValueError("GitHub returned an invalid tag")
            state.update(
                {
                    "checked_at": _iso(moment),
                    "latest_tag": tag,
                    "last_error": None,
                }
            )
            state["next_check_at"] = _iso(
                moment + timedelta(seconds=interval + _jitter_seconds(interval))
            )
        except _NoRelease:
            state.update(
                {
                    "checked_at": _iso(moment),
                    "latest_tag": None,
                    "last_error": None,
                }
            )
            state["next_check_at"] = _iso(
                moment + timedelta(seconds=interval + _jitter_seconds(interval))
            )
        except Exception as exc:  # fail quiet, keep the last known tag
            status = getattr(exc, "status_code", None)
            reason = f"http:{status}" if status else type(exc).__name__
            state.update(
                {
                    "checked_at": _iso(moment),
                    "last_error": reason,
                }
            )
            state["next_check_at"] = _iso(
                moment + timedelta(seconds=FAILURE_RETRY_SECONDS)
            )
            logger.bind(
                event="updates.check_failed",
                reason=reason,
                status=status,
            ).warning("update check failed")

        save_state(state)
        return state_dict()
