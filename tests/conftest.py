"""Shared test isolation.

Two globals are process-wide and default to the *repository's* `data/opamp.db`:
the credential/principal/session stores (#94, #95) and the alert config (#50).
Without this fixture, a test run reads whatever a developer's live server left
behind (a real principal would flip `auth.configured()` and turn "read-only mode"
tests green) and writes test fixtures into that same file.

So every test gets throwaway stores, and lockout state is reset between tests
(it is in-memory by design, so it would otherwise leak across the whole session).
"""

from __future__ import annotations

import copy

import pytest

from server import agent_auth, alerts, auth
from server.state import (
    SQLiteCredentialStore,
    SQLitePrincipalStore,
    SQLiteSessionStore,
)

DEFAULT_ALERT_CONFIG = copy.deepcopy(alerts.ALERT_CONFIG)


@pytest.fixture(autouse=True)
def isolated_global_stores(monkeypatch, tmp_path):
    """Point every SQLite-backed singleton at this test's tmp_path."""
    monkeypatch.setattr(
        auth, "PRINCIPALS", SQLitePrincipalStore(tmp_path / "auth.db")
    )
    monkeypatch.setattr(auth, "SESSIONS", SQLiteSessionStore(tmp_path / "auth.db"))
    monkeypatch.setattr(
        agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "agents.db")
    )
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "alerts.db")
    monkeypatch.setattr(
        alerts, "ALERT_CONFIG", copy.deepcopy(DEFAULT_ALERT_CONFIG)
    )
    auth.reset_lockouts()
    yield
    auth.reset_lockouts()
