"""opampctl — CLI for the OpAMP server, built for humans and AI agents alike.

Every documented REST endpoint has a CLI equivalent. Output is **JSON by
default** (agents parse JSON best); use ``--raw`` where a command produces a
markdown/YAML payload to print just that text.

Configuration (flags beat env vars):
- ``--server URL`` / ``OPAMP_SERVER_URL``       server base URL (default http://localhost:4320)
- ``--password PW`` / ``ADMIN_PASSWORD``        admin password (Basic auth, legacy)
- ``--token TOK`` / ``OPAMP_ADMIN_TOKEN``       admin session token (``opampctl auth login``)
- ``--db PATH``                                 offline mode: read SQLite state directly,
                                                no server required (read-only commands only)

Examples::

    opampctl health
    opampctl agents list --healthy true --attr environment=prod
    opampctl agents manifest <agent-id> --raw
    opampctl reports fleet --raw
    opampctl alerts set @alerts.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from client.opamp_client import (
    OpampApiError,
    OpampClient,
    OpampClientError,
    OpampConnectionError,
)

app = typer.Typer(
    name="opampctl",
    help="Inspect OpAMP agents, build OCB manifests, run compliance checks, manage alerts.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

agents_app = typer.Typer(help="Agent inspection commands", no_args_is_help=True)
token_app = typer.Typer(
    help="Agent credentials for OPAMP_AGENT_AUTH=token (#94)", no_args_is_help=True
)
reports_app = typer.Typer(help="Markdown fleet reports", no_args_is_help=True)
compliance_app = typer.Typer(help="OPA policy compliance", no_args_is_help=True)
alerts_app = typer.Typer(help="Alert configuration (admin)", no_args_is_help=True)
auth_app = typer.Typer(
    help="Admin auth: mode, sessions and identities (#95)", no_args_is_help=True
)
config_app = typer.Typer(
    help="Remote config: propose, approve, revise, roll back (#81, #99, #100)",
    no_args_is_help=True,
)
agent_config_app = typer.Typer(
    help="Desired/effective config for one agent (#81)", no_args_is_help=True
)
rollout_app = typer.Typer(
    help="Staged rollouts: plan, promote, abort, roll back (#101, #103)",
    no_args_is_help=True,
)
audit_app = typer.Typer(
    help="Audit log: query, verify, export (#97)", no_args_is_help=True
)
updates_app = typer.Typer(
    help="Server update checks (#46)", no_args_is_help=True
)

app.add_typer(agents_app, name="agents", help="Agent inspection commands")
agents_app.add_typer(token_app, name="token", help="Agent credentials (admin)")
agents_app.add_typer(agent_config_app, name="config", help="Remote config for one agent")
app.add_typer(reports_app, name="reports", help="Markdown fleet reports")
app.add_typer(compliance_app, name="compliance", help="OPA policy compliance")
app.add_typer(alerts_app, name="alerts", help="Alert configuration (admin)")
app.add_typer(auth_app, name="auth", help="Admin auth: mode, sessions, identities")
app.add_typer(config_app, name="config", help="Remote config control plane")
config_app.add_typer(rollout_app, name="rollout", help="Staged rollouts (#101)")
app.add_typer(audit_app, name="audit", help="Audit log: query, verify, export")
app.add_typer(updates_app, name="updates", help="Server update checks (#46)")


class _Settings:
    """Parsed global options shared by all commands."""

    server: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    db: Optional[Path] = None
    pretty: bool = False

    @property
    def offline(self) -> bool:
        return self.db is not None


_settings = _Settings()


@app.callback()
def _main(
    server: Optional[str] = typer.Option(
        None, "--server", "-s", envvar="OPAMP_SERVER_URL",
        help="Server base URL [default: OPAMP_SERVER_URL or http://localhost:4320]",
    ),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="ADMIN_PASSWORD",
        help="Admin password (Basic auth, legacy) [default: ADMIN_PASSWORD]",
    ),
    token: Optional[str] = typer.Option(
        None, "--token", "-t", envvar="OPAMP_ADMIN_TOKEN",
        help="Admin session token from `opampctl auth login` [default: OPAMP_ADMIN_TOKEN]. "
        "Wins over --password when both are set.",
    ),
    db: Optional[Path] = typer.Option(
        None, "--db",
        help="Offline mode: read this SQLite state file directly (no server). "
        "Supports read-only commands (agents, reports, manifest); live commands "
        "(health, compliance, alerts) exit with an error.",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", help="Pretty-print JSON with indentation",
    ),
) -> None:
    _settings.server = server
    _settings.password = password
    _settings.token = token
    _settings.db = db
    _settings.pretty = pretty


# --------------------------------------------------------------------- output

def _emit(data: Any, raw_field: Optional[str] = None) -> None:
    """Print payload: raw text of data[raw_field], else JSON (pretty optional)."""
    if raw_field is not None:
        if isinstance(data, dict) and raw_field in data:
            text = data[raw_field]
            if not isinstance(text, str):
                text = json.dumps(text, indent=2 if _settings.pretty else None, default=str)
            typer.echo(text)
            return
        _fail(f"Response has no {raw_field!r} field to print with --raw", data)
    typer.echo(json.dumps(data, indent=2 if _settings.pretty else None, default=str))


def _fail(message: str, data: Any = None) -> None:
    """Structured error to stderr, JSON shape so agents can parse failures too."""
    payload: Dict[str, Any] = {"error": message}
    if data is not None:
        payload["response"] = data
    typer.echo(json.dumps(payload, indent=2 if _settings.pretty else None, default=str), err=True)
    raise typer.Exit(code=1)


def _client() -> OpampClient:
    if _settings.offline:
        _fail(
            "This command requires a live server and is unavailable with --db; "
            "drop --db to talk to the server."
        )
    try:
        return OpampClient(
            base_url=_settings.server,
            password=_settings.password,
            token=_settings.token,
        )
    except OpampClientError as exc:
        _fail(str(exc))


def _client_call(action):
    """Run a client call and return its value, with the same error mapping as `_run`.

    For the few commands that compose two calls (a dry run that resolves the selector
    first, then prints what it *would* do) rather than emitting one response.
    """
    try:
        with _client() as oc:
            return action(oc)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))
    except OpampClientError as exc:
        _fail(str(exc))


def _run(action, raw_field: Optional[str] = None) -> None:
    """Execute a client call, mapping client errors to structured CLI errors."""
    try:
        with _client() as oc:
            _emit(action(oc), raw_field=raw_field)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))
    except OpampClientError as exc:
        _fail(str(exc))


# ------------------------------------------------------------------ offline db

def _offline_states() -> List[Any]:
    """Load agent states from the SQLite file given via --db."""
    from server.state import SQLiteAgentStore

    store = SQLiteAgentStore(_settings.db)
    states = list(store.load_all().values())
    if not states:
        _fail(f"No agents found in {_settings.db}")
    return states


def _offline_metrics(agent_id: str) -> Dict[str, Any]:
    from server.state import SQLiteMetricsStore

    return SQLiteMetricsStore(_settings.db).get(agent_id) or {}


def _offline_audit_store():
    """Open the audit log straight from `--db` — no server, no credential (#97)."""
    from server.state import SQLiteAuditStore

    return SQLiteAuditStore(_settings.db)


def _offline_get_state(agent_id: str) -> Any:
    for state in _offline_states():
        if state.agent_id == agent_id:
            return state
    _fail(f"Agent {agent_id!r} not found in {_settings.db}")


# ----------------------------------------------------------------------- ops

@app.command()
def health() -> None:
    """Server health: status, connected agents, OPA availability."""
    _run(lambda oc: oc.health())


def _read_config_file(path: Path, role: str = "") -> Dict[str, Any]:
    """Build a one-object config map from a file on disk.

    The file *is* the body; `--content-type` describes it. That is the shape a
    collector config has in the real world, and it keeps `opampctl` usable in a
    pipeline: `opampctl agents config set <id> --file collector.yaml`.
    """
    try:
        body = path.read_text()
    except OSError as exc:
        _fail(f"Cannot read {path}: {exc}")
    entry: Dict[str, Any] = {"body": body, "content_type": "text/yaml"}
    if role:
        # v0.20.0 `AgentConfigObject.role`: agent-defined, carried verbatim (#68).
        entry["role"] = role
    return {"": entry}


@config_app.command("posture")
def config_posture() -> None:
    """Config push security posture: which requirements are met, and why not.

    Any role may read it (`viewer`); the anonymous viewer of a read-only server
    (no credential configured) can read it too.
    """
    _run(lambda oc: oc.config_posture())


@config_app.command("validate")
def config_validate(
    file: Path = typer.Option(..., "--file", "-f", exists=True, dir_okay=False,
                              help="Config body to validate"),
    content_type: str = typer.Option("text/yaml", "--content-type",
                                     help="MIME type of the body"),
    role: str = typer.Option("", "--role",
                             help="Agent-defined role for this config object (v0.20.0)"),
) -> None:
    """Validate a config body without storing it (live server only).

    Always exits 0 with `{"valid": bool, "errors": [...], "config_hash": ...}` — an
    invalid config is an answer, not a failure. Use it in CI before proposing.
    """
    config_map = _read_config_file(file, role=role)
    config_map[""]["content_type"] = content_type
    _run(lambda oc: oc.validate_config(config_map))


@config_app.command("propose")
def config_propose(
    file: Path = typer.Option(..., "--file", "-f", exists=True, dir_okay=False,
                              help="Config body to propose"),
    content_type: str = typer.Option("text/yaml", "--content-type",
                                     help="MIME type of the body"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Target agent id"),
    agents: Optional[str] = typer.Option(None, "--agents",
                                         help="Comma-separated target agent ids"),
    attr: List[str] = typer.Option([], "--attr",
                                   help="Selector: --attr environment=prod (repeatable)"),
    include_future: bool = typer.Option(
        False, "--include-future",
        help="With a selector: also apply to agents that match it later",
    ),
    allow_endpoint_change: bool = typer.Option(
        False, "--allow-endpoint-change",
        help="Permit moving the opamp endpoint away from this server (approver confirms again)",
    ),
    override_reason: Optional[str] = typer.Option(
        None, "--override-reason",
        help="Record why a policy violation is acceptable (audited)",
    ),
    role: str = typer.Option("", "--role",
                             help="Agent-defined role for this config object (v0.20.0)"),
    note: Optional[str] = typer.Option(None, "--note", help="Reason, recorded in the audit log"),
) -> None:
    """Propose a config revision — drafts it, delivers nothing (operator).

    One of `--agent`, `--agents` or `--attr` is required: a missing selector is
    never "every agent". The #100 pipeline runs here (YAML parse, structural rules,
    protected invariants, agent inventory, OPA) and the structured verdict comes
    back. An approver must run `opampctl config approve <proposal_id>` before any
    agent receives it.
    """
    config_map = _read_config_file(file, role=role)
    config_map[""]["content_type"] = content_type

    resolved_agents = [item.strip() for item in (agents or "").split(",") if item.strip()]
    filters: Dict[str, Any] = {}
    for item in attr:
        key, _, value = item.partition("=")
        filters.setdefault(key, []).append(value)

    chosen = sum(1 for value in (agent, resolved_agents or None, filters or None) if value)
    if chosen != 1:
        _fail(
            "provide exactly one scope: --agent <id>, --agents a,b or --attr key=value",
            data={"hint": "a missing scope is never \"all agents\""},
        )

    if agent:
        scope: Dict[str, Any] = {"agent_id": agent}
    elif resolved_agents:
        scope = {"agents": resolved_agents}
    else:
        scope = {"filter": filters, "include_future": include_future}

    _run(
        lambda oc: oc.propose_config(
            config_map,
            scope=scope,
            note=note,
            allow_endpoint_change=allow_endpoint_change,
            override_reason=override_reason,
        )
    )


@config_app.command("revisions")
def config_revisions(
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Filter by agent id"),
    status: Optional[str] = typer.Option(
        None, "--status",
        help="proposed|active|superseded|rolled_back|poisoned|rejected",
    ),
    limit: int = typer.Option(100, "--limit", "-n", help="Maximum rows"),
) -> None:
    """Revision history: metadata, status, verdict and delivery facts (viewer).

    **No config bodies** — hashes only (the bodies are secrets). Works offline
    (`--db`), which is what makes "what did we push and when" answerable from a
    backup.
    """
    if _settings.offline:
        from server import config_control
        from server.state import SQLiteDesiredConfigStore

        store = SQLiteDesiredConfigStore(_settings.db)
        rows = store.list_revisions(agent_id=agent, status=status, limit=limit)
        _emit(
            {
                "revisions": [config_control.revision_view(row) for row in rows],
                "count": len(rows),
                "statuses": list(SQLiteDesiredConfigStore.STATUSES),
                "note": "offline: revision metadata only, never a config body",
            }
        )
        return
    _run(lambda oc: oc.list_config_revisions(agent=agent, status=status, limit=limit))


@config_app.command("show")
def config_show(proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)")) -> None:
    """One proposal: every per-agent revision, its status, verdict and delivery record.

    Works offline (`--db`). Bodies are never included; read one agent's desired
    config with `opampctl agents config get <id>`.
    """
    if _settings.offline:
        from server import config_control
        from server.state import SQLiteDesiredConfigStore

        store = SQLiteDesiredConfigStore(_settings.db)
        rows = store.by_proposal(proposal_id)
        if not rows:
            _fail(f"Proposal {proposal_id!r} not found in {_settings.db}")
        statuses = {row.get("status") for row in rows}
        _emit(
            {
                "proposal_id": proposal_id,
                "status": next(iter(statuses)) if len(statuses) == 1 else "mixed",
                "statuses": sorted(status for status in statuses if status),
                "revisions": [config_control.revision_view(row) for row in rows],
                "agents": len({row["agent_id"] for row in rows}),
                "config_hash": rows[0].get("config_hash"),
                "note": "offline: metadata only",
            }
        )
        return
    _run(lambda oc: oc.get_config_proposal(proposal_id))


@config_app.command("approve")
def config_approve(
    proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)" ),
    note: Optional[str] = typer.Option(None, "--note", help="Reason, recorded in the audit log"),
    override_reason: Optional[str] = typer.Option(
        None, "--override-reason",
        help="Approve despite a policy violation (recorded and audited)",
    ),
    confirm_endpoint_change: bool = typer.Option(
        False, "--confirm-endpoint-change",
        help="Confirm that moving the opamp endpoint is intended",
    ),
    confirm_scope_change: bool = typer.Option(
        False, "--confirm-scope-change",
        help="Approve even though the selector now matches agents it did not at propose time",
    ),
) -> None:
    """Approve and activate a proposal (approver, live server only).

    Re-resolves the selector and re-runs validation. A config that no longer
    validates is refused with `re-validate: …` (409); a policy violation needs
    `--override-reason`. Supersedes each agent's previous revision, which stays
    available for `opampctl agents config rollback`.
    """
    _run(
        lambda oc: oc.approve_config(
            proposal_id,
            note=note,
            confirm_endpoint_change=confirm_endpoint_change,
            confirm_scope_change=confirm_scope_change,
            override_reason=override_reason,
        )
    )


@config_app.command("reject")
def config_reject(
    proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)" ),
    reason: Optional[str] = typer.Option(None, "--reason", help="Why it was refused"),
) -> None:
    """Reject a proposal (approver, live server only). The revision is kept as evidence."""
    _run(lambda oc: oc.reject_config(proposal_id, reason=reason))


