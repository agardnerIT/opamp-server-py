"""End-to-end config push: API → wire → agent (#81) with redaction (#98).

The acceptance criteria these tests exist for:

- push config → the agent's next reply carries `remote_config` with our hash;
- push again with no change → no second `remote_config` field;
- the agent reports `FAILED` → surfaced in the API, no retry loop;
- withdraw → an empty config map, delivered once;
- the feature is disabled by default (404, not 403);
- secrets never render in `/agents`, `/agent/{id}`, `/agent/{id}/config`, the
  reports or the audit log, and only an audited `reveal` shows them.
"""

from __future__ import annotations

import base64
import json

import pytest

from proto import opamp_pb2
from server import admin_auth, agent_auth, audit, crypto, remote_config
from tests.conformance.harness import handle_message as reply
from server.state import AGENT_REGISTRY, SQLiteCredentialStore
from server.ws import REGISTRY as WS_REGISTRY
from tests.support import ADMIN, admin_headers, spec_wire_hash

UID = bytes.fromhex("ab" * 16)
AGENT_ID = "ab" * 16
SECRET = "s3cr3t-config-value"

CONFIG = {
    "": {
        "body": (
            "receivers:\n"
            "  otlp:\n"
            "    protocols:\n"
            "      grpc:\n"
            "exporters:\n"
            "  otlphttp:\n"
            "    endpoint: https://backend.example.com:4318\n"
            f"    password: {SECRET}\n"
            "    headers:\n"
            f"      Authorization: Basic {SECRET}\n"
        ),
        "content_type": "text/yaml",
    }
}


def agent_message(
    uid: bytes = UID,
    status: str | None = None,
    reported_hash_b64: str | None = None,
    error: str = "",
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = (
        opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        | opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
    )
    msg.health.healthy = True
    if status:
        msg.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses, f"RemoteConfigStatuses_{status}"
        )
        if reported_hash_b64:
            msg.remote_config_status.last_remote_config_hash = base64.b64decode(
                reported_hash_b64
            )
        msg.remote_config_status.error_message = error
    return msg.SerializeToString()


