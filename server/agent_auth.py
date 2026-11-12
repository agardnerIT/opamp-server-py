"""Per-agent credentials for the OpAMP channel (#94).

Identity comes from the credential, never from the message body (epic #93, D1).
A client presents ``Authorization: Bearer <token>`` on either transport; the token
resolves to one ``agent_id``, and ``AgentToServer.instance_uid`` must hex to that
same id or the message is refused (HTTP 403 / WebSocket 1008) **before** any state
is applied. That is the whole point: an impostor cannot receive an agent's config,
take over its socket binding, or report health on its behalf.

Why tokens and not mTLS: this server speaks plain HTTP behind the TLS terminator
(uvicorn on loopback), so an app-level client certificate would mean trusting a
header any local process can forge. ``token`` is the only agent-auth mode (#125);
``off`` and ``mtls`` are refused at startup rather than treated as "no credential
required", because a tokenless endpoint believes whatever ``instance_uid`` a client
puts in the body.

Storage is SHA-256 only. Tokens are 256 bits of ``secrets.token_urlsafe``, so a
fast hash is the right primitive — there is nothing to brute-force — and it gives
an O(1) lookup; verification still uses ``hmac.compare_digest``. A token is
returned exactly once, by ``POST /agents/{id}/token``, and never appears in logs,
listings, audit entries or error bodies.

Bootstrap is deliberately out-of-band. ``extension/opampextension`` registers no
``OnOpampConnectionSettings`` callback, so an in-band "here is your token" reply
would be ignored by the most common agent; :func:`collector_snippet` returns the
headers block an operator pastes into the collector config instead.
Registration-on-first-use, connection-settings offers and CSR/TOFU stay with #83.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import prometheus_client as prom_client
from loguru import logger

from server import posture
from server import state as state_store
from server.state import SQLiteCredentialStore, utcnow

TOKEN_PREFIX = "opamp_agent_"
TOKEN_BYTES = 32
AUTH_SCHEME = "bearer"
AUTH_HEADER = "Authorization"
DEFAULT_ENDPOINT = "http://localhost:4320"
# `last_seen_at` is refreshed at most this often (see `_touch_needed`).
TOUCH_INTERVAL_SECONDS = 60

# Failure reasons — the label on `opamp_agent_auth_failures_total` and the
# `reason` field of every audit entry. Keep this list stable: dashboards and the
# cutover runbook in the README both key off it.
REASON_MISSING = "missing"
REASON_MALFORMED = "malformed"
REASON_UNKNOWN = "unknown"
REASON_REVOKED = "revoked"
REASON_EXPIRED = "expired"
REASON_AGENT_LIMIT = "agent_limit"
REASON_UID_MISMATCH = "uid_mismatch"
REASON_BINDING_CONFLICT = "binding_conflict"

REASONS = (
    REASON_MISSING,
    REASON_MALFORMED,
    REASON_UNKNOWN,
    REASON_REVOKED,
    REASON_EXPIRED,
    REASON_AGENT_LIMIT,
    REASON_UID_MISMATCH,
    REASON_BINDING_CONFLICT,
)

#: The expiry presets the API and UI offer (#126). `None` means never.
EXPIRY_PRESETS = (1, 7, 30, None)
#: Upper bound for `expires_in_days`. A "temporary" token that outlives its fleet is
#: just a permanent token with a misleading label.
MAX_EXPIRY_DAYS = 365

PROM_AGENT_AUTH_FAILURES = prom_client.Counter(
    "opamp_agent_auth_failures_total",
    "Rejected OpAMP agent authentications by reason",
    ["reason"],
)

PROM_AGENT_AUTH_OK = prom_client.Counter(
    "opamp_agent_auth_total",
    "Authenticated OpAMP agent connections (mode=token)",
)

# Constructed at import like AGENT_REGISTRY; tests swap in a tmp_path-backed store.
CREDENTIALS = SQLiteCredentialStore()


class AgentAuthError(Exception):
    """A refused agent message. The transport decides 403 vs close 1008.

    Raised *before* the message is applied, so a refusal can never leave partial
    state behind. Recording the failure (metric + audit) belongs to the caller,
    which is where the transport and client address are known.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        principal: str = "",
        claimed: str = "",
    ):
        self.reason = reason
        self.detail = detail
        self.principal = principal
        self.claimed = claimed
        super().__init__(detail)