@config_app.command("reactivate")
def config_reactivate(
    proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)" ),
    reason: Optional[str] = typer.Option(None, "--reason", help="Why it may be retried now"),
    override_reason: Optional[str] = typer.Option(
        None, "--override-reason", help="Override a policy violation (recorded and audited)"
    ),
) -> None:
    """Re-approve a **poisoned** revision and resume delivery (approver, live server only).

    Nothing comes back on a timer: a revision poisoned by `FAILED` or by a detected
    auto-revert only returns through this explicit decision, and it is validated again
    first.
    """
    _run(lambda oc: oc.reactivate_config(proposal_id, reason=reason, override_reason=override_reason))


@config_app.command("apply")
def config_apply(
    file: Path = typer.Option(..., "--file", "-f", exists=True, dir_okay=False,
                              help="Config body to push"),
    content_type: str = typer.Option("text/yaml", "--content-type",
                                     help="MIME type of the body"),
    agent: List[str] = typer.Option([], "--agent", "-a",
                                    help="Target agent id, repeatable"),
    attr: List[str] = typer.Option([], "--attr",
                                   help="Filter by metadata, repeatable: --attr environment=prod"),
    confirm: bool = typer.Option(False, "--confirm", "--yes", "-y",
                                 help="Required: acknowledge the blast radius (--yes is the "
                                      "#106 spelling, --confirm the legacy one)"),
    role: str = typer.Option("", "--role",
                             help="Agent-defined role for this config object (v0.20.0)"),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the resolved target list and exit; pushes nothing",
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Reason, recorded in the audit log"),
) -> None:
    """Push one config to many agents (admin, live server only).

    Requires `--confirm`/`--yes` and an explicit selector (`--agent` and/or `--attr`).
    A missing selector is refused — it never means "all agents".

    `--dry-run` resolves the selector server-side and prints the exact target list
    (agent ids, count) without storing or delivering anything, which is the check to
    make before typing `--yes`. For anything larger than one host, prefer the staged
    flow (`config rollout plan` → `config rollout create`) over this direct push: this
    one skips staging by design.
    """
    config_map = _read_config_file(file, role=role)
    config_map[""]["content_type"] = content_type
    filters: Dict[str, Any] = {}
    for item in attr:
        key, _, value = item.partition("=")
        filters.setdefault(key, []).append(value)
    if dry_run:
        _emit(_resolve_targets_preview(filters, agent, config_map))
        return
    _run(
        lambda oc: oc.apply_agent_config(
            config_map,
            agents=agent or None,
            filter=filters or None,
            confirm=confirm,
            note=note,
        )
    )


