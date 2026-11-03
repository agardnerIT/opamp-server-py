"""Collector groups (#111): a saved selector, resolved at use time.

Every test here maps to an acceptance criterion on the issue, and the shape of the
file follows the three rules that make the feature safe:

* **membership is never stored**, so a group cannot drift from the fleet (and a
  group-scoped push resolves to exactly what the equivalent `filter` would);
* **the definition that was approved is snapshotted onto the revision**, so editing a
  group changes future pushes only;
* **the dangerous cases are refused by default**: a health-defined group, a group that
  shrank silently, a push to an empty group, an archived group.

The guards are tested through the API because that is where an operator meets them —
a guard only the control-plane module enforces is a guard a route can forget.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from proto import anyvalue_pb2, opamp_pb2
from server import (
    admin_auth,
    agent_auth,
    config_control,
    crypto,
    groups,
    remote_config,
    rollout,
)
from server.main import _handle_agent_message, app
from server.state import AGENT_REGISTRY, COLLECTOR_GROUPS, SQLiteGroupStore, utcnow
from tests.conformance.harness import handle_message as reply

ADMIN = "admin-pw-1234567890"
UID = bytes.fromhex("ab" * 16)
AGENT_ID = "ab" * 16
OTHER_UID = bytes.fromhex("cd" * 16)
OTHER_ID = "cd" * 16
THIRD_UID = bytes.fromhex("ef" * 16)
THIRD_ID = "ef" * 16

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


def admin_headers() -> dict:
    token = base64.b64encode(f":{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def describe(uid: bytes, env: str = "prod", region: str = "emea") -> None:
    """Register an agent with the two attributes the fixtures filter on."""
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    # A health-defined group matches on this, so the fixture has to be healthy.
    msg.health.healthy = True
    msg.agent_description.identifying_attributes.add(
        key="service.name",
        value=anyvalue_pb2.AnyValue(string_value=f"collector-{env}-{region}"),
    )
    msg.agent_description.non_identifying_attributes.add(
        key="environment", value=anyvalue_pb2.AnyValue(string_value=env)
    )
    msg.agent_description.non_identifying_attributes.add(
        key="region", value=anyvalue_pb2.AnyValue(string_value=region)
    )
    _handle_agent_message(msg.SerializeToString())


def create(client, name="prod-emea", definition=None, **extra):
    body = {
        "name": name,
        "definition": definition if definition is not None else {"environment": "prod"},
    }
    body.update(extra)
    return client.post("/groups", json=body, headers=admin_headers())


def propose(client, scope, **extra):
    body = {"config_map": CONFIG, "scope": scope}
    body.update(extra)
    return client.post("/config/revisions", json=body, headers=admin_headers())


@pytest.fixture(autouse=True)
def secure_posture(monkeypatch, tmp_path):
    """Push enabled, secure posture, no grace window — the posture a group is for."""
    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", ADMIN)
    monkeypatch.setattr(admin_auth, "PASSWORD_SOURCE", "env")
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.setenv("CONFIG_REVERT_GRACE_SECONDS", "0")
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    crypto.reset_key_cache()
    config_control.reset_drift_alerts()
    yield
    config_control.reset_drift_alerts()
    crypto.reset_key_cache()


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------- create and read


def test_a_group_survives_a_restart_and_reports_a_live_member_count(client, tmp_path):
    describe(UID, "prod")
    created = create(client)
    assert created.status_code == 200, created.text
    body = created.json()
    group_id = body["group_id"]
    assert body["member_count"] == 1, "membership is resolved live, not stored"

    # A second store instance over the same file is what a restart looks like.
    reopened = SQLiteGroupStore(COLLECTOR_GROUPS.db_path)
    assert reopened.get(group_id)["name"] == "prod-emea"

    describe(OTHER_UID, "prod")
    listing = client.get("/groups", headers=admin_headers()).json()
    assert listing["count"] == 1
    assert listing["groups"][0]["member_count"] == 2, "the count follows the fleet"


def test_a_group_resolves_to_exactly_what_the_equivalent_filter_resolves_to(client):
    """The one test that keeps the two target paths from diverging (#111).

    `--group prod-emea` must hit the same agents as
    `--attr environment=prod --attr region=emea`; if it ever does not, the name is
    worse than typing the filter.
    """
    describe(UID, "prod", "emea")
    describe(OTHER_UID, "prod", "apac")  # right environment, wrong region
    describe(THIRD_UID, "staging", "emea")  # right region, wrong environment
    group = create(client, definition={"environment": "prod", "region": "emea"}).json()

    by_group = propose(client, {"group": group["group_id"]})
    by_filter = propose(
        client, {"filter": {"environment": "prod", "region": "emea"}}
    )
    assert by_group.status_code == 200, by_group.text
    assert by_filter.status_code == 200, by_filter.text
    resolved_group = sorted(
        revision["agent_id"] for revision in by_group.json()["revisions"]
    )
    resolved_filter = sorted(
        revision["agent_id"] for revision in by_filter.json()["revisions"]
    )
    assert resolved_group == resolved_filter == [AGENT_ID]
    # The group scope records what the name meant, not just which name it was.
    scope = by_group.json()["scope"]
    assert scope["group_name"] == "prod-emea"
    assert scope["group_snapshot"]["definition"] == {
        "environment": "prod",
        "region": "emea",
    }


def test_include_forces_an_agent_in_and_exclude_forces_one_out(client):
    describe(UID, "prod")
    describe(OTHER_UID, "staging")  # fails the definition
    describe(THIRD_UID, "prod")  # passes it
    group = create(
        client,
        include=[OTHER_ID],
        exclude=[THIRD_ID],
    ).json()
    assert sorted(group["members"]) == sorted([AGENT_ID, OTHER_ID])
    assert group["sources"]["included"] == [OTHER_ID]
    assert group["sources"]["excluded"] == [THIRD_ID]

    proposed = propose(client, {"group": "prod-emea"})
    assert proposed.status_code == 200, proposed.text
    assert sorted(row["agent_id"] for row in proposed.json()["revisions"]) == sorted(
        [AGENT_ID, OTHER_ID]
    )


def test_attribute_keys_come_from_live_agents_and_never_from_a_config_body(client):
    describe(UID, "prod")
    describe(OTHER_UID, "staging")
    keys = client.get("/groups/attribute-keys", headers=admin_headers()).json()
    by_key = {bucket["key"]: bucket for bucket in keys["keys"]}
    assert set(by_key) >= {"environment", "region", "service.name"}
    assert by_key["environment"]["values"] == ["prod", "staging"]
    assert keys["reserved"] == ["healthy", "remote_config_status", "status"]
    # A description harvest must never smuggle a config body into a listing (D8).
    assert "body" not in keys and "config_map" not in keys


# ------------------------------------------------------- editing and snapshots


def test_editing_a_group_changes_future_pushes_only(client):
    describe(UID, "prod")
    describe(OTHER_UID, "staging")
    create(client)
    first = propose(client, {"group": "prod-emea"}).json()
    proposal_id = first["proposal_id"]

    client.put(
        "/groups/prod-emea",
        json={"definition": {"environment": "staging"}},
        headers=admin_headers(),
    )

    # The approved-against revision still reports the old definition *and* the old
    # member list, and says the name no longer means that.
    revision = client.get(
        f"/config/revisions/{proposal_id}", headers=admin_headers()
    ).json()["revisions"][0]
    assert revision["group_snapshot"]["definition"] == {"environment": "prod"}
    assert revision["scope"]["resolved_agents"] == [AGENT_ID]
    assert revision["group_definition_changed"] is True

    # A new proposal uses the new definition. The membership moved, so the shrink
    # guard asks for the acknowledgement it would ask from any other operator.
    second = propose(client, {"group": "prod-emea"}, confirm_scope_change=True)
    assert second.status_code == 200, second.text
    assert [row["agent_id"] for row in second.json()["revisions"]] == [OTHER_ID]


def test_the_update_audit_row_carries_the_definition_diff(client):
    create(client)
    response = client.put(
        "/groups/prod-emea",
        json={"definition": {"environment": "prod", "region": "emea"}},
        headers=admin_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["changes"]["definition"] == {
        "added": {"region": "emea"},
        "removed": {},
        "changed": {},
    }

    entries = client.get(
        "/audit", params={"action": "group.update"}, headers=admin_headers()
    ).json()
    detail = json.loads(entries["entries"][0]["detail_json"])
    assert detail["changes"]["definition"]["added"] == {"region": "emea"}, (
        "the audit row must record what changed, not just that something did"
    )


def test_a_rollout_still_targets_the_agents_it_resolved_at_creation(client):
    describe(UID, "prod")
    describe(OTHER_UID, "prod")
    create(client)
    proposal_id = propose(client, {"group": "prod-emea"}).json()["proposal_id"]
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"canary_percent": 0, "max_agents_per_batch": 1},
        headers=admin_headers(),
    )
    assert created.status_code == 200, created.text
    rollout_id = created.json()["rollout_id"]

    # A third matching agent appears mid-rollout: the plan is stored, not recomputed.
    describe(THIRD_UID, "prod")
    detail = client.get(f"/config/rollouts/{rollout_id}", headers=admin_headers()).json()
    assert detail["agents"]["total"] == 2
    planned = {aid for batch in detail["batches"] for aid in batch["agent_ids"]}
    assert THIRD_ID not in planned, (
        "a rollout never re-resolves a group after approval; the plan is stored"
    )


# ------------------------------------------------------------- guardrails (#111)


def test_a_health_defined_group_is_refused_without_a_stored_resolution(client):
    describe(UID, "prod")
    describe(OTHER_UID, "prod")
    create(client, definition={"healthy": "true"})
    describe(OTHER_UID, "prod")  # re-report, still healthy

    refused = propose(client, {"group": "prod-emea"})
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "healthy" in detail["message"]
    assert detail["hint"].startswith("a health-based group")

    # With a recorded membership it is a deliberate push to a stated set of agents.
    previewed = client.post("/groups/prod-emea/preview", headers=admin_headers()).json()
    assert previewed["count"] == 2
    allowed = propose(
        client, {"group": "prod-emea", "use_stored_resolution": True}
    )
    assert allowed.status_code == 200, allowed.text
    assert sorted(row["agent_id"] for row in allowed.json()["revisions"]) == sorted(
        [AGENT_ID, OTHER_ID]
    )


def test_a_shrinking_group_requires_the_dropped_diff_to_be_acknowledged(client):
    describe(UID, "prod")
    describe(OTHER_UID, "prod")
    create(client)
    assert client.post(
        "/groups/prod-emea/preview", headers=admin_headers()
    ).json()["count"] == 2

    # The attribute is renamed on one host: the group silently resolves smaller.
    describe(OTHER_UID, "PRODUCTION")
    refused = propose(client, {"group": "prod-emea"})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["dropped"] == [OTHER_ID]
    assert detail["added"] == []
    assert "confirm_scope_change" in detail["hint"]

    confirmed = propose(client, {"group": "prod-emea"}, confirm_scope_change=True)
    assert confirmed.status_code == 200, confirmed.text
    assert [row["agent_id"] for row in confirmed.json()["revisions"]] == [AGENT_ID]


def test_an_empty_group_is_valid_to_create_and_preview_but_not_to_push(client):
    describe(UID, "prod")
    created = create(client, name="future-apac", definition={"region": "apac"})
    assert created.status_code == 200, created.text
    assert created.json()["member_count"] == 0

    preview = client.post("/groups/future-apac/preview", headers=admin_headers())
    assert preview.status_code == 200
    assert preview.json()["count"] == 0

    pushed = propose(client, {"group": "future-apac"})
    assert pushed.status_code == 404
    assert "no members" in pushed.json()["detail"]["message"]


def test_an_archived_group_is_hidden_refuses_edits_and_pushes_and_can_be_restored(client):
    describe(UID, "prod")
    created = create(client).json()
    archived = client.delete("/groups/prod-emea", headers=admin_headers())
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    assert client.get("/groups", headers=admin_headers()).json()["count"] == 0
    assert (
        client.get("/groups", params={"include_archived": True}, headers=admin_headers())
        .json()["count"]
        == 1
    )
    refused_push = propose(client, {"group": "prod-emea"})
    assert refused_push.status_code == 409
    refused_edit = client.put(
        "/groups/prod-emea", json={"owner": "someone"}, headers=admin_headers()
    )
    assert refused_edit.status_code == 409
    # Still readable, because revisions and the audit log point at it.
    assert client.get(f"/groups/{created['group_id']}", headers=admin_headers()).status_code == 200

    restored = client.post("/groups/prod-emea/restore", headers=admin_headers())
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert propose(client, {"group": "prod-emea"}).status_code == 200


def test_archiving_is_refused_while_a_live_rollout_uses_the_group(client):
    describe(UID, "prod")
    create(client)
    proposal_id = propose(client, {"group": "prod-emea"}).json()["proposal_id"]
    created = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal_id},
        json={"canary_percent": 0, "max_agents_per_batch": 1},
        headers=admin_headers(),
    )
    assert created.status_code == 200, created.text
    refused = client.delete("/groups/prod-emea", headers=admin_headers())
    assert refused.status_code == 409
    assert refused.json()["detail"]["rollouts"][0]["proposal_id"] == proposal_id


def test_names_are_unique_case_insensitively_and_never_a_reserved_filter_key(client):
    assert create(client, name="prod-emea").status_code == 200
    duplicate = create(client, name="Prod-EMEA")
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]["message"]

    for reserved in ("healthy", "status", "remote_config_status"):
        refused = create(client, name=reserved)
        assert refused.status_code == 422, reserved
        assert "reserved filter key" in refused.json()["detail"]

    for invalid in ("prod-*", "Prod Emea", "-prod", "p", "prod/emea"):
        assert create(client, name=invalid).status_code == 422, invalid

    empty = create(client, name="empty-definition", definition={})
    assert empty.status_code == 422
    assert "whole fleet" in empty.json()["detail"]


def test_a_group_scope_cannot_be_combined_with_the_other_scopes(client):
    describe(UID, "prod")
    create(client)
    for scope in (
        {"group": "prod-emea", "agent_id": AGENT_ID},
        {"group": "prod-emea", "agents": [AGENT_ID]},
        {"group": "prod-emea", "filter": {"environment": "prod"}},
    ):
        response = propose(client, scope)
        assert response.status_code == 400, scope
        assert "cannot be combined" in response.json()["detail"]


def test_a_pin_cannot_be_both_forced_in_and_forced_out(client):
    response = create(client, include=[OTHER_ID], exclude=[OTHER_ID])
    assert response.status_code == 422
    assert "both pinned in and pinned out" in response.json()["detail"]


# ----------------------------------------------------------- break-glass apply


def test_a_group_target_on_config_apply_obeys_the_same_guardrails(client):
    describe(UID, "prod")
    create(client, name="health", definition={"healthy": "true"})
    refused = client.post(
        "/config/apply",
        json={"config_map": CONFIG, "group": "health", "confirm": True},
        headers=admin_headers(),
    )
    assert refused.status_code == 400, refused.text
    assert "healthy" in refused.json()["detail"]["message"]

    allowed = client.post(
        "/config/apply",
        json={
            "config_map": CONFIG,
            "group": "health",
            "use_stored_resolution": True,
            "confirm": True,
        },
        headers=admin_headers(),
    )
    assert allowed.status_code == 409, "no stored resolution yet, so it must refuse"
    assert "no recorded resolution" in allowed.json()["detail"]["message"]

    client.post("/groups/health/preview", headers=admin_headers())
    proved = client.post(
        "/config/apply",
        json={
            "config_map": CONFIG,
            "group": "health",
            "use_stored_resolution": True,
            "confirm": True,
        },
        headers=admin_headers(),
    )
    assert proved.status_code == 200, proved.text
    assert proved.json()["agents"] == 1


# ------------------------------------------------------------------- the store


def test_the_store_refuses_a_definition_that_is_not_a_filter_document():
    with pytest.raises(Exception) as excinfo:
        COLLECTOR_GROUPS.validate_definition({"environment": {"nested": True}})
    assert "scalar" in str(excinfo.value)
    with pytest.raises(Exception):
        COLLECTOR_GROUPS.validate_definition({"environment": None})
    with pytest.raises(Exception):
        COLLECTOR_GROUPS.validate_definition({"environment": []})


def test_resolution_history_is_recorded_and_counts_membership_changes(client):
    describe(UID, "prod")
    create(client)
    client.post("/groups/prod-emea/preview", headers=admin_headers())
    describe(OTHER_UID, "prod")
    preview = client.post("/groups/prod-emea/preview", headers=admin_headers()).json()
    assert preview["added"] == [OTHER_ID]
    assert preview["previous_count"] == 1

    detail = client.get("/groups/prod-emea", headers=admin_headers()).json()
    assert detail["membership_changes"] == 1
    assert detail["last_resolution"]["agent_count"] == 2
    assert len(detail["resolutions"]) == 2


def test_the_membership_swing_threshold_is_configurable(client, monkeypatch):
    """A big swing with no push behind it is the silent-enrolment signal (#111).

    Logged, not alerted: an alert on silent enrolment belongs with the `include_future`
    work (an explicit non-goal here), but *seeing* a swing is what makes a renamed
    attribute findable. The threshold is read per call so an operator can tune it.

    Asserted through a loguru sink rather than pytest's capture: the sink is bound at
    import, so fd-level capture legitimately sees nothing.
    """
    from loguru import logger

    describe(UID, "prod")
    describe(OTHER_UID, "prod")
    create(client)
    client.post("/groups/prod-emea/preview", headers=admin_headers())

    messages = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        monkeypatch.setenv("OPAMP_GROUP_MEMBERSHIP_SWING", "1")
        describe(OTHER_UID, "staging")
        client.post("/groups/prod-emea/preview", headers=admin_headers())
    finally:
        logger.remove(sink)
    assert any("membership moved" in str(record) for record in messages)

    # Above the threshold, the same swing is quiet.
    monkeypatch.setenv("OPAMP_GROUP_MEMBERSHIP_SWING", "50")
    quiet = []
    sink = logger.add(quiet.append, level="WARNING")
    try:
        describe(OTHER_UID, "prod")
        client.post("/groups/prod-emea/preview", headers=admin_headers())
    finally:
        logger.remove(sink)
    assert not any("membership moved" in str(record) for record in quiet)
