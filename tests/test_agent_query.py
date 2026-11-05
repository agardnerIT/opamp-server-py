"""SQL selection over the fleet (#132).

Two things are being protected here, and they are different:

* the **language** behaves like SQL over the columns we document (attributes are
  columns, quoting works, `attr()` is shorthand, only SELECT is allowed), so an
  operator or an agent can write a filter without learning a private DSL;
* a **group saved from a query** is still a group: membership is resolved on read,
  health-defined queries keep the push guard, and a legacy filter-document group
  keeps working unchanged.

The refusals are tested through the API as well as the module because a guard that
only `server/agent_query.py` enforces is a guard a route can forget.
"""

from __future__ import annotations

import pytest

from proto import anyvalue_pb2, opamp_pb2
from server import admin_auth, agent_query, config_control, crypto
from server.main import _handle_agent_message
from server.state import AGENT_REGISTRY, SQLiteGroupStore
from tests.support import ADMIN, admin_headers

UID = bytes.fromhex("11" * 16)
AGENT_ID = "11" * 16
OTHER_UID = bytes.fromhex("22" * 16)
OTHER_ID = "22" * 16
THIRD_UID = bytes.fromhex("33" * 16)
THIRD_ID = "33" * 16

CONFIG = {
    "": {
        "body": (
            "receivers:\n"
            "  otlp:\n"
            "    protocols:\n"
            "      grpc:\n"
            "processors:\n"
            "  batch: {}\n"
            "exporters:\n"
            "  debug: {}\n"
            "service:\n"
            "  extensions: [opamp]\n"
            "  pipelines:\n"
            "    traces:\n"
            "      receivers: [otlp]\n"
            "      processors: [batch]\n"
            "      exporters: [debug]\n"
            "extensions:\n"
            "  opamp:\n"
            "    server:\n"
            "      ws:\n"
            "        endpoint: wss://opamp.example.com/v1/opamp\n"
        ),
        "content_type": "text/yaml",
    }
}


def describe(uid: bytes, env: str, arch: str, os_type: str, healthy: bool = True) -> None:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = healthy
    for key, value in (
        ("environment", env),
        ("host_arch", arch),
        ("os.type", os_type),
    ):
        msg.agent_description.non_identifying_attributes.add(
            key=key, value=anyvalue_pb2.AnyValue(string_value=value)
        )
    _handle_agent_message(msg.SerializeToString())


@pytest.fixture(autouse=True)
def secure_posture(monkeypatch):
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    crypto.reset_key_cache()
    yield
    crypto.reset_key_cache()


def create_group(client, name, query):
    return client.post(
        "/groups",
        json={"name": name, "query": query},
        headers=admin_headers(),
    )


# --------------------------------------------------------------- the language


def test_attributes_are_columns_and_dotted_keys_can_be_quoted(client):
    describe(UID, "prod", "amd64", "darwin")
    describe(OTHER_UID, "prod", "arm64", "linux")
    describe(THIRD_UID, "dev", "amd64", "darwin")

    for query in (
        "SELECT * FROM agents WHERE host_arch = 'amd64'",
        'SELECT * FROM agents WHERE "host_arch" = \'amd64\'',
        "SELECT * FROM agents WHERE attr('host_arch') = 'amd64'",
    ):
        body = client.get("/agents", params={"q": query}).json()
        assert sorted(agent["id"] for agent in body["agents"]) == sorted(
            [AGENT_ID, THIRD_ID]
        ), query

    dotted = client.get(
        "/agents", params={"q": 'SELECT * FROM agents WHERE "os.type" = \'darwin\''}
    ).json()
    assert sorted(agent["id"] for agent in dotted["agents"]) == sorted(
        [AGENT_ID, THIRD_ID]
    )


def test_where_clause_supports_and_or_in_like_and_null(client):
    describe(UID, "prod", "amd64", "darwin")
    describe(OTHER_UID, "prod", "arm64", "linux", healthy=False)
    describe(THIRD_UID, "dev", "amd64", "darwin")

    def ids(query):
        return sorted(
            agent["id"] for agent in client.get("/agents", params={"q": query}).json()["agents"]
        )

    assert ids(
        "SELECT * FROM agents WHERE host_arch = 'amd64' AND environment = 'prod'"
    ) == [AGENT_ID]
    assert ids("SELECT * FROM agents WHERE environment IN ('prod', 'dev')") == sorted(
        [AGENT_ID, OTHER_ID, THIRD_ID]
    )
    assert ids("SELECT * FROM agents WHERE host_arch LIKE 'arm%'") == [OTHER_ID]
    # The fixture that reports `healthy: false` ends up with no health report at
    # all (proto3 scalar presence), so it is the "never reported" row here.
    assert ids("SELECT * FROM agents WHERE healthy IS NULL") == [OTHER_ID]