def _resolve_targets_preview(
    filters: Dict[str, Any], agents: List[str], config_map: Dict[str, Any]
) -> Dict[str, Any]:
    """`config apply --dry-run`: the resolved targets, from the server's own filter.

    Read-only. It asks `GET /agents` with the same selector the push would use, so the
    list printed is the list that would be hit — resolving it client-side is how a
    dry run ends up disagreeing with the real thing.
    """
    targets = agents
    matched_by = "agents"
    if not targets and filters:
        listing = _client_call(lambda oc: oc.list_agents(**{
            key: value[0] if len(value) == 1 else value
            for key, value in filters.items()
        }))
        targets = [row["id"] for row in listing.get("agents", [])]
        matched_by = "filter"
    return {
        "dry_run": True,
        "matched_by": matched_by,
        "agents": len(targets),
        "targets": targets,
        "objects": len(config_map),
        "delivery": {
            "state": "unchanged",
            "reason": "dry_run",
            "detail": "nothing was stored or delivered",
        },
    }


@config_app.command("diff")
def config_diff(
    agent_id: str = typer.Argument(..., help="Agent id (hex instance_uid)"),
    reveal: bool = typer.Option(
        False, "--reveal", help="Show the desired body unredacted (approver, audited)"
    ),
) -> None:
    """What this agent *should* be running versus what it *is* running (viewer).

    Prints the desired revision/hash, the reported hash, `drift`
    (`true | false | null`) and the YAML paths the desired config is missing from the
    agent's effective config. `null` means "cannot tell yet" — not healthy, not broken.

    Drift is deliberately a *containment* check, not a hash compare: the supervisor
    composes and re-serialises files, so a byte compare reports drift on every healthy
    agent.
    """
    detail = _client_call(lambda oc: oc.get_agent_config(agent_id, reveal=reveal))
    desired = detail.get("desired") or {}
    assignment = detail.get("assignment") or {}
    drift = detail.get("drift") or {}
    _emit(
        {
            "agent_id": agent_id,
            "desired": {
                "revision_id": desired.get("revision_id"),
                "config_hash": desired.get("config_hash"),
                "status": desired.get("status"),
                "held": desired.get("held"),
                "withdrawn": desired.get("withdrawn"),
            },
            "assignment": {
                "state": assignment.get("state"),
                "delivered_at": assignment.get("delivered_at"),
                "channel": assignment.get("channel"),
                "attempts": assignment.get("attempts"),
                "last_status": assignment.get("last_status"),
                "last_error": assignment.get("last_error"),
            },
            "reported_hash": (detail.get("remote_config_status") or {}).get(
                "last_remote_config_hash"
            ),
            "drift": drift.get("drift"),
            "drift_reason": drift.get("reason"),
            "missing": drift.get("missing") or [],
            "config_push_allowed": detail.get("config_push_allowed"),
        }
    )