@dataclass(frozen=True)
class Principal:
    """Who is speaking, according to the credential they presented.

    `agent_id` is set only for a **bound** token (#94): the credential that *is* one
    agent's identity. A **fleet** token (#126) leaves it empty — the token authorises
    access to the endpoint, and the uid in the message body stands, which is why the
    same token can onboard a fleet without the server knowing any uid in advance.

    `token_id` is the public handle, and it is what a WebSocket binding is keyed on: two
    agents behind one fleet token are the same principal, two tokens are not.
    """

    agent_id: str = ""
    label: str = ""
    token_hash: str = ""
    token_id: str = ""
    max_agents: Optional[int] = None
    expires_at: Optional[str] = None

    @property
    def bound(self) -> bool:
        return bool(self.agent_id)


# --------------------------------------------------------------------- helpers


def normalize_agent_id(agent_id: str) -> str:
    """Validate a hex instance UID (`agent_id` is `hex(instance_uid)`).

    Raises ValueError for anything that cannot be one, so the API can answer 422
    instead of minting a credential nobody can ever present.
    """
    value = (agent_id or "").strip().lower()
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(
            "agent_id must be a 16-byte OpAMP instance UID in hex "
            "(32 characters, e.g. '4f3c2b1a...'); it is the hex of instance_uid"
        )
    return value


def hash_token(token: str) -> str:
    """SHA-256 hex of a token — the only form ever stored or compared."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_from_header(headers: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """Extract the token hash from a request's headers.

    Returns `(hash, None)` or `(None, reason)`. The reason is reported to the
    metric and the audit log, never to the client: every rejection is the same
    401 to a stranger.
    """
    raw = (headers.get(AUTH_HEADER) or "").strip()
    if not raw:
        return None, REASON_MISSING

    scheme, _, token = raw.partition(" ")
    if scheme.strip().lower() != AUTH_SCHEME or not token.strip():
        # Wrong scheme or an empty bearer: a broken client, not a guess.
        return None, REASON_MALFORMED
    return hash_token(token.strip()), None


# ------------------------------------------------------------------- lifecycle


def issue_token(
    agent_id: str = "",
    label: str = "",
    retire_previous: bool = False,
    *,
    expires_in_days: Optional[int] = None,
    max_agents: Optional[int] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Mint a token. Returns `(token, record)` — the plaintext token, exactly once.

    Two shapes, one function (#126):

    * `agent_id=""` (the default) → a **fleet token**: it authorises agents up to
      `max_agents` and leaves each agent's `instance_uid` as its identity. This is the
      one that fixes the bootstrap deadlock — an admin mints before any agent exists.
    * `agent_id=<hex uid>` → a **bound token** (#94), where the credential *is* that
      agent's identity. Kept because "pin this credential to one agent" is a real
      requirement, not because it is the default.

    `expires_in_days=None` means never; 1/7/30 are the presets. `max_agents=None` means
    unlimited. Both are enforced on the auth path (`authenticate` / `authorize_agent`),
    not here — issuing a token that cannot be used yet is legitimate.

    The plaintext token is not persisted anywhere. When a bound token already exists for
    that agent, the outgoing ones are marked `retire_on_use` so they stop working the
    moment this one is first seen (`retire_previous=True` retires them immediately
    instead, for an operator who has already deployed the new config and wants a hard
    cutover).
    """
    if agent_id:
        agent_id = normalize_agent_id(agent_id)
    expires_at = _expiry_timestamp(expires_in_days)
    max_agents = _validate_max_agents(max_agents)
    previous = CREDENTIALS.active(agent_id) if agent_id else []

    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    digest = hash_token(token)
    token_id = "agtok_" + secrets.token_hex(8)
    CREDENTIALS.add(
        digest,
        agent_id,
        label=label or "",
        token_id=token_id,
        expires_at=expires_at,
        max_agents=max_agents,
    )

    for record in previous:
        if retire_previous:
            CREDENTIALS.revoke(record["token_hash"])
        else:
            CREDENTIALS.set_retire_on_use(record["token_hash"], digest)

    target = f"agent {agent_id}" if agent_id else "fleet"
    logger.info(
        f"agent token {token_id} issued for {target}"
        + (f" (label: {label})" if label else "")
        + f"; expires {expires_at or 'never'}"
        + f"; max agents {max_agents if max_agents is not None else 'unlimited'}"
        + (f"; {len(previous)} previous credential(s) "
           + ("revoked" if retire_previous else "retired on first use")
           if previous
           else "")
    )
    return token, CREDENTIALS.get(digest) or {}


