"""Protocol-level validation shared by both OpAMP transports (#73).

A malformed body is a *protocol* error, not an authentication one: the HTTP
endpoint answers 400 and the socket closes 1002, after telling the agent what was
wrong in a `ServerErrorResponse{BAD_REQUEST}` (spec §Bad Request).

Keeping this in its own module is deliberate. `server.main` owns state and
`server.ws` owns connections; both need the same answer to "is this a message at
all, and does it have an identity?", and neither should import the other.
"""

from __future__ import annotations

import binascii
import secrets
import string
import threading
import time
from typing import Dict, List, Optional, Tuple

from proto.opamp_pb2 import ServerErrorResponseType_BadRequest, ServerToAgent

# #79: the OpAMP specification release this build targets. The counterpart of the
# generated stubs (`proto/opamp.proto`, v0.20.0, #66), documented in docs/capabilities.md
# and and served by `GET /health`, so a client never has to read the
# source to find out what it is talking to.
SPEC_VERSION = "0.20.0"
SPEC_RELEASE_URL = (
    f"https://github.com/open-telemetry/opamp-spec/releases/tag/v{SPEC_VERSION}"
)


BAD_REQUEST = ServerErrorResponseType_BadRequest

# Spec §AgentToServer.instance_uid says "MUST be 16 bytes long". We enforce only
# "non-empty" on purpose: every harness, benchmark and test client in this repo
# uses readable ASCII ids, and existing fleets are already registered under them.
# Rejecting those would be a breaking change with no safety benefit — the uid is
# not a security boundary (the credential is, #94). An *empty* uid is different:
# it is not an id at all, and it makes every such agent collide on one registry
# row. Recorded in memory.md so nobody "fixes" this back to 16-byte-only by
# accident; if that changes, every fixture in tests/ needs a UUIDv7 first.
EXPECTED_INSTANCE_UID_BYTES = 16
MIN_ACCEPTED_UID_BYTES = 1


class BadRequest(ValueError):
    """An AgentToServer we refuse to apply. The transport picks 400 vs close 1002."""

    def __init__(self, detail: str, instance_uid: bytes = b""):
        self.detail = detail
        # Echoed in the error_response when the body had an identity, so a
        # multiplexed stream can tell which agent was refused.
        self.instance_uid = instance_uid
        super().__init__(detail)


def error_response(detail: str, instance_uid: bytes = b"") -> ServerToAgent:
    """A spec-shaped BAD_REQUEST reply: `error_response` set and nothing else.

    The proto says "if error_response is set then all other fields below must be
    unset"; `instance_uid` is field 1, above it, and is what multiplexing needs.
    """
    response = ServerToAgent()
    if instance_uid:
        response.instance_uid = instance_uid
    response.error_response.type = BAD_REQUEST
    response.error_response.error_message = detail
    return response


