"""Observability (#108): structured logs, correlation, posture metrics, OTLP traces.

The acceptance criteria these tests exist for:

* one JSON object per line, carrying `event` and (where relevant) `request_id` and
  `agent_id`, with `LOG_FORMAT=text` still readable;
* `/health` and `/metrics` probes do not produce `INFO` request lines;
* no admin password, agent token, `Authorization` header or config body survives in
  a captured log line or an exported span;
* `/metrics` exposes the posture series, and `opamp_config_push_insecure == 1`
  exactly when the #96 override is in force;
* with tracing enabled a state-changing message produces `opamp.message`, while a
  heartbeat produces neither a span nor an `INFO` line; with tracing off no
  provider is constructed;
* heartbeats stay quiet.

Threat mapping (epic #93): T2/T4 — "is the insecure posture visible to an operator"
and "who turned push on"; T6 — secrets never reach a log sink or span exporter.
"""

from __future__ import annotations

import json

import prometheus_client as prom_client
import pytest
from loguru import logger

from proto import opamp_pb2
from server import posture, tracing
from server import logging as server_logging
from server.main import _handle_agent_message
from server.state import AGENT_REGISTRY
from tests.support import ADMIN



# --------------------------------------------------------------------- fixtures


@pytest.fixture
def capture():
    """Factory capturing the rendered line exactly as the configured sinks print it.

    A factory because `configure_logging(force=True)` (used to switch format) removes
    every existing handler, including a sink added by a fixture.
    """
    sinks: list = []

    def _capture(level: str = "DEBUG") -> list:
        lines: list = []
        sinks.append(
            logger.add(
                lambda message: lines.append(str(message)),
                format="{extra[_rendered]}",
                level=level,
            )
        )
        return lines

    yield _capture
    for sink in sinks:
        try:
            logger.remove(sink)
        except ValueError:  # pragma: no cover - already removed by a reconfigure
            pass


@pytest.fixture
def rendered_logs(capture):
    return capture()


@pytest.fixture(autouse=True)
def json_logging(monkeypatch):
    """JSON is the container default; force it so assertions do not depend on TTY."""
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    server_logging.configure_logging(force=True)
    yield
    server_logging.reset_logging()
    tracing.reset_tracing()


@pytest.fixture(autouse=True)
def clean_registry():
    """The in-memory agent registry is process-wide; a persisted row would make a
    "first report" test look like a heartbeat."""
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)
    yield
    for agent in list(AGENT_REGISTRY._agents.values()):
        AGENT_REGISTRY.remove(agent.agent_id)


def _json_lines(lines: list) -> list:
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - assertion aid
            pytest.fail(f"log line is not JSON: {line!r}")
    return parsed


def _events(lines: list, name: str) -> list:
    return [entry for entry in _json_lines(lines) if entry.get("event") == name]


# ---------------------------------------------------------------- log shape


def test_json_line_carries_event_and_bound_fields(rendered_logs):
    logger.bind(event="agent.registered", agent_id="ab" * 16, transport="http").info(
        "New agent connecting"
    )

    entry = _json_lines(rendered_logs)[-1]
    assert entry["event"] == "agent.registered"
    assert entry["agent_id"] == "ab" * 16
    assert entry["transport"] == "http"
    assert entry["level"] == "INFO"
    assert entry["message"] == "New agent connecting"
    assert entry["ts"].endswith("Z") or "+" in entry["ts"]


def test_text_format_stays_readable(monkeypatch, capture):
    monkeypatch.setenv("LOG_FORMAT", "text")
    server_logging.configure_logging(force=True)
    lines = capture()

    logger.bind(event="config_push.gate_blocked").warning("config push is disabled")

    line = lines[-1]
    assert "| WARNING" in line
    assert "config_push.gate_blocked" in line
    assert "config push is disabled" in line


def test_a_message_containing_braces_does_not_break_the_formatter(rendered_logs):
    logger.bind(event="test.braces").info('config = {"a": {"b": 1}}')

    entry = _json_lines(rendered_logs)[-1]
    assert entry["message"] == 'config = {"a": {"b": 1}}'


# ------------------------------------------------------------- correlation