@config_app.command("revalidate")
def config_revalidate(
    proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)"),
) -> None:
    """Re-run validation for a proposal and change nothing (operator, live server only).

    The pipeline runs against each resolved agent, so the inventory warnings are about
    the machines an approval would actually reach. Compare `fingerprint` with the
    stored `stored` verdict to see whether anything moved since it was proposed.
    """
    _run(lambda oc: oc.revalidate_config(proposal_id))


@config_app.command("rollouts")
def config_rollouts(
    status: Optional[str] = typer.Option(
        None, "--status", help="pending|running|paused|completed|aborted|cancelled"
    ),
    limit: int = typer.Option(100, "--limit", min=1, max=1000),
) -> None:
    """List staged rollouts, newest first, with per-state agent counts (viewer)."""
    _run(lambda oc: oc.list_config_rollouts(status=status, limit=limit))


@rollout_app.command("plan")
def config_rollout_plan(
    proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)"),
    canary_percent: Optional[float] = typer.Option(
        None, "--canary-percent", help="Canary size as a % of the fleet [default: 5]"
    ),
    max_agents: Optional[int] = typer.Option(
        None, "--max-agents", help="Absolute batch cap [default: 25]"
    ),
    max_percent: Optional[float] = typer.Option(
        None, "--max-percent", help="Relative batch cap, % of fleet [default: 25]"
    ),
    health_gate: Optional[int] = typer.Option(
        None, "--health-gate", help="Seconds a batch must stay green [default: 90]"
    ),
    restart: bool = typer.Option(
        False, "--restart", help="Queue a restart for each agent once its batch passes"
    ),
) -> None:
    """Dry run: show the exact batches and caps. Approves, holds and delivers nothing."""
    _run(
        lambda oc: oc.plan_config_rollout(
            proposal_id,
            canary_percent=canary_percent,
            max_agents_per_batch=max_agents,
            max_percent_per_batch=max_percent,
            health_gate_seconds=health_gate,
            restart_after_apply=restart,
        )
    )


