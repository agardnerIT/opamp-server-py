"""Desired config: hashing, validation, storage and the delivery decision (#81).

The acceptance criteria this file exists for:

* push config → the agent's next reply carries `remote_config` with our hash;
* push again with no change → no second `remote_config` (no re-push loop);
* the agent reports `FAILED` → surfaced, and never retried;
* a restart mid-cycle does not duplicate a push (the hash is persisted);
* withdraw → an empty config map, delivered once;
* multiple objects and non-empty keys are supported;
* feature disabled by default.
"""

from __future__ import annotations

import pytest

from proto import opamp_pb2
from server import crypto, remote_config
from server.state import SQLiteDesiredConfigStore

AGENT = "ab" * 16
YAML = "receivers:\n  otlp:\n    protocols:\n      grpc:\n"

# A complete collector config: `service` present, the `opamp` extension kept and
# pointing at this server, memory_limiter + batch in the pipeline. That is what
# passes the #100 pipeline without warnings.
FULL_YAML = """receivers:
  otlp:
    protocols:
      grpc:
exporters:
  debug:
processors:
  memory_limiter:
    check_interval: 1s
  batch: {}
service:
  extensions: [opamp]
  telemetry:
    logs:
      level: info
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug]
extensions:
  opamp:
    server:
      ws:
        endpoint: wss://opamp.example.com/v1/opamp
"""


@pytest.fixture(autouse=True)
def _push_on(monkeypatch, tmp_path):
    """A posture where push is allowed, with encryption on (#96/#98)."""
    from server import admin_auth

    monkeypatch.setattr(admin_auth, "ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("OPAMP_CONFIG_PUSH_ENABLED", "true")
    monkeypatch.setenv("OPAMP_AGENT_AUTH", "token")
    monkeypatch.setenv("PUBLIC_URL", "https://opamp.example.com")
    monkeypatch.setenv("CONFIG_AT_REST_KEY", crypto.generate_key())
    monkeypatch.delenv("OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE", raising=False)
    crypto.reset_key_cache()
    yield
    crypto.reset_key_cache()


def _map(body=YAML, content_type="text/yaml"):
    return {"": {"body": body, "content_type": content_type}}


# ---------------------------------------------------------------------- hashing


def test_hash_is_stable_and_hex():
    digest = remote_config.compute_hash(_map())
    assert digest == remote_config.compute_hash(_map())
    assert len(digest) == 64
    int(digest, 16)


def test_hash_is_independent_of_dict_order():
    a = {"one": {"body": "a"}, "two": {"body": "b"}}
    b = {"two": {"body": "b"}, "one": {"body": "a"}}
    assert remote_config.compute_hash(a) == remote_config.compute_hash(b)


def test_hash_is_length_prefixed_so_concatenations_cannot_collide():
    assert remote_config.compute_hash({"ab": {"body": "c"}}) != remote_config.compute_hash(
        {"a": {"body": "bc"}}
    )
    assert remote_config.compute_hash({"a": {"body": "bc", "content_type": ""}}) != (
        remote_config.compute_hash({"a": {"body": "b", "content_type": "c"}})
    )


def test_hash_changes_with_content_and_keys():
    base = remote_config.compute_hash(_map())
    assert remote_config.compute_hash(_map(body=YAML + "\n")) != base
    assert remote_config.compute_hash({"x": {"body": YAML}}) != base


def test_hash_wire_forms_round_trip():
    digest = remote_config.compute_hash(_map())
    b64 = remote_config.hash_b64(digest)
    assert remote_config.hash_from_wire(b64) == digest
    assert remote_config.hash_from_wire(None) is None
    assert remote_config.hash_from_wire("not base64!!") is None


# ------------------------------------------------------------------- validation


def test_valid_config_map_normalises_and_warns_nothing():
    normalised, warnings = remote_config.validate_config_map(_map(body=FULL_YAML))
    assert normalised == {"": {"body": FULL_YAML, "content_type": "text/yaml"}}
    assert warnings == []


def test_a_fragment_only_warns_about_the_missing_service_section():
    """A body without `service` is a fragment: we cannot tell if it is the whole config.

    #100's structural and protected-invariant checks need a runnable config to check,
    so they are skipped with a warning rather than guessed at.
    """
    normalised, warnings = remote_config.validate_config_map(_map())
    assert normalised == {"": {"body": YAML, "content_type": "text/yaml"}}
    assert len(warnings) == 1
    assert "no `service` section" in warnings[0]


def test_multiple_objects_and_non_empty_keys_are_supported():
    payload = {
        "config.yaml": {"body": YAML, "content_type": "text/yaml"},
        "": {"body": "exporters: {}", "content_type": "text/yaml"},
    }
    normalised, _ = remote_config.validate_config_map(payload)
    assert set(normalised) == {"config.yaml", ""}
    assert len(remote_config.build_config_map(normalised).config_map) == 2


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ("not a dict", "must be an object"),
        ({}, "config_map is empty"),
        ({"": "nope"}, "expected an object"),
        ({"": {"content_type": "text/yaml"}}, "'body' is required"),
        ({"": {"body": 42}}, "'body' is required"),
        ({"": {"body": "x", "content_type": 1}}, "'content_type' must be a string"),
        ({"": {"body": "x", "role": 1}}, "'role' must be a string"),
        ({"": {"body": "x", "role": "r" * 5000}}, "over MAX_ROLE_CHARS"),
    ],
)
def test_invalid_shapes_are_rejected_with_a_reason(payload, fragment):
    with pytest.raises(remote_config.ConfigValidationError) as exc:
        remote_config.validate_config_map(payload)
    assert any(fragment in error for error in exc.value.errors)