def test_request_id_is_generated_and_echoed(client, rendered_logs):
    response = client.get("/agents")

    request_id = response.headers.get(server_logging.REQUEST_ID_HEADER)
    assert request_id and len(request_id) == 32

    entries = _events(rendered_logs, "http.request")
    assert entries, "every request must produce one completion line"
    entry = entries[-1]
    assert entry["request_id"] == request_id
    assert entry["method"] == "GET"
    assert entry["path"] == "/agents"
    assert entry["status"] == 200
    assert entry["duration_ms"] >= 0


def test_inbound_request_id_is_reused(client, rendered_logs):
    response = client.get("/agents", headers={"X-Request-ID": "trace-me-12345678"})

    assert response.headers[server_logging.REQUEST_ID_HEADER] == "trace-me-12345678"
    assert _events(rendered_logs, "http.request")[-1]["request_id"] == "trace-me-12345678"


def test_a_hostile_request_id_is_replaced(client):
    response = client.get(
        "/agents", headers={"X-Request-ID": "not a valid id\nwith newline"}
    )

    echoed = response.headers[server_logging.REQUEST_ID_HEADER]
    assert echoed != "not a valid id\nwith newline"
    assert server_logging.valid_request_id(echoed)


@pytest.mark.parametrize("path", ["/health", "/metrics"])
def test_probes_do_not_log_at_info(client, rendered_logs, path):
    response = client.get(path)
    assert response.status_code == 200

    request_entries = _events(rendered_logs, "http.request")
    assert request_entries, "the request is still logged, just at DEBUG"
    # `/metrics` is a mounted app: Starlette answers with a 307 to `/metrics/`, so
    # both hops must be DEBUG.
    for entry in request_entries:
        assert entry["path"].rstrip("/") == path.rstrip("/")
        assert entry["level"] == "DEBUG"


def test_no_request_id_leaks_into_the_next_request(client, rendered_logs):
    client.get("/agents", headers={"X-Request-ID": "first-request-0001"})
    client.get("/agents", headers={"X-Request-ID": "second-request-002"})

    ids = [entry["request_id"] for entry in _events(rendered_logs, "http.request")]
    assert ids[-2:] == ["first-request-0001", "second-request-002"]


# ---------------------------------------------------------------- redaction


def test_log_sink_never_sees_credentials(rendered_logs):
    token = "opamp_agent_" + "a" * 40
    admin = "hunter2-admin-password"
    body = 'password: "s3cr3t-config-value"'

    logger.bind(event="test.secret", authorization=f"Bearer {token}").info(
        f"ADMIN_PASSWORD={admin} and config {body}"
    )
    # A bare `Authorization: ...` line, the shape an httpx debug log would print.
    logger.bind(event="test.secret.header").info(f"Authorization: Bearer {token}")

    rendered = "\n".join(rendered_logs)
    assert token not in rendered
    assert admin not in rendered
    assert "s3cr3t-config-value" not in rendered


def test_traceback_is_scrubbed_before_it_reaches_a_sink(rendered_logs):
    secret = "opamp_admin_traceback_leak"
    try:
        raise RuntimeError(f"password={secret}")
    except RuntimeError:
        logger.bind(event="test.traceback").exception("boom")

    rendered = "\n".join(rendered_logs)
    assert secret not in rendered
    # The scrubber replaces the value, it does not drop the traceback entirely.
    assert "RuntimeError" in rendered


# ----------------------------------------------------------------- metrics


def _sample(name: str, labels: dict | None = None) -> float | None:
    return prom_client.REGISTRY.get_sample_value(name, labels or {})


def test_posture_metrics_reflect_the_environment(monkeypatch, tmp_path):
    from server import admin_auth, crypto

    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.delenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", raising=False)
    crypto.reset_key_cache()

    values = posture.publish_metrics()
    assert values["enabled"] == 1
    assert values["insecure"] == 0
    assert _sample("opamp_config_push_enabled") == 1
    assert _sample("opamp_config_push_insecure") == 0
    for requirement, met in values["requirements"].items():
        assert _sample(
            "opamp_security_posture_requirement", {"requirement": requirement}
        ) == met


