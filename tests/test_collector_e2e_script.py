"""The collector e2e assertions themselves (#113).

`scripts/collector_e2e.py` runs against a real collector in CI and locally, so its
checks must not be the part that breaks: a wrong *check* turns the only interop test
into noise. These tests feed it the payload shapes the server actually returned from
`otel/opentelemetry-collector-contrib:0.140.0` (captured when the workflow was written)
and pin what it accepts and what it rejects.

No network, no Docker, no server: the script is deliberately plain HTTP so that the
logic can be tested like this.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import collector_e2e as e2e  # noqa: E402


def attribute(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def detail(
    *,
    agent_name: str = "e2e-collector",
    healthy: bool = True,
    components: dict | None = None,
    used: tuple = ("receiver:otlp", "processor:batch", "exporter:debug", "extension:opamp"),
) -> dict:
    """One `GET /agent/{id}` payload in the shape the server serialises."""
    groups: dict = {}
    for name in used:
        group, component = name.split(":", 1)
        groups.setdefault(group, []).append({"id": component, "version": "0.140.0", "used": True})
    if components is not None:
        groups = components
    return {
        "id": "74f1cb4507a8407f9341f94b62277e87",
        "healthy": healthy,
        "description": {
            "identifyingAttributes": [
                attribute("service.instance.id", "74f1cb45-07a8-407f-9341-f94b62277e87"),
                attribute("service.name", "otelcol-contrib"),
                attribute("service.version", "0.140.1"),
            ],
            "nonIdentifyingAttributes": [
                attribute("agent.name", agent_name),
                attribute("environment", "ci"),
            ],
        },
        "available_components": {"components": {"receivers": {"subComponentMap": {"otlp": {}}}}},
        "components": groups,
    }


def manifest(yaml: str = None) -> dict:
    body = yaml if yaml is not None else (
        "exporters:\n"
        "  - gomod: .../exporter/debugexporter v0.140.0\n"
        "extensions:\n"
        "  - gomod: .../extension/opampextension v0.140.1\n"
        "receivers:\n"
        "  - gomod: .../receiver/otlpreceiver v0.140.0\n"
        "processors:\n"
        "  - gomod: .../processor/batchprocessor v0.140.0\n"
    )
    return {"manifest_yaml": body}


# --- attribute() ------------------------------------------------------------


def test_attribute_reads_both_groups():
    """`agent.name` is non-identifying, `service.name` identifying: both are asked for."""
    payload = detail()

    assert e2e.attribute(payload["description"], "agent.name") == "e2e-collector"
    assert e2e.attribute(payload["description"], "service.name") == "otelcol-contrib"


def test_attribute_ignores_values_it_cannot_read():
    description = {"identifyingAttributes": [{"key": "k", "value": {"arrayValue": {}}}]}

    assert e2e.attribute(description, "k") is None
    assert e2e.attribute({}, "k") is None


# --- find_agent() -----------------------------------------------------------


def test_find_agent_matches_by_agent_name():
    payload = {"agents": [detail(agent_name="other"), detail()], "count": 2}

    found = e2e.find_agent(payload, "e2e-collector")

    assert found is not None
    assert e2e.attribute(found["description"], "agent.name") == "e2e-collector"


def test_find_agent_matches_by_service_name():
    """A collector without a custom `agent.name` is still ours to find."""
    assert e2e.find_agent({"agents": [detail()]}, "otelcol-contrib") is not None


def test_find_agent_returns_none_for_an_empty_or_unknown_fleet():
    assert e2e.find_agent({"agents": [], "count": 0}, "e2e-collector") is None
    assert e2e.find_agent({"agents": [detail()]}, "someone-else") is None
    assert e2e.find_agent({}, "e2e-collector") is None


# --- check_agent() ----------------------------------------------------------


def test_a_real_collector_payload_passes():
    assert e2e.check_agent(detail(), "e2e-collector") == []


def test_wrong_agent_name_is_reported_with_both_candidates():
    problems = e2e.check_agent(detail(agent_name="impostor"), "e2e-collector")

    assert len(problems) == 1
    assert "agent name" in problems[0]
    assert "impostor" in problems[0]


def test_unhealthy_agent_is_a_failure():
    problems = e2e.check_agent(detail(healthy=False), "e2e-collector")

    assert any("health" in p for p in problems)


def test_hash_only_components_are_a_failure():
    """The #77 handshake failing is precisely what this e2e exists to catch."""
    payload = detail()
    payload["available_components"] = {"hash": "qgE="}

    problems = e2e.check_agent(payload, "e2e-collector")

    assert any("hash-only" in p for p in problems)


def test_no_used_components_is_a_failure():
    payload = detail(used=())
    payload["components"] = {
        "receiver": [{"id": "otlp", "version": "0.140.0", "used": False}],
    }

    problems = e2e.check_agent(payload, "e2e-collector")

    assert any("no component is marked as used" in p for p in problems)


def test_a_pipeline_the_server_failed_to_parse_is_a_failure():
    """Components present but none of the declared ones used = effective config unread."""
    payload = detail(used=("receiver:prometheus",))

    problems = e2e.check_agent(payload, "e2e-collector")

    assert any("expected used" in p for p in problems)


# --- check_manifest() -------------------------------------------------------


def test_a_manifest_with_the_used_components_passes():
    assert e2e.check_manifest(manifest()) == []


def test_an_empty_manifest_fails_even_with_status_200():
    assert e2e.check_manifest(manifest("")) == ["manifest: empty"]


def test_a_manifest_missing_a_component_fails():
    partial = manifest("receivers:\n  - gomod: .../receiver/otlpreceiver v0.140.0\n")

    problems = e2e.check_manifest(partial)

    assert problems and "missing" in problems[0]
    assert "batchprocessor" in problems[0]
