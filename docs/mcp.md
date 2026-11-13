# MCP server (for AI agents)

An [MCP](https://modelcontextprotocol.io) server (`opamp-mcp` / `python -m mcp_server`)
exposes 39 typed tools mirroring the API (47 with `OPAMP_MCP_ALLOW_WRITES=true`):
`list_agents`, `query_agents`, `agent_query_schema`, `get_agent`,
`list_agent_types`, `get_agent_metrics`, `generate_manifest`, `fleet_report`,
`heavy_collectors_report`, `outdated_collectors_report`, `get_compliance`,
`check_compliance`, `compliance_summary`, `list_policies`, `validate_policies`,
`reload_policies`, `get_alerts`, `update_alerts`, `test_alerts`, `agent_report`,
`health`, `auth_status`, `whoami`, `config_posture`, `agent_config`,
`validate_config`, `list_config_revisions`, `list_config_rollouts`,
`get_config_rollout`, `list_groups`, `get_group`, `preview_group`,
`attribute_keys`, `audit_log`, `audit_verify`, `audit_export`.

Requires `pip install -e ".[mcp]"`.

**Identity is never delegated to MCP.** Logins, principals and sessions have no MCP
tools at all — minting an identity is a human/IaC action, not a tool call an AI can
be talked into. `whoami` exists so an agent can *see* which role it is acting as
before suggesting a privileged step.

**Reading the audit log is always allowed.** `audit_log`, `audit_verify` and
`audit_export` are read-only tools and are never gated: an AI agent asked "who
pushed this" must be able to answer from evidence, not from its own summary.

**Write tools are absent, not failing.** `create_agent_token`, `revoke_agent_token`,
`set_agent_type` and `propose_config` only exist when the MCP process is started with
`OPAMP_MCP_ALLOW_WRITES=true` (#94 introduced this gate for credential minting;
#106 applies it to every write tool). An agent that cannot see a tool will not
try to use it.

**An AI may propose, never approve.** `propose_config` creates a *draft* revision —
nothing is delivered until a human with the `approver` role approves it in the CLI,
UI or API. There is no MCP tool for approve, reject, reactivate, rollback, apply,
withdraw or **deliver** at any setting: a client that can approve its own proposal
has no separation of duties, and one that can *deliver* would make the proposal
arrive on its own schedule (D3/D9, #102).

Transport is **stdio** by default (works with every local MCP client); use
`--transport sse --port 8765` for remote agents. Configure with the same env vars
as the CLI (`OPAMP_SERVER_URL`, `ADMIN_PASSWORD`), plus
`OPAMP_MCP_ALLOW_WRITES` if you really want credential minting exposed to an AI client.

## Claude Desktop / generic MCP client config

```json
{
  "mcpServers": {
    "opamp-server": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "OPAMP_SERVER_URL": "http://localhost:4320",
        "ADMIN_PASSWORD": "changeme"
      }
    }
  }
}
```

The `command` must point at a Python where the package is installed
(`pip install -e ".[mcp]"`).

## Claude Code

```bash
claude mcp add opamp-server \
  -e OPAMP_SERVER_URL=http://localhost:4320 \
  -e ADMIN_PASSWORD=changeme \
  -- python -m mcp_server
```
