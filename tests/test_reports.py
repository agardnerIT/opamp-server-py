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


def make_agent(agent_id, components, healthy=True):
    return {
        "id": agent_id,
        "healthy": healthy,
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
