import pytest
from fastapi.testclient import TestClient
from server.main import app
from server.state import AGENT_REGISTRY


@pytest.fixture
def client():
    yield TestClient(app)
    for agent_id in list(AGENT_REGISTRY._agents.keys()):
        AGENT_REGISTRY.remove(agent_id)


class TestHealthEndpoints:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "agents_connected" in data

    def test_agents_empty(self, client):
        response = client.get("/agents")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["agents"] == []

    def test_agent_not_found(self, client):
        response = client.get("/agent/nonexistent")
        assert response.status_code == 404


class TestOpAMPEndpoint:
    def test_empty_body(self, client):
        response = client.post(
            "/v1/opamp",
            content=b"",
            headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-protobuf"

    def test_invalid_protobuf(self, client):
        response = client.post(
            "/v1/opamp",
            content=b"invalid data",
            headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-protobuf"

    def test_valid_agent_message(self, client):
        from proto import opamp_pb2
        
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = b"test_opamp_00123456"
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"}
        )
        
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-protobuf"
        
        server_msg = opamp_pb2.ServerToAgent()
        server_msg.ParseFromString(response.content)
        assert server_msg.instance_uid == b"test_opamp_00123456"
        assert server_msg.capabilities > 0

    def test_agent_heartbeat(self, client):
        from proto import opamp_pb2
        
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = b"test_heartbeat_12345678"
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        msg.health.healthy = True
        
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"}
        )
        
        assert response.status_code == 200
        
        agents = client.get("/agents").json()
        assert agents["count"] >= 1

    def test_agents_includes_capability_tags(self, client):
        from proto import opamp_pb2
        
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = b"test_caps_tags_12345678"
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200
        
        agents = client.get("/agents").json()
        assert agents["count"] >= 1
        assert "capability_tags" in agents["agents"][0]
        assert len(agents["agents"][0]["capability_tags"]) > 0

    def test_agent_detail_includes_capability_tags(self, client):
        from proto import opamp_pb2
        
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = b"test_detail_caps_87654321"
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
        
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200
        
        agents = client.get("/agents").json()
        agent_id = agents["agents"][0]["id"]
        
        detail = client.get(f"/agent/{agent_id}").json()
        assert "capability_tags" in detail
        assert len(detail["capability_tags"]) > 0

    def test_agents_includes_components_field(self, client):
        from proto import opamp_pb2
        
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = b"test_comps_11111111"
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsAvailableComponents
        
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200
        
        agents = client.get("/agents").json()
        assert agents["count"] >= 1
        assert "components" in agents["agents"][0]

    def test_agent_detail_includes_components_field(self, client):
        from proto import opamp_pb2
        
        msg = opamp_pb2.AgentToServer()
        msg.instance_uid = b"test_comps_22222222"
        msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsAvailableComponents
        
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200
        
        agents = client.get("/agents").json()
        agent_id = agents["agents"][0]["id"]
        
        detail = client.get(f"/agent/{agent_id}").json()
        assert "components" in detail


class TestMetricsEndpoint:
    def test_metrics(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        content = response.text
        assert "opamp_connected_agents" in content
