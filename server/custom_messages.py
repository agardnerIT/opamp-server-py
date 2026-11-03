"""Custom capabilities and custom messages (#85): the spec's extension mechanism.

OpAMP reserves a reverse-FQDN namespace for features that are not in the core spec —
the collector's `io.opentelemetry.pause` is the motivating example. Two messages:

``AgentToServer.custom_capabilities``
    The agent's whole list, sent when it changes. **Replaced, never appended**: the
    message carries the complete list, so appending would keep offering capabilities
    the agent has dropped.

``CustomMessage{capability, type, data}``
    Opaque bytes scoped to a capability and a type *within* that capability. The
    server never interprets `data` — it has no way to know a capability's payload
    contract, and guessing would corrupt a third-party extension — so this module is
    a validated, audited **transport**, not a feature layer.

Both directions are Development status upstream, so the shape may move; the spec
commit this build targets is pinned in README (`SPEC_VERSION`, #79).

Three rules, mirroring `server/commands.py` because the delivery problem is the same:

1. **The queue is persisted.** HTTP has no server push, so a custom message rides the
   agent's next poll, and a queued message lost on a restart is a silent no-op.
2. **Delivery is one-shot.** `take()` clears the queue *before* the reply is
   serialised: a re-delivered pause is not a pause, it is an outage that keeps
   happening.
3. **Refusal is explicit.** An agent that never advertised a capability will ignore a
   message for it, so "not supported" is a 400 that lists what the agent *does*
   support, never a silent drop.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from loguru import logger

from proto.opamp_pb2 import CustomMessage
from server.state import AGENT_REGISTRY, utcnow

# A custom payload is opaque to us, but not unbounded: an agent that echoes it back,
# a log line and the audit row all pay for it. 64 KiB is far above any real capability
# payload (a pause request is a few dozen bytes) and far below the message limit.
MAX_DATA_BYTES = 64 * 1024


class CustomMessageError(Exception):
    """A refused custom message, in HTTP terms the route can pass through."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def enabled() -> bool:
    """`OPAMP_CUSTOM_MESSAGES_ENABLED` (default true — it is gated by capability).

    A deployment that does not want arbitrary capability payloads crossing the server
    can remove the surface entirely; ops should not have to rely on nobody using it.
    """
    raw = (os.environ.get("OPAMP_CUSTOM_MESSAGES_ENABLED") or "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def supported(agent_id: str) -> List[str]:
    state = AGENT_REGISTRY.get(agent_id)
    return list(getattr(state, "custom_capabilities", None) or []) if state else []


def _normalise_capabilities(value: Any) -> List[str]:
    """The wire list as a deduplicated, ordered list of strings.

    Empty entries are dropped rather than stored: an empty capability would match
    every message whose `capability` is empty, which is not a namespace.
    """
    if isinstance(value, dict):
        value = value.get("capabilities") or []
    if not isinstance(value, (list, tuple)):
        return []
    seen: List[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def note_capabilities(agent_id: str, message: Dict[str, Any]) -> Optional[List[str]]:
    """Return the new capability list when a `custom_capabilities` message changed it.

    Returned rather than written so it joins the agent's one batched write per
    message (#109); `None` means "nothing to change".
    """
    capabilities = _normalise_capabilities(message)
    state = AGENT_REGISTRY.get(agent_id)
    if state is None:
        return capabilities or None
    if list(state.custom_capabilities or []) == capabilities:
        return None
    logger.bind(
        event="opamp.custom_capabilities",
        agent_id=agent_id,
        count=len(capabilities),
        capabilities=capabilities,
    ).info(
        f"agent {agent_id} advertises {len(capabilities)} custom capabilities: "
        + (", ".join(capabilities) if capabilities else "none")
    )
    return capabilities


def note_inbound(agent_id: str, message: Dict[str, Any]) -> None:
    """Log a custom message the *agent* sent us. The server cannot interpret it.

    Deliberately not stored: the payload is capability-defined binary, and a column
    of uninterpreted blobs is how a fleet's data ends up in the admin database by
    accident. The audit trail has the fact, and `data` length only.
    """
    capability = (message or {}).get("capability") or ""
    type_ = (message or {}).get("type") or ""
    data = (message or {}).get("data") or ""
    length = len(data) if isinstance(data, str) else len(str(data))
    logger.bind(
        event="opamp.custom_message_received",
        agent_id=agent_id,
        capability=capability,
        type=type_,
        bytes=length,
    ).info(
        f"agent {agent_id} sent custom message {capability}/{type_} ({length} bytes); "
        "the server does not interpret capability payloads"
    )


def queue(
    agent_id: str,
    *,
    capability: str,
    type: str,
    data: str = "",
    principal: Any = None,
) -> Dict[str, Any]:
    """Queue a custom message for the agent's next poll. Refuses what cannot work.

    Capability first: `CustomMessage.capability` must be one of the agent's advertised
    `custom_capabilities`, and a message for an unadvertised capability is a 400 that
    lists what the agent does support. Sending it anyway would produce a request the
    agent is entitled to ignore, which reads to an operator as "the feature is broken".

    `data` is base64 on the wire (JSON cannot carry bytes) and is validated here so a
    malformed payload fails at the API rather than on a live collector.
    """
    if not enabled():
        raise CustomMessageError(
            404,
            "custom messages are disabled (OPAMP_CUSTOM_MESSAGES_ENABLED=false)",
        )
    capability = (capability or "").strip()
    type_ = (type or "").strip()
    if not capability:
        raise CustomMessageError(422, "capability is required (a reverse FQDN)")
    if not type_:
        raise CustomMessageError(
            422,
            "type is required: types are scoped to the capability, and the server "
            "cannot guess one",
        )

    state = AGENT_REGISTRY.get(agent_id)
    if state is None:
        raise CustomMessageError(404, f"agent {agent_id} not found")
    advertised = list(getattr(state, "custom_capabilities", None) or [])
    if capability not in advertised:
        raise CustomMessageError(
            400,
            {
                "message": (
                    f"agent {agent_id} does not advertise the capability {capability!r}"
                ),
                "supported_capabilities": advertised,
                "hint": (
                    "an agent ignores a message for a capability it did not advertise; "
                    "`GET /agent/{id}` shows `custom_capabilities`"
                ),
            },
        )

    try:
        raw = base64.b64decode(data or "", validate=True)
    except Exception:
        raise CustomMessageError(
            422,
            "data must be base64 (an empty string is valid: some capabilities carry "
            "no payload)",
        )
    if len(raw) > MAX_DATA_BYTES:
        raise CustomMessageError(
            413,
            f"custom message payload is {len(raw)} bytes; the limit is {MAX_DATA_BYTES}",
        )

    record = {
        "capability": capability,
        "type": type_,
        # Stored as the base64 the caller sent: it is the exact wire form, and
        # re-encoding a decoded buffer is a place to introduce a bug for no gain.
        "data": data or "",
        "queued_at": utcnow().isoformat(),
        "queued_by": getattr(principal, "name", None) or "system",
    }
    AGENT_REGISTRY.update(agent_id, persist=True, pending_custom_message=record)
    logger.bind(
        event="opamp.custom_message.queued",
        agent_id=agent_id,
        capability=capability,
        type=type_,
        bytes=len(raw),
        queued_by=record["queued_by"],
    ).info(
        f"custom message {capability}/{type_} queued for {agent_id} by "
        f"{record['queued_by']}; it will be delivered on the agent's next message"
    )
    return record


def pending_for(agent_id: str) -> Optional[Dict[str, Any]]:
    state = AGENT_REGISTRY.get(agent_id)
    if state is None:
        return None
    return getattr(state, "pending_custom_message", None)


def take(agent_id: str) -> Optional[CustomMessage]:
    """Pop the pending custom message, if any. Exactly once.

    Cleared before the reply is serialised, for the same reason the restart command
    is: a re-delivered imperative every poll is a loop, and the operator's only signal
    would be "the collector keeps doing the thing I asked for once".
    """
    record = pending_for(agent_id)
    if not record:
        return None
    capability = record.get("capability") or ""
    type_ = record.get("type") or ""
    if not capability or not type_:
        logger.bind(
            event="opamp.custom_message.dropped", agent_id=agent_id, record=record
        ).warning(f"dropping malformed queued custom message for {agent_id}")
        AGENT_REGISTRY.update(agent_id, persist=True, pending_custom_message=None)
        return None

    delivered_at = utcnow().isoformat()
    AGENT_REGISTRY.update(
        agent_id,
        persist=True,
        pending_custom_message=None,
        custom_message_delivered_at=delivered_at,
    )
    logger.bind(
        event="opamp.custom_message.delivered",
        agent_id=agent_id,
        capability=capability,
        type=type_,
        queued_at=record.get("queued_at"),
        queued_by=record.get("queued_by"),
    ).info(f"custom message {capability}/{type_} delivered to {agent_id}")

    message = CustomMessage()
    message.capability = capability
    message.type = type_
    try:
        message.data = base64.b64decode(record.get("data") or "", validate=True)
    except Exception:
        # The queue is already cleared: a payload that cannot be decoded is dropped
        # rather than retried forever.
        logger.bind(
            event="opamp.custom_message.undecodable",
            agent_id=agent_id,
            capability=capability,
        ).warning(
            f"queued custom message {capability}/{type_} for {agent_id} has invalid "
            "base64 data; dropped"
        )
        message.data = b""
    return message
