"""Admin session handling for the Streamlit UI (#105) — **streamlit-free**.

The UI used to hold the admin *password* in `st.session_state` for the life of the
browser tab and re-send HTTP Basic on every request, with the login form
copy-pasted into three places. This module is the one place that knows how a UI
becomes (and stops being) an admin session: it logs in through `POST
/auth/session` (#95), keeps the opaque **token**, and classifies what came back.

Deliberately streamlit-free, for the same reason `ui/config_view.py` is (#68): the
UI dependency stack is not part of the test environment, so anything that decides
*"is this caller allowed"* must be testable with plain pytest. `ui/shared.py`
imports this and adds the widgets; the `st.*` calls stay there.

A warning about the role ladder below: it mirrors `server.auth.ROLE_LEVELS` and is
**not** imported from it. Importing `server.auth` constructs the principal/session
SQLite stores against `data/opamp.db`, and the UI container mounts `./data`
read-only — an import that tries to create tables there fails at startup. The
parity is enforced by a test (`tests/test_ui_session.py`) instead, because a UI
that quietly disagrees with the server about roles is worse than no gating at all.

**The UI is a hint, never the boundary.** Hiding a button is cosmetics; every
privileged call is re-authorised by the server, and the audit log records the
server's view of the caller, not the UI's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import requests

# --------------------------------------------------------------------- roles

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_ADMIN = "admin"

# Ordered, exactly like `server.auth.ROLE_LEVELS`: a role implies everything below it.
ROLE_LEVELS: Dict[str, int] = {
    ROLE_VIEWER: 1,
    ROLE_OPERATOR: 2,
    ROLE_APPROVER: 3,
    ROLE_ADMIN: 4,
}
ROLES = tuple(ROLE_LEVELS)


def normalize_role(role: str) -> str:
    value = (role or "").strip().lower()
    if value not in ROLE_LEVELS:
        raise ValueError(f"role must be one of {', '.join(ROLES)}; got {role!r}")
    return value


def role_satisfies(have: str, required: str) -> bool:
    """Is `have` at or above `required`? Unknown roles satisfy nothing."""
    return ROLE_LEVELS.get((have or "").strip().lower(), 0) >= ROLE_LEVELS.get(
        (required or "").strip().lower(), 99
    )


# ------------------------------------------------------------------- headers


def session_headers(token: str) -> Dict[str, str]:
    """The only credential form this UI sends: `Authorization: Bearer <token>`.

    HTTP Basic is deliberately absent — it is what put the password back on the
    wire on every request (#105), and the token is revocable, expiring and
    attributable in a way a shared password never is.
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("an admin session token is required")
    return {"Authorization": f"Bearer {token}"}


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- session


@dataclass(frozen=True)
class AdminSession:
    """What the UI remembers after login: the token and who it belongs to.

    There is no password field, and there is not going to be one. `__repr__` and
    `__str__` redact the token so it cannot end up in a traceback, an
    `st.write(session)` or a log line — the same rule as `server.redaction` for
    config bodies, applied to the one other secret the UI holds.
    """

    token: str
    name: str
    role: str
    expires_at: Optional[str] = None
    idle_timeout_seconds: int = 0
    method: str = "session"
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not (self.token or "").strip():
            raise ValueError("login response contained no token")
        if self.role not in ROLE_LEVELS:
            raise ValueError(
                f"login response carried an unknown role {self.role!r}; "
                f"expected one of {', '.join(ROLES)}"
            )
        object.__setattr__(self, "role", normalize_role(self.role))

    # -- redaction ---------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - trivial, but must never leak
        return (
            f"AdminSession(name={self.name!r}, role={self.role!r}, "
            f"expires_at={self.expires_at!r}, token=<redacted>)"
        )

    def __str__(self) -> str:
        return self.__repr__()

    # -- expiry ------------------------------------------------------------
    def expires_in(self, now: Optional[datetime] = None) -> Optional[int]:
        """Seconds until the absolute expiry; `None` when the server sent none."""
        expires = _parse_timestamp(self.expires_at)
        if expires is None:
            return None
        return int((expires - (now or _utcnow())).total_seconds())

    def expired(self, now: Optional[datetime] = None) -> bool:
        remaining = self.expires_in(now)
        return remaining is not None and remaining <= 0

    def expiry_label(self, now: Optional[datetime] = None) -> str:
        """Human sentence for the sidebar — never a negative countdown."""
        remaining = self.expires_in(now)
        if remaining is None:
            return "expiry unknown"
        if remaining <= 0:
            return "session expired"
        minutes, seconds = divmod(remaining, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"expires in {hours}h {minutes:02d}m"
        if minutes:
            return f"expires in {minutes}m {seconds:02d}s"
        return f"expires in {seconds}s"

    # -- authorisation (cosmetic) ------------------------------------------
    def can(self, required: str) -> bool:
        return role_satisfies(self.role, required)

    def label(self) -> str:
        return f"{self.name} · {self.role}"

    # -- construction ------------------------------------------------------
    @classmethod
    def from_login(cls, payload: Mapping[str, Any]) -> "AdminSession":
        """Build from a `POST /auth/session` body. Raises `ValueError` if malformed."""
        if not isinstance(payload, Mapping):
            raise ValueError("login response was not a JSON object")
        principal = payload.get("principal") or {}
        if not isinstance(principal, Mapping):
            principal = {}
        name = principal.get("name") or payload.get("name") or "admin"
        role = principal.get("role") or payload.get("role") or ""
        return cls(
            token=str(payload.get("token") or ""),
            name=str(name),
            role=str(role),
            expires_at=payload.get("expires_at"),
            idle_timeout_seconds=int(payload.get("idle_timeout_seconds") or 0),
            method=str(payload.get("method") or "session"),
        )


# -------------------------------------------------------------------- errors

# Stable `kind` values. The UI switches on these, so they are the contract
# (`tests/test_ui_session.py` pins them the way `server.auth.REASONS` is pinned).
KIND_INVALID = "invalid"          # 401 — wrong name/password
KIND_FORBIDDEN = "forbidden"      # 403 — right password, no permission (or revoked)
KIND_LOCKED_OUT = "locked_out"    # 429 — brute-force backoff, retry later
KIND_READ_ONLY = "read_only"      # 403 with the read-only detail from #95
KIND_AUDIT = "audit_unavailable"  # 503 — fail-closed audit log (#97)
KIND_UNAVAILABLE = "unavailable"  # 5xx — server broke
KIND_NETWORK = "network"          # no answer at all

KINDS = (
    KIND_INVALID,
    KIND_FORBIDDEN,
    KIND_LOCKED_OUT,
    KIND_READ_ONLY,
    KIND_AUDIT,
    KIND_UNAVAILABLE,
    KIND_NETWORK,
)

READ_ONLY_HINT = "read-only"


class AuthError(Exception):
    """A login or admin request that did not authenticate.

    Carries enough for the UI to say something specific *without* leaking whether
    a principal exists (the server never distinguishes "no such principal" from
    "wrong password", and the UI must not invent that distinction either).
    """

    def __init__(
        self,
        kind: str,
        message: str,
        status: Optional[int] = None,
        retry_after: Optional[int] = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.detail = detail

    def human(self) -> str:
        """One sentence for `st.error` — includes the retry delay when locked out."""
        if self.kind == KIND_LOCKED_OUT and self.retry_after:
            return f"{self.message} Try again in {format_delay(self.retry_after)}."
        return self.message


def format_delay(seconds: int) -> str:
    """`30` → "30 seconds", `900` → "15 minutes". Used for 429 copy."""
    seconds = max(int(seconds or 0), 1)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f"{minutes}m {remainder}s"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def retry_after_seconds(
    headers: Optional[Mapping[str, Any]] = None,
    payload: Optional[Mapping[str, Any]] = None,
    default: int = 30,
) -> int:
    """Seconds to wait from a 429: `Retry-After` header first, body as fallback.

    The server sends both (`Retry-After` header and `retry_after` in the audit
    detail), and proxies are allowed to strip or rewrite headers, so neither is
    trusted alone.
    """
    header = ""
    if headers:
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                header = str(value or "").strip()
                break
    if header:
        try:
            return max(int(float(header)), 1)
        except ValueError:
            pass
    if payload:
        for key in ("retry_after", "retry_after_seconds"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
    return max(int(default or 1), 1)


def _detail_text(payload: Any) -> str:
    """Pull a readable `detail` out of an error body without dumping the whole thing."""
    if isinstance(payload, Mapping):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail.strip()
        if isinstance(detail, list):  # FastAPI validation errors
            parts = [
                str(item.get("msg", "")).strip()
                for item in detail
                if isinstance(item, Mapping)
            ]
            return "; ".join(part for part in parts if part)
        if detail is not None:
            return str(detail).strip()
    return ""


def _json_or_none(response: Any) -> Optional[Mapping[str, Any]]:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


# ------------------------------------------------------------------ outcomes


@dataclass(frozen=True)
class Outcome:
    """What to do with a response — the single 401/403/429/503 decision (#105).

    `relogin` is the important field: **401 means the token is gone** (expired,
    revoked, or never valid) and the UI must return to the login form, whereas
    **403 means the token is fine and the role is not**, so dropping it would log
    the user out for clicking a button they cannot use.
    """

    kind: str
    ok: bool
    message: str = ""
    retry_after: Optional[int] = None
    relogin: bool = False
    status: Optional[int] = None


def classify_response(
    status_code: int,
    headers: Optional[Mapping[str, Any]] = None,
    payload: Optional[Mapping[str, Any]] = None,
    required_role: str = "",
) -> Outcome:
    """Classify an admin API response. Pure — this is what the tests drive."""
    detail = _detail_text(payload)
    needed = f" (it requires the {required_role!r} role)" if required_role else ""

    if 200 <= int(status_code) < 300:
        return Outcome(kind="ok", ok=True, status=int(status_code))

    if status_code == 401:
        return Outcome(
            kind=KIND_INVALID,
            ok=False,
            status=401,
            relogin=True,
            message=(
                "Your admin session is no longer valid (it expired, was signed out, "
                "or the server restarted). Please sign in again."
            ),
        )

    if status_code == 403:
        if READ_ONLY_HINT in detail.lower():
            # #95 read-only mode: no credential is configured at all.
            return Outcome(
                kind=KIND_READ_ONLY,
                ok=False,
                status=403,
                message=(
                    "This server has no admin credential configured, so it is "
                    "read-only. Set ADMIN_PASSWORD (deprecated) or create a "
                    "principal, then restart."
                ),
            )
        return Outcome(
            kind=KIND_FORBIDDEN,
            ok=False,
            status=403,
            message=(
                detail
                or f"You are not allowed to perform this action{needed}."
            ),
        )

    if status_code == 429:
        wait = retry_after_seconds(headers, payload)
        return Outcome(
            kind=KIND_LOCKED_OUT,
            ok=False,
            status=429,
            retry_after=wait,
            message=(
                "Too many failed attempts — sign-in is temporarily locked. "
                f"Try again in {format_delay(wait)}."
            ),
        )

    if status_code == 503:
        return Outcome(
            kind=KIND_AUDIT,
            ok=False,
            status=503,
            message=(
                detail
                or "The audit log is unavailable, so this privileged action was "
                "refused (#97). Fix the audit store before trying again."
            ),
        )

    return Outcome(
        kind=KIND_UNAVAILABLE,
        ok=False,
        status=int(status_code),
        message=detail or f"The server answered {status_code}.",
    )


# --------------------------------------------------------------------- login

DEFAULT_TIMEOUT = 10


def login(
    base_url: str,
    name: str,
    password: str,
    post: Any = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> AdminSession:
    """Exchange name+password for a session token via `POST /auth/session`.

    The password exists for the duration of this call and is never returned,
    stored or attached to the session — the caller's only handle on the new
    session is the token (`#105`: "the password is never kept in session state").

    `post` is injectable so this is testable without a server; it must behave like
    `requests.post`.
    """
    post = post or requests.post
    url = f"{str(base_url).rstrip('/')}/auth/session"
    body = {"name": name or "", "password": password or ""}

    try:
        response = post(url, json=body, timeout=timeout)
    except Exception as exc:  # requests raises a family of transport errors
        raise AuthError(
            KIND_NETWORK,
            f"Could not reach the server at {base_url}: {exc}",
            detail=str(exc),
        ) from exc

    status = int(getattr(response, "status_code", 0) or 0)
    payload = _json_or_none(response)

    if status in (200, 201):
        try:
            return AdminSession.from_login(payload or {})
        except ValueError as exc:
            raise AuthError(
                KIND_UNAVAILABLE,
                f"The server's login response could not be read: {exc}",
                status=status,
            ) from exc

    if status == 429:
        wait = retry_after_seconds(getattr(response, "headers", None), payload)
        raise AuthError(
            KIND_LOCKED_OUT,
            "Too many failed sign-in attempts.",
            status=429,
            retry_after=wait,
            detail=_detail_text(payload),
        )

    if status == 403:
        detail = _detail_text(payload)
        kind = KIND_READ_ONLY if READ_ONLY_HINT in detail.lower() else KIND_FORBIDDEN
        raise AuthError(
            kind,
            detail or "These credentials are not allowed to sign in.",
            status=403,
            detail=detail,
        )

    if status == 401:
        # Never say *which* half was wrong: the server does not, so the UI must not
        # turn itself into the enumeration oracle the server refuses to be.
        raise AuthError(
            KIND_INVALID,
            "Invalid credentials.",
            status=401,
            detail=_detail_text(payload),
        )

    raise AuthError(
        KIND_UNAVAILABLE,
        _detail_text(payload) or f"Sign-in failed: the server answered {status}.",
        status=status,
        detail=_detail_text(payload),
    )


def logout(base_url: str, token: str, delete: Any = None, timeout: int = 5) -> bool:
    """Revoke this session (`DELETE /auth/session`). Best-effort; never raises.

    Returns whether the server confirmed. The UI clears local state regardless —
    a session whose token cannot be revoked must still not stay in the browser.
    """
    if not (token or "").strip():
        return False
    delete = delete or requests.delete
    try:
        response = delete(
            f"{str(base_url).rstrip('/')}/auth/session",
            headers=session_headers(token),
            timeout=timeout,
        )
    except Exception:
        return False
    return 200 <= int(getattr(response, "status_code", 0) or 0) < 300


def sessions_of(session: AdminSession, payload: Mapping[str, Any]) -> list:
    """Sessions belonging to the same principal, from `GET /auth/sessions`.

    Used by "sign out everywhere": revoking *other people's* sessions is
    `DELETE /auth/principals/{id}` — an admin decision about a person, not
    something a sign-out button should do behind the user's back.

    Sessions opened with the shared `ADMIN_PASSWORD` have no principal row, so they
    are labelled `"admin (shared password)"` with `principal_id="legacy"` (#95).
    Those are matched on the id, not on the label, so "sign out everywhere" for the
    shared credential really does close every shared-credential session.
    """
    others = []
    for record in (payload or {}).get("sessions") or []:
        if not isinstance(record, Mapping) or not record.get("session_id"):
            continue
        legacy = session.method == "legacy" and record.get("principal_id") == "legacy"
        if legacy or str(record.get("principal") or "") == session.name:
            others.append(record)
    return others