def _expiry_timestamp(expires_in_days: Optional[int]) -> Optional[str]:
    """`None` = never, else `now + days` as an ISO-8601 UTC string.

    Validated rather than clamped: a caller asking for 400 days has a bug or a policy
    this server should not silently overrule, and a caller asking for 0 has asked for a
    token that is already dead.
    """
    if expires_in_days is None:
        return None
    if isinstance(expires_in_days, bool) or not isinstance(expires_in_days, int):
        raise ValueError("expires_in_days must be a whole number of days, or null for never")
    if expires_in_days < 1 or expires_in_days > MAX_EXPIRY_DAYS:
        raise ValueError(
            f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS} (or null for never)"
        )
    return (utcnow() + timedelta(days=expires_in_days)).isoformat()


def _validate_max_agents(max_agents: Optional[int]) -> Optional[int]:
    if max_agents is None:
        return None
    if isinstance(max_agents, bool) or not isinstance(max_agents, int):
        raise ValueError("max_agents must be a whole number, or null for unlimited")
    if max_agents < 1:
        raise ValueError("max_agents must be at least 1 (null means unlimited)")
    return max_agents


def revoke_tokens(agent_id: str) -> int:
    """Revoke every live credential for an agent. Returns how many were live.

    Revocation blocks delivery; it does not delete the agent, its state or (once
    #99 exists) its desired config. That surprises people, so it is also stated
    in the README.
    """
    agent_id = normalize_agent_id(agent_id)
    count = CREDENTIALS.revoke_agent(agent_id)
    logger.warning(f"revoked {count} agent credential(s) for {agent_id}")
    return count


def revoke_token(token_id: str) -> bool:
    """Revoke one token by its public handle (#126). The row is kept, not deleted."""
    record = CREDENTIALS.by_token_id(token_id)
    if record is None:
        return False
    revoked = CREDENTIALS.revoke_by_token_id(token_id)
    if revoked:
        logger.warning(
            f"agent token {token_id} revoked"
            + (f" (label: {record['label']})" if record.get("label") else "")
        )
    return revoked


def list_tokens() -> List[Dict[str, Any]]:
    """Every token as the API shows it: state, never the token or its hash (#126)."""
    return [token_view(row) for row in CREDENTIALS.list_all()]


def token_view(record: Mapping[str, Any]) -> Dict[str, Any]:
    """One row in public shape — `token_id`, not `token_hash`.

    `agents_used` and `max_agents` are reported together because a cap is only
    meaningful as "3 of 15"; `expires_at: null` means never, and `expired` is computed
    here so no caller has to re-derive it from a timestamp.
    """
    expires_at = record.get("expires_at")
    return {
        "token_id": record.get("token_id") or "",
        "label": record.get("label") or "",
        "agent_id": record.get("agent_id") or None,
        "bound": bool(record.get("agent_id")),
        "created_at": record.get("created_at"),
        "expires_at": expires_at,
        "expired": bool(expires_at) and _is_expired(expires_at),
        "max_agents": record.get("max_agents"),
        "agents_used": int(record.get("agents_used") or 0),
        "last_seen_at": record.get("last_seen_at"),
        "revoked_at": record.get("revoked_at"),
        "active": not record.get("revoked_at") and not (
            expires_at and _is_expired(expires_at)
        ),
    }


def _is_expired(expires_at: str) -> bool:
    """Is this ISO timestamp in the past? A timestamp we cannot read is expired.

    Fail closed: an unparseable expiry is a corrupt row, and the safe reading of "I
    cannot tell when this dies" is "it is dead", not "it lives forever".
    """
    try:
        moment = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= utcnow()


def authenticate(headers: Mapping[str, str]) -> Tuple[Optional[Principal], Optional[str]]:
    """Resolve a request's credential. Returns `(principal, None)` or `(None, reason)`.

    There is no tokenless path (#125): every call requires a credential, and a
    deployment configured for `off`/`mtls` refuses to start rather than reaching here.

    Every refusal is a reason, and the reasons are ordered by what the operator has to
    fix: `missing`/`malformed` (the agent's config), `unknown`/`revoked`/`expired` (the
    token), `agent_limit` and `uid_mismatch` (the policy), `binding_conflict` (a live
    socket).
    """
    digest, reason = _hash_from_header(headers)
    if digest is None:
        return None, reason

    record = CREDENTIALS.get(digest)
    if record is None or not hmac.compare_digest(record["token_hash"], digest):
        return None, REASON_UNKNOWN
    if record["revoked_at"]:
        return None, REASON_REVOKED
    if record.get("expires_at") and _is_expired(record["expires_at"]):
        return None, REASON_EXPIRED

    # Zero-downtime rotation: this is the first use of the replacement, so the
    # credential(s) it replaced stop working now. Bound tokens only — a fleet token
    # replaces nothing.
    if record.get("agent_id"):
        retired = CREDENTIALS.supersede(record["agent_id"], digest)
        if retired:
            logger.info(
                f"rotated credential for {record['agent_id']}: retired {retired} "
                "previous credential(s) after first use of the new token"
            )
    if _touch_needed(record):
        CREDENTIALS.touch(digest)
    PROM_AGENT_AUTH_OK.inc()
    return (
        Principal(
            agent_id=record.get("agent_id") or "",
            label=record["label"] or "",
            token_hash=digest,
            token_id=record.get("token_id") or "",
            max_agents=record.get("max_agents"),
            expires_at=record.get("expires_at"),
        ),
        None,
    )


