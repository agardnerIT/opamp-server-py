"""#137: a declared `agent.name` is shown, and is not an identity.

The attribute is the agent's own label — not an OTel semantic convention, not unique,
and changeable on restart. So this suite protects two things at once:

* the label **reaches every surface** that has to display an agent (the API field, the
  SQL column, and — in `tests/test_layer_parity.py` — the CLI and MCP proxies);
* the label **never becomes an address**: the id stays the key for `/agent/{id}`,
  registry lookups and SQL result membership, whatever the agent calls itself.
"""

from __future__ import annotations

import pytest

from proto import anyvalue_pb2, opamp_pb2
from server import agent_query
from server.main import _handle_agent_message
from server.state import AGENT_REGISTRY, AgentState

UID = bytes.fromhex("aa" * 16)
AGENT_ID = "aa" * 16
OTHER_UID = bytes.fromhex("bb" * 16)
OTHER_ID = "bb" * 16


def describe(uid: bytes, *, name=None, where="non_identifying", environment=None) -> None:
    """One agent message, with (or without) a declared `agent.name`."""
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = True
    target = (
        msg.agent_description.identifying_attributes
        if where == "identifying"
        else msg.agent_description.non_identifying_attributes
    )
    if name is not None:
        target.add(key="agent.name", value=anyvalue_pb2.AnyValue(string_value=name))
    if environment is not None:
        target.add(key="environment", value=anyvalue_pb2.AnyValue(string_value=environment))
    _handle_agent_message(msg.SerializeToString())


def state(name=None, *, where="non_identifying", agent_id=AGENT_ID) -> AgentState:
    """A hand-built state whose description carries (or does not carry) a name."""
    attributes = [{"key": "service.name", "value": {"stringValue": "collector"}}]
    if name is not None:
        attributes.append({"key": "agent.name", "value": name})
    entry = {"identifyingAttributes": [], "nonIdentifyingAttributes": []}
    entry["identifyingAttributes" if where == "identifying" else "nonIdentifyingAttributes"] = (
        attributes
    )
    return AgentState(
        instance_uid=bytes.fromhex(agent_id),
        agent_id=agent_id,
        description=entry,
    )


# ------------------------------------------------------------------ the property


def test_the_declared_name_is_read_from_both_attribute_lists():
    """The fixtures declare it as non-identifying, but it is a free-form key.

    `get_attr` looks in both lists because nothing stops an agent putting it under
    `identifying_attributes`; a name that only worked in one place would be a rule the
    agent cannot discover.
    """
    assert state({"stringValue": "e2e-collector"}).name == "e2e-collector"
    assert (
        state({"stringValue": "e2e-collector"}, where="identifying").name
        == "e2e-collector"
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),  # the agent declared no name at all
        ({"stringValue": "   "}, None),  # declared, but blank
        ({"stringValue": ""}, None),
        ({"stringValue": "  e2e-collector  "}, "e2e-collector"),  # trimmed
        ({"kvlistValue": {"values": []}}, None),  # a map is not a name
    ],
)
def test_a_name_that_is_not_a_name_is_none(value, expected):
    """Blank and non-scalar values are "no name", not "" and not a rendered dict."""
    assert state(value).name == expected


def test_the_name_never_changes_the_identity():
    """The field is additive: `id` is still the uid, and the registry is keyed on it."""
    agent = state({"stringValue": "e2e-collector"})
    payload = agent.to_dict()
    assert payload["name"] == "e2e-collector"
    assert payload["id"] == AGENT_ID
    AGENT_REGISTRY.register(AGENT_ID, agent, persist=False)
    assert AGENT_REGISTRY.get("e2e-collector") is None
    assert AGENT_REGISTRY.get(AGENT_ID) is agent


def test_no_declared_name_is_null_in_the_api_shape():
    """`null` rather than a truncated id: "this agent declared none" is a fact."""
    payload = state().to_dict()
    assert payload["name"] is None
    assert payload["id"] == AGENT_ID


# ------------------------------------------------------------------------ the API


def test_the_api_returns_the_name_and_keeps_addressing_by_id(client):
    describe(UID, name="e2e-collector", environment="ci")

    listing = client.get("/agents").json()
    entry = next(a for a in listing["agents"] if a["id"] == AGENT_ID)
    assert entry["name"] == "e2e-collector"

    detail = client.get(f"/agent/{AGENT_ID}").json()
    assert detail["name"] == "e2e-collector"


def test_an_agent_without_a_name_is_still_listed(client):
    describe(UID)

    entry = next(a for a in client.get("/agents").json()["agents"] if a["id"] == AGENT_ID)
    assert entry["name"] is None


def test_the_existing_attribute_filter_can_filter_by_name(client):
    describe(UID, name="e2e-collector")
    describe(OTHER_UID, name="other-collector")

    selected = client.get("/agents", params={"agent.name": "e2e-collector"}).json()
    assert selected["count"] == 1
    assert selected["agents"][0]["id"] == AGENT_ID
    assert selected["agents"][0]["name"] == "e2e-collector"


# ------------------------------------------------------------------------ the SQL


def registered():
    return [
        state({"stringValue": "e2e-collector"}, agent_id=AGENT_ID),
        state({"stringValue": "e2e-collector"}, agent_id=OTHER_ID),
        state(agent_id="cc" * 16),
    ]


def test_name_is_a_core_column_so_a_selection_does_not_need_the_dotted_spelling():
    result = agent_query.run(
        "SELECT id, name FROM agents WHERE name = 'e2e-collector'", registered()
    )
    assert {row["id"] for row in result["rows"]} == {AGENT_ID, OTHER_ID}


def test_agent_name_is_an_alias_and_an_unnamed_agent_is_null_not_empty():
    result = agent_query.run(
        "SELECT id, agent_name, name FROM agents WHERE name IS NULL", registered()
    )
    assert [row["id"] for row in result["rows"]] == ["cc" * 16]
    assert result["rows"][0]["name"] is None

    alias = agent_query.run(
        "SELECT id FROM agents WHERE agent_name = 'e2e-collector'", registered()
    )
    assert {row["id"] for row in alias["rows"]} == {AGENT_ID, OTHER_ID}


def test_a_name_still_resolves_back_to_ids_not_to_names():
    """The result is membership: a duplicated name selects two agents, not one."""
    result = agent_query.run("SELECT * FROM agents WHERE name = 'e2e-collector'", registered())
    assert len(result["agents"]) == 2
    assert {agent.agent_id for agent in result["agents"]} == {AGENT_ID, OTHER_ID}


def test_the_schema_suggests_the_column_the_live_names_and_an_example():
    payload = agent_query.schema(registered())
    columns = {column["name"]: column for column in payload["columns"]}
    assert columns["name"]["kind"] == "core"
    assert columns["name"]["values"] == ["e2e-collector"]
    assert columns["agent_name"]["values"] == ["e2e-collector"]
    assert "SELECT * FROM agents WHERE name = 'e2e-collector'" in payload["examples"]


def test_the_schema_does_not_invent_a_name_example_when_nobody_declared_one():
    payload = agent_query.schema([state(agent_id="cc" * 16)])
    assert not [example for example in payload["examples"] if " name = " in example]
