"""GET /agents?group=... — use a saved group to filter the agent list (#148).

#132 let an operator search the fleet with SQL and save the query as a group. #148
closes the loop: the group is usable *as* the list filter, through the same
`resolve_members` a group-scoped push runs — so the list and the blast radius can
never disagree about what the group means.

Refusals are tested through the API because that is where an operator meets them:
an unknown group is a 404, an archived group a 409, and `q` + `group` together a
400 (two selectors cannot both be the selector).
"""

from __future__ import annotations

import pytest

from proto import anyvalue_pb2, opamp_pb2
from server import admin_auth, crypto
from server.main import _handle_agent_message
from tests.support import ADMIN, admin_headers, pin_collector

UID = bytes.fromhex("41" * 16)
AGENT_ID = "41" * 16
OTHER_UID = bytes.fromhex("42" * 16)
OTHER_ID = "42" * 16
THIRD_UID = bytes.fromhex("43" * 16)
THIRD_ID = "43" * 16


def describe(uid: bytes, env: str, arch: str, healthy: bool = True) -> None:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = healthy
    for key, value in (("environment", env), ("host_arch", arch)):
        msg.agent_description.non_identifying_attributes.add(
            key=key, value=anyvalue_pb2.AnyValue(string_value=value)
        )
    _handle_agent_message(msg.SerializeToString())
    pin_collector(uid.hex())


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


def create_group(client, name, **body):
    payload = {"name": name}
    payload.update(body)
    return client.post("/groups", json=payload, headers=admin_headers())


def ids_of(response):
    return sorted(a["id"] for a in response.json()["agents"])


# ------------------------------------------------------------------ the selector


def test_a_definition_group_filters_the_list(client):
    describe(UID, "prod", "amd64")
    describe(OTHER_UID, "staging", "amd64")
    created = create_group(client, "prod-agents", definition={"environment": "prod"})
    assert created.status_code == 200, created.text

    response = client.get("/agents", params={"group": "prod-agents"})
    assert response.status_code == 200
    assert ids_of(response) == [AGENT_ID]


def test_a_query_group_filters_the_list(client):
    describe(UID, "prod", "amd64")
    describe(OTHER_UID, "prod", "arm64")
    describe(THIRD_UID, "prod", "arm64")
    created = create_group(
        client, "amd64-prod", query="SELECT * FROM agents WHERE host_arch = 'amd64'"
    )
    assert created.status_code == 200, created.text

    response = client.get("/agents", params={"group": "amd64-prod"})
    assert ids_of(response) == [AGENT_ID]
    body = response.json()
    assert body["group"]["kind"] == "query"
    assert body["group"]["query"].endswith("host_arch = 'amd64'")


def test_the_group_block_names_the_selector_and_the_member_sources(client):
    describe(UID, "prod", "amd64")
    describe(OTHER_UID, "staging", "amd64")
    created = create_group(
        client,
        "prod-plus",
        definition={"environment": "prod"},
        include=[OTHER_ID],
    )
    assert created.status_code == 200, created.text

    body = client.get("/agents", params={"group": "prod-plus"}).json()
    group = body["group"]
    assert group["kind"] == "definition"
    assert group["definition"] == {"environment": "prod"}
    assert group["query"] is None
    assert group["count"] == 2
    assert group["sources"]["definition"] == [AGENT_ID]
    assert group["sources"]["included"] == [OTHER_ID]


def test_exclude_forces_an_agent_out_of_the_list(client):
    describe(UID, "prod", "amd64")
    describe(OTHER_UID, "prod", "arm64")
    created = create_group(
        client, "prod-without", definition={"environment": "prod"}, exclude=[OTHER_ID]
    )
    assert created.status_code == 200, created.text

    response = client.get("/agents", params={"group": "prod-without"})
    assert ids_of(response) == [AGENT_ID]
    assert response.json()["group"]["sources"]["excluded"] == [OTHER_ID]


