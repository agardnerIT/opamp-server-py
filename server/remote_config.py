"""Desired (remote) config: hashing, validation and the delivery decision (#81).

The protocol half of remote configuration, kept out of `server.main` so it can be
tested without an ASGI app and so the API, the CLI and the MCP server all describe
a config the same way.

**Hashing.** Spec §Calculating Hashes says the *server* chooses the method ("The
Server MUST choose a strong hash calculation method… The hashes are opaque to the
Agent, the Agent never calculates hashes, it only stores and compares them"). The
OpAMP Supervisor verifies this: it persists the `AgentRemoteConfig` it received and
echoes `remoteConfig.GetConfigHash()` back in `RemoteConfigStatus.LastRemoteConfigHash`
— it never re-derives it. So the only requirements are determinism and collision
resistance, not a byte-for-byte match with another implementation. This module
hashes a **length-prefixed, key-sorted** encoding of the config map (SHA-256), which
is unambiguous: two different maps cannot serialise to the same bytes, and field
order in the request body cannot change the hash.

**Validation** is the pipeline in `server.config_validation` (#100): shape and size,
bounded parse, structural rules, protected invariants, inventory warnings and the
OPA policy gate. It is re-run at approve/apply time, because the schema, the
policies and an agent's component inventory can all move between Monday and Friday.

**Delivery.** A pure decision function, ``offer_for()``, so the HTTP and WebSocket
transports cannot diverge. It offers only when push is allowed by the posture, the
agent advertises `AcceptsRemoteConfig`, the assignment points at a revision that is
`active` and not poisoned, and the agent's reported hash differs from ours. On top
of #81's hash comparison it now carries #99's lifecycle guards: a delivery counter
(an agent that never reports back is not re-pushed forever), an auto-revert detector
(T9: an agent that reports a *different* config after we delivered ours has restored
its last working config, so re-pushing is the infinite loop), and delivery
bookkeeping that survives a restart.

Nothing here writes to SQLite on the steady-state message path: `DesiredConfigRegistry`
keeps the assignment in memory (loaded at startup, updated on push) because #109
measured what a per-message write costs. Writes happen only when a fact changes
(a delivery was made, a status changed, a revision was poisoned).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from proto.opamp_pb2 import (
    AgentCapabilities,
    AgentConfigMap,
    AgentRemoteConfig,
    RemoteConfigStatuses,
)
from server import audit, config_validation, crypto, posture
from server.config_validation import ConfigValidationError  # noqa: F401  (re-export)
from server.state import SQLiteDesiredConfigStore, utcnow

HASH_ALGORITHM = "sha256"
HASH_DOMAIN = b"opamp-config-map-v1\x00"

# 1 MiB by default. A collector config is kilobytes; the cap exists so a typo
# cannot put a megabyte into every agent's response and into every SQLite write.
DEFAULT_MAX_BYTES = config_validation.DEFAULT_MAX_BYTES

# How many frames we will put on the wire for one assignment before deciding the
# agent is never going to acknowledge it. Not poisoning: the revision may be fine
# and the agent wedged, so this surfaces as `unconfirmed` and stops the loop.
DEFAULT_MAX_DELIVERY_ATTEMPTS = 5

# How long a supervisor gets to apply a config and report before a repeated
# "different hash" report is treated as an auto-revert (T9). SIGHUP plus a restart
# is not instantaneous, and poisoning a healthy rollout is its own incident.
DEFAULT_REVERT_GRACE_SECONDS = 120

STATUS_NAMES = {
    RemoteConfigStatuses.RemoteConfigStatuses_UNSET: "UNSET",
    RemoteConfigStatuses.RemoteConfigStatuses_APPLIED: "APPLIED",
    RemoteConfigStatuses.RemoteConfigStatuses_APPLYING: "APPLYING",
    RemoteConfigStatuses.RemoteConfigStatuses_FAILED: "FAILED",
}

STATUS_FAILED = "FAILED"
STATUS_APPLIED = "APPLIED"

_REVISION_PREFIX = "rev"


# ---------------------------------------------------------------------- settings


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; using {default}")
        return default
    if value <= 0:
        logger.warning(f"{name}={raw!r} must be positive; using {default}")
        return default
    return value


def max_bytes() -> int:
    return config_validation.max_bytes()


def max_delivery_attempts() -> int:
    return _env_int("CONFIG_MAX_DELIVERY_ATTEMPTS", DEFAULT_MAX_DELIVERY_ATTEMPTS)


def revert_grace_seconds() -> int:
    """`CONFIG_REVERT_GRACE_SECONDS`; `0` disables the window (tests, fast labs)."""
    raw = (os.environ.get("CONFIG_REVERT_GRACE_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_REVERT_GRACE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_REVERT_GRACE_SECONDS
    return value if value >= 0 else DEFAULT_REVERT_GRACE_SECONDS


def retention_days() -> int:
    """`CONFIG_RETENTION_DAYS` (default 30; `<= 0` keeps superseded bodies forever)."""
    raw = (os.environ.get("CONFIG_RETENTION_DAYS") or "").strip()
    if not raw:
        return 30
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"CONFIG_RETENTION_DAYS={raw!r} is not an integer; using 30")
        return 30


# ------------------------------------------------------------------------ hashing


def compute_hash(config_map: Dict[str, Dict[str, Any]]) -> str:
    """SHA-256 over a canonical, length-prefixed encoding of the config map (hex).

    Length prefixes are what make this unambiguous: without them
    ``{"ab": "c"}`` and ``{"a": "bc"}`` would hash the same concatenation.
    """
    digest = hashlib.sha256(HASH_DOMAIN)
    for key in sorted(config_map):
        entry = config_map[key] or {}
        body = entry.get("body", "")
        if isinstance(body, str):
            body = body.encode("utf-8")
        parts = [key.encode("utf-8"), (entry.get("content_type") or "").encode("utf-8"), body]
        role = entry.get("role") or ""
        if role:
            # Appended only when set, and length-prefixed like every other part, so a
            # config without a role keeps the hash it had before #68 — otherwise every
            # stored revision would look changed on upgrade and be re-pushed fleet-wide.
            parts.append(str(role).encode("utf-8"))
        for part in parts:
            digest.update(struct.pack(">I", len(part)))
            digest.update(part)
    return digest.hexdigest()


def hash_bytes(config_hash: str) -> bytes:
    return bytes.fromhex(config_hash)


def hash_b64(config_hash: str) -> str:
    """The base64 form, which is what `MessageToDict` reports for a bytes field."""
    return base64.b64encode(hash_bytes(config_hash)).decode("ascii")


def hash_from_wire(value: Optional[str]) -> Optional[str]:
    """Convert a reported `last_remote_config_hash` (base64) to our hex form."""
    if not value:
        return None
    try:
        return base64.b64decode(value).hex()
    except Exception:
        return None


# --------------------------------------------------------------------- validation


def validate_config_map(
    config_map: Any,
    allow_empty: bool = False,
    **kwargs: Any,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Validate a `{key: {body, content_type}}` payload. Returns `(normalised, warnings)`.

    Raises `ConfigValidationError` naming **every** problem at once — an operator
    fixing one error per request is how a config push takes an afternoon. The full
    pipeline (and the structured verdict) lives in `server.config_validation`; this
    wrapper keeps #81's call signature and string-shaped messages.
    """
    return config_validation.validate_config_map(config_map, allow_empty, **kwargs)


