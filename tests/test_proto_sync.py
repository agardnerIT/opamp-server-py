"""Proto sync to opamp-spec v0.20.0 (#66).

The generated stubs are checked in, so a wrong regeneration is invisible at
review time — every test still passes while the wire format has silently moved.
These tests pin the things a regeneration can break:

* the proto package (`opamp.proto` → `opamp.proto.v1`, cosmetic on the wire but
  it is the name agents see in `MessageToDict` output and in reflection),
* every pre-existing field number (protobuf renumbers silently: the bytes still
  parse, into the wrong field),
* the byte-for-byte encoding of a v0.18.0-era message, captured before the sync.

`GOLDEN_FIELDS` was generated from the *pre-sync* stubs, so it can only be
updated by someone deliberately acknowledging a wire change.
"""

import json

from google.protobuf.descriptor import FieldDescriptor

from proto import anyvalue_pb2, opamp_pb2

# Captured from proto/opamp_pb2.py at v0.18.0 (commit dbecb98^), message → {field: number}.
GOLDEN_FIELDS = {
    "AgentConfigFile": {"body": 1, "content_type": 2},
    "AgentConfigMap": {"config_map": 1},
    "AgentDescription": {"identifying_attributes": 1, "non_identifying_attributes": 2},
    "AgentDisconnect": {},
    "AgentIdentification": {"new_instance_uid": 1},
    "AgentRemoteConfig": {"config": 1, "config_hash": 2},
    "AgentToServer": {
        "agent_description": 3, "agent_disconnect": 9, "available_components": 14,
        "capabilities": 4, "connection_settings_request": 11,
        "connection_settings_status": 15, "custom_capabilities": 12,
        "custom_message": 13, "effective_config": 6, "flags": 10, "health": 5,
        "instance_uid": 1, "package_statuses": 8, "remote_config_status": 7,
        "sequence_num": 2,
    },
    "AvailableComponents": {"components": 1, "hash": 2},
    "CertificateRequest": {"csr": 1},
    "ComponentDetails": {"metadata": 1, "sub_component_map": 2},
    "ComponentHealth": {
        "component_health_map": 6, "healthy": 1, "last_error": 3,
        "start_time_unix_nano": 2, "status": 4, "status_time_unix_nano": 5,
    },
    "ConnectionSettingsOffers": {
        "hash": 1, "opamp": 2, "other_connections": 6, "own_logs": 5,
        "own_metrics": 3, "own_traces": 4,
    },
    "ConnectionSettingsRequest": {"opamp": 1},
    "ConnectionSettingsStatus": {
        "error_message": 3, "last_connection_settings_hash": 1, "status": 2,
    },
    "CustomCapabilities": {"capabilities": 1},
    "CustomMessage": {"capability": 1, "data": 3, "type": 2},
    "DownloadableFile": {"content_hash": 2, "download_url": 1, "headers": 4, "signature": 3},
    "EffectiveConfig": {"config_map": 1},
    "Header": {"key": 1, "value": 2},
    "Headers": {"headers": 1},
    "OpAMPConnectionSettings": {
        "certificate": 3, "destination_endpoint": 1, "headers": 2,
        "heartbeat_interval_seconds": 4, "proxy": 6, "tls": 5,
    },
    "OpAMPConnectionSettingsRequest": {"certificate_request": 1},
    "OtherConnectionSettings": {
        "certificate": 3, "destination_endpoint": 1, "headers": 2,
        "other_settings": 4, "proxy": 6, "tls": 5,
    },
    "PackageAvailable": {"file": 3, "hash": 4, "type": 1, "version": 2},
    "PackageDownloadDetails": {"download_bytes_per_second": 2, "download_percent": 1},
    "PackageStatus": {
        "agent_has_hash": 3, "agent_has_version": 2, "download_details": 8,
        "error_message": 7, "name": 1, "server_offered_hash": 5,
        "server_offered_version": 4, "status": 6,
    },
    "PackageStatuses": {"error_message": 3, "packages": 1, "server_provided_all_packages_hash": 2},
    "PackagesAvailable": {"all_packages_hash": 2, "packages": 1},
    "ProxyConnectionSettings": {"connect_headers": 2, "url": 1},
    "RemoteConfigStatus": {"error_message": 3, "last_remote_config_hash": 1, "status": 2},
    "RetryInfo": {"retry_after_nanoseconds": 1},
    "ServerErrorResponse": {"error_message": 2, "retry_info": 3, "type": 1},
    "ServerToAgent": {
        "agent_identification": 8, "capabilities": 7, "command": 9,
        "connection_settings": 4, "custom_capabilities": 10, "custom_message": 11,
        "error_response": 2, "flags": 6, "instance_uid": 1,
        "packages_available": 5, "remote_config": 3,
    },
    "ServerToAgentCommand": {"type": 1},
    "TLSCertificate": {"ca_cert": 3, "cert": 1, "private_key": 2},
    "TLSConnectionSettings": {
        "ca_pem_contents": 1, "cipher_suites": 6, "include_system_ca_certs_pool": 2,
        "insecure_skip_verify": 3, "max_version": 5, "min_version": 4,
    },
    "TelemetryConnectionSettings": {
        "certificate": 3, "destination_endpoint": 1, "headers": 2, "proxy": 5, "tls": 4,
    },
}