def test_healthy_zero_is_a_row_value_not_a_missing_column():
    from server.state import AgentState

    unhealthy = AgentState(
        instance_uid=bytes.fromhex(OTHER_ID),
        agent_id=OTHER_ID,
        healthy=False,
        health={"status": "config_conflict"},
    )
    healthy = AgentState(
        instance_uid=bytes.fromhex(UID.hex()),
        agent_id=AGENT_ID,
        healthy=True,
    )
    agents = [unhealthy, healthy]
    assert agent_query.select_ids("SELECT * FROM agents WHERE healthy = 0", agents) == [
        OTHER_ID
    ]
    assert agent_query.select_ids(
        "SELECT * FROM agents WHERE health_status = 'config_conflict'", agents
    ) == [OTHER_ID]


def test_a_write_multi_statement_or_unknown_table_is_refused(client):
    describe(UID, "prod", "amd64", "darwin")
    for query in (
        "DELETE FROM agents",
        "SELECT * FROM agents; DROP TABLE agents",
        "SELECT * FROM config_revisions",
        "PRAGMA table_info(agents)",
        "SELECT load_extension('x')",
    ):
        response = client.get("/agents", params={"q": query})
        assert response.status_code == 422, (query, response.text)


def test_the_schema_payload_suggests_values_that_live_agents_report(client):
    describe(UID, "prod", "amd64", "darwin")
    schema = client.get("/agents/query/schema", headers=admin_headers()).json()
    by_name = {column["name"]: column for column in schema["columns"]}
    assert by_name["host_arch"]["kind"] == "attribute"
    assert by_name["host_arch"]["values"] == ["amd64"]
    assert by_name["healthy"]["kind"] == "core"
    assert any("host_arch = 'amd64'" in example for example in schema["examples"])
    assert schema["table"] == "agents"
    assert schema["guide"] == "docs/sql-agent-queries.md"


# ------------------------------------------------------- saved groups as SQL