def test_all_errors_are_reported_at_once():
    with pytest.raises(remote_config.ConfigValidationError) as exc:
        remote_config.validate_config_map(
            {"a": {"body": 1, "role": "r"}, "b": {"content_type": "text/yaml"}}
        )
    assert len(exc.value.errors) == 2


def test_role_is_carried_through_normalisation_hash_and_wire():
    """v0.20.0 `AgentConfigObject.role` survives every step of the push pipeline."""
    with_role = {"raw": {"body": "a: 1\n", "content_type": "text/yaml", "role": "raw"}}
    normalised, _ = remote_config.validate_config_map(with_role)
    assert normalised["raw"]["role"] == "raw"

    # It reaches the agent, or the approver approved an object nobody receives.
    wire = remote_config.build_config_map(normalised).config_map["raw"]
    assert (wire.role, wire.content_type, wire.body) == ("raw", "text/yaml", b"a: 1\n")

    # ...and it changes the hash: two objects with the same body but different roles
    # are different configurations.
    bare = {"raw": {"body": "a: 1\n", "content_type": "text/yaml"}}
    assert remote_config.compute_hash(normalised) != remote_config.compute_hash(bare)


def test_a_role_less_map_keeps_the_hash_it_had_before_role_existed():
    """Upgrade safety: stored revisions must not all look changed and be re-pushed.

    `role` is appended to the digest only when it is set, so every revision pushed
    before v0.20.0 keeps its hash. The literal below was produced by the pre-#68
    implementation; if this changes, agents on the old hash will be re-sent configs
    after an upgrade.
    """
    legacy = {"": {"body": "receivers:\n", "content_type": "text/yaml"}}
    assert remote_config.compute_hash(legacy) == (
        "cc6b77c36cc412c5dc1ac52c815b25384be3328e2e888403e2450c7fe2ea8fe9"
    )
    # An explicit empty role is still role-less.
    assert remote_config.compute_hash(
        {"": {"body": "receivers:\n", "content_type": "text/yaml", "role": ""}}
    ) == remote_config.compute_hash(legacy)


def test_yaml_that_does_not_parse_is_refused():
    """A body that will not parse leaves the collector down until a manual restart."""
    with pytest.raises(remote_config.ConfigValidationError) as exc:
        remote_config.validate_config_map(_map(body="receivers: [unclosed"))
    assert "not valid" in exc.value.errors[0]


def test_yaml_without_a_declared_type_only_warns():
    normalised, warnings = remote_config.validate_config_map(
        _map(body="receivers: [unclosed", content_type="")
    )
    assert warnings and "not valid" in warnings[0]


def test_oversized_config_is_refused(monkeypatch):
    monkeypatch.setenv("CONFIG_MAX_BYTES", "10")
    with pytest.raises(remote_config.ConfigValidationError) as exc:
        remote_config.validate_config_map(_map(body="x" * 11))
    assert "over CONFIG_MAX_BYTES" in exc.value.errors[0]


def test_alias_bomb_shape_is_refused():
    bomb = "\n".join(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 12))
    with pytest.raises(remote_config.ConfigValidationError) as exc:
        remote_config.validate_config_map(_map(body=bomb))
    assert "alias" in exc.value.errors[0]


