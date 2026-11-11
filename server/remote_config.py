"""Desired (remote) config: hashing, validation and the delivery decision (#81).

The protocol half of remote configuration, kept out of `server.main` so it can be
tested without an ASGI app and so the API, the CLI and the MCP server all describe
a config the same way.

**Hashing.** Two hashes, and the difference is the point of #136.

*Storage* hash (`compute_hash`): the server's own length-prefixed, key-sorted encoding
of the config map (SHA-256, hex). Length prefixes make it unambiguous — two different
maps cannot serialise to the same bytes — and the role component is appended only when
set, so a revision stored before #68 keeps its identity. It keys revisions, audit rows,
drift caching and the "is this the same config" question inside the server.

*Wire* hash (`spec_hash`): `sha256(AgentConfigMap.SerializeToString(deterministic=True))`,
which is what `opamp.proto` means by `AgentRemoteConfig.config_hash`, base64 on the wire.
It is **not** free choice: spec-following agents re-derive it, and a mismatch is a
failed delivery (`opamp-core` raises `RemoteAgentConfigHashMismatchError` before writing
the file). Our canonical encoding is not the protobuf serialisation, so sending it made
every verifying agent refuse the push while the server still showed its own hash — a
stalled rollout that looked delivered (#136). The old argument that the spec lets the
server pick any method is wrong for verifying agents; the hash is opaque only to those
that echo it back unexamined.

An agent may therefore report either hash: a spec-following one reports the wire hash,
and one still running against a pre-#136 push echoes the canonical bytes it was given.
Every comparison goes through `reported_matches()`, so both mean "the agent has our
config" and neither causes a re-push loop.

**Validation** is the pipeline in `server.config_validation` (#100): shape and size,
bounded parse, structural rules, protected invariants, inventory warnings and the
OPA policy gate. It is re-run at approve/apply time, because the schema, the
policies and an agent's component inventory can all move between Monday and Friday.

**Delivery.** A pure decision function, ``decision_for()``, so the HTTP and WebSocket
transports cannot diverge, plus two thin builders over it: ``offer_for()`` for the
reply path (the offer rides the agent's own response) and ``push_frame()`` /
``deliver_now()`` for the unsolicited "push now" path (#102). Both go through the same
gate — posture, capability, lifecycle, poisoned, reported-hash — so a push can never
deliver something a poll would refuse (epic #93, D5: desired state, not a command).
It offers only when push is allowed by the posture, the
agent advertises `AcceptsRemoteConfig`, the assignment points at a revision that is
`active` and not poisoned, and the agent's reported hash differs from ours. On top
of #81's hash comparison it now carries #99's lifecycle guards: a delivery counter
(an agent that never reports back is not re-pushed forever), an auto-revert detector
(T9: an agent that reports a *different* config after we delivered ours has restored
its last working config, so re-pushing is the infinite loop), and delivery
bookkeeping that survives a restart.

**When delivery is recorded** differs by transport, and that difference is the whole
point of #102: the reply path records at build time (the offer is in the same
response body, so "built" and "sent" are the same event), while the push path
records in an `on_sent` callback that only runs once the frame is actually written.
A queued-then-dropped frame therefore consumes no delivery attempt and is re-offered
by reconciliation on the next report, instead of being silently lost.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import prometheus_client as prom_client
from loguru import logger

from proto.opamp_pb2 import (
    AgentCapabilities,
    AgentConfigMap,
    AgentRemoteConfig,
    RemoteConfigStatuses,
    ServerCapabilities,
    ServerToAgent,
)
from server import audit, config_validation, crypto, posture, ws
from server.config_validation import ConfigValidationError  # noqa: F401  (re-export)
from server.framing import MessageTooBig, encode_message, message_limit
from server.state import AGENT_REGISTRY, SQLiteDesiredConfigStore, utcnow

HASH_ALGORITHM = "sha256"

# canonical hash -> spec hash, for `wire_hash()`. A map's canonical hash identifies it
# uniquely, so an entry cannot go stale; the size cap is only so a long-lived process
# with many revisions does not grow without bound.
_WIRE_HASH_CACHE: Dict[str, str] = {}

# revision_id -> spec hash, for `prior_accepted_hashes()` (T9 auto-revert detection).
_PRIOR_WIRE_HASHES: Dict[str, Optional[str]] = {}
HASH_DOMAIN = b"opamp-config-map-v1\x00"

# 1 MiB by default. A collector config is kilobytes; the cap exists so a typo
# cannot put a megabyte into every agent's response and into every SQLite write.
DEFAULT_MAX_BYTES = config_validation.DEFAULT_MAX_BYTES

# How many frames we will put on the wire for one assignment before deciding the
# agent is never going to acknowledge it. Not poisoning: the revision may be fine
# and the agent wedged, so this surfaces as `unconfirmed` and stops the loop.
DEFAULT_MAX_DELIVERY_ATTEMPTS = 5

# How long a spent delivery budget keeps blocking a re-push (#135). The budget stops
# a re-push loop, but "never acknowledged" is not permanent: an agent that restarts,
# or whose daemon lost the config, comes back reporting no hash at all, and refusing
# it forever is how a fleet ends up APPLIED-less with nothing to recover with.
# `0` restores the old "never retry" behaviour.
DEFAULT_UNCONFIRMED_RETRY_SECONDS = 300

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

# #102: how long "delivered" takes to become "applied", per delivery channel. A
# rollout that never reaches APPLIED is the failure operators actually care about,
# so the latency is measured from the write to the agent's own report — never
# inferred by the server.
OPAMP_CONFIG_DELIVERY_LATENCY = prom_client.Histogram(
    "opamp_config_delivery_latency_seconds",
    "Seconds from a config frame being delivered to the agent reporting APPLIED",
    ["channel"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900),
)


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


def unconfirmed_retry_seconds() -> int:
    """`CONFIG_UNCONFIRMED_RETRY_SECONDS` - how long a spent budget blocks a push.

    `0` is allowed and means "never give the budget back", which is what the server
    did before this existed. A non-zero value bounds the re-push loop the budget
    protects against (at most one extra budget per window) while letting an agent
    that lost its config - a process restart, a daemon that booted without it -
    recover without an operator creating a new revision.
    """
    raw = (os.environ.get("CONFIG_UNCONFIRMED_RETRY_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_UNCONFIRMED_RETRY_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            f"CONFIG_UNCONFIRMED_RETRY_SECONDS={raw!r} is not an integer; "
            f"using {DEFAULT_UNCONFIRMED_RETRY_SECONDS}"
        )
        return DEFAULT_UNCONFIRMED_RETRY_SECONDS
    if value < 0:
        logger.warning(
            f"CONFIG_UNCONFIRMED_RETRY_SECONDS={raw!r} must be >= 0; using 0 "
            "(never retry)"
        )
        return 0
    return value


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


def spec_hash(config_map: Dict[str, Dict[str, Any]]) -> str:
    """The spec's hash of this config map: SHA-256 over its deterministic protobuf
    serialisation, hex (#136).

    This is what goes on the wire. Deterministic serialisation pins field order and
    map ordering, so the same map always serialises to the same bytes — which is the
    only canonicalisation the spec relies on, and the one agents re-derive.
    """
    message = build_config_map(config_map)
    return hashlib.sha256(message.SerializeToString(deterministic=True)).hexdigest()


def wire_hash(config_map: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """`spec_hash`, memoised on the canonical hash (which identifies a map uniquely).

    Called from the message path — the delivery gate asks it on every inbound
    message — and a collector config is kilobytes, so this keeps a hash of a body
    that has not changed from costing a SHA-256 per heartbeat.
    """
    key = compute_hash(config_map)
    cached = _WIRE_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    value = spec_hash(config_map)
    if len(_WIRE_HASH_CACHE) >= 512:
        _WIRE_HASH_CACHE.clear()
    _WIRE_HASH_CACHE[key] = value
    return value


def accepted_hashes(
    config_map: Optional[Dict[str, Dict[str, Any]]], stored_hash: Optional[str]
) -> Tuple[str, ...]:
    """Every reported hash (our hex form) that means "the agent has this config".

    The wire hash, plus the canonical one for an agent still echoing a pre-#136 push,
    plus the stored hash when the body is not in hand (a rollout row without a live
    assignment). Empty when neither is known.
    """
    values: List[str] = []
    if stored_hash:
        values.append(str(stored_hash).lower())
    # `is not None`, not truthiness: a withdrawn revision is an *empty* config map and
    # its spec hash is a real hash the agent reports back (#136).
    if config_map is not None:
        try:
            values.append(wire_hash(config_map) or "")
        except Exception:  # a body we cannot serialise is not a hash we can accept
            pass
    return tuple(dict.fromkeys(value for value in values if value))


def reported_matches(
    reported: Optional[str],
    config_map: Optional[Dict[str, Dict[str, Any]]],
    stored_hash: Optional[str],
) -> bool:
    """Does this reported hash mean the agent is running the config we want?"""
    if not reported:
        return False
    return str(reported).lower() in accepted_hashes(config_map, stored_hash)


def prior_accepted_hashes(agent_id: str, limit: int = 20) -> set:
    """Every hash that means "one of our configs" for this agent (T9 detection).

    Canonical hashes are one query. The spec hashes of *previous* revisions need their
    bodies, so each is decoded once and memoised on `revision_id` — the revision's
    hash is a pure function of it, so the entry cannot go stale. Called only in
    `note_status`'s suspicious branch: a status report that matches settles earlier.
    """
    accepted = set(REGISTRY.store.hashes_for_agent(agent_id))
    for row in REGISTRY.store.history(agent_id, limit=limit):
        revision_id = row.get("revision_id")
        if not revision_id:
            continue
        if revision_id in _PRIOR_WIRE_HASHES:
            cached = _PRIOR_WIRE_HASHES[revision_id]
        else:
            try:
                config_map = REGISTRY.decode_revision(revision_id)
            except Exception:  # unreadable body: canonical membership still applies
                config_map = None
            cached = wire_hash(config_map) if config_map is not None else None
            _PRIOR_WIRE_HASHES[revision_id] = cached
        if cached:
            accepted.add(cached)
    return accepted


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
        """The assignment/delivery facts the API and the drift check report (#99).

        Read from the in-memory assignment (the message path must not hit SQLite,
        #109); `delivery_view()` is the same shape for callers that already have the
        row, so a listing and a detail view cannot disagree.
        """
        return delivery_view(self._cache.get(agent_id), reported_hash(agent_id))

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
        held: bool = False,
        group_id: Optional[str] = None,
        group_snapshot: Optional[Dict[str, Any]] = None,
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
            group_id=group_id,
            group_snapshot_json=(
                json.dumps(group_snapshot, sort_keys=True) if group_snapshot else None
            ),
            make_current=make_current,
            held=held,
        )
        if make_current:
            decoded = self._decode(record)
            if decoded is not None:
                self._cache[agent_id] = decoded
        return record

    def activate(
        self, agent_id: str, revision_id: str, held: bool = False
    ) -> None:
        """Approve-time assignment, refreshing the cache without re-reading SQLite.

        `held` is #101's rollout hold: the revision becomes the agent's desired config
        but is not deliverable until its batch is promoted. It is written in the same
        statement as the assignment, so there is no window in which a status report
        could collect a config the rollout has not cleared.
        """
        self._store.assign(agent_id, revision_id, held=held)
        record = self._store.current(agent_id)
        decoded = self._decode(record)
        if decoded is not None:
            self._cache[agent_id] = decoded

    def set_held(self, agent_id: str, held: bool) -> bool:
        """Release (or re-apply) the rollout hold, cache and store (#101)."""
        changed = self._store.set_held(agent_id, held)
        record = self._cache.get(agent_id)
        if record is not None:
            record["held"] = bool(held)
        return changed

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

    def record_delivery(
        self, agent_id: str, revision_id: str, channel: Optional[str] = None
    ) -> int:
        """One frame was put on the wire; returns the assignment's attempt count.

        `channel` is `push` (unsolicited WebSocket frame) or `reply` (carried on the
        agent's own request — an HTTP poll or a WS message/reply). It is stored so
        the API can say *how* the config arrived instead of pretending a poll is
        instant (#102).
        """
        attempts = self._store.record_delivery(agent_id, revision_id, channel)
        record = self._cache.get(agent_id)
        if record is not None and record.get("revision_id") == revision_id:
            record["attempts"] = attempts
            record["delivered_hash"] = record.get("config_hash")
            record["delivered_at"] = utcnow().isoformat()
            record["delivery_channel"] = channel
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

    def reset_delivery_budget(self, agent_id: str) -> None:
        """Give an assignment a fresh delivery budget, store and cache together.

        Called when there is evidence the previous budget no longer describes the
        situation: the agent acknowledged the config (:func:`note_status`), or it has
        been unacknowledged long enough that re-delivering is the only way forward
        (:func:`decision_for`).
        """
        self._store.reset_delivery_budget(agent_id)
        record = self._cache.get(agent_id)
        if record is not None:
            record["attempts"] = 0
            record["unconfirmed"] = False
            record["last_error"] = None

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
    * the revision is **held** by a staged rollout whose batch is not promoted yet
      (#101) - the assignment exists, delivery does not;
    * the revision is poisoned, by a `FAILED` report or an auto-revert (D6);
    * the agent never acknowledged the last delivery (`unconfirmed`), so re-pushing
      is noise, not recovery;
    * the revision body is gone (retention pruned a row that is somehow current) -
      delivering an empty map would silently wipe the agent's config.
    """
    if not record:
        return False
    if record.get("status") != SQLiteDesiredConfigStore.STATUS_ACTIVE:
        return False
    if record.get("held"):
        return False
    if record.get("failed_at") or record.get("poisoned") or record.get("unconfirmed"):
        return False
    return True


# Every ServerCapabilities bit in proto/opamp.proto, classified (#74). The
# capability test fails when a proto bump adds a value none of these maps mentions,
# which is the durable guard: silently dropping a bit is a symptom, an unclassified
# bit is what over-claims to every agent. A set bit is a promise, and a supervisor
# that sees one is entitled to wait for behaviour that may never arrive.

# Backed by code today, so advertised unconditionally.
ALWAYS_ADVERTISED_CAPABILITIES = frozenset(
    {
        # _apply_status_report() persists health, description, effective config,
        # remote-config status and package statuses.
        ServerCapabilities.ServerCapabilities_AcceptsStatus,
        # `effective_config` is stored on AgentState and served by /agent/{id}.
        ServerCapabilities.ServerCapabilities_AcceptsEffectiveConfig,
        # `package_statuses` is persisted in the agent row (state.py).
        ServerCapabilities.ServerCapabilities_AcceptsPackagesStatus,
    }
)

# Backed by code, but only advertised in the posture that can honour it.
CONDITIONAL_CAPABILITIES = {
    # Delivered by #81/#102, allowed only by posture.push_allowed() (#96).
    ServerCapabilities.ServerCapabilities_OffersRemoteConfig: (
        "advertised only when posture.push_allowed()"
    ),
}

# No implementation yet. Each bit names the issue that turns it on, so re-adding
# one is a deliberate, reviewable change rather than a drive-by bitmask edit.
UNIMPLEMENTED_CAPABILITIES = {
    # #82 closed as not planned, so this one is not coming (docs/spec-conformance.md,
    # §Packages): upstream's `opampextension` registers no package callbacks, so no
    # stock agent would act on the offer, and serving binaries is deploy tooling's
    # job - we ship the OCB manifest instead. Kept in this bucket rather than
    # deleted because the bit still exists in the proto.
    ServerCapabilities.ServerCapabilities_OffersPackages: "#82 (closed, not planned)",
    ServerCapabilities.ServerCapabilities_OffersConnectionSettings: "#83",
    ServerCapabilities.ServerCapabilities_AcceptsConnectionSettingsRequest: "#83",
}

# Proto sentinels that are never advertised.
RESERVED_CAPABILITIES = {
    ServerCapabilities.ServerCapabilities_Unspecified: "sentinel; never advertised",
}

CLASSIFIED_CAPABILITIES = frozenset(
    ALWAYS_ADVERTISED_CAPABILITIES
    | CONDITIONAL_CAPABILITIES.keys()
    | UNIMPLEMENTED_CAPABILITIES.keys()
    | RESERVED_CAPABILITIES.keys()
)


def server_capabilities() -> int:
    """The `ServerToAgent.capabilities` advertisement (#74, used by #102).

    Shared by the reply path and the unsolicited push frame so an agent never sees a
    different server depending on who spoke first. `OffersRemoteConfig` is only
    advertised when the server may actually deliver a config (#96): a capability bit
    is a promise, and a supervisor that sees it waits for a config that never comes.

    `OffersConnectionSettings` and `AcceptsConnectionSettingsRequest` are deliberately
    absent until #83 implements CSR/TOFU — a supervisor would otherwise block waiting
    for an offer this server never sends.
    """
    capabilities = 0
    for bit in ALWAYS_ADVERTISED_CAPABILITIES:
        capabilities |= bit
    if posture.push_allowed():
        capabilities |= ServerCapabilities.ServerCapabilities_OffersRemoteConfig
    return capabilities


@dataclass(frozen=True)
class OfferDecision:
    """The answer to "should this agent be sent its config now?", with no side effects.

    `reason` is set exactly when `deliver` is False, and it is what the push API
    reports instead of a bare "nothing happened": "your agent already has it" and
    "the revision is poisoned" are different answers to the same question.
    """

    deliver: bool
    revision_id: Optional[str] = None
    config_hash: Optional[str] = None
    config_map: Mapping[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None


# Why a delivery did not happen, in the caller's terms. Kept next to the decision so
# the API, the CLI and the UI cannot invent different words for the same state.
REASON_DETAILS = {
    "push_disabled": "config push is disabled by the security posture",
    "agent_does_not_accept": "the agent does not advertise AcceptsRemoteConfig",
    "nothing_assigned": "no desired config is assigned to this agent",
    "revision_not_deliverable": "the current revision is not active and deliverable",
    "revision_has_no_body": "the revision body is unavailable (pruned or undecryptable)",
    "reported_hash_matches": "the agent already reports this config hash",
    "held_by_rollout": (
        "a staged rollout is holding this config until the agent's batch is promoted"
    ),
    "frame_too_big": "the push frame would exceed OPAMP_MAX_MESSAGE_BYTES",
    "attempts_exhausted": (
        "the delivery budget is exhausted (CONFIG_MAX_DELIVERY_ATTEMPTS); the agent "
        "never acknowledged it"
    ),
}


def _unconfirmed_budget_expired(record: Mapping[str, Any]) -> bool:
    """Has the spent delivery budget stopped describing the situation? (#135)

    Measured from the last delivery, because that is when the agent last had a
    chance to apply it. An assignment marked unconfirmed with no delivery timestamp
    has no evidence behind the flag at all, so it is treated as expired: refusing to
    deliver on a record that cannot say when we delivered is how an agent stays
    configless with no way back.
    """
    limit = unconfirmed_retry_seconds()
    if limit <= 0:
        return False
    delivered = _delivered_at(record)
    if delivered is None:
        return True
    return (utcnow() - delivered).total_seconds() >= limit


def _retry_unconfirmed(agent_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Reset an expired budget so this delivery is attempted, not refused.

    One reset per window, so the budget still bounds a re-push loop: an agent that
    keeps failing to acknowledge gets one new budget per
    `CONFIG_UNCONFIRMED_RETRY_SECONDS`, not one per heartbeat.
    """
    if not record.get("unconfirmed"):
        return record
    if not _unconfirmed_budget_expired(record):
        return record
    attempts = int(record.get("attempts") or 0)
    logger.bind(
        event="config.delivery.retry_unconfirmed",
        agent_id=agent_id,
        attempts=attempts,
        revision_id=record.get("revision_id"),
    ).warning(
        f"agent {agent_id}: delivery of {record.get('revision_id')} was unconfirmed "
        f"after {attempts} attempt(s) and the agent still has no config; granting a "
        f"new budget (CONFIG_UNCONFIRMED_RETRY_SECONDS="
        f"{unconfirmed_retry_seconds()})"
    )
    REGISTRY.reset_delivery_budget(agent_id)
    refreshed = REGISTRY.get(agent_id)
    return refreshed if refreshed is not None else record


def decision_for(
    agent_id: str,
    capabilities: int,
    status: Optional[Dict[str, Any]] = None,
) -> OfferDecision:
    """Should this agent be offered its desired config right now? (pure, #81/#99/#102)

    Conditions, in order, each with a reason the caller can report:

    1. config push is allowed by the posture (#96) — a capability bit is a promise;
    2. the agent advertises `AcceptsRemoteConfig` — the spec's MUST;
    3. the assignment is deliverable (see :func:`deliverable`);
    4. the revision has a body — delivering an empty map would wipe the agent;
    5. the agent's reported hash differs from ours — the spec's MUST, and what stops
       a server from re-pushing a config the agent already has. "Ours" is a set of
       two (#136): the wire hash a verifying agent re-derives, and the canonical one
       an agent holding a pre-#136 push echoes back;
    6. the delivery budget is not exhausted — an agent that never reports back is
       surfaced as `unconfirmed` instead of being re-pushed forever.

    The budget is per episode, not permanent (#135): an agent that acknowledges
    clears it, and one that has been unconfirmed for
    `CONFIG_UNCONFIRMED_RETRY_SECONDS` gets a fresh one. Without that, an agent whose
    process restarted (and so reports no hash at all) could never be given its config
    again - that is a server refusing to reconcile, not a fleet that is converged.

    The one write this function can cause (`mark_unconfirmed`, when the budget runs
    out) is kept here rather than in a builder so both paths surface it identically.
    """
    if not posture.push_allowed():
        return OfferDecision(False, reason="push_disabled")
    if not capabilities & AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig:
        return OfferDecision(False, reason="agent_does_not_accept")

    record = REGISTRY.get(agent_id)
    if not record:
        return OfferDecision(False, reason="nothing_assigned")
    # #135: an expired budget is not a refusal, it is a stale fact. Reset it before
    # `deliverable()` sees it, or the agent stays configless forever.
    record = _retry_unconfirmed(agent_id, record)
    revision_id = record.get("revision_id")
    config_hash = record.get("config_hash")
    if not deliverable(record):
        return OfferDecision(False, revision_id=revision_id, config_hash=config_hash,
                             reason="held_by_rollout" if record.get("held")
                             else "revision_not_deliverable")
    if not record.get("config_map") and not record.get("withdrawn"):
        logger.warning(
            f"agent {agent_id}: revision {revision_id} has no decoded body; "
            "nothing to deliver"
        )
        return OfferDecision(False, revision_id=revision_id, config_hash=config_hash,
                             reason="revision_has_no_body")

    reported = hash_from_wire((status or {}).get("lastRemoteConfigHash"))
    if reported_matches(reported, record.get("config_map"), config_hash):
        return OfferDecision(False, revision_id=revision_id, config_hash=config_hash,
                             reason="reported_hash_matches")

    attempts = int(record.get("attempts") or 0)
    limit = max_delivery_attempts()
    if attempts >= limit:
        detail = (
            f"delivered {attempts} times and the agent never reported hash "
            f"{str(config_hash)[:12]}…; delivery stopped"
        )
        REGISTRY.mark_unconfirmed(agent_id, detail)
        logger.warning(f"agent {agent_id}: {detail}")
        return OfferDecision(False, revision_id=revision_id, config_hash=config_hash,
                             reason="attempts_exhausted")

    return OfferDecision(
        True,
        revision_id=revision_id,
        config_hash=config_hash,
        config_map=record.get("config_map") or {},
    )


def offer_for(
    agent_id: str,
    capabilities: int,
    status: Optional[Dict[str, Any]] = None,
) -> Optional[AgentRemoteConfig]:
    """Build the `remote_config` offer for one message, or None (#81, #99).

    The **reply** path: the offer is put in the response the agent is waiting for, so
    building it and sending it are the same event and the delivery is recorded here.
    A restart cannot double-deliver because `delivered_hash` is persisted, and a
    re-delivery is only justified by a hash mismatch, not by a lost in-memory flag.

    For the unsolicited path see :func:`push_frame`, which defers the same
    bookkeeping to the moment the frame is actually written (#102).
    """
    decision = decision_for(agent_id, capabilities, status)
    if not decision.deliver:
        return None

    offer = AgentRemoteConfig()
    offer.config.CopyFrom(build_config_map(dict(decision.config_map)))
    # #136: the spec's hash, not ours. A verifying agent re-derives this from the map
    # above and refuses the push when it disagrees; the canonical hash stays the
    # storage/audit identity and is still accepted if an agent echoes it (pre-#136).
    offer.config_hash = hash_bytes(
        wire_hash(dict(decision.config_map)) or decision.config_hash or ""
    )
    REGISTRY.record_delivery(agent_id, decision.revision_id or "", channel=ws.CHANNEL_REPLY)
    return offer


def push_frame(
    agent_id: str,
    *,
    capabilities: Optional[int] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[bytes], OfferDecision]:
    """Build the unsolicited `ServerToAgent` a "push now" would send (#102).

    Deliberately the same `decision_for()` gate as the reply path (D5), with two
    differences that only exist because nobody asked for this frame: the reply's
    `ReportFullState` flag is absent (it answers a message we did not receive), and
    the capabilities are the full advertisement, so an agent that has only ever seen
    pushes still learns what the server can do.

    Returns `(None, decision)` when there is nothing to send, so the caller can report
    *why* rather than guessing.
    """
    state = AGENT_REGISTRY.get(agent_id)
    if capabilities is None:
        capabilities = int(getattr(state, "capabilities", 0) or 0)
    if status is None:
        status = reported_status(agent_id)

    decision = decision_for(agent_id, capabilities, status)
    if not decision.deliver:
        return None, decision

    response = ServerToAgent()
    response.instance_uid = bytes.fromhex(agent_id)
    response.capabilities = server_capabilities()
    response.remote_config.config.CopyFrom(build_config_map(dict(decision.config_map)))
    response.remote_config.config_hash = hash_bytes(
        wire_hash(dict(decision.config_map)) or decision.config_hash or ""
    )
    # Last line of defence (#96/#102): nothing may put remote_config on the wire
    # while the posture says config push is not allowed.
    if posture.scrub_remote_config(response) or not response.HasField("remote_config"):
        return None, OfferDecision(False, reason="push_disabled")
    try:
        # Framed like every other outbound frame (varint header + protobuf). Returning
        # a raw message here would put an unframed frame on the wire — the transport
        # writes `payload` verbatim.
        frame = encode_message(response.SerializeToString(), max_bytes=message_limit())
    except MessageTooBig as exc:
        logger.error(f"agent {agent_id}: refusing to push an oversized frame: {exc}")
        return None, OfferDecision(False, reason="frame_too_big")
    return frame, decision


@dataclass(frozen=True)
class DeliveryResult:
    """What happened to one "deliver now" request (#102).

    Three states, and the difference between the first two is the whole point:
    `delivered` means the frame was handed to a live connection, `queued` means there
    was nowhere to put it and the agent will pick it up on its next report, and
    `unchanged` means nothing was deliverable at all (`reason` says why). Never
    `applied`: that word belongs to the agent.
    """

    state: str
    revision_id: Optional[str] = None
    config_hash: Optional[str] = None
    channel: Optional[str] = None
    reason: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "revision_id": self.revision_id,
            "config_hash": self.config_hash,
            "channel": self.channel,
            "reason": self.reason,
            "detail": self.detail,
        }


def _queued_detail(agent_id: str) -> str:
    """Say when it will actually arrive, using the agent's own observed cadence."""
    state = AGENT_REGISTRY.get(agent_id)
    interval = getattr(state, "report_interval", None)
    if not interval:
        return (
            "queued — no live connection; delivered on the agent's next status report"
        )
    return (
        "queued — no live connection; delivered within one polling interval "
        f"({float(interval):.0f}s observed)"
    )


def deliver_now(agent_id: str, principal: Any = None) -> DeliveryResult:
    """Deliver the agent's current active revision over its live connection (#102).

    This is a latency optimisation, not a second delivery model (D5): the desired
    state is already stored, this only decides *when* it arrives. Nothing here can
    resurrect a poisoned revision, bypass the posture, or exceed the delivery budget,
    because the frame comes from :func:`push_frame` — the same gate a poll goes
    through.

    Delivery is recorded in `on_sent`, so a frame that is dropped, superseded or
    never written leaves `delivered_at` unset and reconciliation re-offers it.

    `principal` is the admin identity that asked for the delivery, used only to
    attribute a refusal to the person who caused it (T1: a socket whose binding
    moved is either a reconnect the registry has not caught up with or an attempt to
    receive someone else's config — both deserve a record naming the requester).
    """
    frame, decision = push_frame(agent_id)
    revision_id = decision.revision_id
    config_hash = decision.config_hash
    if frame is None:
        reason = decision.reason or "nothing_to_deliver"
        return DeliveryResult(
            state="unchanged",
            revision_id=revision_id,
            config_hash=config_hash,
            reason=reason,
            detail=REASON_DETAILS.get(reason, reason),
        )

    def _on_sent() -> None:
        # Only the writer calls this, and only after `send_bytes()` returned.
        REGISTRY.record_delivery(agent_id, revision_id or "", channel=ws.CHANNEL_PUSH)

    uid = bytes.fromhex(agent_id)
    blocker = ws.push_blocker(uid)
    if blocker == ws.BLOCKER_FOREIGN_PRINCIPAL:
        # The socket holding this uid authenticated as somebody else (#94). Refusing
        # is the security answer; it is still *queued*, because the agent's own next
        # report will collect the config, so the operator sees a delay, not a lie.
        audit.audit_event(
            "config.deliver",
            principal=getattr(principal, "name", None) or "system",
            role=getattr(principal, "role", None),
            actor_kind="human" if principal is not None else "system",
            result="error",
            target_kind="agent",
            target_id=agent_id,
            revision_id=revision_id,
            config_hash=config_hash,
            error="refused: instance_uid is bound to a different principal",
            reason="foreign_principal",
        )
        return DeliveryResult(
            state="queued",
            revision_id=revision_id,
            config_hash=config_hash,
            reason="foreign_principal",
            detail=(
                "refused — the instance_uid is bound to a different principal; the "
                "revocation or the reconnect will resolve it"
            ),
        )
    try:
        sent = ws.send_to_agent_threadsafe(
            uid, frame, kind=ws.KIND_CONFIG, on_sent=_on_sent
        )
    except RuntimeError as exc:
        # Called from the event loop (a rollout engine tick, #101): fall back to the
        # poll path rather than blocking the loop on itself. Logged, never silent.
        logger.bind(event="config.push.inline_fallback", agent_id=agent_id).warning(
            f"cannot push to {agent_id} inline: {exc}"
        )
        sent = False

    if sent:
        return DeliveryResult(
            state="delivered",
            revision_id=revision_id,
            config_hash=config_hash,
            channel=ws.CHANNEL_PUSH,
            detail="sent over the agent's live WebSocket connection",
        )
    return DeliveryResult(
        state="queued",
        revision_id=revision_id,
        config_hash=config_hash,
        reason="no_live_connection",
        detail=_queued_detail(agent_id),
    )


def deliver_many(agent_ids: Sequence[str], principal: Any = None) -> Dict[str, Any]:
    """`deliver_now()` for a set of agents, rolled up into one honest summary.

    `state` is `delivered` only when **every** target was reached live, `queued` when
    none was, `partial` in between — a fleet push that reached half the fleet must not
    read as success. The counts stay visible because "18 delivered, 2 queued" is the
    sentence a rollout UI needs.
    """
    results = [deliver_now(agent_id, principal) for agent_id in agent_ids]
    counts = {"delivered": 0, "queued": 0, "unchanged": 0}
    for result in results:
        counts[result.state] = counts.get(result.state, 0) + 1
    if counts["delivered"] and not counts["queued"]:
        state = "delivered"
    elif counts["queued"] and not counts["delivered"]:
        state = "queued"
    elif counts["delivered"]:
        state = "partial"
    else:
        state = "unchanged"
    return {
        "state": state,
        "revision_id": results[0].revision_id if len(results) == 1 else None,
        "channel": ws.CHANNEL_PUSH if counts["delivered"] else None,
        "agents": counts,
        "total": len(results),
        "detail": (
            f"{counts['delivered']} delivered over a live connection, "
            f"{counts['queued']} queued for the next status report, "
            f"{counts['unchanged']} with nothing to deliver"
        ),
    }


_HEX_HASH = re.compile(r"^[0-9a-f]{64}$")


def reported_status(agent_id: str) -> Dict[str, Any]:
    """The agent's last `RemoteConfigStatus` hash in **wire (base64)** form.

    The push path has no request body to quote, so it reconstructs the shape
    `MessageToDict` produces for a real report. Hex and base64 are not
    interchangeable here: `decision_for()` decodes the wire form, and handing it our
    own hex made every "the agent already has this" answer wrong.
    """
    raw = reported_hash(agent_id)
    if not raw:
        return {"lastRemoteConfigHash": None}
    try:
        return {"lastRemoteConfigHash": hash_b64(raw)}
    except ValueError:
        # A stored hash we cannot read is not a hash the agent reported: treat it as
        # unknown rather than raising on the delivery path.
        logger.warning(f"agent {agent_id}: stored remote config hash is malformed")
        return {"lastRemoteConfigHash": None}


def reported_hash(agent_id: str) -> Optional[str]:
    """The hash the agent last reported, in our hex form (`None` if unknown).

    Read from `AgentState` rather than SQLite: it is already in memory, and this is
    called from read paths (`GET /agent/{id}/config`) that must not pay for a query.

    `AgentState.last_remote_config_hash` is stored **hex** (`_apply_status_report`
    converts it on the way in), while the wire and `MessageToDict` use base64 — so
    this accepts either instead of assuming, because getting it wrong makes every
    agent look like it never applied anything.
    """
    state = AGENT_REGISTRY.get(agent_id)
    raw = getattr(state, "last_remote_config_hash", None)
    if not raw:
        return None
    return raw.lower() if _HEX_HASH.match(str(raw).lower()) else hash_from_wire(raw)


def assignment_state(
    record: Optional[Dict[str, Any]], reported: Optional[str] = None
) -> str:
    """One word for "where is this assignment?" (#102).

    `applied` is only ever the agent's word, matched against **our** hash: the agent
    having `APPLIED` an *older* config is not "applied". Everything else is derived
    from the delivery facts, so a UI can say "queued" instead of pretending a push
    was instant, and "delivered" instead of claiming it worked.
    """
    if not record:
        return "none"
    ours = record.get("config_hash")
    status = record.get("last_status")
    if reported and reported == ours and status != STATUS_FAILED:
        return "applied"
    if (
        record.get("poisoned")
        or record.get("failed_at")
        or record.get("status") == SQLiteDesiredConfigStore.STATUS_POISONED
    ):
        return "poisoned"
    if record.get("unconfirmed"):
        return "unconfirmed"
    if status == STATUS_FAILED:
        return "failed"
    if record.get("held"):
        # #101: assigned, approved, deliberately not deliverable yet. Before the
        # poisoned/failed checks this would mask a poisoned assignment, so it sits
        # after them; before `delivered`/`queued` because nothing was sent.
        return "held"
    if not deliverable(record):
        return record.get("status") or "undeliverable"
    if record.get("delivered_hash") and record.get("delivered_hash") == ours:
        return "delivered"
    return "queued"


def delivery_view(
    record: Optional[Dict[str, Any]], reported: Optional[str] = None
) -> Dict[str, Any]:
    """Delivery facts plus the derived `state` for one assignment row (#99, #102).

    Takes a row rather than an agent id so both sources can use it — the in-memory
    cache (`DesiredConfigRegistry.delivery_record`, no SQLite on the message path)
    and an authoritative SQL read (proposal listings, which must not show this
    process's stale cache). `state` is **derived, never stored**: `applied` is the
    agent's own report and nothing else, so the server cannot claim a config took
    effect.
    """
    record = record or {}
    return {
        "delivered_hash": record.get("delivered_hash"),
        "delivered_at": record.get("delivered_at"),
        "channel": record.get("delivery_channel"),
        "state": assignment_state(record, reported),
        "last_status": record.get("last_status"),
        "last_error": record.get("last_error"),
        "attempts": int(record.get("attempts") or 0),
        "poisoned": bool(record.get("poisoned")),
        "unconfirmed": bool(record.get("unconfirmed")),
        "held": bool(record.get("held")),
    }


def _observe_delivery_latency(record: Dict[str, Any]) -> None:
    """Record delivered → APPLIED once per revision (#102)."""
    delivered_at = _delivered_at(record)
    if delivered_at is None:
        return
    seconds = max(0.0, (utcnow() - delivered_at).total_seconds())
    channel = record.get("delivery_channel") or "unknown"
    OPAMP_CONFIG_DELIVERY_LATENCY.labels(channel=channel).observe(seconds)


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
    # #136: the agent may report the wire (spec) hash or, if it is still echoing a
    # pre-#136 push, our canonical one. Both mean "this is our config".
    matches_ours = reported_matches(reported, record.get("config_map"), ours)

    # #102: delivered → APPLIED, measured once per revision on the transition. A
    # heartbeat repeating APPLIED must not keep observing the same latency.
    if (
        status_name == STATUS_APPLIED
        and matches_ours
        and record.get("last_status") != STATUS_APPLIED
    ):
        _observe_delivery_latency(record)

    if status_name == STATUS_FAILED and matches_ours:
        detail = error_message or "agent reported FAILED without an error message"
        return _poison(agent_id, record, "failed", detail)

    delivered = bool(record.get("delivered_hash")) and record.get("delivered_hash") == ours
    mismatched = bool(reported) and not matches_ours

    # The history lookup is inside the suspicious branch on purpose: it is a SQLite
    # read, and `note_status` runs on every status report (#109).
    if mismatched and delivered and reported in prior_accepted_hashes(agent_id):
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

    # #135: the agent has our config, so "never acknowledged" is no longer true.
    # Clear the spent budget (and the error message describing it) here rather than
    # waiting for a new revision: the recovery path must not be "propose again".
    if status_name == STATUS_APPLIED and matches_ours:
        if record.get("unconfirmed") or int(record.get("attempts") or 0):
            REGISTRY.reset_delivery_budget(agent_id)

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
