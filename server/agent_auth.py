"""Per-agent credentials for the OpAMP channel (#94).

Identity comes from the credential, never from the message body (epic #93, D1).
A client presents ``Authorization: Bearer <token>`` on either transport; the token
resolves to one ``agent_id``, and ``AgentToServer.instance_uid`` must hex to that
same id or the message is refused (HTTP 403 / WebSocket 1008) **before** any state
is applied. That is the whole point: an impostor cannot receive an agent's config,
take over its socket binding, or report health on its behalf.

Why tokens and not mTLS: this server speaks plain HTTP behind the TLS terminator
(uvicorn on loopback), so an app-level client certificate would mean trusting a
header any local process can forge. ``server.posture.ENFORCED_AGENT_AUTH_MODES``
decides which modes open the config-push gate and only ``token`` is implemented,
so ``OPAMP_AGENT_AUTH=mtls`` fails closed instead of pretending.

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import prometheus_client as prom_client
from loguru import logger

from server import posture
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
REASON_UID_MISMATCH = "uid_mismatch"
REASON_BINDING_CONFLICT = "binding_conflict"

REASONS = (
    REASON_MISSING,
    REASON_MALFORMED,
    REASON_UNKNOWN,
    REASON_REVOKED,
    REASON_UID_MISMATCH,
    REASON_BINDING_CONFLICT,
)

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
    """Who is speaking, according to the credential they presented."""

    agent_id: str
    label: str = ""
    token_hash: str = ""


# --------------------------------------------------------------------- helpers


def mode() -> str:
    """`off|token|mtls`, read at call time (same source as the posture report)."""
    return posture.agent_auth_mode()


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
    agent_id: str,
    label: str = "",
    retire_previous: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Mint a credential for `agent_id`. Returns `(token, record)` — once.

    The plaintext token is not persisted anywhere. When the agent already has
    live credentials, the outgoing ones are marked `retire_on_use` so they stop
    working the moment this one is first seen (`retire_previous=True` retires
    them immediately instead, for an operator who has already deployed the new
    config and wants a hard cutover).
    """
    agent_id = normalize_agent_id(agent_id)
    previous = CREDENTIALS.active(agent_id)

    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    digest = hash_token(token)
    CREDENTIALS.add(digest, agent_id, label=label or "")

    for record in previous:
        if retire_previous:
            CREDENTIALS.revoke(record["token_hash"])
        else:
            CREDENTIALS.set_retire_on_use(record["token_hash"], digest)

    logger.info(
        f"agent credential issued for {agent_id}"
        + (f" (label: {label})" if label else "")
        + f"; {len(previous)} previous credential(s) "
        + ("revoked" if retire_previous else "retired on first use of the new token")
    )
    return token, CREDENTIALS.get(digest) or {}


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


def authenticate(headers: Mapping[str, str]) -> Tuple[Optional[Principal], Optional[str]]:
    """Resolve a request's credential. Returns `(principal, None)` or `(None, reason)`.

    In `off` mode this is a no-op returning `(None, None)` — exactly the legacy
    behaviour, which is what keeps the migration path open.
    """
    if mode() != "token":
        return None, None

    digest, reason = _hash_from_header(headers)
    if digest is None:
        return None, reason

    record = CREDENTIALS.get(digest)
    if record is None or not hmac.compare_digest(record["token_hash"], digest):
        return None, REASON_UNKNOWN
    if record["revoked_at"]:
        return None, REASON_REVOKED

    # Zero-downtime rotation: this is the first use of the replacement, so the
    # credential(s) it replaced stop working now.
    retired = CREDENTIALS.supersede(record["agent_id"], digest)
    if retired:
        logger.info(
            f"rotated credential for {record['agent_id']}: retired {retired} "
            "previous credential(s) after first use of the new token"
        )
    if _touch_needed(record):
        CREDENTIALS.touch(digest)
    PROM_AGENT_AUTH_OK.inc()
    return Principal(record["agent_id"], record["label"] or "", digest), None


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


def authorize_uid(principal: Principal, claimed_agent_id: str) -> None:
    """Refuse a message whose claimed identity is not the credential's identity."""
    if principal.agent_id != claimed_agent_id:
        raise AgentAuthError(
            REASON_UID_MISMATCH,
            "credential is not valid for this instance_uid",
            principal=principal.agent_id,
            claimed=claimed_agent_id,
        )


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
    """
    rows = history if history is not None else (store or CREDENTIALS).history(agent_id)
    live = [row for row in rows if not row["revoked_at"]]
    seen = [row["last_seen_at"] for row in rows if row["last_seen_at"]]
    revoked = [row["revoked_at"] for row in rows if row["revoked_at"]]
    return {
        "enrolled": bool(rows),
        "active": len(live),
        "last_seen_at": max(seen) if seen else None,
        "revoked_at": (max(revoked) if revoked and not live else None),
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


def collector_snippet(token: str, transport: str = "http") -> str:
    """A paste-ready `extensions.opamp` block carrying the token.

    This is the v1 bootstrap: the token goes into the agent's config, because the
    collector extension cannot receive one in-band (see the module docstring).
    """
    base = (posture.public_url() or DEFAULT_ENDPOINT).rstrip("/")
    scheme, _, host = base.partition("://")
    if transport == "ws":
        ws_scheme = "wss" if scheme == "https" else "ws"
        endpoint = f"{ws_scheme}://{host or base}/v1/opamp"
        block = "ws"
    else:
        endpoint = f"{base}/v1/opamp"
        block = "http"
    return (
        "extensions:\n"
        "  opamp:\n"
        "    server:\n"
        f"      {block}:\n"
        f"        endpoint: {endpoint}\n"
        "        headers:\n"
        f'          Authorization: "Bearer {token}"\n'
    )


# --------------------------------------------------------------------- startup


def warn_if_unenforced() -> None:
    """Startup warning for `off` / `mtls` (call from the lifespan and `__main__`).

    `off` is the default so existing deployments keep working, but nobody should
    discover it from an incident: any client that can reach the port can claim any
    identity until this is `token`, and config push refuses to start without it.
    """
    auth_mode = mode()
    if auth_mode == "off":
        logger.warning(
            "OPAMP_AGENT_AUTH=off: agents are not authenticated — any client that can "
            "reach this server can claim any instance_uid. Set OPAMP_AGENT_AUTH=token and "
            "give each agent a credential (`opampctl agents token create <agent-id>`) "
            "before enabling config push."
        )
    elif auth_mode == "mtls":
        logger.warning(
            "OPAMP_AGENT_AUTH=mtls is configured but NOT enforced by this build: client "
            "certificates are terminated by your proxy, and trusting a forwarded header "
            "would be forgeable. Agent identity stays unauthenticated; use "
            "OPAMP_AGENT_AUTH=token (mTLS lands with the CA work in #83)."
        )