def _touch_needed(record: Dict[str, Any]) -> bool:
    """Only write `last_seen_at` when it is stale enough to matter.

    Every agent message authenticates, so an unthrottled UPDATE would mean a
    SQLite write (and commit) per poll — hundreds per second on a large fleet —
    to answer a question operators ask in minutes. A minute of granularity is
    plenty for watching a cutover.
    """
    last_seen = record.get("last_seen_at")
    if not last_seen:
        return True
    try:
        last = datetime.fromisoformat(last_seen)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (utcnow() - last) >= timedelta(seconds=TOUCH_INTERVAL_SECONDS)


def authorize_agent(principal: Principal, claimed_agent_id: str) -> None:
    """Refuse a message this credential may not carry, before any state is applied.

    A **bound** token (#94) must match the uid exactly: the credential is the identity,
    so anything else is an impostor.

    A **fleet** token (#126) is access, not identity, so the uid stands — but it is
    counted against the token's cap. `max_agents` is what stops a leaked token from
    consuming the server: the uid that would exceed it is refused here, with the same
    403 the transport already knows how to answer.
    """
    if principal.bound:
        if principal.agent_id != claimed_agent_id:
            raise AgentAuthError(
                REASON_UID_MISMATCH,
                "credential is not valid for this instance_uid",
                principal=principal.agent_id,
                claimed=claimed_agent_id,
            )
        return

    if not principal.token_hash:
        # A hand-built principal (tests, an in-process caller) with no stored row: no
        # cap to enforce and nothing to count.
        return
    if _already_counted(principal.token_hash, claimed_agent_id):
        return
    outcome = CREDENTIALS.register_agent(
        principal.token_hash, claimed_agent_id, principal.max_agents
    )
    if outcome == state_store.AGENT_LIMIT_REACHED:
        raise AgentAuthError(
            REASON_AGENT_LIMIT,
            f"token {principal.token_id or '(unknown)'} has reached its limit of "
            f"{principal.max_agents} agent(s)",
            principal=principal.token_id,
            claimed=claimed_agent_id,
        )
    _remember_counted(principal.token_hash, claimed_agent_id)


# `authorize_uid` was the #94 name and is still the right one for a bound token; keep it
# as an alias so existing call sites and tests do not have to know about the rename.
authorize_uid = authorize_agent


# One DB write transaction per (token, uid) is enough: after the first success the
# answer cannot change (a slot is never released), and this runs on every message.
_COUNTED: Dict[Tuple[str, str], bool] = {}
_COUNTED_LOCK = threading.Lock()
_COUNTED_LIMIT = 10_000


def _already_counted(token_hash: str, agent_id: str) -> bool:
    with _COUNTED_LOCK:
        return (token_hash, agent_id) in _COUNTED


def _remember_counted(token_hash: str, agent_id: str) -> None:
    with _COUNTED_LOCK:
        if len(_COUNTED) >= _COUNTED_LIMIT:
            _COUNTED.clear()
        _COUNTED[(token_hash, agent_id)] = True


def forget_counted() -> None:
    """Drop the in-process cap cache (tests, and after a store swap)."""
    with _COUNTED_LOCK:
        _COUNTED.clear()


# ---------------------------------------------------------------- audit + metrics

# One audited seam for the whole server (#95, #97). Imported rather than
# re-implemented so `AUDIT {json}` lines have a single shape; `agent_auth.audit_event`
# stays importable because #94's call sites and tests use it by name.
from server.audit import audit_event  # noqa: E402  (kept next to its only helpers)


def record_failure(
    reason: str,
    *,
    principal: str = "",
    claimed: str = "",
    transport: str = "",
    client: str = "",
) -> None:
    """Count and audit a refused agent request. Never called with a token."""
    PROM_AGENT_AUTH_FAILURES.labels(reason=reason).inc()
    audit_event(
        "agent_auth.denied",
        kind="agent_auth",
        result="denied",
        reason=reason,
        principal=principal,
        claimed=claimed,
        transport=transport,
        client=client,
    )


