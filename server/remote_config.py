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

**Validation.** Deliberately minimal — structural checks only, and the seam for
#100's full pipeline (protected invariants, OPA). The two checks that earn their
place today are the ones whose failure mode is a dead collector:

* a body that claims to be YAML and does not parse leaves the collector down until
  someone restarts it by hand (upstream says so explicitly), and
* a **redacted** body must be impossible to push (#98). Pushing one silently wipes
  every credential in the fleet, and it is a one-click mistake in a UI.

**Delivery.** A pure decision function, ``offer_for()``, so the HTTP and WebSocket
transports cannot diverge: offer iff push is allowed, the agent advertises
`AcceptsRemoteConfig`, a revision exists, the revision is not poisoned, and the
agent's reported hash differs from ours. That last clause is the spec's MUST and
also the thing that prevents a re-push loop.

Nothing here writes to SQLite on the message path: `DesiredConfigRegistry` keeps the
decoded current revision in memory (loaded at startup, updated on push) because
#109 measured what a per-message write costs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import uuid
from typing import Any, Dict, List, Optional, Tuple

import yaml
from loguru import logger

from proto.opamp_pb2 import (
    AgentCapabilities,
    AgentConfigMap,
    AgentRemoteConfig,
    RemoteConfigStatuses,
)
from server import audit, crypto, posture, redaction
from server.state import SQLiteDesiredConfigStore, utcnow

HASH_ALGORITHM = "sha256"
HASH_DOMAIN = b"opamp-config-map-v1\x00"

# 1 MiB by default. A collector config is kilobytes; the cap exists so a typo
# cannot put a megabyte into every agent's response and into every SQLite write.
DEFAULT_MAX_BYTES = 1024 * 1024

# Alias/anchor guard. `yaml.safe_load` expands aliases, so a small document can
# still ask for gigabytes ("billion laughs"). #100 owns real limits; this refuses
# the obvious shape today.
MAX_ANCHORS = 500

STATUS_NAMES = {
    RemoteConfigStatuses.RemoteConfigStatuses_UNSET: "UNSET",
    RemoteConfigStatuses.RemoteConfigStatuses_APPLIED: "APPLIED",
    RemoteConfigStatuses.RemoteConfigStatuses_APPLYING: "APPLYING",
    RemoteConfigStatuses.RemoteConfigStatuses_FAILED: "FAILED",
}

STATUS_FAILED = "FAILED"
STATUS_APPLIED = "APPLIED"

_REVISION_PREFIX = "rev"


class ConfigValidationError(ValueError):
    """The payload is not a config map we are willing to store or deliver."""

    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


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
    return _env_int("CONFIG_MAX_BYTES", DEFAULT_MAX_BYTES)


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
        for part in (key.encode("utf-8"), (entry.get("content_type") or "").encode("utf-8"), body):
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


def _parse_bodies(config_map: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Return `(errors, warnings)` from parsing each body as its declared type."""
    errors: List[str] = []
    warnings: List[str] = []
    for key, entry in config_map.items():
        body = entry.get("body", "")
        if not isinstance(body, str):
            continue
        content_type = (entry.get("content_type") or "").lower()
        looks_yaml = "yaml" in content_type or "yml" in content_type
        looks_json = "json" in content_type
        if not (looks_yaml or looks_json):
            # Undeclared type: try anyway, but a failure is only a warning. A body
            # that is not a config fragment at all (a PEM certificate, a shell
            # script) must not be rejected because it is not YAML.
            looks_yaml = True
        if looks_yaml or looks_json:
            anchors = body.count("&") + body.count("*")
            if anchors > MAX_ANCHORS:
                errors.append(
                    f"{key or '(default)'}: body contains {anchors} YAML anchors/aliases, "
                    f"over the limit of {MAX_ANCHORS} (possible alias bomb)"
                )
                continue
            try:
                yaml.safe_load(body)
            except yaml.YAMLError as exc:
                message = f"{key or '(default)'}: body is not valid {content_type or 'YAML'}: {exc}"
                # An *undeclared* type is advisory: the body may legitimately not be
                # YAML at all. A declared `text/yaml` that does not parse is an error,
                # because the collector will stay down until someone restarts it.
                if looks_yaml and "yaml" in content_type:
                    errors.append(message)
                else:
                    warnings.append(message)
    return errors, warnings


def validate_config_map(
    config_map: Any,
    allow_empty: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Validate a `{key: {body, content_type}}` payload. Returns `(normalised, warnings)`.

    Raises `ConfigValidationError` naming **every** problem at once — an operator
    fixing one error per request is how a config push takes an afternoon.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(config_map, dict):
        raise ConfigValidationError(["config_map must be an object mapping names to config bodies"])
    if not config_map and not allow_empty:
        raise ConfigValidationError(
            ["config_map is empty: use DELETE to withdraw a config, not an empty push"]
        )

    normalised: Dict[str, Dict[str, Any]] = {}
    total = 0
    for key, entry in config_map.items():
        label = key or "(default)"
        if not isinstance(key, str):
            errors.append(f"{label}: config map keys must be strings")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{label}: expected an object with a 'body' field")
            continue
        body = entry.get("body")
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        if not isinstance(body, str):
            errors.append(f"{label}: 'body' is required and must be a string")
            continue
        content_type = entry.get("content_type") or ""
        if not isinstance(content_type, str):
            errors.append(f"{label}: 'content_type' must be a string")
            continue
        if "role" in entry:
            # The v0.20.0 `AgentConfigObject.role` field is not in this repo's proto
            # yet (#66). Silently dropping it would make a config look accepted but
            # deliver something else.
            errors.append(
                f"{label}: 'role' is not supported by this build's protocol version; "
                "it arrives with the v0.20.0 proto sync (#66)"
            )
            continue
        total += len(body.encode("utf-8"))
        normalised[key] = {"body": body, "content_type": content_type}

    if total > max_bytes():
        errors.append(
            f"config map is {total} bytes, over CONFIG_MAX_BYTES={max_bytes()}"
        )

    # #98: the most dangerous foot-gun in the whole feature.
    if redaction.contains_redaction(config_map):
        errors.append(
            "config body contains a redaction marker ('<redacted>' or the #98 marker): "
            "a redacted document is a display artefact and pushing it would wipe "
            "credentials across the fleet. Submit the real body."
        )

    if errors:
        raise ConfigValidationError(errors)

    parse_errors, parse_warnings = _parse_bodies(normalised)
    if parse_errors:
        raise ConfigValidationError(parse_errors)
    warnings.extend(parse_warnings)
    return normalised, warnings


# -------------------------------------------------------------------- protobuf


def build_config_map(config_map: Dict[str, Dict[str, Any]]) -> AgentConfigMap:
    message = AgentConfigMap()
    for key, entry in config_map.items():
        message.config_map[key].body = (entry.get("body") or "").encode("utf-8")
        message.config_map[key].content_type = entry.get("content_type") or ""
    return message


def to_wire_config_map(config_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """JSON shape of a config map for API responses (bodies decoded to text)."""
    return {
        key: {
            "body": entry.get("body", ""),
            "content_type": entry.get("content_type", ""),
        }
        for key, entry in config_map.items()
    }


def new_revision_id() -> str:
    return f"{_REVISION_PREFIX}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------- registry


class DesiredConfigRegistry:
    """In-memory view of the current desired config per agent, backed by SQLite.

    The message path asks this on every status report, so `get()` must not touch
    the database (#109): the decoded config map is cached at load and replaced on
    push. A push is a human-scale event, so the cache is refreshed from the store
    afterwards rather than patched by hand.
    """

    def __init__(self, store: Optional[SQLiteDesiredConfigStore] = None):
        self._store = store or SQLiteDesiredConfigStore()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.reload()

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

    def history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._store.history(agent_id, limit)

    def count(self) -> int:
        return len(self._cache)

    # -- writes --------------------------------------------------------------

    def push(
        self,
        agent_id: str,
        config_map: Dict[str, Dict[str, Any]],
        author: str,
        withdrawn: bool = False,
        revision_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store a new revision and make it current. Returns the new record.

        `revision_id` is passed in by the API so the audit row written *before* the
        mutation can name the revision it authorised (#97's pre-write contract).
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
        )
        decoded = self._decode(record)
        if decoded is not None:
            self._cache[agent_id] = decoded
        return record

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

    def rollback(self, agent_id: str, author: str) -> Optional[Dict[str, Any]]:
        """Re-push the previous revision's content as a **new** revision (D4)."""
        previous = self._store.previous(agent_id)
        decoded = self._decode(previous)
        if decoded is None:
            return None
        return self.push(agent_id, decoded["config_map"], author)

    def mark_failed(self, revision_id: str, detail: str = "") -> None:
        self._store.mark_failed(revision_id, detail)
        for record in self._cache.values():
            if record.get("revision_id") == revision_id:
                record["failed_at"] = record.get("failed_at") or utcnow().isoformat()
                record["failure_detail"] = detail

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


def offer_for(
    agent_id: str,
    capabilities: int,
    status: Optional[Dict[str, Any]] = None,
) -> Optional[AgentRemoteConfig]:
    """Build the `remote_config` offer for one message, or None (#81).

    Conditions, in the order the spec states them:

    1. config push is allowed by the posture (#96) — a capability bit is a promise;
    2. the agent advertises `AcceptsRemoteConfig` — the spec's MUST;
    3. a revision exists and has not been poisoned by a failure (D6);
    4. the agent's reported hash differs from ours — the spec's MUST, and what
       stops a server from re-pushing a config the agent already has.
    """
    if not posture.push_allowed():
        return None
    if not capabilities & AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig:
        return None

    record = REGISTRY.get(agent_id)
    if record is None:
        return None
    if record.get("failed_at"):
        # Poisoned: never auto-retry. Re-approval creates a new revision.
        return None

    reported = hash_from_wire((status or {}).get("lastRemoteConfigHash"))
    if reported == record["config_hash"]:
        return None

    offer = AgentRemoteConfig()
    offer.config.CopyFrom(build_config_map(record.get("config_map") or {}))
    offer.config_hash = hash_bytes(record["config_hash"])
    return offer


def note_status(
    agent_id: str,
    status_name: Optional[str],
    reported_hash_b64: Optional[str],
    error_message: str = "",
) -> Optional[Dict[str, Any]]:
    """React to a reported `RemoteConfigStatus`; returns an audit row if notable.

    Only a `FAILED` for **our** revision poisons it. A failure the agent reports for
    some other config is not ours to punish, and treating it as ours would stop
    delivery for a config that never caused it.
    """
    if status_name != STATUS_FAILED:
        return None
    record = REGISTRY.get(agent_id)
    if record is None:
        return None
    reported = hash_from_wire(reported_hash_b64)
    if reported != record["config_hash"]:
        return None
    detail = error_message or "agent reported FAILED without an error message"
    REGISTRY.mark_failed(record["revision_id"], detail)
    logger.error(
        f"agent {agent_id}: remote config revision {record['revision_id']} FAILED: {detail} "
        "(revision is now poisoned; it will not be retried — push a new revision or roll back)"
    )
    return audit.audit_event(
        "config.apply.failed",
        principal="system",
        actor_kind="system",
        result="error",
        target_kind="agent",
        target_id=agent_id,
        revision_id=record["revision_id"],
        config_hash=record["config_hash"],
        error=detail,
    )


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
