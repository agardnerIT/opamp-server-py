import asyncio
import binascii
import json
import os
from contextlib import nullcontext
from pathlib import Path
import prometheus_client as prom_client
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, Query, WebSocket, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from google.protobuf.json_format import MessageToDict
from loguru import logger
from typing import Optional, Dict, Any, List, Literal
from functools import wraps

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# #108: configure loguru before importing anything that logs at import time (the
# alert store, the persistent agent registry). In a container this is what makes
# every line JSON, including the first one.
from server import logging as server_logging  # noqa: E402
from server.logging import RequestContextMiddleware  # noqa: E402

server_logging.configure_logging()

from proto.opamp_pb2 import AgentCapabilities, AgentToServer, ServerToAgent, ServerCapabilities, ServerToAgentFlags
from server.state import (
    AgentRegistry,
    AgentState,
    AGENT_REGISTRY,
    SQLiteMetricsStore,
    _normalise_audit_ts,
    utcnow,
)
from server.manifest import (
    generate_manifest,
    generate_ocb_command,
    DEFAULT_VERSION,
    validate_manifest_version,
)
from server.opa_client import evaluate_agent_compliance, get_available_policies, get_policy_validation, OPA_ENABLED, OPA_URL
from server.reports import (
    generate_agent_report,
    generate_heavy_collectors_report,
    generate_outdated_collectors_report,
    _count_outdated_collectors,
    _is_heavy,
)
from server.alerts import get_alert_config, update_alert_config, send_test_alert, send_alert, ALERT_TYPES, ALERT_CONFIG, ALERT_EVENTS, send_new_agent_alert, send_stale_agent_alert, send_compliance_alert, redact_config, merge_redacted
from server import (
    admin_auth,
    agent_auth,
    audit,
    auth,
    config_control,
    config_validation,
    crypto,
    posture,
    redaction,
    remote_config,
    tracing,
)
from server.redaction import redact_document
from server.auth import (  # noqa: F401  (privileged routes below)
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    audit_action,
    require_admin,
    require_audit,
    require_roles,
    role_satisfies,
)
from server.framing import message_limit
from server.ws import (
    CLOSE_POLICY_VIOLATION,
    REGISTRY as WS_REGISTRY,
    ProcessedMessage,
    origin_allowed,
    serve as serve_websocket,
    ws_enabled,
)

# #69: a stale timeout must follow the agent's *observed* reporting cadence, not a
# fixed guess. The floor covers collectors on their 30s default (and any agent that
# reports more often than that); beyond it we expire at `margin × observed gap`, so a
# `polling_interval: 120s` agent is not deleted and re-registered every minute.
AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", 180))
AGENT_TIMEOUT_INTERVAL_MARGIN = float(os.environ.get("AGENT_TIMEOUT_INTERVAL_MARGIN", 3))
# Until one full gap has been observed, "polling every 10 minutes" and "died on
# startup" are indistinguishable, so the first contact gets a generous window.
AGENT_FIRST_CONTACT_SECONDS = float(os.environ.get("AGENT_FIRST_CONTACT_SECONDS", 900))
_cleanup_task = None


def _log_audit_startup() -> None:
    """One startup line: is the audit log on, and how much is in it (#97).

    The count (not a full `verify()`) is deliberate: verification is O(n) and this
    runs on every restart, while `/audit/verify` is the on-demand answer.
    """
    if not audit.enabled():
        logger.bind(event="audit.disabled").warning(
            "audit log is DISABLED (OPAMP_AUDIT_ENABLED=false): privileged actions are "
            "refused and only AUDIT log lines remain"
        )
        return
    try:
        bounds = audit.STORE.bounds()
    except Exception as exc:  # pragma: no cover - defensive
        logger.bind(event="audit.unavailable", error=str(exc)).warning(
            f"audit log unavailable at startup: {exc}"
        )
        return
    logger.bind(event="audit.ready", entries=bounds["count"]).info(
        f"audit log: {bounds['count']} entries"
        + (f" ({bounds['first_ts']} .. {bounds['last_ts']})" if bounds["count"] else "")
        + f", retention {audit.retention_days()}d; verify with GET /audit/verify"
    )


