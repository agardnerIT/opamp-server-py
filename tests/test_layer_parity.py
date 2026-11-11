"""Layer-parity inventory (#106): a route cannot ship without a client method.

AGENTS.md's rule — "add the endpoint, extend `opamp_client`, add an `opampctl` command,
add an MCP tool, then add tests for each" — was a convention enforced by review. This
file makes it mechanical for the layer that can be checked from the app object:

* every **mutating or config-related** route must be declared here, with the client
  method that speaks it;
* every declared route must exist in the app (no dead entries after a rename);
* every declared client method must exist on `OpampClient` (no promise without a
  method);
* every client method that talks to a declared route must be reachable — the point is
  that "I forgot the client" fails a test instead of shipping.

CLI and MCP coverage is asserted where it is assertable (the command/tool must exist),
because their ergonomics are tested behaviourally in `tests/test_cli.py` and
`tests/test_mcp_server.py`.
"""

from __future__ import annotations

import inspect
import json

import pytest
from fastapi.routing import APIRoute

from cli.main import app as cli_app
from client.opamp_client import OpampClient
from mcp_server import server as mcp_server
from server.main import app
from tests.asgi_transport import InProcessASGITransport

# (method, path) -> the OpampClient method that speaks it.
#
# Scope: every route that reads or writes config, agent identity or rollout state.
# Telemetry ingest, compliance, alerts, reports, auth and the OpAMP endpoint are
# deliberately out of scope — their layers have their own suites (#54, #58, #97).
ROUTE_TO_CLIENT = {
    ("GET", "/health"): "health",
    ("GET", "/agents"): "list_agents",
    # #132: SQL selection schema/autosuggestion is its own route.
    ("GET", "/agents/query/schema"): "agent_query_schema",
    ("GET", "/agent/types"): "agent_types",
    ("PUT", "/agent/{agent_id}/type"): "set_agent_type",
    ("GET", "/agent/{agent_id}"): "get_agent",
    ("GET", "/agent/{agent_id}/config"): "get_agent_config",
    ("PUT", "/agent/{agent_id}/config"): "set_agent_config",
    ("DELETE", "/agent/{agent_id}/config"): "delete_agent_config",
    ("POST", "/agent/{agent_id}/config/deliver"): "deliver_agent_config",
    ("POST", "/agent/{agent_id}/config/rollback"): "rollback_agent_config",
    ("POST", "/agent/{agent_id}/restart"): "restart_agent",
    ("POST", "/agent/{agent_id}/custom-message"): "send_custom_message",
    ("POST", "/config/validate"): "validate_config",
    ("POST", "/config/apply"): "apply_agent_config",
    ("GET", "/agent/{agent_id}/metrics"): "get_agent_metrics",
    ("GET", "/agent/{agent_id}/report"): "agent_report",
    ("POST", "/agent/{agent_id}/manifest"): "generate_manifest",
    ("GET", "/agent/{agent_id}/compliance"): "get_compliance",
    ("GET", "/auth/principals"): "list_principals",
    ("GET", "/auth/verify"): "auth_verify",
    ("POST", "/agents/{agent_id}/token"): "create_agent_token",
    ("DELETE", "/agents/{agent_id}/token"): "revoke_agent_token",
    # #126: fleet tokens — mint before any agent exists, expire, and cap.
    ("POST", "/agent-tokens"): "create_fleet_token",
    ("GET", "/agent-tokens"): "list_agent_tokens",
    ("DELETE", "/agent-tokens/{token_id}"): "revoke_fleet_token",
    ("GET", "/config/posture"): "config_posture",
    ("GET", "/updates"): "get_updates",
    ("POST", "/updates/check"): "check_updates",
    ("PUT", "/updates/settings"): "set_update_check",
    ("POST", "/config/revisions"): "propose_config",
    ("GET", "/config/revisions"): "list_config_revisions",
    ("GET", "/config/revisions/{proposal_id}"): "get_config_proposal",
    ("POST", "/config/revisions/{proposal_id}/approve"): "approve_config",
    ("POST", "/config/revisions/{proposal_id}/reject"): "reject_config",
    ("POST", "/config/revisions/{proposal_id}/reactivate"): "reactivate_config",
    ("POST", "/config/revisions/{proposal_id}/validate"): "revalidate_config",
    # #111: collector groups — a saved selector, not a member list.
    ("GET", "/groups"): "groups",
    ("POST", "/groups"): "create_group",
    ("GET", "/groups/attribute-keys"): "attribute_keys",
    ("GET", "/groups/{group_id}"): "group",
    ("PUT", "/groups/{group_id}"): "update_group",
    ("DELETE", "/groups/{group_id}"): "archive_group",
    ("POST", "/groups/{group_id}/restore"): "restore_group",
    ("POST", "/groups/{group_id}/preview"): "preview_group",
    ("POST", "/config/revisions/{proposal_id}/apply"): "create_config_rollout",
    ("POST", "/config/rollouts"): "create_config_rollout",
    ("GET", "/config/rollouts"): "list_config_rollouts",
    ("GET", "/config/rollouts/{rollout_id}"): "get_config_rollout",
    ("POST", "/config/rollouts/{rollout_id}/abort"): "abort_config_rollout",
    ("POST", "/config/rollouts/{rollout_id}/resume"): "resume_config_rollout",
    ("POST", "/config/rollouts/{rollout_id}/rollback"): "rollback_config_rollout",
}