# v0.19.0 renamed this message; protobuf does not transmit message names, so the
# golden entry is matched against the new name. Nothing else may be renamed.
RENAMED = {"AgentConfigFile": "AgentConfigObject"}

# An AgentToServer a v0.18.0 collector would send, serialised (deterministic) by
# the *pre-sync* stubs: description, health with sub-components, a two-object
# effective config and a remote config status.
GOLDEN_WIRE_HEX = (
    "0a10000102030405060708090a0b0c0d0e0f10071a1b0a190a0c736572766963652e6e616d"
    "6512090a076f74656c636f6c2087102a2a08011100002a36fe9c971722085374617475734f"
    "4b32130a0d72656365697665723a6f746c7012020801323d0a3b0a130a03726177120c0a0a"
    "7261773a20747275650a0a240a0012200a137265636569766572733a0a20206f746c703a0a"
    "1209746578742f79616d6c3a240a2000000000000000000000000000000000000000000000"
    "0000000000000000000010015001"
)


def _fields(message_or_descriptor) -> dict:
    descriptor = getattr(message_or_descriptor, "DESCRIPTOR", message_or_descriptor)
    return {f.name: f.number for f in descriptor.fields}


def test_package_is_v1():
    assert opamp_pb2.AgentToServer.DESCRIPTOR.file.package == "opamp.proto.v1"
    assert anyvalue_pb2.AnyValue.DESCRIPTOR.file.package == "opamp.proto.v1"


def test_messages_resolve_inside_the_package():
    # A mismatched package between anyvalue.proto and opamp.proto shows up here as
    # an unresolvable type rather than as a confusing descriptor-pool error.
    assert anyvalue_pb2.KeyValue.DESCRIPTOR.full_name == "opamp.proto.v1.KeyValue"
    attributes = opamp_pb2.ComponentHealth.DESCRIPTOR.fields_by_name["attributes"]
    assert attributes.message_type.full_name == "opamp.proto.v1.KeyValue"


def test_agent_config_object_exists_with_role():
    fields = _fields(opamp_pb2.AgentConfigObject)
    assert fields == {"body": 1, "content_type": 2, "role": 3}
    assert opamp_pb2.AgentConfigObject(role="raw").role == "raw"


def test_agent_config_file_is_gone():
    # Keeping an alias would make it too easy to write new code against the old
    # name; the rename is the whole point of the sync.
    assert not hasattr(opamp_pb2, "AgentConfigFile")


def test_component_health_attributes_is_field_seven():
    field = opamp_pb2.ComponentHealth.DESCRIPTOR.fields_by_number[7]
    assert field.name == "attributes"
    assert field.is_repeated
    assert field.type == FieldDescriptor.TYPE_MESSAGE


def test_config_map_holds_objects():
    field = opamp_pb2.AgentConfigMap.DESCRIPTOR.fields_by_name["config_map"]
    # A map field's message_type is the synthetic `*Entry` message; its `value`
    # field is what the agent sends for every key.
    value = field.message_type.fields_by_name["value"]
    assert value.message_type.name == "AgentConfigObject"


def test_no_field_was_renumbered():
    current = dict(opamp_pb2.DESCRIPTOR.message_types_by_name)
    for old_name, fields in GOLDEN_FIELDS.items():
        name = RENAMED.get(old_name, old_name)
        assert name in current, f"{name} disappeared from the proto"
        now = _fields(current[name])
        for field, number in fields.items():
            assert field in now, f"{name}.{field} disappeared"
            assert now[field] == number, (
                f"{name}.{field} moved from {number} to {now[field]}: protobuf "
                "renumbers silently, agents keep sending the old number"
            )


def test_only_known_fields_were_added():
    added = {}
    for old_name, fields in GOLDEN_FIELDS.items():
        name = RENAMED.get(old_name, old_name)
        now = _fields(opamp_pb2.DESCRIPTOR.message_types_by_name[name])
        # message_types_by_name yields descriptors, not classes
        extra = {f: n for f, n in now.items() if f not in fields}
        if extra:
            added[name] = extra
    assert added == {"ComponentHealth": {"attributes": 7}, "AgentConfigObject": {"role": 3}}


