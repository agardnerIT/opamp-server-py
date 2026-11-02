"""Staged rollout engine with a health gate, blast-radius caps and auto-pause (#101).

This module owns **who gets the config next and whether we may continue**. It does not
own the wire (`server.remote_config`, #81/#102) or the HTTP surface (the routes land in
#103) — it is deliberately callable without FastAPI so the whole promotion decision is
unit-testable.

The failure mode this exists for: one typo, four hundred collectors down, and because
their `opamp` extension is no longer running there is no remote way to fix them
(upstream: an invalid config leaves the collector down "until manually restarted").
So a rollout never moves from one agent to two hundred without evidence that agent one
came back healthy.

    pending ─promote─► delivered ─gate ok─► next batch … ─► completed
                                   │
                                   └─gate fails─► paused (never an automatic rollback)

Four rules the shape of this module enforces:

1. **Nothing is delivered until its batch is promoted.** Approval assigns every
   revision but *holds* delivery (`desired_configs.held`), so an HTTP agent polling in
   the meantime is refused a config whose batch has not been cleared. The hold is
   written in the same statement as the assignment, so there is no window between
   "approved" and "staged".
2. **The gates are pure functions over agent facts.** `batch_gate()` takes the facts and
   a clock value and returns a verdict; it reads no env, no database and no wall clock.
   That is why the accepted tests move time instead of sleeping.
3. **A failure pauses; it never rolls the fleet back.** Rolling back three hundred
   agents because one failed is a second outage. The operator gets "N applied, K failed,
   paused at batch 2" and two decisions: retry the failed agents (an approver
   reactivation, #99) or a rollback rollout, which is its own rollout with the same
   gating.
4. **A stalled rollout expires.** `CONFIG_PUSH_ROLLOUT_TTL_MINUTES` means an approval
   nobody acted on cannot fire three weeks later against a fleet that has moved on.

Promotion is driven by one background task (`rollout_loop()` in `server/main.py`), not a
thread per rollout, and each tick is a handful of reads plus writes only when a fact
changed — the message path is untouched (#109).
"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import prometheus_client as prom_client
from loguru import logger

from server import audit, commands, config_control, posture, remote_config
from server.state import (
    AGENT_REGISTRY,
    AgentState,
    SQLiteRolloutStore,
    utcnow,
)

# Batch planning (#101). Defaults match the issue: canary 5% (minimum one agent), then
# fixed steps bounded by both an absolute and a relative cap, so a 500-agent fleet is
# promoted 25 at a time rather than 125.
DEFAULT_CANARY_PERCENT = 5.0
DEFAULT_MAX_AGENTS_PER_BATCH = 25
DEFAULT_MAX_PERCENT_PER_BATCH = 25.0
# How long the whole batch must have been green before we promote the next one. The
# supervisor needs a restart (SIGHUP) to apply a config, so this is not impatience
# insurance: it is the window in which a crash-looping collector shows up as unhealthy
# or silent instead of merely slow.
DEFAULT_HEALTH_GATE_SECONDS = 90
# An approval nobody acted on must not fire three weeks later.
DEFAULT_TTL_MINUTES = 60
# How often the background task advances rollouts. Short enough that "abort" and a
# cleared gate both take effect promptly, long enough to be invisible in SQLite load.
DEFAULT_TICK_SECONDS = 5.0

KIND_PROMOTE = "promote"
KIND_ROLLBACK = "rollback"

# Gate verdicts. `ok` and the wait reasons are normal; the pause reasons are the ones
# that must stop the rollout and be surfaced.
GATE_OK = "ok"
GATE_AWAITING_DELIVERY = "awaiting_delivery"
GATE_AWAITING_REPORT = "awaiting_report"
GATE_HEALTH_WINDOW = "health_gate"
# Pause reasons (#101 item 6: `opamp_config_rollout_paused_total{reason}`).
PAUSE_FAILED = "failed"
PAUSE_AUTO_REVERTED = "auto_reverted"
PAUSE_SILENT = "agent_went_silent"
PAUSE_UNHEALTHY = "unhealthy"
PAUSE_UNCONFIRMED = "delivery_unconfirmed"
PAUSE_SUPERSEDED = "superseded"
PAUSE_REASONS = frozenset(
    {
        PAUSE_FAILED,
        PAUSE_AUTO_REVERTED,
        PAUSE_SILENT,
        PAUSE_UNHEALTHY,
        PAUSE_UNCONFIRMED,
        PAUSE_SUPERSEDED,
    }
)

PROM_ROLLOUT_STATUS = prom_client.Gauge(
    "opamp_config_rollout_status",
    "Staged rollouts by status (#101)",
    ["status", "kind"],
)

PROM_ROLLOUT_BATCH = prom_client.Gauge(
    "opamp_config_rollout_batch",
    "Current batch index of the newest rollout per status (0-based)",
    ["status"],
)

PROM_ROLLOUT_AGENTS = prom_client.Gauge(
    "opamp_config_rollout_agents_total",
    "Agents in an open staged rollout, by per-agent rollout state (#101)",
    ["state"],
)

PROM_ROLLOUT_PAUSED = prom_client.Counter(
    "opamp_config_rollout_paused_total",
    "Staged rollouts paused, by reason (#101)",
    ["reason"],
)

PROM_ROLLOUT_PROMOTED = prom_client.Counter(
    "opamp_config_rollout_promoted_total",
    "Batches promoted, by kind (#101)",
    ["kind"],
)

# Prometheus label cardinality is a paging-cost decision: only ever set the fixed
# vocabulary, never a reason string a caller invented at runtime.
_ALL_REASONS = tuple(sorted(PAUSE_REASONS))


# ---------------------------------------------------------------------- settings


def _env_number(name: str, default, cast=int, minimum=0):
    """Read a number from the environment, warning and defaulting (read per call)."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using {default}")
        return default
    if value < minimum:
        logger.warning(f"{name}={raw!r} must be >= {minimum}; using {default}")
        return default
    return value


def canary_percent() -> float:
    """`CONFIG_PUSH_CANARY_PERCENT` (default 5, minimum one agent regardless)."""
    return float(
        _env_number("CONFIG_PUSH_CANARY_PERCENT", DEFAULT_CANARY_PERCENT, float, 0)
    )


def max_agents_per_batch() -> int:
    """`CONFIG_PUSH_MAX_AGENTS_PER_BATCH` — absolute blast-radius cap (default 25)."""
    return int(
        _env_number(
            "CONFIG_PUSH_MAX_AGENTS_PER_BATCH", DEFAULT_MAX_AGENTS_PER_BATCH, int, 1
        )
    )


