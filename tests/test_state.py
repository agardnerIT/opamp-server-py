import pytest
import tempfile
from pathlib import Path
from tests.uids import uid
# #118: `COMPONENT_TYPES` (and the other collector tables) moved to
# `server/profiles/otelcol.py`; the `_detect_type` tests moved to tests/test_profiles.py.
from server.state import AgentRegistry, AgentState, utcnow, CAPABILITY_TAGS, parse_effective_config, SQLiteAgentStore
from datetime import datetime, timedelta


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        store = SQLiteAgentStore(db_path=db_path)
        registry = AgentRegistry()
        registry._store = store
        registry._agents = store.load_all()
        yield registry
        for agent_id in list(registry._agents.keys()):
            registry.remove(agent_id)


class TestAgentState:
    def test_agent_state_creation(self):
        state = AgentState(
            instance_uid=uid("test123"),
            agent_id="test123"
        )
        assert state.agent_id == "test123"
        assert state.instance_uid == uid("test123")
        assert state.healthy is None
        assert state.capabilities == 0

    def test_agent_state_to_dict(self):
        state = AgentState(
            instance_uid=uid("test456"),
            agent_id="test456",
            healthy=True
        )
        d = state.to_dict()
        assert d["id"] == "test456"
        assert d["healthy"] is True
        assert "connected_at" in d

    def test_capability_tags_all_bits_set(self):
        all_caps = sum(CAPABILITY_TAGS.keys())
        state = AgentState(
            instance_uid=uid("test_caps"),
            agent_id="test_caps",
            capabilities=all_caps
        )
        tags = state.capability_tags
        assert len(tags) == len(CAPABILITY_TAGS)

    def test_capability_tags_empty_when_zero(self):
        state = AgentState(
            instance_uid=uid("test_no_caps"),
            agent_id="test_no_caps",
            capabilities=0
        )
        assert state.capability_tags == []

    def test_capability_tags_subset(self):
        state = AgentState(
            instance_uid=uid("test_some_caps"),
            agent_id="test_some_caps",
            capabilities=0x00000001 | 0x00000040
        )
        tags = state.capability_tags
        assert len(tags) == 2
        labels = [t["label"] for t in tags]
        assert "Reports Status" in labels
        assert "Reports Own Metrics" in labels

    def test_components_groups_by_type(self):
        state = AgentState(
            instance_uid=uid("test_comps"),
            agent_id="test_comps",
            available_components={
                "components": {
                    "receivers": {"subComponentMap": {"otlp": {}}},
                    "processors": {"subComponentMap": {"batch": {}}},
                    "exporters": {"subComponentMap": {"debug": {}}},
                }
            }
        )
        comps = state.components
        assert "receiver" in comps
        assert "processor" in comps
        assert "exporter" in comps
        assert len(comps["receiver"]) == 1
        assert comps["receiver"][0]["id"] == "otlp"

    def test_components_marks_used_status(self):
        state = AgentState(
            instance_uid=uid("test_used"),
            agent_id="test_used",
            available_components={
                "components": {
                    "receivers": {"subComponentMap": {"otlp": {}, "prometheus": {}}},
                }
            },
            health={
                "componentHealthMap": {
                    "pipeline:traces": {
                        "componentHealthMap": {
                            "receiver:otlp": {}
                        }
                    }
                }
            }
        )
        comps = state.components
        otlp = next(c for c in comps["receiver"] if c["id"] == "otlp")
        prom = next(c for c in comps["receiver"] if c["id"] == "prometheus")
        assert otlp["used"] is True
        assert prom["used"] is False


