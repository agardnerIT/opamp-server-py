"""Admin identity: principals, roles, sessions and login lockout (#95).

`server/admin_auth.py` answers *"what is the admin password"* — one shared secret
from an env var, a file or a generated file. This module answers *"who is making
this request and are they allowed to"*. The legacy secret is still accepted (a
synthesised `admin` principal) so existing deployments and scripts keep working,
but every privileged route now asks for a **role**, and roles mean the two-phase
config push in #93 can be real: the person who approves a change is not
necessarily the person who proposed it.

Design notes worth keeping:

* **Roles are ordered** (`viewer < operator < approver < admin`) — a role implies
  everything below it. Approval is additive, not a separate universe: an
  `approver` may also do everything an `operator` may.
* **Read-only mode stays read-open.** With no credential configured at all,
  `viewer` requests pass and everything that mutates is refused with 403. That
  keeps the documented dashboard-only deployment working while closing the hole
  that matters (#95: "unset password ⇒ auth disabled" was not one hole, it was
  every hole). Enabling anything write-capable requires a credential, and
  `server.posture` already refuses to boot config push in that state.
* **Two hash primitives.** Sessions are 256-bit random tokens, so SHA-256 at rest
  is correct (nothing to brute-force, O(1) lookup). Human passwords are not, so
  they go through scrypt. Using one primitive for both is how projects end up
  either storing fast hashes of passwords or doing expensive lookups per request.
* **Lockout state is in memory, on purpose.** A brute-force attempt is exactly
  the traffic you do not want to turn into one SQLite write per guess. Losing
  counters on restart is acceptable; an attacker cannot restart the server.
* **`X-Forwarded-For` is opt-in** (`TRUST_PROXY_HEADERS`). Blindly trusting it
  gives every client the same lockout bucket (a self-inflicted DoS) and puts a
  fabricated IP in the audit trail — the same class of mistake as trusting a
  forwarded client certificate, which is why #94 refuses to do that for mTLS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import prometheus_client as prom_client
from fastapi import HTTPException, Request
from loguru import logger

from server import admin_auth
from server.audit import audit_event
from server.state import (
    SQLitePrincipalStore,
    SQLiteSessionStore,
    utcnow,
)

# ------------------------------------------------------------------- constants

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_ADMIN = "admin"

# Ordered: a role satisfies every requirement at or below its level.
ROLE_LEVELS: Dict[str, int] = {
    ROLE_VIEWER: 1,
    ROLE_OPERATOR: 2,
    ROLE_APPROVER: 3,
    ROLE_ADMIN: 4,
}
ROLES = tuple(ROLE_LEVELS)

SESSION_PREFIX = "opamp_admin_"
SESSION_BYTES = 32
SESSION_HEADER = "Authorization"
SESSION_SCHEME = "bearer"

# scrypt parameters: OWASP's minimum for interactive logins (2^14, r=8, p=1).
# ~16 MiB and tens of milliseconds per verification — cheap enough for a human
# login, expensive enough to make an offline wordlist run pointless.
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DKLEN = 32

# `last_seen_at` refresh throttle: every authenticated request would otherwise be
# a SQLite write; a minute of granularity is plenty for an idle timeout of 15.
TOUCH_INTERVAL_SECONDS = 60

# Bound on the in-memory lockout map, so a spoofed-header flood (or a very large
# fleet of attackers) cannot grow it without limit. Expired entries are pruned
# first, then the oldest.
MAX_LOCKOUT_ENTRIES = 5000

# Failure reasons — the label on `opamp_admin_auth_failures_total` and the
# `reason` field in the audit line. Stable: the README runbook keys off it.
REASON_MISSING = "missing"
REASON_MALFORMED = "malformed"
REASON_UNKNOWN_PRINCIPAL = "unknown_principal"
REASON_BAD_SECRET = "bad_secret"
REASON_REVOKED = "revoked"
REASON_EXPIRED = "expired"
REASON_SESSION_UNKNOWN = "session_unknown"
REASON_SESSION_EXPIRED = "session_expired"
REASON_SESSION_REVOKED = "session_revoked"
REASON_LOCKED_OUT = "locked_out"
REASON_ROLE = "role_denied"

REASONS = (
    REASON_MISSING,
    REASON_MALFORMED,
    REASON_UNKNOWN_PRINCIPAL,
    REASON_BAD_SECRET,
    REASON_REVOKED,
    REASON_EXPIRED,
    REASON_SESSION_UNKNOWN,
    REASON_SESSION_EXPIRED,
    REASON_SESSION_REVOKED,
    REASON_LOCKED_OUT,
    REASON_ROLE,
)

PROM_ADMIN_AUTH_FAILURES = prom_client.Counter(
    "opamp_admin_auth_failures_total",
    "Refused admin authentications by reason",
    ["reason"],
)
PROM_ADMIN_LOGINS = prom_client.Counter(
    "opamp_admin_logins_total",
    "Successful admin authentications by method",
    ["method"],
)

# Constructed at import like the other stores; tests swap in tmp_path-backed ones.
PRINCIPALS = SQLitePrincipalStore()
SESSIONS = SQLiteSessionStore()


@dataclass(frozen=True)
class Principal:
    """Who is calling, and what they may do.

    `principal_id` is empty for the synthesised legacy `admin` principal (the
    shared `ADMIN_PASSWORD`) and for anonymous read-only callers — neither has a
    row in `admin_principals`.
    """

    name: str
    role: str
    principal_id: str = ""
    method: str = ""  # "session" | "password" | "legacy" | "anonymous"
    session_hash: str = ""


# ---------------------------------------------------------------- configuration


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; using {default}")
        return default
    return value if value > 0 else default


def session_ttl_seconds() -> int:
    """Absolute session lifetime (`ADMIN_SESSION_TTL_SECONDS`, default 3600)."""
    return _env_int("ADMIN_SESSION_TTL_SECONDS", 3600)


def session_idle_seconds() -> int:
    """Idle timeout (`ADMIN_SESSION_IDLE_SECONDS`, default 900)."""
    return _env_int("ADMIN_SESSION_IDLE_SECONDS", 900)


def max_attempts() -> int:
    """Failed logins allowed per (identity, client) before backoff starts."""
    return _env_int("ADMIN_LOGIN_MAX_ATTEMPTS", 5)


def backoff_base_seconds() -> int:
    return _env_int("ADMIN_LOGIN_BACKOFF_SECONDS", 30)


def backoff_max_seconds() -> int:
    return _env_int("ADMIN_LOGIN_MAX_BACKOFF_SECONDS", 900)


def trust_proxy_headers() -> bool:
    """Whether `X-Forwarded-For` may be believed (`TRUST_PROXY_HEADERS`)."""
    return (os.environ.get("TRUST_PROXY_HEADERS") or "").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def configured() -> bool:
    """Is there any credential at all? False = read-only mode (#95 closes writes)."""
    return admin_auth.configured() or PRINCIPALS.count_live() > 0


def legacy_secret() -> str:
    """The shared `ADMIN_PASSWORD`, read live so tests and `ensure()` stay truthful."""
    return admin_auth.ADMIN_PASSWORD


def client_ip(request: Request) -> str:
    """Caller address for lockout and audit.

    Honours the **first** `X-Forwarded-For` hop only when `TRUST_PROXY_HEADERS` is
    on; otherwise the socket peer, because a header any client can set is not an
    identity. A chain with a proxy in front puts the real client first, which is
    what we want for per-client lockout.
    """
    if trust_proxy_headers():
        forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else ""


# ------------------------------------------------------------------ primitives


def normalize_role(role: str) -> str:
    """Validate a role name. Raises ValueError so the API can answer 422."""
    value = (role or "").strip().lower()
    if value not in ROLE_LEVELS:
        raise ValueError(
            f"role must be one of {', '.join(ROLES)}; got {role!r}"
        )
    return value


def role_satisfies(have: str, required: str) -> bool:
    """Is `have` at or above `required` in the role order?"""
    return ROLE_LEVELS.get(have, 0) >= ROLE_LEVELS.get(required, 99)


def hash_password(password: str) -> str:
    """scrypt hash, self-describing: `scrypt$n$r$p$<salt_b64>$<hash_b64>`.

    Self-describing so parameters can be raised later without a migration: old
    rows keep verifying with their own parameters.
    """
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        (password or "").encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored scrypt hash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(
        base64.b64encode(derived).decode("ascii"), hash_b64
    )


def hash_session(token: str) -> str:
    """SHA-256 hex of a session token — the only form stored or compared."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _dummy_hash() -> str:
    """A hash to verify against when a principal name is unknown.

    Keeps "no such principal" and "wrong password" the same cost, so response
    timing does not enumerate valid names. Cached: it exists only for its cost.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


_DUMMY_HASH: Optional[str] = None


def _secret_matches(supplied: str, expected: str) -> bool:
    """Constant-time compare that does not leak length through an early exit."""
    left = hashlib.sha256((supplied or "").encode("utf-8")).digest()
    right = hashlib.sha256((expected or "").encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


# --------------------------------------------------------------------- lockout

# key -> (consecutive failures, when the last one happened)
_FAILURES: Dict[str, Tuple[int, float]] = {}
_LOCKED_UNTIL: Dict[str, float] = {}


def lockout_key(identity: str, ip: str) -> str:
    return f"{(identity or '').strip().lower()}|{ip or ''}"


def locked_for(key: str) -> int:
    """Seconds the caller must wait, 0 if not locked."""
    until = _LOCKED_UNTIL.get(key, 0.0)
    remaining = int(until - time.monotonic())
    if remaining <= 0:
        _LOCKED_UNTIL.pop(key, None)
        return 0
    return remaining


def record_failure(key: str, reason: str) -> int:
    """Count one failure and extend the lockout. Returns seconds of backoff (0 if none)."""
    count = (_FAILURES.get(key, (0, 0.0))[0]) + 1
    _FAILURES[key] = (count, time.monotonic())
    PROM_ADMIN_AUTH_FAILURES.labels(reason=reason).inc()
    # Bound the map even before the first lockout: a spoofed-IP flood must not be
    # able to grow it one entry per guess.
    _prune_lockouts()

    if count < max_attempts():
        return 0

    # Exponential from the first excess attempt: after `max`, wait base, 2x base,
    # 4x base, ... capped. Reset only by a success or by the process restarting.
    exponent = min(count - max_attempts(), 10)
    backoff = min(backoff_base_seconds() * (2**exponent), backoff_max_seconds())
    _LOCKED_UNTIL[key] = time.monotonic() + backoff
    return backoff


def clear_failures(key: str) -> None:
    _FAILURES.pop(key, None)
    _LOCKED_UNTIL.pop(key, None)


def reset_lockouts() -> None:
    """Drop all lockout state (tests, and an operator unblocking a locked-out user)."""
    _FAILURES.clear()
    _LOCKED_UNTIL.clear()


def _prune_lockouts() -> None:
    """Keep the lockout maps bounded under a spoofed-IP flood."""
    if len(_FAILURES) <= MAX_LOCKOUT_ENTRIES:
        return
    now = time.monotonic()
    for key in [k for k, until in _LOCKED_UNTIL.items() if until <= now]:
        _FAILURES.pop(key, None)
        _LOCKED_UNTIL.pop(key, None)
    # Still too many: drop the oldest failures (they are the ones with the least
    # information left — the backoff for them has either fired or expired).
    if len(_FAILURES) > MAX_LOCKOUT_ENTRIES:
        for key, _ in sorted(_FAILURES.items(), key=lambda item: item[1][1])[
            : len(_FAILURES) - MAX_LOCKOUT_ENTRIES
        ]:
            _FAILURES.pop(key, None)
            _LOCKED_UNTIL.pop(key, None)


def failure_count(key: str) -> int:
    return _FAILURES.get(key, (0, 0.0))[0]


# ------------------------------------------------------------------ principals


def _principal_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """A principal as the API may see it — never the secret or its hash."""
    return {
        "principal_id": record.get("principal_id"),
        "name": record.get("name"),
        "role": record.get("role"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "revoked_at": record.get("revoked_at"),
        "last_seen_at": record.get("last_seen_at"),
        "active": bool(
            record.get("principal_id")
            and not record.get("revoked_at")
            and not _is_expired(record.get("expires_at"))
        ),
    }


def _is_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= utcnow()
    except ValueError:
        # A malformed expiry fails closed.
        return True


def create_principal(
    name: str,
    role: str,
    password: str,
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a principal. Raises ValueError for a bad name/role/password."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    if len(clean_name) > 64 or any(ch in clean_name for ch in ":\n\r\t "):
        raise ValueError(
            "name must be 1-64 characters and must not contain whitespace or ':' "
            "(it is used as the HTTP Basic username)"
        )
    clean_role = normalize_role(role)
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    if expires_at:
        try:
            datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO-8601 timestamp") from exc

    principal_id = "prin_" + secrets.token_hex(8)
    record = PRINCIPALS.create(
        principal_id,
        clean_name,
        clean_role,
        hash_password(password),
        expires_at=expires_at or None,
    )
    audit_event(
        "admin.principal.created",
        kind="admin_auth",
        result="ok",
        principal=clean_name,
        role=clean_role,
        principal_id=principal_id,
    )
    logger.info(f"admin principal {clean_name!r} created with role {clean_role}")
    return _principal_view(record)


def list_principals() -> List[Dict[str, Any]]:
    return [_principal_view(record) for record in PRINCIPALS.list_all()]


def principal_names() -> Dict[str, str]:
    """`principal_id -> name`, for labelling sessions without N queries."""
    return {
        record["principal_id"]: record["name"]
        for record in PRINCIPALS.list_all()
        if record.get("principal_id")
    }


def revoke_principal(principal_id: str) -> Dict[str, Any]:
    """Revoke a principal and every session it holds. Returns a summary."""
    record = PRINCIPALS.get(principal_id)
    if record is None:
        raise LookupError("principal not found")
    revoked = PRINCIPALS.revoke(principal_id)
    sessions = SESSIONS.revoke_principal(principal_id)
    if revoked:
        audit_event(
            "admin.principal.revoked",
            kind="admin_auth",
            result="ok",
            principal=record["name"],
            role=record["role"],
            principal_id=principal_id,
            sessions_revoked=sessions,
        )
        logger.warning(
            f"admin principal {record['name']!r} revoked; {sessions} session(s) closed"
        )
    return {
        "principal_id": principal_id,
        "name": record["name"],
        "revoked": bool(revoked),
        "sessions_revoked": sessions,
    }


# ------------------------------------------------------------------- sessions


def _session_expiry() -> str:
    return (utcnow() + timedelta(seconds=session_ttl_seconds())).isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _session_expired(session: Dict[str, Any]) -> Optional[str]:
    """Return a failure reason when the session is no longer usable."""
    if session.get("revoked_at"):
        return REASON_SESSION_REVOKED
    expires = _parse(session.get("expires_at"))
    if expires is None or expires <= utcnow():
        return REASON_SESSION_EXPIRED
    last = _parse(session.get("last_seen_at"))
    if last is not None and (utcnow() - last).total_seconds() > session_idle_seconds():
        return REASON_SESSION_EXPIRED
    return None


def session_view(session: Dict[str, Any], names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A session as the API may see it — never the token or its hash."""
    view = {
        "session_id": (session.get("session_hash") or "")[:12],
        "principal_id": session.get("principal_id"),
        "created_at": session.get("created_at"),
        "last_seen_at": session.get("last_seen_at"),
        "expires_at": session.get("expires_at"),
        "client_ip": session.get("client_ip"),
        "user_agent": session.get("user_agent"),
        "expired": _session_expired(session) is not None,
    }
    if names is not None:
        view["principal"] = names.get(session.get("principal_id", ""), "")
    return view


def login(
    name: str,
    password: str,
    request: Request,
) -> Dict[str, Any]:
    """Exchange a name+password for a session token.

    Raises `HTTPException` 401 (bad credentials), 403 (revoked/expired) or 429
    (locked out, with `Retry-After`). Never distinguishes "no such principal" from
    "wrong password" in the response body.
    """
    ip = client_ip(request)
    identity = (name or "").strip() or "admin"
    key = lockout_key(identity, ip)

    wait = locked_for(key)
    if wait:
        PROM_ADMIN_AUTH_FAILURES.labels(reason=REASON_LOCKED_OUT).inc()
        audit_event(
            "admin.login.denied",
            kind="admin_auth",
            result="denied",
            reason=REASON_LOCKED_OUT,
            principal=identity,
            client=ip,
            retry_after=wait,
        )
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts; try again later",
            headers={"Retry-After": str(wait)},
        )

    record = PRINCIPALS.by_name(identity)
    secret = password or ""

    if record is not None:
        if record.get("revoked_at"):
            _login_failed(key, identity, ip, REASON_REVOKED)
            raise HTTPException(status_code=403, detail="principal is revoked")
        if _is_expired(record.get("expires_at")):
            _login_failed(key, identity, ip, REASON_EXPIRED)
            raise HTTPException(status_code=403, detail="principal has expired")
        if not verify_password(secret, record.get("secret_hash", "")):
            _login_failed(key, identity, ip, REASON_BAD_SECRET)
            raise HTTPException(status_code=401, detail="invalid credentials")
        principal = Principal(
            name=record["name"],
            role=record["role"],
            principal_id=record["principal_id"],
            method="password",
        )
    else:
        # Unknown name: burn the same time as a real verification, then decide.
        verify_password(secret, _dummy_hash())
        legacy = legacy_secret()
        if not legacy or not _secret_matches(secret, legacy):
            reason = REASON_UNKNOWN_PRINCIPAL if legacy else REASON_BAD_SECRET
            _login_failed(key, identity, ip, reason)
            raise HTTPException(status_code=401, detail="invalid credentials")
        # The shared ADMIN_PASSWORD: one synthesised admin principal, no row.
        principal = Principal(name="admin", role=ROLE_ADMIN, method="legacy")

    clear_failures(key)
    token = SESSION_PREFIX + secrets.token_urlsafe(SESSION_BYTES)
    session = SESSIONS.create(
        hash_session(token),
        principal.principal_id or ("legacy" if principal.method == "legacy" else "unknown"),
        _session_expiry(),
        client_ip=ip,
        user_agent=request.headers.get("User-Agent", ""),
    )
    if principal.principal_id:
        PRINCIPALS.touch(principal.principal_id)
    PROM_ADMIN_LOGINS.labels(method=principal.method).inc()
    audit_event(
        "admin.login.ok",
        kind="admin_auth",
        result="ok",
        principal=principal.name,
        role=principal.role,
        method=principal.method,
        client=ip,
    )
    return {
        "token": token,
        "token_type": "bearer",
        "expires_at": session.get("expires_at"),
        "idle_timeout_seconds": session_idle_seconds(),
        "ttl_seconds": session_ttl_seconds(),
        "principal": {"name": principal.name, "role": principal.role},
        "note": (
            "Send `Authorization: Bearer <token>`. Shown once and stored as a "
            "SHA-256 hash; log out with DELETE /auth/session."
        ),
    }


def _login_failed(key: str, identity: str, ip: str, reason: str) -> None:
    backoff = record_failure(key, reason)
    audit_event(
        "admin.login.denied",
        kind="admin_auth",
        result="denied",
        reason=reason,
        principal=identity,
        client=ip,
        backoff_seconds=backoff or None,
    )


def logout(principal: Principal) -> Dict[str, Any]:
    """Revoke the caller's own session. Idempotent."""
    if not principal.session_hash:
        raise ValueError("not authenticated with a session token")
    revoked = SESSIONS.revoke(principal.session_hash)
    audit_event(
        "admin.logout",
        kind="admin_auth",
        result="ok" if revoked else "noop",
        principal=principal.name,
        role=principal.role,
    )
    return {"logged_out": bool(revoked)}


def list_sessions() -> List[Dict[str, Any]]:
    """Every live session, labelled with its principal — admin only.

    Iterates sessions rather than principals so sessions opened with the legacy
    shared password (which has no principal row) are visible and revocable too.
    """
    names = principal_names()
    names.setdefault("legacy", "admin (shared password)")
    return [session_view(session, names) for session in SESSIONS.list_all()]


def revoke_session(session_id: str) -> Dict[str, Any]:
    """Revoke a session by the short id shown in `GET /auth/sessions`."""
    prefix = (session_id or "").strip()
    if len(prefix) < 6:
        raise ValueError("session id must be at least 6 characters")
    names = principal_names()
    names.setdefault("legacy", "admin (shared password)")
    for session in SESSIONS.list_all():
        session_hash = session["session_hash"]
        if session_hash.startswith(prefix):
            SESSIONS.revoke(session_hash)
            view = session_view(session, names)
            audit_event(
                "admin.session.revoked",
                kind="admin_auth",
                result="ok",
                principal=view.get("principal"),
                session_id=prefix,
            )
            return {"session_id": prefix, "revoked": True, **view}
    raise LookupError("session not found")


# ------------------------------------------------------------- authenticate


def _touch_principal(record: Dict[str, Any]) -> None:
    """Record `last_seen_at`, at most once a minute (Basic auth has no session row)."""
    if not record.get("principal_id"):
        return
    last = _parse(record.get("last_seen_at"))
    if last is None or (utcnow() - last).total_seconds() >= TOUCH_INTERVAL_SECONDS:
        PRINCIPALS.touch(record["principal_id"])


def _principal_from_record(record: Dict[str, Any]) -> Principal:
    return Principal(
        name=record["name"],
        role=record["role"],
        principal_id=record["principal_id"],
    )


def _authenticate_session(token: str) -> Tuple[Optional[Principal], Optional[str]]:
    session_hash = hash_session(token)
    session = SESSIONS.get(session_hash)
    if session is None:
        return None, REASON_SESSION_UNKNOWN
    reason = _session_expired(session)
    if reason is not None:
        return None, reason

    principal_id = session.get("principal_id", "")
    if principal_id in ("", "legacy"):
        # A session opened with the shared ADMIN_PASSWORD. It dies with the
        # password: if no legacy secret is configured any more, so is the session.
        if not legacy_secret():
            return None, REASON_SESSION_REVOKED
        principal = Principal(name="admin", role=ROLE_ADMIN, method="legacy")
    else:
        record = PRINCIPALS.get(principal_id)
        if record is None:
            return None, REASON_SESSION_UNKNOWN
        if record.get("revoked_at"):
            return None, REASON_REVOKED
        if _is_expired(record.get("expires_at")):
            return None, REASON_EXPIRED
        principal = _principal_from_record(record)

    base = Principal(
        name=principal.name,
        role=principal.role,
        principal_id=principal.principal_id,
        method="session",
        session_hash=session_hash,
    )
    last = _parse(session.get("last_seen_at"))
    if last is None or (utcnow() - last).total_seconds() >= TOUCH_INTERVAL_SECONDS:
        # One write per session per minute, not per request (see TOUCH_INTERVAL_SECONDS).
        SESSIONS.touch(session_hash)
        if base.principal_id:
            PRINCIPALS.touch(base.principal_id)
    return base, None


def _decode_basic(header: str) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    if not header.startswith("Basic "):
        return None, REASON_MALFORMED
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return None, REASON_MALFORMED
    name, _, password = decoded.partition(":")
    return (name, password), None


def authenticate(request: Request) -> Tuple[Optional[Principal], Optional[str]]:
    """Resolve the caller. Returns `(principal, None)` or `(None, reason)`.

    Accepted credentials, in order: `Authorization: Bearer <session token>`, then
    HTTP Basic (`name:password` against a principal, or any name + the legacy
    `ADMIN_PASSWORD`). No credential at all in read-only mode resolves to an
    anonymous `viewer` — see `require_roles`.
    """
    header = (request.headers.get(SESSION_HEADER) or "").strip()
    if not header:
        return None, REASON_MISSING

    scheme, _, value = header.partition(" ")
    scheme = scheme.strip().lower()

    if scheme == SESSION_SCHEME:
        if not value.strip():
            return None, REASON_MALFORMED
        return _authenticate_session(value.strip())

    if scheme == "basic":
        credentials, reason = _decode_basic(header)
        if credentials is None:
            return None, reason
        name, password = credentials
        return _authenticate_basic(name.strip(), password)

    return None, REASON_MALFORMED


def _authenticate_basic(name: str, password: str) -> Tuple[Optional[Principal], Optional[str]]:
    record = PRINCIPALS.by_name(name) if name else None
    if record is not None:
        if record.get("revoked_at"):
            return None, REASON_REVOKED
        if _is_expired(record.get("expires_at")):
            return None, REASON_EXPIRED
        if not verify_password(password, record.get("secret_hash", "")):
            return None, REASON_BAD_SECRET
        _touch_principal(record)
        return (
            Principal(
                name=record["name"],
                role=record["role"],
                principal_id=record["principal_id"],
                method="password",
            ),
            None,
        )

    verify_password(password, _dummy_hash())
    legacy = legacy_secret()
    if not legacy or not _secret_matches(password, legacy):
        return None, REASON_UNKNOWN_PRINCIPAL if legacy else REASON_BAD_SECRET
    # `-u :password`, `-u admin:password` and `-u anything:password` all work with
    # the legacy secret — that is what existing clients send.
    return Principal(name="admin", role=ROLE_ADMIN, method="legacy"), None


def _lockout_subject(request: Request) -> Tuple[str, str]:
    """`(lockout_key, identity)` for a credential that can be guessed.

    Only HTTP Basic can be brute-forced here: a session token is 256 random bits,
    so counting failures for it would just fill the map. `identity` is the name in
    the Basic header (empty for the legacy `:password` form).
    """
    header = (request.headers.get(SESSION_HEADER) or "").strip()
    if not header.lower().startswith("basic "):
        return "", ""
    credentials, _ = _decode_basic(header)
    name = (credentials[0].strip() if credentials else "") or "admin"
    return lockout_key(name, client_ip(request)), name


def current_principal(request: Request) -> Optional[Principal]:
    """Resolve without raising — for `/auth/me`-style informational routes."""
    principal, _ = authenticate(request)
    return principal


# ---------------------------------------------------------------- enforcement


def require_roles(request: Request, required: str) -> Principal:
    """Enforce `required` and return the caller. Raises `HTTPException` otherwise.

    - no credential and nothing configured → anonymous `viewer`, so a read-only
      deployment keeps working for reads and refuses every write (403)
    - no credential, credential configured → 401 with `WWW-Authenticate`
    - authenticated but role too low → 403, counted and audited
    """
    required = normalize_role(required)
    key, identity = _lockout_subject(request)

    # Same lockout as `POST /auth/session`: without this, `/auth/verify` (or any
    # Basic-authenticated route) would be a free, unlimited credential oracle.
    if key:
        wait = locked_for(key)
        if wait:
            PROM_ADMIN_AUTH_FAILURES.labels(reason=REASON_LOCKED_OUT).inc()
            audit_event(
                "admin.auth.locked_out",
                kind="admin_auth",
                result="denied",
                reason=REASON_LOCKED_OUT,
                principal=identity,
                required_role=required,
                client=client_ip(request),
                retry_after=wait,
            )
            raise HTTPException(
                status_code=429,
                detail="too many failed attempts; try again later",
                headers={"Retry-After": str(wait)},
            )

    principal, reason = authenticate(request)

    if principal is None:
        if key and reason in (REASON_BAD_SECRET, REASON_UNKNOWN_PRINCIPAL):
            record_failure(key, reason)
        if not configured():
            if role_satisfies(ROLE_VIEWER, required):
                return Principal(name="anonymous", role=ROLE_VIEWER, method="anonymous")
            PROM_ADMIN_AUTH_FAILURES.labels(reason=REASON_MISSING).inc()
            audit_event(
                "admin.auth.denied",
                kind="admin_auth",
                result="denied",
                reason="read_only_mode",
                required_role=required,
                client=client_ip(request),
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    "admin auth is not configured: this deployment is read-only. Set "
                    "ADMIN_PASSWORD (deprecated) or create a principal to enable "
                    "privileged actions"
                ),
            )
        if reason:
            PROM_ADMIN_AUTH_FAILURES.labels(reason=reason).inc()
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="opamp", Bearer'},
        )

    if not role_satisfies(principal.role, required):
        PROM_ADMIN_AUTH_FAILURES.labels(reason=REASON_ROLE).inc()
        audit_event(
            "admin.auth.denied",
            kind="admin_auth",
            result="denied",
            reason=REASON_ROLE,
            principal=principal.name,
            role=principal.role,
            required_role=required,
            client=client_ip(request),
        )
        raise HTTPException(
            status_code=403,
            detail=f"role {principal.role!r} cannot perform this action "
            f"(requires {required!r})",
        )

    return principal


def require_role(required: str):
    """FastAPI dependency form: `Depends(require_role("operator"))`."""

    def dependency(request: Request) -> Principal:
        return require_roles(request, required)

    dependency.__name__ = f"require_{required}"
    return dependency


def require_admin(request: Request) -> bool:
    """Backwards-compatible alias for `require_role("admin")` (#95).

    Kept because it predates roles and is called directly by the routes and the
    Streamlit pages; new code should use `require_role(...)`.
    """
    require_roles(request, ROLE_ADMIN)
    return True


# ------------------------------------------------------------------- startup


def warn_if_legacy() -> None:
    """Startup hint for deployments still using the shared password (#95)."""
    if legacy_secret() and PRINCIPALS.count_live() == 0:
        logger.warning(
            "admin auth uses the shared ADMIN_PASSWORD secret: there is no per-person "
            "identity, no roles and no way to revoke one person's access. Create "
            "principals with `POST /auth/principals` (or `opampctl auth principals "
            "create`) and drop ADMIN_PASSWORD when done."
        )


def warn_if_read_only() -> None:
    """Startup warning for the read-open, write-closed mode."""
    if not configured():
        logger.warning(
            "no admin credential configured: read-only mode. Reads are open and every "
            "privileged write answers 403 until you set ADMIN_PASSWORD or create a "
            "principal."
        )