@rollout_app.command("create")
def config_rollout_create(
    proposal_id: str = typer.Argument(..., help="Proposal id (prop-…)"),
    canary_percent: Optional[float] = typer.Option(None, "--canary-percent"),
    max_agents: Optional[int] = typer.Option(None, "--max-agents"),
    max_percent: Optional[float] = typer.Option(None, "--max-percent"),
    health_gate: Optional[int] = typer.Option(None, "--health-gate"),
    ttl_minutes: Optional[int] = typer.Option(
        None, "--ttl-minutes", help="Cancel a stalled rollout after N minutes [default: 60]"
    ),
    restart: bool = typer.Option(
        False, "--restart", help="Queue a restart for each agent once its batch passes"
    ),
    confirm_scope_change: bool = typer.Option(
        False, "--confirm-scope-change",
        help="Acknowledge that the selector now covers more agents than when proposed",
    ),
    override_reason: Optional[str] = typer.Option(
        None, "--override-reason", help="Override a policy violation (recorded, audited)"
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Reason, recorded in the audit log"),
) -> None:
    """Approve a proposal **and** stage it: canary first, gated after that (approver).

    Creating the rollout is the approval. Only the canary batch is released; the rest
    of the fleet stays held until the gate clears for the batch before it.
    """
    _run(
        lambda oc: oc.create_config_rollout(
            proposal_id,
            note=note,
            canary_percent=canary_percent,
            max_agents_per_batch=max_agents,
            max_percent_per_batch=max_percent,
            health_gate_seconds=health_gate,
            ttl_minutes=ttl_minutes,
            restart_after_apply=restart,
            confirm_scope_change=confirm_scope_change,
            override_reason=override_reason,
        )
    )


@rollout_app.command("show")
def config_rollout_show(
    rollout_id: str = typer.Argument(..., help="Rollout id (ro-…)"),
) -> None:
    """Batch progress, per-agent state and — when paused — exactly why (viewer)."""
    _run(lambda oc: oc.get_config_rollout(rollout_id))


@rollout_app.command("abort")
def config_rollout_abort(
    rollout_id: str = typer.Argument(..., help="Rollout id (ro-…)"),
    reason: Optional[str] = typer.Option(None, "--reason"),
) -> None:
    """Stop promoting **now** (operator). Already-delivered configs are not recalled."""
    _run(lambda oc: oc.abort_config_rollout(rollout_id, reason=reason))


@rollout_app.command("rollback")
def config_rollout_rollback(
    rollout_id: str = typer.Argument(..., help="Rollout id (ro-…)"),
    health_gate: Optional[int] = typer.Option(None, "--health-gate"),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    """Roll back what this rollout applied, as its own gated rollout (approver)."""
    _run(
        lambda oc: oc.rollback_config_rollout(
            rollout_id, note=note, health_gate_seconds=health_gate
        )
    )


@agent_config_app.command("restart")
def agent_restart(
    agent_id: str = typer.Argument(..., help="Agent id (hex instance_uid)"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Recorded in the audit log"),
) -> None:
    """Ask an agent to restart on its next poll (operator, live server only).

    Queued, delivered **exactly once**, and refused with 409 when the collector does
    not set `accepts_restart_command: true` in its `opamp` extension.
    """
    _run(lambda oc: oc.restart_agent(agent_id, reason=reason))


@config_app.command("rotate-key")
def config_rotate_key(
    db: Path = typer.Option(..., "--db", exists=True, dir_okay=False,
                            help="Path to opamp.db (required: this is a maintenance command)"),
) -> None:
    """Re-encrypt stored config bodies under the current CONFIG_AT_REST_KEY (#98).

    Rotation procedure: set the new `CONFIG_AT_REST_KEY`, keep the old one in
    `CONFIG_AT_REST_KEY_PREVIOUS`, run this, then drop the previous key. Runs
    against the database file directly (no server, no credential), so it works on a
    stopped server or a restored backup. Exits non-zero if any row cannot be read —
    a rotation that silently skips rows is worse than one that stops.
    """
    from server import crypto
    from server.state import SQLiteAgentStore, SQLiteDesiredConfigStore

    if not crypto.configured():
        _fail(f"{crypto.KEY_ENV} is not set: nothing to rotate to")
    agent_store = SQLiteAgentStore(db)
    config_store = SQLiteDesiredConfigStore(db)
    rotated, failed = 0, []

    for revision in config_store.sealed_revisions():
        blob = revision.get("config_blob")
        if not crypto.needs_rotation(blob):
            continue
        try:
            config_store.reseal(
                revision["revision_id"],
                crypto.rotate_sealed(blob, f"revision:{revision['revision_id']}"),
            )
            rotated += 1
        except crypto.ConfigDecryptError as exc:
            failed.append({"revision_id": revision["revision_id"], "error": str(exc)})

    for agent_id, state in agent_store.load_all().items():
        sealed = state.sealed_effective_config
        if not sealed or not crypto.needs_rotation(sealed):
            continue
        try:
            agent_store.upsert(
                type(state)(
                    **{
                        **state.__dict__,
                        "effective_config": None,
                        "sealed_effective_config": crypto.rotate_sealed(
                            sealed, f"agent:{agent_id}"
                        ),
                    }
                )
            )
            rotated += 1
        except crypto.ConfigDecryptError as exc:
            failed.append({"agent_id": agent_id, "error": str(exc)})

    _emit({"rotated": rotated, "failed": failed, "db": str(db)})
    if failed:
        raise typer.Exit(code=1)


# ------------------------------------------------------------------- admin auth

principals_app = typer.Typer(
    help="Admin identities and roles (admin role)", no_args_is_help=True
)
sessions_app = typer.Typer(
    help="Live admin sessions (admin role)", no_args_is_help=True
)
auth_app.add_typer(principals_app, name="principals")
auth_app.add_typer(sessions_app, name="sessions")


@auth_app.command("status")
def auth_status() -> None:
    """How this server authenticates admin callers (no secrets)."""
    _run(lambda oc: oc.auth_status())


@auth_app.command("verify")
def auth_verify() -> None:
    """Verify the configured credential against the server (exit 0 = ok).

    Works with `--password` or `--token`; any role passes. Repeated failures feed
    the server's login lockout, so do not loop on this.
    """
    if _settings.token:
        _run(lambda oc: oc.whoami())
        return
    try:
        with _client() as oc:
            result = oc.auth_verify(oc.password or "")
            _emit(result)
            if not result.get("verified"):
                raise typer.Exit(code=1)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))


@auth_app.command("login")
def auth_login(
    name: Optional[str] = typer.Option(
        None, "--name", "-n", help="Principal name (omit for the legacy ADMIN_PASSWORD)"
    ),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="ADMIN_PASSWORD",
        help="Password to log in with [default: ADMIN_PASSWORD]",
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Print just the token (for `export OPAMP_ADMIN_TOKEN=$(...)`)"
    ),
) -> None:
    """Log in and print a session token — the token IS a credential, treat it as one.

    Print it into an env var (`export OPAMP_ADMIN_TOKEN=$(opampctl auth login --raw)`)
    rather than into a chat, an issue or a commit. Sessions expire after
    `ADMIN_SESSION_TTL_SECONDS` and after `ADMIN_SESSION_IDLE_SECONDS` idle; revoke
    with `opampctl auth logout`. Use `--raw` only in a pipeline.
    """
    secret = password if password is not None else (_settings.password or "")
    _run(
        lambda oc: oc.login(name or "", secret),
        raw_field="token" if raw else None,
    )


@auth_app.command("logout")
def auth_logout() -> None:
    """Revoke the session token this command is using (`--token`)."""
    _run(lambda oc: oc.logout())


@auth_app.command("whoami")
def auth_whoami() -> None:
    """Who am I: principal name, role and session expiry."""
    _run(lambda oc: oc.whoami())


@sessions_app.command("list")
def auth_sessions_list() -> None:
    """Live sessions with their principal, client IP and expiry (admin role)."""
    _run(lambda oc: oc.list_sessions())


@sessions_app.command("revoke")
def auth_sessions_revoke(
    session_id: str = typer.Argument(..., help="Short session id from `auth sessions list`"),
) -> None:
    """Revoke one session — "sign that device out" (admin role)."""
    _run(lambda oc: oc.revoke_session(session_id))


@principals_app.command("list")
def auth_principals_list() -> None:
    """Admin identities and their roles (admin role)."""
    _run(lambda oc: oc.list_principals())


@principals_app.command("create")
def auth_principals_create(
    name: str = typer.Argument(..., help="Principal name (used as the HTTP Basic username)"),
    role: str = typer.Option(
        "viewer", "--role", "-r",
        help="viewer | operator | approver | admin (a role satisfies the ones below it)",
    ),
    password: str = typer.Option(
        ..., "--password", "-p", prompt=False,
        help="Password for the new principal (min 8 chars; scrypt-hashed server-side)",
    ),
    expires_at: Optional[str] = typer.Option(
        None, "--expires-at", help="Optional ISO-8601 expiry, e.g. 2026-12-31T00:00:00Z"
    ),
) -> None:
    """Create an identity with a role (admin role).

    `operator` proposes/applies approved revisions, `approver` approves/rejects/
    rolls back; keeping them as different people is the point of the two-phase
    push (#99, #103). The password is never returned or logged.
    """
    _run(lambda oc: oc.create_principal(name, role, password, expires_at=expires_at))


@principals_app.command("revoke")
def auth_principals_revoke(
    principal_id: str = typer.Argument(..., help="principal_id from `auth principals list`"),
) -> None:
    """Revoke an identity and close its sessions (admin role).

    The row is kept so the name stays reserved — "remove alice, re-add alice" must
    not silently hand her identity to someone else.
    """
    _run(lambda oc: oc.revoke_principal(principal_id))


