"""gzip in both directions (#71).

Spec §Plain HTTP Transport: the client MAY gzip the request body and MUST set
`Content-Encoding: gzip`; the server **MUST** honour the header, and **SHOULD**
compress the response when the client advertised `Accept-Encoding`.

The dangerous half is the request: gzip is a decompression-bomb vector, so the
unrolling happens *inside* the bounded read (`server/http_body.py`) rather than
before it. The order — read → cap compressed → decompress → cap decompressed →
parse — is what these tests pin, because reversing any two steps reintroduces the
bug the code exists to prevent.

Response-side behaviour is deliberately conservative: below
`GZIP_MIN_RESPONSE_BYTES` the reply stays plain, since gzipping a 40-byte
capabilities frame costs more than it saves.
"""

from __future__ import annotations

import gzip

import pytest
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server.main import GZIP_MIN_RESPONSE_BYTES, app
from server.state import AGENT_REGISTRY
from server.ws import REGISTRY as WS_REGISTRY

UID = bytes.fromhex("ef" * 16)
AGENT_ID = UID.hex()


def status_message(healthy: bool = True, description: str = "compressed-agent") -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = healthy
    attribute = msg.agent_description.identifying_attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = description
    return msg.SerializeToString()


@pytest.fixture
def client(monkeypatch):
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    yield TestClient(app)
    WS_REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


def post(client, body: bytes, **headers):
    return client.post(
        "/v1/opamp",
        content=body,
        headers={"Content-Type": "application/x-protobuf", **headers},
    )


# --- request side -----------------------------------------------------------


def test_gzipped_request_stores_exactly_the_same_state_as_a_plain_one(client):
    """The acceptance criterion that matters: no silent partial success.

    The old failure mode was worse than an error — gzip bytes parsed as protobuf
    produced a `200` with an almost-empty reply and the report was thrown away.
    Timestamps are excluded from the comparison because they are the observation
    time, not the report.
    """
    volatile = {"connected_at", "last_heartbeat", "report_interval_seconds"}

    plain = post(client, status_message(healthy=True))
    assert plain.status_code == 200
    plain_state = {
        k: v for k, v in client.get(f"/agent/{AGENT_ID}").json().items() if k not in volatile
    }

    AGENT_REGISTRY.remove(AGENT_ID)
    assert client.get(f"/agent/{AGENT_ID}").status_code == 404

    gzipped = post(
        client,
        gzip.compress(status_message(healthy=True)),
        **{"Content-Encoding": "gzip"},
    )

    assert gzipped.status_code == 200
    stored = {
        k: v for k, v in client.get(f"/agent/{AGENT_ID}").json().items() if k not in volatile
    }
    assert stored == plain_state
    # Not just "some state": the report's own fields survived the round trip.
    assert stored["healthy"] is True
    assert stored["description"]["identifyingAttributes"][0]["key"] == "service.name"


def test_x_gzip_is_accepted_as_an_alias(client):
    """`x-gzip` is the historical spelling and is still in the wild."""
    response = post(
        client,
        gzip.compress(status_message()),
        **{"Content-Encoding": "x-gzip"},
    )

    assert response.status_code == 200
    assert AGENT_REGISTRY.count == 1


def test_identity_encoding_is_a_plain_body(client):
    response = post(client, status_message(), **{"Content-Encoding": "identity"})

    assert response.status_code == 200
    assert AGENT_REGISTRY.count == 1


def test_corrupt_gzip_is_400_with_no_state_change(client):
    response = post(client, b"this is not gzip", **{"Content-Encoding": "gzip"})

    assert response.status_code == 400
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(response.content)
    assert reply.error_response.type == opamp_pb2.ServerErrorResponseType_BadRequest
    assert AGENT_REGISTRY.count == 0


def test_truncated_gzip_is_400(client):
    payload = gzip.compress(status_message())[:-6]

    response = post(client, payload, **{"Content-Encoding": "gzip"})

    assert response.status_code == 400
    assert AGENT_REGISTRY.count == 0


def test_gzip_bomb_is_refused_on_the_decompressed_size(client, monkeypatch):
    """A few hundred compressed bytes that expand past the limit → 413, no OOM."""
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "4096")
    bomb = gzip.compress(b"\x00" * (64 * 1024 * 1024))

    response = post(client, bomb, **{"Content-Encoding": "gzip"})

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


def test_gzip_does_not_raise_the_effective_limit(client, monkeypatch):
    """The limit applies after decompression: a small gzip body can still be too big.

    This is the case a naive implementation gets wrong by checking only the bytes
    received (which comfortably fit).
    """
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "512")
    big = status_message(description="x" * 2000)
    assert len(gzip.compress(big)) < 512  # the wire bytes fit, the message does not

    response = post(client, gzip.compress(big), **{"Content-Encoding": "gzip"})

    assert response.status_code == 413


# --- response side ----------------------------------------------------------


def test_small_opamp_reply_is_not_compressed(client):
    """A capabilities-sized reply is not worth gzipping, so it stays plain."""
    response = post(client, status_message(), **{"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "content-encoding" not in {k.lower() for k in response.headers}


def test_large_response_is_gzipped_when_the_client_asks(client):
    # A big effective config makes `GET /agent/{id}` comfortably exceed the floor.
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.effective_config.config_map.config_map["collector.yaml"].body = (
        b"receivers:\n  otlp:\n    protocols:\n      grpc:\n" + b"# padding\n" * 400
    )
    assert post(client, msg.SerializeToString()).status_code == 200

    gzipped = client.get(f"/agent/{AGENT_ID}", headers={"Accept-Encoding": "gzip"})
    plain = client.get(f"/agent/{AGENT_ID}", headers={"Accept-Encoding": "identity"})

    assert len(plain.content) > GZIP_MIN_RESPONSE_BYTES
    assert gzipped.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in {k.lower() for k in plain.headers}
    # httpx decodes transparently, so identical content proves the compression is
    # lossless rather than that the body was mangled.
    assert gzipped.content == plain.content


def test_response_advertises_vary_accept_encoding(client):
    """Without `Vary`, a cache could hand gzip to a client that cannot read it."""
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.effective_config.config_map.config_map["c.yaml"].body = b"x" * 4000
    post(client, msg.SerializeToString())

    response = client.get(f"/agent/{AGENT_ID}", headers={"Accept-Encoding": "gzip"})

    assert "accept-encoding" in response.headers["vary"].lower()


def test_no_accept_encoding_means_a_plain_response(client):
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.effective_config.config_map.config_map["c.yaml"].body = b"x" * 4000
    post(client, msg.SerializeToString())

    response = client.get(f"/agent/{AGENT_ID}", headers={"Accept-Encoding": ""})

    assert "content-encoding" not in {k.lower() for k in response.headers}


def test_prometheus_endpoint_still_renders_with_compression_negotiated(client):
    """`GET /metrics` must keep working; compression is transparent to the scraper."""
    gzipped = client.get("/metrics", headers={"Accept-Encoding": "gzip"})
    plain = client.get("/metrics", headers={"Accept-Encoding": "identity"})

    assert gzipped.status_code == 200
    assert plain.status_code == 200
    assert gzipped.text == plain.text
    assert "opamp_connected_agents" in plain.text


def test_otlp_metrics_ingestion_is_unaffected(client):
    """`POST /v1/metrics` carries OTLP, not OpAMP: it keeps its own limit and path."""
    response = client.post(
        "/v1/metrics", json={"resourceMetrics": []}, headers={"Accept-Encoding": "gzip"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "success"}
