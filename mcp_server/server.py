"""MCP server exposing OpAMP server capabilities as typed tools for AI agents.

Built with FastMCP; every tool wraps an :class:`client.opamp_client.OpampClient`
method (single implementation of HTTP logic). Runs over **stdio** by default:

    python -m mcp_server            # stdio (works with every local MCP client)
    python -m mcp_server --transport sse --port 8765   # SSE for remote agents

Configuration (env or flags):
    OPAMP_SERVER_URL       server base URL (default http://localhost:4320)
    ADMIN_PASSWORD         admin password for admin tools (alerts, compliance check/reload)
    OPAMP_MCP_ALLOW_WRITES "true" exposes privileged write tools (credential minting,
                           #94). Absent by default — #106 extends this gate to every
                           write tool.

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
import os
from typing import Any, Dict, Optional

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
def get_update_status() -> str:
    """Cached update-check state (#46). Read-only; never contacts GitHub."""
    return _tool(lambda oc: oc.get_updates())


@mcp.tool()
def check_for_updates() -> str:
    """Run one update check now (admin). This makes one outbound GitHub request."""
    return _tool(lambda oc: oc.check_updates())


@mcp.tool()
def auth_status() -> str:
    """How the server authenticates admin callers: mode, roles, live principals (#95).

    `auth_mode` is `password` (legacy shared secret only), `principals` (per-person
    identities with roles) or `disabled` (read-only server: reads open, privileged
    writes refused). Read-only tool.
    """
    return _tool(lambda oc: oc.auth_status())


@mcp.tool()
def whoami() -> str:
    """Which principal and role this MCP server is acting as (#95).

    `method` is `session`, `password`, `legacy` or `anonymous`. Use it before
    suggesting a privileged action: if the role is `viewer`, an operator-only call
    will fail with 403 no matter how it is phrased. Identity administration
    (principals, sessions, logins) is deliberately **not** exposed to MCP — minting
    identities is a human/IaC action.
    """
    return _tool(lambda oc: oc.whoami())


@mcp.tool()
def audit_log(
    since: Optional[str] = None,
    until: Optional[str] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Who did what, when — the audit trail for privileged actions (#97).

    Newest first. `action` is a prefix match (`config.` returns every config-push
    event). Entries name an `actor` (`agent:<id>` for agents), `actor_kind`
    (human/agent/system), `role`, `target`, `result` (ok/denied/error/noop) and a
    redacted `detail_json`. Config bodies and secrets are never stored — only
    `config_hash`. Read-only tool; requires an admin credential.
    """
    return _tool(
        lambda oc: oc.audit_log(
            since=since,
            until=until,
            actor=actor,
            action=action,
            target=target,
            result=result,
            limit=limit,
            offset=offset,
        )
    )


@mcp.tool()
def audit_verify() -> str:
    """Check the audit log's hash chain for tampering (admin).

    `ok: false` means a row was edited or deleted and the log is no longer
    evidence; `broken_at` names the entry where that becomes visible.
    `anchored_by_checkpoint: true` is benign (retention pruned older rows and
    recorded what it cut). Read-only tool.
    """
    return _tool(lambda oc: oc.audit_verify())


@mcp.tool()
def audit_export(format: str = "jsonl", since: Optional[str] = None, until: Optional[str] = None) -> str:
    """Export the retained audit log as JSONL or CSV text, oldest first (admin).

    Returned as a JSON string (`{"format", "bytes", "content"}`) rather than a raw
    body so the result stays parseable in an MCP client. Use `audit_log` when you
    only need to read entries. Read-only tool.
    """
    def call(oc: OpampClient):
        body = oc.audit_export(format=format, since=since, until=until)
        return {"format": format, "bytes": len(body), "content": body}
    return _tool(call)


@mcp.tool()
def config_posture() -> str:
    """Config push security posture: admin auth, agent auth, TLS, override state.

    Safe to call any time (read-only). Requires ADMIN_PASSWORD when one is set.
    """
    return _tool(lambda oc: oc.config_posture())


# ------------------------------------------------------------------ write gate


def writes_enabled() -> bool:
    """`OPAMP_MCP_ALLOW_WRITES` — privileged write tools exist only when true.

    Absent, not failing: an agent that cannot see a tool will not try to use it.
    #106 applies this gate to every write tool; #94 introduces it for credential
    minting, which is the most dangerous write in the system — it hands out a
    fleet identity.
    """
    return (os.environ.get("OPAMP_MCP_ALLOW_WRITES") or "").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


# --------------------------------------------------------------------- agents

@mcp.tool()
def list_agents(
    healthy: Optional[str] = None,
    status: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
) -> str:
    """List connected agents, optionally filtered.

    Args:
        healthy: "true", "false" or "unknown" — health filter.
        status: Remote config status filter (UNSET/APPLIED/APPLYING/FAILED).
        attributes: Metadata filters on agent description attributes, e.g.
            {"environment": "prod"} or {"environment": ["prod", "staging"]}.
        query: SQL selection (#132), e.g.
            "SELECT * FROM agents WHERE host_arch = 'amd64'". Wins over the other
            arguments. Read `agent_query_schema` first for the columns and the
            values live agents actually report.
    """
    def call(oc: OpampClient):
        attrs = attributes or {}
        if query is not None:
            return oc.query_agents(query)
        return oc.list_agents(healthy=healthy, status=status, **attrs)
    return _tool(call)


@mcp.tool()
def query_agents(query: str) -> str:
    """Run a SQL selection over the live fleet (#132). Read-only.

    The only table is `agents`. Description attributes are columns named exactly as
    the agent reports them (quote a key containing a dot or a space), and `attr('key')`
    is shorthand. `healthy` is 1, 0 or NULL. Only SELECT is accepted. A group saved
    from a query may not use LIMIT/OFFSET; this search may.

    Call `agent_query_schema` first rather than guessing a column name: it lists the
    columns and the values observed on connected agents, which is what makes a
    suggestion (`host_arch = 'amd64'`) different from a guess.
    """
    return _tool(lambda oc: oc.query_agents(query))


@mcp.tool()
def agent_query_schema() -> str:
    """Columns, live values, operators and example SQL for query_agents (#132).

    The autosuggestion payload: core columns (`id`, `healthy`, `remote_config_status`,
    `agent_type`, ...) plus every description attribute a live agent reports, with
    sample values. Descriptions only — never an effective config. The language guide
    is `docs/sql-agent-queries.md`.
    """
    return _tool(lambda oc: oc.agent_query_schema())


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


@mcp.tool()
def list_agent_types() -> str:
    """Agent types this server can recognise (#118). Read-only.

    `otelcol` is an OpenTelemetry Collector (the server reads its components and may
    judge its config); `generic` is any other OpAMP agent, whose config is stored and
    relayed without being interpreted. Check this before reading a component list or
    a validation verdict for an agent: for `generic`, both are collector-specific and
    do not describe the agent. Every agent's `agent_type`, `agent_type_source`
    (pinned/declared/sniffed/fallback) and `agent_type_warning` come back in
    list_agents and get_agent. `agent_type_ambiguous` names the other profiles whose
    evidence also matches, when more than one does (#134) — a tie is a decision
    nobody can audit, so it is reported rather than hidden.

    Each entry also carries `icon`, `priority`, `identity` (the attribute rules and
    sniffer), `telemetry` (`null` = this profile reports none) and `panels` (the
    agent-detail blocks it declares), so a client can render an agent generically
    instead of hardcoding the collector (#134).
    """
    return _tool(lambda oc: oc.agent_types())


# --------------------------------------------------------------- remote config


@mcp.tool()
def agent_config(agent_id: str) -> str:
    """Desired and effective collector config for one agent — **redacted**.

    Returns {"agent_id", "desired", "effective", "remote_config_status",
    "history", "config_push_allowed"}. Secret-looking keys and auth-shaped header
    values are replaced with "<redacted>" and listed in "redacted_paths"; there is
    deliberately **no** reveal option here, because an AI client has no business
    reading fleet credentials (epic #93, D9).

    "desired" is null until a config has been pushed. "failed_at" on a desired
    revision means the agent reported FAILED and that revision is poisoned — it
    will not be retried; a human must push a new revision or roll back.
    """
    return _tool(lambda oc: oc.get_agent_config(agent_id))


@mcp.tool()
def validate_config(config_map: Dict[str, Any], agent_id: Optional[str] = None) -> str:
    """Validate a config map **without** pushing it (safe for an AI to call).

    Args:
        config_map: {"<object name>": {"body": "<yaml/json text>",
            "content_type": "text/yaml", "role": "<optional>"}}. Use "" as the key
            for the single-object case.
        agent_id: Optional target agent, which adds its component inventory to the
            checks (catches "this config needs a component that agent does not
            ship").

    Returns {"valid", "errors", "details", "warnings", "policy", "fingerprint",
    "config_hash", "size_bytes", "objects"}. `errors` are strings, `details` carry
    a YAML path + hint. This is the validate half of the flow: MCP may check a
    config and report the hash, but approve/rollback/reactivate are never exposed
    here ([#93](https://github.com/agardnerIT/opamp-server-py/issues/93), D9).
    **`deliver` is not here either** (#102): re-sending a config to a live fleet
    machine is an operational action, and an AI proposing a config must not be the
    thing that makes it arrive.
    """
    return _tool(lambda oc: oc.validate_config(config_map, agent_id=agent_id))


@mcp.tool()
def list_config_revisions(
    agent: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Config revision history: metadata only, never a body (read-only).

    Args:
        agent: Filter by agent id.
        status: proposed|active|superseded|rolled_back|poisoned|rejected.
        limit: Maximum rows.

    Returns {"revisions", "count", "statuses"}. Each row carries the lifecycle
    status, hash, author, approver, the stored validation verdict and the delivery
    facts (`attempts`, `last_status`, `poisoned`).
    """
    return _tool(
        lambda oc: oc.list_config_revisions(agent=agent, status=status, limit=limit)
    )


@mcp.tool()
def list_config_rollouts(status: Optional[str] = None, limit: int = 50) -> str:
    """Staged rollouts, newest first, with per-state agent counts (read-only).

    Args:
        status: pending|running|paused|completed|aborted|cancelled.
        limit: Maximum rows.

    Returns {"rollouts", "count", "statuses"}. Each row carries the batch progress
    (`batch.current`/`batch.count`), the agent count, the per-agent state rollup and
    `paused_reason` when it is paused.
    """
    return _tool(lambda oc: oc.list_config_rollouts(status=status, limit=limit))


@mcp.tool()
def get_config_rollout(rollout_id: str) -> str:
    """One staged rollout: batch progress, per-agent state and the pause reason.

    Read-only. `agents.states` is the rollup an operator reads ("N applied, K failed,
    paused at batch 2") and `paused_reason` is one of `failed`, `auto_reverted`,
    `agent_went_silent`, `unhealthy`, `delivery_unconfirmed`, `superseded` — never a
    generic error, because the operator's next action differs per reason.

    Deliberately absent: creating a rollout (which *is* the approval), aborting one and
    rolling one back. Approval, abort and rollback are never MCP tools at any setting
    (epic #93, D9): a client that can approve its own proposal has no separation of
    duties, and one that can abort a rollout in flight can stop a human's remediation.
    """
    return _tool(lambda oc: oc.get_config_rollout(rollout_id))


# ------------------------------------------------------------------ groups (#111)


@mcp.tool()
def list_groups(include_archived: bool = False) -> str:
    """Saved selectors ("collector groups") with their live member count.

    A group is a **name for a filter document**, plus explicit `include`/`exclude`
    pins; membership is resolved on read, so it cannot drift from the fleet.
    `last_resolution` is what the group meant when it was last evaluated. Prefer a
    group over a hand-typed filter when proposing: it is the same selector, spelled
    once.
    """
    return _tool(lambda oc: oc.groups(include_archived=include_archived))


@mcp.tool()
def get_group(group: str) -> str:
    """One group by name or id: definition, live members, pins and resolution history.

    `sources` says which members came from the definition and which from pins;
    `health_defined` is non-empty when the definition filters on `healthy`/`status`,
    which makes the group unsuitable as a push target (agents drop out exactly when
    they break).
    """
    return _tool(lambda oc: oc.group(group))


@mcp.tool()
def preview_group(group: str) -> str:
    """Who a push to this group would hit now, versus its last resolution. Read-only.

    `added`/`dropped`/`unchanged` against the last recorded resolution. Use it before
    proposing a group-scoped config, and to see a membership swing (a renamed
    attribute looks exactly like this) before it becomes a push.
    """
    return _tool(lambda oc: oc.preview_group(group))


@mcp.tool()
def attribute_keys() -> str:
    """Attribute keys and sample values reported by the live fleet.

    Use these to write a group definition or a proposal filter: matching is exact,
    case-insensitive equality, so `env` never matches `environment`. Descriptions
    only — never an effective config.
    """
    return _tool(lambda oc: oc.attribute_keys())


def create_group(
    name: str,
    definition: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
    include: Optional[list] = None,
    exclude: Optional[list] = None,
    description: Optional[str] = None,
    owner: Optional[str] = None,
) -> str:
    """Create a named selector (operator; write-gated). Pushes nothing.

    Pass exactly one selector: `query` (SQL, #132, recommended) or `definition` (a
    legacy filter document). Use `agent_query_schema` to write a query that matches
    real columns and values.

    Only present when OPAMP_MCP_ALLOW_WRITES=true: a group changes the blast radius
    of every future push that names it, which is exactly the sort of decision a human
    should be making. Approving, applying and archiving stay unavailable at any
    setting (D9).
    """
    return _tool(
        lambda oc: oc.create_group(
            name,
            query=query,
            definition=definition,
            include=include,
            exclude=exclude,
            description=description,
            owner=owner,
        )
    )


def propose_config(
    config_map: Dict[str, Any],
    scope: Dict[str, Any],
    note: Optional[str] = None,
) -> str:
    """Propose a config revision (operator; write-gated). **Delivers nothing.**

    Args:
        config_map: {"<object name>": {"body": "...", "content_type": "text/yaml",
            "role": "<optional, agent-defined>"}}.
        scope: Exactly one of {"agent_id": id}, {"agents": [ids]},
            {"filter": {...}} (the GET /agents filter keys) or {"group": "prod-emea"}
            (a saved selector, #111), plus an optional "include_future": true on a
            filter/group scope. A missing scope is refused — it is never "the whole
            fleet".
        note: Why, recorded in the audit log.

    Returns the proposal id, the per-agent revisions and the validation verdict. A
    **human** approver must approve it before any agent sees it: approval is
    deliberately not an MCP tool (D3/D9). Only present when
    OPAMP_MCP_ALLOW_WRITES=true, because it mutates the revision store.
    """
    return _tool(lambda oc: oc.propose_config(config_map, scope=scope, note=note))


# ---------------------------------------------------------------- credentials


def create_agent_token(agent_id: str, label: str = "", retire_previous: bool = False) -> str:
    """Mint a per-agent OpAMP credential (admin; write-gated, requires ADMIN_PASSWORD).

    The token is returned once — only its hash is stored. The response includes a
    paste-ready `collector_snippet` for the agent's config. `retire_previous`
    revokes older credentials immediately instead of on first use of the new one.
    """
    return _tool(
        lambda oc: oc.create_agent_token(
            agent_id, label=label, retire_previous=retire_previous
        )
    )


def revoke_agent_token(agent_id: str) -> str:
    """Revoke every live credential for one agent (admin; write-gated).

    Blocks OpAMP delivery but keeps the agent's state — revocation is not a wipe.
    """
    return _tool(lambda oc: oc.revoke_agent_token(agent_id))


@mcp.tool()
def list_agent_tokens() -> str:
    """List agent tokens with expiry, cap and use (read-only; any role).

    Never returns a token or a hash — `token_id` is the handle used to revoke one.
    `agents_used`/`max_agents` is the cap ("3 of 15"), and `expired`/`active` are
    computed server-side. Check this before blaming an agent for a 401: `expired` and
    `agent_limit` are token-side refusals, not agent-side misconfiguration.
    """
    return _tool(lambda oc: oc.list_agent_tokens())


def create_fleet_token(
    label: str = "",
    expires_in_days: Optional[int] = None,
    max_agents: Optional[int] = None,
) -> str:
    """Mint a fleet agent token (admin; write-gated, requires ADMIN_PASSWORD).

    Not bound to an instance_uid, so it can be minted *before* any agent has
    contacted the server — this is the token that onboards a fleet. `max_agents` is
    the cap that stops a leaked token from consuming the fleet (the agent over the
    cap is refused `agent_limit`), and `expires_in_days` (1-365, or null for never)
    bounds how long a leak is useful.

    The token is returned once — only its hash is stored — with `token_id` (the
    handle to list or revoke it) and a paste-ready `collector_snippet`.
    """
    return _tool(
        lambda oc: oc.create_fleet_token(
            label=label, expires_in_days=expires_in_days, max_agents=max_agents
        )
    )


def revoke_fleet_token(token_id: str) -> str:
    """Revoke one token by its handle (admin; write-gated).

    Blocks new messages immediately, keeps the row (audit trail, cap count) and
    leaves every agent's state alone.
    """
    return _tool(lambda oc: oc.revoke_fleet_token(token_id))


def set_update_check(enabled: Optional[bool] = None) -> str:
    """Enable/disable automatic update checks (admin override); null clears it."""
    return _tool(lambda oc: oc.set_update_check(enabled))


def set_agent_type(agent_id: str, agent_type: Optional[str] = None) -> str:
    """Pin an agent's type, or return it to auto-detection (operator; write-gated).

    Use when detection is wrong or the agent cannot say what it is: a pin outranks
    every signal the agent sends, survives restarts and is audited. Pass null to
    clear the pin. Names come from list_agent_types; an unknown name is refused (422)
    rather than silently treated as `generic`.
    """
    return _tool(lambda oc: oc.set_agent_type(agent_id, agent_type))


def _register_write_tools() -> None:
    """Expose the write tools only with OPAMP_MCP_ALLOW_WRITES=true (#94, #106)."""
    if not writes_enabled():
        return
    mcp.tool()(create_agent_token)
    mcp.tool()(revoke_agent_token)
    # #126: minting a fleet token is the operation that grants access to the endpoint,
    # so it sits behind the same gate as every other write.
    mcp.tool()(create_fleet_token)
    mcp.tool()(revoke_fleet_token)
    # #99/D9: MCP may *propose*. Approve, reject, reactivate and rollback are never
    # tools here, at any setting — approval is a human role, and a client that can
    # approve its own proposal has no separation of duties to speak of.
    mcp.tool()(propose_config)
    # #111: creating a group is propose-side intent (it pushes nothing) but it decides
    # the blast radius of every future push, so it sits behind the same gate.
    mcp.tool()(create_group)
    mcp.tool()(set_update_check)
    # #118: a pin changes what the server is willing to claim about an agent (and
    # therefore which validator its configs face), so it is a write, not a read.
    mcp.tool()(set_agent_type)


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


_register_write_tools()


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
