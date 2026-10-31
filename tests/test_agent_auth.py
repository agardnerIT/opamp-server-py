"""Agent authentication on the OpAMP channel (#94).

Each acceptance criterion in the issue maps to a test here:

- no / bad / revoked token → 401 (HTTP) or 1008 (WS), and no `ServerToAgent`
- a valid token claiming another agent's uid → 403 / 1008, audited, victim untouched
- a second socket claiming a live uid under a different principal → refused;
  the same credential reconnects cleanly
- rotation without downtime, revocation blocks delivery but retains state
- tokens never appear in logs, `/agents`, audit lines or error bodies
- `off` still behaves exactly like before (#96/#89 suites cover that)
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from starlette.websockets import WebSocketDisconnect

from proto import opamp_pb2
from server import admin_auth, agent_auth, posture
from server.framing import decode_message, encode_message
from server.main import app
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from server.ws import CLOSE_POLICY_VIOLATION, REGISTRY

ADMIN = "admin-pw-1234567890"
VICTIM_UID = bytes.fromhex("11" * 16)
ATTACKER_UID = bytes.fromhex("22" * 16)


def agent_message(uid: bytes, healthy: bool = True, disconnect: bool = False) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = healthy
    if disconnect:
        msg.agent_disconnect.SetInParent()
    return msg.SerializeToString()


def frame(uid: bytes, **kwargs) -> bytes:
    return encode_message(agent_message(uid, **kwargs))


def admin_headers() -> dict:
    token = base64.b64encode(f":{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Token mode, a tmp credential DB, an admin credential, no leftovers."""
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "false")
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def ws_on(monkeypatch):
    monkeypatch.setenv("OPAMP_WS_ENABLED", "true")


def mint(client: TestClient, uid: bytes = VICTIM_UID, **body) -> dict:
    response = client.post(f"/agents/{uid.hex()}/token", headers=admin_headers(), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def count_failures(reason: str) -> float:
    return agent_auth.PROM_AGENT_AUTH_FAILURES.labels(reason=reason)._value.get()


# ------------------------------------------------------------------- the store


class TestCredentialStore:
    def test_only_the_hash_is_persisted(self, client):
        token = mint(client)["token"]
        rows = agent_auth.CREDENTIALS.history(VICTIM_UID.hex())
        assert len(rows) == 1
        assert rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in json.dumps(rows)
        assert token not in json.dumps(agent_auth.credential_summary(VICTIM_UID.hex()))

    def test_issue_rejects_a_non_uid(self):
        for bad in ("", "not-hex", "11" * 15, "zz" * 16):
            with pytest.raises(ValueError):
                agent_auth.issue_token(bad)

    def test_rotation_marks_the_previous_credential(self):
        first, _ = agent_auth.issue_token(VICTIM_UID.hex())
        second, _ = agent_auth.issue_token(VICTIM_UID.hex())
        rows = {row["token_hash"]: row for row in agent_auth.CREDENTIALS.history(VICTIM_UID.hex())}
        assert rows[agent_auth.hash_token(first)]["retire_on_use"] == agent_auth.hash_token(second)
        assert rows[agent_auth.hash_token(second)]["retire_on_use"] is None

    def test_revoke_agent_keeps_the_rows(self):
        agent_auth.issue_token(VICTIM_UID.hex())
        assert agent_auth.revoke_tokens(VICTIM_UID.hex()) == 1
        assert agent_auth.revoke_tokens(VICTIM_UID.hex()) == 0  # idempotent
        rows = agent_auth.CREDENTIALS.history(VICTIM_UID.hex())
        assert len(rows) == 1 and rows[0]["revoked_at"]


# ------------------------------------------------------------------------ HTTP


class TestHttpEnforcement:
    def test_no_token_gets_401_without_a_body(self, client):
        response = client.post("/v1/opamp", content=agent_message(VICTIM_UID))
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        # Not a ServerToAgent: an unauthenticated client learns nothing, not even
        # which capabilities exist.
        assert response.headers["content-type"].startswith("application/json")

    def test_unknown_token_gets_401(self, client):
        response = client.post(
            "/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer("opamp_agent_nope")
        )
        assert response.status_code == 401

    def test_malformed_authorization_gets_401(self, client):
        response = client.post(
            "/v1/opamp", content=agent_message(VICTIM_UID), headers={"Authorization": "Basic abc"}
        )
        assert response.status_code == 401

    def test_valid_token_is_accepted(self, client):
        token = mint(client)["token"]
        response = client.post(
            "/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token)
        )
        assert response.status_code == 200
        reply = opamp_pb2.ServerToAgent()
        reply.ParseFromString(response.content)
        assert reply.instance_uid == VICTIM_UID
        assert reply.capabilities & opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsStatus

    def test_token_for_another_agent_is_refused_and_the_victim_is_untouched(self, client):
        victim_token = mint(client, VICTIM_UID)["token"]
        attacker_token = mint(client, ATTACKER_UID)["token"]
        client.post("/v1/opamp", content=agent_message(VICTIM_UID, healthy=True),
                    headers=bearer(victim_token))
        before = AGENT_REGISTRY.get(VICTIM_UID.hex())
        before_state = (before.healthy, before.last_heartbeat, dict(before.description))

        response = client.post(
            "/v1/opamp",
            content=agent_message(VICTIM_UID, healthy=False),
            headers=bearer(attacker_token),
        )

        assert response.status_code == 403
        assert "instance_uid" in response.json()["detail"]
        after = AGENT_REGISTRY.get(VICTIM_UID.hex())
        assert (after.healthy, after.last_heartbeat, dict(after.description)) == before_state
        assert AGENT_REGISTRY.count == 1  # the attacker did not register a second agent

    def test_revoked_token_gets_401_and_state_is_retained(self, client):
        token = mint(client)["token"]
        client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token))
        assert client.delete(f"/agents/{VICTIM_UID.hex()}/token",
                             headers=admin_headers()).json()["revoked"] == 1

        response = client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token))
        assert response.status_code == 401
        # Revocation blocks delivery, it does not wipe the agent.
        assert AGENT_REGISTRY.get(VICTIM_UID.hex()) is not None

    def test_off_mode_is_unchanged(self, client, monkeypatch):
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "off")
        response = client.post("/v1/opamp", content=agent_message(VICTIM_UID))
        assert response.status_code == 200

    def test_binding_conflict_is_refused_over_http(self, client):
        """A uid bound to another principal may not be spoken for over HTTP either."""
        token = mint(client)["token"]
        REGISTRY.bind(1, VICTIM_UID, "someone-else")
        response = client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token))
        assert response.status_code == 403
        assert "principal" in response.json()["detail"]
        assert REGISTRY.principal_for(VICTIM_UID) == "someone-else"


