"""Custom capabilities and custom messages (#85).

The spec's extension mechanism, and the acceptance criteria are all about *honesty*:

* the capability list is **replaced**, never appended (the message carries the whole
  list, so appending would keep offering capabilities the agent dropped);
* a message for an unadvertised capability is a **400 that lists what the agent does
  support** — sending it anyway produces a request the agent may ignore, which reads
  as "the feature is broken";
* the payload is **opaque**: the server passes bytes through and never interprets
  them, because the contract belongs to the capability;
* delivery is **exactly once**, and the config offer waits one poll so a reply carries
  one server-initiated action (the #84 attribution rule).
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server import admin_auth, config_control, crypto, custom_messages
from server.state import AGENT_REGISTRY
from tests.conformance.harness import handle_message as reply
from tests.support import ADMIN, admin_headers

UID = bytes.fromhex("ab" * 16)
AGENT_ID = "ab" * 16
PAUSE = "io.opentelemetry.pause"


def agent_message(
    capabilities=(PAUSE,),
    custom_message=None,
    uid: bytes = UID,
    accepts_remote_config: bool = False,
) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    if accepts_remote_config:
        msg.capabilities |= (
            opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
        )
        msg.capabilities |= (
            opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsEffectiveConfig
        )
    msg.health.healthy = True
    if capabilities is not None:
        msg.custom_capabilities.capabilities.extend(list(capabilities))
    if custom_message is not None:
        msg.custom_message.capability = custom_message[0]
        msg.custom_message.type = custom_message[1]
        msg.custom_message.data = custom_message[2]
    return msg.SerializeToString()


def post(client: TestClient, body: dict, agent_id: str = AGENT_ID):
    return client.post(
        f"/agent/{agent_id}/custom-message", json=body, headers=admin_headers()
    )


@pytest.fixture(autouse=True)
def secure_posture(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    # Agent auth is off here (the custom-message surface does not need it), and one
    # test needs a real config offer, so the documented development override applies.
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "off")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    monkeypatch.delenv("OPAMP_CUSTOM_MESSAGES_ENABLED", raising=False)
    crypto.reset_key_cache()
    config_control.reset_drift_alerts()
    yield
    config_control.reset_drift_alerts()
    crypto.reset_key_cache()


@pytest.fixture
def registered():
    reply(agent_message())
    return AGENT_ID


# ------------------------------------------------------------------ capabilities


def test_custom_capabilities_are_stored_and_visible(client, registered):
    agent = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert agent["custom_capabilities"] == [PAUSE]
    assert agent["pending_custom_message"] is None
    assert agent["custom_message_delivered_at"] is None


def test_the_capability_list_is_replaced_not_appended(client, registered):
    """The spec's message carries the whole list, so a report is the new truth."""
    reply(agent_message(capabilities=(PAUSE, "io.opentelemetry.debug")))
    assert AGENT_REGISTRY.get(AGENT_ID).custom_capabilities == [
        PAUSE,
        "io.opentelemetry.debug",
    ]

    reply(agent_message(capabilities=("io.opentelemetry.debug",)))
    assert AGENT_REGISTRY.get(AGENT_ID).custom_capabilities == [
        "io.opentelemetry.debug"
    ], "a dropped capability must not survive the next report"


def test_an_empty_capability_list_clears_the_state(client, registered):
    reply(agent_message(capabilities=()))
    assert AGENT_REGISTRY.get(AGENT_ID).custom_capabilities == []
    agent = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert agent["custom_capabilities"] == []


def test_duplicate_and_blank_capabilities_are_normalised():
    assert custom_messages._normalise_capabilities(
        {"capabilities": ["a.b", "a.b", "  ", "c.d"]}
    ) == ["a.b", "c.d"]


# ---------------------------------------------------------------------- sending


def test_a_supported_custom_message_is_delivered_exactly_once(client, registered):
    payload = b'{"reason": "noisy"}'
    queued = post(
        client,
        {
            "capability": PAUSE,
            "type": "pause",
            "data": base64.b64encode(payload).decode(),
        },
    )
    assert queued.status_code == 200, queued.text
    body = queued.json()
    assert body["queued"] is True
    assert body["custom_message"]["capability"] == PAUSE
    assert body["supported_capabilities"] == [PAUSE]
    assert body["delivery"]["state"] == "queued"

    first = reply(agent_message())
    assert first.HasField("custom_message")
    assert first.custom_message.capability == PAUSE
    assert first.custom_message.type == "pause"
    assert first.custom_message.data == payload, (
        "the payload is opaque: it must arrive byte-for-byte"
    )

    # Exactly once: the next three polls carry nothing.
    for _ in range(3):
        assert not reply(agent_message()).HasField("custom_message")

    agent = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert agent["pending_custom_message"] is None
    assert agent["custom_message_delivered_at"] is not None


def test_an_empty_payload_is_valid(client, registered):
    """Some capabilities are pure commands: the type *is* the message."""
    assert post(client, {"capability": PAUSE, "type": "resume"}).status_code == 200
    delivered = reply(agent_message())
    assert delivered.custom_message.type == "resume"
    assert delivered.custom_message.data == b""


