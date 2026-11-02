"""Server→agent commands (#84): the `ServerToAgentCommand` queue.

OpAMP gives the server one imperative message, and the spec is strict about it
(`specification.md:1094`): a `ServerToAgent` carrying `command` must carry **nothing
but** `instance_uid`, `capabilities` and `command` — every other field is ignored by
the agent. So a command is never "attached" to a reply; it replaces it for one poll.

Three consequences shape this module:

1. **The queue is persisted.** HTTP has no server push, so the command rides the
   agent's next poll — and a command lost on a server restart is a silent no-op.
   It lives in the agent row (`pending_command`), through the same batched write path
   as every other field group (#109).
2. **Delivery is one-shot.** `take()` clears the command *before* the reply is
   serialised, so a re-delivered restart is impossible. A re-delivering queue turns
   one operator action into a crash loop.
3. **Refusal is explicit.** Most collectors ship with
   `accepts_restart_command: false` (upstream default), so "no capability" is the
   common case: 409 with a message naming the flag, never a silent no-op.

Restart is the only command this build sends. `CommandType` has exactly one other
value (the zero sentinel), so nothing else can be requested by accident.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from loguru import logger

from proto.opamp_pb2 import (
    AgentCapabilities,
    CommandType,
    ServerToAgentCommand,
)
from server.state import AGENT_REGISTRY, utcnow

COMMAND_RESTART = "restart"
COMMANDS = (COMMAND_RESTART,)


class CommandError(Exception):
    """A refused command request, in HTTP terms the route can pass through."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def restart_allowed() -> bool:
    """`OPAMP_RESTART_COMMAND_ENABLED` (default true — it is gated by capability).

    A separate switch because a restart is disruptive to a live collector and some
    deployments want the surface to not exist at all, even for an operator.
    """
    raw = (os.environ.get("OPAMP_RESTART_COMMAND_ENABLED") or "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def accepts_restart(state: Any) -> bool:
    return bool(
        int(getattr(state, "capabilities", 0) or 0)
        & AgentCapabilities.AgentCapabilities_AcceptsRestartCommand
    )


def pending_for(agent_id: str) -> Optional[Dict[str, Any]]:
    state = AGENT_REGISTRY.get(agent_id)
    if state is None:
        return None
    return getattr(state, "pending_command", None)


def request_restart(agent_id: str, principal: Any = None, reason: Optional[str] = None) -> Dict[str, Any]:
    """Queue a restart for the agent's next poll. Refuses when it cannot be honoured.

    Capability first: an agent that never advertised `AcceptsRestartCommand` will
    ignore the command, and pretending otherwise turns "the collector is still
    broken" into "the button does nothing". The `instance_uid` is checked against the
    registry too, so a stale agent id is a 404 rather than a command queued for
    nobody.
    """
    if not restart_allowed():
        raise CommandError(
            404,
            "the restart command is disabled (OPAMP_RESTART_COMMAND_ENABLED=false)",
        )
    state = AGENT_REGISTRY.get(agent_id)
    if state is None:
        raise CommandError(404, f"agent {agent_id} not found")
    if not accepts_restart(state):
        raise CommandError(
            409,
            {
                "message": (
                    f"agent {agent_id} does not accept restart commands "
                    "(AgentCapabilities_AcceptsRestartCommand is not set)"
                ),
                "hint": (
                    "set `accepts_restart_command: true` on the collector's `opamp` "
                    "extension and restart it once; upstream defaults this to false"
                ),
                "capabilities": int(getattr(state, "capabilities", 0) or 0),
            },
        )

    record = {
        "type": COMMAND_RESTART,
        "requested_at": utcnow().isoformat(),
        "requested_by": getattr(principal, "name", None) or "system",
        "reason": (reason or "")[:500] or None,
    }
    AGENT_REGISTRY.update(agent_id, persist=True, pending_command=record)
    logger.bind(
        event="agent.command.queued",
        agent_id=agent_id,
        command=COMMAND_RESTART,
        requested_by=record["requested_by"],
    ).info(
        f"restart queued for {agent_id} by {record['requested_by']}; it will be "
        "delivered on the agent's next message"
    )
    return record


def take(agent_id: str) -> Optional[ServerToAgentCommand]:
    """Pop the pending command, if any, marking it delivered. Exactly once.

    The queue is cleared in the same call that builds the message, so a writer that
    crashes mid-send loses one restart (visible in `command_delivered_at` and the log)
    instead of repeating it forever.
    """
    state = AGENT_REGISTRY.get(agent_id)
    record = getattr(state, "pending_command", None) if state is not None else None
    if not record:
        return None
    command_type = record.get("type")
    if command_type != COMMAND_RESTART:
        # An unknown queued command is dropped rather than guessed at: sending the
        # wrong imperative to a live collector is worse than doing nothing.
        logger.bind(
            event="agent.command.dropped", agent_id=agent_id, command=command_type
        ).warning(f"dropping unknown queued command {command_type!r} for {agent_id}")
        AGENT_REGISTRY.update(agent_id, persist=True, pending_command=None)
        return None

    delivered_at = utcnow().isoformat()
    AGENT_REGISTRY.update(
        agent_id,
        persist=True,
        pending_command=None,
        command_delivered_at=delivered_at,
    )
    logger.bind(
        event="agent.command.delivered",
        agent_id=agent_id,
        command=COMMAND_RESTART,
        requested_at=record.get("requested_at"),
        requested_by=record.get("requested_by"),
    ).info(
        f"restart delivered to {agent_id} "
        f"(requested {record.get('requested_at')} by {record.get('requested_by')})"
    )
    command = ServerToAgentCommand()
    command.type = CommandType.CommandType_Restart
    return command