# ------------------------------------------------------------------- WebSocket


class TestWebSocketEnforcement:
    def test_handshake_without_a_token_is_refused(self, client, ws_on):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/opamp"):
                pass
        assert exc.value.code == CLOSE_POLICY_VIOLATION

    def test_handshake_with_a_bad_token_is_refused(self, client, ws_on):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/opamp", headers=bearer("opamp_agent_nope")):
                pass
        assert exc.value.code == CLOSE_POLICY_VIOLATION

    def test_authenticated_socket_serves_the_agent(self, client, ws_on):
        token = mint(client)["token"]
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as ws:
            ws.send_bytes(frame(VICTIM_UID))
            reply = opamp_pb2.ServerToAgent()
            reply.ParseFromString(decode_message(ws.receive_bytes()))
            assert reply.instance_uid == VICTIM_UID

    def test_claiming_another_uid_closes_1008(self, client, ws_on):
        token = mint(client)["token"]
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/opamp", headers=bearer(token)) as ws:
                ws.send_bytes(frame(ATTACKER_UID))
                ws.receive_bytes()
        assert exc.value.code == CLOSE_POLICY_VIOLATION
        assert AGENT_REGISTRY.get(ATTACKER_UID.hex()) is None

    def test_same_credential_reconnect_moves_the_binding(self, client, ws_on):
        token = mint(client)["token"]
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as first:
            first.send_bytes(frame(VICTIM_UID))
            first.receive_bytes()
            bound_before = REGISTRY.connection_for(VICTIM_UID)
            with client.websocket_connect("/v1/opamp", headers=bearer(token)) as second:
                second.send_bytes(frame(VICTIM_UID))
                second.receive_bytes()
                assert REGISTRY.connection_for(VICTIM_UID) != bound_before
                assert REGISTRY.principal_for(VICTIM_UID) == VICTIM_UID.hex()

    def test_takeover_by_a_different_principal_closes_1008(self, client, ws_on):
        """A live binding held by another principal may not be claimed, even by
        the agent whose credential it is — the socket is refused, state untouched."""
        token = mint(client)["token"]
        REGISTRY.bind(1, VICTIM_UID, "someone-else")
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/opamp", headers=bearer(token)) as ws:
                ws.send_bytes(frame(VICTIM_UID))
                ws.receive_bytes()
        assert exc.value.code == CLOSE_POLICY_VIOLATION
        assert REGISTRY.principal_for(VICTIM_UID) == "someone-else"
        assert AGENT_REGISTRY.get(VICTIM_UID.hex()) is None

    def test_authenticated_principal_pre_empts_an_unauthenticated_binding(self, client, ws_on):
        token = mint(client)["token"]
        REGISTRY.bind(1, VICTIM_UID, None)  # an `off`-mode socket from before the cutover
        with client.websocket_connect("/v1/opamp", headers=bearer(token)) as ws:
            ws.send_bytes(frame(VICTIM_UID))
            ws.receive_bytes()
            assert REGISTRY.principal_for(VICTIM_UID) == VICTIM_UID.hex()


# --------------------------------------------------------------------- rotation


