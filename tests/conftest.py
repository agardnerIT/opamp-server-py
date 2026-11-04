"""Shared test isolation and the fixtures every suite may request.

Two globals are process-wide and default to the *repository's* `data/opamp.db`:
the credential/principal/session/audit stores (#94, #95, #97) and the alert config
(#50).
Without this fixture, a test run reads whatever a developer's live server left
behind (a real principal would flip `auth.configured()` and turn "read-only mode"
tests green) and writes test fixtures into that same file.

So every test gets throwaway stores, and lockout state is reset between tests
(it is in-memory by design, so it would otherwise leak across the whole session).

The WebSocket registry is reset here for the same reason: it holds live connections,
bindings and push accounting, none of which a test may inherit. Fourteen suites used
to wrap `TestClient(app)` in their own reset boilerplate; one autouse reset is the
same guarantee without fourteen copies to keep in sync.
"""

from __future__ import annotations

import copy
import os

# Before anything imports `server.main`: a developer's `.env` configures a deployment,
# not a test run. Without this, `OPAMP_CONFIG_PUSH_ENABLED=true` in a local `.env`
# flips every config-push posture test red — the cause lives in a file the failing
# test never mentions, and CI (which has no `.env`) never sees it.
os.environ["OPAMP_NO_DOTENV"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import (  # noqa: E402
    agent_auth,
    alerts,
    audit,
    auth,
    crypto,
    remote_config,
    rollout,
    updates,
)
from server.main import app  # noqa: E402
from server.state import (
    AGENT_REGISTRY,
    COLLECTOR_GROUPS,
    SQLiteAgentStore,
    SQLiteAuditStore,
    SQLiteCredentialStore,
    SQLiteDesiredConfigStore,
    SQLiteMetricsStore,
    SQLitePrincipalStore,
    SQLiteRolloutStore,
    SQLiteSessionStore,
)
from server.ws import REGISTRY as WS_REGISTRY

DEFAULT_ALERT_CONFIG = copy.deepcopy(alerts.ALERT_CONFIG)


@pytest.fixture
def client():
    """The shared app object, in-process (no lifespan, so the test's env is what counts).

    Suites that need extra posture (a WebSocket-enabled server, a push-enabled one)
    add their own autouse fixture rather than another copy of this.
    """
    return TestClient(app)


@pytest.fixture
def ws_on(monkeypatch):
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")


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
    monkeypatch.setattr(audit, "STORE", SQLiteAuditStore(tmp_path / "audit.db"))
    # #81/#98: the desired-config registry is a process-wide cache over the
    # repository's database, and it holds decrypted config bodies. Never share it.
    monkeypatch.setattr(
        remote_config,
        "REGISTRY",
        remote_config.DesiredConfigRegistry(
            SQLiteDesiredConfigStore(tmp_path / "config.db")
        ),
    )
    # #101: the staged rollout store is process-wide too - a developer's live rollout
    # must not become a test's "running" rollout (or vice versa).
    monkeypatch.setattr(rollout, "STORE", SQLiteRolloutStore(tmp_path / "rollouts.db"))
    # #111: groups are a module-level store bound into default arguments (groups.py,
    # config_control.py), so the *path* moves rather than the object.
    COLLECTOR_GROUPS.reopen(tmp_path / "groups.db")
    # The agent registry and the metrics cache also default to the repository's
    # `data/opamp.db`. Swap the *store* (not the registry object — every module and
    # test already holds a reference to it) and drop whatever the import loaded, so
    # no test reads a developer's fleet or writes a test fleet into the repo (#107).
    monkeypatch.setattr(AGENT_REGISTRY, "_store", SQLiteAgentStore(tmp_path / "agents.db"))
    AGENT_REGISTRY._agents.clear()
    AGENT_REGISTRY._persisted_at.clear()
    AGENT_REGISTRY._full_state_requested_at.clear()
    WS_REGISTRY.reset()
    from server import main as server_main

    monkeypatch.setattr(
        server_main, "_METRICS_STORE", SQLiteMetricsStore(tmp_path / "metrics.db")
    )
    server_main.AGENT_METRICS.clear()
    # Decoded encryption keys are cached against the raw env value; a test that sets
    # a key must not leak it into the next one.
    crypto.reset_key_cache()
    monkeypatch.setattr(
        alerts, "ALERT_CONFIG", copy.deepcopy(DEFAULT_ALERT_CONFIG)
    )
    # #46: the update loop must never reach GitHub from a test. Individual update
    # tests opt back in with their own fixture and a fake client.
    monkeypatch.setenv("UPDATE_CHECK", "off")
    # #126: agent auth is now `token` unless the server is told otherwise. Most suites
    # here are protocol tests that post AgentToServer messages without a credential, so
    # the baseline is the explicit override rather than an implicit default; the auth
    # suites set OPAMP_AGENT_AUTH themselves and keep testing the real gate.
    monkeypatch.setenv("OPAMP_ALLOW_UNAUTHENTICATED_AGENTS", "true")
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.setattr(updates, "STATE_PATH", tmp_path / "update-check.json")
    monkeypatch.setattr(updates, "SETTINGS_PATH", tmp_path / "update-settings.json")
    auth.reset_lockouts()
    audit.reset_circuit_breaker()
    audit.reset_prune_timer()
    yield
    AGENT_REGISTRY._agents.clear()
    AGENT_REGISTRY._persisted_at.clear()
    AGENT_REGISTRY._full_state_requested_at.clear()
    WS_REGISTRY.reset()
    server_main.AGENT_METRICS.clear()
    auth.reset_lockouts()
    audit.reset_circuit_breaker()
    crypto.reset_key_cache()
