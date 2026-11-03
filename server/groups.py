"""Named collector groups (#111): a saved selector, resolved at use time.

A group is **not** a member list. It is a name for a filter document
(`{"environment": "prod", "region": "emea"}`) plus two deliberate exceptions
(`include` forces an agent in, `exclude` forces one out). Membership is answered by
`AgentState.matches_scope()` — the same implementation `GET /agents` and a config
`filter` scope use — so a group can never mean something different from the filter it
replaces.

Why that matters, in one sentence: the failure this feature exists to prevent is a
push that silently covers fewer (or more) collectors than the operator believed, so
the meaning of a name must be *derivable and checkable*, never a snapshot somebody
has to remember updating.

Three rules are enforced here rather than in the routes:

1. **The definition at approval time is recorded on the revision**
   (`group_snapshot_json`). Editing a group changes future pushes only; a past
   revision still says what "prod-emea" meant when it was approved.
2. **A group defined on health is refused by default.** `healthy` and
   `remote_config_status` are legal filter keys but poisonous in a group: the moment
   an agent breaks, it leaves the group, so the push that would fix it skips exactly
   the agents that need it. `use_stored_resolution` is the escape hatch: send to the
   membership as resolved at a stated time instead of matching now.
3. **Silent shrinkage is refused.** Resolving smaller than the group's last recorded
   resolution returns the `added`/`dropped` diff and needs `confirm_scope_change`,
   because "the attribute was renamed" looks exactly like "the fleet is fine" from a
   count alone.

Nothing here writes SQL: all state goes through `SQLiteGroupStore`. Nothing here
imports FastAPI or `config_control` (which imports this module), so the module stays
unit-testable and cannot grow a route-shaped shortcut.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from server.state import (
    COLLECTOR_GROUPS,
    RESERVED_FILTER_KEYS,
    AgentState,
    GroupError,
    SQLiteGroupStore,
)

# Reasons a membership can be evaluated. Kept next to the store's vocabulary.
REASON_PREVIEW = "preview"
REASON_PROPOSE = "propose"
REASON_PUSH = "push"
REASON_APPROVE = "approve"

# The reserved keys that make a definition *dynamic in a harmful way*: matching on
# liveness means a broken agent drops out of the group that should be fixing it.
HEALTH_KEYS = frozenset({"healthy", "status", "remote_config_status"})

# A membership swing larger than this with no push behind it is the signal for
# "something changed the fleet's attributes, and nobody decided that" (#111 work item
# 7). Read per call, not at import, so tests and operators can move it without a
# restart being the only way.
DEFAULT_MEMBERSHIP_SWING = 5


def membership_swing_threshold() -> int:
    raw = os.environ.get("OPAMP_GROUP_MEMBERSHIP_SWING", "")
    try:
        return max(1, int(raw)) if str(raw).strip() else DEFAULT_MEMBERSHIP_SWING
    except ValueError:
        return DEFAULT_MEMBERSHIP_SWING


def _warn_on_swing(record: Mapping[str, Any], added: Sequence[str], dropped: Sequence[str]) -> None:
    threshold = membership_swing_threshold()
    swing = max(len(added), len(dropped))
    if swing < threshold:
        return
    # One structured line, not an alert: an alert on silent enrolment belongs with the
    # `include_future` work (an explicit non-goal of #111), but *seeing* a swing is
    # cheap and is exactly what makes a renamed attribute findable.
    logger.bind(
        event="opamp.group.membership_swing",
        group_id=record.get("group_id"),
        group=record.get("name"),
        added=len(added),
        dropped=len(dropped),
        threshold=threshold,
    ).warning(
        f"group {record.get('name')!r}: membership moved by {swing} agents "
        f"({len(added)} added, {len(dropped)} dropped) — check that the definition still "
        "means what it did (an attribute rename looks like this)"
    )


def health_defined(definition: Mapping[str, Any]) -> List[str]:
    """Keys that make this definition health-based (empty list when it is not)."""
    return sorted(key for key in (definition or {}) if key in HEALTH_KEYS)


def definition_of(record: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(record.get("definition") or {})


def snapshot(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The immutable copy of a group that a revision records (#111 invariant 1).

    Deliberately a *copy*, not the group id: `config show prop-…` has to be able to
    say "pushed to prod-emea as defined on 2026-03-04; the definition has since
    changed" and show the difference. A bare `group_id` would retroactively rewrite
    the meaning of every past revision that named it.
    """
    return {
        "group_id": record.get("group_id"),
        "name": record.get("name"),
        "definition": definition_of(record),
        "include": list(record.get("include") or []),
        "exclude": list(record.get("exclude") or []),
        "description": record.get("description"),
        "owner": record.get("owner"),
        "updated_at": record.get("updated_at"),
        "archived_at": record.get("archived_at"),
    }