def test_a_redacted_body_can_never_be_pushed():
    """The single most dangerous foot-gun: a redacted push wipes the fleet."""
    with pytest.raises(remote_config.ConfigValidationError) as exc:
        remote_config.validate_config_map(
            _map(body="exporters:\n  x:\n    password: <redacted>\n")
        )
    assert "redaction marker" in exc.value.errors[0]


def test_an_empty_map_is_allowed_only_for_withdrawal():
    with pytest.raises(remote_config.ConfigValidationError):
        remote_config.validate_config_map({})
    assert remote_config.validate_config_map({}, allow_empty=True) == ({}, [])


# ------------------------------------------------------------------ store/cache


def test_push_creates_a_revision_and_updates_the_cache():
    record = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    assert record["revision_id"].startswith("rev-")
    assert record["author"] == "alice"
    cached = remote_config.REGISTRY.get(AGENT)
    assert cached["config_map"] == _map()
    assert cached["config_hash"] == remote_config.compute_hash(_map())


def test_a_revision_is_immutable_and_the_previous_one_is_kept():
    first = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    second = remote_config.REGISTRY.push(AGENT, _map(body="exporters: {}"), "bob")
    assert first["revision_id"] != second["revision_id"]
    previous = remote_config.REGISTRY._store.previous(AGENT)
    assert previous["revision_id"] == first["revision_id"]


def test_rollback_creates_a_new_revision_with_the_old_content():
    first = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    remote_config.REGISTRY.push(AGENT, _map(body="exporters: {}"), "bob")
    rolled = remote_config.REGISTRY.rollback(AGENT, "carol")
    assert rolled is not None
    assert rolled["revision_id"] not in (first["revision_id"],)
    assert rolled["config_hash"] == first["config_hash"]
    assert rolled["author"] == "carol"
    assert remote_config.REGISTRY.get(AGENT)["config_map"] == _map()


def test_rollback_without_a_previous_revision_returns_none():
    remote_config.REGISTRY.push(AGENT, _map(), "alice")
    assert remote_config.REGISTRY.rollback(AGENT, "carol") is None


def test_withdraw_pushes_an_empty_map():
    remote_config.REGISTRY.push(AGENT, _map(), "alice")
    record = remote_config.REGISTRY.withdraw(AGENT, "alice")
    assert record["withdrawn"] is True
    assert remote_config.REGISTRY.get(AGENT)["config_map"] == {}
    assert record["config_hash"] == remote_config.compute_hash({})


def test_state_survives_a_restart(tmp_path):
    """Acceptance criterion: a restart mid-cycle must not duplicate a push."""
    db = tmp_path / "config.db"
    first = remote_config.DesiredConfigRegistry(SQLiteDesiredConfigStore(db))
    record = first.push(AGENT, _map(), "alice")

    reloaded = remote_config.DesiredConfigRegistry(SQLiteDesiredConfigStore(db))
    assert reloaded.get(AGENT)["revision_id"] == record["revision_id"]
    assert reloaded.get(AGENT)["config_hash"] == record["config_hash"]


def test_an_undecryptable_revision_is_ignored_not_fatal(tmp_path, monkeypatch):
    db = tmp_path / "config.db"
    registry = remote_config.DesiredConfigRegistry(SQLiteDesiredConfigStore(db))
    registry.push(AGENT, _map(), "alice")

    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())
    crypto.reset_key_cache()
    reloaded = remote_config.DesiredConfigRegistry(SQLiteDesiredConfigStore(db))
    assert reloaded.get(AGENT) is None
    assert reloaded.count() == 0


# --------------------------------------------------------------------- delivery


def _caps(accepts=True):
    caps = 0
    if accepts:
        caps |= opamp_pb2.AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
    return caps


def test_no_offer_when_push_is_disallowed(monkeypatch):
    remote_config.REGISTRY.push(AGENT, _map(), "alice")
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED")
    assert remote_config.offer_for(AGENT, _caps()) is None


def test_no_offer_without_the_agent_capability():
    remote_config.REGISTRY.push(AGENT, _map(), "alice")
    assert remote_config.offer_for(AGENT, _caps(accepts=False)) is None


def test_no_offer_without_a_revision():
    assert remote_config.offer_for(AGENT, _caps()) is None


def test_offer_carries_the_config_and_our_hash():
    record = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    offer = remote_config.offer_for(AGENT, _caps())
    assert offer is not None
    assert offer.config_hash == bytes.fromhex(record["config_hash"])
    assert offer.config.config_map[""].body == YAML.encode()
    assert offer.config.config_map[""].content_type == "text/yaml"


