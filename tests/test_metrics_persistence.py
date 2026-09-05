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
