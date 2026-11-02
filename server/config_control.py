"""The config control plane (#99): propose → approve → active → rollback.

`server.remote_config` owns the *wire* (#81) and `server.config_validation` owns the
*judgement* (#100). This module owns the **lifecycle** around them, and it exists so
that no route contains a lifecycle rule:

    proposed ──approve──► active ──supersede──► superseded
        │                   │  └──rollback────► rolled_back
        │                   └──FAILED/auto-revert──► poisoned ──reactivate──► active
        └──reject──► rejected

Four rules that the shape of this module is designed to make true:

1. **Immutable.** Nothing in here edits a revision body. Rollback creates a new
   revision that names its parent (`parent_revision_id`), so the chain "what
   replaced what and why" is answerable from the revisions alone.
2. **One active revision per agent.** Approving supersedes the agent's previous
   revision in the same operation; the pointer (`desired_configs`) moves with it and
   the delivery counters reset, so "what should it be running" has one answer.
3. **Selector scope resolves at approve time.** The resolved agent list is stored on
   the revision. A later-joining agent inherits the config *only* when the operator
   ticked `include_future` (`reconcile_future`), because silent inheritance is how a
   fleet-wide config reaches a machine nobody thought about.
4. **Never auto-retry.** Poisoning is a fact about a revision id (D6). Retrying is a
   new revision, or an explicit `reactivate` by an approver — never a timer.

A "proposal" is one `proposal_id` shared by N per-agent revisions. That is what keeps
revisions per-agent (so assignments, delivery counters and rollback stay simple)
while a selector/fleet scope is still one operator action.

Nothing here writes SQL directly: every mutation is a small method on
`SQLiteDesiredConfigStore`, so the lifecycle cannot be bypassed by a route and the
schema stays in one place. Audit rows are written by the *routes* before calling in
(the #97 pre-write contract), because "recorded, then acted" is what makes the log
trustworthy.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from server import alerts, audit, config_validation, posture, remote_config
from server.state import AgentState, SQLiteDesiredConfigStore, utcnow

SCOPE_AGENT = "agent"
SCOPE_AGENTS = "agents"
SCOPE_SELECTOR = "selector"

# How many revisions a proposal view will resolve before it stops reading. A fleet
# proposal is one row per agent; without a cap, listing proposals is a fleet scan.
PROPOSAL_LIST_LIMIT = 500

# Scope keys that are not description attributes when resolving a selector.
_RESERVED_FILTERS = {"healthy", "status", "remote_config_status"}

_DRIFT_ALERTED: set = set()

# Drift is computed per agent on demand (`GET /agent/{id}/config`) and for the whole
# fleet every tick. Both parse two YAML documents, so the result is memoised on the
# inputs that can change it — a per-request parse of every agent is exactly the cost
# #109 removed from the read path.
_DRIFT_CACHE: Dict[str, tuple] = {}


class ConfigControlError(Exception):
    """A refused lifecycle transition, in HTTP terms the route can pass through."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


# ------------------------------------------------------------------------ scopes