async def periodic_cleanup():
    while True:
        await asyncio.sleep(15)
        cleanup_stale_agents()
        # Retention runs at most hourly (the tick is every 15s) and appends an
        # `audit.checkpoint` row so the chain stays verifiable (#97).
        audit.maybe_prune()
        # #98: superseded config bodies age out; hashes and authors stay. The
        # current revision is never pruned, or a restart would forget the fleet.
        try:
            remote_config.REGISTRY.prune()
        except Exception as exc:  # pragma: no cover - defensive
            logger.bind(event="store.write_failed", operation="config_prune").warning(
                f"config retention prune failed: {exc}"
            )
        # #99: drift is recomputed here, not on the message path — an alert is a
        # network call and one alert per poll is the flood the issue rules out. The
        # `include_future` sweep lives here too, so a later-joining agent inherits a
        # proposal only when the operator explicitly opted in.
        try:
            config_control.sweep_drift(AGENT_REGISTRY.list_all())
        except Exception as exc:  # pragma: no cover - defensive
            logger.bind(event="store.write_failed", operation="drift_sweep").warning(
                f"config drift sweep failed: {exc}"
            )
        try:
            config_control.reconcile_future(AGENT_REGISTRY.list_all())
        except Exception as exc:  # pragma: no cover - defensive
            logger.bind(
                event="store.write_failed", operation="include_future_sweep"
            ).warning(f"include_future sweep failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task
    # Credential first: the posture check needs to know whether admin auth exists.
    admin_auth.ensure()
    # #95: no credential at all means read-open / write-closed; a shared password
    # with no principals means nobody can be revoked individually. Both are worth
    # exactly one startup line.
    auth.warn_if_read_only()
    auth.warn_if_legacy()
    # Agents are unauthenticated by default (#94) — say so once, loudly, rather
    # than letting an operator discover it from an incident.
    agent_auth.warn_if_unenforced()
    # Refuses to start (non-zero exit) when config push is enabled on an insecure
    # posture (#96). Read-only deployments are unaffected.
    posture.assert_startup_posture()
    # #108: the posture is public on /health already; publishing it as metrics is
    # what makes "someone left the insecure override on" a pager rule instead of a
    # thing you find by reading a JSON endpoint. Pure `metric_values()` in, gauges out.
    posture.publish_metrics()
    _log_audit_startup()
    # #98/#108: the log filter (redaction + correlation) is installed by
    # `configure_logging()` at import, before the first line is written. Calling
    # `redaction.install_log_filter()` here would replace the composed patcher and
    # drop `request_id` from every line, so it is deliberately not repeated.
    crypto.warn_if_plaintext()
    # #81: the desired-config cache is loaded once; the message path must not read
    # SQLite per message (#109).
    remote_config.REGISTRY.reload()
    _cleanup_task = asyncio.create_task(periodic_cleanup())
    yield
    if _cleanup_task:
        _cleanup_task.cancel()
    tracing.shutdown_tracing()

prom_client.REGISTRY.unregister(prom_client.PROCESS_COLLECTOR)
prom_client.REGISTRY.unregister(prom_client.PLATFORM_COLLECTOR)
prom_client.REGISTRY.unregister(prom_client.GC_COLLECTOR)


PROM_CONNECTED_AGENTS = prom_client.Gauge(
    "opamp_connected_agents",
    "Number of currently connected agents"
)

PROM_AGENT_HEALTH = prom_client.Gauge(
    "opamp_agent_health",
    "Health status of agents (1=healthy, 0=unhealthy)",
    ["agent_id"]
)

PROM_MESSAGES_RECEIVED = prom_client.Counter(
    "opamp_messages_received_total",
    "Total OpAMP messages received",
    ["message_type"]
)

PROM_OPA_ENABLED = prom_client.Gauge(
    "opamp_opa_enabled",
    "Whether OPA compliance checking is enabled (1=yes, 0=no)"
)

PROM_OPA_ENABLED.set(1 if OPA_ENABLED else 0)

PROM_CONFIG_OFFERS = prom_client.Counter(
    "opamp_remote_config_offers_total",
    "Remote configs delivered to agents (#81)",
)

PROM_CONFIG_PUSHES = prom_client.Counter(
    "opamp_remote_config_pushes_total",
    "Desired config revisions created, by result",
    ["result"],
)


def _parse_cors_origins(raw: str) -> list:
    """Parse the CORS_ORIGINS env var (comma-separated origins; "*" = allow all)."""
    return [o.strip() for o in raw.split(",") if o.strip()]


CORS_ORIGINS = _parse_cors_origins(os.environ.get("CORS_ORIGINS", "*"))

app = FastAPI(
    title="OpAMP Server",
    description=(
        "OpenTelemetry OpAMP server — inspect connected agents, build OCB collector "
        "manifests, run OPA compliance checks, and manage alerts.\n\n"
        "Interactive docs: [`/docs`](/docs) · ReDoc: [`/redoc`](/redoc) · "
        "Machine-readable schema: [`/openapi.json`](/openapi.json).\n\n"
        "**Auth:** every privileged endpoint takes a role, lowest to highest: "
        "`viewer` (reads), `operator` (mutations, propose), `approver` (approve / "
        "reject / rollback) and `admin` (identity, fleet-wide actions). Authenticate "
        "with an admin session (`POST /auth/session` → `Authorization: Bearer <token>`) "
        "or, for scripts, HTTP Basic (`name:password`, or just the legacy "
        "`ADMIN_PASSWORD`). With no credential configured the server runs in read-only "
        "mode: reads are open and every privileged write answers 403 — check "
        "`GET /auth/status`.\n\n"
        "**Agent auth:** off by default. With `OPAMP_AGENT_AUTH=token` every agent "
        "message must carry `Authorization: Bearer <token>`; mint one with "
        "`POST /agents/{id}/token` (or `opampctl agents token create`). "
        "`GET /agents` reports each agent's credential state.\n\n"
        "**Config push:** off by default (`OPAMP_CONFIG_PUSH_ENABLED=false`); its "
        "routes 404 and no `remote_config` is ever sent until the posture is secure. "
        "Check `GET /health` → `config_push`.\n\n"
        "**Audit log:** on by default (`OPAMP_AUDIT_ENABLED=true`) — an append-only, "
        "hash-chained record of every privileged action in `data/opamp.db`. Read it "
        "with `GET /audit`, check it with `GET /audit/verify`, export it with "
        "`GET /audit/export` (all `admin`). A privileged change that cannot be "
        "recorded is refused."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    # Browsers reject "Access-Control-Allow-Origin: *" alongside credentials, so only
    # send credentials when specific origins are configured.
    allow_credentials="*" not in CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# #108: outermost, so even a CORS rejection and a 4xx from a route carries a
# request id and exactly one completion line. WebSocket scopes pass through
# untouched (the socket layer binds `conn_id` itself).
app.add_middleware(RequestContextMiddleware)

# #108: tracing must be wired *before* Starlette builds the middleware stack, so it
# cannot happen inside the lifespan (adding middleware to a started app raises).
# `init_tracing` is a no-op unless OPAMP_TRACING_ENABLED=true.
tracing.init_tracing(app)


@app.get("/auth/status", tags=["auth"], summary="Check whether admin auth is required")
def auth_status():
    """Report the admin auth mode — no secrets.

    Returns `{"password_required": bool, "auth_mode": "password"|"principals"|
    "disabled", "roles": [str], "sessions": bool, "principals": int,
    "legacy_password": bool}`.

    `password` means only the shared `ADMIN_PASSWORD` (or a stored/generated
    credential) is configured: one anonymous `admin` principal, deprecated.
    `principals` means per-person identities exist and `roles` is the full role
    ladder they may hold. `disabled` is **read-only mode** — no credential exists,
    so reads are open and every privileged write answers 403 (#95). `sessions` says
    whether `POST /auth/session` can be used to get a bearer token, and
    `principals` is the number of live identities. Unauthenticated — call this
    first to discover how to authenticate.
    """
    live_principals = auth.PRINCIPALS.count_live()
    is_configured = auth.configured()
    if live_principals:
        auth_mode = "principals"
    elif admin_auth.configured():
        auth_mode = "password"
    else:
        auth_mode = "disabled"
    return {
        "password_required": is_configured,
        "auth_mode": auth_mode,
        "roles": list(auth.ROLES) if is_configured else [],
        "sessions": is_configured,
        "principals": live_principals,
        "legacy_password": bool(admin_auth.configured()),
    }


class LoginRequest(BaseModel):
    """Body for `POST /auth/session`."""

    name: str = ""
    password: str = ""


class PrincipalRequest(BaseModel):
    """Body for `POST /auth/principals`."""

    name: str
    role: str = ROLE_VIEWER
    password: str
    expires_at: Optional[str] = None


@app.post(
    "/auth/session",
    tags=["auth"],
    summary="Log in and get a bearer session token",
    status_code=201,
)
def create_session(body: LoginRequest, request: Request, response: Response):
    """Exchange `{"name": ..., "password": ...}` for a session token.

    Returns `{"token", "token_type": "bearer", "expires_at", "principal":
    {"name", "role"}, "ttl_seconds", "idle_timeout_seconds"}`. Send the token as
    `Authorization: Bearer <token>` on subsequent calls; log out with
    `DELETE /auth/session`.

    The token is shown **once** — only its SHA-256 is stored. Sessions expire after
    `ADMIN_SESSION_TTL_SECONDS` (default 3600) and after `ADMIN_SESSION_IDLE_SECONDS`
    (default 900) of inactivity. With only the legacy `ADMIN_PASSWORD` configured,
    any name (or none, or `admin`) works; with principals, `name` must match one.

    Failed logins are counted per `(identity, client IP)` and answered with 429 +
    `Retry-After` once `ADMIN_LOGIN_MAX_ATTEMPTS` (default 5) is exceeded, with
    exponential backoff; the response never reveals whether the name exists. Key the
    lockout on the real client by setting `TRUST_PROXY_HEADERS=true` behind a proxy.
    """
    result = auth.login(body.name, body.password, request)
    response.headers["Cache-Control"] = "no-store"
    return result


@app.delete("/auth/session", tags=["auth"], summary="Log out (revoke your session)")
def delete_session(request: Request):
    """Revoke the session token used for this request. Requires a session token.

    Idempotent: `{"logged_out": false}` means it was already gone. HTTP Basic
    callers have no session to revoke and get 400.
    """
    principal = require_roles(request, ROLE_VIEWER)
    try:
        return auth.logout(principal)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/auth/me", tags=["auth"], summary="Who am I, and what may I do")
def whoami(request: Request):
    """The caller's identity, role and session expiry.

    Returns `{"authenticated": bool, "name", "role", "method", "roles",
    "session_expires_at", "session_idle_timeout_seconds"}`. `method` is `session`,
    `password`, `legacy` (the shared `ADMIN_PASSWORD`) or `anonymous` (read-only
    mode). A UI uses this to hide actions the caller cannot perform — the server
    enforces the role on every call regardless.
    """
    principal = require_roles(request, ROLE_VIEWER)
    session = (
        auth.SESSIONS.get(principal.session_hash) if principal.session_hash else None
    )
    return {
        "authenticated": principal.method != "anonymous",
        "name": principal.name,
        "role": principal.role,
        "method": principal.method,
        "roles": list(auth.ROLES),
        "session_expires_at": session.get("expires_at") if session else None,
        "session_idle_timeout_seconds": auth.session_idle_seconds(),
    }


@app.get("/auth/sessions", tags=["auth"], summary="List live admin sessions (admin)")
def get_sessions(request: Request):
    """Every live admin session, labelled with its principal. Requires `admin`.

    Returns `{"sessions": [...]}` with `session_id` (a short prefix you pass to
    `DELETE /auth/sessions/{id}`), principal, timestamps, `client_ip`, `user_agent`
    and whether it has expired. Never returns a token or its hash. Sessions opened
    with the shared password have no principal row, so they do not appear here.
    """
    require_roles(request, ROLE_ADMIN)
    return {"sessions": auth.list_sessions()}


@app.delete(
    "/auth/sessions/{session_id}",
    tags=["auth"],
    summary="Revoke one admin session (admin)",
)
def delete_session_by_id(session_id: str, request: Request):
    """Revoke one session by the short id from `GET /auth/sessions`. Requires `admin`.

    Use it for "sign that device out". To revoke a person, revoke the principal
    (`DELETE /auth/principals/{id}`) — that closes their sessions too. Responds 404
    for an unknown id and 422 for an id shorter than 6 characters.
    """
    require_roles(request, ROLE_ADMIN)
    try:
        return auth.revoke_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/auth/principals", tags=["auth"], summary="List admin principals (admin)")
def get_principals(request: Request):
    """Admin identities and their roles. Requires `admin`.

    Returns `{"principals": [{"principal_id", "name", "role", "created_at",
    "expires_at", "revoked_at", "last_seen_at", "active"}], "roles": [...]}`. Never
    returns a password or its hash. Revoked principals stay listed (`active: false`)
    so their name stays reserved and older audit entries stay readable.
    """
    require_roles(request, ROLE_ADMIN)
    return {"principals": auth.list_principals(), "roles": list(auth.ROLES)}


@app.post(
    "/auth/principals",
    tags=["auth"],
    summary="Create an admin principal (admin)",
    status_code=201,
)
def create_principal(body: PrincipalRequest, request: Request, response: Response):
    """Create a named identity with a role. Requires `admin`.

    Body: `{"name": "alice", "role": "approver", "password": "...",
    "expires_at": null}`. Roles: `viewer` < `operator` < `approver` < `admin`; a
    role satisfies every role below it. `operator` proposes and applies approved
    revisions, `approver` approves/rejects/rolls back — keeping those separate is
    what makes the two-phase push (#99, #103) an approval rather than a formality.

    The password is stored **only** as a scrypt hash (a fast hash of a human
    password is an offline crack waiting to happen) and is never returned. Responds
    409 if the name is taken (including by a revoked principal) and 422 for an
    unknown role, a password under 8 characters, or a malformed `expires_at`.
    """
    require_roles(request, ROLE_ADMIN)
    try:
        record = auth.create_principal(
            body.name, body.role, body.password, expires_at=body.expires_at
        )
    except ValueError as exc:
        status = 409 if "already taken" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc))
    response.headers["Cache-Control"] = "no-store"
    return record


@app.delete(
    "/auth/principals/{principal_id}",
    tags=["auth"],
    summary="Revoke an admin principal (admin)",
)
def delete_principal(principal_id: str, request: Request):
    """Revoke a principal and close every session it holds. Requires `admin`.

    Returns `{"principal_id", "name", "revoked": bool, "sessions_revoked": n}`. The
    row is kept so the name stays reserved and historical audit entries still
    resolve. A revoked name cannot be re-created — deliberately, so "remove alice
    then re-add alice" cannot silently hand her identity to someone else. Responds
    404 for an unknown id.
    """
    require_roles(request, ROLE_ADMIN)
    try:
        return auth.revoke_principal(principal_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/auth/verify", tags=["auth"], summary="Verify admin credentials")
def verify_auth(request: Request):
    """Verify credentials; returns 200 `{"authenticated": true, "role": str}` or 401.

    Accepts a session token (`Authorization: Bearer <token>`) or HTTP Basic
    (`name:password`, or the legacy `ADMIN_PASSWORD`). Any role passes — this only
    proves the credential works. In read-only mode (nothing configured) it succeeds
    as an anonymous `viewer`.

    Failed Basic attempts feed the same lockout as `POST /auth/session` (429 +
    `Retry-After` once `ADMIN_LOGIN_MAX_ATTEMPTS` is exceeded), so this endpoint is
    not an unlimited credential oracle. Session tokens are not counted — 256 random
    bits are not guessable, and counting them would only fill the lockout map.
    """
    principal = require_roles(request, ROLE_VIEWER)
    return {"authenticated": principal.method != "anonymous", "role": principal.role}


# ------------------------------------------------------------------- audit log


def _audit_filters(
    since: Optional[str] = None,
    until: Optional[str] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target: Optional[str] = None,
    result: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalise audit query parameters, answering 422 for an unparseable bound.

    Bad timestamps are rejected rather than silently ignored: a filter that quietly
    matches everything is how an operator concludes "nothing happened".
    """
    try:
        return {
            "since": _normalise_audit_ts(since),
            "until": _normalise_audit_ts(until),
            "actor": (actor or None),
            "action": (action or None),
            "target": (target or None),
            "result": (result or None),
        }
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid timestamp filter: {exc}"
        )


@app.get("/audit", tags=["audit"], summary="Query the audit log (admin)")
def get_audit(
    request: Request,
    since: Optional[str] = Query(None, description="ISO-8601 lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="ISO-8601 upper bound (inclusive)"),
    actor: Optional[str] = Query(None, description="Exact actor, e.g. alice or agent:<id>"),
    action: Optional[str] = Query(None, description="Action or prefix, e.g. config."),
    target: Optional[str] = Query(None, description="Exact target_id or revision_id"),
    result: Optional[str] = Query(None, description="ok | denied | error | noop"),
    limit: int = Query(100, ge=1, le=1000, description="Page size"),
    offset: int = Query(0, ge=0, description="Rows to skip from the newest entry"),
):
    """Privileged-action history, newest first. Requires `admin`.

    Returns `{"entries": [...], "total", "count", "limit", "offset", "filters"}`.
    Each entry has `id`, `ts`, `actor`, `actor_kind`, `role`, `action`,
    `target_kind`, `target_id`, `revision_id`, `config_hash`, `result`,
    `detail_json` (redacted, size-capped) and the chain fields `prev_hash` /
    `entry_hash`. **There are no config bodies and no secrets** — hashes only, and
    fields named like credentials are dropped at the seam.

    `action` is a prefix match, so `action=config.` returns propose/approve/apply.
    Malformed timestamps answer 422 rather than being ignored: a filter that
    silently matches everything reads as "nothing happened". Unauthenticated
    callers get 401; a `viewer` or `operator` gets 403 — reading the audit log is
    an `admin` action.
    """
    require_roles(request, ROLE_ADMIN)
    filters = _audit_filters(since, until, actor, action, target, result)
    entries = audit.STORE.list_entries(**filters, limit=limit, offset=offset)
    total = audit.STORE.count_entries(**filters)
    return {
        "entries": entries,
        "total": total,
        "count": len(entries),
        "limit": limit,
        "offset": offset,
        "filters": filters,
        "enabled": audit.enabled(),
    }


@app.get("/audit/verify", tags=["audit"], summary="Verify the audit chain (admin)")
def verify_audit(request: Request):
    """Recompute every entry hash and the chain links between them. Requires `admin`.

    Returns `{"ok": bool, "entries", "checked", "broken_at", "reason",
    "anchored_by_checkpoint", "first_id", "last_id", "first_ts", "last_ts",
    "breaker"}`. `ok: false` means the log cannot be trusted as evidence: a row was
    edited (`entry_hash` mismatch) or removed/reordered (`prev_hash` mismatch), and
    `broken_at` is the id where that becomes visible. `anchored_by_checkpoint` is
    the one benign reason the first retained entry does not chain to genesis —
    retention (#97) pruned older rows and recorded what it cut.

    This is O(n) over the retained log, so it is on demand, not per request.
    """
    require_roles(request, ROLE_ADMIN)
    report = audit.STORE.verify()
    report["breaker"] = audit.breaker_state()
    return report


@app.get("/audit/export", tags=["audit"], summary="Export the audit log as JSONL or CSV (admin)")
def export_audit(
    request: Request,
    format: Literal["jsonl", "csv"] = Query("jsonl", description="jsonl (SIEM) or csv (spreadsheet)"),
    since: Optional[str] = Query(None, description="ISO-8601 lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="ISO-8601 upper bound (inclusive)"),
    limit: int = Query(100000, ge=1, le=1000000, description="Row cap"),
):
    """Download entries oldest-first for SIEM ingestion or a spreadsheet. Requires `admin`.

    `format=jsonl` returns one JSON object per line (the shape ingest pipelines
    expect); `format=csv` returns a fixed column set for Excel/pandas. Both are
    `Content-Disposition: attachment`. The default cap is 100,000 rows — raise
    `limit` for a full export, or page `GET /audit` when memory matters.

    Only the retained window is exported: retention (`AUDIT_RETENTION_DAYS`) deletes
    older entries, so an off-box copy is the operator's job if the log is evidence.
    """
    require_roles(request, ROLE_ADMIN)
    filters = _audit_filters(since, until)
    rows = list(
        reversed(
            audit.STORE.list_entries(
                since=filters["since"], until=filters["until"], limit=limit
            )
        )
    )
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    body = audit.format_entries(rows, format)
    media_type = "text/csv" if format == "csv" else "application/x-ndjson"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.{format}"'},
    )


AGENT_METRICS: Dict[str, Dict[str, Any]] = {}
_METRICS_STORE = SQLiteMetricsStore()


def _record_agent_metrics(agent_id: str, metrics_data: Dict[str, Any]) -> None:
    """Store the latest metric snapshot in memory (cache) and SQLite (durable)."""
    entry = {
        "metrics": metrics_data,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    AGENT_METRICS[agent_id] = entry
    _METRICS_STORE.upsert(agent_id, entry)


def _get_agent_metrics_entry(agent_id: str) -> Dict[str, Any]:
    """Latest metric snapshot: in-memory cache first, SQLite fallback (lazy)."""
    return AGENT_METRICS.get(agent_id) or _METRICS_STORE.get(agent_id) or {}

logger.info(f"Loaded {AGENT_REGISTRY.count} agents from persistent store")

metrics_app = prom_client.make_asgi_app()
app.mount("/metrics", metrics_app)


@app.post("/v1/metrics", tags=["telemetry"], summary="OTLP metrics ingestion endpoint for collectors")
async def receive_metrics(request: Request):
    """Receive OTLP metrics from a collector (OTLP/HTTP: JSON or protobuf).

    Accepts `application/json` or `application/x-protobuf` OTLP
    `ExportMetricsServiceRequest` bodies. Extracts the agent identity from the
    resource attributes `service.instance.id` (preferred) or `service.name`, and
    stores the latest gauge values per agent. Unauthenticated. Returns
    `{"status": "success"}`; on processing failure returns 200 with
    `{"status": "error", "error": ...}` (ingestion errors are non-fatal by design).
    """
    try:
        content_type = request.headers.get("content-type", "")
        
        if "application/json" in content_type:
            data = await request.json()
            service_name = None
            service_instance_id = None
            
            for resource in data.get("resourceMetrics", []):
                for attr in resource.get("resource", {}).get("attributes", []):
                    if attr.get("key") == "service.name":
                        service_name = attr.get("value", {}).get("stringValue")
                    elif attr.get("key") == "service.instance.id":
                        service_instance_id = attr.get("value", {}).get("stringValue")
            
            agent_id = (service_instance_id or service_name or "").replace("-", "")
            
            # #108: per-scrape lines are DEBUG. One line per collector per scrape at
            # INFO is hot-loop noise, and it was the loudest line in a healthy fleet.
            logger.bind(
                event="metrics.ingest",
                agent_id=agent_id,
                encoding="json",
                known=bool(agent_id and agent_id in AGENT_REGISTRY._agents),
            ).debug(f"JSON metrics received: agent_id={agent_id}")
            
            if agent_id and agent_id in AGENT_REGISTRY._agents:
                metrics_data = {}
                for resource in data.get("resourceMetrics", []):
                    for scope in resource.get("scopeMetrics", []):
                        for metric in scope.get("metrics", []):
                            val = _extract_json_metric_value(metric)
                            if val is not None:
                                metrics_data[metric.get("name", "")] = val
                
                _record_agent_metrics(agent_id, metrics_data)
                logger.bind(event="metrics.stored", agent_id=agent_id, encoding="json").debug(
                    f"JSON: Stored metrics for {agent_id}"
                )
        else:
            body = await request.body()
            
            from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
            metrics_req = ExportMetricsServiceRequest()
            metrics_req.ParseFromString(body)
            
            service_name = None
            service_instance_id = None
            
            for resource in metrics_req.resource_metrics:
                for attr in resource.resource.attributes:
                    if attr.key == "service.name":
                        service_name = attr.value.string_value
                    elif attr.key == "service.instance.id":
                        service_instance_id = attr.value.string_value
            
            agent_id = (service_instance_id or service_name or "").replace("-", "")
            
            logger.bind(
                event="metrics.ingest",
                agent_id=agent_id,
                encoding="protobuf",
                known=bool(agent_id and agent_id in AGENT_REGISTRY._agents),
            ).debug(f"Protobuf metrics received: agent_id={agent_id}")
            
            if agent_id and agent_id in AGENT_REGISTRY._agents:
                metrics_data = {}
                for resource in metrics_req.resource_metrics:
                    for scope in resource.scope_metrics:
                        for metric in scope.metrics:
                            val = _extract_proto_metric_value(metric)
                            if val is not None:
                                metrics_data[metric.name] = val
                
                _record_agent_metrics(agent_id, metrics_data)
                # Values, not the whole snapshot: `all=` printed every metric the
                # collector sent, per scrape, into the log.
                logger.bind(
                    event="metrics.stored",
                    agent_id=agent_id,
                    encoding="protobuf",
                    metric_count=len(metrics_data),
                ).debug(f"Protobuf: Stored {len(metrics_data)} metrics for {agent_id}")
        
        return {"status": "success"}
    except Exception as e:
        logger.bind(event="metrics.ingest_failed", error=str(e)).error(
            f"Failed to process metrics: {e}"
        )
        return {"status": "error", "error": str(e)}


def _extract_json_metric_value(metric: Dict) -> Any:
    gauge = metric.get("gauge", {})
    data_points = gauge.get("dataPoints", [])
    if data_points:
        dp = data_points[0]
        if "asInt" in dp:
            return dp["asInt"]
        elif "asDouble" in dp:
            return dp["asDouble"]
    return None


def _extract_proto_metric_value(metric) -> Any:
    if metric.HasField("gauge"):
        for dp in metric.gauge.data_points:
            if dp.HasField("as_int"):
                return dp.as_int
            elif dp.HasField("as_double"):
                return dp.as_double
            elif dp.HasField("as_uint"):
                return dp.as_uint
    if metric.HasField("sum"):
        for dp in metric.sum.data_points:
            if dp.HasField("as_int"):
                return dp.as_int
            elif dp.HasField("as_double"):
                return dp.as_double
            elif dp.HasField("as_uint"):
                return dp.as_uint
    return None


def update_metrics():
    PROM_CONNECTED_AGENTS.set(len(AGENT_REGISTRY._agents))


def cleanup_stale_agents(now: Optional[datetime] = None) -> list:
    """Remove agents that have gone silent past their own timeout (#69).

    `now` is injectable so the removal window can be tested against a controllable
    clock instead of `sleep()`.
    """
    now = now or datetime.now(timezone.utc)
    stale = []

    for agent in AGENT_REGISTRY.list_all():
        timeout = agent.timeout_seconds(
            AGENT_TIMEOUT_SECONDS,
            AGENT_TIMEOUT_INTERVAL_MARGIN,
            AGENT_FIRST_CONTACT_SECONDS,
        )
        silent = (now - agent.last_heartbeat).total_seconds()
        if silent > timeout:
            stale.append((agent, silent, timeout))

    if not stale:
        # Every 15s is 5760 identical lines a day, and this is the tick that also
        # prunes audit rows — the log must show something only when it acted.
        logger.debug("No stale agents")
        return []

    logger.info(f"Found {len(stale)} stale agent(s)")

    for agent, silent, timeout in stale:
        logger.info(
            f"Removing stale agent: {agent.agent_id} "
            f"(silent {silent:.0f}s > timeout {timeout:.0f}s, "
            f"observed interval "
            f"{f'{agent.report_interval:.0f}s' if agent.report_interval else 'unknown'})"
        )
        AGENT_REGISTRY.remove(agent.agent_id)
        PROM_AGENT_HEALTH.remove(agent.agent_id)
        _METRICS_STORE.delete(agent.agent_id)
        AGENT_METRICS.pop(agent.agent_id, None)
        send_stale_agent_alert(agent.agent_id)

    update_metrics()
    return [agent.agent_id for agent, _, _ in stale]


HEARTBEAT_PERSIST_SECONDS = 60  # bounds post-restart heartbeat skew (#109)


def _apply_status_report(agent_id: str, agent_dict: Dict[str, Any], persist: bool = True) -> bool:
    """Apply the status fields present in any valid agent message.

    Fields are collected and applied to the in-memory state in one pass, then
    persisted at most once: a message that changes nothing performs no write at
    all, and one that changes N field groups performs exactly one (#109).
    """
    updates: Dict[str, Any] = {}

    if 'health' in agent_dict and agent_dict['health']:
        updates['healthy'] = agent_dict['health'].get('healthy', False)
        PROM_AGENT_HEALTH.labels(agent_id=agent_id).set(1 if updates['healthy'] else 0)
        PROM_MESSAGES_RECEIVED.labels(message_type="health").inc()

    if 'agentDescription' in agent_dict:
        updates['description'] = agent_dict['agentDescription']

    if 'effectiveConfig' in agent_dict:
        updates['effective_config'] = json.dumps(agent_dict['effectiveConfig'])
        PROM_MESSAGES_RECEIVED.labels(message_type="effective_config").inc()

    if 'health' in agent_dict:
        updates['health'] = agent_dict['health']

    if 'availableComponents' in agent_dict:
        updates['available_components'] = agent_dict['availableComponents']
        PROM_MESSAGES_RECEIVED.labels(message_type="available_components").inc()

    if 'packageStatuses' in agent_dict:
        updates['package_statuses'] = agent_dict['packageStatuses']
        PROM_MESSAGES_RECEIVED.labels(message_type="package_status").inc()

    if 'remoteConfigStatus' in agent_dict:
        status = agent_dict['remoteConfigStatus'] or {}
        updates['remote_config_status'] = status.get('status', 'UNSET')
        # #81: the spec's reconciliation MUST compares hashes, so the hash the agent
        # echoes back (base64 on the wire) and its error text are kept. Stored as
        # hex, which is what the API and the audit log use.
        reported_hash = remote_config.hash_from_wire(status.get('lastRemoteConfigHash'))
        if reported_hash is not None:
            updates['last_remote_config_hash'] = reported_hash
        updates['remote_config_error'] = status.get('errorMessage') or None
        PROM_MESSAGES_RECEIVED.labels(message_type="remote_config_status").inc()

    PROM_MESSAGES_RECEIVED.labels(message_type="heartbeat").inc()

    changed = AGENT_REGISTRY.update(agent_id, persist=False, **updates)
    # #69: the message itself is the heartbeat (and the cadence sample). In-memory
    # only: the row is written once below, by `persist()`.
    AGENT_REGISTRY.record_report(agent_id)
    if not persist:
        return changed

    # Steady state: nothing changed, so skip the write unless the stored
    # heartbeat is old enough that a restart would look like a stale fleet.
    if changed or AGENT_REGISTRY.heartbeat_persist_due(agent_id, HEARTBEAT_PERSIST_SECONDS):
        AGENT_REGISTRY.persist(agent_id)
        return True
    return False


MESSAGE_KINDS = (
    "remoteConfigStatus",
    "agentDisconnect",
    "agentDescription",
    "effectiveConfig",
    "availableComponents",
    "packageStatuses",
    "connectionSettingsRequest",
    "health",
)


def _message_kinds(agent_dict: Dict[str, Any]) -> List[str]:
    """The report kinds present in one message, for logs, spans and metrics."""
    kinds = [key for key in MESSAGE_KINDS if key in agent_dict]
    return kinds or ["heartbeat"]


def _handle_agent_message(
    data: bytes,
    principal: "agent_auth.Principal | None" = None,
    transport: str = "http",
) -> ProcessedMessage:
    """Apply one `AgentToServer` message and build the `ServerToAgent` reply.

    Shared by the HTTP and WebSocket transports so the two cannot drift; the
    transport only differs in framing and connection lifecycle. Returns the
    serialized reply plus the fields the socket layer needs for its registry.

    `principal` is who presented the credential (#94); `None` in `off` mode. A
    credential that does not match the claimed `instance_uid`, or a uid already
    bound to a different principal, raises `AgentAuthError` — before any state is
    read or written, so a refusal can never leave a half-applied report behind.

    `transport` is only for observability (#108): the log line carries it, and the
    `opamp.message` span records `transport=http|ws`, which is the first question
    asked when one transport misbehaves and the other does not.
    """
    response = ServerToAgent()
    response.capabilities = 0

    if not data:
        response.capabilities = ServerCapabilities.ServerCapabilities_AcceptsStatus
        return ProcessedMessage(response.SerializeToString())

    try:
        agent_msg = AgentToServer()
        agent_msg.ParseFromString(data)
    except Exception as e:
        logger.bind(event="opamp.message.rejected", transport=transport, reason="parse").error(
            f"Failed to parse OpAMP message: {e}"
        )
        response.capabilities = ServerCapabilities.ServerCapabilities_AcceptsStatus
        return ProcessedMessage(response.SerializeToString())

    agent_id = binascii.hexlify(agent_msg.instance_uid).decode('utf-8')
    agent_dict = MessageToDict(agent_msg)

    if principal is not None:
        # Identity comes from the credential, never from the body (D1). Reject
        # before `_apply_status_report`, so an impostor cannot mutate even briefly.
        agent_auth.authorize_uid(principal, agent_id)
        bound = WS_REGISTRY.principal_for(agent_msg.instance_uid)
        if bound is not None and bound != principal.agent_id:
            raise agent_auth.AgentAuthError(
                agent_auth.REASON_BINDING_CONFLICT,
                "instance_uid is bound to a different principal",
                principal=principal.agent_id,
                claimed=agent_id,
            )

    # Bound here, not per log call: every line written while applying this message
    # (the offer decision, the audit write, the alert it may trigger) carries the
    # same `agent_id` without threading it through every function (#108).
    with logger.contextualize(agent_id=agent_id, transport=transport):
        return _apply_agent_message(agent_msg, agent_dict, agent_id, principal, transport)


def _apply_agent_message(
    agent_msg: AgentToServer,
    agent_dict: Dict[str, Any],
    agent_id: str,
    principal: "agent_auth.Principal | None",
    transport: str,
) -> ProcessedMessage:
    """The state-mutating half of `_handle_agent_message`, inside the log/trace scope."""
    response = ServerToAgent()
    response.instance_uid = agent_msg.instance_uid

    # #102: one advertisement for both paths. `OffersRemoteConfig` is only included
    # when the server may actually deliver (#96), so an unsolicited push frame and a
    # reply never disagree about what the server can do.
    response.capabilities = remote_config.server_capabilities()

    existing = AGENT_REGISTRY.get(agent_id)
    is_new = existing is None
    health_changed = bool(
        "health" in agent_dict
        and existing is not None
        and existing.healthy != agent_dict["health"].get("healthy", False)
    )
    kinds = _message_kinds(agent_dict)
    accepts_remote = bool(
        agent_msg.capabilities & AgentCapabilities.AgentCapabilities_AcceptsRemoteConfig
    )
    heartbeat = tracing.message_is_heartbeat(
        agent_dict, is_new=is_new, health_changed=health_changed
    )

    # Heartbeats are the highest-volume event in the system: a log line at DEBUG and
    # no span. Everything else is a state change worth one INFO line and one span.
    if not heartbeat:
        logger.bind(
            event="opamp.message",
            kinds=kinds,
            new_agent=is_new,
            health_changed=health_changed,
        ).info(f"OpAMP message from {agent_id}: {', '.join(kinds)}")
    else:
        logger.bind(event="opamp.heartbeat", kinds=kinds).debug(
            f"heartbeat from {agent_id}"
        )

    span_attributes = {
        "agent_id": agent_id,
        "instance_uid": agent_id,
        "transport": transport,
        "message.kinds": kinds,
        "agent.capabilities.accepts_remote_config": accepts_remote,
        "agent.new": is_new,
    }
    manager = (
        nullcontext()
        if heartbeat
        else tracing.span("opamp.message", **span_attributes)
    )
    with manager as sp:
        if is_new:
            logger.bind(event="agent.registered", agent_id=agent_id, transport=transport).info(
                f"New agent connecting: {agent_id}"
            )
            AGENT_REGISTRY.register(agent_id, AgentState(
                instance_uid=agent_msg.instance_uid,
                agent_id=agent_id,
                capabilities=agent_msg.capabilities,
                description=agent_dict.get("agentDescription", {}),
            ), persist=False)
            response.flags = ServerToAgentFlags.ServerToAgentFlags_ReportFullState
            update_metrics()

            send_new_agent_alert(agent_id)

        # One write for the whole message (#109): `_apply_status_report` batches the
        # field groups and flushes the row at most once, including for a new agent.
        _apply_status_report(agent_id, agent_dict)

        # #81: react to a FAILED report before deciding what to offer — a poisoned
        # revision must not be re-sent in the same reply that learned it failed (D6).
        status_name, reported_hash, error_message = remote_config.parse_status(agent_dict)
        if status_name:
            remote_config.note_status(agent_id, status_name, reported_hash, error_message)

        disconnected = 'agentDisconnect' in agent_dict
        if disconnected:
            logger.bind(event="agent.disconnected", agent_id=agent_id, transport=transport).info(
                f"Agent disconnecting: {agent_id}"
            )
            AGENT_REGISTRY.remove(agent_id)
            PROM_AGENT_HEALTH.remove(agent_id)
            _METRICS_STORE.delete(agent_id)
            AGENT_METRICS.pop(agent_id, None)
            update_metrics()

        # #81: offer the desired config when (and only when) the agent's reported hash
        # differs from ours. The agent is authenticated by this point, so the offer can
        # only ever go to the agent it was written for (D1).
        offered = False
        if not disconnected and posture.push_allowed():
            offer = remote_config.offer_for(agent_id, agent_msg.capabilities, agent_dict.get("remoteConfigStatus"))
            if offer is not None:
                response.remote_config.CopyFrom(offer)
                PROM_CONFIG_OFFERS.inc()
                offered = True

        # Last line of defence for #81/#102: nothing may put remote_config on the
        # wire while the posture says config push is not allowed.
        posture.scrub_remote_config(response)

        tracing.set_span_attributes(
            sp,
            **{
                "reply.capabilities": response.capabilities,
                "reply.remote_config_offered": offered,
            },
        )

    return ProcessedMessage(
        response.SerializeToString(),
        instance_uid=bytes(agent_msg.instance_uid),
        disconnected=disconnected,
    )


@app.post("/v1/opamp", tags=["opamp"], summary="OpAMP protocol endpoint for agents")
async def opamp_endpoint(request: Request) -> Response:
    """OpAMP protocol endpoint — agents connect here over plain HTTP.

    Accepts protobuf-encoded `AgentToServer` messages and replies with
    `ServerToAgent` (`application/x-protobuf`). Registers new agents, updates
    heartbeats, health, description, effective config, package status, and remote
    config status. Not for human/agent tooling use — pointed at by the collector's
    `opamp` extension (`endpoint: http://host:4320/v1/opamp`).

    **Authentication** depends on `OPAMP_AGENT_AUTH`. With `token` the agent must
    send `Authorization: Bearer <token>` (minted by `POST /agents/{id}/token`):
    a missing, unknown or revoked token is **401 with no `ServerToAgent` body**, so
    an unauthenticated client never learns the server's capabilities; a valid
    token claiming another agent's `instance_uid` is **403** and the real agent's
    state is untouched. Every rejection is audited and counted in
    `opamp_agent_auth_failures_total{reason}`. The default (`off`) is
    unauthenticated, exactly as before.

    The same path also accepts a WebSocket upgrade when `OPAMP_WS_ENABLED=true`.
    """
    client = request.client.host if request.client else ""
    principal, reason = agent_auth.authenticate(request.headers)
    if reason is not None:
        logger.bind(
            event="agent.auth.failed", reason=reason, transport="http", client=client
        ).warning(f"agent credential refused: {reason}")
        agent_auth.record_failure(reason, transport="http", client=client)
        raise HTTPException(
            status_code=401,
            detail="invalid agent credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        result = _handle_agent_message(await request.body(), principal)
    except agent_auth.AgentAuthError as exc:
        agent_auth.record_failure(
            exc.reason,
            principal=exc.principal,
            claimed=exc.claimed,
            transport="http",
            client=client,
        )
        raise HTTPException(status_code=403, detail=exc.detail)

    return Response(
        content=result.response,
        media_type="application/x-protobuf",
        headers={"Connection": "keep-alive"}
    )


@app.websocket("/v1/opamp")
async def opamp_ws_endpoint(websocket: WebSocket) -> None:
    """OpAMP WebSocket transport — same path as the HTTP endpoint.

    Disabled by default. When `OPAMP_WS_ENABLED` is false the handshake is
    refused with close code 1008 and the server behaves exactly as an HTTP-only
    server. When enabled, each binary frame is a varint header (must be 0)
    followed by an `AgentToServer` protobuf, per spec §WebSocket Transport.

    Authentication runs **before** `accept()` (#94): with `OPAMP_AGENT_AUTH=token`
    a missing/unknown/revoked token fails the handshake (close 1008, which a
    client sees as a rejected upgrade), and a message claiming a uid that belongs
    to a different credential closes the socket with 1008 mid-stream. A browser
    `Origin` must also satisfy `CORS_ORIGINS`, because WebSocket upgrades are not
    covered by `CORSMiddleware`.
    """
    if not ws_enabled():
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, CORS_ORIGINS):
        logger.warning(f"Refusing OpAMP WebSocket upgrade from origin {origin}")
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    principal, reason = agent_auth.authenticate(websocket.headers)
    if reason is not None:
        agent_auth.record_failure(
            reason,
            transport="ws",
            client=websocket.client.host if websocket.client else "",
        )
        logger.warning(f"Refusing OpAMP WebSocket upgrade: {reason}")
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()
    await serve_websocket(websocket, _handle_agent_message, principal=principal)


@app.get("/agents")
def list_agents(
    request: Request,
    healthy: Optional[Literal["true", "false", "unknown"]] = Query(
        default=None,
        description=(
            "Filter by health: true (healthy), false (unhealthy), or "
            "unknown (no health reported). Matches the UI Agents page grouping."
        ),
    ),
    status: Optional[str] = Query(
        default=None,
        description=(
            "Filter by remote config status (case-insensitive), e.g. UNSET, "
            "APPLIED, APPLYING, FAILED."
        ),
    ),
    remote_config_status: Optional[str] = Query(
        default=None,
        description=(
            "Explicit alias for 'status' — filter by remote config status. "
            "Use this when the agent description has its own 'status' attribute."
        ),
    ),
):
    """List agents, optionally filtered.

    Reserved query params: ``healthy``, ``status`` (alias ``remote_config_status``).
    Any other query param filters on the agent's OpAMP description metadata
    (identifyingAttributes + nonIdentifyingAttributes), e.g. ``?environment=prod``.
    Repeated params match any value (OR): ``?environment=prod&environment=staging``.
    Agents missing an attribute are excluded.

    Unauthenticated. Returns `{"agents": [...], "count": n, "filters": {...}}`.
    Each entry carries a `credential` block (#94) — `enrolled`, `active`,
    `last_seen_at`, `revoked_at` — so an operator can watch a fleet finish the
    token cutover. It never contains a token or a token hash.
    """
    remote_config_status = remote_config_status if remote_config_status is not None else status
    reserved = {"healthy", "status", "remote_config_status"}
    attr_filters: Dict[str, list] = {}
    for key, value in request.query_params.multi_items():
        if key not in reserved:
            attr_filters.setdefault(key, []).append(value)

    selected = [
        agent
        for agent in AGENT_REGISTRY.list_all()
        if agent.matches_filters(
            healthy=healthy,
            remote_config_status=remote_config_status,
            attributes=attr_filters,
        )
    ]
    credentials = agent_auth.credential_summaries([agent.agent_id for agent in selected])
    agents = []
    for agent in selected:
        entry = agent.to_dict()
        entry["credential"] = credentials.get(agent.agent_id)
        agents.append(entry)

    filters = {}
    if healthy is not None:
        filters["healthy"] = healthy
    if remote_config_status is not None:
        filters["remote_config_status"] = remote_config_status
    if attr_filters:
        filters["attributes"] = attr_filters

    return {"agents": agents, "count": len(agents), "filters": filters}


@app.get("/agent/{agent_id}", tags=["agents"], summary="Full details for one agent")
def get_agent(agent_id: str):
    """Return full details for a single agent, including its latest OTLP metrics.

    Unauthenticated. `agent_id` is the hex instance UID (as returned by `GET /agents`).
    Responds 404 if the agent is not connected. The `metrics` key holds the last
    OTLP snapshot (empty dict if none received); `credential` reports the agent's
    auth state (`enrolled`, `active`, `last_seen_at`, `revoked_at` — never a token).
    `report_interval_seconds` is the cadence learned from the agent's own messages
    and `stale_timeout_seconds` the silence budget applied to it (#69), so "why is
    this agent still listed / why did it disappear" is answerable from one response.
    """
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent_dict = agent.to_dict()
    agent_dict["metrics"] = _get_agent_metrics_entry(agent_id)
    agent_dict["credential"] = agent_auth.credential_summary(agent_id)
    # #69: the silence budget actually applied to this agent, so "why is it still
    # listed / why was it dropped" is answerable without reading the cleanup code.
    agent_dict["stale_timeout_seconds"] = agent.timeout_seconds(
        AGENT_TIMEOUT_SECONDS,
        AGENT_TIMEOUT_INTERVAL_MARGIN,
        AGENT_FIRST_CONTACT_SECONDS,
    )
    
    return agent_dict


class AgentTokenRequest(BaseModel):
    """Optional body for credential issuance."""

    label: str = ""
    retire_previous: bool = False


@app.post(
    "/agents/{agent_id}/token",
    tags=["agents"],
    summary="Issue (or rotate) an agent credential (admin)",
    status_code=201,
)
def create_agent_token(
    agent_id: str,
    request: Request,
    response: Response,
    body: AgentTokenRequest | None = None,
):
    """Mint a per-agent token for `OPAMP_AGENT_AUTH=token`. Requires admin auth.

    `agent_id` is the hex `instance_uid` the token is bound to — the credential
    *is* the identity, so a token may only ever speak for that one agent. The
    plaintext token is returned **once**, is stored only as a SHA-256 hash, and
    never appears in logs, listings or audit entries.

    Body (optional): `{"label": "prod-1", "retire_previous": false}`.
    `retire_previous` revokes the agent's existing credentials immediately;
    the default retires them on the first successful use of the new token, so a
    rotation needs no downtime. `collector_snippet` is a paste-ready headers block
    for the collector config (the out-of-band bootstrap — `extension/opampextension`
    cannot accept a credential in-band).

    Responds 422 if `agent_id` is not a 16-byte hex instance UID, 401 without
    valid admin credentials, and with `Cache-Control: no-store`.
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    body = body or AgentTokenRequest()
    try:
        agent_id = agent_auth.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # #97: validated, then recorded, then acted. A credential that cannot be
    # audited is not minted — "who could talk to this agent" must be answerable.
    audit_action(
        require_roles(request, ROLE_ADMIN),
        "agent.credential.issued",
        target_kind="agent",
        target_id=agent_id,
        label=body.label,
        retire_previous=body.retire_previous,
    )
    try:
        token, record = agent_auth.issue_token(
            agent_id, label=body.label, retire_previous=body.retire_previous
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    response.headers["Cache-Control"] = "no-store"
    return {
        "agent_id": record.get("agent_id", agent_id),
        "token": token,
        "label": record.get("label", ""),
        "created_at": record.get("created_at"),
        "collector_snippet": agent_auth.collector_snippet(token),
        "note": (
            "Shown once and not recoverable: put it in the agent's config now. "
            "The token's hash is all this server keeps."
        ),
    }


@app.delete("/agents/{agent_id}/token", tags=["agents"], summary="Revoke an agent's credentials (admin)")
def revoke_agent_token(agent_id: str, request: Request):
    """Revoke every live credential for one agent. Requires admin auth.

    Idempotent: returns `{"agent_id", "revoked": n}` where `revoked` is the number
    of live credentials that were just revoked (0 if the agent had none).

    Revocation **blocks delivery, it does not wipe state**: the agent's record,
    its inventory and (once #99 lands) its desired config are retained — the pit
    this avoids is an operator revoking a compromised agent and accidentally
    deleting the config they need to restore it. Responds 422 if `agent_id` is not
    a 16-byte hex instance UID.
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    try:
        agent_id = agent_auth.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # #97: recorded before revoked, so the gap between "who had access" and "who
    # ended it" is never a guess.
    audit_action(
        require_roles(request, ROLE_ADMIN),
        "agent.credential.revoked",
        target_kind="agent",
        target_id=agent_id,
    )
    return {"agent_id": agent_id, "revoked": agent_auth.revoke_tokens(agent_id)}


class ManifestRequest(BaseModel):
    """Optional body for manifest generation."""

    version: str = DEFAULT_VERSION


@app.get("/agent/{agent_id}/metrics", tags=["agents"], summary="Latest OTLP metrics for one agent")
def get_agent_metrics(agent_id: str):
    """Return the agent's latest ingested OTLP metric values.

    Unauthenticated. `{"<metric_name>": value, ..., "updated_at": iso8601}`; empty
    object if the agent has sent no metrics. Snapshots persist in SQLite, so they
    survive server restarts. Responds 404 if the agent is unknown.
    """
    if agent_id not in AGENT_REGISTRY._agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _get_agent_metrics_entry(agent_id)


@app.post("/agent/{agent_id}/manifest", tags=["agents"], summary="Generate an OCB manifest.yaml for a slim collector build")
def generate_agent_manifest(agent_id: str, request: ManifestRequest | None = None):
    """Generate an OCB manifest.yaml for a slim collector build from an agent's used components.

    Read-only and unauthenticated, consistent with the other agent read endpoints.
    Returns the manifest YAML, the OCB build command, and the resolved distro version.
    Responds 409 when the agent has no components in use (nothing to build a slim
    distro from), and 422 when the requested distro version is not semver.
    """
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    version = DEFAULT_VERSION if request is None else request.version
    try:
        version = validate_manifest_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    components = agent.components
    if not components or not any(
        comp.get("used") for group in components.values() for comp in group
    ):
        raise HTTPException(
            status_code=409,
            detail="Agent has no components in use; cannot generate a buildable OCB manifest",
        )

    return {
        "manifest_yaml": generate_manifest(components, version),
        "ocb_command": generate_ocb_command(version),
        "collector_version": version,
    }


@app.get("/agent/{agent_id}/report", tags=["reports"], summary="Markdown report for one agent")
def agent_report(agent_id: str):
    """Generate a markdown report for a single agent (versions, health, components).

    Unauthenticated, read-only. Same shape/content the UI Reports page renders.
    Responds 404 if the agent is unknown.
    """
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "report_markdown": generate_agent_report({"agents": [agent.to_dict()]}, "markdown"),
    }


@app.get("/reports/fleet", tags=["reports"], summary="Full fleet summary report (markdown)")
def fleet_report():
    """Fleet-wide agent report: component versions, outdated/heavy collectors, per-agent detail.

    Unauthenticated, read-only. Returns `{"report_markdown": str, "agent_count": n}`
    — markdown identical to what the UI Reports page renders.
    """
    agents = [a.to_dict() for a in AGENT_REGISTRY.list_all()]
    return {
        "report_markdown": generate_agent_report({"agents": agents}, "markdown"),
        "agent_count": len(agents),
    }


@app.get("/reports/heavy-collectors", tags=["reports"], summary="Heavy collectors report (many unused components)")
def heavy_collectors_report(
    threshold: float = Query(
        default=0.5,
        ge=0,
        le=1,
        description="Unused-component ratio above which a collector is 'heavy'",
    ),
):
    """Report collectors whose unused-component ratio exceeds `threshold`.

    Unauthenticated, read-only. Returns `{"report_markdown": str, "heavy_count": n,
    "threshold": float}`. `threshold` must be within [0, 1] (FastAPI 422 otherwise).
    """
    agents = [a.to_dict() for a in AGENT_REGISTRY.list_all()]
    heavy = [a for a in agents if _is_heavy(a, threshold)]
    return {
        "report_markdown": generate_heavy_collectors_report({"agents": agents}, threshold),
        "heavy_count": len(heavy),
        "threshold": threshold,
    }


@app.get("/reports/outdated-collectors", tags=["reports"], summary="Outdated collectors report (component versions)")
def outdated_collectors_report(
    version: str = Query(
        default="0.149.0",
        description="Reference collector version (plain semver, e.g. '0.149.0'); "
        "components older than this are flagged",
    ),
):
    """Report collectors with components older than `version`.

    Unauthenticated, read-only. Returns `{"report_markdown": str,
    "collectors_count": n, "components_count": n, "version": str}`. Note the
    endpoint takes the reference version as a parameter — auto-detecting the
    latest release from GitHub remains a UI concern. Responds 422 if `version`
    is not plain semver.
    """
    try:
        version = validate_manifest_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    agents = [a.to_dict() for a in AGENT_REGISTRY.list_all()]
    collectors_count, components_count = _count_outdated_collectors(agents, version)
    return {
        "report_markdown": generate_outdated_collectors_report({"agents": agents}, version),
        "collectors_count": collectors_count,
        "components_count": components_count,
        "version": version,
    }


@app.get("/agent/{agent_id}/compliance", tags=["compliance"], summary="Evaluate one agent against OPA policies")
def get_agent_compliance(agent_id: str):
    """Run OPA compliance evaluation for an agent and return the result.

    Unauthenticated (this is an evaluation, not an admin action). When OPA is
    enabled: `{"compliant": bool, "violations": [...], ...}`. When OPA is disabled:
    `{"compliant": null, "opa_enabled": false, "message": ...}`. Responds 404 if the
    agent is unknown.
    """
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    if OPA_ENABLED:
        compliance_result = evaluate_agent_compliance(agent)
        AGENT_REGISTRY.update(agent_id, compliance=compliance_result)
        
        if compliance_result.get("violations"):
            send_compliance_alert(agent_id, compliance_result.get("violations", []))
        
        return compliance_result
    else:
        return {
            "compliant": None,
            "violations": [],
            "opa_enabled": False,
            "message": "OPA not enabled. Set OPA_ENABLED=true and configure OPA_URL"
        }


@app.post("/compliance/check/{agent_id}", tags=["compliance"], summary="Trigger a compliance check for one agent (operator)")
def check_compliance(agent_id: str, request: Request):
    """Force a compliance evaluation for an agent. Requires `operator`.

    Unlike `GET /agent/{id}/compliance`, this is a mutating action and fails with
    503 when OPA is not enabled. Responds 404 if the agent is unknown.
    """
    principal = require_roles(request, ROLE_OPERATOR)

    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not OPA_ENABLED:
        raise HTTPException(status_code=503, detail="OPA not enabled")

    # #97: validated, recorded, then acted — a forced evaluation writes the verdict
    # onto the agent record, so it is a mutation, not a read.
    audit_action(
        principal,
        "compliance.checked",
        target_kind="agent",
        target_id=agent_id,
    )

    compliance_result = evaluate_agent_compliance(agent)
    AGENT_REGISTRY.update(agent_id, compliance=compliance_result)

    if compliance_result.get("violations"):
        send_compliance_alert(agent_id, compliance_result.get("violations", []))

    return compliance_result


@app.get("/compliance/summary", tags=["compliance"], summary="Fleet-wide compliance summary")
def compliance_summary():
    """Fleet-wide compliance counts across all connected agents.

    Unauthenticated. Returns `{"opa_enabled", "compliant", "non_compliant",
    "not_evaluated", "total"}`. Agents whose `compliance` state is null count as
    `not_evaluated` (nothing evaluated yet or OPA disabled).
    """
    compliant_count = 0
    non_compliant_count = 0
    not_evaluated_count = 0
    
    for agent in AGENT_REGISTRY.list_all():
        if agent.compliance is None:
            not_evaluated_count += 1
        elif agent.compliance.get("compliant"):
            compliant_count += 1
        else:
            non_compliant_count += 1
    
    return {
        "opa_enabled": OPA_ENABLED,
        "compliant": compliant_count,
        "non_compliant": non_compliant_count,
        "not_evaluated": not_evaluated_count,
        "total": AGENT_REGISTRY.count,
    }


@app.get("/compliance/policies", tags=["compliance"], summary="List available OPA policies")
def list_policies():
    """List policy modules available to OPA (loaded from `POLICIES_DIR`).

    Unauthenticated. Returns `{"opa_enabled": bool, "policies": [...]}`; empty list
    when OPA is disabled.
    """
    if not OPA_ENABLED:
        return {"opa_enabled": False, "policies": []}
    
    policies = get_available_policies()
    return {
        "opa_enabled": True,
        "policies": policies,
    }


@app.post("/compliance/reload", tags=["compliance"], summary="Ask OPA to reload its policies (admin)")
def reload_policies(request: Request):
    """Trigger OPA to reload policies from disk. Requires admin auth (401 otherwise).

    Returns `{"success": true}` on success, or `{"success": false, "error": ...}`
    when OPA is disabled or the reload failed (still HTTP 200 — inspect the body).
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not OPA_ENABLED:
        return {"success": False, "error": "OPA not enabled"}

    # #97: reloading policies changes what every later compliance verdict means.
    audit_action(
        require_roles(request, ROLE_ADMIN),
        "compliance.policies_reloaded",
        target_kind="server",
        target_id="opa",
    )
    
    try:
        from server.opa_client import get_opa_client
        client = get_opa_client()
        if client:
            client.reload()
            return {"success": True}
        return {"success": False, "error": "OPA client not available"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/compliance/validate", tags=["compliance"], summary="Validate OPA policy files")
def validate_policies():
    """Validate all OPA policy files and return per-policy results.

    Unauthenticated. Returns `{"opa_enabled": bool, "policies": [...]}`; empty list
    when OPA is disabled.
    """
    if not OPA_ENABLED:
        return {"opa_enabled": False, "policies": []}
    
    validation = get_policy_validation()
    return {
        "opa_enabled": True,
        "policies": validation,
    }


@app.get("/alerts", tags=["alerts"], summary="Get alert configuration (redacted)")
def get_alerts(request: Request):
    """Return the current alert configuration, secrets redacted. Requires `viewer`.

    Returns `{"types": [...], "events": {...}, "config": {...}}` — the valid event
    types, the available dispatcher/event settings, and the live config (per-event
    enablement/templates).

    **Redacted:** `webhook_url` comes back as `scheme://host/***REDACTED***` and
    `headers` as `***REDACTED***`, because a webhook path *is* a bearer secret. Send
    the sentinel back unchanged on `PUT /alerts` and the stored value is kept — so a
    UI that round-trips this response cannot wipe the webhook.
    """
    require_roles(request, ROLE_VIEWER)
    return {
        "types": ALERT_TYPES,
        "events": ALERT_EVENTS,
        "config": redact_config(get_alert_config()),
    }


@app.put("/alerts", tags=["alerts"], summary="Update alert configuration (operator)")
def put_alerts(request: Request, body: dict):
    """Merge the alert configuration. Requires `operator`.

    Body is the config object (same shape `GET /alerts` returns under `config`).
    A field sent as `***REDACTED***` keeps its stored value, so feeding a fetched
    config back in is safe. Returns `{"config": {...}}` with the stored result,
    **redacted** for the same reason `GET /alerts` is. Config persists in SQLite
    (`alert_config` table in `data/opamp.db`).
    """
    principal = require_roles(request, ROLE_OPERATOR)
    merged = merge_redacted(body)
    # #97: keys only, never values — a webhook URL is a credential in practice.
    audit_action(
        principal,
        "alerts.updated",
        target_kind="server",
        target_id="alerts",
        fields=sorted(body.keys()),
    )
    config = update_alert_config(merged)
    return {"config": redact_config(config)}


@app.post("/alerts/test", tags=["alerts"], summary="Send a test alert (operator)")
def test_alerts(request: Request, body: dict = None):
    """Fire a test alert through the configured dispatcher. Requires `operator`.

    Optional body: `{"event_type": "new_agent", "event_config": {...}}`. When
    `event_config` is supplied it is used for this one send and then reverted
    (handy for validating unsaved webhook settings); any `***REDACTED***` value in
    it is filled from the stored config first. Returns `{"success": bool,
    "error": str|null}`.
    """
    principal = require_roles(request, ROLE_OPERATOR)

    if body is None:
        body = {}
    event_type = body.get("event_type", "new_agent")
    # #97: a test alert leaves the server (it hits a webhook), so it gets a row.
    audit_action(
        principal,
        "alerts.test",
        target_kind="alert",
        target_id=str(event_type),
        temporary_event_config=bool(body.get("event_config")),
    )
    event_config = body.get("event_config")
    
    if event_config:
        old_config = ALERT_CONFIG["events"].get(event_type, {})
        ALERT_CONFIG["events"][event_type] = event_config
        try:
            success, error = send_test_alert(event_type)
        finally:
            ALERT_CONFIG["events"][event_type] = old_config
    else:
        success, error = send_test_alert(event_type)
    
    return {"success": success, "error": error}


class RemoteConfigRequest(BaseModel):
    """Body for pushing a desired config (#81).

    `config_map` maps a config object name to its body; `{"": {...}}` is the
    single-object case the spec recommends. Keys are agent-defined: `""` is valid
    and not special, and more than one object is normal.
    """

    config_map: Dict[str, Any]
    note: Optional[str] = None
    # #100: moving the `opamp` endpoint away from this server is refused unless the
    # caller says so explicitly; an approver has to confirm it again at approve time.
    allow_endpoint_change: bool = False


class FleetConfigRequest(RemoteConfigRequest):
    """Body for a fleet-wide apply. Explicit targets or a filter; `confirm` gates it."""

    agents: Optional[List[str]] = None
    filter: Optional[Dict[str, Any]] = None
    confirm: bool = False


class ValidateConfigRequest(BaseModel):
    """Body for `POST /config/validate` (#100)."""

    config_map: Dict[str, Any]
    agent_id: Optional[str] = None
    allow_endpoint_change: bool = False


class ConfigScope(BaseModel):
    """Who a proposal targets. Exactly one of `agent_id`, `agents` or `filter`."""

    agent_id: Optional[str] = None
    agents: Optional[List[str]] = None
    filter: Optional[Dict[str, Any]] = None
    include_future: bool = False


class ProposeConfigRequest(BaseModel):
    """Body for `POST /config/revisions` (#99): a draft, not a delivery."""

    config_map: Dict[str, Any]
    scope: ConfigScope
    note: Optional[str] = None
    allow_endpoint_change: bool = False
    override_reason: Optional[str] = None


class ApproveConfigRequest(BaseModel):
    """Body for approving a proposal (#99). An override needs a recorded reason."""

    note: Optional[str] = None
    confirm_endpoint_change: bool = False
    # The selector is re-resolved at approve time; if it now covers agents the
    # approver never saw, the approval needs this explicit acknowledgement.
    confirm_scope_change: bool = False
    override_reason: Optional[str] = None


class RejectConfigRequest(BaseModel):
    """Body for `POST /config/revisions/{id}/reject`."""

    reason: Optional[str] = None


class ReactivateConfigRequest(BaseModel):
    """Body for `POST /config/revisions/{id}/reactivate` (poisoned revision)."""

    reason: Optional[str] = None
    confirm_endpoint_change: bool = False
    override_reason: Optional[str] = None


class RollbackRequest(BaseModel):
    """Body for `POST /agent/{id}/config/rollback`."""

    note: Optional[str] = None


def _desired_view(agent_id: str, reveal: bool = False) -> Optional[Dict[str, Any]]:
    """API-shaped view of an agent's desired config (#81), redacted unless revealed (#98)."""
    record = remote_config.REGISTRY.get(agent_id)
    if record is None:
        return None
    config_map = record.get("config_map") or {}
    if reveal:
        redacted, paths = config_map, []
    else:
        redacted, paths = redact_document(config_map)
    verdict = None
    raw_verdict = record.get("verdict_json")
    if raw_verdict:
        try:
            verdict = json.loads(raw_verdict)
        except (TypeError, ValueError):
            verdict = None
    scope = None
    raw_scope = record.get("scope_json")
    if raw_scope:
        try:
            scope = json.loads(raw_scope)
        except (TypeError, ValueError):
            scope = None
    return {
        "revision_id": record.get("revision_id"),
        "config_hash": record.get("config_hash"),
        "author": record.get("author"),
        "created_at": record.get("created_at"),
        "withdrawn": bool(record.get("withdrawn")),
        "failed_at": record.get("failed_at"),
        "failure_detail": record.get("failure_detail"),
        # #99 lifecycle: only an `active`, unpoisoned revision is deliverable.
        "status": record.get("status"),
        "proposal_id": record.get("proposal_id"),
        "approved_by": record.get("approved_by"),
        "approved_at": record.get("approved_at"),
        "note": record.get("note"),
        "parent_revision_id": record.get("parent_revision_id"),
        "scope_kind": record.get("scope_kind"),
        "scope": scope,
        "verdict": verdict,
        "deliverable": remote_config.deliverable(record),
        "config_map": redacted,
        "redacted": bool(paths),
        "redacted_paths": paths,
    }


def _effective_view(state: AgentState, reveal: bool) -> Optional[Dict[str, Any]]:
    """Effective config as the API shows it: redacted unless explicitly revealed."""
    if state is None:
        return None
    raw = state.effective_config
    if reveal:
        return {
            "config": json.loads(raw) if raw else None,
            "redacted": False,
            "redacted_paths": [],
            "config_error": state.config_error,
        }
    redacted, paths = state.redacted_effective_config()
    return {
        "config": json.loads(redacted) if redacted else None,
        "redacted": bool(paths),
        "redacted_paths": paths,
        "config_error": state.config_error,
    }


@app.get(
    "/agent/{agent_id}/config",
    tags=["config"],
    summary="Desired and effective config for one agent (redacted)",
)
def get_agent_config(agent_id: str, request: Request, reveal: bool = False):
    """Desired (pushed) config alongside the effective (reported) one. Requires `viewer`.

    **Both are redacted by default** (#98): secret-looking keys at any depth and
    auth-shaped header values are replaced with `<redacted>`, and
    `redacted_paths` lists what was masked without showing it. This is the one
    endpoint that can reveal them again: `?reveal=true` requires the `approver`
    role or higher, writes an audit row (`config.reveal`), and sets
    `Cache-Control: no-store`. Revealing is a debugging action, not a browsing one.

    `desired` is null when no config has been pushed. `withdrawn` means the last
    push was a withdrawal (an empty config map, which tells the agent to run its
    local config). `failed_at` means the agent reported `FAILED` for that revision:
    it is **poisoned** and will not be retried (epic #93, D6) — push a new revision
    or roll back.

    `desired.status` is the #99 lifecycle (`proposed`, `active`, `superseded`,
    `rolled_back`, `poisoned`, `rejected`); only `active` is deliverable, and
    `desired.deliverable` says so directly. `assignment` is the delivery record:
    what we last put on the wire, how many attempts it took, what the agent last
    said. `drift` answers "is the agent running what we approved?" — `true`,
    `false`, or `null` when it cannot be known yet — with the missing YAML paths.

    `history` lists revision metadata (hash, author, timestamps) without bodies, so
    "who changed this and when" is answerable without shipping config in a listing.
    """
    principal = require_roles(request, ROLE_VIEWER)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    state = AGENT_REGISTRY.get(agent_id)
    desired = _desired_view(agent_id, reveal)
    if state is None and desired is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")

    if reveal:
        # #98: the only way to see a secret value, and it leaves a record.
        if not role_satisfies(principal.role, ROLE_APPROVER):
            raise HTTPException(
                status_code=403,
                detail=f"revealing secrets requires the {ROLE_APPROVER} role or higher",
            )
        audit_action(
            principal,
            "config.reveal",
            target_kind="agent",
            target_id=agent_id,
        )

    history = [
        {
            "revision_id": row["revision_id"],
            "config_hash": row["config_hash"],
            "author": row["author"],
            "created_at": row["created_at"],
            "status": row.get("status"),
            "proposal_id": row.get("proposal_id"),
            "approved_by": row.get("approved_by"),
            "parent_revision_id": row.get("parent_revision_id"),
            "withdrawn": bool(row.get("withdrawn")),
            "failed_at": row.get("failed_at"),
            "body_pruned_at": row.get("body_pruned_at"),
        }
        for row in remote_config.REGISTRY.history(agent_id, limit=20)
    ]

    record = remote_config.REGISTRY.get(agent_id)
    response = JSONResponse(
        {
            "agent_id": agent_id,
            "desired": desired,
            "effective": _effective_view(state, reveal),
            "remote_config_status": {
                "status": state.remote_config_status if state else None,
                "last_remote_config_hash": state.last_remote_config_hash if state else None,
                "error": state.remote_config_error if state else None,
            },
            "assignment": remote_config.REGISTRY.delivery_record(agent_id),
            "drift": config_control.drift_for(record, state),
            "history": history,
            "config_push_allowed": posture.push_allowed(),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _delivery_for(agent_id: str, principal=None) -> Dict[str, Any]:
    """Attempt immediate delivery after a state change and describe the outcome (#102).

    Every route that changes what an agent *should* run ends with this: the state is
    already stored (that is the model, D5), so a push is only a latency optimisation
    and a failure to reach the agent is `queued`, never an error. The principal is
    passed through so a refusal is attributed to whoever asked for the delivery.
    """
    return remote_config.deliver_now(agent_id, principal).to_dict()


def _delivery_for_many(agent_ids: List[str], principal=None) -> Dict[str, Any]:
    return remote_config.deliver_many(agent_ids, principal)


@app.put(
    "/agent/{agent_id}/config",
    tags=["config"],
    summary="Push a desired config to one agent (approver)",
)
def put_agent_config(
    agent_id: str,
    body: RemoteConfigRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.push")),
):
    """Store a new desired config revision for one agent. Requires `approver`.

    Returns 404 when config push is disabled or the posture is unsafe (#96) — the
    same answer as a route that does not exist, so a stranger learns nothing.

    The body is validated before anything is stored: malformed YAML, a body over
    `CONFIG_MAX_BYTES`, a `role` field this protocol version cannot express, or a
    **redacted** document (pushing one would wipe the fleet's credentials) is a 422
    naming every problem. A revision is immutable (D4); pushing again creates a new
    one and keeps the previous for rollback.

    Delivery is *desired state*, not a command (D5): it is handed to a live
    WebSocket connection immediately (#102) and otherwise rides the agent's next
    status report, and only ever when the hash it reports differs from ours. The
    response's `delivery` object says which happened — `delivered`, `queued` or
    `unchanged` — and never claims the config is applied. Nothing is retried
    automatically — a `FAILED` revision is poisoned until a human pushes again (D6).

    This direct push is approval-by-fiat: the caller holds the approver role. The
    two-phase propose/approve flow that separates those duties arrives with #99 and
    #103; it will reuse this store rather than add a second delivery path.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    state = AGENT_REGISTRY.get(agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")

    verdict = config_control.validate_for_targets(
        body.config_map,
        agents=[state],
        allow_endpoint_change=body.allow_endpoint_change,
    )
    if not verdict.ok:
        PROM_CONFIG_PUSHES.labels(result="invalid").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "message": "config validation failed",
                "errors": verdict.error_messages,
                "details": [finding.to_dict() for finding in verdict.errors],
                "warnings": verdict.warning_messages,
                "policy": verdict.policy,
            },
        )
    config_map, warnings = verdict.normalised, verdict.warning_messages

    config_hash = remote_config.compute_hash(config_map)
    revision_id = remote_config.new_revision_id()
    # #97: recorded before the mutation, so a push that cannot be recorded does not
    # happen. The body never appears — only its hash.
    audit_action(
        principal,
        "config.push",
        target_kind="agent",
        target_id=agent_id,
        revision_id=revision_id,
        config_hash=config_hash,
        objects=len(config_map),
        note=body.note,
    )
    try:
        record = remote_config.REGISTRY.push(
            agent_id,
            config_map,
            principal.name,
            revision_id=revision_id,
            note=body.note,
            verdict=verdict,
        )
    except Exception as exc:
        audit.audit_event(
            "config.push",
            principal=principal.name,
            role=principal.role,
            result="error",
            target_kind="agent",
            target_id=agent_id,
            revision_id=revision_id,
            error=str(exc),
        )
        PROM_CONFIG_PUSHES.labels(result="error").inc()
        raise HTTPException(status_code=500, detail=f"could not store the revision: {exc}")

    PROM_CONFIG_PUSHES.labels(result="ok").inc()
    delivery = _delivery_for(agent_id, principal)
    return {
        "agent_id": agent_id,
        "revision_id": record["revision_id"],
        "config_hash": record["config_hash"],
        "objects": len(config_map),
        "status": record.get("status"),
        "warnings": warnings,
        "verdict": verdict.to_dict(),
        "delivery": delivery,
    }


@app.delete(
    "/agent/{agent_id}/config",
    tags=["config"],
    summary="Withdraw a desired config (approver)",
)
def delete_agent_config(
    agent_id: str,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.withdraw")),
):
    """Withdraw the desired config by pushing an **empty** config map. Requires `approver`.

    Withdrawal is not a special case: an empty remote config is how the spec says
    "run your local config", so it is delivered as an ordinary revision whose hash
    differs from the agent's, exactly once. The agent's status is then tracked like
    any other change.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if remote_config.REGISTRY.get(agent_id) is None and AGENT_REGISTRY.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")

    revision_id = remote_config.new_revision_id()
    audit_action(
        principal,
        "config.withdraw",
        target_kind="agent",
        target_id=agent_id,
        revision_id=revision_id,
    )
    record = remote_config.REGISTRY.withdraw(agent_id, principal.name, revision_id=revision_id)
    return {
        "agent_id": agent_id,
        "revision_id": record["revision_id"],
        "config_hash": record["config_hash"],
        "withdrawn": True,
        "delivery": _delivery_for(agent_id, principal),
    }


@app.post(
    "/agent/{agent_id}/config/deliver",
    tags=["config"],
    summary="Deliver the agent's active config now (operator)",
)
def deliver_agent_config(
    agent_id: str,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.deliver")),
):
    """Deliver the agent's current active revision over its live connection. Requires `operator`.

    This is **re-delivery, not a new decision** (epic #93, D5): the revision is
    already active and approved, so this endpoint cannot change what any agent runs.
    That is why `operator` suffices here while creating or editing a config needs
    `approver` — there is nothing here to approve.

    Idempotent by design: it always answers 200 with a `delivery` object whose
    `state` is one of

    * `delivered` — handed to the agent's live WebSocket connection (the writer
      records `delivered_at` when the frame is actually written);
    * `queued` — no live connection, no accepted push (throttled, or the uid is bound
      to a different principal), so the agent gets it on its next status report;
    * `unchanged` — nothing was deliverable; `reason` says why
      (`reported_hash_matches`, `revision_not_deliverable`, `attempts_exhausted`, …).

    `queued` is not a failure — desired state is the model and the poll queue always
    catches up — which is exactly why a rollout engine can call this without having to
    interpret errors. Every attempt is audited as `config.deliver`, and a refusal on
    principal grounds is recorded a second time with `result="error"`.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if AGENT_REGISTRY.get(agent_id) is None and remote_config.REGISTRY.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")

    record = remote_config.REGISTRY.get(agent_id) or {}
    # #97 ordering: recorded before it acts, so a delivery that cannot be recorded
    # does not happen. The outcome is in the response and the metrics; the row names
    # what was being delivered.
    audit_action(
        principal,
        "config.deliver",
        target_kind="agent",
        target_id=agent_id,
        revision_id=record.get("revision_id"),
        config_hash=record.get("config_hash"),
    )
    return {"agent_id": agent_id, "delivery": _delivery_for(agent_id, principal)}


@app.post(
    "/config/apply",
    tags=["config"],
    summary="Push a desired config to many agents (admin, confirmed)",
)
def apply_config_fleet(
    body: FleetConfigRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.apply")),
):
    """Push one config revision to a set of agents. Requires `admin` and `confirm: true`.

    Blast radius is explicit: either `agents: [<id>, ...]` or a `filter` using the
    same keys as `GET /agents` (`healthy`, `remote_config_status`, and description
    attributes). No selector is a 400 rather than "all agents", because a missing
    field must not mean the whole fleet.

    One audit row covers the request (config hash, agent count); the per-agent
    revision ids are in each agent's config history. Per-agent rows for a 400-agent
    rollout would bury every other entry in the log.
    """
    principal = require_roles(request, ROLE_ADMIN)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "fleet-wide config push requires \"confirm\": true — set it only after "
                "checking the target list, or use PUT /agent/{id}/config for one agent"
            ),
        )

    if body.agents:
        targets = []
        for raw_id in body.agents:
            try:
                targets.append(remote_config.normalize_agent_id(raw_id))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
    elif body.filter:
        attributes: Dict[str, List[str]] = {}
        filters = body.filter or {}
        healthy = filters.get("healthy")
        remote_status = filters.get("remote_config_status") or filters.get("status")
        for key, value in filters.items():
            if key in ("healthy", "remote_config_status", "status"):
                continue
            values = value if isinstance(value, list) else [value]
            attributes[key] = [str(item) for item in values]
        targets = [
            state.agent_id
            for state in AGENT_REGISTRY.list_all()
            if state.matches_filters(
                healthy=healthy,
                remote_config_status=remote_status,
                attributes=attributes,
            )
        ]
    else:
        raise HTTPException(
            status_code=400,
            detail="provide \"agents\": [...] or \"filter\": {...}; a missing selector is never \"all agents\"",
        )

    if not targets:
        raise HTTPException(status_code=404, detail="no agents matched the selector")

    # #100: validate with the resolved targets in scope, so the inventory warnings
    # are about the agents this request is actually about.
    targets_states = [AGENT_REGISTRY.get(target) for target in targets]
    verdict = config_control.validate_for_targets(
        body.config_map,
        agents=[state for state in targets_states if state is not None],
        allow_endpoint_change=body.allow_endpoint_change,
    )
    if not verdict.ok:
        PROM_CONFIG_PUSHES.labels(result="invalid").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "message": "config validation failed",
                "errors": verdict.error_messages,
                "details": [finding.to_dict() for finding in verdict.errors],
                "warnings": verdict.warning_messages,
                "policy": verdict.policy,
            },
        )
    config_map, warnings = verdict.normalised, verdict.warning_messages

    config_hash = remote_config.compute_hash(config_map)
    audit_action(
        principal,
        "config.apply",
        target_kind="fleet",
        target_id=f"{len(targets)} agents",
        config_hash=config_hash,
        agent_count=len(targets),
        objects=len(config_map),
        note=body.note,
    )

    results = []
    for target in targets:
        revision_id = remote_config.new_revision_id()
        try:
            record = remote_config.REGISTRY.push(
                target,
                config_map,
                principal.name,
                revision_id=revision_id,
                note=body.note,
                verdict=verdict,
            )
            results.append({"agent_id": target, "revision_id": record["revision_id"], "ok": True})
            PROM_CONFIG_PUSHES.labels(result="ok").inc()
        except Exception as exc:  # one agent's failure must not abort the rollout
            results.append({"agent_id": target, "ok": False, "error": str(exc)})
            PROM_CONFIG_PUSHES.labels(result="error").inc()

    return {
        "config_hash": config_hash,
        "agents": len(targets),
        "succeeded": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "warnings": warnings,
        "results": results,
        "delivery": _delivery_for_many(
            [item["agent_id"] for item in results if item["ok"]], principal
        ),
    }


@app.post(
    "/config/validate",
    tags=["config"],
    summary="Validate a config body without storing it",
)
def validate_config(body: ValidateConfigRequest, request: Request):
    """Check a config map and return its hash, size and problems. Requires `viewer`.

    No storage, no audit, no side effects — this is the "may I?" question that a
    CLI, a UI draft or an AI agent asks before proposing anything (epic #93, D9:
    MCP may propose and validate, never apply). Always 200: `valid: false` with the
    `errors` list is the answer, and a validator that raises on invalid input is
    awkward to call in a loop.

    The checks are structural (shape, size, YAML/JSON parses, no redaction marker).
    The protected-invariant and OPA policy gate is #100 and will extend this
    function, not replace it.
    """
    require_roles(request, ROLE_VIEWER)
    agents = []
    if body.agent_id:
        try:
            agent_id = remote_config.normalize_agent_id(body.agent_id)
        except ValueError as exc:
            return {
                "valid": False,
                "errors": [str(exc)],
                "details": [],
                "warnings": [],
                "policy": {"status": "skipped", "violations": []},
                "config_hash": None,
                "size_bytes": None,
                "objects": None,
            }
        state = AGENT_REGISTRY.get(agent_id)
        if state is not None:
            agents.append(state)

    verdict = config_validation.check_config_map(
        body.config_map,
        agents=agents,
        allow_endpoint_change=body.allow_endpoint_change,
    )
    if not verdict.ok:
        return {
            "valid": False,
            "errors": verdict.error_messages,
            "details": [finding.to_dict() for finding in verdict.errors],
            "warnings": verdict.warning_messages,
            "warning_details": [finding.to_dict() for finding in verdict.warnings],
            "policy": verdict.policy,
            "fingerprint": verdict.fingerprint(),
            "config_hash": remote_config.compute_hash(verdict.normalised)
            if verdict.normalised
            else None,
            "size_bytes": _config_size(verdict.normalised),
            "objects": len(verdict.normalised) or None,
        }
    size = _config_size(verdict.normalised)
    return {
        "valid": True,
        "errors": [],
        "details": [],
        "warnings": verdict.warning_messages,
        "warning_details": [finding.to_dict() for finding in verdict.warnings],
        "policy": verdict.policy,
        "fingerprint": verdict.fingerprint(),
        "config_hash": remote_config.compute_hash(verdict.normalised),
        "size_bytes": size,
        "objects": len(verdict.normalised),
    }


def _config_size(config_map: Dict[str, Dict[str, Any]]) -> Optional[int]:
    if not config_map:
        return None
    return sum(len((entry.get("body") or "").encode("utf-8")) for entry in config_map.values())


# ------------------------------------------------------ config control plane (#99)


def _control_error(exc: "config_control.ConfigControlError") -> HTTPException:
    """Translate a lifecycle refusal into the HTTP answer, keeping its detail."""
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post(
    "/config/revisions",
    tags=["config"],
    summary="Propose a config revision (operator)",
)
def propose_config_revision(
    body: ProposeConfigRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.propose")),
):
    """Draft a config revision for one agent, a list, a selector or the fleet. Requires `operator`.

    **Proposing delivers nothing.** It runs the #100 validation pipeline, resolves the
    scope *now*, and stores one immutable `proposed` revision per target agent under a
    single `proposal_id`. An approver then approves or rejects it
    (`POST /config/revisions/{proposal_id}/approve`), which is what makes "who
    approved this?" answerable and the approval a real second pair of eyes (D3).

    Refused with 422 when the config is broken (unparseable, structurally invalid,
    missing the `opamp` extension, oversized, redacted) or when the **policy gate**
    rejects it and no `override_reason` was supplied. Proposal does not move the
    agent's assignment: a proposal that is never approved changes nothing.

    `scope.include_future` only applies to a `filter` scope; when set, agents that
    match the filter *later* inherit the config (the sweep in the periodic tick
    attaches them). Without it, a later-joining agent inherits nothing — silent
    inheritance is how a fleet-wide config reaches a machine nobody thought about.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    try:
        scope = config_control.parse_scope(body.scope.model_dump())
        prepared = config_control.prepare(
            body.config_map,
            scope=scope,
            agents=AGENT_REGISTRY.list_all(),
            allow_endpoint_change=body.allow_endpoint_change,
            override_reason=body.override_reason,
        )
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)

    proposal_id = remote_config.new_proposal_id()
    audit_action(
        principal,
        "config.propose",
        target_kind="proposal",
        target_id=proposal_id,
        config_hash=prepared["config_hash"],
        agent_count=len(prepared["targets"]),
        objects=len(prepared["config_map"]),
        scope_kind=prepared["scope_kind"],
        override_reason=body.override_reason,
        note=body.note,
    )
    result = config_control.propose(
        prepared, author=principal.name, proposal_id=proposal_id, note=body.note
    )
    PROM_CONFIG_PUSHES.labels(result="proposed").inc()
    return result


@app.get(
    "/config/revisions",
    tags=["config"],
    summary="List config revisions (viewer)",
)
def list_config_revisions(
    request: Request,
    agent: Optional[str] = Query(None, description="Filter by agent id"),
    status: Optional[str] = Query(
        None,
        description="proposed|active|superseded|rolled_back|poisoned|rejected",
    ),
    limit: int = Query(100, ge=1, le=1000),
):
    """Revision metadata across the fleet, newest first. Requires `viewer`.

    **No config bodies.** Hashes, authors, timestamps, lifecycle status, the stored
    validation verdict and the delivery facts only — a listing is not a place to
    leak a fleet's credentials (D8). Read one agent's desired body through
    `GET /agent/{id}/config`, which is redacted and audited.
    """
    require_roles(request, ROLE_VIEWER)
    if agent:
        try:
            agent = remote_config.normalize_agent_id(agent)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    try:
        return config_control.list_proposals(agent_id=agent, status=status, limit=limit)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)


@app.get(
    "/config/revisions/{proposal_id}",
    tags=["config"],
    summary="One proposal with its revisions and verdicts (viewer)",
)
def get_config_proposal(proposal_id: str, request: Request):
    """Proposal detail: every per-agent revision, its status, verdict and delivery facts.

    Requires `viewer`. Bodies are excluded (D8); use `GET /agent/{id}/config`.
    `status` is the rollup: one status when every revision agrees, `mixed` while an
    approval is partially applied, and `statuses` always lists what is present.
    """
    require_roles(request, ROLE_VIEWER)
    try:
        return config_control.proposal_view(proposal_id)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)


@app.post(
    "/config/revisions/{proposal_id}/approve",
    tags=["config"],
    summary="Approve a proposed revision (approver)",
)
def approve_config_revision(
    proposal_id: str,
    body: ApproveConfigRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.approve")),
):
    """Approve and activate a proposal. Requires `approver` — a different duty from `operator`.

    Re-runs the full validation pipeline against each target before activating (#100:
    a config approved on Monday must not be delivered on Friday unchecked). A
    *blocking* change (the config is now invalid, or the `opamp` endpoint would move
    without `confirm_endpoint_change`) refuses with `409 re-validate`. A **policy**
    violation refuses too, unless `override_reason` is supplied — that override is
    recorded in the audit log as `config.override`.

    Approving supersedes each agent's previous active revision (kept for rollback) and
    resets the delivery record, so the revision is delivered exactly once per agent.

    Because the selector is resolved again here, an approval can cover *more* agents
    than it did at propose time. If it does, the request must repeat with
    `confirm_scope_change: true` — an approval that quietly widens its own blast
    radius is the failure this flow exists to prevent. The response lists `added`
    and `dropped` agents.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        info = config_control.summary(proposal_id)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    audit_action(
        principal,
        "config.approve",
        target_kind="proposal",
        target_id=proposal_id,
        config_hash=info["config_hash"],
        # The pre-write row records what was *proposed*; the selector is resolved
        # again inside the approval, so the authoritative per-agent record is each
        # revision's own `approved_by`/`approved_at` (#99 rule 4).
        proposed_agent_count=len(info["agents"]),
        confirm_endpoint_change=body.confirm_endpoint_change,
        confirm_scope_change=body.confirm_scope_change,
        override_reason=body.override_reason,
        note=body.note,
    )
    try:
        result = config_control.approve(
            proposal_id,
            approver=principal.name,
            agents=AGENT_REGISTRY.list_all(),
            allow_endpoint_change_confirmed=body.confirm_endpoint_change,
            confirm_scope_change=body.confirm_scope_change,
            override_reason=body.override_reason,
            note=body.note,
        )
    except config_control.ConfigControlError as exc:
        audit.audit_event(
            "config.approve",
            principal=principal.name,
            role=principal.role,
            result="error",
            target_kind="proposal",
            target_id=proposal_id,
            config_hash=info["config_hash"],
            error=str(exc.detail),
        )
        raise _control_error(exc)
    if body.override_reason:
        audit_action(
            principal,
            "config.override",
            target_kind="proposal",
            target_id=proposal_id,
            config_hash=info["config_hash"],
            reason=body.override_reason,
        )
    PROM_CONFIG_PUSHES.labels(result="approved").inc()
    # #102: approval is what makes a revision active, so this is the first moment it
    # may be delivered. Agents with a live connection get it now; the rest get it on
    # their next report, and `delivery.state` says which happened.
    result["delivery"] = _delivery_for_many(
        [revision["agent_id"] for revision in result.get("revisions", [])], principal
    )
    return result


@app.post(
    "/config/revisions/{proposal_id}/reject",
    tags=["config"],
    summary="Reject a proposed revision (approver)",
)
def reject_config_revision(
    proposal_id: str,
    body: RejectConfigRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.reject")),
):
    """Reject a proposal. Requires `approver`.

    The revision is **kept**, marked `rejected`: a config somebody proposed and a
    human refused is evidence, and deleting it would make "we never proposed that"
    unfalsifiable. Only `proposed` revisions can be rejected; an active one is
    withdrawn or rolled back instead.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        info = config_control.summary(proposal_id)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    audit_action(
        principal,
        "config.reject",
        target_kind="proposal",
        target_id=proposal_id,
        config_hash=info["config_hash"],
        reason=body.reason,
    )
    try:
        return config_control.reject(
            proposal_id, approver=principal.name, reason=body.reason
        )
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)


@app.post(
    "/config/revisions/{proposal_id}/reactivate",
    tags=["config"],
    summary="Reactivate a poisoned revision (approver)",
)
def reactivate_config_revision(
    proposal_id: str,
    body: ReactivateConfigRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.reactivate")),
):
    """Re-approve a poisoned revision and resume delivery. Requires `approver`.

    A `FAILED` report or a detected auto-revert poisons a revision permanently (D6):
    nothing reactivates on a timer, because "try it again and hope" against a live
    fleet is how a partial outage becomes a loop. Reactivation re-runs the validation
    pipeline first — the reason it failed may have been fixed since — and is audited.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        info = config_control.summary(proposal_id)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    audit_action(
        principal,
        "config.reactivate",
        target_kind="proposal",
        target_id=proposal_id,
        config_hash=info["config_hash"],
        reason=body.reason,
        override_reason=body.override_reason,
    )
    try:
        result = config_control.reactivate(
            proposal_id,
            approver=principal.name,
            agents=AGENT_REGISTRY.list_all(),
            reason=body.reason,
            allow_endpoint_change_confirmed=body.confirm_endpoint_change,
            override_reason=body.override_reason,
        )
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    # Reactivation clears the poison, so delivery may resume immediately (#102).
    result["delivery"] = _delivery_for_many(
        [revision["agent_id"] for revision in result.get("revisions", [])], principal
    )
    return result


@app.post(
    "/agent/{agent_id}/config/rollback",
    tags=["config"],
    summary="Roll an agent back to its previous revision (approver)",
)
def rollback_agent_config(
    agent_id: str,
    body: RollbackRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.rollback")),
):
    """Roll back to the agent's previous revision. Requires `approver`.

    Rollback is a **new revision** whose body is the previous one and whose
    `parent_revision_id` names it (D4: revisions are immutable, so rollback cannot be
    a pointer move that makes the audit trail lie). The revision it replaces is
    marked `rolled_back`, and the previous config is re-validated before delivery
    because the protected invariants and the agent's inventory may have moved.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    store = remote_config.REGISTRY.store
    if store.current(agent_id) is None and AGENT_REGISTRY.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    previous = store.previous(agent_id)
    if previous is None:
        raise HTTPException(
            status_code=409,
            detail=f"agent {agent_id} has no previous revision to roll back to",
        )
    revision_id = remote_config.new_revision_id()
    audit_action(
        principal,
        "config.rollback",
        target_kind="agent",
        target_id=agent_id,
        revision_id=revision_id,
        config_hash=previous.get("config_hash"),
        parent_revision_id=previous["revision_id"],
        note=body.note,
    )
    try:
        result = config_control.rollback(
            agent_id,
            author=principal.name,
            agents=AGENT_REGISTRY.list_all(),
            note=body.note,
            revision_id=revision_id,
        )
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    result["delivery"] = _delivery_for(agent_id, principal)
    return result


@app.get("/health", tags=["ops"], summary="Health check")
def health_check():
    """Liveness/readiness probe: server status, agent count, OPA availability, posture.

    Unauthenticated. Returns `{"status": "healthy", "agents_connected": n,
    "opa_enabled": bool, "opa_url": str|null, "alerts_enabled": true,
    "config_push": {"enabled", "insecure", "agent_auth", "audit", "tls",
    "config_at_rest"}, "desired_configs": n}`.
    The `config_push` block is the public posture summary — `insecure: true` means
    config push is on despite an unsafe posture and the UI shows a banner.
    `config_at_rest` is `encrypted`, `plaintext` (explicit development opt-out) or
    `unconfigured` (#98): a plaintext config store must never be invisible.
    Prometheus metrics are served separately at `GET /metrics`.
    """
    from server.opa_client import OPAClient
    opa_client = OPAClient()
    opa_available = OPA_ENABLED and opa_client.is_available()
    return {
        "status": "healthy",
        "agents_connected": AGENT_REGISTRY.count,
        "opa_enabled": opa_available,
        "opa_url": OPA_URL if OPA_ENABLED else None,
        "alerts_enabled": True,
        "config_push": posture.posture(),
        "desired_configs": remote_config.REGISTRY.count(),
    }


@app.get("/config/posture", tags=["config"], summary="Full config push security posture")
def config_posture(request: Request):
    """Full posture detail for operators and the UI — requires `viewer`.

    `GET /health` carries the one-line summary a banner needs; this endpoint adds
    *why*: which requirements are met and which are not, where the admin
    credential came from, and whether the development override is in force. No
    secrets are returned (never a password, never a token). Reads nothing an
    attacker cannot learn from `/health` plus one guess, so any role — including an
    anonymous viewer in read-only mode — may call it.
    """
    require_roles(request, ROLE_VIEWER)
    reqs = posture.requirements()
    return {
        "config_push": posture.posture(),
        "allowed": posture.push_allowed(),
        "override": posture.allow_insecure_config_push(),
        "requirements": {
            key: {"met": state["met"], "detail": state["detail"]}
            for key, state in reqs.items()
        },
        "unmet": posture.unmet_requirements(),
        "http": {
            "public_url": posture.public_url() or None,
            "tls": posture.tls_enabled(),
            "tls_terminated_upstream": posture.tls_terminated_upstream(),
        },
        "admin_auth": {
            "mode": "password" if admin_auth.configured() else "disabled",
            "source": admin_auth.source(),
        },
        "config_at_rest": crypto.describe(),
        "remote_config": {
            "revisions": remote_config.REGISTRY.count(),
            "max_bytes": remote_config.max_bytes(),
            "retention_days": remote_config.retention_days(),
            # #99/#100: the delivery and validation bounds, so an operator can see
            # *why* an agent stopped being offered a config.
            "max_delivery_attempts": remote_config.max_delivery_attempts(),
            "revert_grace_seconds": remote_config.revert_grace_seconds(),
            "validation": {
                "max_aliases": config_validation.max_aliases(),
                "max_depth": config_validation.max_depth(),
                "max_nodes": config_validation.max_nodes(),
                "allowed_exporter_hosts": list(config_validation.allowed_exporter_hosts()),
            },
        },
    }


if __name__ == "__main__":
    import sys

    import uvicorn

    admin_auth.ensure()
    agent_auth.warn_if_unenforced()
    try:
        # Fail here with a readable message and a non-zero exit; the lifespan hook
        # repeats the check for uvicorn/gunicorn startup.
        posture.assert_startup_posture()
    except posture.PostureError as exc:
        logger.error(str(exc))
        sys.exit(1)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=4320,
        keepalive_timeout=300,
        timeout_keep_alive=300,
        limit_concurrency=1000,
        backlog=1024,
        # uvicorn's own default (16 MiB) is lower than OPAMP_MAX_MESSAGE_BYTES,
        # which would pre-empt the app-level limit; keep the app authoritative.
        ws_max_size=message_limit(),
    )
