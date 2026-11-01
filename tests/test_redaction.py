"""Redaction: one filter, every surface (#98).

The acceptance criteria this file exists for:

* a config containing `password`, `api_key` and an `Authorization` header renders
  redacted — shape preserved — wherever it is shown;
* a captured loguru sink never contains the sentinel secret, at any level, and
  including a traceback;
* a body carrying a redaction marker is recognised as un-pushable.
"""

from __future__ import annotations

import json

import pytest
from loguru import logger

from server import redaction
from server.state import AgentState

SENTINEL = redaction.SENTINEL
SECRET = "s3cr3t-SENTINEL-value"

COLLECTOR_CONFIG = {
    "receivers": {
        "otlp": {
            "protocols": {
                "grpc": {"endpoint": "0.0.0.0:4317", "tls": {"private_key": SECRET}},
            }
        }
    },
    "exporters": {
        "otlphttp": {
            "endpoint": "https://backend.example.com:4318",
            "headers": {"Authorization": f"Basic {SECRET}"},
            "password": SECRET,
            "api_key": SECRET,
        }
    },
    "extensions": {"basicauth": {"client_auth": {"username": "u", "password": SECRET}}},
}


@pytest.fixture(autouse=True)
def _no_log_patcher():
    """Every test starts and ends without the global loguru patcher."""
    redaction.uninstall_log_filter()
    yield
    redaction.uninstall_log_filter()


# ------------------------------------------------------------------ key matching


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "db_password",
        "api_key",
        "apiKey",
        "apikey",
        "Authorization",
        "client_secret",
        "private_key",
        "access_key",
        "token",
        "agent_token_hash",
        "x-api-secret",
        "passphrase",
    ],
)
def test_secret_key_names_are_recognised(key):
    assert redaction.is_secret_key(key) is True


@pytest.mark.parametrize(
    "key",
    ["endpoint", "compression", "timeout", "username", "headers_count", "role", ""],
)
def test_innocent_key_names_are_not(key):
    assert redaction.is_secret_key(key) is False


def test_non_string_keys_are_not_secret():
    assert redaction.is_secret_key(None) is False
    assert redaction.is_secret_key(42) is False


# ------------------------------------------------------------------- redaction


def test_nested_secrets_are_replaced_at_any_depth():
    redacted, paths = redaction.redact_document(COLLECTOR_CONFIG)

    assert redacted["receivers"]["otlp"]["protocols"]["grpc"]["tls"]["private_key"] == SENTINEL
    assert redacted["exporters"]["otlphttp"]["password"] == SENTINEL
    assert redacted["exporters"]["otlphttp"]["api_key"] == SENTINEL
    assert redacted["extensions"]["basicauth"]["client_auth"]["password"] == SENTINEL
    # `Authorization` is a secret *name*, so the whole header value goes, scheme
    # and all — no point leaving "Basic" as a hint about the credential type.
    assert redacted["exporters"]["otlphttp"]["headers"]["Authorization"] == SENTINEL
    assert SECRET not in json.dumps(redacted)


def test_shape_and_non_secrets_are_preserved():
    redacted, _ = redaction.redact_document(COLLECTOR_CONFIG)
    assert redacted["exporters"]["otlphttp"]["endpoint"] == "https://backend.example.com:4318"
    assert redacted["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"] == "0.0.0.0:4317"
    assert set(redacted) == set(COLLECTOR_CONFIG)


def test_redaction_does_not_mutate_the_input():
    before = json.dumps(COLLECTOR_CONFIG, sort_keys=True)
    redaction.redact_document(COLLECTOR_CONFIG)
    assert json.dumps(COLLECTOR_CONFIG, sort_keys=True) == before


def test_paths_describe_what_was_hidden_without_showing_it():
    _, paths = redaction.redact_document(COLLECTOR_CONFIG)
    assert "receivers.otlp.protocols.grpc.tls.private_key" in paths
    assert "exporters.otlphttp.password" in paths
    assert all(SECRET not in path for path in paths)