# Routes whose CLI spelling differs from the client method name.
CLI_COMMANDS = {
    "config propose": ("config", "propose"),
    "config validate": ("config", "validate"),
    "config revisions": ("config", "revisions"),
    "config show": ("config", "show"),
    "config approve": ("config", "approve"),
    "config reject": ("config", "reject"),
    "config reactivate": ("config", "reactivate"),
    "config revalidate": ("config", "revalidate"),
    "config diff": ("config", "diff"),
    "config rollouts": ("config", "rollouts"),
    "config rollout plan": ("config", "rollout", "plan"),
    "config rollout create": ("config", "rollout", "create"),
    "config rollout show": ("config", "rollout", "show"),
    "config rollout abort": ("config", "rollout", "abort"),
    "config rollout resume": ("config", "rollout", "resume"),
    "config rollout rollback": ("config", "rollout", "rollback"),
    "agents config restart": ("agents", "config", "restart"),
    "agent-tokens create": ("agent-tokens", "create"),
    "agent-tokens list": ("agent-tokens", "list"),
    "agent-tokens revoke": ("agent-tokens", "revoke"),
    "agents types": ("agents", "types"),
    "agents set-type": ("agents", "set-type"),
    "agents custom send": ("agents", "custom", "send"),
    "agents config withdraw": ("agents", "config", "withdraw"),
    "agents config deliver": ("agents", "config", "deliver"),
    "groups list": ("groups", "list"),
    "groups show": ("groups", "show"),
    "groups create": ("groups", "create"),
    "groups update": ("groups", "update"),
    "groups archive": ("groups", "archive"),
    "groups restore": ("groups", "restore"),
    "groups preview": ("groups", "preview"),
    "groups keys": ("groups", "keys"),
    # #132: `agents list --query` and the schema that makes it writable.
    "agents query-schema": ("agents", "query-schema"),
}