@pytest.fixture(autouse=True)
def push_enabled(monkeypatch, tmp_path):
    """A fully secure posture with push on: admin auth, token agent auth, TLS, key."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(agent_auth, "CREDENTIALS", SQLiteCredentialStore(tmp_path / "creds.db"))
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    crypto.reset_key_cache()
    WS_REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    WS_REGISTRY.reset()
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    crypto.reset_key_cache()


@pytest.fixture
def registered():
    """The agent has spoken once, so the server knows it.

    Pinned to `otelcol` (#119): these tests are about the collector's structural rules,
    and after the split a profile that owns no rules is not judged by them. Which profile
    a body is judged by now follows the agent, so the fixture says which agent it is
    instead of relying on every agent being graded as a collector.
    """
    reply(agent_message())
    AGENT_REGISTRY.get(AGENT_ID).agent_type = "otelcol"
    return AGENT_ID


# ------------------------------------------------------------------ happy path


def test_push_then_deliver_then_stop(client, registered):
    pushed = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    assert pushed.status_code == 200, pushed.text
    body = pushed.json()
    assert body["revision_id"].startswith("rev-")
    assert body["config_hash"] == remote_config.compute_hash(CONFIG)

    # The agent has not seen it: the reply carries the config and our hash.
    first = reply(agent_message())
    assert first.HasField("remote_config")
    assert bytes(first.remote_config.config_hash).hex() == spec_wire_hash(first)
    assert first.remote_config.config.config_map[""].body.decode().startswith("receivers:")

    # The agent applies it and echoes our hash: no second push.
    second = reply(
        agent_message(
            status="APPLIED", reported_hash_b64=remote_config.hash_b64(body["config_hash"])
        )
    )
    assert not second.HasField("remote_config")


def test_status_is_persisted_on_the_agent(client, registered):
    pushed = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    ).json()
    reply(
        agent_message(
            status="APPLIED", reported_hash_b64=remote_config.hash_b64(pushed["config_hash"])
        )
    )
    agent = AGENT_REGISTRY.get(AGENT_ID)
    # The stored value is the proto enum name (MessageToDict), which is the
    # existing API contract; #81 only adds the hash alongside it.
    assert agent.remote_config_status.endswith("APPLIED")
    assert agent.last_remote_config_hash == pushed["config_hash"]


def test_a_failed_revision_is_surfaced_and_never_retried(client, registered):
    pushed = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    ).json()

    failed = reply(
        agent_message(
            status="FAILED",
            reported_hash_b64=remote_config.hash_b64(pushed["config_hash"]),
            error="collector refused the config",
        )
    )
    # The same reply that learned about the failure must not re-send it.
    assert not failed.HasField("remote_config")

    # ...and neither does the next one (D6: no auto-retry).
    assert not reply(agent_message()).HasField("remote_config")

    detail = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert detail["desired"]["failed_at"]
    assert "refused" in detail["desired"]["failure_detail"]
    assert detail["remote_config_status"]["status"].endswith("FAILED")

    # The failure is audited.
    rows = audit.STORE.list_entries(action="config.apply.failed", limit=5)
    assert rows, "a FAILED revision must leave an audit row"


def test_withdraw_offers_an_empty_map_once(client, registered):
    pushed = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    ).json()
    reply(
        agent_message(
            status="APPLIED", reported_hash_b64=remote_config.hash_b64(pushed["config_hash"])
        )
    )

    withdrawn = client.delete(f"/agent/{AGENT_ID}/config", headers=admin_headers())
    assert withdrawn.status_code == 200, withdrawn.text

    offer = reply(agent_message())
    assert offer.HasField("remote_config")
    assert len(offer.remote_config.config.config_map) == 0

    done = reply(
        agent_message(
            status="APPLIED",
            reported_hash_b64=remote_config.hash_b64(withdrawn.json()["config_hash"]),
        )
    )
    assert not done.HasField("remote_config")


# ----------------------------------------------------------------------- gating


def test_push_is_404_when_config_push_is_disabled(client, registered, monkeypatch):
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED")
    response = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "config push is disabled"
    assert (
        client.delete(f"/agent/{AGENT_ID}/config", headers=admin_headers()).status_code == 404
    )
    assert client.post("/config/apply", json={"config_map": CONFIG}, headers=admin_headers()).status_code == 404


def test_push_requires_the_approver_role(client, registered, monkeypatch):
    # A principal with only `operator` may not push: approval is a separate duty.
    from server import auth

    auth.PRINCIPALS.create(
        "p-1", "op", "operator", auth.hash_password("pw-operator-123")
    )
    response = client.put(
        f"/agent/{AGENT_ID}/config",
        json={"config_map": CONFIG},
        auth=("op", "pw-operator-123"),
    )
    assert response.status_code == 403


def test_push_requires_a_credential(client, registered, monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    assert client.put(f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}).status_code == 401


def test_push_to_an_unknown_agent_is_404(client):
    response = client.put(
        f"/agent/{'cd' * 16}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    assert response.status_code == 404


def test_push_to_a_malformed_agent_id_is_422(client):
    response = client.put(
        "/agent/not-hex/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    assert response.status_code == 422


def test_invalid_config_is_422_with_every_error(client, registered):
    response = client.put(
        f"/agent/{AGENT_ID}/config",
        json={"config_map": {"": {"body": "receivers: [broken", "content_type": "text/yaml"}}},
        headers=admin_headers(),
    )
    assert response.status_code == 422
    assert "not valid" in json.dumps(response.json())


def test_a_redacted_body_cannot_be_pushed(client, registered):
    response = client.put(
        f"/agent/{AGENT_ID}/config",
        json={"config_map": {"": {"body": "password: <redacted>\n", "content_type": "text/yaml"}}},
        headers=admin_headers(),
    )
    assert response.status_code == 422
    assert "redaction marker" in json.dumps(response.json())
    # Nothing was stored, so nothing can be delivered.
    assert remote_config.REGISTRY.get(AGENT_ID) is None


# ------------------------------------------------------------------- redaction


def test_effective_config_is_redacted_everywhere(client, registered):
    # The agent reports a config containing secrets.
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.effective_config.config_map.config_map[""].body = json.dumps(
        {"exporters": {"x": {"password": SECRET, "endpoint": "https://ok"}}}
    ).encode()
    reply(msg.SerializeToString())

    for path in (f"/agent/{AGENT_ID}", "/agents", f"/agent/{AGENT_ID}/report"):
        body = client.get(path, headers=admin_headers())
        assert body.status_code == 200, path
        assert SECRET not in body.text, path

    listing = client.get("/agents", headers=admin_headers()).json()
    agent = next(item for item in listing["agents"] if item["id"] == AGENT_ID)
    assert agent["effective_config_redacted"] is True
    assert agent["redacted_paths"]


def test_desired_config_is_redacted_in_the_api(client, registered):
    client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    body = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()
    assert SECRET not in json.dumps(body)
    assert body["desired"]["redacted"] is True
    assert body["desired"]["redacted_paths"]


def test_reveal_requires_approver_and_is_audited(client, registered):
    from server import auth

    client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    auth.PRINCIPALS.create("p-2", "view", "viewer", auth.hash_password("pw-viewer-1234"))
    denied = client.get(
        f"/agent/{AGENT_ID}/config?reveal=true", auth=("view", "pw-viewer-1234")
    )
    assert denied.status_code == 403

    allowed = client.get(
        f"/agent/{AGENT_ID}/config?reveal=true", headers=admin_headers()
    )
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    # The desired config body is the real one now.
    assert SECRET in json.dumps(allowed.json()["desired"])
    rows = audit.STORE.list_entries(action="config.reveal", limit=5)
    assert rows


def test_the_audit_log_never_contains_a_config_body(client, registered):
    client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    rows = audit.STORE.list_entries(limit=50)
    assert SECRET not in json.dumps(rows)


def test_the_database_never_contains_the_secret(client, registered, tmp_path):
    """#98: inspect the file, not the API."""
    client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    )
    # conftest points the registry at tmp_path/config.db; find it through the store.
    db_path = remote_config.REGISTRY._store.db_path
    raw = db_path.read_bytes()
    assert SECRET.encode() not in raw