def test_lists_are_walked():
    document = {"exporters": [{"password": SECRET}, {"endpoint": "x"}]}
    redacted, paths = redaction.redact_document(document)
    assert redacted["exporters"][0]["password"] == SENTINEL
    assert redacted["exporters"][1]["endpoint"] == "x"
    assert "exporters[0].password" in paths


def test_url_userinfo_is_masked():
    redacted, _ = redaction.redact_document(
        {"endpoint": f"postgres://user:{SECRET}@db.internal:5432/app"}
    )
    assert redacted["endpoint"] == f"postgres://user:{SENTINEL}@db.internal:5432/app"


def test_header_maps_mask_unknown_header_names_by_value_shape():
    """`X-Custom-Auth` is not a secret-looking name; its value is what betrays it."""
    redacted, _ = redaction.redact_document(
        {"headers": {"X-Custom-Auth": f"Bearer {SECRET}", "X-Trace": "abc"}}
    )
    assert redacted["headers"]["X-Custom-Auth"] == f"Bearer {SENTINEL}"
    assert redacted["headers"]["X-Trace"] == "abc"


def test_depth_bomb_is_replaced_not_recursed():
    node: dict = {"password": SECRET}
    for _ in range(redaction.MAX_DEPTH + 5):
        node = {"nested": node}
    redacted, paths = redaction.redact_document(node)
    assert SECRET not in json.dumps(redacted)
    assert paths  # something was marked


def test_redacted_envelope_carries_the_marker():
    envelope = redaction.redacted_envelope({"password": SECRET})
    assert envelope["redacted"] is True
    assert envelope["marker"] == redaction.MARKER
    assert envelope["document"]["password"] == SENTINEL
    clean = redaction.redacted_envelope({"endpoint": "x"})
    assert clean["redacted"] is False and clean["marker"] is None


# ------------------------------------------------------- un-pushable detection


def test_contains_redaction_finds_the_sentinel_and_the_marker():
    assert redaction.contains_redaction({"password": SENTINEL}) is True
    assert redaction.contains_redaction({"note": redaction.MARKER}) is True
    assert redaction.contains_redaction([{"a": {"b": SENTINEL}}]) is True
    assert redaction.contains_redaction(f'{{"a": "{SENTINEL}"}}') is True
    assert redaction.contains_redaction(SENTINEL.encode()) is True
    assert redaction.contains_redaction({"password": "real-value"}) is False
    assert redaction.contains_redaction(None) is False


def test_a_redacted_document_is_always_detected():
    """Round trip: redact → the result must never be acceptable as a push body."""
    redacted, paths = redaction.redact_document(COLLECTOR_CONFIG)
    assert paths
    assert redaction.contains_redaction(redacted) is True


# ------------------------------------------------------------- effective config


def test_redact_effective_config_keeps_it_parseable():
    raw = json.dumps(COLLECTOR_CONFIG)
    redacted, paths = redaction.redact_effective_config(raw)
    parsed = json.loads(redacted)  # the UI parses this to work out used components
    assert parsed["exporters"]["otlphttp"]["password"] == SENTINEL
    assert paths


def test_redact_effective_config_handles_empty_and_garbage():
    assert redaction.redact_effective_config(None) == (None, [])
    assert redaction.redact_effective_config("") == ("", [])
    # Not JSON: do not guess — mark it, so nothing unparsed is echoed back.
    redacted, paths = redaction.redact_effective_config("password: " + SECRET)
    assert SECRET not in redacted
    assert paths == ["$"]


def test_agent_to_dict_is_redacted_and_cached():
    state = AgentState(
        instance_uid=b"\x01" * 16,
        agent_id="01" * 16,
        effective_config=json.dumps(COLLECTOR_CONFIG),
    )
    first = state.to_dict()
    assert SECRET not in json.dumps(first)
    assert first["effective_config_redacted"] is True
    assert first["redacted_paths"]

    # Second call hits the cache (same object) — redaction is not repeated per read.
    second = state.to_dict()
    assert second["effective_config"] == first["effective_config"]
    assert state._redacted_effective_config is not None

    # A changed body invalidates it.
    state.effective_config = json.dumps({"exporters": {"x": {"password": SECRET}}})
    third = state.to_dict()
    assert third["effective_config"] != first["effective_config"]
    assert SECRET not in json.dumps(third)


