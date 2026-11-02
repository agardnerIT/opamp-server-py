"""HTTP OpAMP message size limits (#72).

Acceptance criteria pinned here:

* over-limit body → 413, no state change;
* chunked body (no Content-Length) over the limit → 413, and reading stops;
* gzipped body whose decompressed size exceeds the limit → 413;
* a body exactly at the limit → 200;
* the limit applies after decompression, not to the compressed bytes;
* `/v1/metrics` is intentionally not subject to the OpAMP limit.
"""

from __future__ import annotations

import asyncio
import gzip

import pytest
from fastapi.testclient import TestClient

from proto import opamp_pb2
from server.http_body import BodyTooLarge, read_body
from server.main import app
from server.state import AGENT_REGISTRY
from server.ws import REGISTRY

UID = bytes.fromhex("ab" * 16)


def message(total: int | None = None) -> opamp_pb2.AgentToServer:
    """A valid AgentToServer, optionally padded to an exact serialized size."""
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = UID
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    if total is None:
        return msg
    for size in range(max(1, total - 80), total):
        msg.effective_config.config_map.config_map["pad"].body = b"x" * size
        if len(msg.SerializeToString()) == total:
            return msg
    raise AssertionError(f"could not build a {total}-byte message")


@pytest.fixture
def client():
    REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)
    yield TestClient(app)
    REGISTRY.reset()
    for agent_id in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(agent_id)


@pytest.fixture(autouse=True)
def no_push(monkeypatch):
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)


def post(client, body: bytes, **headers):
    return client.post(
        "/v1/opamp",
        content=body,
        headers={"Content-Type": "application/x-protobuf", **headers},
    )


def test_content_length_over_limit_is_413_and_mutates_nothing(client, monkeypatch):
    limit = 64
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", str(limit))

    response = post(client, message(limit + 1).SerializeToString())

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


def test_body_exactly_at_the_limit_is_accepted(client, monkeypatch):
    body = message(64).SerializeToString()
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", str(len(body)))

    response = post(client, body)

    assert response.status_code == 200
    assert AGENT_REGISTRY.get(UID.hex()) is not None


def test_one_byte_over_the_limit_is_413(client, monkeypatch):
    body = message(64).SerializeToString()
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", str(len(body) - 1))

    response = post(client, body)

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


def test_chunked_body_over_limit_is_413(client, monkeypatch):
    # An iterable content has no Content-Length, so the header check cannot help.
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "64")
    chunks = (b"x" * 32 for _ in range(100))

    response = post(client, chunks)

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


def test_gzipped_body_within_limit_is_accepted(client, monkeypatch):
    # #71 owns full Content-Encoding support; #72 already needs the bounded
    # decompression path, so a normal gzip request must work.
    body = gzip.compress(message().SerializeToString())
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "256")

    response = post(client, body, **{"Content-Encoding": "gzip"})

    assert response.status_code == 200
    assert AGENT_REGISTRY.get(UID.hex()) is not None


def test_gzip_bomb_is_413_on_the_decompressed_size(client, monkeypatch):
    limit = 64
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", str(limit))
    body = gzip.compress(b"\x00" * 4096)
    assert len(body) < limit  # the wire size is fine; the expansion is not

    response = post(client, body, **{"Content-Encoding": "gzip"})

    assert response.status_code == 413
    assert AGENT_REGISTRY.count == 0


def test_corrupt_gzip_is_bad_request(client, monkeypatch):
    from proto import opamp_pb2

    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "256")

    response = post(client, b"not actually gzip", **{"Content-Encoding": "gzip"})

    assert response.status_code == 400
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(response.content)
    assert reply.error_response.type == (
        opamp_pb2.ServerErrorResponseType_BadRequest
    )
    assert AGENT_REGISTRY.count == 0


def test_unknown_content_encoding_is_bad_request(client, monkeypatch):
    from proto import opamp_pb2

    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "256")

    response = post(client, b"whatever", **{"Content-Encoding": "br"})

    assert response.status_code == 400
    reply = opamp_pb2.ServerToAgent()
    reply.ParseFromString(response.content)
    assert "Content-Encoding" in reply.error_response.error_message


def test_metrics_endpoint_does_not_share_the_opamp_limit(client, monkeypatch):
    monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "8")

    response = client.post("/v1/metrics", json={"resourceMetrics": []})

    assert response.status_code == 200
    assert response.json() == {"status": "success"}


class _FakeRequest:
    """Minimal Request stand-in: headers + an async chunk stream."""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.consumed = 0

    async def stream(self):
        for chunk in self._chunks:
            self.consumed += len(chunk)
            yield chunk


def test_streaming_read_stops_at_the_limit_instead_of_buffering_everything():
    request = _FakeRequest([b"x" * 8] * 100)

    with pytest.raises(BodyTooLarge):
        asyncio.run(read_body(request, limit=10))

    assert request.consumed < 800  # the remaining chunks were never pulled


def test_content_length_aborts_before_reading_a_chunk():
    request = _FakeRequest([b"x" * 8] * 100, headers={"content-length": "800"})

    with pytest.raises(BodyTooLarge):
        asyncio.run(read_body(request, limit=10))

    assert request.consumed == 0