# ---------------------------------------------------------------- fleet + validate


def test_fleet_apply_requires_confirm_and_a_selector(client, registered):
    no_confirm = client.post(
        "/config/apply", json={"config_map": CONFIG}, headers=admin_headers()
    )
    assert no_confirm.status_code == 400

    confirmed_but_no_selector = client.post(
        "/config/apply", json={"config_map": CONFIG, "confirm": True}, headers=admin_headers()
    )
    assert confirmed_but_no_selector.status_code == 400
    assert "never" in confirmed_but_no_selector.json()["detail"]


def test_fleet_apply_by_explicit_agent_list(client, registered):
    response = client.post(
        "/config/apply",
        json={"config_map": CONFIG, "agents": [AGENT_ID], "confirm": True},
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agents"] == 1 and body["succeeded"] == 1
    assert reply(agent_message()).HasField("remote_config")


def test_fleet_apply_by_filter(client, registered):
    response = client.post(
        "/config/apply",
        json={"config_map": CONFIG, "filter": {"healthy": "true"}, "confirm": True},
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["agents"] == 1


def test_fleet_apply_with_no_matches_is_404(client):
    response = client.post(
        "/config/apply",
        json={"config_map": CONFIG, "filter": {"environment": "nope"}, "confirm": True},
        headers=admin_headers(),
    )
    assert response.status_code == 404


def test_validate_is_side_effect_free(client):
    ok = client.post(
        "/config/validate", json={"config_map": CONFIG}, headers=admin_headers()
    ).json()
    assert ok["valid"] is True
    assert ok["config_hash"] == remote_config.compute_hash(CONFIG)
    assert ok["objects"] == 1
    assert ok["size_bytes"] > 0

    bad = client.post(
        "/config/validate",
        json={"config_map": {"": {"body": "a: [b", "content_type": "text/yaml"}}},
        headers=admin_headers(),
    )
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["errors"]


# -------------------------------------------------------------------- history


def test_history_lists_metadata_without_bodies(client, registered):
    first = client.put(
        f"/agent/{AGENT_ID}/config", json={"config_map": CONFIG}, headers=admin_headers()
    ).json()
    client.put(
        f"/agent/{AGENT_ID}/config",
        json={"config_map": {"": {"body": "exporters: {}\n", "content_type": "text/yaml"}}},
        headers=admin_headers(),
    )
    history = client.get(f"/agent/{AGENT_ID}/config", headers=admin_headers()).json()["history"]
    assert [row["revision_id"] for row in history][-1] == first["revision_id"]
    assert all("config_map" not in row for row in history)