def test_v018_wire_bytes_still_encode_identically():
    payload = bytes.fromhex(GOLDEN_WIRE_HEX)
    parsed = opamp_pb2.AgentToServer.FromString(payload)

    # The payload carries a two-object config map, which is what a v0.20.0 agent
    # sends too — the old stubs simply had no name for it.
    config_map = parsed.effective_config.config_map.config_map
    assert sorted(config_map) == ["", "raw"]
    assert config_map[""].content_type == "text/yaml"
    assert config_map["raw"].role == ""  # absent on the wire, so it must default
    assert parsed.health.component_health_map["receiver:otlp"].healthy is True
    assert parsed.remote_config_status.status == opamp_pb2.RemoteConfigStatuses_APPLIED

    # Deterministic re-encoding proves no field moved: a renumbered field would
    # write the same logical value under a different tag here.
    assert parsed.SerializeToString(deterministic=True) == payload


def test_unknown_future_fields_are_dropped_not_fatal():
    # Forward compatibility, the property that lets this proto be replaced again
    # without coordinating with collectors: an agent on a newer spec sends fields
    # we do not know, and the message still parses.
    payload = bytes.fromhex(GOLDEN_WIRE_HEX) + b"\xa8\x06\x01"  # field 101, varint 1
    parsed = opamp_pb2.AgentToServer.FromString(payload)
    assert parsed.sequence_num == 7
    assert parsed.effective_config.config_map.config_map["raw"].body == b"raw: true\n"
    # The unknown field is never surfaced as a known one (whether the runtime
    # preserves or drops the bytes is an implementation detail); nothing raises.
    assert all(field.number != 101 for field, _ in parsed.ListFields())


def test_stub_and_type_hint_were_generated_together():
    """A stale `.pyi` is how a typo'd field name survives type checking and only
    fails at runtime, so the hints must describe the synced messages too.
    """
    from pathlib import Path

    proto_dir = Path(__file__).resolve().parents[1] / "proto"
    source = (proto_dir / "opamp_pb2.py").read_text()
    hints = (proto_dir / "opamp_pb2.pyi").read_text()

    assert "Generated by the protocol buffer compiler.  DO NOT EDIT!" in source
    assert "Protobuf Python Version: " in source
    assert "class AgentConfigObject(_message.Message):" in hints
    assert "role: str" in hints
    assert "AgentConfigFile" not in hints
    assert "attributes: _containers.RepeatedCompositeFieldContainer" in hints


def test_role_can_be_pushed_once_the_pipeline_carries_it():
    """The proto field is only useful if it survives validation → hash → wire (#68).

    `role` was refused while the push pipeline dropped it; this pins the other side:
    an object that declares a role is accepted and the role reaches the agent.
    """
    from server import remote_config
    from server.config_validation import check_config_map

    payload = {"raw": {"body": "a: 1\n", "content_type": "text/yaml", "role": "raw"}}
    verdict = check_config_map(payload)
    assert verdict.ok, verdict.error_messages
    assert verdict.normalised["raw"]["role"] == "raw"
    assert remote_config.build_config_map(verdict.normalised).config_map["raw"].role == "raw"

    # A role is a name, not a payload: it is bounded.
    too_long = {"raw": {"body": "a: 1\n", "role": "r" * 500}}
    assert not check_config_map(too_long).ok


def test_v020_agent_message_stores_exactly_as_before():
    """A v0.20.0 collector's status must land in the registry unchanged.

    The sync is only safe if the *server* is indifferent to the new fields: an agent
    that sends `content_type`-bearing config objects with a `role`, and health
    attributes, keeps the same stored shape as one that does not.
    """
    from fastapi.testclient import TestClient

    from server import agent_auth
    from server.main import app
    from server.state import AGENT_REGISTRY

    uid = bytes.fromhex("00112233445566778899aabbccddeeff")
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = True
    msg.health.status = "StatusOK"
    msg.health.attributes.add(
        key="otelcol.component.id",
        value=anyvalue_pb2.AnyValue(string_value="otlp/receiver"),
    )
    msg.effective_config.config_map.config_map[""].body = b"receivers:\n  otlp:\n"
    msg.effective_config.config_map.config_map[""].content_type = "text/yaml"
    msg.effective_config.config_map.config_map["raw"].body = b"raw: {}\n"
    msg.effective_config.config_map.config_map["raw"].role = "raw"

    token, _ = agent_auth.issue_token(label="proto-sync")
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/opamp",
            content=msg.SerializeToString(),
            headers={
                "Content-Type": "application/x-protobuf",
                "Authorization": f"Bearer {token}",
            },
        )
        assert response.status_code == 200

        agent_id = AGENT_REGISTRY.list_all()[0].agent_id
        detail = client.get(f"/agent/{agent_id}").json()
        assert detail["healthy"] is True
        # Both objects survive the round-trip through `MessageToDict`, which is what
        # the store keeps (`effective_config` is the serialised document).
        stored = json.loads(detail["effective_config"])["configMap"]["configMap"]
        assert sorted(stored) == ["", "raw"]
        assert stored["raw"]["role"] == "raw"
        assert stored[""]["contentType"] == "text/yaml"  # MessageToDict is lowerCamelCase
    finally:
        for existing in list(AGENT_REGISTRY._agents.keys()):
            AGENT_REGISTRY.remove(existing)