# ---------------------------------------------------------------------- audit


@audit_app.command("list")
def audit_list(
    since: Optional[str] = typer.Option(None, "--since", help="ISO-8601 lower bound (inclusive)"),
    until: Optional[str] = typer.Option(None, "--until", help="ISO-8601 upper bound (inclusive)"),
    actor: Optional[str] = typer.Option(None, "--actor", help="Exact actor, e.g. alice or agent:<id>"),
    action: Optional[str] = typer.Option(None, "--action", help="Action or prefix, e.g. config."),
    target: Optional[str] = typer.Option(None, "--target", help="Exact target_id or revision_id"),
    result: Optional[str] = typer.Option(None, "--result", help="ok | denied | error | noop"),
    limit: int = typer.Option(100, "--limit", help="Page size (max 1000)"),
    offset: int = typer.Option(0, "--offset", help="Rows to skip from the newest entry"),
) -> None:
    """Privileged-action history, newest first (admin role).

    Returns `{entries, total, count, limit, offset, filters, enabled}`. Each entry
    has an actor (`agent:<id>` for agents), a role, an action, a target and a
    result. **No config bodies and no secrets** — `config_hash` is a hash.

    `--action` is a prefix match, so `--action config.` returns every config-push
    event. Exit 1 on HTTP errors (403 means the credential is not `admin`).

    Works with `--db <path>`: reads `audit_log` straight from the SQLite file, which
    is what an on-call operator needs when the server itself is the casualty.
    """
    if _settings.offline:
        store = _offline_audit_store()
        filters = {
            "since": since,
            "until": until,
            "actor": actor,
            "action": action,
            "target": target,
            "result": result,
        }
        try:
            entries = store.list_entries(limit=limit, offset=offset, **filters)
            total = store.count_entries(**filters)
        except ValueError as exc:
            _fail(f"invalid timestamp filter: {exc}")
        _emit(
            {
                "entries": entries,
                "total": total,
                "count": len(entries),
                "limit": limit,
                "offset": offset,
                "filters": filters,
                "source": str(_settings.db),
            }
        )
        return
    _run(
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


@audit_app.command("verify")
def audit_verify() -> None:
    """Recompute the audit chain and report tampering (admin role).

    `ok: false` means the log cannot be trusted as evidence: a row was edited
    (`entry_hash` mismatch) or removed (`prev_hash` mismatch), and `broken_at`
    names the entry where it shows. `anchored_by_checkpoint: true` is benign —
    retention pruned older entries and recorded exactly what it cut.

    Exits non-zero when the chain is broken, so it can be a cron/CI check.

    Works with `--db <path>` — verifying the file on disk is the honest check when
    the running server is what you are worried about.
    """
    if _settings.offline:
        report = _offline_audit_store().verify()
        report["source"] = str(_settings.db)
        _emit(report)
        if not report["ok"]:
            raise typer.Exit(code=1)
        return
    try:
        with _client() as oc:
            report = oc.audit_verify()
            _emit(report)
            if not report.get("ok", False):
                raise typer.Exit(code=1)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))


@audit_app.command("export")
def audit_export(
    format: str = typer.Option("jsonl", "--format", "-f", help="jsonl (SIEM) or csv (spreadsheet)"),
    since: Optional[str] = typer.Option(None, "--since", help="ISO-8601 lower bound (inclusive)"),
    until: Optional[str] = typer.Option(None, "--until", help="ISO-8601 upper bound (inclusive)"),
    limit: int = typer.Option(100000, "--limit", help="Row cap (max 1000000)"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to a file instead of stdout"),
) -> None:
    """Export retained entries oldest-first (admin role).

    `--format jsonl` is what an ingest pipeline wants; `--format csv` opens in a
    spreadsheet. Only the retained window is exported — retention
    (`AUDIT_RETENTION_DAYS`) deletes older entries, so copy the log off-box if it
    is evidence. Pipe or `--output` it; it is not printed as JSON.
    """
    if format not in ("jsonl", "csv"):
        _fail("--format must be jsonl or csv")
    if _settings.offline:
        store = _offline_audit_store()
        try:
            rows = list(
                reversed(store.list_entries(since=since, until=until, limit=limit))
            )
        except ValueError as exc:
            _fail(f"invalid timestamp filter: {exc}")
        from server.audit import format_entries

        body = format_entries(rows, format)
        if output is not None:
            output.write_text(body, encoding="utf-8")
            _emit({"written": str(output), "bytes": len(body), "format": format})
            return
        typer.echo(body, nl=False)
        return
    try:
        with _client() as oc:
            body = oc.audit_export(format=format, since=since, until=until, limit=limit)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))
    if output is not None:
        output.write_text(body, encoding="utf-8")
        _emit({"written": str(output), "bytes": len(body), "format": format})
        return
    typer.echo(body, nl=False)


# --------------------------------------------------------------------- agents

@agents_app.command("list")
def agents_list(
    healthy: Optional[str] = typer.Option(
        None, "--healthy", help="Filter: true / false / unknown",
    ),
    status: Optional[str] = typer.Option(
        None, "--status", help="Remote config status filter (UNSET/APPLIED/APPLYING/FAILED)",
    ),
    attr: List[str] = typer.Option(
        [], "--attr", "-a", help="Metadata filter, repeatable: --attr environment=prod",
    ),
) -> None:
    """List agents with optional health/status/metadata filters."""
    if _settings.offline:
        from server.agent_auth import credential_summaries
        from server.state import SQLiteCredentialStore

        attr_filters: Dict[str, list] = {}
        for item in attr:
            key, _, value = item.partition("=")
            attr_filters.setdefault(key, []).append(value)
        states = _offline_states()
        credentials = credential_summaries(
            [state.agent_id for state in states], store=SQLiteCredentialStore(_settings.db)
        )
        dicts = []
        for state in states:
            if not state.matches_filters(healthy=healthy, remote_config_status=status,
                                        attributes=attr_filters):
                continue
            entry = state.to_dict()
            entry["credential"] = credentials.get(state.agent_id)
            dicts.append(entry)
        _emit({"agents": dicts, "count": len(dicts),
               "filters": {"offline_db": str(_settings.db)}})
        return
    kwargs: Dict[str, Any] = {}
    for item in attr:
        key, _, value = item.partition("=")
        kwargs.setdefault(key, []).append(value)
    _run(lambda oc: oc.list_agents(healthy=healthy, status=status, **kwargs))


