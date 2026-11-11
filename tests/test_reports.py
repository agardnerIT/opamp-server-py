"""Tests for the report generators and report endpoints (issue #51, gap G5)."""

import pytest
from fastapi.testclient import TestClient

from server import main as server_main
from server.reports import (
    _count_outdated_collectors,
    _is_heavy,
    generate_agent_report,
    generate_heavy_collectors_report,
    generate_outdated_collectors_report,
    parse_version,
)
from server.state import AGENT_REGISTRY, AgentState


def make_agent(agent_id, components, healthy=True, agent_type="otelcol"):
    """One `GET /agents` row.

    `agent_type` is not decoration since #120: a report is scoped to the profiles that
    declare it, so a row without a profile is "unattributed" — counted as not applicable
    rather than as a collector with nothing to report.
    """
    return {
        "id": agent_id,
        "healthy": healthy,
        "agent_type": agent_type,
        "components": components,
    }


HEAVY_COMPS = {
    "receivers": [{"id": "otlp", "version": "0.149.0", "used": True},
                  {"id": "jaeger", "version": "0.149.0", "used": False},
                  {"id": "zipkin", "version": "0.149.0", "used": False}],
    "exporters": [{"id": "otlp", "version": "0.149.0", "used": True}],
}

LEAN_COMPS = {
    "receivers": [{"id": "otlp", "version": "0.149.0", "used": True}],
    "exporters": [{"id": "otlp", "version": "0.149.0", "used": True}],
}

OUTDATED_COMPS = {
    "receivers": [{"id": "otlp", "version": "0.120.0", "used": True}],
    "exporters": [{"id": "otlp", "version": "0.149.0", "used": True}],
}


class TestGenerators:
    def test_parse_version(self):
        assert parse_version("0.149.0") == (0, 149, 0)
        assert parse_version("v1.2.3") == (1, 2, 3)
        assert parse_version("garbage") in ((), (0,))
        assert (0, 120, 0) < (0, 149, 0)

    def test_is_heavy(self):
        # 2/4 unused = exactly 0.5 -> not > 0.5
        assert _is_heavy(make_agent("a", HEAVY_COMPS), 0.5) is False
        assert _is_heavy(make_agent("a", HEAVY_COMPS), 0.4) is True
        assert _is_heavy(make_agent("a", LEAN_COMPS), 0.5) is False
        assert _is_heavy(make_agent("a", {}), 0.5) is False

    def test_heavy_report_counts(self):
        data = {"agents": [make_agent("h1", HEAVY_COMPS), make_agent("lean", LEAN_COMPS)]}
        md = generate_heavy_collectors_report(data, 0.4)
        assert "Found 1 heavy collector(s)" in md
        assert "h1" in md and "lean" not in md

    def test_outdated_report_and_counts(self):
        data = {"agents": [make_agent("old", OUTDATED_COMPS), make_agent("new", LEAN_COMPS)]}
        md = generate_outdated_collectors_report(data, "0.149.0")
        assert "Found 1 outdated collector(s)" in md
        assert "0.120.0" in md
        collectors, components = _count_outdated_collectors(data["agents"], "0.149.0")
        assert (collectors, components) == (1, 1)

    def test_agent_report_sections(self):
        data = {"agents": [make_agent("a1", HEAVY_COMPS)]}
        md = generate_agent_report(data, "markdown")
        for section in ["Component Versions", "Outdated Collectors", "Heavy Collectors", "Detailed Agent List"]:
            assert section in md


@pytest.fixture()
def client_with_agents(monkeypatch):
    agent_id = "b" * 32
    state = AgentState(instance_uid=bytes(16), agent_id=agent_id)
    state.effective_config = "{}"  # empty config -> no components detected
    AGENT_REGISTRY.register(agent_id, state)
    monkeypatch.setattr(server_main, "AGENT_REGISTRY", AGENT_REGISTRY)
    yield TestClient(server_main.app), agent_id
    AGENT_REGISTRY.remove(agent_id)


class TestEndpoints:
    def test_fleet_report(self, client_with_agents):
        client, _ = client_with_agents
        resp = client.get("/reports/fleet")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_count"] == 1
        assert body["report_markdown"].startswith("# Agent Report")

    def test_heavy_collectors_report(self, client_with_agents):
        client, _ = client_with_agents
        resp = client.get("/reports/heavy-collectors")
        assert resp.status_code == 200
        body = resp.json()
        assert body["heavy_count"] == 0
        assert body["threshold"] == 0.5
        assert "Heavy Collectors Report" in body["report_markdown"]
        assert client.get("/reports/heavy-collectors?threshold=1.5").status_code == 422
        assert client.get("/reports/heavy-collectors?threshold=-0.1").status_code == 422

    def test_outdated_collectors_report(self, client_with_agents):
        client, _ = client_with_agents
        resp = client.get("/reports/outdated-collectors")
        assert resp.status_code == 200
        body = resp.json()
        assert body["collectors_count"] == 0
        assert body["components_count"] == 0
        assert body["version"] == "0.149.0"
        bad = client.get("/reports/outdated-collectors?version=not-semver")
        assert bad.status_code == 422

    def test_agent_report_404(self, client_with_agents):
        client, agent_id = client_with_agents
        assert client.get("/agent/zzzz/report").status_code == 404
        resp = client.get(f"/agent/{agent_id}/report")
        assert resp.status_code == 200
        assert resp.json()["report_markdown"].startswith("# Agent Report")