# ------------------------------------------------------------------- reporting


def credential_summary(
    agent_id: str,
    history: Optional[List[Dict[str, Any]]] = None,
    store: Optional[SQLiteCredentialStore] = None,
) -> Dict[str, Any]:
    """Credential state for one agent — never a token, never a hash.

    - `enrolled`: a credential was ever issued (revoked ones still count, so a
      revoked agent does not silently look "never enrolled").
    - `active`: live credentials. Two during a rotation grace window is normal.
    - `last_seen_at`: newest successful authentication, `None` if never used.
    - `revoked_at`: set only when nothing is live — the moment the last
      credential was revoked.
    - `tokens`: the tokens this agent got in with, by `token_id` (#126). A fleet token
      is not the agent's identity, but "which token admitted this agent" is the first
      question when a cap is hit or a token leaks, and it cannot be answered from the
      per-agent history alone.
    """
    store = store or CREDENTIALS
    rows = history if history is not None else store.history(agent_id)
    live = [row for row in rows if not row["revoked_at"]]
    seen = [row["last_seen_at"] for row in rows if row["last_seen_at"]]
    revoked = [row["revoked_at"] for row in rows if row["revoked_at"]]
    fleet = store.tokens_for_agent(agent_id) if history is None else []
    seen += [row["last_seen_at"] for row in fleet if row["last_seen_at"]]
    return {
        "enrolled": bool(rows) or bool(fleet),
        "active": len(live) + len([row for row in fleet if not row["revoked_at"]]),
        "last_seen_at": max(seen) if seen else None,
        "revoked_at": (max(revoked) if revoked and not live else None),
        "tokens": [
            {
                "token_id": row.get("token_id") or "",
                "label": row.get("label") or "",
                "bound": bool(row.get("agent_id")),
                "expires_at": row.get("expires_at"),
                "revoked_at": row.get("revoked_at"),
            }
            for row in (fleet + rows)
        ],
    }


def credential_summaries(
    agent_ids: Iterable[str],
    store: Optional[SQLiteCredentialStore] = None,
) -> Dict[str, Dict[str, Any]]:
    """Bulk variant used by `GET /agents` so listing does not do N queries."""
    store = store or CREDENTIALS
    ids = list(agent_ids)
    grouped = store.all_history(ids)
    return {
        agent_id: credential_summary(agent_id, grouped.get(agent_id, []), store=store)
        for agent_id in ids
    }


def snippet_endpoint(transport: str = "http") -> str:
    """The OpAMP URL an agent should be pointed at (`http` or `ws` transport)."""
    base = (posture.public_url() or DEFAULT_ENDPOINT).rstrip("/")
    scheme, _, host = base.partition("://")
    if transport == "ws":
        ws_scheme = "wss" if scheme == "https" else "ws"
        return f"{ws_scheme}://{host or base}/v1/opamp"
    return f"{base}/v1/opamp"


def collector_snippet(
    token: str,
    transport: str = "http",
    agent_type: Optional[str] = None,
) -> str:
    """A paste-ready bootstrap block carrying the token, in the agent's own shape.

    This is the v1 bootstrap: the token goes into the agent's config, because the
    collector extension cannot receive one in-band (see the module docstring).

    *#118*: the YAML is the `otelcol` profile's, not this module's. "How do I wire a
    credential into this product" is exactly the kind of type-specific knowledge a
    profile owns — a generic agent gets plain text (endpoint + header) rather than an
    invented YAML document it cannot read. `agent_type` is the resolved profile name
    when the agent is already known.

    *#134*: there is no `or "otelcol"` fallback in code any more. A token is usually
    minted *before* the agent first connects, so there is nothing to resolve; the
    dictionary answers which snippet to show then, with `bootstrap_default: true`
    (`registry.bootstrap_default()`), and the fallback profile's plain text when no
    file claims it.
    """
    from server import profiles

    profile = profiles.get(agent_type) or profiles.bootstrap_default()
    return profile.bootstrap_snippet(snippet_endpoint(transport), token)


# --------------------------------------------------------------------- startup


def log_startup() -> None:
    """One startup line naming the one way in (call from the lifespan and `__main__`).

    This is not a warning any more (#125): a configuration that would disable agent
    auth is refused before this runs, so the only honest message is where the token
    comes from.
    """
    logger.info(
        "agent authentication is mandatory (token): every agent presents a token "
        "minted with `opampctl agent-tokens create` (or `agents token create` for one "
        "bound agent); a tokenless connection is refused 401/1008"
    )