@agents_app.command("get")
def agents_get(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Full details for one agent, including latest metrics."""
    if _settings.offline:
        from server.agent_auth import credential_summary
        from server.state import SQLiteCredentialStore

        state = _offline_get_state(agent_id)
        data = state.to_dict()
        data["metrics"] = _offline_metrics(agent_id)
        data["credential"] = credential_summary(
            agent_id, store=SQLiteCredentialStore(_settings.db)
        )
        _emit(data)
        return
    _run(lambda oc: oc.get_agent(agent_id))


@agents_app.command("metrics")
def agents_metrics(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Latest ingested OTLP metric values for one agent."""
    if _settings.offline:
        _emit(_offline_metrics(agent_id))
        return
    _run(lambda oc: oc.get_agent_metrics(agent_id))


@agents_app.command("manifest")
def agents_manifest(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    version: Optional[str] = typer.Option(None, "--version", help="Distro version (semver)"),
    raw: bool = typer.Option(False, "--raw", help="Print just the manifest.yaml text"),
) -> None:
    """Generate an OCB manifest.yaml for a slim collector build."""
    if _settings.offline:
        from server.manifest import generate_manifest, generate_ocb_command, validate_manifest_version, DEFAULT_VERSION

        try:
            resolved = validate_manifest_version(version or DEFAULT_VERSION)
        except ValueError as exc:
            _fail(str(exc))
        comps = _offline_get_state(agent_id).components
        if not comps or not any(c.get("used") for g in comps.values() for c in g):
            _fail("Agent has no components in use; cannot generate a buildable OCB manifest")
        result = {
            "manifest_yaml": generate_manifest(comps, resolved),
            "ocb_command": generate_ocb_command(resolved),
            "collector_version": resolved,
        }
        _emit(result, raw_field="manifest_yaml" if raw else None)
        return
    _run(lambda oc: oc.generate_manifest(agent_id, version=version),
         raw_field="manifest_yaml" if raw else None)


@agent_config_app.command("get")
def agents_config_get(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Show real secret values (approver role, audited as config.reveal)",
    ),
) -> None:
    """Desired + effective config for one agent — redacted unless `--reveal`.

    Works offline (`--db`): the effective config is read straight from SQLite and
    is **always** redacted there, because offline mode has no credential and no
    audit trail to record a reveal in.
    """
    if _settings.offline:
        from server import redaction
        from server.state import SQLiteDesiredConfigStore

        state = _offline_get_state(agent_id)
        effective, paths = state.redacted_effective_config()
        store = SQLiteDesiredConfigStore(_settings.db)
        current = store.current(agent_id)
        _emit(
            {
                "agent_id": agent_id,
                "desired": {
                    "revision_id": current["revision_id"],
                    "config_hash": current["config_hash"],
                    "author": current["author"],
                    "created_at": current["created_at"],
                    "withdrawn": bool(current.get("withdrawn")),
                    "failed_at": current.get("failed_at"),
                }
                if current
                else None,
                "effective": {
                    "config": json.loads(effective) if effective else None,
                    "redacted": bool(paths),
                    "redacted_paths": paths,
                    "config_error": state.config_error,
                },
                "remote_config_status": {
                    "status": state.remote_config_status,
                    "last_remote_config_hash": state.last_remote_config_hash,
                    "error": state.remote_config_error,
                },
                "note": "offline: config bodies are redacted and never revealed",
            }
        )
        return
    _run(lambda oc: oc.get_agent_config(agent_id, reveal=reveal))


@agent_config_app.command("set")
def agents_config_set(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    file: Path = typer.Option(..., "--file", "-f", exists=True, dir_okay=False,
                              help="Config body to push"),
    content_type: str = typer.Option("text/yaml", "--content-type",
                                     help="MIME type of the body"),
    role: str = typer.Option("", "--role",
                             help="Agent-defined role for this config object (v0.20.0)"),
    note: Optional[str] = typer.Option(None, "--note",
                                       help="Reason, recorded in the audit log"),
) -> None:
    """Push a desired config to one agent (approver, live server only).

    The file becomes the single config object (`""` key). The response's
    `delivery.state` says whether it went over a live WebSocket connection
    (`delivered`) or will ride the next status report (`queued`); a failed revision
    is never retried automatically.
    """
    config_map = _read_config_file(file, role=role)
    config_map[""]["content_type"] = content_type
    _run(lambda oc: oc.set_agent_config(agent_id, config_map, note=note))


@agent_config_app.command("rollback")
def agents_config_rollback(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    note: Optional[str] = typer.Option(None, "--note", help="Reason, recorded in the audit log"),
) -> None:
    """Roll an agent back to its previous revision (approver, live server only).

    Rollback is a new revision whose body is the previous one, so history stays
    append-only and the chain (`parent_revision_id`) records what replaced what.
    """
    _run(lambda oc: oc.rollback_agent_config(agent_id, note=note))


@agent_config_app.command("deliver")
def agents_config_deliver(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
) -> None:
    """Deliver the agent's active config now (operator, live server only).

    Re-delivery, not a new decision: the revision is already active, so this cannot
    change what any agent runs. Use it after a dropped push or to save a poll cycle.

    `delivery.state` is `delivered` (handed to the live WebSocket connection),
    `queued` (no live connection — it arrives on the next status report) or
    `unchanged` (nothing to deliver; `reason` says why). Exits 0 for all three:
    queued is the designed fallback, so it is not a failure to script against.
    """
    _run(lambda oc: oc.deliver_agent_config(agent_id))


@agent_config_app.command("withdraw")
def agents_config_withdraw(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
) -> None:
    """Withdraw the desired config (approver, live server only).

    Pushes an empty config map, which tells the agent to run its local config.
    """
    _run(lambda oc: oc.delete_agent_config(agent_id))