class TestRotationAndRevocation:
    def test_rotation_is_zero_downtime(self, client):
        old = mint(client, label="old")["token"]
        new = mint(client, label="new")["token"]
        assert client.post("/v1/opamp", content=agent_message(VICTIM_UID),
                           headers=bearer(old)).status_code == 200
        assert client.post("/v1/opamp", content=agent_message(VICTIM_UID),
                           headers=bearer(new)).status_code == 200
        # The new token has now been seen, so its predecessor is retired.
        assert client.post("/v1/opamp", content=agent_message(VICTIM_UID),
                           headers=bearer(old)).status_code == 401

    def test_retire_previous_cuts_over_immediately(self, client):
        old = mint(client)["token"]
        mint(client, retire_previous=True)
        assert client.post("/v1/opamp", content=agent_message(VICTIM_UID),
                           headers=bearer(old)).status_code == 401

    def test_credential_summary_tracks_the_cutover(self, client):
        token = mint(client)["token"]
        client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token))

        enrolled = client.get(f"/agent/{VICTIM_UID.hex()}").json()["credential"]
        assert enrolled["enrolled"] is True and enrolled["active"] == 1
        assert enrolled["last_seen_at"] is not None

        client.delete(f"/agents/{VICTIM_UID.hex()}/token", headers=admin_headers())
        revoked = client.get("/agents").json()["agents"][0]["credential"]
        assert revoked["active"] == 0 and revoked["revoked_at"] is not None


# -------------------------------------------------------------- secret hygiene


class TestTokensStaySecret:
    def test_token_never_reaches_logs_or_audit(self, client, ws_on, monkeypatch):
        lines = []
        handler = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            token = mint(client)["token"]
            client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token))
            client.post("/v1/opamp", content=agent_message(ATTACKER_UID), headers=bearer(token))
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/v1/opamp", headers=bearer("opamp_agent_wrong")):
                    pass
        finally:
            logger.remove(handler)

        output = "".join(lines)
        assert "agent_auth.denied" in output  # the audit seam fired
        assert "uid_mismatch" in output
        assert token not in output
        assert agent_auth.hash_token(token) not in output

    def test_token_never_appears_in_api_responses(self, client):
        token = mint(client)["token"]
        client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(token))
        for path in ("/agents", f"/agent/{VICTIM_UID.hex()}", "/health"):
            body = client.get(path).text
            assert token not in body
            assert agent_auth.hash_token(token) not in body

    def test_issue_requires_admin(self, client, monkeypatch):
        monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "")  # read-only mode = open
        assert client.post(f"/agents/{VICTIM_UID.hex()}/token").status_code == 201
        monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
        assert client.post(f"/agents/{ATTACKER_UID.hex()}/token").status_code == 401
        assert client.delete(f"/agents/{ATTACKER_UID.hex()}/token").status_code == 401

    def test_issue_validates_the_uid(self, client):
        response = client.post("/agents/not-a-uid/token", headers=admin_headers())
        assert response.status_code == 422

    def test_response_is_not_cacheable(self, client):
        response = client.post(f"/agents/{VICTIM_UID.hex()}/token", headers=admin_headers())
        assert response.headers["cache-control"] == "no-store"


# ------------------------------------------------------------ metrics & snippet


class TestMetricsAndBootstrap:
    @pytest.mark.parametrize(
        "headers, reason",
        [
            ({}, agent_auth.REASON_MISSING),
            ({"Authorization": "Basic abc"}, agent_auth.REASON_MALFORMED),
            ({"Authorization": "Bearer nope"}, agent_auth.REASON_UNKNOWN),
        ],
    )
    def test_failure_reasons_are_counted(self, client, headers, reason):
        before = count_failures(reason)
        client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=headers)
        assert count_failures(reason) == before + 1

    def test_uid_mismatch_and_binding_conflict_are_counted(self, client):
        victim = mint(client, VICTIM_UID)["token"]
        attacker = mint(client, ATTACKER_UID)["token"]
        before = count_failures(agent_auth.REASON_UID_MISMATCH)
        client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(attacker))
        assert count_failures(agent_auth.REASON_UID_MISMATCH) == before + 1

        REGISTRY.bind(1, VICTIM_UID, "someone-else")
        before = count_failures(agent_auth.REASON_BINDING_CONFLICT)
        client.post("/v1/opamp", content=agent_message(VICTIM_UID), headers=bearer(victim))
        assert count_failures(agent_auth.REASON_BINDING_CONFLICT) == before + 1

    def test_collector_snippet_is_paste_ready(self, client, monkeypatch):
        monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
        token = mint(client)["token"]
        snippet = agent_auth.collector_snippet(token)
        assert "endpoint: https://opamp.example.com/v1/opamp" in snippet
        assert f'Authorization: "Bearer {token}"' in snippet
        assert "headers:" in snippet

    def test_collector_snippet_ws_transport(self):
        snippet = agent_auth.collector_snippet("opamp_agent_x", transport="ws")
        assert "ws:" in snippet and "ws://" in snippet


# --------------------------------------------------------------------- posture


class TestPostureIntegration:
    def test_token_mode_satisfies_the_push_gate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
        monkeypatch.setattr(admin_auth, "DATA_DIR", tmp_path)
        monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
        monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
        monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
        assert posture.push_allowed() is True

        monkeypatch.setenv("OPAMP_AGENT_AUTH", "mtls")
        assert posture.push_allowed() is False