# ------------------------------------- scoped reports and "not applicable" (#120)


class TestReportsAreScopedToTheProfilesThatDeclareThem:
    """"Found 0 heavy collector(s)" was a measurement where a refusal belongs (#120).

    A FluentBit agent cannot be a heavy collector, so counting it as "not heavy" makes a
    mixed fleet read clean. The scope comes from the dictionary (`reports:` on each
    profile), never from a product name in this module.
    """

    def test_a_generic_agent_is_not_applicable_and_says_so(self):
        data = {"agents": [make_agent("c1", HEAVY_COMPS), make_agent("g1", {}, agent_type="generic")]}

        md = generate_heavy_collectors_report(data, 0.4)

        assert "Found 1 heavy collector(s)" in md
        assert "> Not applicable to 1 agent(s) on profiles: generic (1)." in md
        # And the collector that *was* judged still appears.
        assert "c1" in md

    def test_an_all_collector_fleet_has_no_footer(self):
        """The all-otelcol markdown must not move: the footer is for skipped agents only."""
        data = {"agents": [make_agent("c1", HEAVY_COMPS)]}

        assert "Not applicable" not in generate_heavy_collectors_report(data, 0.4)
        assert "Not applicable" not in generate_outdated_collectors_report(data, "0.149.0")

    def test_the_outdated_report_skips_and_counts_only_applicable_agents(self):
        data = {
            "agents": [
                make_agent("old", OUTDATED_COMPS),
                make_agent("g1", OUTDATED_COMPS, agent_type="generic"),
                make_agent("g2", {}, agent_type="generic"),
            ]
        }

        md = generate_outdated_collectors_report(data, "0.149.0")
        collectors, components = _count_outdated_collectors(data["agents"], "0.149.0")

        assert "Found 1 outdated collector(s)" in md
        assert "> Not applicable to 2 agent(s) on profiles: generic (2)." in md
        assert (collectors, components) == (1, 1), (
            "the count must mean the same thing as the markdown beside it"
        )

    def test_a_legacy_payload_without_a_profile_is_still_reported_on(self):
        """Absent evidence is not the same as evidence of "not a collector".

        A row from before #118 (or one a caller built by hand) carries no `agent_type`,
        and the historical behaviour — report on it — has to stand, or every existing
        caller's report silently empties.
        """
        legacy = {"id": "legacy", "healthy": True, "components": HEAVY_COMPS}

        md = generate_heavy_collectors_report({"agents": [legacy]}, 0.4)

        assert "Found 1 heavy collector(s)" in md
        assert "Not applicable" not in md

    def test_the_endpoint_reports_the_skipped_agents_by_profile(self, client_with_agents):
        client, _agent_id = client_with_agents  # a `generic` agent, no components

        body = client.get("/reports/heavy-collectors").json()

        assert body["applicable_count"] == 0
        assert body["not_applicable"] == {"generic": 1}
        assert body["heavy_count"] == 0
        assert "Not applicable to 1 agent(s) on profiles: generic (1)." in body["report_markdown"]


class TestComponentsStatus:
    """`unsupported` ≠ `unreported` ≠ empty, and the API keeps them apart (#120)."""

    def _agent(self, **kwargs):
        return AgentState(
            instance_uid=bytes.fromhex("ab" * 16), agent_id="ab" * 16, **kwargs
        )

    def test_a_profile_with_no_component_concept_is_unsupported(self):
        agent = self._agent()  # resolves to `generic`
        assert agent.components_status == "unsupported"
        assert agent.components == {}
        assert agent.to_dict()["components_status"] == "unsupported"

    def test_a_collector_that_has_not_reported_is_unreported(self):
        agent = self._agent()
        agent.agent_type = "otelcol"
        assert agent.components_status == "unreported"

    def test_a_collector_that_reported_is_reported(self):
        agent = self._agent()
        agent.agent_type = "otelcol"
        agent.available_components = {
            "components": {"receivers": {"subComponentMap": {"otlp": {"version": "0.1"}}}}
        }
        assert agent.components_status == "reported"
        assert agent.to_dict()["components_status"] == "reported"
