"""Server capability advertisement (#74).

The acceptance criteria this file exists for:

* the advertised bitmask is exactly the implemented set — no help wanted;
* every `ServerCapabilities` value in the proto is classified as implemented,
  conditional, unimplemented (with its issue) or reserved, so a proto bump fails
  here instead of silently over-claiming to the fleet;
* the future connection-settings/packages bits stay off until their issues land.
"""

from __future__ import annotations

import pytest

from proto.opamp_pb2 import ServerCapabilities
from server import remote_config

# 1 | 4 | 16. Written as a number *and* spelled out below, so a wrong constant can
# never agree with a wrong expectation by construction.
BASE_CAPABILITIES = 0x15


def _bit(name: str) -> int:
    return getattr(ServerCapabilities, name)


def test_advertised_bitmask_is_exactly_the_implemented_set(monkeypatch):
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    advertised = remote_config.server_capabilities()
    assert advertised == BASE_CAPABILITIES
    assert advertised == (
        _bit("ServerCapabilities_AcceptsStatus")
        | _bit("ServerCapabilities_AcceptsEffectiveConfig")
        | _bit("ServerCapabilities_AcceptsPackagesStatus")
    )


def test_connection_settings_bits_are_not_advertised(monkeypatch):
    monkeypatch.delenv("OPAMP_CONFIG_PUSH_ENABLED", raising=False)
    advertised = remote_config.server_capabilities()
    assert not advertised & _bit("ServerCapabilities_OffersConnectionSettings")
    assert not advertised & _bit("ServerCapabilities_AcceptsConnectionSettingsRequest")
    assert not advertised & _bit("ServerCapabilities_OffersPackages")


def test_offers_remote_config_is_conditional_on_posture(monkeypatch):
    monkeypatch.setattr(remote_config.posture, "push_allowed", lambda: False)
    assert remote_config.server_capabilities() == BASE_CAPABILITIES

    monkeypatch.setattr(remote_config.posture, "push_allowed", lambda: True)
    assert remote_config.server_capabilities() == (
        BASE_CAPABILITIES | _bit("ServerCapabilities_OffersRemoteConfig")
    )


def test_every_proto_capability_is_classified():
    """A new protocol bit must be a deliberate decision, not an accidental omission."""
    proto_values = {value.number for value in ServerCapabilities.DESCRIPTOR.values}

    classified = set(remote_config.CLASSIFIED_CAPABILITIES)
    assert proto_values <= classified, (
        "unclassified ServerCapabilities: "
        f"{sorted(proto_values - classified)} — add each to "
        "ALWAYS_ADVERTISED_CAPABILITIES, CONDITIONAL_CAPABILITIES, "
        "UNIMPLEMENTED_CAPABILITIES or RESERVED_CAPABILITIES in server/remote_config.py"
    )

    # The four buckets must be disjoint: a bit cannot be both promised and not.
    buckets = [
        set(remote_config.ALWAYS_ADVERTISED_CAPABILITIES),
        set(remote_config.CONDITIONAL_CAPABILITIES),
        set(remote_config.UNIMPLEMENTED_CAPABILITIES),
        set(remote_config.RESERVED_CAPABILITIES),
    ]
    seen: set[int] = set()
    for bucket in buckets:
        assert not (seen & bucket), f"capability in two buckets: {seen & bucket}"
        seen |= bucket


@pytest.mark.parametrize("bit, issue", list(remote_config.UNIMPLEMENTED_CAPABILITIES.items()))
def test_unimplemented_capabilities_name_the_issue_that_lands_them(bit, issue):
    assert issue.startswith("#")
    assert bit not in remote_config.ALWAYS_ADVERTISED_CAPABILITIES
