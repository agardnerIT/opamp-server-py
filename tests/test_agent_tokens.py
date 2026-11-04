"""Fleet agent tokens: expiry, capacity and the mandatory default (#126).

#94's model is *per-agent*: a credential that **is** one agent's identity, minted only
after that agent has registered. It is correct and it cannot be a default, because a
fresh install has no uid to bind to and therefore cannot onboard anything. #126 keeps it
as the **bound** token and adds the **fleet** token: an admin mints it before the fleet
exists, it authorises up to `max_agents` agents, and it expires.

The owner's decision (issue #125) is what these tests encode:

- a token is minted by an admin and pasted into the agent's config; the server refuses a
  missing or wrong one;
- tokens have a validity period (1/7/30 days or never);
- a token has a cap, and agent N+1 is refused with an error to the client **and** a line
  in the server log, so a leaked token cannot consume the server;
- tokenless connections are not allowed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from server import admin_auth, agent_auth, posture
from server.state import AGENT_REGISTRY, SQLiteCredentialStore, utcnow
from server.ws import REGISTRY
from tests.support import ADMIN, admin_headers, basic, bearer

UID_A = bytes.fromhex("aa" * 16)
UID_B = bytes.fromhex("bb" * 16)
UID_C = bytes.fromhex("cc" * 16)


def agent_message(uid: bytes) -> bytes:
    from proto import opamp_pb2

    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = True
    return msg.SerializeToString()


def post(client: TestClient, uid: bytes, token: str | None = None):
    headers = bearer(token) if token else {}
    return client.post("/v1/opamp", content=agent_message(uid), headers=headers)


def mint_fleet(client: TestClient, **body) -> dict:
    response = client.post("/agent-tokens", headers=admin_headers(), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def count_failures(reason: str) -> float:
    return agent_auth.PROM_AGENT_AUTH_FAILURES.labels(reason=reason)._value.get()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Token mode, a throwaway credential DB, an admin credential, no leftovers."""
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.delenv("OPAMP_ALLOW_UNAUTHENTICATED_AGENTS", raising=False)
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    agent_auth.forget_counted()
    REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    agent_auth.forget_counted()
    REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)


# ------------------------------------------------------------------ the default


class TestMandatoryByDefault:
    """Unset means `token`: the open endpoint has to be a decision, not an omission."""

    def test_an_unset_mode_requires_a_token(self, client, monkeypatch):
        monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
        monkeypatch.delenv("OPAMP_ALLOW_UNAUTHENTICATED_AGENTS", raising=False)
        assert posture.agent_auth_mode() == "token"
        assert post(client, UID_A).status_code == 401

    def test_the_override_restores_the_old_behaviour(self, client, monkeypatch):
        monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
        monkeypatch.setenv("OPAMP_ALLOW_UNAUTHENTICATED_AGENTS", "true")
        assert posture.agent_auth_mode() == "off"
        assert post(client, UID_A).status_code == 200
        # ...and it is reported, so no surface can pretend the endpoint is closed.
        assert client.get("/health").json()["config_push"]["agent_auth_override"] is True

    def test_an_explicit_off_is_honoured_and_reported(self, client, monkeypatch):
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "off")
        assert posture.agent_auth_mode() == "off"
        assert post(client, UID_A).status_code == 200
        health = client.get("/health").json()["config_push"]
        assert health["agent_auth"] == "off"
        assert health["agent_auth_override"] is False

    def test_a_typo_does_not_open_the_endpoint(self, client, monkeypatch):
        """`OPAMP_AGENT_AUTH=tokn` used to mean "off". It now means "on, and I warned"."""
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "tokn")
        assert posture.agent_auth_mode() == "token"
        assert post(client, UID_A).status_code == 401


# ---------------------------------------------------------------- fleet tokens