def max_percent_per_batch() -> float:
    """`CONFIG_PUSH_MAX_PERCENT_PER_BATCH` — relative blast-radius cap (default 25%)."""
    return float(
        _env_number(
            "CONFIG_PUSH_MAX_PERCENT_PER_BATCH", DEFAULT_MAX_PERCENT_PER_BATCH, float, 0
        )
    )


def health_gate_seconds() -> int:
    """`CONFIG_PUSH_HEALTH_GATE_SECONDS` (default 90; `0` allowed for fast labs)."""
    return int(
        _env_number(
            "CONFIG_PUSH_HEALTH_GATE_SECONDS", DEFAULT_HEALTH_GATE_SECONDS, int, 0
        )
    )


def ttl_minutes() -> int:
    """`CONFIG_PUSH_ROLLOUT_TTL_MINUTES` (default 60; `0` disables expiry)."""
    return int(_env_number("CONFIG_PUSH_ROLLOUT_TTL_MINUTES", DEFAULT_TTL_MINUTES, int, 0))


def tick_seconds() -> float:
    """`CONFIG_PUSH_TICK_SECONDS` — how often the engine advances (default 5s)."""
    return float(
        _env_number("CONFIG_PUSH_TICK_SECONDS", DEFAULT_TICK_SECONDS, float, 0.1)
    )


# ----------------------------------------------------------------------- batches


def plan_batches(
    agent_ids: Sequence[str],
    *,
    canary_percent_value: Optional[float] = None,
    max_agents_per_batch_value: Optional[int] = None,
    max_percent_per_batch_value: Optional[float] = None,
) -> List[List[str]]:
    """Split the targets into deterministic batches — canary first (#101 work item 3).

    Deterministic without a random seed: the agents are **sorted**, so the same target
    set always produces the same batches (a rollout is reproducible in a test and
    reviewable before it runs). The plan is stored per agent, so a tick never recomputes
    it against a fleet that changed.

    Three caps, and the smallest wins:

    * the canary is `CONFIG_PUSH_CANARY_PERCENT` of the fleet, at least one agent, and
      never larger than an ordinary batch;
    * an ordinary batch is `CONFIG_PUSH_MAX_AGENTS_PER_BATCH` (absolute), and
    * `CONFIG_PUSH_MAX_PERCENT_PER_BATCH` of the total (relative).

    Percentages round **up** so a cap can never silently round a batch to zero, and the
    absolute minimum of one agent keeps a one-agent fleet promotable.
    """
    ordered = sorted({str(agent_id) for agent_id in agent_ids})
    total = len(ordered)
    if total == 0:
        return []

    canary_pct = canary_percent() if canary_percent_value is None else canary_percent_value
    max_agents = (
        max_agents_per_batch()
        if max_agents_per_batch_value is None
        else max_agents_per_batch_value
    )
    max_pct = (
        max_percent_per_batch()
        if max_percent_per_batch_value is None
        else max_percent_per_batch_value
    )

    max_agents = max(1, int(max_agents))
    step = max(1, min(max_agents, math.ceil(total * max_pct / 100.0)))
    canary = max(1, min(step, math.ceil(total * max(0.0, canary_pct) / 100.0)))

    batches: List[List[str]] = [ordered[:canary]]
    index = canary
    while index < total:
        batches.append(ordered[index : index + step])
        index += step
    return batches


def batch_size(
    total: int, *, max_agents_per_batch_value: Optional[int] = None,
    max_percent_per_batch_value: Optional[float] = None,
) -> int:
    """The planned step size, recorded on the rollout so the plan is explainable."""
    max_agents = (
        max_agents_per_batch()
        if max_agents_per_batch_value is None
        else max_agents_per_batch_value
    )
    max_pct = (
        max_percent_per_batch()
        if max_percent_per_batch_value is None
        else max_percent_per_batch_value
    )
    return max(1, min(max(1, int(max_agents)), math.ceil(total * max_pct / 100.0)))


# ------------------------------------------------------------------------- gates


@dataclass(frozen=True)
class AgentGate:
    """One agent's facts, as the gate sees them. Pure data, no store, no clock.

    Built by :func:`facts_for` from the in-memory `AgentState` plus the agent's
    assignment row, so the message path is never re-read for a promotion decision.
    """

    agent_id: str
    expected_hash: Optional[str] = None
    present: bool = True
    assigned: bool = True  # the rollout's revision is still this agent's assignment
    delivered: bool = False
    reported_status: Optional[str] = None  # short form: APPLIED/APPLYING/FAILED/UNSET
    reported_hash: Optional[str] = None  # our hex form
    healthy: Optional[bool] = None
    stale: bool = False
    poisoned: bool = False
    unconfirmed: bool = False

    @property
    def applied(self) -> bool:
        """`APPLIED` **with our hash** — #99's poisoning rules, not naive equality.

        The hash is compared only after `remote_config.reported_hash()` has converted
        the wire's base64 into our hex form, so "the agent has *a* config" never reads
        as "the agent has *our* config".
        """
        return (
            self.reported_status == remote_config.STATUS_APPLIED
            and bool(self.reported_hash)
            and bool(self.expected_hash)
            and self.reported_hash == self.expected_hash
        )

    @property
    def reverted(self) -> bool:
        """Poisoned while reporting a *different* hash: the supervisor restored one."""
        return (
            self.poisoned
            and bool(self.reported_hash)
            and bool(self.expected_hash)
            and self.reported_hash != self.expected_hash
            and self.reported_status != remote_config.STATUS_FAILED
        )


@dataclass(frozen=True)
class GateResult:
    """The promotion verdict for one batch, with everything a UI needs to explain it."""

    ok: bool
    reason: str
    detail: str = ""
    blocking: Tuple[str, ...] = field(default_factory=tuple)
    waiting: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def pause(self) -> bool:
        return self.reason in PAUSE_REASONS


