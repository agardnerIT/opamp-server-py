"""Credential helpers shared by the HTTP-facing suites.

`admin_headers`, `basic` and `bearer` were copy-pasted into nine test files before
they lived here. That is not a style problem: a security suite that builds its own
admin header can quietly disagree with the one every other suite uses, and then
"the gate is enforced" stops being the same claim in two places.

What deliberately does *not* live here: `agent_message` builders. They differ per
suite in the capabilities they advertise (a rollout test's agent accepts restarts, a
config-control test's agent reports its effective config), and folding those into one
parameterised builder would hide the posture each test is actually asserting.

Fixtures (`client`, `ws_on`) are in `conftest.py` instead, so they need no import.
"""

from __future__ import annotations

import base64
import hashlib

# The shared password every suite uses when it wants "a configured admin". Kept in
# one place so `admin_headers()` cannot drift from the value a fixture monkeypatched
# into `admin_auth.ADMIN_PASSWORD`.
ADMIN = "admin-pw-1234567890"
DEFAULT_PASSWORD = "pw-123456789"


def basic(name: str, password: str = DEFAULT_PASSWORD) -> dict:
    """A Basic-auth header for a named principal (or `""` for the shared password)."""
    token = base64.b64encode(f"{name}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def admin_headers(password: str = ADMIN) -> dict:
    """The shared-password admin header (empty username, password in the secret)."""
    return basic("", password)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def spec_wire_hash(message) -> str:
    """The spec's config hash (hex) for a message that is already on the wire (#136).

    `sha256(AgentConfigMap.SerializeToString(deterministic=True))` — the exact
    computation a verifying agent performs before it writes the file. Tests assert
    the wire hash this way rather than against `remote_config.compute_hash`, because
    the storage hash is a *different* function and conflating them is the defect
    #136 exists to prevent.
    """
    config_map = getattr(message, "remote_config", message)
    config_map = getattr(config_map, "config", config_map)
    return hashlib.sha256(config_map.SerializeToString(deterministic=True)).hexdigest()