class TestFleetTokens:
    def test_one_token_onboards_a_fleet(self, client):
        """The point of #126: nothing has to register before the token is minted."""
        minted = mint_fleet(client, label="prod", max_agents=15)
        assert post(client, UID_A, minted["token"]).status_code == 200
        assert post(client, UID_B, minted["token"]).status_code == 200

        listed = client.get("/agent-tokens", headers=admin_headers()).json()["tokens"]
        assert len(listed) == 1
        assert listed[0]["token_id"] == minted["token_id"]
        assert listed[0]["agents_used"] == 2
        assert listed[0]["max_agents"] == 15
        assert listed[0]["bound"] is False
        assert listed[0]["active"] is True

    def test_the_uid_in_the_body_is_the_identity(self, client):
        minted = mint_fleet(client)
        post(client, UID_A, minted["token"])
        agent = AGENT_REGISTRY.get(UID_A.hex())
        assert agent is not None
        assert agent.agent_id == UID_A.hex()
        # The token is access, not identity — and the agent's own record says which
        # token admitted it, which is the first question when a token leaks.
        summary = client.get(f"/agent/{UID_A.hex()}").json()["credential"]
        assert [token["token_id"] for token in summary["tokens"]] == [minted["token_id"]]

    def test_the_cap_refuses_the_agent_over_the_limit(self, client):
        minted = mint_fleet(client, max_agents=1)
        assert post(client, UID_A, minted["token"]).status_code == 200
        before = count_failures("agent_limit")

        response = post(client, UID_B, minted["token"])
        assert response.status_code == 403
        assert count_failures("agent_limit") == before + 1
        # Refused before any state was applied: the over-cap agent was never registered,
        # and the agent that holds the slot is untouched.
        assert AGENT_REGISTRY.get(UID_B.hex()) is None
        assert AGENT_REGISTRY.get(UID_A.hex()) is not None

        # The same uid keeps working: the cap counts agents, not messages.
        assert post(client, UID_A, minted["token"]).status_code == 200

    def test_the_cap_is_counted_once_per_uid(self, client):
        minted = mint_fleet(client, max_agents=1)
        for _ in range(5):
            assert post(client, UID_A, minted["token"]).status_code == 200
        listed = client.get("/agent-tokens", headers=admin_headers()).json()["tokens"]
        assert listed[0]["agents_used"] == 1

    def test_an_expired_token_is_refused(self, client):
        minted = mint_fleet(client, expires_in_days=1)
        agent_auth.CREDENTIALS._connect().execute(
            "UPDATE agent_credentials SET expires_at=? WHERE token_id=?",
            ((utcnow() - timedelta(minutes=1)).isoformat(), minted["token_id"]),
        ).connection.commit()

        before = count_failures("expired")
        response = post(client, UID_A, minted["token"])
        assert response.status_code == 401
        assert count_failures("expired") == before + 1
        listed = client.get("/agent-tokens", headers=admin_headers()).json()["tokens"]
        assert listed[0]["expired"] is True and listed[0]["active"] is False

    def test_expiry_is_validated_rather_than_clamped(self, client):
        for bad in (0, -1, 366):
            response = client.post(
                "/agent-tokens", headers=admin_headers(), json={"expires_in_days": bad}
            )
            assert response.status_code == 422, bad
        for bad in (0, -3):
            response = client.post(
                "/agent-tokens", headers=admin_headers(), json={"max_agents": bad}
            )
            assert response.status_code == 422, bad

    def test_a_revoked_token_stops_working_immediately(self, client):
        minted = mint_fleet(client)
        assert post(client, UID_A, minted["token"]).status_code == 200

        revoked = client.delete(
            f"/agent-tokens/{minted['token_id']}", headers=admin_headers()
        )
        assert revoked.status_code == 200 and revoked.json()["revoked"] is True
        assert post(client, UID_A, minted["token"]).status_code == 401
        # Revocation is not a wipe: the agent, its state and the row all survive.
        assert AGENT_REGISTRY.get(UID_A.hex()) is not None
        assert client.delete(
            f"/agent-tokens/{minted['token_id']}", headers=admin_headers()
        ).status_code == 404

    def test_a_fleet_token_cannot_be_confused_with_a_bound_one(self, client):
        """A bound token still refuses another uid — the #94 guarantee is unchanged."""
        bound = client.post(
            f"/agents/{UID_A.hex()}/token", headers=admin_headers(), json={}
        ).json()
        assert post(client, UID_A, bound["token"]).status_code == 200
        response = post(client, UID_B, bound["token"])
        assert response.status_code == 403
        assert AGENT_REGISTRY.get(UID_B.hex()) is None