def test_a_query_group_resolves_live_members_and_reports_its_kind(client):
    describe(UID, "prod", "amd64", "darwin")
    describe(OTHER_UID, "prod", "arm64", "linux")
    created = create_group(
        client,
        "macos-prod",
        "SELECT * FROM agents WHERE \"os.type\" = 'darwin' AND environment = 'prod'",
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["selector_kind"] == "query"
    assert body["query"].startswith("SELECT * FROM agents")
    assert body["definition"] == {}
    assert body["members"] == [AGENT_ID]
    assert body["sources"]["query"] == [AGENT_ID]

    # Membership follows the fleet, not the create-time snapshot.
    describe(THIRD_UID, "prod", "arm64", "darwin")
    listing = client.get("/groups", headers=admin_headers()).json()
    assert listing["groups"][0]["member_count"] == 2


def test_a_query_group_scope_resolves_through_the_config_control_plane(client):
    describe(UID, "prod", "amd64", "darwin")
    describe(OTHER_UID, "dev", "arm64", "linux")
    create_group(client, "macos-prod", "SELECT * FROM agents WHERE host_arch = 'amd64'")

    proposed = client.post(
        "/config/revisions",
        json={"config_map": CONFIG, "scope": {"group": "macos-prod"}},
        headers=admin_headers(),
    )
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert [row["agent_id"] for row in body["revisions"]] == [AGENT_ID]
    assert body["scope"]["selector_kind"] == "query"
    assert body["scope"]["group_query"] == "SELECT * FROM agents WHERE host_arch = 'amd64'"
    assert body["scope"]["filter"] == {}
    # The snapshot records the query, so a later edit cannot rewrite this revision.
    assert body["scope"]["group_snapshot"]["query"].endswith("host_arch = 'amd64'")


def test_a_query_group_with_include_future_attaches_later_matches(client):
    describe(UID, "prod", "amd64", "darwin")
    create_group(client, "prod-query", "SELECT * FROM agents WHERE environment = 'prod'")
    proposed = client.post(
        "/config/revisions",
        json={
            "config_map": CONFIG,
            "scope": {"group": "prod-query", "include_future": True},
        },
        headers=admin_headers(),
    )
    assert proposed.status_code == 200, proposed.text
    approved = client.post(
        f"/config/revisions/{proposed.json()['proposal_id']}/approve",
        json={},
        headers=admin_headers(),
    )
    assert approved.status_code == 200, approved.text

    describe(OTHER_UID, "prod", "arm64", "linux")
    # The sweep re-evaluates the saved query, not an empty filter.
    assert config_control.reconcile_future(AGENT_REGISTRY.list_all()) == 1
    assert config_control.reconcile_future(AGENT_REGISTRY.list_all()) == 0


def test_a_query_group_refuses_limit_because_a_group_is_a_filter(client):
    describe(UID, "prod", "amd64", "darwin")
    response = create_group(client, "limited", "SELECT * FROM agents LIMIT 1")
    assert response.status_code == 422
    assert "LIMIT" in response.text


def test_a_health_query_group_keeps_the_push_guard(client):
    describe(UID, "prod", "amd64", "darwin")
    created = create_group(
        client, "healthy-prod", "SELECT * FROM agents WHERE healthy = 1"
    )
    assert created.status_code == 200, created.text
    assert created.json()["health_defined"] == ["healthy"]

    proposed = client.post(
        "/config/revisions",
        json={"config_map": CONFIG, "scope": {"group": "healthy-prod"}},
        headers=admin_headers(),
    )
    assert proposed.status_code == 400
    assert "health" in proposed.text.lower()

    # The escape hatch is unchanged: preview records a membership, then a stored
    # resolution push is allowed.
    preview = client.post(
        "/groups/healthy-prod/preview", json={}, headers=admin_headers()
    )
    assert preview.status_code == 200, preview.text
    stored = client.post(
        "/config/revisions",
        json={
            "config_map": CONFIG,
            "scope": {"group": "healthy-prod", "use_stored_resolution": True},
        },
        headers=admin_headers(),
    )
    assert stored.status_code == 200, stored.text


def test_editing_a_query_and_switching_selector_kind_keeps_one_selector(client):
    describe(UID, "prod", "amd64", "darwin")
    describe(OTHER_UID, "dev", "arm64", "linux")
    group = create_group(
        client, "selector-swap", "SELECT * FROM agents WHERE environment = 'prod'"
    ).json()
    assert group["members"] == [AGENT_ID]

    edited = client.put(
        f"/groups/{group['group_id']}",
        json={"query": "SELECT * FROM agents WHERE host_arch = 'arm64'"},
        headers=admin_headers(),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["members"] == [OTHER_ID]
    assert edited.json()["changes"]["query"]["to"].endswith("host_arch = 'arm64'")

    # Rewrite it as a legacy filter document: the query must be cleared, not kept as
    # a second selector that would silently win.
    legacy = client.put(
        f"/groups/{group['group_id']}",
        json={"definition": {"environment": "prod"}},
        headers=admin_headers(),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["selector_kind"] == "definition"
    assert legacy.json()["query"] is None
    assert legacy.json()["members"] == [AGENT_ID]

    # And back again.
    back = client.put(
        f"/groups/{group['group_id']}",
        json={"query": "SELECT * FROM agents WHERE environment = 'dev'"},
        headers=admin_headers(),
    )
    assert back.status_code == 200, back.text
    assert back.json()["selector_kind"] == "query"
    assert back.json()["definition"] == {}
    assert back.json()["members"] == [OTHER_ID]


def test_a_group_cannot_be_saved_with_two_selectors_or_neither(client):
    describe(UID, "prod", "amd64", "darwin")
    both = client.post(
        "/groups",
        json={
            "name": "two-selectors",
            "query": "SELECT * FROM agents",
            "definition": {"environment": "prod"},
        },
        headers=admin_headers(),
    )
    assert both.status_code == 422
    neither = client.post(
        "/groups", json={"name": "no-selector"}, headers=admin_headers()
    )
    assert neither.status_code == 422


def test_a_legacy_definition_group_still_resolves_unchanged(client):
    describe(UID, "prod", "amd64", "darwin")
    legacy = client.post(
        "/groups",
        json={"name": "legacy", "definition": {"environment": "prod"}},
        headers=admin_headers(),
    )
    assert legacy.status_code == 200, legacy.text
    body = legacy.json()
    assert body["selector_kind"] == "definition"
    assert body["query"] is None
    assert body["members"] == [AGENT_ID]
    assert body["sources"]["definition"] == [AGENT_ID]


def test_the_store_upgrades_a_pre_132_database_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE collector_groups ("
            "group_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,"
            "definition_json TEXT NOT NULL, include_json TEXT NOT NULL DEFAULT '[]',"
            "exclude_json TEXT NOT NULL DEFAULT '[]', description TEXT, owner TEXT,"
            "source TEXT NOT NULL DEFAULT 'api', created_by TEXT NOT NULL,"
            "created_at TEXT NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL,"
            "archived_at TEXT, archived_by TEXT)"
        )
        conn.execute(
            "INSERT INTO collector_groups VALUES "
            "('grp-old','old','old','{\"environment\": \"prod\"}','[]','[]',NULL,NULL,"
            "'api','test','2026-01-01',NULL,'2026-01-01',NULL,NULL)"
        )
    store = SQLiteGroupStore(path)
    record = store.find("old")
    assert record is not None
    assert record["definition"] == {"environment": "prod"}
    assert record.get("query") is None


def test_the_module_refuses_a_query_without_an_id_projection():
    with pytest.raises(agent_query.AgentQueryError) as excinfo:
        agent_query.run("SELECT environment FROM agents", [])
    assert excinfo.value.status_code == 422
    assert "id" in str(excinfo.value.detail).lower()