def _dedupe(values: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(str(value) for value in values))


def resolve_members(
    record: Mapping[str, Any],
    agents: Sequence[AgentState],
    *,
    stored_agent_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Resolve a group against the fleet *now*, reporting where each member came from.

    `stored_agent_ids` switches to the stored-resolution mode: the membership is the
    recorded list, and `include`/`exclude` still apply because a pin is a human
    decision that does not expire with a snapshot.

    `sources` is not decoration: the Remote Config page must be able to show "9 from
    the definition, 1 pinned in, 1 pinned out", which is the difference between an
    operator trusting the blast radius and re-deriving it by hand.
    """
    definition = definition_of(record)
    include = [str(agent_id) for agent_id in (record.get("include") or [])]
    exclude = [str(agent_id) for agent_id in (record.get("exclude") or [])]
    by_id = {agent.agent_id: agent for agent in agents}

    if stored_agent_ids is None:
        matched = [agent.agent_id for agent in agents if agent.matches_scope(definition)]
        matched.sort()
        base = "definition"
    else:
        matched = [str(agent_id) for agent_id in stored_agent_ids]
        base = "stored"

    excluded = [agent_id for agent_id in matched if agent_id in exclude]
    members = [agent_id for agent_id in matched if agent_id not in exclude]
    forced = [agent_id for agent_id in include if agent_id not in members]
    members = _dedupe(members + forced)
    # A pin for an agent we have never seen is not an error (desired state is stored,
    # not pushed), but it must be visible: a typo'd agent id would otherwise look
    # like a working pin forever.
    unknown_pins = [agent_id for agent_id in include if agent_id not in by_id]

    sources = {
        "definition": [
            agent_id for agent_id in members if agent_id in matched and agent_id not in include
        ],
        "included": [agent_id for agent_id in members if agent_id in include],
        "excluded": excluded,
    }
    return {
        "members": members,
        "count": len(members),
        "base": base,
        "sources": sources,
        "unknown_pins": unknown_pins,
        "definition": definition,
    }


def preview(
    record: Mapping[str, Any],
    agents: Sequence[AgentState],
    *,
    store: SQLiteGroupStore = COLLECTOR_GROUPS,
    actor: Optional[str] = None,
    record_resolution: bool = True,
) -> Dict[str, Any]:
    """What would this group hit now, versus the last recorded resolution?

    Read-only apart from the resolution log (which is the baseline the *next* preview
    and the shrink guard compare against; without recording, `dropped` would be
    permanently empty and the drift view would have nothing to drift from).
    """
    last = store.last_resolution(record["group_id"])
    resolved = resolve_members(record, agents)
    previous = set(last["agent_ids"]) if last else set()
    current = set(resolved["members"])
    diff = {
        "added": sorted(current - previous),
        "dropped": sorted(previous - current),
        "unchanged": sorted(current & previous),
    }
    _warn_on_swing(record, diff["added"], diff["dropped"])
    result = {
        "group_id": record.get("group_id"),
        "name": record.get("name"),
        "archived": bool(record.get("archived_at")),
        "definition": resolved["definition"],
        "include": list(record.get("include") or []),
        "exclude": list(record.get("exclude") or []),
        "members": resolved["members"],
        "count": resolved["count"],
        "sources": resolved["sources"],
        "unknown_pins": resolved["unknown_pins"],
        "previous_count": len(previous),
        "last_resolution": last,
        **diff,
    }
    if record_resolution:
        result["recorded"] = store.record_resolution(
            record["group_id"],
            resolved["members"],
            reason=REASON_PREVIEW,
            actor=actor,
        )
        # The baseline we compared against, not the row we just wrote.
        result["compared_to"] = last
    return result


def expand_scope(
    scope: Mapping[str, Any],
    *,
    agents: Sequence[AgentState],
    store: SQLiteGroupStore = COLLECTOR_GROUPS,
    actor: Optional[str] = None,
    use_stored_resolution: bool = False,
    confirm_scope_change: bool = False,
) -> Dict[str, Any]:
    """Turn `{"group": "prod-emea"}` into a parsed selector scope + snapshot.

    Called from `config_control.parse_scope`, which is the one gate every config
    entry point goes through (propose, approve, apply, rollout), so a new caller
    cannot bypass the health guard or the shrink guard by forgetting to ask.

    The returned scope keeps `kind: selector` so every downstream step — approve-time
    re-resolution, `include_future`, the rollout engine, `confirm_scope_change` —
    keeps working unchanged. The group is a *name for a selector*, not a new scope
    kind (#111).
    """
    raw = scope.get("group") or scope.get("group_id") or scope.get("group_name")
    if not raw:
        raise GroupError(400, "group scope needs a group name or id")
    record = store.find(str(raw))
    if record is None:
        raise GroupError(
            404,
            {
                "message": f"group {raw!r} not found",
                "hint": "list the saved selectors with GET /groups",
            },
        )
    if record.get("archived_at"):
        raise GroupError(
            409,
            {
                "message": f"group {record['name']!r} is archived",
                "archived_at": record.get("archived_at"),
                "hint": "restore it (POST /groups/{id}/restore) or use an attribute filter",
            },
        )

    definition = definition_of(record)
    dynamic = health_defined(definition)
    last = store.last_resolution(record["group_id"])
    if dynamic and not use_stored_resolution:
        raise GroupError(
            400,
            {
                "message": (
                    f"group {record['name']!r} is defined on "
                    f"{', '.join(dynamic)}, so its membership changes exactly when "
                    "agents break"
                ),
                "definition": definition,
                "hint": (
                    "a health-based group drops the agents that need the fix; repeat "
                    "with \"use_stored_resolution\": true to push to the membership "
                    "resolved at a stated time" + (
                        f" ({last['agent_count']} agents at {last['evaluated_at']})"
                        if last
                        else " (none recorded yet — preview the group first)"
                    )
                ),
            },
        )

    stored_ids: Optional[Sequence[str]] = None
    if use_stored_resolution:
        if not last:
            raise GroupError(
                409,
                {
                    "message": f"group {record['name']!r} has no recorded resolution",
                    "hint": "POST /groups/{id}/preview first: there is no stated "
                    "membership to push to yet",
                },
            )
        stored_ids = last["agent_ids"]

    resolved = resolve_members(record, agents, stored_agent_ids=stored_ids)
    members = resolved["members"]
    if not members:
        raise GroupError(
            404,
            {
                "message": f"group {record['name']!r} has no members right now",
                "count": 0,
                "hint": "an empty group is a valid thing to save and preview, but a "
                "push that ships nothing is a mistake; check the definition against "
                "GET /groups/attribute-keys",
            },
        )

    previous = set(last["agent_ids"]) if last else set()
    dropped = sorted(previous - set(members))
    added = sorted(set(members) - previous)
    _warn_on_swing(record, added, dropped)
    if dropped and not confirm_scope_change:
        raise GroupError(
            409,
            {
                "message": (
                    f"group {record['name']!r} now resolves to {len(members)} agents, "
                    f"down from {len(previous)}"
                ),
                "added": added,
                "dropped": dropped,
                "unchanged": sorted(previous & set(members)),
                "hint": "silent shrinkage is the failure this check exists for (an "
                "attribute was renamed, a region retired); repeat with "
                "\"confirm_scope_change\": true once the dropped list is expected",
            },
        )

    group_snapshot = snapshot(record)
    return {
        "kind": "selector",
        "filter": dict(definition),
        "include_future": bool(scope.get("include_future")),
        "group_id": record["group_id"],
        "group_name": record["name"],
        "group_snapshot": group_snapshot,
        "group_definition": definition,
        "include": list(record.get("include") or []),
        "exclude": list(record.get("exclude") or []),
        # Named `group_resolved_agents` (not `resolved_agents`): the latter is
        # written by `config_control.prepare()` with the final target list, and two
        # meanings under one key is how "why did this push skip my agent?" becomes
        # unanswerable. This one is the group's own evaluation at parse time.
        "group_resolved_agents": members,
        "resolution_base": resolved["base"],
        "resolution_sources": resolved["sources"],
        "resolution_compared_to": last,
        "resolution_added": added,
        "resolution_dropped": dropped,
    }


def resolve_group_scope(
    scope: Mapping[str, Any],
    agents: Sequence[AgentState],
) -> List[str]:
    """Resolve an already-expanded group scope to agent ids.

    Instead of re-matching the definition, this honours the *expanded* scope: a
    stored-resolution push carries the ids it was approved against, so a later match
    cannot quietly add agents to it (invariant 2: a rollout never re-resolves).
    """
    include = [str(agent_id) for agent_id in (scope.get("include") or [])]
    exclude = [str(agent_id) for agent_id in (scope.get("exclude") or [])]
    if scope.get("resolution_base") == "stored":
        base = scope.get("group_resolved_agents") or []
    else:
        base = None
    if base is None:
        base = [
            agent.agent_id
            for agent in agents
            if agent.matches_scope(scope.get("filter") or {})
        ]
    members = [str(agent_id) for agent_id in base if agent_id not in exclude]
    members = _dedupe(members + [agent_id for agent_id in include if agent_id not in members])
    return sorted(members)


def record_push_resolution(
    scope: Mapping[str, Any],
    agent_ids: Sequence[str],
    *,
    actor: Optional[str],
    store: SQLiteGroupStore = COLLECTOR_GROUPS,
    reason: str = REASON_PUSH,
) -> Optional[Dict[str, Any]]:
    """Record what a group-scoped push actually targeted (drift + audit baseline)."""
    group_id = scope.get("group_id")
    if not group_id:
        return None
    if store.get(group_id) is None:
        return None
    return store.record_resolution(group_id, agent_ids, reason=reason, actor=actor)


def attribute_keys(
    agents: Sequence[AgentState],
    *,
    limit_values: int = 12,
) -> Dict[str, Any]:
    """Distinct description attribute keys and sample values from the live fleet.

    This is the typo-proofing half of the feature (#111 work item 6): a builder that
    offers keys harvested from real agents removes the "environment" vs "env" class
    of mistake entirely.

    Deliberately reads only `description` — never the effective config, which is
    where a fleet's credentials live (D8).
    """
    keys: Dict[str, Dict[str, Any]] = {}
    for agent in agents:
        attributes = list((agent.description or {}).get("identifyingAttributes") or []) + list(
            (agent.description or {}).get("nonIdentifyingAttributes") or []
        )
        for entry in attributes:
            key = (entry or {}).get("key")
            if not key:
                continue
            bucket = keys.setdefault(
                str(key),
                {"key": str(key), "agent_count": 0, "values": [], "value_count": 0},
            )
            bucket["agent_count"] += 1
            value = agent.get_attr(str(key))
            if value is None:
                continue
            if value not in bucket["values"]:
                if len(bucket["values"]) < limit_values:
                    bucket["values"].append(value)
                bucket["value_count"] += 1
    for bucket in keys.values():
        bucket["values"].sort(key=str.lower)
    return {
        "keys": sorted(keys.values(), key=lambda bucket: bucket["key"].lower()),
        "count": len(keys),
        "reserved": sorted(RESERVED_FILTER_KEYS),
        "matching": "exact, case-insensitive equality — no globs, no regex, no prefix",
        "agents": len(agents),
    }


def view(
    record: Mapping[str, Any],
    agents: Sequence[AgentState],
    *,
    store: SQLiteGroupStore = COLLECTOR_GROUPS,
    include_history: bool = True,
) -> Dict[str, Any]:
    """API-shaped group: the definition, its live member count and its history.

    `member_count` is live and never stored, so `GET /groups` cannot drift from the
    fleet the way a cached membership would.
    """
    resolved = resolve_members(record, agents)
    last = store.last_resolution(record["group_id"])
    out = {
        "group_id": record.get("group_id"),
        "name": record.get("name"),
        "definition": definition_of(record),
        "include": list(record.get("include") or []),
        "exclude": list(record.get("exclude") or []),
        "description": record.get("description"),
        "owner": record.get("owner"),
        "source": record.get("source"),
        "created_by": record.get("created_by"),
        "created_at": record.get("created_at"),
        "updated_by": record.get("updated_by"),
        "updated_at": record.get("updated_at"),
        "archived_at": record.get("archived_at"),
        "archived_by": record.get("archived_by"),
        "member_count": resolved["count"],
        "members": resolved["members"],
        "sources": resolved["sources"],
        "unknown_pins": resolved["unknown_pins"],
        "health_defined": health_defined(definition_of(record)),
        "last_resolution": last,
        "membership_changes": _membership_changes(store, record["group_id"]),
    }
    if include_history:
        out["resolutions"] = store.resolutions(record["group_id"], limit=10)
    return out


def _membership_changes(store: SQLiteGroupStore, group_id: str, *, window: int = 20) -> int:
    """How many recorded resolutions had a different membership than the one before.

    "resolution changed 3 times since" (#111 work item 6) is a drift signal an
    operator can act on; a raw history list makes them count it themselves.
    """
    history = store.resolutions(group_id, limit=window)
    previous: Optional[List[str]] = None
    changes = 0
    for entry in reversed(history):
        current = list(entry.get("agent_ids") or [])
        if previous is not None and current != previous:
            changes += 1
        previous = current
    return changes