def test_no_second_offer_when_the_agent_reports_our_hash():
    record = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    status = {"lastRemoteConfigHash": remote_config.hash_b64(record["config_hash"])}
    assert remote_config.offer_for(AGENT, _caps(), status) is None


def test_a_different_reported_hash_gets_an_offer():
    record = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    status = {"lastRemoteConfigHash": remote_config.hash_b64("00" * 32)}
    offer = remote_config.offer_for(AGENT, _caps(), status)
    assert offer is not None
    assert offer.config_hash == bytes.fromhex(record["config_hash"])


def test_withdrawal_is_delivered_once_as_an_empty_map():
    remote_config.REGISTRY.push(AGENT, _map(), "alice")
    withdrawn = remote_config.REGISTRY.withdraw(AGENT, "alice")

    offer = remote_config.offer_for(AGENT, _caps(), {"lastRemoteConfigHash": None})
    assert offer is not None
    assert len(offer.config.config_map) == 0

    # The agent applies it and echoes our hash: nothing more to send.
    assert (
        remote_config.offer_for(
            AGENT, _caps(), {"lastRemoteConfigHash": remote_config.hash_b64(withdrawn["config_hash"])}
        )
        is None
    )


# ---------------------------------------------------------------------- failure


def test_failed_for_our_revision_poisons_it():
    record = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    assert record["revision_id"]
    row = remote_config.note_status(
        AGENT,
        "FAILED",
        remote_config.hash_b64(record["config_hash"]),
        "collector refused the config",
    )
    assert row is not None
    assert row["action"] == "config.apply.failed"
    assert remote_config.REGISTRY.get(AGENT)["failed_at"]

    # Poisoned: never retried, even though the hashes still differ (D6).
    assert remote_config.offer_for(AGENT, _caps(), {"lastRemoteConfigHash": None}) is None


def test_failed_for_a_different_revision_is_not_ours_to_punish():
    remote_config.REGISTRY.push(AGENT, _map(), "alice")
    assert (
        remote_config.note_status(AGENT, "FAILED", remote_config.hash_b64("11" * 32), "other")
        is None
    )
    assert remote_config.REGISTRY.get(AGENT)["failed_at"] is None
    assert remote_config.offer_for(AGENT, _caps(), {"lastRemoteConfigHash": None}) is not None


def test_applied_status_is_not_an_event():
    record = remote_config.REGISTRY.push(AGENT, _map(), "alice")
    assert record["revision_id"]
    assert (
        remote_config.note_status(
            AGENT, "APPLIED", remote_config.hash_b64(record["config_hash"])
        )
        is None
    )


def test_failed_without_a_known_revision_is_ignored():
    assert remote_config.note_status(AGENT, "FAILED", None) is None


def test_parse_status_reads_a_message_to_dict_shape():
    status, digest, error = remote_config.parse_status(
        {"remoteConfigStatus": {"status": "FAILED", "lastRemoteConfigHash": "abc=", "errorMessage": "boom"}}
    )
    assert status == "FAILED"
    assert digest == "abc="
    assert error == "boom"
    assert remote_config.parse_status({}) == (None, None, "")


# ------------------------------------------------------------------- retention


def test_prune_keeps_the_current_revision_body(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_RETENTION_DAYS", "30")
    store = SQLiteDesiredConfigStore(tmp_path / "config.db")
    registry = remote_config.DesiredConfigRegistry(store)
    monkeypatch.setattr(remote_config, "REGISTRY", registry)

    registry.push(AGENT, _map(), "alice")
    registry.push(AGENT, _map(body="exporters: {}"), "alice")
    assert registry.prune() == 0  # nothing is old enough yet

    monkeypatch.setattr(remote_config, "retention_days", lambda: 0)
    assert registry.prune() == 0  # 0 disables retention

    monkeypatch.setattr(remote_config, "retention_days", lambda: 30)
    store.prune("2999-01-01T00:00:00+00:00")  # simulate age
    assert registry.get(AGENT) is not None  # current body survived


def test_agent_id_normalisation():
    assert remote_config.normalize_agent_id(AGENT.upper()) == AGENT
    with pytest.raises(ValueError):
        remote_config.normalize_agent_id("not-hex")
    with pytest.raises(ValueError):
        remote_config.normalize_agent_id("ab" * 15)