def check_config_map(config_map: Any, **kwargs: Any) -> config_validation.Verdict:
    """The whole #100 pipeline as a structured verdict (never raises)."""
    return config_validation.check_config_map(config_map, **kwargs)


# -------------------------------------------------------------------- protobuf


def build_config_map(config_map: Dict[str, Dict[str, Any]]) -> AgentConfigMap:
    message = AgentConfigMap()
    for key, entry in config_map.items():
        message.config_map[key].body = (entry.get("body") or "").encode("utf-8")
        message.config_map[key].content_type = entry.get("content_type") or ""
        message.config_map[key].role = entry.get("role") or ""
    return message


def to_wire_config_map(config_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """JSON shape of a config map for API responses (bodies decoded to text)."""
    wire = {}
    for key, entry in config_map.items():
        wire[key] = {
            "body": entry.get("body", ""),
            "content_type": entry.get("content_type", ""),
        }
        if entry.get("role"):
            wire[key]["role"] = entry["role"]
    return wire


def new_revision_id() -> str:
    return f"{_REVISION_PREFIX}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------- registry


def new_proposal_id() -> str:
    """Id shared by every per-agent revision one propose/approve call creates."""
    return f"prop-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class DesiredConfigRegistry:
    """In-memory view of the current desired config per agent, backed by SQLite.

    The message path asks this on every status report, so `get()` must not touch
    the database (#109): the decoded config map is cached at load and replaced on
    push. A push is a human-scale event, so the cache is refreshed from the store
    afterwards rather than patched by hand.

    Each cache entry is the agent's **assignment** — revision content plus the
    delivery facts (`delivered_hash`, `attempts`, `poisoned`, `last_status`) — because
    a second lookup on the message path would be a second SQLite read (#109).
    """

    def __init__(self, store: Optional[SQLiteDesiredConfigStore] = None):
        self._store = store or SQLiteDesiredConfigStore()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.reload()

    @property
    def store(self) -> SQLiteDesiredConfigStore:
        return self._store

    # -- cache ---------------------------------------------------------------

    def reload(self) -> int:
        self._cache = {}
        for agent_id, record in self._store.load_current().items():
            decoded = self._decode(record)
            if decoded is not None:
                self._cache[agent_id] = decoded
        return len(self._cache)

    def _decode(self, record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not record:
            return None
        blob = record.get("config_blob")
        if not blob:
            # Retention pruned the body of a revision that is somehow still current
            # (or a plaintext-era row): treat it as absent rather than crashing.
            logger.warning(
                f"revision {record.get('revision_id')} has no stored body; ignoring"
            )
            return None
        try:
            payload = crypto.open_if_sealed(blob, f"revision:{record['revision_id']}")
            config_map = (json.loads(payload).get("config_map") if payload else {}) or {}
        except (crypto.ConfigDecryptError, ValueError) as exc:
            logger.warning(f"revision {record.get('revision_id')}: {exc}")
            return None
        decoded = dict(record)
        decoded["config_map"] = config_map
        return decoded

    # -- reads ---------------------------------------------------------------

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._cache.get(agent_id)

    def all(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot of every assignment (drift sweeps, diagnostics)."""
        return dict(self._cache)

    def history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._store.history(agent_id, limit)

    def decode_revision(self, revision_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """Decode *any* revision's body (approve/rollback re-validate history).

        Not on the message path: this reads SQLite, which is why `get()` is the only
        method the delivery decision may call (#109).
        """
        record = self._store.revision(revision_id)
        decoded = self._decode(record)
        return decoded.get("config_map") if decoded else None

    def count(self) -> int:
        return len(self._cache)

    def delivery_record(self, agent_id: str) -> Dict[str, Any]:
        """The assignment/delivery facts the API and the drift check report (#99)."""
        record = self._cache.get(agent_id) or {}
        return {
            "delivered_hash": record.get("delivered_hash"),
            "delivered_at": record.get("delivered_at"),
            "last_status": record.get("last_status"),
            "last_error": record.get("last_error"),
            "attempts": int(record.get("attempts") or 0),
            "poisoned": bool(record.get("poisoned")),
            "unconfirmed": bool(record.get("unconfirmed")),
        }

    # -- writes --------------------------------------------------------------

    def push(
        self,
        agent_id: str,
        config_map: Dict[str, Dict[str, Any]],
        author: str,
        withdrawn: bool = False,
        revision_id: Optional[str] = None,
        *,
        status: str = SQLiteDesiredConfigStore.STATUS_ACTIVE,
        proposal_id: Optional[str] = None,
        scope_kind: Optional[str] = None,
        scope_json: Optional[str] = None,
        note: Optional[str] = None,
        parent_revision_id: Optional[str] = None,
        verdict: Optional[config_validation.Verdict] = None,
        make_current: bool = True,
    ) -> Dict[str, Any]:
        """Store a new revision, optionally making it current. Returns the record.

        `revision_id` is passed in by the API so the audit row written *before* the
        mutation can name the revision it authorised (#97's pre-write contract).
        `make_current=False` is the propose step: the revision exists and is
        validated, but no agent is pointed at it until an approver assigns it.
        """
        config_hash = compute_hash(config_map)
        revision_id = revision_id or new_revision_id()
        record = self._store.put(
            agent_id=agent_id,
            revision_id=revision_id,
            config_map=config_map,
            config_hash=config_hash,
            author=author,
            withdrawn=withdrawn,
            status=status,
            proposal_id=proposal_id,
            scope_kind=scope_kind,
            scope_json=scope_json,
            note=note,
            parent_revision_id=parent_revision_id,
            verdict_json=json.dumps(verdict.to_dict(), sort_keys=True) if verdict else None,
            make_current=make_current,
        )
        if make_current:
            decoded = self._decode(record)
            if decoded is not None:
                self._cache[agent_id] = decoded
        return record

    def activate(self, agent_id: str, revision_id: str) -> None:
        """Approve-time assignment, refreshing the cache without re-reading SQLite."""
        self._store.assign(agent_id, revision_id)
        record = self._store.current(agent_id)
        decoded = self._decode(record)
        if decoded is not None:
            self._cache[agent_id] = decoded

    def withdraw(
        self, agent_id: str, author: str, revision_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Withdraw by offering an **empty** config map.

        That is the spec's own semantics: the agent merges the remote config with
        its local config, so an empty remote config means "run your local config".
        It is also why withdrawal is not a special case in `offer_for()` — it is a
        revision like any other, with a different hash, delivered exactly once.
        """
        return self.push(
            agent_id, {}, author, withdrawn=True, revision_id=revision_id
        )

    def rollback(
        self,
        agent_id: str,
        author: str,
        revision_id: Optional[str] = None,
        note: Optional[str] = None,
        proposal_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Re-push the previous revision's content as a **new** revision (D4).

        The new revision names its parent, so the chain "what replaced what, and why"
        is answerable from the revisions alone rather than from an audit row a
        retention policy might one day prune.
        """
        previous = self._store.previous(agent_id)
        decoded = self._decode(previous)
        if decoded is None:
            return None
        return self.push(
            agent_id,
            decoded["config_map"],
            author,
            revision_id=revision_id,
            note=note or f"rollback to {previous['revision_id']}",
            parent_revision_id=previous["revision_id"],
            proposal_id=proposal_id,
        )

    def record_delivery(self, agent_id: str, revision_id: str) -> int:
        """One frame was put on the wire; returns the assignment's attempt count."""
        attempts = self._store.record_delivery(agent_id, revision_id)
        record = self._cache.get(agent_id)
        if record is not None and record.get("revision_id") == revision_id:
            record["attempts"] = attempts
            record["delivered_hash"] = record.get("config_hash")
            record["delivered_at"] = utcnow().isoformat()
        return attempts

    def note_report(
        self,
        agent_id: str,
        status: Optional[str],
        error: str = "",
    ) -> bool:
        """Record the agent's latest word on its assignment. Returns True if written.

        Only writes when the value actually changes: an agent reports the same
        `APPLIED` on every heartbeat, and a SQLite write per message is exactly what
        #109 removed from this path.
        """
        record = self._cache.get(agent_id)
        if record is None:
            return False
        short = (status or "")[:40] or None
        message = (error or "")[:2000] or None
        if record.get("last_status") == short and record.get("last_error") == message:
            return False
        self._store.note_report(agent_id, short, message)
        record["last_status"] = short
        record["last_error"] = message
        return True

    def poison(self, agent_id: str, revision_id: str, detail: str = "") -> None:
        """Poison a revision and the assignment that points at it (D6)."""
        self._store.poison_revision(revision_id, detail)
        record = self._cache.get(agent_id)
        if record is not None and record.get("revision_id") == revision_id:
            record["status"] = SQLiteDesiredConfigStore.STATUS_POISONED
            record["failed_at"] = record.get("failed_at") or utcnow().isoformat()
            record["failure_detail"] = detail
            record["poisoned"] = True
            record["last_error"] = detail

    def mark_failed(self, revision_id: str, detail: str = "") -> None:
        """Backwards-compatible alias for `poison` (#81 call sites)."""
        for agent_id, record in self._cache.items():
            if record.get("revision_id") == revision_id:
                self.poison(agent_id, revision_id, detail)
                return
        self._store.poison_revision(revision_id, detail)

    def mark_unconfirmed(self, agent_id: str, detail: str = "") -> None:
        """Stop re-pushing a revision the agent never acknowledged (#99)."""
        self._store.mark_unconfirmed(agent_id, detail)
        record = self._cache.get(agent_id)
        if record is not None:
            record["unconfirmed"] = True
            record["last_error"] = detail

    def prune(self) -> int:
        """Apply `CONFIG_RETENTION_DAYS` to superseded revision bodies (#98)."""
        days = retention_days()
        if days <= 0:
            return 0
        from datetime import timedelta

        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        pruned = self._store.prune(cutoff)
        if pruned:
            logger.info(
                f"config retention: pruned {pruned} superseded revision bodies "
                f"older than {cutoff}"
            )
        return pruned


REGISTRY = DesiredConfigRegistry()


# ---------------------------------------------------------------------- delivery


def deliverable(record: Optional[Dict[str, Any]]) -> bool:
    """True when this assignment may be put on the wire right now.

    Four reasons not to deliver, and they are all "stop", never "try again":

    * the revision is not `active` (proposed, rejected, superseded, rolled back);
    * the revision is poisoned, by a `FAILED` report or an auto-revert (D6);
    * the agent never acknowledged the last delivery (`unconfirmed`), so re-pushing
      is noise, not recovery;
    * the revision body is gone (retention pruned a row that is somehow current) —
      delivering an empty map would silently wipe the agent's config.
    """
    if not record:
        return False
    if record.get("status") != SQLiteDesiredConfigStore.STATUS_ACTIVE:
        return False
    if record.get("failed_at") or record.get("poisoned") or record.get("unconfirmed"):
        return False
    return True


def offer_for(
    agent_id: str,
    capabilities: int,
    status: Optional[Dict[str, Any]] = None,
) -> Optional[AgentRemoteConfig]:
    """Build the `remote_config` offer for one message, or None (#81, #99).

    Conditions, in order:

    1. config push is allowed by the posture (#96) — a capability bit is a promise;
    2. the agent advertises `AcceptsRemoteConfig` — the spec's MUST;
    3. the assignment is deliverable (see :func:`deliverable`);
    4. the agent's reported hash differs from ours — the spec's MUST, and what
       stops a server from re-pushing a config the agent already has;
    5. the delivery budget is not exhausted — an agent that never reports back is
       surfaced as `unconfirmed` instead of being re-pushed forever.

    Delivery bookkeeping (one write per frame, never per message) is recorded here
    so a restart cannot double-deliver: `delivered_hash` is persisted, and a
    re-delivery is only justified by a hash mismatch, not by a lost in-memory flag.
    """
    if not posture.push_allowed():
        return None
    if not capabilities & AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig:
        return None

    record = REGISTRY.get(agent_id)
    if not deliverable(record):
        return None
    if not record.get("config_map") and not record.get("withdrawn"):
        logger.warning(
            f"agent {agent_id}: revision {record.get('revision_id')} has no decoded body; "
            "nothing to deliver"
        )
        return None

    reported = hash_from_wire((status or {}).get("lastRemoteConfigHash"))
    if reported == record["config_hash"]:
        return None

    revision_id = record["revision_id"]
    attempts = int(record.get("attempts") or 0)
    limit = max_delivery_attempts()
    if attempts >= limit:
        detail = (
            f"delivered {attempts} times and the agent never reported hash "
            f"{record['config_hash'][:12]}…; delivery stopped"
        )
        REGISTRY.mark_unconfirmed(agent_id, detail)
        logger.warning(f"agent {agent_id}: {detail}")
        return None

    offer = AgentRemoteConfig()
    offer.config.CopyFrom(build_config_map(record.get("config_map") or {}))
    offer.config_hash = hash_bytes(record["config_hash"])
    REGISTRY.record_delivery(agent_id, revision_id)
    return offer


def _delivered_at(record: Dict[str, Any]) -> Optional[datetime]:
    raw = record.get("delivered_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _poison(agent_id: str, record: Dict[str, Any], reason: str, detail: str) -> Dict[str, Any]:
    """Poison and audit in one place, so every path logs the same words (#99, T9)."""
    revision_id = record["revision_id"]
    REGISTRY.poison(agent_id, revision_id, detail)
    logger.error(
        f"agent {agent_id}: remote config revision {revision_id} poisoned ({reason}): "
        f"{detail} — nothing more will be delivered for it; push a new revision or "
        "roll back"
    )
    return audit.audit_event(
        "config.apply.failed",
        principal="system",
        actor_kind="system",
        result="error",
        target_kind="agent",
        target_id=agent_id,
        revision_id=revision_id,
        config_hash=record.get("config_hash"),
        error=detail,
        reason=reason,
    )


def note_status(
    agent_id: str,
    status_name: Optional[str],
    reported_hash_b64: Optional[str],
    error_message: str = "",
) -> Optional[Dict[str, Any]]:
    """React to a reported `RemoteConfigStatus`; returns an audit row if notable.

    Three cases matter (epic #93, T9):

    1. **`FAILED` for our revision** — poison it. Never retried (D6).
    2. **A different hash after we delivered ours** — the OpAMP Supervisor restores
       `last_working_remote_config.dat` when a new config fails, so an agent that
       reports "APPLIED, hash = something else" after we sent ours has auto-reverted.
       Re-pushing that revision is the infinite loop T9 describes. Poison it once the
       grace window has passed and we have had to deliver more than once; a single
       stale report during a restart is not an auto-revert.
    3. Anything else is just status — recorded, not punished. A failure the agent
       reports for a config we never sent is not ours to punish.
    """
    record = REGISTRY.get(agent_id)
    if record is None:
        return None
    ours = record["config_hash"]
    reported = hash_from_wire(reported_hash_b64)

    if status_name == STATUS_FAILED and reported == ours:
        detail = error_message or "agent reported FAILED without an error message"
        return _poison(agent_id, record, "failed", detail)

    delivered = bool(record.get("delivered_hash")) and record.get("delivered_hash") == ours
    mismatched = bool(reported) and reported != ours

    # The history lookup is inside the suspicious branch on purpose: it is a SQLite
    # read, and `note_status` runs on every status report (#109).
    if mismatched and delivered and reported in set(
        REGISTRY.store.hashes_for_agent(agent_id)
    ):
        # The agent is back on a config of ours that is not the current one.
        delivered_at = _delivered_at(record)
        waited = (
            delivered_at is None
            or (utcnow() - delivered_at).total_seconds() >= revert_grace_seconds()
        )
        attempts = int(record.get("attempts") or 0)
        if status_name == STATUS_FAILED or (waited and attempts >= 2):
            detail = (
                error_message
                or f"agent reverted to an earlier revision (reported {reported[:12]}…, "
                f"expected {ours[:12]}…)"
            )
            return _poison(agent_id, record, "auto_reverted", detail)

    REGISTRY.note_report(agent_id, status_name, error_message)
    return None


def short_status(name: Any) -> Optional[str]:
    """Normalise a status name to `UNSET|APPLIED|APPLYING|FAILED`.

    `MessageToDict` renders this proto's unscoped enum values with their full name
    (`RemoteConfigStatuses_APPLIED`), which is what `AgentState` stores. Comparisons
    have to be against the short form or a `FAILED` report silently never matches —
    and a failure that is never noticed is a re-push loop (T9).
    """
    if isinstance(name, int):
        name = STATUS_NAMES.get(name)
    if not isinstance(name, str) or not name:
        return None
    return name.rsplit("RemoteConfigStatuses_", 1)[-1].upper()


def parse_status(agent_dict: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], str]:
    """Pull `(status, hash_b64, error_message)` out of a `MessageToDict` report."""
    status = agent_dict.get("remoteConfigStatus") or {}
    if not isinstance(status, dict):
        return None, None, ""
    return (
        short_status(status.get("status")),
        status.get("lastRemoteConfigHash"),
        status.get("errorMessage") or "",
    )


def normalize_agent_id(agent_id: str) -> str:
    """Validate the hex `instance_uid` form used as an agent key everywhere."""
    cleaned = (agent_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", cleaned):
        raise ValueError(
            f"agent_id {agent_id!r} is not a 32-character hex instance UID"
        )
    return cleaned