class TestParseEffectiveConfig:
    def test_parse_extracts_pipeline_components(self):
        yaml_config = """
service:
  extensions: [opamp]
  pipelines:
    traces:
      receivers:
        - otlp
        - jaeger
      processors:
        - batch
      exporters:
        - debug
"""
        effective_config = {
            "configMap": {
                "configMap": {
                    "": {"body": yaml_config.encode("utf-8")}
                }
            }
        }
        result = parse_effective_config(effective_config)
        assert "opamp" in result.get("extension", set())
        assert "otlp" in result.get("receiver", set())
        assert "jaeger" in result.get("receiver", set())
        assert "batch" in result.get("processor", set())
        assert "debug" in result.get("exporter", set())

    def test_parse_extracts_extensions(self):
        yaml_config = """
service:
  extensions: [zpages, health_check]
"""
        effective_config = {
            "configMap": {
                "configMap": {
                    "": {"body": yaml_config.encode("utf-8")}
                }
            }
        }
        result = parse_effective_config(effective_config)
        assert "zpages" in result.get("extension", set())
        assert "health_check" in result.get("extension", set())

    def test_parse_returns_empty_when_no_pipelines(self):
        yaml_config = """
receivers:
  otlp:
    protocols:
      grpc:
"""
        effective_config = {
            "configMap": {
                "configMap": {
                    "": {"body": yaml_config.encode("utf-8")}
                }
            }
        }
        result = parse_effective_config(effective_config)
        assert result == {}

    def test_parse_returns_empty_on_invalid_yaml(self):
        effective_config = {
            "configMap": {
                "configMap": {
                    "": {"body": b"not: valid: yaml: [[["}
                }
            }
        }
        result = parse_effective_config(effective_config)
        assert result == {}

    def test_parse_returns_empty_when_no_config_map(self):
        effective_config = {}
        result = parse_effective_config(effective_config)
        assert result == {}

    def test_parse_handles_base64_encoded_body(self):
        import base64
        yaml_config = """
service:
  extensions: [opamp]
  pipelines:
    traces:
      receivers:
        - otlp
      exporters:
        - debug
"""
        effective_config = {
            "configMap": {
                "configMap": {
                    "": {"body": base64.b64encode(yaml_config.encode("utf-8")).decode("utf-8")}
                }
            }
        }
        result = parse_effective_config(effective_config)
        assert "opamp" in result.get("extension", set())
        assert "otlp" in result.get("receiver", set())
        assert "debug" in result.get("exporter", set())


class TestAgentRegistry:
    def test_registry_empty(self, temp_db):
        assert temp_db.count == 0
        assert temp_db.list_all() == []

    def test_register_agent(self, temp_db):
        state = AgentState(instance_uid=uid("test"), agent_id="test")
        temp_db.register("test", state)
        
        assert temp_db.count == 1
        assert temp_db.get("test") == state

    def test_remove_agent(self, temp_db):
        state = AgentState(instance_uid=uid("test"), agent_id="test")
        temp_db.register("test", state)
        
        result = temp_db.remove("test")
        assert result is True
        assert temp_db.count == 0

    def test_remove_nonexistent(self, temp_db):
        result = temp_db.remove("nonexistent")
        assert result is False

    def test_update_agent(self, temp_db):
        state = AgentState(instance_uid=uid("test"), agent_id="test", healthy=None)
        temp_db.register("test", state)
        
        temp_db.update("test", healthy=True)
        assert temp_db.get("test").healthy is True

    def test_list_all(self, temp_db):
        temp_db.register("a", AgentState(instance_uid=uid("a"), agent_id="a"))
        temp_db.register("b", AgentState(instance_uid=uid("b"), agent_id="b"))
        
        agents = temp_db.list_all()
        assert len(agents) == 2


class TestStateDatabaseHealth:
    """`state_database_health` is the boot-time answer to "is SQLite usable?".

    A damaged database only announced itself as an opaque 500 on the first write, so
    the check runs at startup and answers the same question in milliseconds.
    """

    def test_a_healthy_database_reports_ok(self, tmp_path):
        from server.state import state_database_health

        SQLiteAgentStore(db_path=tmp_path / "state.db")
        ok, detail = state_database_health(tmp_path / "state.db")
        assert ok is True
        assert detail == "ok"

    def test_a_missing_file_is_not_a_failure(self, tmp_path):
        from server.state import state_database_health

        ok, detail = state_database_health(tmp_path / "not-created-yet.db")
        assert ok is True, "the stores create the file on first use"
        assert "not created" in detail

    def test_a_corrupt_file_reports_the_problem_instead_of_raising(self, tmp_path):
        from server.state import state_database_health

        broken = tmp_path / "broken.db"
        broken.write_bytes(b"this is not a sqlite database" * 100)

        ok, detail = state_database_health(broken)
        assert ok is False
        assert detail
