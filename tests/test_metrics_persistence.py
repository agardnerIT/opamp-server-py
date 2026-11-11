"""Tests for agent metrics SQLite persistence (issue #52, gap G8)."""

import json
import sqlite3

from fastapi.testclient import TestClient

from server import main as server_main
from server.state import SQLiteMetricsStore, AGENT_REGISTRY, AgentState


class TestMetricsStore:
    def test_upsert_get_roundtrip(self, tmp_path):
        store = SQLiteMetricsStore(tmp_path / "m.db")
        store.upsert("abc", {"metrics": {"a": 1}, "updated_at": "2026-01-01T00:00:00+00:00"})
        assert store.get("abc") == {
            "metrics": {"a": 1},
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

    def test_upsert_overwrites_latest_snapshot(self, tmp_path):
        store = SQLiteMetricsStore(tmp_path / "m.db")
        store.upsert("abc", {"metrics": {"a": 1}, "updated_at": "t1"})
        store.upsert("abc", {"metrics": {"a": 2, "b": 3}, "updated_at": "t2"})
        got = store.get("abc")
        assert got["metrics"] == {"a": 2, "b": 3}
        assert got["updated_at"] == "t2"

    def test_get_missing_agent(self, tmp_path):
        store = SQLiteMetricsStore(tmp_path / "m.db")
        assert store.get("nope") is None

    def test_delete(self, tmp_path):
        store = SQLiteMetricsStore(tmp_path / "m.db")
        store.upsert("abc", {"metrics": {"a": 1}, "updated_at": "t"})
        store.delete("abc")
        assert store.get("abc") is None
        store.delete("abc")  # idempotent
        assert store.get("abc") is None

    def test_corrupt_json_returns_none(self, tmp_path):
        db = tmp_path / "m.db"
        store = SQLiteMetricsStore(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_metrics (agent_id, metrics, updated_at) "
                "VALUES ('bad', '{oops', 't')"
            )
        assert store.get("bad") is None

    def test_unavailable_db_does_not_raise(self, tmp_path):
        # a directory path makes sqlite3 fail -> swallowed with a warning
        store = SQLiteMetricsStore(tmp_path)
        store.upsert("abc", {"metrics": {"a": 1}, "updated_at": "t"})
        assert store.get("abc") is None
        store.delete("abc")


class TestIngestPersists:
    def test_metrics_ingest_persists_to_store(self, tmp_path, monkeypatch):
        store = SQLiteMetricsStore(tmp_path / "m.db")
        monkeypatch.setattr(server_main, "_METRICS_STORE", store)
        monkeypatch.setattr(server_main, "AGENT_METRICS", {})
        agent_id = "a" * 32
        AGENT_REGISTRY.register(agent_id, AgentState(instance_uid=bytes(16), agent_id=agent_id))

        client = TestClient(server_main.app)
        body = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.instance.id", "value": {"stringValue": agent_id}},
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {"name": "otelcol_process_uptime", "gauge": {"dataPoints": [{"asInt": 42}]}}
                            ]
                        }
                    ],
                }
            ]
        }
        resp = client.post("/v1/metrics", json=body)
        assert resp.status_code == 200

        persisted = store.get(agent_id)
        assert persisted is not None
        assert persisted["metrics"]["otelcol_process_uptime"] == 42
        assert "updated_at" in persisted

        # read path falls back to the store when the in-memory cache is empty
        monkeypatch.setattr(server_main, "AGENT_METRICS", {})
        resp = client.get(f"/agent/{agent_id}/metrics")
        assert resp.status_code == 200
        assert resp.json()["metrics"]["otelcol_process_uptime"] == 42

        AGENT_REGISTRY.remove(agent_id)


# ------------------------------------------------- one identity path (#122)


import pytest


@pytest.fixture()
def client_with_agent(monkeypatch, tmp_path):
    """A client with one registered agent on a throwaway metrics store."""
    agent_id = "ab" * 16
    state = AgentState(instance_uid=bytes.fromhex(agent_id), agent_id=agent_id)
    AGENT_REGISTRY.register(agent_id, state)
    store = SQLiteMetricsStore(tmp_path / "metrics.db")
    monkeypatch.setattr(server_main, "AGENT_REGISTRY", AGENT_REGISTRY)
    monkeypatch.setattr(server_main, "METRICS_STORE", store, raising=False)
    yield TestClient(server_main.app), agent_id
    AGENT_REGISTRY.remove(agent_id)


class TestTheIdentityDerivationExistsOnce:
    """The same expression lived in the JSON branch and the protobuf branch of
    `POST /v1/metrics`, which is how one of them eventually grows a fix the other does
    not have. There is one function now, and both encodings have to agree.
    """

    def test_the_attribute_order_and_the_dash_stripping(self):
        from server.main import IDENTITY_ATTRIBUTES, _identity_from_resource

        assert IDENTITY_ATTRIBUTES == ("service.instance.id", "service.name")
        assert _identity_from_resource(
            [("service.name", "otelcol"), ("service.instance.id", "ab-cd-ef")]
        ) == "abcdef"
        # Falls back when the preferred attribute is absent or blank.
        assert _identity_from_resource([("service.name", "otelcol-1")]) == "otelcol1"
        assert _identity_from_resource(
            [("service.instance.id", "   "), ("service.name", "otelcol")]
        ) == "otelcol"
        # Nothing identifying: no id, and no exception — ingestion is best-effort.
        assert _identity_from_resource([]) == ""
        assert _identity_from_resource([("other.attr", "x")]) == ""

    def test_both_encodings_land_on_the_same_agent(self, client_with_agent):
        """JSON and protobuf are the same request in two spellings; the id must match."""
        client, agent_id = client_with_agent
        resource = {"attributes": [
            {"key": "service.instance.id",
             "value": {"stringValue": agent_id[:8] + "-" + agent_id[8:]}},
        ]}

        json_body = {"resourceMetrics": [{
            "resource": resource,
            "scopeMetrics": [{"metrics": [{"name": "otelcol_process_uptime",
                                           "gauge": {"dataPoints": [{"asInt": "12"}]}}]}],
        }]}
        assert client.post("/v1/metrics", json=json_body).status_code == 200

        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

        request = metrics_service_pb2.ExportMetricsServiceRequest()
        resource_metrics = request.resource_metrics.add()
        attribute = resource_metrics.resource.attributes.add()
        attribute.key = "service.instance.id"
        attribute.value.string_value = agent_id[:8] + "-" + agent_id[8:]
        scope = resource_metrics.scope_metrics.add()
        metric = scope.metrics.add()
        metric.name = "otelcol_process_uptime"
        metric.gauge.data_points.add().as_int = 34
        assert client.post(
            "/v1/metrics",
            content=request.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
        ).status_code == 200

        stored = client.get(f"/agent/{agent_id}/metrics").json()
        values = stored.get("metrics") or {}
        assert values.get("otelcol_process_uptime") == 34, (
            "the protobuf batch must have hit the same agent the JSON batch did"
        )