def batch_gate(
    agents: Sequence[AgentGate],
    *,
    gate_seconds: int = DEFAULT_HEALTH_GATE_SECONDS,
    last_delivery: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> GateResult:
    """May the batch that is in flight be considered healthy? (#101 acceptance)

    All four of the issue's conditions, in the order that produces the most useful
    answer. Everything is injected — the agent facts, the clock and the last delivery
    time — so this is a pure function a test can drive without sleeping.

    1. a `FAILED` report or a poisoned revision → **pause** (`failed`), and an
       auto-revert is named separately (`auto_reverted`) because the operator's next
       action is different;
    2. an agent that was delivered to and has gone silent, or vanished from the
       registry, → **pause** (`agent_went_silent`);
    3. an agent reporting `healthy: false` → **pause** (`unhealthy`);
    4. an agent whose delivery budget ran out → **pause** (`delivery_unconfirmed`);
    5. an agent no longer assigned the rollout's revision → **pause** (`superseded`);
    6. otherwise: not delivered yet / not applied yet / the health window has not
       elapsed → **wait**, and the rollout simply does not advance.

    A gate that waits is not a failure: nothing is delivered while it waits, which is
    the entire safety property.
    """
    now = now or utcnow()
    batch = list(agents)
    if not batch:
        return GateResult(False, GATE_AWAITING_DELIVERY, "the batch has no agents")

    for gate in batch:
        if gate.reverted:
            return GateResult(
                False,
                PAUSE_AUTO_REVERTED,
                f"{gate.agent_id} reported an earlier revision after being delivered "
                f"this one (auto-revert); the revision is poisoned and will not be "
                f"retried",
                blocking=(gate.agent_id,),
            )
    for gate in batch:
        if gate.poisoned or gate.reported_status == remote_config.STATUS_FAILED:
            return GateResult(
                False,
                PAUSE_FAILED,
                f"{gate.agent_id} reported FAILED (or its revision was poisoned); "
                f"nothing further is delivered and nothing is retried automatically",
                blocking=(gate.agent_id,),
            )
    for gate in batch:
        if gate.stale or (not gate.present and gate.delivered):
            return GateResult(
                False,
                PAUSE_SILENT,
                f"{gate.agent_id} went silent after delivery, so its health cannot be "
                f"verified",
                blocking=(gate.agent_id,),
            )
    for gate in batch:
        if gate.healthy is False:
            return GateResult(
                False,
                PAUSE_UNHEALTHY,
                f"{gate.agent_id} reports healthy=false after being delivered this "
                f"config",
                blocking=(gate.agent_id,),
            )
    for gate in batch:
        if gate.unconfirmed:
            return GateResult(
                False,
                PAUSE_UNCONFIRMED,
                f"{gate.agent_id} never acknowledged its delivery "
                f"(CONFIG_MAX_DELIVERY_ATTEMPTS); the rollout cannot be verified",
                blocking=(gate.agent_id,),
            )
    for gate in batch:
        if not gate.assigned:
            return GateResult(
                False,
                PAUSE_SUPERSEDED,
                f"{gate.agent_id} was assigned a different revision, so this rollout's "
                f"config is no longer in force for it",
                blocking=(gate.agent_id,),
            )

    undelivered = tuple(gate.agent_id for gate in batch if not gate.delivered)
    if undelivered:
        return GateResult(
            False,
            GATE_AWAITING_DELIVERY,
            f"{len(undelivered)} agent(s) have not received the config yet",
            waiting=undelivered,
        )

    unapplied = tuple(gate.agent_id for gate in batch if not gate.applied)
    if unapplied:
        return GateResult(
            False,
            GATE_AWAITING_REPORT,
            f"{len(unapplied)} agent(s) have not reported APPLIED with our hash",
            waiting=unapplied,
        )

    if last_delivery is not None:
        elapsed = (now - _aware(last_delivery)).total_seconds()
        if elapsed < gate_seconds:
            return GateResult(
                False,
                GATE_HEALTH_WINDOW,
                f"{max(0.0, gate_seconds - elapsed):.0f}s of the "
                f"{gate_seconds}s health window remain",
                waiting=tuple(gate.agent_id for gate in batch),
            )

    return GateResult(
        True,
        GATE_OK,
        f"{len(batch)} agent(s) applied and healthy",
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_time(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        return None


# ------------------------------------------------------------------- store/state

STORE = SQLiteRolloutStore()


class RolloutError(Exception):
    """A refused rollout operation, in HTTP terms the caller can pass through."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def _store() -> SQLiteRolloutStore:
    return STORE


def new_rollout_id() -> str:
    return f"ro-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def facts_for(
    row: Mapping[str, Any],
    state: Optional[AgentState],
    record: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> AgentGate:
    """Turn a rollout agent row + live agent/assignment facts into gate input."""
    agent_id = row["agent_id"]
    expected = row.get("config_hash")
    ours = bool(record) and record.get("revision_id") == row.get("revision_id")
    return AgentGate(
        agent_id=agent_id,
        expected_hash=expected,
        present=state is not None,
        assigned=ours,
        delivered=bool(ours and record.get("delivered_hash")),
        reported_status=remote_config.short_status(
            getattr(state, "remote_config_status", None)
        ),
        reported_hash=remote_config.reported_hash(agent_id),
        healthy=getattr(state, "healthy", None),
        stale=bool(state is not None and state.is_stale(now or utcnow())),
        poisoned=bool(ours and (record.get("poisoned") or record.get("failed_at"))),
        unconfirmed=bool(ours and record.get("unconfirmed")),
    )


def agent_state_for(gate: AgentGate, *, delivered: bool) -> str:
    """The per-agent rollout state (the issue's vocabulary) derived from the facts.

    Derived, never authoritative: `applied` is the agent's own word and the gate is
    what decides promotion, so this is what a listing reports, not what the engine
    trusts.
    """
    if gate.reverted:
        return "reverted"
    if gate.poisoned or gate.reported_status == remote_config.STATUS_FAILED:
        return "failed"
    if gate.applied:
        return "applied"
    if (
        gate.reported_status == "APPLYING"
        and gate.reported_hash
        and gate.reported_hash == gate.expected_hash
    ):
        return "applying"
    if delivered or gate.delivered:
        return "delivered"
    return "pending"


# ----------------------------------------------------------------------- creation


def create_rollout(
    proposal_id: str,
    *,
    approver: str,
    agents: Optional[Sequence[AgentState]] = None,
    note: Optional[str] = None,
    kind: str = KIND_PROMOTE,
    canary_percent_value: Optional[float] = None,
    max_agents_per_batch_value: Optional[int] = None,
    max_percent_per_batch_value: Optional[float] = None,
    gate_seconds: Optional[int] = None,
    ttl_minutes_value: Optional[int] = None,
    allow_endpoint_change_confirmed: bool = False,
    confirm_scope_change: bool = False,
    override_reason: Optional[str] = None,
    restart_after_apply: bool = False,
    now: Optional[datetime] = None,
    deliver_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Approve a proposal **and** stage it: batch 1 is delivered, the rest is held.

    Approving is what makes a revision active, so creating a rollout *is* the approval
    — hence `approver`, and hence the route (in #103) audits `config.rollout.create`
    before calling in. The hold is applied inside the same assignment statement as the
    approval (`config_control.approve(hold_all=True)`), so no status report can collect
    an unpromoted config in between.
    """
    now = now or utcnow()
    agents = list(agents if agents is not None else AGENT_REGISTRY.list_all())
    approved = config_control.approve(
        proposal_id,
        approver=approver,
        agents=agents,
        allow_endpoint_change_confirmed=allow_endpoint_change_confirmed,
        confirm_scope_change=confirm_scope_change,
        override_reason=override_reason,
        note=note,
        hold_all=True,
    )
    revisions = approved.get("revisions") or []
    agent_ids = [revision["agent_id"] for revision in revisions]
    if not agent_ids:
        raise RolloutError(
            409,
            f"proposal {proposal_id} approved no agents, so there is nothing to roll out",
        )

    gate = health_gate_seconds() if gate_seconds is None else int(gate_seconds)
    ttl = ttl_minutes() if ttl_minutes_value is None else int(ttl_minutes_value)
    batches = plan_batches(
        agent_ids,
        canary_percent_value=canary_percent_value,
        max_agents_per_batch_value=max_agents_per_batch_value,
        max_percent_per_batch_value=max_percent_per_batch_value,
    )

    store = _store()
    rollout_id = new_rollout_id()
    expires_at = (now + timedelta(minutes=ttl)).isoformat() if ttl > 0 else None
    revision_of = {revision["agent_id"]: revision["revision_id"] for revision in revisions}
    desired = remote_config.REGISTRY.store
    config_hash = (desired.revision(revisions[0]["revision_id"]) or {}).get("config_hash")
    store.create(
        rollout_id,
        proposal_id=proposal_id,
        kind=kind,
        status=SQLiteRolloutStore.STATUS_RUNNING,
        config_hash=config_hash,
        author=approved.get("approved_by"),
        approver=approver,
        note=note,
        canary_percent=(
            canary_percent() if canary_percent_value is None else canary_percent_value
        ),
        max_agents_per_batch=(
            max_agents_per_batch()
            if max_agents_per_batch_value is None
            else max_agents_per_batch_value
        ),
        max_percent_per_batch=(
            max_percent_per_batch()
            if max_percent_per_batch_value is None
            else max_percent_per_batch_value
        ),
        health_gate_seconds=gate,
        ttl_minutes=ttl,
        batch_size=batch_size(
            len(agent_ids),
            max_agents_per_batch_value=max_agents_per_batch_value,
            max_percent_per_batch_value=max_percent_per_batch_value,
        ),
        total_agents=len(agent_ids),
        batch_count=len(batches),
        current_batch=0,
        created_at=now.isoformat(),
        expires_at=expires_at,
        restart_after_apply=int(bool(restart_after_apply)),
    )

    revision_of = {revision["agent_id"]: revision["revision_id"] for revision in revisions}
    plan: Dict[str, int] = {}
    for index, members in enumerate(batches):
        for agent_id in members:
            plan[agent_id] = index
            revision = desired.revision(revision_of[agent_id]) or {}
            store.put_agent(
                rollout_id,
                agent_id,
                batch_index=index,
                state="pending",
                revision_id=revision_of[agent_id],
                config_hash=revision.get("config_hash"),
                parent_revision_id=revision.get("parent_revision_id"),
            )

    logger.bind(
        event="config.rollout.created",
        rollout_id=rollout_id,
        proposal_id=proposal_id,
        kind=kind,
        agents=len(agent_ids),
        batches=len(batches),
    ).info(
        f"rollout {rollout_id}: {len(agent_ids)} agent(s) in {len(batches)} batch(es), "
        f"canary {len(batches[0])}"
    )
    _promote(rollout_id, 0, now=now, deliver_fn=deliver_fn)
    publish_metrics()
    return view(rollout_id)


def create_rollback_rollout(
    rollout_id: str,
    *,
    approver: str,
    agents: Optional[Sequence[AgentState]] = None,
    note: Optional[str] = None,
    gate_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
    deliver_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Roll back the agents a rollout already applied, as its own gated rollout (#101).

    Not an automatic reaction to a failure: the issue is explicit that rolling back
    three hundred agents because one failed is a second outage. This is the operator's
    one-click second decision, and it is built like any other rollout — a new revision
    per agent (D4: the previous body, naming its parent), held, canary first, gated on
    the same health evidence.
    """
    now = now or utcnow()
    agents = list(agents if agents is not None else AGENT_REGISTRY.list_all())
    store = _store()
    source = store.get(rollout_id)
    if source is None:
        raise RolloutError(404, f"rollout {rollout_id} not found")
    if source.get("kind") == KIND_ROLLBACK:
        raise RolloutError(
            409,
            "this rollout is already a rollback; roll forward with a new proposal "
            "instead of rolling a rollback back",
        )

    rows = [
        row
        for row in store.agents(rollout_id)
        if row.get("state") in ("applied", "delivered", "applying")
    ]
    if not rows:
        raise RolloutError(
            409,
            f"rollout {rollout_id} has not applied to any agent, so there is nothing "
            "to roll back",
        )

    by_id = {agent.agent_id: agent for agent in agents}
    revision_of: Dict[str, str] = {}
    for row in rows:
        agent_id = row["agent_id"]
        try:
            created = config_control.rollback(
                agent_id,
                author=approver,
                agents=[by_id[agent_id]] if agent_id in by_id else [],
                note=note or f"rollback of rollout {rollout_id}",
                held=True,
            )
        except config_control.ConfigControlError as exc:
            raise RolloutError(
                409,
                {
                    "message": f"cannot roll back {agent_id}",
                    "cause": exc.detail,
                    "hint": "a rollout can only be rolled back for agents that have a "
                    "previous revision; push a new config or propose one instead",
                },
            ) from exc
        revision_of[agent_id] = created["revision_id"]

    rollback_id = new_rollout_id()
    gate = health_gate_seconds() if gate_seconds is None else int(gate_seconds)
    ttl = ttl_minutes()
    batches = plan_batches(sorted(revision_of))
    store.create(
        rollback_id,
        proposal_id=source.get("proposal_id"),
        kind=KIND_ROLLBACK,
        status=SQLiteRolloutStore.STATUS_RUNNING,
        author=approver,
        approver=approver,
        note=note or f"rollback of rollout {rollout_id}",
        canary_percent=canary_percent(),
        max_agents_per_batch=max_agents_per_batch(),
        max_percent_per_batch=max_percent_per_batch(),
        health_gate_seconds=gate,
        ttl_minutes=ttl,
        batch_size=batch_size(len(revision_of)),
        total_agents=len(revision_of),
        batch_count=len(batches),
        current_batch=0,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=ttl)).isoformat() if ttl > 0 else None,
        detail_json=json.dumps({"rollback_of": rollout_id}),
    )
    desired = remote_config.REGISTRY.store
    for index, members in enumerate(batches):
        for agent_id in members:
            revision = desired.revision(revision_of[agent_id]) or {}
            store.put_agent(
                rollback_id,
                agent_id,
                batch_index=index,
                state="pending",
                revision_id=revision_of[agent_id],
                config_hash=revision.get("config_hash"),
                parent_revision_id=revision.get("parent_revision_id"),
            )

    logger.bind(
        event="config.rollout.created",
        rollout_id=rollback_id,
        rollback_of=rollout_id,
        agents=len(revision_of),
    ).info(
        f"rollback rollout {rollback_id}: {len(revision_of)} agent(s) from "
        f"{rollout_id}"
    )
    _promote(rollback_id, 0, now=now, deliver_fn=deliver_fn)
    publish_metrics()
    return view(rollback_id)


# --------------------------------------------------------------------- promotion


def _default_deliver(agent_id: str):
    return remote_config.deliver_now(agent_id)


def _promote(
    rollout_id: str,
    index: int,
    *,
    now: datetime,
    deliver_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Release one batch: drop the hold, then deliver (#101 work items 3 and the caps).

    Delivery goes through `remote_config.deliver_now()`, which is the same gate a poll
    goes through (D5) and which already respects `CONFIG_PUSH_MAX_CONCURRENT`, the
    per-connection token bucket and the WS outbound queue depth. A refused or throttled
    frame is **not** a failure: the agent's next report re-offers it (bounded by
    `CONFIG_MAX_DELIVERY_ATTEMPTS`), which is exactly why a dropped frame must be
    retried by reconciliation rather than lost.
    """
    deliver_fn = deliver_fn or _default_deliver
    store = _store()
    rows = [row for row in store.agents(rollout_id) if row["batch_index"] == index]
    delivered = 0
    queued = 0
    for row in rows:
        agent_id = row["agent_id"]
        # Release first: a hold that is still set makes `deliver_now` a no-op by design.
        remote_config.REGISTRY.set_held(agent_id, False)
        result = deliver_fn(agent_id)
        state = getattr(result, "state", None)
        if state in ("delivered", "queued"):
            if state == "delivered":
                delivered += 1
            else:
                queued += 1
            store.set_agent(
                rollout_id,
                agent_id,
                state="delivered",
                delivered_at=(now.isoformat() if state == "delivered" else None),
                detail=getattr(result, "detail", None) or getattr(result, "reason", None),
            )
        else:
            store.set_agent(
                rollout_id,
                agent_id,
                state="pending",
                detail=getattr(result, "reason", None) or state or "not_delivered",
            )

    rollout = store.get(rollout_id) or {}
    store.update(
        rollout_id,
        current_batch=index,
        batch_started_at=now.isoformat(),
        paused_reason=None,
        pause_detail=None,
    )
    PROM_ROLLOUT_PROMOTED.labels(kind=rollout.get("kind") or KIND_PROMOTE).inc()
    # One structured line per batch transition: the thing an operator greps for.
    logger.bind(
        event="config.rollout.batch",
        rollout_id=rollout_id,
        batch=index,
        batches=rollout.get("batch_count"),
        agents=len(rows),
        delivered=delivered,
        queued=queued,
        state="promoted",
    ).info(
        f"rollout {rollout_id}: batch {index + 1}/{rollout.get('batch_count')} "
        f"promoted ({delivered} delivered over a live connection, {queued} queued for "
        f"the next status report)"
    )
    return {"batch": index, "agents": len(rows), "delivered": delivered, "queued": queued}


def _pause(rollout: Mapping[str, Any], verdict: GateResult, now: datetime) -> None:
    """Pause, with a distinct reason, and leave every remaining hold in place."""
    store = _store()
    rollout_id = rollout["rollout_id"]
    store.update(
        rollout_id,
        status=SQLiteRolloutStore.STATUS_PAUSED,
        paused_reason=verdict.reason,
        pause_detail=verdict.detail[:2000],
        paused_at=now.isoformat(),
    )
    for agent_id in verdict.blocking:
        # Keep the *specific* state (`failed`, `reverted`) that caused the pause; only an
        # agent that was simply in flight becomes `paused`. Overwriting "failed" with
        # "paused" would make the one detail the operator needs disappear.
        current = next(
            (row for row in store.agents(rollout_id) if row["agent_id"] == agent_id),
            None,
        )
        if current is not None and current.get("state") in ("failed", "reverted"):
            continue
        store.set_agent(rollout_id, agent_id, state="paused", detail=verdict.reason)
    if verdict.reason in PAUSE_REASONS:
        PROM_ROLLOUT_PAUSED.labels(reason=verdict.reason).inc()
    summary = summary_of(rollout_id)
    logger.bind(
        event="config.rollout.paused",
        rollout_id=rollout_id,
        reason=verdict.reason,
        batch=rollout.get("current_batch"),
        applied=summary.get("applied"),
        failed=summary.get("failed"),
    ).error(
        f"rollout {rollout_id} paused at batch {int(rollout.get('current_batch') or 0) + 1}"
        f"/{rollout.get('batch_count')} ({verdict.reason}): {verdict.detail}. "
        f"{summary.get('applied')} applied, {summary.get('failed')} failed — nothing is "
        "retried automatically and no agent is rolled back automatically"
    )
    audit.audit_event(
        "config.rollout.paused",
        principal="system",
        actor_kind="system",
        result="error",
        target_kind="rollout",
        target_id=rollout_id,
        config_hash=rollout.get("config_hash"),
        reason=verdict.reason,
        detail=verdict.detail[:500],
        batch=int(rollout.get("current_batch") or 0),
        applied=summary.get("applied"),
        failed=summary.get("failed"),
    )


def _complete(rollout: Mapping[str, Any], now: datetime) -> None:
    store = _store()
    rollout_id = rollout["rollout_id"]
    store.update(
        rollout_id,
        status=SQLiteRolloutStore.STATUS_COMPLETED,
        completed_at=now.isoformat(),
    )
    summary = summary_of(rollout_id)
    logger.bind(
        event="config.rollout.completed",
        rollout_id=rollout_id,
        applied=summary.get("applied"),
        batches=rollout.get("batch_count"),
    ).info(
        f"rollout {rollout_id} completed: {summary.get('applied')} agent(s) applied"
    )
    audit.audit_event(
        "config.rollout.completed",
        principal="system",
        actor_kind="system",
        target_kind="rollout",
        target_id=rollout_id,
        config_hash=rollout.get("config_hash"),
        applied=summary.get("applied"),
        agents=summary.get("total"),
    )


# ------------------------------------------------------------------------- ticks


def _advance(
    rollout: Mapping[str, Any],
    *,
    now: datetime,
    agents: Sequence[AgentState],
    deliver_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """One tick for one running rollout: refresh facts, gate, then promote or pause."""
    store = _store()
    rollout_id = rollout["rollout_id"]
    index = int(rollout.get("current_batch") or 0)
    by_id = {agent.agent_id: agent for agent in agents}
    desired = remote_config.REGISTRY

    rows = store.agents(rollout_id)
    for row in rows:
        if row["batch_index"] > index:
            continue
        state = by_id.get(row["agent_id"])
        record = desired.get(row["agent_id"])
        gate = facts_for(row, state, record, now=now)
        derived = agent_state_for(gate, delivered=bool(row.get("delivered_at")))
        if row.get("state") == "paused" and derived == "pending":
            # A paused agent stays paused until a human decides (D6).
            continue
        delivered_at = None
        if gate.delivered and record is not None:
            delivered_at = record.get("delivered_at")
        if derived == "applied" and row.get("state") != "applied":
            store.set_agent(
                rollout_id,
                row["agent_id"],
                state="applied",
                applied_at=now.isoformat(),
                detail="agent reports APPLIED with our hash",
            )
        elif derived != row.get("state"):
            store.set_agent(
                rollout_id,
                row["agent_id"],
                state=derived,
                delivered_at=delivered_at or row.get("delivered_at"),
            )
        elif delivered_at and not row.get("delivered_at"):
            store.set_agent(rollout_id, row["agent_id"], delivered_at=delivered_at)

    in_batch = [row for row in rows if row["batch_index"] == index]
    gates = [
        facts_for(
            row,
            by_id.get(row["agent_id"]),
            desired.get(row["agent_id"]),
            now=now,
        )
        for row in in_batch
    ]
    last_delivery = _last_delivery(rollout, in_batch)
    verdict = batch_gate(
        gates,
        gate_seconds=int(rollout.get("health_gate_seconds") or 0),
        last_delivery=last_delivery,
        now=now,
    )
    if verdict.pause:
        _pause(rollout, verdict, now)
        return {"rollout_id": rollout_id, "action": "paused", "reason": verdict.reason}
    if not verdict.ok:
        return {
            "rollout_id": rollout_id,
            "action": "waiting",
            "reason": verdict.reason,
            "waiting": len(verdict.waiting),
        }

    # #84: "I pushed a config and it needs a restart" is one operator action. The
    # restart is queued *after* the gate opens, so the config is applied before the
    # collector restarts, and it is queued only for agents that advertise the
    # capability (the others get a log line, not a silent no-op).
    if int(rollout.get("restart_after_apply") or 0):
        _queue_restarts(rollout, [row["agent_id"] for row in in_batch], by_id)

    batch_count = int(rollout.get("batch_count") or 1)
    if index + 1 >= batch_count:
        _complete(rollout, now)
        return {"rollout_id": rollout_id, "action": "completed"}

    _promote(rollout_id, index + 1, now=now, deliver_fn=deliver_fn)
    return {"rollout_id": rollout_id, "action": "promoted", "batch": index + 1}


def _last_delivery(
    rollout: Mapping[str, Any], in_batch: Sequence[Mapping[str, Any]]
) -> Optional[datetime]:
    """When the batch was last handed a config — the start of the health window.

    `max(batch_started_at, every recorded delivery in the batch)`: the window starts
    when we promoted (so an agent that never reports still ages out), and it restarts if
    a delivery only actually happened later (an agent that was offline and collected it
    on its next poll).
    """
    stamps = [_parse_time(rollout.get("batch_started_at"))]
    stamps.extend(_parse_time(row.get("delivered_at")) for row in in_batch)
    present = [stamp for stamp in stamps if stamp is not None]
    return max(present) if present else None



def _queue_restarts(
    rollout: Mapping[str, Any],
    agent_ids: Sequence[str],
    by_id: Mapping[str, Optional[AgentState]],
) -> List[str]:
    """Queue a restart for each promoted agent that accepts one (#84, #101)."""
    queued: List[str] = []
    for agent_id in agent_ids:
        state = by_id.get(agent_id)
        if state is None:
            continue
        if not commands.accepts_restart(state):
            logger.bind(
                event="config.rollout.restart_skipped",
                rollout_id=rollout["rollout_id"],
                agent_id=agent_id,
            ).info(
                f"rollout {rollout['rollout_id']}: {agent_id} does not accept restart "
                "commands; skipping the restart for this batch"
            )
            continue
        try:
            commands.request_restart(
                agent_id,
                principal=None,
                reason=f"rollout {rollout['rollout_id']} batch "
                f"{int(rollout.get('current_batch') or 0) + 1}",
            )
            queued.append(agent_id)
        except commands.CommandError as exc:  # pragma: no cover - capability re-checked
            logger.bind(
                event="config.rollout.restart_failed",
                rollout_id=rollout["rollout_id"],
                agent_id=agent_id,
            ).warning(f"could not queue a restart for {agent_id}: {exc.detail}")
    if queued:
        logger.bind(
            event="config.rollout.restart",
            rollout_id=rollout["rollout_id"],
            agents=len(queued),
        ).info(
            f"rollout {rollout['rollout_id']}: restart queued for {len(queued)} agent(s)"
        )
    return queued


def preview_rollout(
    proposal_id: str,
    *,
    canary_percent_value: Optional[float] = None,
    max_agents_per_batch_value: Optional[int] = None,
    max_percent_per_batch_value: Optional[float] = None,
    gate_seconds: Optional[int] = None,
    ttl_minutes_value: Optional[int] = None,
    restart_after_apply: bool = False,
) -> Dict[str, Any]:
    """The dry run (#103): resolve, plan and validate — **moves nothing**.

    This is the button people press most, so it is cheap and it is honest about what
    would happen: the exact batches, the canary, the caps in force and the stored
    validation verdict. It does not approve, activate, hold or deliver anything, and
    it refuses a proposal that has already been decided rather than pretending.
    """
    store = _store()
    proposal = config_control.proposal_view(proposal_id)
    statuses = proposal.get("statuses") or []
    if statuses and statuses != ["proposed"]:
        raise RolloutError(
            409,
            {
                "message": f"proposal {proposal_id} is already {', '.join(statuses)}",
                "hint": "a rollout can only be created from a proposed revision; "
                "reactivate a poisoned one (#99) or propose a new revision",
            },
        )
    agent_ids = sorted({row["agent_id"] for row in proposal["revisions"]})
    if not agent_ids:
        raise RolloutError(409, f"proposal {proposal_id} targets no agents")

    gate = health_gate_seconds() if gate_seconds is None else int(gate_seconds)
    ttl = ttl_minutes() if ttl_minutes_value is None else int(ttl_minutes_value)
    batches = plan_batches(
        agent_ids,
        canary_percent_value=canary_percent_value,
        max_agents_per_batch_value=max_agents_per_batch_value,
        max_percent_per_batch_value=max_percent_per_batch_value,
    )
    verdict = (proposal["revisions"][0] or {}).get("verdict") or {}
    return {
        "dry_run": True,
        "proposal_id": proposal_id,
        "config_hash": proposal.get("config_hash"),
        "status": "planned",
        "agents": len(agent_ids),
        "batch": {
            "count": len(batches),
            "canary_percent": (
                canary_percent() if canary_percent_value is None else canary_percent_value
            ),
            "max_agents_per_batch": (
                max_agents_per_batch()
                if max_agents_per_batch_value is None
                else max_agents_per_batch_value
            ),
            "max_percent_per_batch": (
                max_percent_per_batch()
                if max_percent_per_batch_value is None
                else max_percent_per_batch_value
            ),
            "size": batch_size(
                len(agent_ids),
                max_agents_per_batch_value=max_agents_per_batch_value,
                max_percent_per_batch_value=max_percent_per_batch_value,
            ),
            "health_gate_seconds": gate,
            "ttl_minutes": ttl,
            "restart_after_apply": bool(restart_after_apply),
        },
        "batches": [
            {"batch": index, "agents": len(members), "agent_ids": members}
            for index, members in enumerate(batches)
        ],
        "verdict": verdict or None,
        "delivery": {
            "state": "unchanged",
            "reason": "dry_run",
            "detail": "a dry run approves, holds and delivers nothing",
        },
    }


def _expire(rollout: Mapping[str, Any], now: datetime) -> Dict[str, Any]:
    """TTL: an approval nobody acted on must not fire later against a moved fleet."""
    store = _store()
    rollout_id = rollout["rollout_id"]
    store.update(
        rollout_id,
        status=SQLiteRolloutStore.STATUS_CANCELLED,
        cancelled_at=now.isoformat(),
        paused_reason="ttl_expired",
        pause_detail=(
            f"the rollout expired after {rollout.get('ttl_minutes')} minutes without "
            "completing"
        ),
    )
    summary = summary_of(rollout_id)
    logger.bind(
        event="config.rollout.cancelled",
        rollout_id=rollout_id,
        reason="ttl_expired",
        applied=summary.get("applied"),
    ).warning(
        f"rollout {rollout_id} cancelled: TTL expired after "
        f"{rollout.get('ttl_minutes')} minutes ({summary.get('applied')} applied, "
        f"{summary.get('pending')} never delivered)"
    )
    audit.audit_event(
        "config.rollout.cancelled",
        principal="system",
        actor_kind="system",
        result="error",
        target_kind="rollout",
        target_id=rollout_id,
        config_hash=rollout.get("config_hash"),
        reason="ttl_expired",
        applied=summary.get("applied"),
        pending=summary.get("pending"),
    )
    return {"rollout_id": rollout_id, "action": "cancelled", "reason": "ttl_expired"}


def tick(
    *,
    now: Optional[datetime] = None,
    agents: Optional[Sequence[AgentState]] = None,
    deliver_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Advance every open rollout once. Called from the background task, never a thread.

    A single pass over the open rollouts: gate the in-flight batch, promote or pause,
    then expire anything past its TTL. Nothing here is per-rollout asynchronous, so a
    slow fleet cannot accumulate tasks.
    """
    if not posture.push_allowed():
        return {"skipped": "push_disabled", "events": []}
    now = now or utcnow()
    if agents is None:
        agents = AGENT_REGISTRY.list_all()
    store = _store()
    events: List[Dict[str, Any]] = []

    for rollout in store.list(statuses=[SQLiteRolloutStore.STATUS_RUNNING]):
        try:
            events.append(
                _advance(
                    rollout, now=now, agents=agents, deliver_fn=deliver_fn
                )
            )
        except Exception as exc:  # pragma: no cover - one rollout must not stop the tick
            logger.bind(
                event="config.rollout.tick_failed", rollout_id=rollout.get("rollout_id")
            ).warning(f"rollout {rollout.get('rollout_id')} could not advance: {exc}")

    if ttl_minutes() > 0:
        for rollout in store.list(
            statuses=[
                SQLiteRolloutStore.STATUS_RUNNING,
                SQLiteRolloutStore.STATUS_PAUSED,
                SQLiteRolloutStore.STATUS_PENDING,
            ]
        ):
            expires_at = _parse_time(rollout.get("expires_at"))
            if expires_at is not None and now >= expires_at:
                events.append(_expire(rollout, now))

    publish_metrics()
    return {"events": events, "advanced": len(events)}


# ---------------------------------------------------------------------- operator


def abort(
    rollout_id: str,
    *,
    principal: Any = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Stop promotion immediately. **Does not recall already-delivered configs.**

    It cannot: the config is on the agent and the agent is running it. What abort
    guarantees is that no further batch is released and that every un-promoted agent
    stays held, so an aborted rollout can never widen. Already-applied agents are left
    exactly as they are — rolling them back is a separate, gated decision
    (`create_rollback_rollout`), because that is a second outage if it is automatic.

    The audit row is written **here** rather than by the caller: #103's route will use
    `require_audit("config.rollout.abort")`, which preflights the log without writing,
    so the transition and the record cannot disagree (and there is exactly one row).
    """
    now = now or utcnow()
    store = _store()
    rollout = store.get(rollout_id)
    if rollout is None:
        raise RolloutError(404, f"rollout {rollout_id} not found")
    if rollout.get("status") not in SQLiteRolloutStore.OPEN_STATUSES:
        raise RolloutError(
            409,
            f"rollout {rollout_id} is {rollout.get('status')}; only an open rollout "
            "can be aborted",
        )
    store.update(
        rollout_id,
        status=SQLiteRolloutStore.STATUS_ABORTED,
        cancelled_at=now.isoformat(),
        paused_reason="aborted",
        pause_detail=(reason or "aborted by an operator")[:2000],
    )
    summary = summary_of(rollout_id)
    actor = getattr(principal, "name", None) or "system"
    logger.bind(
        event="config.rollout.aborted",
        rollout_id=rollout_id,
        actor=actor,
        applied=summary.get("applied"),
        pending=summary.get("pending"),
    ).warning(
        f"rollout {rollout_id} aborted by {actor}: {summary.get('applied')} agent(s) "
        f"already applied are untouched, {summary.get('pending')} un-promoted agent(s) "
        "stay held"
    )
    audit.audit_event(
        "config.rollout.abort",
        principal=actor,
        role=getattr(principal, "role", None),
        actor_kind="human" if principal is not None else "system",
        target_kind="rollout",
        target_id=rollout_id,
        config_hash=rollout.get("config_hash"),
        reason=reason,
        applied=summary.get("applied"),
        pending=summary.get("pending"),
    )
    publish_metrics()
    return view(rollout_id)


# ------------------------------------------------------------------------ views


def summary_of(rollout_id: str) -> Dict[str, int]:
    """`{state: n}` plus the rollup an operator reads: "N applied, K failed"."""
    store = _store()
    counts = {state: 0 for state in SQLiteRolloutStore.AGENT_STATES}
    for row in store.agents(rollout_id):
        counts[row.get("state") or "pending"] = counts.get(row.get("state") or "pending", 0) + 1
    counts["total"] = sum(
        counts.get(state, 0) for state in SQLiteRolloutStore.AGENT_STATES
    )
    return counts


def view(rollout_id: str) -> Dict[str, Any]:
    """Rollout metadata plus per-agent batch state. **Never a config body** (D8)."""
    store = _store()
    rollout = store.get(rollout_id)
    if rollout is None:
        raise RolloutError(404, f"rollout {rollout_id} not found")
    rows = store.agents(rollout_id)
    batches: List[Dict[str, Any]] = []
    for index in range(int(rollout.get("batch_count") or 0)):
        members = [row for row in rows if row["batch_index"] == index]
        counts: Dict[str, int] = {}
        for row in members:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        status = "pending"
        if index < int(rollout.get("current_batch") or 0):
            status = "applied"
        elif index == int(rollout.get("current_batch") or 0):
            status = (
                "paused"
                if rollout.get("status") == SQLiteRolloutStore.STATUS_PAUSED
                else "in_flight"
            )
            if rollout.get("status") == SQLiteRolloutStore.STATUS_COMPLETED:
                status = "applied"
        batches.append(
            {
                "batch": index,
                "agents": len(members),
                "status": status,
                "states": counts,
                "agent_ids": [row["agent_id"] for row in members],
            }
        )
    return {
        "rollout_id": rollout_id,
        "proposal_id": rollout.get("proposal_id"),
        "kind": rollout.get("kind"),
        "status": rollout.get("status"),
        "created_at": rollout.get("created_at"),
        "updated_at": rollout.get("updated_at"),
        "expires_at": rollout.get("expires_at"),
        "completed_at": rollout.get("completed_at"),
        "cancelled_at": rollout.get("cancelled_at"),
        "paused_reason": rollout.get("paused_reason"),
        "pause_detail": rollout.get("pause_detail"),
        "paused_at": rollout.get("paused_at"),
        "approver": rollout.get("approver"),
        "note": rollout.get("note"),
        "config_hash": rollout.get("config_hash"),
        "batch": {
            "current": int(rollout.get("current_batch") or 0),
            "count": int(rollout.get("batch_count") or 0),
            "size": int(rollout.get("batch_size") or 0),
            "canary_percent": rollout.get("canary_percent"),
            "max_agents_per_batch": rollout.get("max_agents_per_batch"),
            "max_percent_per_batch": rollout.get("max_percent_per_batch"),
            "health_gate_seconds": rollout.get("health_gate_seconds"),
            "ttl_minutes": rollout.get("ttl_minutes"),
            "restart_after_apply": bool(rollout.get("restart_after_apply")),
            "started_at": rollout.get("batch_started_at"),
        },
        "agents": {
            "total": int(rollout.get("total_agents") or 0),
            "states": summary_of(rollout_id),
        },
        "batches": batches,
        "detail": json.loads(rollout.get("detail_json") or "{}"),
    }


def list_rollouts(
    *, status: Optional[str] = None, limit: int = 100
) -> Dict[str, Any]:
    """Rollout listing, newest first. Metadata only, bodies are never involved."""
    if status and status not in SQLiteRolloutStore.STATUSES:
        raise RolloutError(
            422, f"status must be one of {', '.join(SQLiteRolloutStore.STATUSES)}"
        )
    rows = _store().list(statuses=[status] if status else None, limit=limit)
    return {
        "rollouts": [
            {
                "rollout_id": row["rollout_id"],
                "proposal_id": row.get("proposal_id"),
                "kind": row.get("kind"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "batch": {
                    "current": int(row.get("current_batch") or 0),
                    "count": int(row.get("batch_count") or 0),
                },
                "agents": int(row.get("total_agents") or 0),
                "paused_reason": row.get("paused_reason"),
                "states": summary_of(row["rollout_id"]),
            }
            for row in rows
        ],
        "count": len(rows),
        "statuses": list(SQLiteRolloutStore.STATUSES),
    }


def summary(rollout_id: str) -> Dict[str, Any]:
    """Read-only facts a route needs *before* auditing a decision (pre-write, #97)."""
    rollout = _store().get(rollout_id)
    if rollout is None:
        raise RolloutError(404, f"rollout {rollout_id} not found")
    return {
        "rollout_id": rollout_id,
        "proposal_id": rollout.get("proposal_id"),
        "kind": rollout.get("kind"),
        "status": rollout.get("status"),
        "config_hash": rollout.get("config_hash"),
        "agents": int(rollout.get("total_agents") or 0),
    }


def publish_metrics() -> None:
    """Refresh the gauges from the store. Cheap enough for one background tick."""
    store = _store()
    open_rows = store.list(
        statuses=list(SQLiteRolloutStore.OPEN_STATUSES), limit=1000
    )
    for status in SQLiteRolloutStore.STATUSES:
        for kind in (KIND_PROMOTE, KIND_ROLLBACK):
            PROM_ROLLOUT_STATUS.labels(status=status, kind=kind).set(
                sum(
                    1
                    for row in open_rows
                    if row.get("status") == status and (row.get("kind") or KIND_PROMOTE) == kind
                )
            )
    for status in SQLiteRolloutStore.STATUSES:
        batches = [
            int(row.get("current_batch") or 0)
            for row in open_rows
            if row.get("status") == status
        ]
        PROM_ROLLOUT_BATCH.labels(status=status).set(max(batches) if batches else 0)

    counts = store.state_counts([row["rollout_id"] for row in open_rows])
    for state in SQLiteRolloutStore.AGENT_STATES:
        PROM_ROLLOUT_AGENTS.labels(state=state).set(counts.get(state, 0))


def reset_counters() -> None:
    """Test helper: zero the pause counter so assertions do not see other tests."""
    for reason in _ALL_REASONS:
        PROM_ROLLOUT_PAUSED.labels(reason=reason).reset()
