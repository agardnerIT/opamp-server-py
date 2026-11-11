"""The agent-type surface: `GET /agent/types`, `PUT /agent/{id}/type` (#118).

The unit behaviour (precedence, cache, the generic fallback, attribution) is in
`tests/test_profiles.py`. This file is the *surface*: the two routes, their refusals,
the audit rows, persistence across a restart, and the fact that every layer can
reach them.

The security story is asserted here in the only way that means anything: a pin is an
operator action, an unknown name is refused rather than silently `generic`, and the
resolved type is reported **with its provenance** so a spoofed `service.name` can
change a label but never produce an unqualified claim.
"""

from __future__ import annotations

import base64
import json

import pytest

from server import admin_auth, audit
from server.main import app
from server.state import AGENT_REGISTRY, AgentState, SQLiteAgentStore

PASSWORD = "correct-horse-battery-staple"
AGENT = "ab" * 16


def headers() -> dict:
    return {
        "Authorization": "Basic "
        + base64.b64encode(f":{PASSWORD}".encode()).decode()
    }


@pytest.fixture(autouse=True)
def _operator(monkeypatch):
    """A shared password (admin role) so every write route is reachable."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    for existing in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(existing)
    yield
    for existing in list(AGENT_REGISTRY._agents):
        AGENT_REGISTRY.remove(existing)


def register(**kwargs) -> AgentState:
    state = AgentState(instance_uid=bytes.fromhex(AGENT), agent_id=AGENT, **kwargs)
    AGENT_REGISTRY.register(AGENT, state)
    return state


def collector_snapshot() -> AgentState:
    """A registered agent that sniffs as a collector (components, no description)."""
    return register(
        available_components={"components": {"receivers": {"subComponentMap": {"otlp": {}}}}}
    )


class TestTypesEndpoint:
    def test_lists_the_profiles_this_build_knows(self, client):
        response = client.get("/agent/types")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(body["types"])
        names = [entry["name"] for entry in body["types"]]
        assert "otelcol" in names and "generic" in names

    def test_the_listing_says_which_profiles_are_validated(self, client):
        types = {entry["name"]: entry for entry in client.get("/agent/types").json()["types"]}
        assert types["otelcol"]["validates_structure"] is True
        assert types["generic"]["validates_structure"] is False
        assert types["generic"]["detectable"] is False

    def test_it_is_declared_before_the_agent_route(self, client):
        """`/agent/types` must not be parsed as an agent id."""
        assert client.get("/agent/types").status_code == 200


class TestPinEndpoint:
    def test_an_operator_can_pin_an_unknown_profiles_future_agent(self, client):
        register()

        response = client.put(
            f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "otelcol"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_type"] == "otelcol"
        assert body["agent_type_source"] == "pinned"
        assert body["agent_type_pinned"] == "otelcol"
        assert body["changed"] is True
        assert body["previous"] is None

    def test_an_unknown_name_is_refused_not_treated_as_generic(self, client):
        register()

        # `fluentbit` is a real profile since #123, so the example has to be a name no
        # dictionary could plausibly ship.
        response = client.put(
            f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "no-such-product"}
        )

        assert response.status_code == 422
        assert "otelcol" in response.json()["detail"]
        assert AGENT_REGISTRY.get(AGENT).agent_type is None

    def test_an_unknown_agent_is_a_404(self, client):
        response = client.put(
            f"/agent/{'cd' * 16}/type", headers=headers(), json={"agent_type": "otelcol"}
        )
        assert response.status_code == 404

    def test_null_returns_the_agent_to_auto_detection(self, client):
        register()
        client.put(f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "otelcol"})

        cleared = client.put(
            f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": None}
        )

        assert cleared.status_code == 200
        assert cleared.json()["agent_type_source"] == "fallback"
        assert cleared.json()["agent_type"] == "generic"
        assert cleared.json()["agent_type_pinned"] is None

    def test_an_empty_string_also_clears_the_pin(self, client):
        register()
        client.put(f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "otelcol"})
        response = client.put(
            f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": ""}
        )
        assert response.json()["agent_type_pinned"] is None

    def test_the_pin_is_case_insensitive_and_stored_canonically(self, client):
        register()
        response = client.put(
            f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "OTelCol"}
        )
        assert response.status_code == 200
        assert response.json()["agent_type_pinned"] == "otelcol"

    def test_a_pin_changes_the_resolution_immediately(self, client):
        """A collector that sniffed as `otelcol` can be pinned to `generic`."""
        collector_snapshot()
        assert client.get(f"/agent/{AGENT}").json()["agent_type"] == "otelcol"

        client.put(f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "generic"})

        body = client.get(f"/agent/{AGENT}").json()
        assert body["agent_type"] == "generic"
        assert body["agent_type_source"] == "pinned"
        assert body["components"] == {}
        assert body["agent_type_warning"]


class TestAudit:
    def test_pinning_is_recorded_before_it_is_applied(self, client):
        register()
        client.put(f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "otelcol"})

        entry = next(
            row for row in audit.STORE.list_entries() if row["action"] == "agent.type.pinned"
        )
        assert entry["target_id"] == AGENT
        detail = json.loads(entry["detail_json"])
        assert detail["agent_type"] == "otelcol"
        assert detail.get("previous") is None  # None is dropped from detail_json

    def test_clearing_the_pin_has_its_own_action(self, client):
        register()
        client.put(f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": "otelcol"})
        client.put(f"/agent/{AGENT}/type", headers=headers(), json={"agent_type": None})

        actions = [row["action"] for row in audit.STORE.list_entries()]
        assert "agent.type.pinned" in actions
        assert "agent.type.auto" in actions


class TestPersistence:
    def test_a_pin_survives_a_restart(self, client, tmp_path):
        """The pin is a column on the agent row, not in-memory state."""
        store = SQLiteAgentStore(tmp_path / "agents.db")
        state = AgentState(
            instance_uid=bytes.fromhex(AGENT), agent_id=AGENT, agent_type="otelcol"
        )
        store.upsert(state)

        reloaded = store.load_all()[AGENT]
        assert reloaded.agent_type == "otelcol"
        assert reloaded.type_info()["agent_type_source"] == "pinned"

    def test_an_unpinned_row_is_null_not_a_guess(self, tmp_path):
        store = SQLiteAgentStore(tmp_path / "agents.db")
        store.upsert(AgentState(instance_uid=bytes.fromhex(AGENT), agent_id=AGENT))
        assert store.load_all()[AGENT].agent_type is None

    def test_a_pre_existing_database_gains_the_column(self, tmp_path):
        """`ALTER TABLE` is the migration: an old row must load, unpinned."""
        import sqlite3

        db = tmp_path / "old.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, instance_uid BLOB,"
                " connected_at TEXT, last_heartbeat TEXT, healthy INTEGER,"
                " capabilities INTEGER DEFAULT 0, description TEXT, effective_config TEXT,"
                " remote_config_status TEXT DEFAULT 'UNSET', package_statuses TEXT,"
                " available_components TEXT, health TEXT, compliance TEXT, updated_at TEXT)"
            )
            conn.execute(
                "INSERT INTO agents (agent_id, description, available_components, health)"
                " VALUES (?, '{}', '{}', '{}')",
                (AGENT,),
            )

        loaded = SQLiteAgentStore(db).load_all()[AGENT]
        assert loaded.agent_type is None
        assert loaded.type_info()["agent_type"] == "generic"


class TestReadSurfaces:
    def test_the_fleet_listing_carries_the_type(self, client):
        register()
        agents = client.get("/agents").json()["agents"]
        assert agents[0]["agent_type"] == "generic"
        assert agents[0]["agent_type_source"] == "fallback"

    def test_the_detail_view_carries_the_type(self, client):
        collector_snapshot()
        body = client.get(f"/agent/{AGENT}").json()
        assert body["agent_type"] == "otelcol"
        assert body["agent_type_source"] == "sniffed"
        assert body["agent_type_warning"] is None

    def test_the_validation_surface_names_the_profile(self, client):
        collector_snapshot()
        response = client.post(
            "/config/validate",
            headers=headers(),
            json={
                "agent_id": AGENT,
                "config_map": {
                    "": {
                        "body": "receivers:\n  otlp:\nservice:\n  pipelines:\n"
                        "    traces:\n      receivers: [otlp]\n      exporters: [debug]\n",
                        "content_type": "text/yaml",
                    }
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["profile"] == "otelcol"
        assert body["profile_source"] == "sniffed"

    def test_a_validation_with_no_agent_names_no_profile(self, client):
        response = client.post(
            "/config/validate",
            headers=headers(),
            json={"config_map": {"": {"body": "a: 1\n", "content_type": "text/yaml"}}},
        )
        assert response.json()["profile"] == ""


class TestLayers:
    def test_the_client_speaks_both_routes(self):
        from client.opamp_client import OpampClient
        from tests.asgi_transport import InProcessASGITransport

        register()
        api = OpampClient(
            base_url="http://opamp.test",
            password=PASSWORD,
            transport=InProcessASGITransport(app),
        )
        assert {entry["name"] for entry in api.agent_types()["types"]} >= {"otelcol", "generic"}
        assert api.set_agent_type(AGENT, "otelcol")["agent_type_source"] == "pinned"
        assert api.set_agent_type(AGENT)["agent_type_source"] == "fallback"

    def test_the_client_refuses_an_unknown_name_like_the_api(self):
        from client.opamp_client import OpampApiError, OpampClient
        from tests.asgi_transport import InProcessASGITransport

        register()
        api = OpampClient(
            base_url="http://opamp.test",
            password=PASSWORD,
            transport=InProcessASGITransport(app),
        )
        with pytest.raises(OpampApiError) as caught:
            api.set_agent_type(AGENT, "nope")
        assert caught.value.status_code == 422

    def test_mcp_exposes_the_read_tool_and_gates_the_write(self):
        from mcp_server import server as mcp

        assert mcp.writes_enabled() is False
        names = {tool.name for tool in mcp.mcp._tool_manager.list_tools()}
        assert "list_agent_types" in names
        assert "set_agent_type" not in names

    def test_mcp_exposes_the_write_tool_when_enabled(self, monkeypatch):
        """The gate is global state, so this test puts the registry back as it found it.

        `_register_write_tools()` mutates the module-level `FastMCP`; a test that leaves
        tools behind makes every later suite believe writes are enabled (which is how
        `test_layer_parity` and `test_mcp_server` started failing in a full run).
        """
        from mcp_server import server as mcp

        before = {tool.name for tool in mcp.mcp._tool_manager.list_tools()}
        monkeypatch.setenv("OPAMP_MCP_ALLOW_WRITES", "true")
        mcp._register_write_tools()
        try:
            names = {tool.name for tool in mcp.mcp._tool_manager.list_tools()}
            assert "set_agent_type" in names
        finally:
            for name in names - before:
                mcp.mcp._tool_manager.remove_tool(name)
        assert {tool.name for tool in mcp.mcp._tool_manager.list_tools()} == before

    def test_the_cli_has_the_commands(self):
        from cli.main import app as cli_app

        registered = set()

        def walk(typer_app, prefix=()):
            for command in getattr(typer_app, "registered_commands", []):
                registered.add(prefix + (command.name,))
            for group in getattr(typer_app, "registered_groups", []):
                walk(group.typer_instance, prefix + (group.name,))

        walk(cli_app)
        assert ("agents", "types") in registered
        assert ("agents", "set-type") in registered
