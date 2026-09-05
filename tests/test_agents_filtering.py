"""Tests for GET /agents query-param filtering (metadata, health, remote config status).

Covered: healthy=true/false/unknown, status + alias, arbitrary description-attribute
filters (identifying + non-identifying), repeated-param OR semantics, missing-attribute
exclusion, and 422 on invalid healthy values.
"""

import pytest
from fastapi.testclient import TestClient

from server.main import app
from server.state import AgentState, AGENT_REGISTRY


def make_agent(
    agent_id,
    *,
    healthy=None,
    remote_config_status="UNSET",
    identifying=None,
    non_identifying=None,
):
    AGENT_REGISTRY.register(
        agent_id,
        AgentState(
            instance_uid=f"filter-test-{agent_id}".encode(),
            agent_id=agent_id,
            healthy=healthy,
            remote_config_status=remote_config_status,
            description={
                "identifyingAttributes": identifying or [],
                "nonIdentifyingAttributes": non_identifying or [],
            },
        ),
    )
    return agent_id


@pytest.fixture
def client():
    yield TestClient(app)
    for agent_id in list(AGENT_REGISTRY._agents.keys()):
        AGENT_REGISTRY.remove(agent_id)


@pytest.fixture
def seeded_agents(client):
    make_agent(
        "agent-prod",
        healthy=True,
        identifying=[
            {"key": "service.name", "value": {"stringValue": "prod-collector"}},
            {"key": "environment", "value": {"stringValue": "prod"}},
        ],
        non_identifying=[
            {"key": "tier", "value": {"stringValue": "gold"}},
            {"key": "replicas", "value": {"intValue": "3"}},
        ],
    )
    make_agent(
        "agent-staging",
        healthy=False,
        remote_config_status="APPLIED",
        identifying=[
            {"key": "service.name", "value": {"stringValue": "staging-collector"}},
            {"key": "environment", "value": {"stringValue": "staging"}},
        ],
        non_identifying=[
            {"key": "tier", "value": {"stringValue": "silver"}},
        ],
    )
    make_agent(
        "agent-edge",
        healthy=None,
        remote_config_status="FAILED",
        identifying=[
            {"key": "environment", "value": {"stringValue": "edge"}},
        ],
        non_identifying=[
            {"key": "tier", "value": {"stringValue": "bronze"}},
        ],
    )
    make_agent("agent-bare")
    return ["agent-prod", "agent-staging", "agent-edge", "agent-bare"]


def ids_of(response):
    return [a["id"] for a in response.json()["agents"]]


class TestNoFilters:
    def test_no_filters_returns_all(self, client, seeded_agents):
        response = client.get("/agents")
        assert response.status_code == 200
        assert set(ids_of(response)) == set(seeded_agents)
        assert response.json()["count"] == len(seeded_agents)

    def test_filters_echo_empty(self, client, seeded_agents):
        response = client.get("/agents")
        assert response.json()["filters"] == {}


class TestHealthFilter:
    def test_healthy_true(self, client, seeded_agents):
        response = client.get("/agents", params={"healthy": "true"})
        assert ids_of(response) == ["agent-prod"]
        assert response.json()["filters"] == {"healthy": "true"}

    def test_healthy_false(self, client, seeded_agents):
        response = client.get("/agents", params={"healthy": "false"})
        assert ids_of(response) == ["agent-staging"]

    def test_healthy_unknown(self, client, seeded_agents):
        response = client.get("/agents", params={"healthy": "unknown"})
        assert ids_of(response) == ["agent-edge", "agent-bare"]

    def test_invalid_healthy_422(self, client, seeded_agents):
        response = client.get("/agents", params={"healthy": "maybe"})
        assert response.status_code == 422


class TestRemoteConfigStatusFilter:
    def test_status_unsets_default(self, client, seeded_agents):
        response = client.get("/agents", params={"status": "UNSET"})
        assert ids_of(response) == ["agent-prod", "agent-bare"]

    def test_status_case_insensitive(self, client, seeded_agents):
        response = client.get("/agents", params={"status": "applied"})
        assert ids_of(response) == ["agent-staging"]
        assert response.json()["filters"] == {"remote_config_status": "applied"}

    def test_remote_config_status_alias(self, client, seeded_agents):
        response = client.get("/agents", params={"remote_config_status": "FAILED"})
        assert ids_of(response) == ["agent-edge"]

    def test_status_beats_description_attribute(self, client, seeded_agents):
        # A description attribute literally named "status" must NOT be treated
        # as the remote config filter: this agent reports FAILED remotely but
        # carries a description attr status=UNSET. ?status=UNSET must exclude it.
        make_agent("agent-custom-status", remote_config_status="FAILED", non_identifying=[
            {"key": "status", "value": {"stringValue": "UNSET"}},
        ])
        response = client.get("/agents", params={"status": "UNSET"})
        result = ids_of(response)
        assert "agent-custom-status" not in result
        assert "agent-prod" in result


class TestMetadataAttributeFilter:
    def test_identifying_attribute(self, client, seeded_agents):
        response = client.get("/agents", params={"environment": "prod"})
        assert ids_of(response) == ["agent-prod"]

    def test_non_identifying_attribute(self, client, seeded_agents):
        response = client.get("/agents", params={"tier": "gold"})
        assert ids_of(response) == ["agent-prod"]

    def test_int_value_matches(self, client, seeded_agents):
        response = client.get("/agents", params={"replicas": "3"})
        assert ids_of(response) == ["agent-prod"]

    def test_missing_attribute_excluded(self, client, seeded_agents):
        response = client.get("/agents", params={"environment": "prod"})
        # agent-bare has no attributes at all and must be excluded.
        assert "agent-bare" not in ids_of(response)

    def test_no_match(self, client, seeded_agents):
        response = client.get("/agents", params={"environment": "qa"})
        assert response.json()["agents"] == []

    def test_repeated_params_or_semantics(self, client, seeded_agents):
        response = client.get("/agents", params=[("environment", "prod"), ("environment", "staging")])
        assert ids_of(response) == ["agent-prod", "agent-staging"]

    def test_filters_echo(self, client, seeded_agents):
        response = client.get("/agents", params={"environment": "prod"})
        assert response.json()["filters"] == {"attributes": {"environment": ["prod"]}}


class TestCombinedFilters:
    def test_combined_metadata_and_health(self, client, seeded_agents):
        response = client.get("/agents", params={"environment": "prod", "healthy": "true"})
        assert ids_of(response) == ["agent-prod"]