def uuid7() -> bytes:
    """A 16-byte UUIDv7: 48-bit unix-ms timestamp, version/variant bits, random tail.

    `uuid.uuid7()` only exists in Python 3.14 and this project supports 3.10, so
    fifteen lines are cheaper than a dependency. The point of v7 over v4 is that
    ids sort by creation time, which the DB and the UI get for free.
    """
    millis = (time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    value = bytearray(millis.to_bytes(6, "big") + secrets.token_bytes(10))
    value[6] = (value[6] & 0x0F) | 0x70  # version 7
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant
    return bytes(value)


# --------------------------------------------------------------------- identity

# Spec §Plain HTTP Transport (`specification.md:568-573`): the client SHOULD set this
# header to the canonical string form of the `instance_uid` on every request. Starlette
# lowercases header names, so the lookup uses the lower-case spelling (#76).
INSTANCE_UID_HEADER = "opamp-instance-uid"

# #76: a header that disagrees with the body is a diagnostic, not a refusal, unless
# this is explicitly turned on. Proxies, multiplexers and sidecars rewrite headers in
# ways that are legitimate for them, and failing hard would break working fleets.
STRICT_UID_HEADER_ENV = "OPAMP_UID_HEADER_STRICT"

# #75: which description attributes say *which machine* this is. `service.instance.id`
# first, because it is unique per process and is what a cloned VM keeps or changes
# depending on how it was cloned; `host.name` is the fallback that survives a restart
# of the same collector on the same host.
HOST_IDENTITY_KEYS = ("service.instance.id", "host.name")

_HEX_DIGITS = frozenset(string.hexdigits)


def parse_instance_uid_header(raw: Optional[str]) -> Optional[bytes]:
    """The 16 bytes an `OpAMP-Instance-UID` header means, or `None` if it is not one.

    Lenient on purpose (#76): the spec's canonical form is the hyphenated lower-case
    UUID string, but an upper-case value, braces, a `urn:uuid:` prefix and bare
    32-character hex all denote the same uid and all appear in the wild. Never raises:
    the header is a hint for logs, and the body stays authoritative.
    """
    if not raw:
        return None
    value = raw.strip()
    if value[:9].lower() == "urn:uuid:":
        value = value[9:]
    value = value.strip("{} ").replace("-", "")
    if len(value) != EXPECTED_INSTANCE_UID_BYTES * 2 or not all(c in _HEX_DIGITS for c in value):
        return None
    try:
        return binascii.unhexlify(value)
    except (binascii.Error, ValueError):  # pragma: no cover - unhexlify after the check
        return None


def _string_value(value: object) -> Optional[str]:
    """The scalar inside a `MessageToDict` AnyValue, as a string."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
            if key in value:
                inner = value[key]
                text = inner if isinstance(inner, str) else str(inner)
                return text or None
    return None


def host_identity(description: Optional[dict]) -> Optional[str]:
    """`key=value` for the attribute that says which host is talking (#75).

    Returns `None` when the agent reports neither attribute, which is the common case
    for a first message before `AgentDescription` arrives. Duplicate detection treats
    "unknown" as "no evidence" rather than as agreement, so an agent that never sends
    a description is never re-issued a uid on this signal alone.
    """
    if not description:
        return None
    attributes: List[dict] = []
    for source in ("identifyingAttributes", "nonIdentifyingAttributes"):
        attributes.extend(description.get(source) or [])
    for key in HOST_IDENTITY_KEYS:
        for attribute in attributes:
            if attribute.get("key") != key:
                continue
            value = _string_value(attribute.get("value"))
            if value:
                return f"{key}={value}"
    return None


# ----------------------------------------------------------------- duplicate uid

# Spec §Duplicate instance_uid Detection (`specification.md:3467`): two agents can
# present one uid (cloned VM, restored backup, broken id generator), and the server
# SHOULD mint a replacement.
#
# The signal is *interleaving*, not "the identity changed": one agent that renames its
# `service.instance.id` in place produces `A A B B B`, while two agents sharing a uid
# produce `A B A` (or `B A B`) — a return to an identity we have already seen after a
# different one. That rule is what keeps the in-place rename (an acceptance criterion
# in #75) from being re-issued a uid, and it needs no clock, no store and no
# per-host registration.
REASON_HOST_IDENTITY = "host_identity"
# The clone case: two agents on machines that look identical (a VM image) alternate
# their independent counters, so `sequence_num` *decreases* twice in a row. A single
# decrease is an agent that restarted its counter, which must not re-issue anything.
REASON_SEQUENCE_REGRESSION = "sequence_regression"

# How many recent identities per uid to compare against, and how long to stay quiet
# after acting (the agent needs a poll or two to adopt the uid it was given).
DEFAULT_HISTORY = 4
DEFAULT_SUPPRESSION_SECONDS = 60.0
DEFAULT_MAX_TRACKED = 4096


class DuplicateUidDetector:
    """Spots two agents behind one `instance_uid` and remembers the ones it re-issued.

    In-memory by design: this is traffic analysis, not fleet state. A restart loses
    the streak and detection resumes after two messages, while the replacement itself
    (the new uid and why it changed) is persisted on the agent row by `main.py`.

    `observe()` is called from both transports, so the maps are guarded. It never
    raises and never decides *what* to do — `main.py` owns the policy (re-issue only
    when the agent is unauthenticated) and the observability.
    """

    def __init__(
        self,
        history: int = DEFAULT_HISTORY,
        suppression_seconds: float = DEFAULT_SUPPRESSION_SECONDS,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        self._history_depth = max(2, history)
        self._suppression_seconds = max(0.0, suppression_seconds)
        self._max_tracked = max(1, max_tracked)
        self._lock = threading.Lock()
        self._history: Dict[str, List[str]] = {}
        self._sequences: Dict[str, Tuple[int, int]] = {}  # uid -> (last, decreasing)
        self._quiet_until: Dict[str, float] = {}

    def observe(
        self,
        agent_id: str,
        identity: Optional[str],
        sequence_num: int,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Record one message; return a reason once two agents are indicated.

        `agent_id` is the uid as the client sent it (hex), so the record keeps its
        identity even while the *effective* id is being replaced.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            until = self._quiet_until.get(agent_id)
            if until is not None:
                if now < until:
                    return None
                del self._quiet_until[agent_id]

            history = self._history.setdefault(agent_id, [])
            reason = None
            if identity and history and history[-1] != identity:
                # A return to an identity we have already seen, with a different one
                # in between: two agents, not a rename.
                if identity in history:
                    reason = REASON_HOST_IDENTITY
            if identity:
                history.append(identity)
                del history[: -self._history_depth]

            if sequence_num:
                previous, decreasing = self._sequences.get(agent_id, (0, 0))
                if previous and sequence_num < previous:
                    decreasing += 1
                elif previous and sequence_num > previous:
                    decreasing = 0
                # A repeat is a gap (#70), not evidence of a second agent: an agent
                # that retries the same message looks identical, so the streak holds.
                self._sequences[agent_id] = (sequence_num, decreasing)
                if decreasing >= 2 and reason is None:
                    reason = REASON_SEQUENCE_REGRESSION

            self._evict()
            return reason

    def confirm(self, agent_id: str, now: Optional[float] = None) -> None:
        """Act on a detection: forget the evidence and stop watching for a while.

        Without this, the duplicate keeps polling under the old uid until it has read
        the reply, and every one of those messages re-triggers a detection — a new uid
        minted per poll.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._history.pop(agent_id, None)
            self._sequences.pop(agent_id, None)
            self._quiet_until[agent_id] = now + self._suppression_seconds

    def forget(self, agent_id: str) -> None:
        """Drop everything known about a uid that no longer exists."""
        with self._lock:
            self._history.pop(agent_id, None)
            self._sequences.pop(agent_id, None)
            self._quiet_until.pop(agent_id, None)

    def reset(self) -> None:
        """Forget every uid (tests, and nothing else)."""
        with self._lock:
            self._history.clear()
            self._sequences.clear()
            self._quiet_until.clear()

    def _evict(self) -> None:
        """Bound the maps so a churning fleet cannot grow them without limit."""
        while len(self._history) > self._max_tracked:
            oldest = next(iter(self._history))
            self._history.pop(oldest, None)
            self._sequences.pop(oldest, None)
            self._quiet_until.pop(oldest, None)