# MCP tools that must exist, and the write-shaped ones that must NEVER exist (D9).
MCP_READ_TOOLS = {
    "list_config_revisions",
    "list_config_rollouts",
    "get_config_rollout",
    "agent_config",
    "validate_config",
    # #118: reading the recognised agent types is how a client decides whether a
    # component list or a verdict means anything for a given agent.
    "list_agent_types",
    # #126: "which token is about to expire or has hit its cap" is a read, and it is
    # how an agent diagnoses a 401 it did not cause.
    "list_agent_tokens",
    # #111: groups are propose-side. Reading them is how an agent picks a target
    # instead of inventing a filter; `create_group` is gated, never a read tool.
    "list_groups",
    "get_group",
    "preview_group",
    "attribute_keys",
    # #132: SQL selection and the live schema that makes it writable.
    "query_agents",
    "agent_query_schema",
}
MCP_FORBIDDEN_TOOLS = {
    "create_config_rollout",
    "plan_config_rollout",
    "abort_config_rollout",
    "resume_config_rollout",
    "rollback_config_rollout",
    "approve_config",
    "reject_config",
    "reactivate_config",
    "restart_agent",
    "deliver_agent_config",
    "set_agent_config",
    "send_custom_message",
    # #118: pinning could have gone either way; it is write-gated rather than
    # forbidden (an operator may legitimately want an agent to fix a typo'd
    # detection), so it is asserted as gated in tests/test_mcp_server.py instead.
}


def app_routes() -> set:
    routes = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            routes.add((method, route.path))
    return routes


def test_every_declared_route_exists():
    missing = sorted(set(ROUTE_TO_CLIENT) - app_routes())
    assert not missing, (
        "the parity map names routes that no longer exist (rename or delete the entry "
        f"above): {missing}"
    )


def test_every_declared_client_method_exists():
    missing = sorted(
        method
        for method in set(ROUTE_TO_CLIENT.values())
        if not callable(getattr(OpampClient, method, None))
    )
    assert not missing, (
        "ROUTE_TO_CLIENT promises a client method that does not exist: "
        f"{missing} — add it to client/opamp_client.py"
    )


def test_no_config_route_is_missing_from_the_parity_map():
    """The rule that actually bites: a NEW config/agent route must come with a client.

    Scoped to `/config*`, `/agent*` and `/agents*`, which are the surfaces AGENTS.md's
    four-layer rule is about. Anything added there without a declaration fails here
    until somebody writes the client method (or explains why it has none).
    """
    watched = {
        (method, path)
        for method, path in app_routes()
        if path.startswith(("/config", "/agent", "/agents", "/groups", "/v1/opamp"))
    }
    # The OpAMP protocol endpoint is consumed by agents, not by `opamp_client`.
    watched = {entry for entry in watched if entry != ("POST", "/v1/opamp")}
    undeclared = sorted(watched - set(ROUTE_TO_CLIENT))
    assert not undeclared, (
        "route(s) with no declared client method: "
        f"{undeclared} — extend `client/opamp_client.py` and add the entry to "
        "ROUTE_TO_CLIENT (AGENTS.md: all layers stay in sync)"
    )