@agents_app.command("compliance")
def agents_compliance(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Evaluate one agent against OPA policies (live server only)."""
    _run(lambda oc: oc.get_compliance(agent_id))


# ------------------------------------------------------------------- tokens

@token_app.command("create")
def agents_token_create(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex) the token is bound to"),
    label: str = typer.Option("", "--label", "-l", help="Free-form label, e.g. the hostname"),
    retire_previous: bool = typer.Option(
        False, "--retire-previous",
        help="Revoke the agent's older credentials now instead of on first use of the new one",
    ),
    raw: bool = typer.Option(False, "--raw", help="Print just the token (for $(...) use)"),
) -> None:
    """Mint (or rotate) a per-agent credential — admin, live server only.

    The token is shown once and only its hash is stored. The JSON response carries
    a `collector_snippet`: paste it into the agent's `extensions.opamp` block.
    """
    _run(
        lambda oc: oc.create_agent_token(
            agent_id, label=label, retire_previous=retire_previous
        ),
        raw_field="token" if raw else None,
    )


@token_app.command("revoke")
def agents_token_revoke(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
) -> None:
    """Revoke every live credential for an agent — admin, live server only.

    Blocks OpAMP delivery but keeps the agent's record and state (revocation is
    not a wipe). Idempotent: `revoked: 0` means there was nothing live.
    """
    _run(lambda oc: oc.revoke_agent_token(agent_id))


@agents_app.command("report")
def agents_report(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    raw: bool = typer.Option(False, "--raw", help="Print just the markdown report"),
) -> None:
    """Markdown report for one agent."""
    if _settings.offline:
        state = _offline_get_state(agent_id)
        from server.reports import generate_agent_report

        _emit({"report_markdown": generate_agent_report({"agents": [state.to_dict()]})},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.agent_report(agent_id), raw_field="report_markdown" if raw else None)


# -------------------------------------------------------------------- reports

@reports_app.command("fleet")
def reports_fleet(raw: bool = typer.Option(False, "--raw", help="Print just the markdown")) -> None:
    """Fleet-wide agent report (versions, outdated/heavy collectors, detail)."""
    if _settings.offline:
        from server.reports import generate_agent_report

        dicts = [s.to_dict() for s in _offline_states()]
        _emit({"report_markdown": generate_agent_report({"agents": dicts}),
               "agent_count": len(dicts)},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.fleet_report(), raw_field="report_markdown" if raw else None)


@reports_app.command("heavy")
def reports_heavy(
    threshold: float = typer.Option(0.5, "--threshold", min=0.0, max=1.0,
                                    help="Unused-component ratio above which a collector is 'heavy'"),
    raw: bool = typer.Option(False, "--raw", help="Print just the markdown"),
) -> None:
    """Report collectors with many unused components."""
    if _settings.offline:
        from server.reports import generate_heavy_collectors_report, _is_heavy

        dicts = [s.to_dict() for s in _offline_states()]
        heavy = [a for a in dicts if _is_heavy(a, threshold)]
        _emit({"report_markdown": generate_heavy_collectors_report({"agents": dicts}, threshold),
               "heavy_count": len(heavy), "threshold": threshold},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.heavy_collectors_report(threshold=threshold),
         raw_field="report_markdown" if raw else None)


@reports_app.command("outdated")
def reports_outdated(
    version: str = typer.Option("0.149.0", "--version", help="Reference collector version (semver)"),
    raw: bool = typer.Option(False, "--raw", help="Print just the markdown"),
) -> None:
    """Report collectors with components older than a reference version."""
    if _settings.offline:
        from server.manifest import validate_manifest_version
        from server.reports import generate_outdated_collectors_report, _count_outdated_collectors

        try:
            resolved = validate_manifest_version(version)
        except ValueError as exc:
            _fail(str(exc))
        dicts = [s.to_dict() for s in _offline_states()]
        collectors, components = _count_outdated_collectors(dicts, resolved)
        _emit({"report_markdown": generate_outdated_collectors_report({"agents": dicts}, resolved),
               "collectors_count": collectors, "components_count": components,
               "version": resolved},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.outdated_collectors_report(version=version),
         raw_field="report_markdown" if raw else None)


# ----------------------------------------------------------------- compliance

@compliance_app.command("summary")
def compliance_summary() -> None:
    """Fleet-wide compliance counts."""
    _run(lambda oc: oc.compliance_summary())


@compliance_app.command("policies")
def compliance_policies() -> None:
    """List available OPA policies."""
    _run(lambda oc: oc.list_policies())


@compliance_app.command("validate")
def compliance_validate() -> None:
    """Validate OPA policy files."""
    _run(lambda oc: oc.validate_policies())


@compliance_app.command("reload")
def compliance_reload() -> None:
    """Ask OPA to reload policies (admin)."""
    _run(lambda oc: oc.reload_policies())


@compliance_app.command("check")
def compliance_check(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Force a compliance evaluation for one agent (admin)."""
    _run(lambda oc: oc.check_compliance(agent_id))


# --------------------------------------------------------------------- alerts

def _load_config(spec: str) -> Dict[str, Any]:
    """Parse a JSON config from a literal string or '@path/to/file.json'."""
    if spec.startswith("@"):
        path = Path(spec[1:])
        if not path.exists():
            _fail(f"Config file not found: {path}")
        try:
            return json.loads(path.read_text())
        except ValueError as exc:
            _fail(f"Invalid JSON in {path}: {exc}")
    try:
        return json.loads(spec)
    except ValueError as exc:
        _fail(f"Invalid JSON config: {exc}")


@alerts_app.command("get")
def alerts_get() -> None:
    """Get the current alert configuration (admin)."""
    _run(lambda oc: oc.get_alerts())


@alerts_app.command("set")
def alerts_set(
    config: str = typer.Argument(..., help="Alert config as JSON, or '@file.json'"),
) -> None:
    """Update the alert configuration (admin). Body: same shape GET returns under 'config'."""
    _run(lambda oc: oc.update_alerts(_load_config(config)))


@alerts_app.command("test")
def alerts_test(
    event_type: str = typer.Option("new_agent", "--event-type", help="Event type to test"),
    config: Optional[str] = typer.Option(None, "--config", help="Optional event config JSON (used once, not saved)"),
) -> None:
    """Send a test alert through the configured dispatcher (admin)."""
    event_config = _load_config(config) if config is not None else None
    _run(lambda oc: oc.test_alerts(event_type=event_type, event_config=event_config))


def main() -> None:
    app()


if __name__ == "__main__":
    main()


# -------------------------------------------------------------------- updates

@updates_app.command("status")
def updates_status() -> None:
    """Cached update-check state. Never triggers a network call."""
    _run(lambda oc: oc.get_updates())


@updates_app.command("check")
def updates_check() -> None:
    """Run one update check now (admin). This contacts GitHub once."""
    _run(lambda oc: oc.check_updates())


@updates_app.command("enable")
def updates_enable() -> None:
    """Turn automatic update checks on (admin override)."""
    _run(lambda oc: oc.set_update_check(True))


@updates_app.command("disable")
def updates_disable() -> None:
    """Turn automatic update checks off (admin override)."""
    _run(lambda oc: oc.set_update_check(False))


@updates_app.command("default")
def updates_default() -> None:
    """Clear the admin override and use the deployment default."""
    _run(lambda oc: oc.set_update_check(None))