def test_the_config_offer_waits_one_poll_for_a_custom_message(client, registered, monkeypatch):
    """One server-initiated action per reply, so attribution stays unambiguous (#84)."""
    from server import remote_config
    from server.state import SQLiteDesiredConfigStore

    remote_config.REGISTRY.push(
        AGENT_ID,
        {
            "": {
                "body": (
                    "extensions:\n  opamp:\n    server:\n      ws:\n"
                    "        endpoint: wss://opamp.example.com/v1/opamp\n"
                    "service:\n  extensions: [opamp]\n  pipelines:\n    traces:\n"
                    "      receivers: [otlp]\n      exporters: [debug]\n"
                ),
                "content_type": "text/yaml",
            }
        },
        "tester",
        status=SQLiteDesiredConfigStore.STATUS_ACTIVE,
    )
    post(client, {"capability": PAUSE, "type": "pause"})

    with_custom = reply(agent_message(accepts_remote_config=True))
    assert with_custom.HasField("custom_message")
    assert not with_custom.HasField("remote_config"), (
        "a reply carries one server-initiated action at a time"
    )

    next_poll = reply(agent_message(accepts_remote_config=True))
    assert next_poll.HasField("remote_config")
    assert not next_poll.HasField("custom_message")


def test_an_unadvertised_capability_is_a_400_listing_what_is_supported(client, registered):
    refused = post(
        client, {"capability": "io.example.not-implemented", "type": "do"}
    )
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert PAUSE in detail["supported_capabilities"]
    assert "does not advertise" in detail["message"]

    # And nothing was queued as a side effect.
    assert not reply(agent_message()).HasField("custom_message")


def test_a_non_base64_payload_is_refused_not_sent_as_garbage(client, registered):
    refused = post(
        client, {"capability": PAUSE, "type": "pause", "data": "not base64!!"}
    )
    assert refused.status_code == 422
    assert "base64" in refused.json()["detail"]


def test_an_oversized_payload_is_refused(client, registered):
    oversized = base64.b64encode(b"x" * (custom_messages.MAX_DATA_BYTES + 1)).decode()
    refused = post(client, {"capability": PAUSE, "type": "pause", "data": oversized})
    assert refused.status_code == 413


def test_a_missing_type_is_refused_because_the_server_cannot_guess_one(client, registered):
    refused = post(client, {"capability": PAUSE, "type": "  "})
    assert refused.status_code == 422
    assert "type is required" in refused.json()["detail"]


def test_an_unknown_agent_is_a_404(client):
    refused = post(
        client, {"capability": PAUSE, "type": "pause"}, agent_id="cd" * 16
    )
    assert refused.status_code == 404


def test_the_feature_can_be_switched_off_entirely(client, registered, monkeypatch):
    monkeypatch.setenv("OPAMP_CUSTOM_MESSAGES_ENABLED", "false")
    refused = post(client, {"capability": PAUSE, "type": "pause"})
    assert refused.status_code == 404
    assert "OPAMP_CUSTOM_MESSAGES_ENABLED" in refused.json()["detail"]


def test_extra_fields_are_rejected(client, registered):
    """A mistyped field that silently does nothing is worse than a 422."""
    refused = client.post(
        f"/agent/{AGENT_ID}/custom-message",
        json={"capability": PAUSE, "type": "pause", "payload": "oops"},
        headers=admin_headers(),
    )
    assert refused.status_code == 422


# ----------------------------------------------------------------- persistence


def test_a_queued_message_survives_a_restart(client, registered, tmp_path):
    from server.state import SQLiteAgentStore

    post(client, {"capability": PAUSE, "type": "pause", "data": ""})
    AGENT_REGISTRY.persist(AGENT_ID)
    reloaded = SQLiteAgentStore(AGENT_REGISTRY._store.db_path).load_all()[AGENT_ID]
    assert reloaded.pending_custom_message["capability"] == PAUSE
    assert reloaded.pending_custom_message["type"] == "pause"
    assert reloaded.custom_capabilities == [PAUSE]


# --------------------------------------------------------------------- inbound


def test_an_inbound_custom_message_is_logged_and_not_stored(client, registered, capfd):
    """The payload is capability-defined binary; storing it is how fleet data leaks."""
    from loguru import logger

    messages = []
    sink = logger.add(messages.append, level="INFO")
    try:
        reply(agent_message(custom_message=(PAUSE, "status", b"\x00\x01")))
    finally:
        logger.remove(sink)

    assert any("io.opentelemetry.pause/status" in str(record) for record in messages)
    agent = client.get(f"/agent/{AGENT_ID}", headers=admin_headers()).json()
    assert "custom_message" not in agent
    assert not any("payload" in key for key in agent)


# --------------------------------------------------------------------- audit


def test_sending_is_audited_without_the_payload(client, registered):
    post(
        client,
        {"capability": PAUSE, "type": "pause", "data": base64.b64encode(b"secret").decode()},
    )
    entries = client.get(
        "/audit", params={"action": "agent.custom_message"}, headers=admin_headers()
    ).json()["entries"]
    assert entries, "a custom message to a live collector must be recorded"
    detail = json.loads(entries[0]["detail_json"])
    assert detail["capability"] == PAUSE
    assert detail["type"] == "pause"
    assert "secret" not in json.dumps(entries[0]), (
        "the payload belongs to the capability; the log records the fact"
    )