def test_insecure_override_is_visible_in_metrics(monkeypatch):
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_ALLOW_INSECURE_CONFIG_PUSH", "true")
    monkeypatch.delenv("OPAMP_AGENT_AUTH", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("TLS_TERMINATED_UPSTREAM", raising=False)

    posture.publish_metrics()

    assert _sample("opamp_config_push_insecure") == 1
    assert _sample("opamp_config_push_enabled") == 1


def test_gate_probe_increments_the_blocked_counter(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    before = _sample("opamp_config_push_gate_blocked_total") or 0

    with pytest.raises(HTTPException) as exc:
        posture.config_push_gate()

    assert exc.value.status_code == 404
    assert (_sample("opamp_config_push_gate_blocked_total") or 0) == before + 1


# ------------------------------------------------------------------ tracing


def test_tracing_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OPAMP_TRACING_ENABLED", raising=False)
    tracing.reset_tracing()

    assert tracing.init_tracing() is False
    with tracing.span("opamp.message") as span:
        assert span is None
    assert tracing.current_ids() == (None, None)


class _Exporter:
    """In-memory exporter: no collector, no network, no flakiness."""

    def __init__(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        self.inner = InMemorySpanExporter()

    def export(self, spans):
        return self.inner.export(spans)

    def shutdown(self):
        return self.inner.shutdown()

    def force_flush(self, timeout_millis=None):
        return self.inner.force_flush(timeout_millis)

    def get_finished_spans(self):
        return self.inner.get_finished_spans()


@pytest.fixture
def traced(monkeypatch):
    monkeypatch.setenv("OPAMP_TRACING_ENABLED", "true")
    exporter = _Exporter()
    assert tracing.init_tracing(exporter=exporter) is True
    yield exporter
    tracing.reset_tracing()


def _message(uid: bytes, **kwargs):
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = kwargs.get("healthy", True)
    if kwargs.get("status"):
        msg.remote_config_status.status = getattr(
            opamp_pb2.RemoteConfigStatuses,
            f"RemoteConfigStatuses_{kwargs['status']}",
        )
    return msg.SerializeToString()


def test_a_state_change_produces_one_message_span(traced):
    uid = bytes.fromhex("aa" * 16)
    _handle_agent_message(_message(uid), transport="ws")

    spans = traced.get_finished_spans()
    names = [span.name for span in spans]
    assert names.count("opamp.message") == 1
    span = spans[names.index("opamp.message")]
    assert span.attributes["agent_id"] == uid.hex()
    assert span.attributes["transport"] == "ws"
    assert span.attributes["agent.capabilities.accepts_remote_config"] is False


def test_a_heartbeat_produces_no_span_and_no_info_line(traced, rendered_logs):
    uid = bytes.fromhex("bb" * 16)
    _handle_agent_message(_message(uid))
    traced.inner.clear()
    rendered_logs.clear()

    # Same health, nothing else: a pure heartbeat.
    _handle_agent_message(_message(uid))

    assert [span.name for span in traced.get_finished_spans()] == []
    message_events = _events(rendered_logs, "opamp.message")
    assert message_events == []
    assert _events(rendered_logs, "opamp.heartbeat"), "heartbeats log at DEBUG"


def test_logs_inside_a_span_carry_the_trace_id(rendered_logs, traced):
    with tracing.span("opamp.message", agent_id="ab" * 16):
        trace_id, span_id = tracing.current_ids()
        logger.bind(event="inside.span").info("correlated")

    entry = [item for item in _json_lines(rendered_logs) if item["event"] == "inside.span"][0]
    assert entry["trace_id"] == trace_id
    assert entry["span_id"] == span_id


def test_spans_carry_no_credentials_or_config_body(traced):
    secret = "s3cr3t-config-value"
    uid = bytes.fromhex("cc" * 16)
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.remote_config_status.status = (
        opamp_pb2.RemoteConfigStatuses.RemoteConfigStatuses_FAILED
    )
    msg.remote_config_status.error_message = f"failed with password {secret}"

    _handle_agent_message(msg.SerializeToString())

    rendered = json.dumps(
        [
            {"name": span.name, "attributes": dict(span.attributes)}
            for span in traced.get_finished_spans()
        ],
        default=str,
    )
    assert secret not in rendered