def test_agent_to_dict_reports_a_config_error():
    state = AgentState(
        instance_uid=b"\x02" * 16,
        agent_id="02" * 16,
        effective_config=None,
        config_error="sealed with a key you do not have",
    )
    body = state.to_dict()
    assert body["config_error"] == "sealed with a key you do not have"


# ------------------------------------------------------------------- log lines


@pytest.fixture
def sink():
    lines: list = []
    handler = logger.add(lambda message: lines.append(str(message)), level="DEBUG", colorize=False)
    yield lines
    logger.remove(handler)


def test_log_filter_scrubs_dict_repr(sink):
    redaction.install_log_filter()
    logger.info(f"pushing config {COLLECTOR_CONFIG}")
    assert SECRET not in "".join(sink)
    assert SENTINEL in "".join(sink)


def test_log_filter_scrubs_json_and_env_shapes(sink):
    redaction.install_log_filter()
    logger.warning('body={"password": "%s"}' % SECRET)
    logger.warning(f"ADMIN_PASSWORD={SECRET}")
    logger.warning(f"Authorization: Bearer {SECRET}")
    joined = "".join(sink)
    assert SECRET not in joined


def test_log_filter_scrubs_exception_traces(sink):
    redaction.install_log_filter()
    try:
        raise RuntimeError(f"failed to apply config with password={SECRET}")
    except RuntimeError:
        logger.exception("apply failed")
    joined = "".join(sink)
    assert SECRET not in joined
    assert "Traceback" in joined  # the traceback is still there, just scrubbed


def test_log_filter_scrubs_extra_fields(sink):
    redaction.install_log_filter()
    logger.bind(password=SECRET, endpoint="https://ok.example").info("bound")
    joined = "".join(sink)
    assert SECRET not in joined


def test_uninstall_removes_the_patcher(sink):
    redaction.install_log_filter()
    redaction.uninstall_log_filter()
    logger.info(f"password={SECRET}")
    assert SECRET in "".join(sink)


def test_scrub_text_is_harmless_on_innocent_text():
    text = "agent 01 accepted config revision rev-1 with 2 objects"
    assert redaction.scrub_text(text) == text
    assert redaction.scrub_text("") == ""


def test_scrubbing_is_linear_enough_for_a_log_line():
    """Regression: an unbounded identifier class made this quadratic (28s for 20 KB).

    `scrub_text` runs on every log record and every audit detail, so a regex that
    backtracks over a long run of ordinary characters is an outage waiting for a
    noisy log line.
    """
    import time

    payload = json.dumps({"blob": "x" * 20000})
    start = time.perf_counter()
    redaction.scrub_text(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"scrub_text took {elapsed:.3f}s on 20 KB"


# ------------------------------------------------------ base64 config bodies


def test_base64_bodies_are_scrubbed():
    """OpAMP bodies are bytes; `MessageToDict` renders them base64.

    A key-name filter sees nothing in `cmVjZWl2ZXJzOi...`, so without this an
    effective config reported by an agent leaks its secrets through `/agents`.
    """
    import base64

    body = base64.b64encode(f"exporters:\n  x:\n    password: {SECRET}\n".encode()).decode()
    redacted, paths = redaction.redact_document({"": {"body": body}})
    decoded = base64.b64decode(redacted[""]["body"]).decode()
    assert SECRET not in decoded
    assert SENTINEL in decoded
    assert paths


def test_a_plain_string_that_looks_like_base64_is_left_alone():
    redacted, paths = redaction.redact_document({"note": "aGVsbG8gd29ybGQ="})
    assert redacted["note"] == "aGVsbG8gd29ybGQ="  # decodes to "hello world": no secret
    assert paths == []


def test_a_redacted_base64_body_is_detected_as_unpushable():
    import base64

    body = base64.b64encode(f"password: {SENTINEL}\n".encode()).decode()
    assert redaction.contains_redaction({"": {"body": body}}) is True