# --------------------------------------------------------------------- listing


class TestListingAndRoles:
    def test_no_token_and_no_hash_is_ever_listed(self, client):
        minted = mint_fleet(client, label="prod")
        body = client.get("/agent-tokens", headers=admin_headers()).text
        assert minted["token"] not in body
        assert agent_auth.hash_token(minted["token"]) not in body
        assert "token_hash" not in body
        # ...but the handle an operator can act on is.
        assert minted["token_id"] in body

    def test_a_viewer_may_read_but_not_mint_or_revoke(self, client):
        minted = mint_fleet(client)
        created = client.post(
            "/auth/principals",
            json={"name": "watcher", "role": "viewer", "password": "viewer-pw-123456"},
            headers=admin_headers(),
        )
        assert created.status_code == 201, created.text
        viewer = basic("watcher", "viewer-pw-123456")

        listed = client.get("/agent-tokens", headers=viewer)
        assert listed.status_code == 200
        assert listed.json()["tokens"][0]["token_id"] == minted["token_id"]
        # Minting and revoking are admin-only, and an unauthenticated read is refused
        # once a credential is configured (this is not a public listing).
        assert client.post("/agent-tokens", headers=viewer, json={}).status_code == 403
        assert client.delete(
            f"/agent-tokens/{minted['token_id']}", headers=viewer
        ).status_code == 403
        assert client.get("/agent-tokens").status_code == 401

    def test_minting_is_audited_by_handle_not_by_token(self, client):
        minted = mint_fleet(client, label="prod", max_agents=3)
        audit = client.get("/audit?limit=20", headers=admin_headers()).json()
        entries = json.dumps(audit)
        assert "agent.token.issued" in entries
        assert minted["token_id"] in entries
        assert minted["token"] not in entries


# ----------------------------------------------------------------------- store


class TestStoreUpgrade:
    def test_a_pre_126_database_gains_the_new_columns(self, tmp_path):
        """The schema change is additive: an existing fleet must not lose its history."""
        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(path)
        legacy.execute(
            """
            CREATE TABLE agent_credentials (
                token_hash TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                revoked_at TEXT,
                retire_on_use TEXT
            )
            """
        )
        legacy.execute(
            "INSERT INTO agent_credentials (token_hash, agent_id, created_at) "
            "VALUES ('deadbeef', 'aa', '2026-01-01T00:00:00+00:00')"
        )
        legacy.commit()
        legacy.close()

        store = SQLiteCredentialStore(path)
        row = store.get("deadbeef")
        assert row["expires_at"] is None and row["max_agents"] is None
        # A handle is derived from the hash, so pre-existing rows are addressable.
        assert row["token_id"] == "agtok_deadbeef"
        assert store.by_token_id("agtok_deadbeef")["agent_id"] == "aa"

    def test_the_cap_survives_a_restart(self, tmp_path):
        path = tmp_path / "creds.db"
        store = SQLiteCredentialStore(path)
        store.add("hash", label="prod", max_agents=2)
        assert store.register_agent("hash", "aa", 2) == "ok"
        assert store.register_agent("hash", "bb", 2) == "ok"
        assert store.register_agent("hash", "cc", 2) == "agent_limit"

        reopened = SQLiteCredentialStore(path)
        assert reopened.agents_for_token("hash") == ["aa", "bb"]
        assert reopened.register_agent("hash", "cc", 2) == "agent_limit"