def parse_scope(scope: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate a requested scope. A missing scope is never "all agents" (#99)."""
    if not scope:
        raise ConfigControlError(
            400,
            "a scope is required: {\"agent_id\": ...}, {\"agents\": [...]} or "
            "{\"filter\": {...}}. A missing scope is never \"the whole fleet\".",
        )
    include_future = bool(scope.get("include_future"))
    agent_id = scope.get("agent_id")
    agents = scope.get("agents")
    selector = scope.get("filter")

    if agent_id and (agents or selector):
        raise ConfigControlError(400, "provide exactly one of agent_id, agents or filter")
    if agents and selector:
        raise ConfigControlError(400, "provide exactly one of agent_id, agents or filter")

    if agent_id:
        try:
            resolved = [remote_config.normalize_agent_id(str(agent_id))]
        except ValueError as exc:
            raise ConfigControlError(422, str(exc))
        if include_future:
            raise ConfigControlError(
                400,
                "include_future only applies to a selector scope: a fixed agent list has "
                "no future members to attach",
            )
        return {"kind": SCOPE_AGENT, "agent_id": resolved[0], "include_future": False}

    if agents:
        if not isinstance(agents, (list, tuple)):
            raise ConfigControlError(400, "agents must be a list of agent ids")
        resolved = []
        for raw in agents:
            try:
                resolved.append(remote_config.normalize_agent_id(str(raw)))
            except ValueError as exc:
                raise ConfigControlError(422, str(exc))
        if not resolved:
            raise ConfigControlError(400, "agents must not be empty")
        if include_future:
            raise ConfigControlError(
                400,
                "include_future only applies to a selector scope: a fixed agent list has "
                "no future members to attach",
            )
        return {"kind": SCOPE_AGENTS, "agents": resolved, "include_future": False}

    if selector is None:
        raise ConfigControlError(
            400,
            "a scope is required: {\"agent_id\": ...}, {\"agents\": [...]} or "
            "{\"filter\": {...}}. A missing scope is never \"the whole fleet\".",
        )
    if not isinstance(selector, Mapping):
        raise ConfigControlError(
            400,
            "filter must be an object using the GET /agents keys (healthy, "
            "remote_config_status, description attributes)",
        )
    return {
        "kind": SCOPE_SELECTOR,
        "filter": {str(key): value for key, value in selector.items()},
        "include_future": include_future,
    }


def _matches(agent: AgentState, filters: Mapping[str, Any]) -> bool:
    """Resolve a selector with the same logic as `GET /agents` (one implementation)."""
    healthy = filters.get("healthy")
    remote_status = filters.get("remote_config_status") or filters.get("status")
    attributes: Dict[str, List[str]] = {}
    for key, value in filters.items():
        if key in _RESERVED_FILTERS:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        attributes[str(key)] = [str(item) for item in values]
    return agent.matches_filters(
        healthy=None if healthy is None else str(healthy),
        remote_config_status=None if remote_status is None else str(remote_status),
        attributes=attributes,
    )


def resolve_targets(
    scope: Mapping[str, Any],
    agents: Sequence[AgentState],
) -> List[str]:
    """Turn a parsed scope into the agent ids it covers *now*."""
    kind = scope.get("kind")
    if kind == SCOPE_AGENT:
        return [scope["agent_id"]]
    if kind == SCOPE_AGENTS:
        return list(scope["agents"])
    matched = [agent.agent_id for agent in agents if _matches(agent, scope.get("filter") or {})]
    if not matched:
        raise ConfigControlError(
            404,
            "no agents matched the selector; refusing to create a proposal that ships "
            "nothing",
        )
    return matched


def _scope_of(record: Mapping[str, Any]) -> Dict[str, Any]:
    raw = record.get("scope_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _verdict_of(record: Mapping[str, Any]) -> Dict[str, Any]:
    raw = record.get("verdict_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------- verdicts


def _policy_errors(verdict: config_validation.Verdict) -> List[config_validation.Finding]:
    return [finding for finding in verdict.errors if finding.path.startswith("policy")]


def _blocking_errors(verdict: config_validation.Verdict) -> List[config_validation.Finding]:
    """Errors a human cannot wave through: a broken config is not a policy opinion."""
    return [finding for finding in verdict.errors if not finding.path.startswith("policy")]


def validate_for_targets(
    config_map: Dict[str, Dict[str, Any]],
    *,
    agents: Sequence[AgentState],
    allow_endpoint_change: bool = False,
    run_policy: bool = True,
) -> config_validation.Verdict:
    """Run the #100 pipeline with the target agents' inventories in scope."""
    present = [agent for agent in agents if agent is not None]
    return config_validation.check_config_map(
        config_map,
        allow_endpoint_change=allow_endpoint_change,
        agents=present,
        run_policy=run_policy,
    )


def _require_approvable(
    verdict: config_validation.Verdict,
    *,
    override_reason: Optional[str] = None,
) -> None:
    """Refuse an approve/apply whose (re)validation says no (#100).

    Two different refusals, and the difference matters:

    * a **broken config** (parse, structure, protected invariant, size) is a 409
      "re-validate" — the config itself is wrong, and no approver can assert
      otherwise;
    * a **policy violation** is a judgement call, so it is refused unless an
      approver supplies a recorded reason. That is the override, and the route
      audits it.
    """
    blocking = _blocking_errors(verdict)
    if blocking:
        raise ConfigControlError(
            409,
            {
                "message": "re-validate: the config no longer passes validation",
                "errors": [finding.to_dict() for finding in blocking],
                "hint": "fix the config and propose a new revision; an approved revision "
                "is immutable and is never edited in place (#99, D4)",
            },
        )

    policy = _policy_errors(verdict)
    if policy and not (override_reason or "").strip():
        raise ConfigControlError(
            409,
            {
                "message": "re-validate: the policy gate is failing",
                "errors": [finding.to_dict() for finding in policy],
                "hint": "an approver may override with \"override_reason\" (recorded and "
                "audited), or fix the config and propose again",
            },
        )


# ---------------------------------------------------------------------- mutations


def prepare(
    config_map: Dict[str, Dict[str, Any]],
    *,
    scope: Dict[str, Any],
    agents: Sequence[AgentState],
    allow_endpoint_change: bool = False,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the targets and run the pipeline — **read-only**, no revision written.

    Split from :func:`propose` so a route can validate first, write its audit row
    naming the config hash, and only then mutate (#97's pre-write ordering: a
    proposal that cannot be recorded must not happen).
    """
    targets = resolve_targets(scope, agents)
    by_id = {agent.agent_id: agent for agent in agents}
    verdict = validate_for_targets(
        config_map,
        agents=[by_id[agent_id] for agent_id in targets if agent_id in by_id],
        allow_endpoint_change=allow_endpoint_change,
    )

    blocking = _blocking_errors(verdict)
    if blocking:
        raise ConfigControlError(
            422,
            {
                "message": "config validation failed",
                "errors": [finding.to_dict() for finding in blocking],
                "warnings": [finding.to_dict() for finding in verdict.warnings],
            },
        )
    if _policy_errors(verdict) and not (override_reason or "").strip():
        raise ConfigControlError(
            422,
            {
                "message": "the policy gate rejected this config",
                "errors": [finding.to_dict() for finding in _policy_errors(verdict)],
                "hint": "an approver may supply \"override_reason\" (recorded and audited)",
            },
        )

    stored_scope = dict(scope)
    stored_scope["resolved_agents"] = targets
    return {
        "targets": targets,
        "verdict": verdict,
        "config_map": verdict.normalised,
        "config_hash": remote_config.compute_hash(verdict.normalised),
        "scope": stored_scope,
        "scope_kind": scope["kind"],
    }


def propose(
    prepared: Mapping[str, Any],
    *,
    author: str,
    proposal_id: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one `proposed` revision per target agent. Nothing is delivered yet.

    A config that is broken (unparseable, structurally wrong, missing the `opamp`
    extension, oversized) never reaches here — :func:`prepare` refuses it, because
    there is nothing to approve. A config that only fails the **policy gate** is
    stored, so an approver can review it and override with a recorded reason at
    approval time; refusing at propose time would make the override unreachable for
    the operator who is not an approver.
    """
    verdict: config_validation.Verdict = prepared["verdict"]
    scope = prepared["scope"]
    scope_json = json.dumps(scope, sort_keys=True)
    config_map = prepared["config_map"]

    revisions = []
    for agent_id in prepared["targets"]:
        revision_id = remote_config.new_revision_id()
        remote_config.REGISTRY.push(
            agent_id,
            config_map,
            author,
            revision_id=revision_id,
            status=SQLiteDesiredConfigStore.STATUS_PROPOSED,
            proposal_id=proposal_id,
            scope_kind=prepared["scope_kind"],
            scope_json=scope_json,
            note=note,
            verdict=verdict,
            make_current=False,
        )
        revisions.append({"agent_id": agent_id, "revision_id": revision_id})

    return {
        "proposal_id": proposal_id,
        "status": SQLiteDesiredConfigStore.STATUS_PROPOSED,
        "config_hash": prepared["config_hash"],
        "agents": len(prepared["targets"]),
        "scope": {key: value for key, value in scope.items() if key != "resolved_agents"},
        "revisions": revisions,
        "verdict": verdict.to_dict(),
        # #102: not a delivery attempt, so it is a static answer by design. The shape
        # matches `remote_config.DeliveryResult` so a client can read `delivery.state`
        # on every config response without special-casing the propose step.
        "delivery": {
            "state": "unchanged",
            "revision_id": None,
            "config_hash": prepared["config_hash"],
            "channel": None,
            "reason": "not_approved",
            "detail": "nothing is delivered until an approver approves this proposal",
        },
    }


def summary(proposal_id: str) -> Dict[str, Any]:
    """Read-only facts a route needs before auditing a decision (hash, agents, status)."""
    rows = remote_config.REGISTRY.store.by_proposal(proposal_id)
    if not rows:
        raise ConfigControlError(404, f"proposal {proposal_id} not found")
    return {
        "proposal_id": proposal_id,
        "config_hash": rows[0].get("config_hash"),
        "agents": sorted({row["agent_id"] for row in rows}),
        "revisions": [row["revision_id"] for row in rows],
        "statuses": sorted({row.get("status") for row in rows if row.get("status")}),
    }


def _supersede(store: SQLiteDesiredConfigStore, agent_id: str, new_revision_id: str) -> Optional[str]:
    """Move the agent's pointer, marking whatever it pointed at as superseded."""
    current = store.current(agent_id)
    previous_id = None
    if current and current.get("revision_id") != new_revision_id:
        previous_id = current["revision_id"]
        if current.get("status") == SQLiteDesiredConfigStore.STATUS_ACTIVE:
            store.set_status(
                previous_id,
                SQLiteDesiredConfigStore.STATUS_SUPERSEDED,
                superseded_at=utcnow().isoformat(),
            )
    return previous_id


def approve(
    proposal_id: str,
    *,
    approver: str,
    agents: Sequence[AgentState],
    allow_endpoint_change_confirmed: bool = False,
    confirm_scope_change: bool = False,
    override_reason: Optional[str] = None,
    note: Optional[str] = None,
    hold_all: bool = False,
) -> Dict[str, Any]:
    """Approve a proposal: re-resolve, re-validate, supersede, assign.

    Two things happen here that do not happen at propose time, and both are the point
    of a two-phase flow:

    * **The selector is resolved again** (#99 rule 4). An agent that started matching
      the filter between propose and approve is captured; one that stopped matching is
      skipped and reported. The resolved list is stored on the revision, and agents
      that arrive *after* approval inherit nothing unless the operator set
      `include_future`.
    * **The pipeline is re-run** (#100). The schema, the OPA policies and the agents'
      component inventories all move, so approving on Monday's verdict and delivering
      on Friday is how a stale config reaches a fleet. A blocking change refuses with
      `re-validate: ...`; a policy violation needs a recorded approver override.

    Everything is validated *before* the first mutation, so a refused approval leaves
    the store exactly as it was.

    `hold_all` (#101) activates every revision but **holds delivery** for all of them,
    which is what lets a staged rollout own the promotion: the assignment exists (so
    nothing else can deliver it), yet no agent receives the config until its batch is
    released. Holding happens in the same statement as the assignment, so a status
    report arriving mid-approval cannot collect a config the rollout has not cleared.
    """
    store = remote_config.REGISTRY.store
    rows = store.by_proposal(proposal_id)
    if not rows:
        raise ConfigControlError(404, f"proposal {proposal_id} not found")

    statuses = {row.get("status") for row in rows}
    if statuses == {SQLiteDesiredConfigStore.STATUS_REJECTED}:
        raise ConfigControlError(409, "this proposal was rejected; propose a new revision")
    pending = [
        row for row in rows if row.get("status") == SQLiteDesiredConfigStore.STATUS_PROPOSED
    ]
    if not pending:
        raise ConfigControlError(
            409,
            {
                "message": "this proposal has already been decided",
                "statuses": sorted(status for status in statuses if status),
                "hint": "use POST /config/revisions/{id}/reactivate for a poisoned "
                "revision, or propose a new one",
            },
        )

    scope = _scope_of(pending[0])
    by_id = {agent.agent_id: agent for agent in agents}
    source_map = remote_config.REGISTRY.decode_revision(pending[0]["revision_id"])
    if source_map is None:
        raise ConfigControlError(
            409,
            {
                "message": f"revision {pending[0]['revision_id']} has no readable body",
                "hint": "retention may have pruned it, or CONFIG_AT_REST_KEY changed; "
                "propose a new revision",
            },
        )

    matched: Optional[set] = None
    if scope.get("kind") == SCOPE_SELECTOR and scope.get("filter"):
        matched = {
            agent.agent_id for agent in agents if _matches(agent, scope["filter"])
        }
    target_ids = sorted(matched) if matched is not None else [row["agent_id"] for row in pending]
    if not target_ids:
        raise ConfigControlError(
            409,
            {
                "message": "no agent matches this proposal's selector any more",
                "selector": scope.get("filter"),
                "hint": "propose a new revision against the current fleet",
            },
        )
    # Agents that no longer match stay `proposed`: nothing is delivered to them and a
    # later reject records the decision. Silently activating them would be the
    # "config reaches a machine nobody thought about" failure rule 4 exists to stop.
    dropped = [row["agent_id"] for row in pending if row["agent_id"] not in target_ids]
    proposed_ids = {row["agent_id"] for row in rows}
    newcomers = [agent_id for agent_id in target_ids if agent_id not in proposed_ids]
    if newcomers and not confirm_scope_change:
        # Re-resolving at approve time is the rule, but an approval that quietly
        # covers *more* machines than the approver saw is the same failure by a
        # different route. Make the growth explicit.
        raise ConfigControlError(
            409,
            {
                "message": "the selector now matches more agents than when this was "
                "proposed",
                "new_agents": newcomers,
                "proposed_agents": sorted(proposed_ids),
                "hint": "re-propose against the current fleet, or repeat the approval "
                "with \"confirm_scope_change\": true",
            },
        )

    verdicts: Dict[str, config_validation.Verdict] = {}
    for agent_id in target_ids:
        target = by_id.get(agent_id)
        verdict = validate_for_targets(
            source_map,
            agents=[target] if target is not None else [],
            allow_endpoint_change=allow_endpoint_change_confirmed,
        )
        _require_approvable(verdict, override_reason=override_reason)
        verdicts[agent_id] = verdict

    now = utcnow().isoformat()
    scope_json = json.dumps({**scope, "resolved_agents": target_ids}, sort_keys=True)
    revisions = {row["agent_id"]: row["revision_id"] for row in pending}
    approved = []
    for agent_id in target_ids:
        revision_id = revisions.get(agent_id)
        if revision_id is None:
            # Matched at approve time: a revision for it never existed.
            revision_id = remote_config.new_revision_id()
            remote_config.REGISTRY.push(
                agent_id,
                source_map,
                pending[0].get("author") or approver,
                revision_id=revision_id,
                status=SQLiteDesiredConfigStore.STATUS_PROPOSED,
                proposal_id=proposal_id,
                scope_kind=SCOPE_SELECTOR,
                scope_json=scope_json,
                note="matched the selector at approval time",
                verdict=verdicts[agent_id],
                make_current=False,
            )
        _supersede(store, agent_id, revision_id)
        store.set_status(
            revision_id,
            SQLiteDesiredConfigStore.STATUS_ACTIVE,
            approved_by=approver,
            approved_at=now,
            note=note,
            verdict_json=json.dumps(verdicts[agent_id].to_dict(), sort_keys=True),
            scope_json=scope_json,
        )
        remote_config.REGISTRY.activate(agent_id, revision_id, held=hold_all)
        approved.append({"agent_id": agent_id, "revision_id": revision_id})

    return {
        "proposal_id": proposal_id,
        "status": SQLiteDesiredConfigStore.STATUS_ACTIVE,
        "approved_by": approver,
        "approved_at": now,
        "revisions": approved,
        "agents": len(approved),
        "dropped": dropped,
        "added": newcomers,
        "held": bool(hold_all),
        "verdict": next(iter(verdicts.values())).to_dict() if verdicts else {},
        "override_reason": (override_reason or "").strip() or None,
        # `delivery` is deliberately absent: the route attaches the result of actually
        # trying to deliver (#102). A lifecycle module must not claim to have handed
        # anything to an agent it cannot see.
    }


def reject(
    proposal_id: str,
    *,
    approver: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Reject a proposal. The revision is kept — a rejected config is evidence."""
    store = remote_config.REGISTRY.store
    rows = store.by_proposal(proposal_id)
    if not rows:
        raise ConfigControlError(404, f"proposal {proposal_id} not found")
    pending = [
        row for row in rows if row.get("status") == SQLiteDesiredConfigStore.STATUS_PROPOSED
    ]
    if not pending:
        raise ConfigControlError(
            409,
            {
                "message": "there is nothing left to reject in this proposal",
                "statuses": sorted({row.get("status") for row in rows if row.get("status")}),
                "hint": "an active revision is withdrawn (DELETE /agent/{id}/config) or "
                "rolled back, not rejected",
            },
        )
    # A proposal can be *partly* decided — `approve` leaves revisions whose agent
    # stopped matching the selector as `proposed` — so rejecting the leftovers must
    # stay possible. Rejecting an already-active revision stays impossible.
    decided = [row for row in rows if row not in pending]
    now = utcnow().isoformat()
    for row in pending:
        store.set_status(
            row["revision_id"],
            SQLiteDesiredConfigStore.STATUS_REJECTED,
            approved_by=approver,
            note=reason or row.get("note"),
        )
    return {
        "proposal_id": proposal_id,
        "status": SQLiteDesiredConfigStore.STATUS_REJECTED,
        "rejected_by": approver,
        "rejected_at": now,
        "revisions": [row["revision_id"] for row in pending],
        "skipped": [row["revision_id"] for row in decided],
        "reason": reason,
    }


def reactivate(
    proposal_id: str,
    *,
    approver: str,
    agents: Sequence[AgentState],
    reason: Optional[str] = None,
    allow_endpoint_change_confirmed: bool = False,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-approve poisoned revisions after a fresh validation pass (#99 rule 5).

    Explicit, approver-only and audited: a poisoned revision never comes back on its
    own, and reactivation re-runs the pipeline because "the config did not work on
    Monday" may have been a policy or inventory problem that has since been fixed.
    """
    store = remote_config.REGISTRY.store
    rows = store.by_proposal(proposal_id)
    if not rows:
        raise ConfigControlError(404, f"proposal {proposal_id} not found")
    poisoned = [
        row
        for row in rows
        if row.get("status") == SQLiteDesiredConfigStore.STATUS_POISONED
        or row.get("failed_at")
    ]
    if not poisoned:
        raise ConfigControlError(
            409,
            {
                "message": "nothing in this proposal is poisoned",
                "statuses": sorted({row.get("status") for row in rows}),
            },
        )

    by_id = {agent.agent_id: agent for agent in agents}
    revalidated: Dict[str, config_validation.Verdict] = {}
    for row in poisoned:
        config_map = remote_config.REGISTRY.decode_revision(row["revision_id"])
        if config_map is None:
            raise ConfigControlError(
                409,
                {
                    "message": f"revision {row['revision_id']} has no readable body",
                    "hint": "propose a new revision instead",
                },
            )
        target = by_id.get(row["agent_id"])
        verdict = validate_for_targets(
            config_map,
            agents=[target] if target is not None else [],
            allow_endpoint_change=allow_endpoint_change_confirmed,
        )
        _require_approvable(verdict, override_reason=override_reason)
        revalidated[row["revision_id"]] = verdict

    now = utcnow().isoformat()
    reactivated = []
    for row in poisoned:
        store.clear_poison(row["revision_id"])
        store.set_status(
            row["revision_id"],
            SQLiteDesiredConfigStore.STATUS_ACTIVE,
            approved_by=approver,
            approved_at=now,
            note=reason or "reactivated after an explicit approver decision",
            failed_at=None,
            failure_detail=None,
            verdict_json=json.dumps(revalidated[row["revision_id"]].to_dict(), sort_keys=True),
        )
        _supersede(store, row["agent_id"], row["revision_id"])
        remote_config.REGISTRY.activate(row["agent_id"], row["revision_id"])
        reactivated.append(
            {"agent_id": row["agent_id"], "revision_id": row["revision_id"]}
        )

    return {
        "proposal_id": proposal_id,
        "status": SQLiteDesiredConfigStore.STATUS_ACTIVE,
        "reactivated_by": approver,
        "reactivated_at": now,
        "revisions": reactivated,
        "reason": reason,
    }


def rollback(
    agent_id: str,
    *,
    author: str,
    agents: Sequence[AgentState] = (),
    note: Optional[str] = None,
    revision_id: Optional[str] = None,
    held: bool = False,
) -> Dict[str, Any]:
    """Roll an agent back to its previous revision, as a **new** revision (D4).

    The new revision names its parent, the revision it replaces is marked
    `rolled_back` (not merely superseded), and the assignment resets so the config is
    delivered once. Rollback is the one-click answer to a poisoned revision, which is
    why it does not require the old revision to be active again.
    """
    store = remote_config.REGISTRY.store
    current = store.current(agent_id)
    previous = store.previous(agent_id)
    if previous is None:
        raise ConfigControlError(
            409,
            {
                "message": f"agent {agent_id} has no previous revision to roll back to",
                "hint": "push a new config (PUT /agent/{id}/config) or approve a proposal",
            },
        )

    config_map = remote_config.REGISTRY.decode_revision(previous["revision_id"])
    if config_map is None:
        raise ConfigControlError(
            409,
            {
                "message": f"previous revision {previous['revision_id']} has no readable body",
                "hint": "retention may have pruned it; push the config again",
            },
        )

    # Re-validate before delivering: the old config has to still be legal today,
    # because the protected invariants and the agent's inventory may have moved.
    target = next((agent for agent in agents if agent.agent_id == agent_id), None)
    verdict = validate_for_targets(config_map, agents=[target] if target else [])
    blocking = _blocking_errors(verdict)
    if blocking:
        raise ConfigControlError(
            409,
            {
                "message": "re-validate: the previous revision no longer passes validation",
                "errors": [finding.to_dict() for finding in blocking],
            },
        )

    revision_id = revision_id or remote_config.new_revision_id()
    record = remote_config.REGISTRY.push(
        agent_id,
        config_map,
        author,
        revision_id=revision_id,
        note=note or f"rollback to {previous['revision_id']}",
        parent_revision_id=previous["revision_id"],
        verdict=verdict,
        held=held,
    )
    if current and current.get("revision_id") != revision_id:
        store.set_status(
            current["revision_id"],
            SQLiteDesiredConfigStore.STATUS_ROLLED_BACK,
            superseded_at=utcnow().isoformat(),
        )
    return {
        "agent_id": agent_id,
        "revision_id": record["revision_id"],
        "parent_revision_id": previous["revision_id"],
        "config_hash": record["config_hash"],
        "withdrawn": bool(record.get("withdrawn")),
        "verdict": verdict.to_dict(),
    }


# ------------------------------------------------------------------ future scope


def reconcile_future(agents: Sequence[AgentState]) -> int:
    """Attach an `include_future` proposal to agents that matched *after* approval.

    Only proposals whose operator explicitly ticked "apply to future matches" are
    honoured, and only from the periodic tick (never the message path): silent
    inheritance is how a fleet-wide config reaches a machine nobody thought about.
    Returns how many agents were attached.
    """
    if not posture.push_allowed():
        return 0
    store = remote_config.REGISTRY.store
    try:
        active = store.list_revisions(
            status=SQLiteDesiredConfigStore.STATUS_ACTIVE, limit=PROPOSAL_LIST_LIMIT
        )
    except Exception as exc:  # pragma: no cover - defensive, sweep must not kill the tick
        logger.warning(f"include_future sweep could not list revisions: {exc}")
        return 0

    proposals: Dict[str, List[Dict[str, Any]]] = {}
    for row in active:
        scope = _scope_of(row)
        if not scope.get("include_future") or not scope.get("filter"):
            continue
        if not row.get("proposal_id"):
            continue
        proposals.setdefault(row["proposal_id"], []).append(row)

    attached = 0
    for proposal_id, rows in proposals.items():
        source = rows[0]
        scope = _scope_of(source)
        # "Already attached" is read from the proposal's own revisions rather than
        # from `scope_json.resolved_agents`: the revision that recorded the first
        # resolution is immutable (D4), so the rows are the truth and a second sweep
        # cannot re-attach the same agent.
        resolved = {row["agent_id"] for row in store.by_proposal(proposal_id)}
        matched = [agent.agent_id for agent in agents if _matches(agent, scope["filter"])]
        newcomers = [agent_id for agent_id in matched if agent_id not in resolved]
        if not newcomers:
            continue
        config_map = remote_config.REGISTRY.decode_revision(source["revision_id"])
        if config_map is None:
            continue
        for agent_id in newcomers:
            revision_id = remote_config.new_revision_id()
            try:
                remote_config.REGISTRY.push(
                    agent_id,
                    config_map,
                    source.get("author") or "system",
                    revision_id=revision_id,
                    proposal_id=proposal_id,
                    scope_kind=SCOPE_SELECTOR,
                    scope_json=json.dumps(scope, sort_keys=True),
                    note=f"apply_to_future: matched proposal {proposal_id}",
                    verdict=None,
                )
            except Exception as exc:  # pragma: no cover - one agent must not stop the sweep
                logger.warning(f"include_future: could not attach {agent_id}: {exc}")
                continue
            attached += 1
            logger.info(
                f"include_future: proposal {proposal_id} attached to new match {agent_id}"
            )
            audit.audit_event(
                "config.apply.future",
                principal="system",
                actor_kind="system",
                target_kind="agent",
                target_id=agent_id,
                revision_id=revision_id,
                config_hash=source.get("config_hash"),
                proposal_id=proposal_id,
            )
    return attached


# ------------------------------------------------------------------------ drift


def effective_bodies(effective_config_json: Optional[str]) -> List[str]:
    """Decode the config bodies out of an agent's reported `effectiveConfig`.

    `MessageToDict` renders a bytes field as base64, so the bodies have to be decoded
    before they can be compared with what we pushed. Anything unexpected yields an
    empty list (unknown drift) rather than an exception: a malformed report must not
    break `GET /agent/{id}/config`.
    """
    if not effective_config_json:
        return []
    try:
        document = json.loads(effective_config_json)
    except (TypeError, ValueError):
        return []
    config_map = ((document or {}).get("configMap") or {}).get("configMap") or {}
    if not isinstance(config_map, dict):
        return []
    bodies: List[str] = []
    for entry in config_map.values():
        body = (entry or {}).get("body") if isinstance(entry, dict) else None
        if isinstance(body, (bytes, bytearray)):
            bodies.append(bytes(body).decode("utf-8", "replace"))
        elif isinstance(body, str):
            try:
                bodies.append(base64.b64decode(body, validate=True).decode("utf-8"))
            except Exception:
                bodies.append(body)
    return bodies


def drift_for(
    record: Optional[Mapping[str, Any]],
    state: Optional[AgentState],
) -> Dict[str, Any]:
    """Is the agent running the config we asked for? (#99 work item 3)

    Deliberately **not** a hash comparison of two documents that are legitimately
    different: the supervisor composes and re-serialises files, so a byte compare
    would report drift on every healthy agent. Instead the desired document must be
    contained in what the agent reports (subset comparison), which tolerates the
    merge and still catches "the exporter we pushed is not there".
    """
    if record is None:
        return {"drift": None, "reason": "no desired config", "missing": []}
    if state is None:
        return {"drift": None, "reason": "agent is not connected", "missing": []}
    if record.get("status") != SQLiteDesiredConfigStore.STATUS_ACTIVE:
        return {
            "drift": None,
            "reason": f"revision is {record.get('status')}",
            "missing": [],
        }
    if record.get("withdrawn"):
        return {"drift": False, "reason": "config withdrawn", "missing": []}

    ours = record.get("config_hash")
    if state.last_remote_config_hash == ours:
        return {"drift": False, "reason": "agent reports the desired hash", "missing": []}

    # Cache key: everything the answer depends on. `hash()` of the raw effective
    # config is O(n) but does not parse YAML, so a fleet sweep stays cheap.
    key = (
        record.get("revision_id"),
        ours,
        state.last_remote_config_hash,
        hash(state.effective_config),
    )
    cached = _DRIFT_CACHE.get(state.agent_id)
    if cached is not None and cached[0] == key:
        return cached[1]

    bodies = effective_bodies(state.effective_config)
    if not bodies:
        result = {
            "drift": None,
            "reason": "agent has not reported an effective config yet",
            "missing": [],
        }
    else:
        desired = [
            (key_, (entry or {}).get("body") or "")
            for key_, entry in (record.get("config_map") or {}).items()
        ]
        missing = config_validation.document_drift(desired, bodies)
        result = {
            "drift": bool(missing),
            "reason": "desired config is not in the reported effective config"
            if missing
            else "reported effective config contains the desired config",
            "missing": missing,
            "desired_hash": ours,
            "reported_hash": state.last_remote_config_hash,
        }
    _DRIFT_CACHE[state.agent_id] = (key, result)
    return result


def sweep_drift(agents: Sequence[AgentState]) -> List[Dict[str, Any]]:
    """Recompute drift for every assignment and fire one alert per (agent, revision).

    Runs from the periodic tick, not the message path: an alert is a network call,
    and one alert per poll is the flood #99 explicitly rules out. The key includes the
    revision id, so a *new* revision drifting is a new alert while the same drift
    stays quiet until it clears.
    """
    by_id = {agent.agent_id: agent for agent in agents}
    assignments = remote_config.REGISTRY.all()
    # Drop cache entries for agents whose assignment is gone, so the memo cannot grow
    # with the fleet's history.
    for stale in set(_DRIFT_CACHE) - set(assignments):
        _DRIFT_CACHE.pop(stale, None)
    drifting: List[Dict[str, Any]] = []
    for agent_id, record in assignments.items():
        state = by_id.get(agent_id)
        result = drift_for(record, state)
        if result.get("drift") is not True:
            _DRIFT_ALERTED.discard((agent_id, record.get("revision_id")))
            continue
        drifting.append({"agent_id": agent_id, **result})
        key = (agent_id, record.get("revision_id"))
        if key in _DRIFT_ALERTED:
            continue
        _DRIFT_ALERTED.add(key)
        logger.warning(
            f"agent {agent_id}: config drift — desired revision "
            f"{record.get('revision_id')} is not in the reported effective config "
            f"({len(result.get('missing') or [])} missing paths)"
        )
        audit.audit_event(
            "config.drift",
            principal="system",
            actor_kind="system",
            result="error",
            target_kind="agent",
            target_id=agent_id,
            revision_id=record.get("revision_id"),
            config_hash=record.get("config_hash"),
            missing=len(result.get("missing") or []),
        )
        alerts.send_config_drift_alert(
            agent_id,
            record.get("revision_id") or "",
            (result.get("missing") or [])[:5],
        )
    return drifting


def reset_drift_alerts() -> None:
    """Test helper: forget which drifts have already been alerted."""
    _DRIFT_ALERTED.clear()
    _DRIFT_CACHE.clear()


# ----------------------------------------------------------------------- views


def revision_view(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Revision metadata for an admin listing. **Never** the config body (D8)."""
    verdict = _verdict_of(record)
    return {
        "revision_id": record.get("revision_id"),
        "agent_id": record.get("agent_id"),
        "proposal_id": record.get("proposal_id"),
        "status": record.get("status"),
        "config_hash": record.get("config_hash"),
        "author": record.get("author"),
        "created_at": record.get("created_at"),
        "approved_by": record.get("approved_by"),
        "approved_at": record.get("approved_at"),
        "note": record.get("note"),
        "parent_revision_id": record.get("parent_revision_id"),
        "withdrawn": bool(record.get("withdrawn")),
        "failed_at": record.get("failed_at"),
        "failure_detail": record.get("failure_detail"),
        "superseded_at": record.get("superseded_at"),
        "body_pruned_at": record.get("body_pruned_at"),
        "scope_kind": record.get("scope_kind"),
        "scope": _scope_of(record),
        "verdict": verdict or None,
    }


def proposal_view(proposal_id: str) -> Dict[str, Any]:
    """One proposal: its revisions (metadata only), rollup status and delivery facts."""
    store = remote_config.REGISTRY.store
    rows = store.by_proposal(proposal_id)
    if not rows:
        raise ConfigControlError(404, f"proposal {proposal_id} not found")
    revisions = []
    for row in rows:
        view = revision_view(row)
        # Authoritative read, not the in-memory cache: a listing must not show this
        # worker's stale view of another worker's delivery (#102).
        assignment = store.current(row["agent_id"])
        current = bool(assignment and assignment.get("revision_id") == row["revision_id"])
        # #102: the same derived state the agent view uses, so "delivered" never
        # means two different things in two listings. It describes the *current*
        # assignment, so it is null on a revision that is no longer current.
        delivery = remote_config.delivery_view(
            assignment, remote_config.reported_hash(row["agent_id"])
        )
        delivery["current"] = current
        if not current:
            delivery["state"] = None
        view["delivery"] = delivery
        revisions.append(view)
    statuses = {row.get("status") for row in rows}
    return {
        "proposal_id": proposal_id,
        "status": next(iter(statuses)) if len(statuses) == 1 else "mixed",
        "statuses": sorted(status for status in statuses if status),
        "revisions": revisions,
        "agents": len({row["agent_id"] for row in rows}),
        "config_hash": rows[0].get("config_hash"),
        "scope": _scope_of(rows[0]),
        "note": rows[0].get("note"),
        "created_at": rows[0].get("created_at"),
    }


def list_proposals(
    *,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Revision history, newest first, flattened per revision (bodies excluded)."""
    if status and status not in SQLiteDesiredConfigStore.STATUSES:
        raise ConfigControlError(
            422,
            f"status must be one of {', '.join(SQLiteDesiredConfigStore.STATUSES)}",
        )
    rows = remote_config.REGISTRY.store.list_revisions(
        agent_id=agent_id, status=status, limit=limit
    )
    return {
        "revisions": [revision_view(row) for row in rows],
        "count": len(rows),
        "statuses": list(SQLiteDesiredConfigStore.STATUSES),
    }