def test_a_pin_for_an_agent_the_server_has_never_seen_is_visible_not_fatal(client):
    describe(UID, "prod", "amd64")
    created = create_group(
        client, "prod-typos", definition={"environment": "prod"}, include=["deadbeef"]
    )
    assert created.status_code == 200, created.text

    body = client.get("/agents", params={"group": "prod-typos"}).json()
    assert sorted(a["id"] for a in body["agents"]) == [AGENT_ID]
    assert body["group"]["unknown_pins"] == ["deadbeef"]


def test_membership_resolves_live_not_at_create_time(client):
    created = create_group(client, "prod-agents", definition={"environment": "prod"})
    assert created.status_code == 200, created.text

    empty = client.get("/agents", params={"group": "prod-agents"})
    assert empty.status_code == 200
    assert empty.json()["agents"] == []
    assert empty.json()["group"]["count"] == 0

    describe(UID, "prod", "amd64")
    describe(OTHER_UID, "prod", "arm64")
    assert ids_of(client.get("/agents", params={"group": "prod-agents"})) == [
        AGENT_ID,
        OTHER_ID,
    ]


def test_the_group_works_by_id_as_well_as_by_name(client):
    describe(UID, "prod", "amd64")
    created = create_group(client, "prod-agents", definition={"environment": "prod"})
    group_id = created.json()["group_id"]

    by_name = client.get("/agents", params={"group": "prod-agents"})
    by_id = client.get("/agents", params={"group": group_id})
    assert ids_of(by_name) == ids_of(by_id) == [AGENT_ID]
    assert by_id.json()["group"]["group_id"] == group_id


def test_an_empty_group_is_a_200_not_an_error(client):
    """The list is read-only, so an empty result is a valid answer (the push route
    404s an empty group because there is nothing to push to)."""
    created = create_group(client, "nobody", definition={"environment": "nowhere"})
    assert created.status_code == 200, created.text
    response = client.get("/agents", params={"group": "nobody"})
    assert response.status_code == 200
    assert response.json()["count"] == 0


# ------------------------------------------------------------------- precedence


def test_the_group_wins_over_the_ad_hoc_params_and_says_so(client):
    describe(UID, "prod", "amd64")
    describe(OTHER_UID, "prod", "arm64", healthy=False)
    created = create_group(client, "prod-agents", definition={"environment": "prod"})
    assert created.status_code == 200, created.text

    # healthy=true would exclude OTHER only if both filters ran; a group is *the*
    # selector, so the ad-hoc params must be ignored — and not echoed as applied.
    body = client.get("/agents", params={"group": "prod-agents", "healthy": "true"}).json()
    assert sorted(a["id"] for a in body["agents"]) == [AGENT_ID, OTHER_ID]
    assert body["filters"] == {}


def test_q_and_group_together_is_a_400(client):
    describe(UID, "prod", "amd64")
    created = create_group(client, "prod-agents", definition={"environment": "prod"})
    assert created.status_code == 200, created.text

    response = client.get(
        "/agents",
        params={"group": "prod-agents", "q": "SELECT * FROM agents"},
    )
    assert response.status_code == 400
    assert "one selector" in response.text


# --------------------------------------------------------------------- refusals


def test_an_unknown_group_is_a_404_with_the_groups_hint(client):
    response = client.get("/agents", params={"group": "no-such-group"})
    assert response.status_code == 404
    assert "GET /groups" in response.text


def test_an_archived_group_is_a_409_not_a_silent_empty_list(client):
    describe(UID, "prod", "amd64")
    created = create_group(client, "prod-agents", definition={"environment": "prod"})
    group_id = created.json()["group_id"]
    archived = client.delete(f"/groups/{group_id}", headers=admin_headers())
    assert archived.status_code == 200, archived.text

    response = client.get("/agents", params={"group": "prod-agents"})
    assert response.status_code == 409
    assert "archived" in response.text