def test_every_client_config_method_is_reachable_from_a_route():
    """The mirror image: no client method that calls nothing (a stale promise)."""
    mapped = set(ROUTE_TO_CLIENT.values())
    public = {
        name
        for name, _ in inspect.getmembers(OpampClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    # Methods that are conveniences over another method, not their own route.
    aliases = {
        "plan_config_rollout",  # create_config_rollout(dry_run=True)
        "ha_health",  # multi-node convenience
        "query_agents",  # list_agents(query=...)
    }
    unrouteable = sorted(public - mapped - aliases - _non_route_methods())
    assert not unrouteable, (
        f"client methods that speak to no declared route: {unrouteable} — either map "
        "them in ROUTE_TO_CLIENT or add them to the alias list"
    )


def _non_route_methods() -> set:
    """Public client methods that are not one-route-one-method (auth, offline helpers)."""
    return {
        # session/identity, audited separately in tests/test_auth.py
        "login",
        "logout",
        "whoami",
        "auth_status",
        "list_sessions",
        "revoke_session",
        "create_principal",
        "revoke_principal",
        "verify_auth",
        # fleet reports read several routes at once
        "fleet_report",
        "heavy_collectors_report",
        "outdated_collectors_report",
        "agent_report",
        # misc
        "compliance_summary",
        "list_policies",
        "validate_policies",
        "check_compliance",
        "reload_policies",
        "get_alerts",
        "update_alerts",
        "test_alerts",
        "agent_config",  # alias used by MCP for get_agent_config
        "audit_log",
        "audit_verify",
        "audit_export",
        "close",
    }


def test_every_declared_cli_command_exists():
    registered = set()

    def walk(typer_app, prefix=()):
        for command in getattr(typer_app, "registered_commands", []):
            registered.add(prefix + (command.name,))
        for group in getattr(typer_app, "registered_groups", []):
            walk(group.typer_instance, prefix + (group.name,))

    walk(cli_app)
    missing = sorted(
        name for name, path in CLI_COMMANDS.items() if tuple(path) not in registered
    )
    assert not missing, f"CLI commands promised by the parity map but absent: {missing}"


def test_mcp_reads_rollouts_and_never_writes_them():
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert MCP_READ_TOOLS <= names, f"missing MCP tools: {MCP_READ_TOOLS - names}"
    forbidden = names & MCP_FORBIDDEN_TOOLS
    assert not forbidden, (
        f"MCP must never expose a write/approve/rollback/restart tool (D9): {forbidden}"
    )
    # #111: group creation is write-gated like `propose_config`, so it must not be
    # visible in the read-only posture.
    assert "create_group" not in names, (
        "create_group is write-gated (OPAMP_MCP_ALLOW_WRITES); exposing it by default "
        "would let an agent change the blast radius of every future push"
    )


# ------------------------------------------------ #134: the profile dictionary
#
# An API field only the server knows about is a field nobody can test, so the four
# layers have to carry #134's additions *in the same change* as the schema (AGENTS.md,
# and #134 deliverable 6). These are field-level assertions, not route-existence ones:
# the shape is what a client actually consumes, and each layer proxies it as JSON, so
# the failure this catches is a layer that reformats, renames or drops a key.

#: What `GET /agent/types` promises per profile since #134.
PROFILE_FIELDS = {
    "name",
    "display_name",
    "icon",
    "priority",
    "summary",
    "validates_structure",
    "content_types",
    "default_filename",
    "reports",
    "reports_telemetry",
    "telemetry",
    "detectable",
    "identity",
    "panels",
    "components",
    "bootstrap_default",
}


def _profile_types(payload) -> dict:
    """`{name: entry}` from whatever wrapper a layer used."""
    entries = payload["types"] if isinstance(payload, dict) and "types" in payload else payload
    return {entry["name"]: entry for entry in entries}


def test_the_profile_shape_reaches_the_client():
    from client.opamp_client import OpampClient

    api = OpampClient(
        base_url="http://opamp.test", transport=InProcessASGITransport(app)
    )
    types = _profile_types(api.agent_types())
    assert set(types["otelcol"]) >= PROFILE_FIELDS
    assert types["otelcol"]["icon"] == "🧩"
    assert types["otelcol"]["panels"]
    json.dumps(types)  # the client must never hand back a non-JSON value


def test_the_profile_shape_reaches_the_cli(monkeypatch):
    import cli.main as cli_main
    from cli.main import app as cli_app
    from client.opamp_client import OpampClient
    from typer.testing import CliRunner

    transport = InProcessASGITransport(app)
    monkeypatch.setattr(
        cli_main,
        "_client",
        lambda: OpampClient(base_url="http://opamp.test", transport=transport),
    )
    result = CliRunner().invoke(cli_app, ["agents", "types"])
    assert result.exit_code == 0, result.output
    types = _profile_types(json.loads(result.output))
    assert set(types["otelcol"]) >= PROFILE_FIELDS


def test_the_profile_shape_reaches_mcp(monkeypatch):
    from client.opamp_client import OpampClient

    transport = InProcessASGITransport(app)
    monkeypatch.setattr(
        mcp_server,
        "_client",
        lambda: OpampClient(base_url="http://opamp.test", transport=transport),
    )
    types = _profile_types(json.loads(mcp_server.list_agent_types()))
    assert set(types["otelcol"]) >= PROFILE_FIELDS
    assert types["otelcol"]["priority"] == 100
    # A read tool must not leak the telemetry *values*, only the declaration.
    assert types["otelcol"]["telemetry"] == {"metric_prefix": "otelcol_"}
