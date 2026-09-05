"""MCP server exposing OpAMP server capabilities as typed tools for AI agents.

Built with FastMCP; every tool wraps an :class:`client.opamp_client.OpampClient`
method (single implementation of HTTP logic). Runs over **stdio** by default:

    python -m mcp_server            # stdio (works with every local MCP client)
    python -m mcp_server --transport sse --port 8765   # SSE for remote agents

Configuration (env or flags):
    OPAMP_SERVER_URL   server base URL (default http://localhost:4320)
    ADMIN_PASSWORD     admin password for admin tools (alerts, compliance check/reload)

MCP client config (Claude Desktop / Claude Code / etc.)::

    {
      "mcpServers": {
        "opamp-server": {
          "command": "python",
          "args": ["/path/to/opamp-server-py/-m", "mcp_server"],
          "env": {"OPAMP_SERVER_URL": "http://localhost:4320", "ADMIN_PASSWORD": "..."}
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from client.opamp_client import (
    OpampApiError,
    OpampClient,
    OpampClientError,
    OpampConnectionError,
)

mcp = FastMCP(
    "opamp-server",
    instructions=(
        "Tools for inspecting OpenTelemetry agents connected to an OpAMP server: "
        "list/filter agents, view agent details and metrics, generate slim OCB "
        "collector manifests, run fleet reports, evaluate OPA compliance, and "
        "manage alerts. Agent IDs are the hex instance UIDs returned by "
        "list_agents. Admin tools (alerts, compliance check/reload) require the "
        "server's ADMIN_PASSWORD to be configured via env."
    ),
)


def _client() -> OpampClient:
    # Construct per call: tools may run long after startup; env can change in tests.
    return OpampClient()


def _tool(fn):
    """Run a client call, returning structured JSON or a structured error string."""
    try:
        return json.dumps(fn(_client()), indent=2, default=str)
    except OpampApiError as exc:
        return json.dumps({"error": True, "status_code": exc.status_code, "detail": exc.detail}, indent=2)
    except OpampConnectionError as exc:
        return json.dumps({"error": True, "connection": str(exc)}, indent=2)
    except OpampClientError as exc:
        return json.dumps({"error": True, "message": str(exc)}, indent=2)


# ------------------------------------------------------------------ ops & auth

@mcp.tool()
def health() -> str:
    """Server health: status, connected agent count, OPA availability."""
    return _tool(lambda oc: oc.health())


@mcp.tool()
def auth_status() -> str:
    """Whether the server requires an admin password."""
    return _tool(lambda oc: oc.auth_status())


# --------------------------------------------------------------------- agents

@mcp.tool()
def list_agents(
    healthy: Optional[str] = None,
    status: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> str:
    """List connected agents, optionally filtered.

    Args:
        healthy: "true", "false" or "unknown" — health filter.
        status: Remote config status filter (UNSET/APPLIED/APPLYING/FAILED).
        attributes: Metadata filters on agent description attributes, e.g.
            {"environment": "prod"} or {"environment": ["prod", "staging"]}.
    """
    def call(oc: OpampClient):
        attrs = attributes or {}
        return oc.list_agents(healthy=healthy, status=status, **attrs)
    return _tool(call)


@mcp.tool()
def get_agent(agent_id: str) -> str:
    """Full details for one agent (components, health, latest metrics).

    agent_id is the hex instance UID returned by list_agents.
    """
    return _tool(lambda oc: oc.get_agent(agent_id))


@mcp.tool()
def get_agent_metrics(agent_id: str) -> str:
    """Latest ingested OTLP metric values for one agent (survives restarts)."""
    return _tool(lambda oc: oc.get_agent_metrics(agent_id))


# ------------------------------------------------------------------ manifests

@mcp.tool()
def generate_manifest(agent_id: str, version: Optional[str] = None) -> str:
    """Generate an OCB manifest.yaml for a slim collector build from an agent's used components.

    Returns {"manifest_yaml", "ocb_command", "collector_version"}. Pass version
    (semver) to set the distro version; fails if the agent has no components in use.
    """
    return _tool(lambda oc: oc.generate_manifest(agent_id, version=version))


# -------------------------------------------------------------------- reports

@mcp.tool()
def agent_report(agent_id: str) -> str:
    """Markdown report for one agent (versions, health, components)."""
    return _tool(lambda oc: oc.agent_report(agent_id))


@mcp.tool()
def fleet_report() -> str:
    """Fleet-wide markdown report: versions, outdated/heavy collectors, per-agent detail."""
    return _tool(lambda oc: oc.fleet_report())


@mcp.tool()
def heavy_collectors_report(threshold: float = 0.5) -> str:
    """Markdown report of collectors whose unused-component ratio exceeds threshold (0-1)."""
    return _tool(lambda oc: oc.heavy_collectors_report(threshold=threshold))


@mcp.tool()
def outdated_collectors_report(version: str = "0.149.0") -> str:
    """Markdown report of collectors with components older than the given semver version."""
    return _tool(lambda oc: oc.outdated_collectors_report(version=version))


# ----------------------------------------------------------------- compliance

@mcp.tool()
def get_compliance(agent_id: str) -> str:
    """Evaluate one agent against OPA policies (compliant, violations)."""
    return _tool(lambda oc: oc.get_compliance(agent_id))


@mcp.tool()
def check_compliance(agent_id: str) -> str:
    """Force a compliance evaluation for one agent (admin; requires ADMIN_PASSWORD)."""
    return _tool(lambda oc: oc.check_compliance(agent_id))


@mcp.tool()
def compliance_summary() -> str:
    """Fleet-wide compliance counts (compliant / non_compliant / not_evaluated)."""
    return _tool(lambda oc: oc.compliance_summary())


@mcp.tool()
def list_policies() -> str:
    """List available OPA policies."""
    return _tool(lambda oc: oc.list_policies())


@mcp.tool()
def validate_policies() -> str:
    """Validate OPA policy files; per-policy results."""
    return _tool(lambda oc: oc.validate_policies())


@mcp.tool()
def reload_policies() -> str:
    """Ask OPA to reload policies from disk (admin; requires ADMIN_PASSWORD)."""
    return _tool(lambda oc: oc.reload_policies())


# --------------------------------------------------------------------- alerts

@mcp.tool()
def get_alerts() -> str:
    """Current alert configuration (admin; requires ADMIN_PASSWORD)."""
    return _tool(lambda oc: oc.get_alerts())


@mcp.tool()
def update_alerts(config: Dict[str, Any]) -> str:
    """Update alert configuration (admin). Pass the same shape get_alerts returns under 'config'."""
    return _tool(lambda oc: oc.update_alerts(config))


@mcp.tool()
def test_alerts(event_type: str = "new_agent", event_config: Optional[Dict[str, Any]] = None) -> str:
    """Send a test alert through the dispatcher (admin). event_config is used once, not saved."""
    return _tool(lambda oc: oc.test_alerts(event_type=event_type, event_config=event_config))


def main() -> None:
    parser = argparse.ArgumentParser(description="OpAMP server MCP tools (stdio/SSE)")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765, help="Port for sse/streamable-http")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run()  # stdio
    else:
        mcp.settings.port = args.port
        mcp.run(args.transport)


if __name__ == "__main__":
    main()
