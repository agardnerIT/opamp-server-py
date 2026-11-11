import asyncio
import binascii
import json
import os
import sqlite3
from contextlib import nullcontext
from pathlib import Path
import prometheus_client as prom_client
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, Query, WebSocket, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field
from google.protobuf.json_format import MessageToDict
from loguru import logger
from typing import Optional, Dict, Any, List, Literal, NamedTuple, Tuple
from functools import wraps

from dotenv import load_dotenv

# The repository's `.env` is the single source of truth for a *deployment*. It is
# deliberately not authoritative for a process that imports this module to test
# something else: a developer with `OPAMP_CONFIG_PUSH_ENABLED=true` in their `.env`
# would otherwise find the posture tests red (or, worse, green for the wrong reason)
# for reasons that exist nowhere in the code. `OPAMP_NO_DOTENV` is the opt-out, set by
# `tests/conftest.py` before this module is imported.
if not os.environ.get("OPAMP_NO_DOTENV"):
    load_dotenv(Path(__file__).parent.parent / ".env")

# #108: configure loguru before importing anything that logs at import time (the
# alert store, the persistent agent registry). In a container this is what makes
# every line JSON, including the first one.
from server import logging as server_logging  # noqa: E402
from server.logging import RequestContextMiddleware  # noqa: E402

server_logging.configure_logging()

from proto.opamp_pb2 import (
    AgentCapabilities,
    AgentToServer,
    AgentToServerFlags,
    ServerCapabilities,
    ServerToAgent,
    ServerToAgentFlags,
)
from server.state import (
    AgentRegistry,
    state_database_health,
    AgentState,
    AGENT_REGISTRY,
    COLLECTOR_GROUPS,
    GroupError,
    COMPONENTS_IGNORED_LOG_AT,
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
    agent_query,
    audit,
    auth,
    commands,
    config_control,
    config_validation,
    crypto,
    custom_messages,
    groups,
    posture,
    profiles,
    protocol,
    redaction,
    remote_config,
    reports,
    rollout,
    sequence,
    throttle,
    tracing,
    updates,
)
from server.http_body import BodyTooLarge, UnsupportedEncoding, read_body
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
_rollout_task = None
_updates_task = None


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


async def rollout_loop():
    """Advance staged rollouts (#101) — one task, every tick, never a thread per rollout.

    The engine is synchronous (SQLite reads plus the bounded push wait), so it runs in a
    worker thread: a rollout that has to wait `CONFIG_PUSH_WAIT_SECONDS` for a socket
    must not stall heartbeats. Not started when config push is disabled — there is
    nothing it could legitimately do, and a background loop that only logs refusals is
    noise.
    """
    while True:
        await asyncio.sleep(rollout.tick_seconds())
        try:
            # Read per iteration, so an operator flipping the posture does not need a
            # restart to stop promotion.
            if not posture.push_allowed():
                continue
            await asyncio.to_thread(rollout.tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - the tick must never die
            logger.bind(event="config.rollout.tick_failed").warning(
                f"rollout tick failed: {exc}"
            )


async def updates_loop():
    """#46: one anonymous release check per interval, never on the request path.

    `UPDATE_CHECK=off` (or the admin toggle) makes this a cheap 60 s wake-up, so
    flipping the toggle does not need a restart. The HTTP call runs in a worker
    thread; a slow GitHub must not stall heartbeats.
    """
    while True:
        await asyncio.sleep(updates.seconds_until_check())
        if not updates.enabled() or not updates.due():
            continue
        try:
            await asyncio.to_thread(updates.check_once)
        except updates.TooSoon:
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - the loop must never die
            logger.bind(event="updates.loop_failed").warning(
                f"update loop failed: {exc}"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task, _rollout_task, _updates_task
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
    # #134: the profile dictionary is loaded on import (a bad file is logged and that
    # profile is absent, so a fleet is never down because of a YAML edit). With
    # `--strict-profiles` / `OPAMP_PROFILES_STRICT=true` the same validation is fatal,
    # which is what CI and the tests use: log-and-carry-on is exactly how a typo ships.
    if profiles.loader.strict_default():
        try:
            profiles.reload(strict=True)
        except profiles.ProfileLoadError as exc:
            logger.error(f"profile dictionary is invalid: {exc}")
            raise
    # Refuses to start (non-zero exit) when config push is enabled on an insecure
    # posture (#96). Read-only deployments are unaffected.
    posture.assert_startup_posture()
    # #108: the posture is public on /health already; publishing it as metrics is
    # what makes "someone left the insecure override on" a pager rule instead of a
    # thing you find by reading a JSON endpoint. Pure `metric_values()` in, gauges out.
    posture.publish_metrics()
    _log_audit_startup()
    # The first write is what surfaces a damaged database (every write becomes a 500
    # until the container restarts), so say it at boot rather than at that moment.
    db_ok, db_detail = state_database_health()
    if db_ok:
        logger.info(f"state database check: {db_detail}")
    else:
        logger.error(
            f"state database check failed ({db_detail}). Every write will answer 503: "
            "restart the container, and if it recurs move DATA_DIR off a bind mount."
        )
    # #98/#108: the log filter (redaction + correlation) is installed by
    # `configure_logging()` at import, before the first line is written. Calling
    # `redaction.install_log_filter()` here would replace the composed patcher and
    # drop `request_id` from every line, so it is deliberately not repeated.
    crypto.warn_if_plaintext()
    # #81: the desired-config cache is loaded once; the message path must not read
    # SQLite per message (#109).
    remote_config.REGISTRY.reload()
    # #78: the overload sampler. Cheap (one 100ms sleep loop) and harmless when the
    # gate is off — the gauge it publishes is worth having either way, because
    # "were we behind?" is the first question after a slow afternoon.
    throttle.start()
    _cleanup_task = asyncio.create_task(periodic_cleanup())
    # #101: the staged rollout engine. Separate from the 15s cleanup tick on purpose:
    # promotion latency and stale-agent reaping are unrelated knobs.
    _rollout_task = asyncio.create_task(rollout_loop())
    # #46: the update check. It is a no-op when UPDATE_CHECK=off.
    _updates_task = asyncio.create_task(updates_loop())
    yield
    if _cleanup_task:
        _cleanup_task.cancel()
    if _rollout_task:
        _rollout_task.cancel()
    if _updates_task:
        _updates_task.cancel()
    throttle.stop()
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

# #70: `ReportFullState` asks, by reason. `new_agent` is the registration baseline;
# `sequence_gap` is a status report the spec says we know to be incomplete.
PROM_FULL_STATE_REQUESTS = prom_client.Counter(
    "opamp_full_state_requested_total",
    "ServerToAgent ReportFullState requests sent to agents, by reason",
    ["reason"],
)

# #77: hash-only component lists. `hash_only` is the first ask for a hash, `hash_changed`
# a re-ask after the agent's component list moved (an upgrade, a pipeline edit).
PROM_COMPONENTS_REQUESTS = prom_client.Counter(
    "opamp_available_components_requested_total",
    "ServerToAgent ReportAvailableComponents requests sent to agents, by reason",
    ["reason"],
)

# #77: agents that kept sending a hash-only list after being asked for the details.
# This is the signal that an agent implementation ignores the flag, which is otherwise
# invisible: the ask succeeds, the reply is spec-shaped, and nothing changes.
PROM_COMPONENTS_IGNORED = prom_client.Counter(
    "opamp_available_components_ignored_total",
    "Agents counted as ignoring a ReportAvailableComponents request",
)

# #75: one uid, two agents. `action` is `reissued` when the agent was given a new
# uid, `detected` when it was not (agent authentication, where the credential is the
# identity, or the feature switched off).
PROM_DUPLICATE_UIDS = prom_client.Counter(
    "opamp_duplicate_instance_uid_total",
    "Suspected duplicate instance_uids, by signal and action taken",
    ["reason", "action"],
)

# #76: `OpAMP-Instance-UID` header values that were not usable, by reason. Separate
# from duplicate detection: this counts what the client *said*, not what it did.
PROM_UID_HEADER_PROBLEMS = prom_client.Counter(
    "opamp_instance_uid_header_problems_total",
    "OpAMP-Instance-UID request headers that were malformed or disagreed with the body",
    ["reason"],
)

# #111: collector groups. A group is a saved selector, so "how many groups exist" and
# "what does this one hit right now" are the two numbers an operator asks for. The
# label is the group *name* (operator-controlled, few), never an agent id.
PROM_GROUPS = prom_client.Gauge(
    "opamp_groups_total",
    "Collector groups (saved selectors), by archive state",
    ["state"],
)
PROM_GROUP_MEMBERS = prom_client.Gauge(
    "opamp_group_members",
    "Live membership of a collector group, resolved at scrape time",
    ["group"],
)
# #85: custom messages, by direction. `queued` is an operator asking for something
# the agent may or may not honour; `delivered` is the server keeping its side.
PROM_CUSTOM_MESSAGES = prom_client.Counter(
    "opamp_custom_messages_total",
    "Custom messages queued for or delivered to agents, by direction",
    ["direction"],
)

PROM_GROUP_MEMBERSHIP_CHANGES = prom_client.Counter(
    "opamp_group_membership_changes_total",
    "Group resolutions recorded, by reason (preview, propose, push, approve)",
    ["reason"],
)

# #78: how many cleanup ticks stood down because the server was shedding load. A
# reaper that silently stopped for an hour is an outage nobody notices until the
# dashboard is full of corpses, so the deferral is a number, not only a log line.
PROM_REAPER_DEFERRALS = prom_client.Counter(
    "opamp_stale_reaper_deferrals_total",
    "Stale-agent cleanup ticks skipped because the server was overloaded (#78)",
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

# #71: the spec says a server SHOULD gzip its reply when the client advertised
# `Accept-Encoding: gzip`, and opamp-go only decompresses when it sees the
# `Content-Encoding` header — so honouring the request is the whole feature. The
# floor keeps a 40-byte capabilities reply plain, where gzip costs more than it
# saves. Starlette's middleware negotiates the encoding, skips responses that
# already set `Content-Encoding` (an error page from a proxy, say), and adds
# `Vary: Accept-Encoding` so a cache cannot serve gzip to a client that cannot read
# it. It passes non-HTTP scopes straight through, so the WebSocket transport is
# unaffected.
GZIP_MIN_RESPONSE_BYTES = 1024
app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_RESPONSE_BYTES)

# #108: outermost, so even a CORS rejection and a 4xx from a route carries a
# request id and exactly one completion line. WebSocket scopes pass through
# untouched (the socket layer binds `conn_id` itself).
app.add_middleware(RequestContextMiddleware)

# #108: tracing must be wired *before* Starlette builds the middleware stack, so it
# cannot happen inside the lifespan (adding middleware to a started app raises).
# `init_tracing` is a no-op unless OPAMP_TRACING_ENABLED=true.
tracing.init_tracing(app)


#: How a damaged state database is reported. The operator gets a next step, not a
#: stack trace: this exact failure (SQLite "database disk image is malformed") turns
#: every write into a 500 until the container is restarted, and a bare
#: "Internal Server Error" gives nobody anything to do about it.
STATE_DB_UNAVAILABLE = (
    "The server's state database could not be read or written. Nothing was changed. "
    "Restart the server container (`docker compose restart server`): a restart re-reads "
    "the write-ahead log and rebuilds the shared-memory index, which is what clears a "
    "stale WAL view after the container was stopped mid-write. If it comes back, the "
    "data directory is on a filesystem SQLite cannot use safely — a macOS bind mount "
    "inside Docker is the usual one — so move it to a named volume."
)


@app.exception_handler(sqlite3.DatabaseError)
async def state_database_unavailable(request: Request, exc: sqlite3.DatabaseError):
    """503 with an actionable message instead of an opaque 500.

    Deliberately does not attempt a repair: rewriting a database underneath the
    process that just failed to read it is how a bad afternoon becomes data loss.
    The retry is "restart the process", which is both safe and effective.
    """
    logger.error(
        f"state database error on {request.method} {request.url.path}: {exc}"
    )
    return JSONResponse(
        status_code=503,
        content={"detail": STATE_DB_UNAVAILABLE, "error": str(exc)},
        headers={"Retry-After": "5"},
    )


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
            
            agent_id = _identity_from_resource(
                [
                    (attr.get("key"), (attr.get("value") or {}).get("stringValue"))
                    for resource in data.get("resourceMetrics", [])
                    for attr in resource.get("resource", {}).get("attributes", [])
                ]
            )
            
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
            
            agent_id = _identity_from_resource(
                [
                    (attr.key, attr.value.string_value)
                    for resource in metrics_req.resource_metrics
                    for attr in resource.resource.attributes
                ]
            )
            
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


#: OTLP resource attributes that identify the reporting agent, in preference order.
#: A product that uses another attribute is **one entry here**, not a code change (#122).
IDENTITY_ATTRIBUTES = ("service.instance.id", "service.name")


def _identity_from_resource(attributes) -> str:
    """The agent id an OTLP resource describes, or `""` when it identifies nothing (#122).

    The derivation is deliberately dumb — preference order, then dashes stripped, because
    an OpAMP instance uid is hex and a `service.name` is not — and it exists **once**, so
    the JSON and protobuf branches of `POST /v1/metrics` cannot drift apart. They had two
    copies of the same expression, which is how one of them eventually grows a fix the
    other does not have.

    An unknown identity is not an error: ingestion is best-effort by design, the caller
    logs at DEBUG and drops the batch. Changing the attribute table is a policy change for
    who may write metrics, so it is data (`IDENTITY_ATTRIBUTES`) rather than a branch.
    """
    values = {str(key): (value or "") for key, value in attributes if key}
    for name in IDENTITY_ATTRIBUTES:
        candidate = str(values.get(name) or "").strip()
        if candidate:
            return candidate.replace("-", "")
    return ""


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
    # #78/#69: an agent told to "retry after 30s" cannot move its heartbeat until it
    # gets through, so reaping during a throttle storm deletes exactly the agents
    # that are behaving correctly. Stand down for the fleet rather than guess which
    # silent agents are throttled: the 503 path never parses a body, so it does not
    # know their ids either.
    if throttle.hold_reaper():
        PROM_REAPER_DEFERRALS.inc()
        logger.bind(
            event="agent.reaper.deferred",
            reason="overload",
            lag_seconds=round(throttle.last_lag_seconds(), 3),
            agents=AGENT_REGISTRY.count,
        ).debug(
            "skipping stale-agent cleanup: server is shedding load (#78)"
        )
        return []
    stale = []

    for agent in AGENT_REGISTRY.list_all():
        if not agent.is_stale(
            now,
            AGENT_TIMEOUT_SECONDS,
            AGENT_TIMEOUT_INTERVAL_MARGIN,
            AGENT_FIRST_CONTACT_SECONDS,
        ):
            continue
        timeout = agent.timeout_seconds(
            AGENT_TIMEOUT_SECONDS,
            AGENT_TIMEOUT_INTERVAL_MARGIN,
            AGENT_FIRST_CONTACT_SECONDS,
        )
        silent = (now - agent.last_heartbeat).total_seconds()
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
        DUPLICATE_UID_DETECTORS.forget(agent.agent_id)
        send_stale_agent_alert(agent.agent_id)

    update_metrics()
    return [agent.agent_id for agent, _, _ in stale]


HEARTBEAT_PERSIST_SECONDS = 60  # bounds post-restart heartbeat skew (#109)


def report_full_state_interval() -> float:
    """Minimum seconds between `ReportFullState` asks for one agent (#70).

    Read per call, not at import, so a test (or an operator restarting with a
    different value) sees the change without reimporting the module. `0` disables the
    rate limit, which is only useful for testing: in production it lets one agent
    with a broken counter turn every poll into a full-state request.
    """
    raw = os.environ.get("REPORT_FULL_STATE_MIN_INTERVAL_SECONDS", "60")
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return value if value >= 0 else 60.0


def duplicate_uid_detection_enabled() -> bool:
    """Is duplicate-`instance_uid` detection on? (#75) `OPAMP_DUPLICATE_UID_DETECTION`.

    Default on: the cost of a false positive is one extra fleet entry, and the cost of
    a missed duplicate is two agents silently overwriting each other. Read per call so
    an operator can turn it off in a test or an emergency without a code change; when
    it is off nothing is even observed, which is the honest meaning of "disabled".
    """
    raw = os.environ.get("OPAMP_DUPLICATE_UID_DETECTION", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


# #75: one detector for the process. It is traffic analysis (in-memory), so it is not
# part of `AGENT_REGISTRY`; the *outcome* (the replacement uid and why) is persisted
# on the agent row. Tests that need their own instance monkeypatch this name.
DUPLICATE_UID_DETECTORS = protocol.DuplicateUidDetector()


def uid_header_strict() -> bool:
    """Does an `OpAMP-Instance-UID`/body mismatch refuse the message? (#76)

    Off by default: the body is authoritative, and a proxy that rewrites headers is a
    legitimate deployment. When on, a mismatch is a `BAD_REQUEST` (HTTP 400, WS 1002).
    """
    raw = os.environ.get(protocol.STRICT_UID_HEADER_ENV, "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def check_uid_header(raw: str, instance_uid: bytes, transport: str) -> None:
    """Cross-check the `OpAMP-Instance-UID` header against the body uid (#76).

    Three outcomes, all of them cheap and none of them able to break a working client:

    * no header, or one that agrees with the body — nothing happens;
    * a value we cannot read as a uuid (and that is not the body uid written as plain
      ascii, which is how every fixture and harness in this repo names its agents) —
      one warning and one metric, then the body is believed;
    * a readable value that disagrees with the body — one warning naming *both* uids
      and one metric, then the body is believed again unless strict mode is on, in
      which case the message is refused.

    Raised as `protocol.BadRequest` rather than an `HTTPException` so the socket layer
    answers exactly like HTTP: error frame, then close 1002.
    """
    if not raw:
        return
    stated = protocol.parse_instance_uid_header(raw)
    if stated is None and raw.strip().encode("utf-8", "replace") == instance_uid:
        # Lenient by design: non-UUID ids are how this project's own clients name
        # themselves, and a header that matches the body is correct whatever it looks
        # like.
        return
    if stated is None:
        PROM_UID_HEADER_PROBLEMS.labels(reason="malformed").inc()
        logger.bind(
            event="opamp.uid_header.malformed",
            transport=transport,
            instance_uid=instance_uid.hex(),
            header_value=raw[:64],
        ).warning(
            f"Ignoring unreadable {protocol.INSTANCE_UID_HEADER} header "
            f"{raw[:64]!r}; using the message body ({instance_uid.hex()})"
        )
        return
    if stated == instance_uid:
        return
    PROM_UID_HEADER_PROBLEMS.labels(reason="mismatch").inc()
    logger.bind(
        event="opamp.uid_header.mismatch",
        transport=transport,
        instance_uid=instance_uid.hex(),
        header_uid=stated.hex(),
        strict=uid_header_strict(),
    ).warning(
        f"{protocol.INSTANCE_UID_HEADER} header says {stated.hex()} but the message "
        f"body says {instance_uid.hex()}; trusting the body"
    )
    if uid_header_strict():
        raise protocol.BadRequest(
            f"{protocol.INSTANCE_UID_HEADER} header ({stated.hex()}) does not match "
            f"AgentToServer.instance_uid ({instance_uid.hex()})",
            instance_uid=instance_uid,
        )


def _components_hash(report: Dict[str, Any]) -> Optional[str]:
    """The `AvailableComponents.hash` as hex (our storage form), or None (#77).

    `MessageToDict` renders the proto `bytes` as base64, so it is decoded exactly like
    a remote-config hash. A value that is not valid base64 is kept verbatim: the hash is
    only ever an identity token for "did this change since we asked?", never compared
    against one we compute, so the cheapest safe thing is to trust what the agent sent.
    """
    raw = (report or {}).get("hash")
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return binascii.hexlify(bytes(raw)).decode()
    return remote_config.hash_from_wire(raw) or str(raw)


def _note_available_components(
    agent: Optional[AgentState],
    report: Dict[str, Any],
    updates: Dict[str, Any],
) -> Tuple[Optional[str], Optional[int]]:
    """Decide whether a hash-only `available_components` report needs the details (#77).

    The spec lets an agent send only the `hash` unless the server set
    `ReportAvailableComponents`, so without this the OCB manifest feature is
    permanently stuck at 409 for such an agent. The bookkeeping is written into
    `updates` so it rides the message's one row write (#109). Returns:

    * `reason` — `hash_only` / `hash_changed` when this reply must set the flag;
    * `ignored` — the report count when it first reaches `COMPONENTS_IGNORED_LOG_AT`,
      so the caller warns exactly once and a silently non-compliant agent is visible.

    One-shot per hash: the hash we asked about is the marker, so an agent that keeps
    sending it is not asked again, and one that changes it (upgrade, pipeline edit) is
    asked once more.
    """
    components = (report or {}).get("components") or {}
    if components:
        # The full list arrived, so whatever we asked for has been answered. The hash
        # stays as the marker: it is still the list we last asked about.
        updates["components_request_ignored"] = 0
        return None, None

    reported = _components_hash(report)
    if reported is None:
        # No hash and no components: there is nothing to key a one-shot ask on, so
        # asking would be a request on every message — exactly the loop to avoid.
        return None, None

    if agent is not None and agent.last_requested_components_hash == reported:
        previous = agent.components_request_ignored or 0
        ignored = min(previous + 1, COMPONENTS_IGNORED_LOG_AT)
        updates["components_request_ignored"] = ignored
        report_ignored = previous < COMPONENTS_IGNORED_LOG_AT <= ignored
        return None, ignored if report_ignored else None

    updates["last_requested_components_hash"] = reported
    updates["components_request_ignored"] = 0
    first_ask = agent is None or agent.last_requested_components_hash is None
    return ("hash_only" if first_ask else "hash_changed"), None


class StatusReport(NamedTuple):
    """What `_apply_status_report` decided about one message."""

    changed: bool
    full_state_reason: Optional[str] = None
    components_reason: Optional[str] = None  # #77
    components_ignored: Optional[int] = None  # #77: count when it reached the threshold


def _apply_status_report(
    agent_id: str,
    agent_dict: Dict[str, Any],
    persist: bool = True,
    sequence_num: Optional[int] = None,
) -> StatusReport:
    """Apply the status fields present in any valid agent message.

    Fields are collected and applied to the in-memory state in one pass, then
    persisted at most once: a message that changes nothing performs no write at
    all, and one that changes N field groups performs exactly one (#109).

    Returns a `StatusReport`. `full_state_reason` is `None` unless the message is
    *known* to be an incomplete picture of the agent (#70), in which case the caller
    sets `ReportFullState` on the reply — subject to its own rate limit, which this
    function deliberately does not apply so the decision stays testable and the policy
    stays in one place (`AgentRegistry.claim_full_state_request`).

    `components_reason` is the mirror image for #77: the agent sent only a hash of its
    component list, so the caller sets `ReportAvailableComponents` — once per hash.
    The two asks are independent and may both appear on one reply.
    """
    updates: Dict[str, Any] = {}
    full_state_reason: Optional[str] = None
    components_reason: Optional[str] = None
    components_ignored: Optional[int] = None

    # #70: read the previous counter *before* recording this one, or every message
    # is compared against itself and no gap is ever seen.
    if sequence_num:
        agent = AGENT_REGISTRY.get(agent_id)
        previous = agent.last_sequence_num if agent is not None else None
        if sequence.is_gap(previous, sequence_num):
            full_state_reason = "sequence_gap"
        updates["last_sequence_num"] = sequence_num

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
        report = agent_dict['availableComponents'] or {}
        updates['available_components'] = report
        components_reason, components_ignored = _note_available_components(
            AGENT_REGISTRY.get(agent_id), report, updates
        )
        PROM_MESSAGES_RECEIVED.labels(message_type="available_components").inc()

    if 'packageStatuses' in agent_dict:
        updates['package_statuses'] = agent_dict['packageStatuses']
        PROM_MESSAGES_RECEIVED.labels(message_type="package_status").inc()

    # #85: the whole list, replaced on every report (the spec's message carries all of
    # it), through the same batched write as every other field group (#109).
    if 'customCapabilities' in agent_dict:
        capabilities = custom_messages.note_capabilities(
            agent_id, agent_dict['customCapabilities'] or {}
        )
        if capabilities is not None:
            updates['custom_capabilities'] = capabilities
        PROM_MESSAGES_RECEIVED.labels(message_type="custom_capabilities").inc()

    # #85: an agent->server custom message. Logged, never interpreted or stored: the
    # payload is capability-defined binary and a column of blobs is how a fleet's data
    # ends up in the admin database by accident.
    if 'customMessage' in agent_dict:
        custom_messages.note_inbound(agent_id, agent_dict['customMessage'] or {})
        PROM_MESSAGES_RECEIVED.labels(message_type="custom_message").inc()

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
        return StatusReport(changed, full_state_reason, components_reason, components_ignored)

    # Steady state: nothing changed, so skip the write unless the stored
    # heartbeat is old enough that a restart would look like a stale fleet.
    if changed or AGENT_REGISTRY.heartbeat_persist_due(agent_id, HEARTBEAT_PERSIST_SECONDS):
        AGENT_REGISTRY.persist(agent_id)
        return StatusReport(True, full_state_reason, components_reason, components_ignored)
    return StatusReport(False, full_state_reason, components_reason, components_ignored)


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
    uid_header: str = "",
) -> ProcessedMessage:
    """Apply one `AgentToServer` message and build the `ServerToAgent` reply.

    Shared by the HTTP and WebSocket transports so the two cannot drift; the
    transport only differs in framing and connection lifecycle. Returns the
    serialized reply plus the fields the socket layer needs for its registry.

    `principal` is who presented the credential (#94); `None` in `off` mode. A
    credential that does not match the claimed `instance_uid`, or a uid already
    bound to a different principal, raises `AgentAuthError` — before any state is
    read or written, so a refusal can never leave a half-applied report behind.

    A body that is not a usable `AgentToServer` raises `protocol.BadRequest` (#73):
    the HTTP transport answers 400 and the socket closes 1002, each after sending a
    `ServerErrorResponse{BAD_REQUEST}` with a human-readable reason. Nothing is
    registered or mutated, because every check here runs before `_apply`.

    `uid_header` is the raw `OpAMP-Instance-UID` request header (#76). It is only ever
    a signal — cross-checked against the body, never trusted over it — and a mismatch
    is a warning unless `OPAMP_UID_HEADER_STRICT` turns it into a `BAD_REQUEST`.

    `transport` is only for observability (#108): the log line carries it, and the
    `opamp.message` span records `transport=http|ws`, which is the first question
    asked when one transport misbehaves and the other does not.
    """
    response = ServerToAgent()
    response.capabilities = 0

    if not data:
        # Bare poll, kept for compatibility. The spec expects a uid on every
        # AgentToServer (even a poll), but an empty body has always been answered
        # with capabilities here and refusing it would break existing clients.
        response.capabilities = ServerCapabilities.ServerCapabilities_AcceptsStatus
        return ProcessedMessage(response.SerializeToString())

    try:
        agent_msg = AgentToServer()
        agent_msg.ParseFromString(data)
    except Exception as e:
        raise protocol.BadRequest(f"malformed AgentToServer message: {e}") from e

    original_uid = bytes(agent_msg.instance_uid)
    if len(original_uid) < protocol.MIN_ACCEPTED_UID_BYTES:
        # An empty uid is not an identity: every such agent would share agent_id
        # "" and overwrite the others in one registry row and SQLite row.
        raise protocol.BadRequest(
            "AgentToServer.instance_uid must not be empty; "
            f"the spec says {protocol.EXPECTED_INSTANCE_UID_BYTES} bytes (UUIDv7)"
        )

    wants_uid = bool(
        agent_msg.flags & AgentToServerFlags.AgentToServerFlags_RequestInstanceUid
    )
    agent_id = binascii.hexlify(original_uid).decode('utf-8')
    agent_dict = MessageToDict(agent_msg)

    # #76: the header is a hint, so check it before touching state — a strict-mode
    # mismatch is a refusal, and the body is still the identity either way.
    check_uid_header(uid_header, original_uid, transport)

    if principal is not None:
        # A bound token *is* the identity, so a mismatch is refused here; a fleet token
        # authorises access and is counted against its cap (#126). Either way this runs
        # before `_apply_status_report`, so a refusal cannot mutate state even briefly.
        agent_auth.authorize_agent(principal, agent_id)
        if wants_uid:
            # A server-minted uid has no credential behind it, so honouring this in
            # token mode would create an unauthenticated agent (D1). Refuse instead.
            raise protocol.BadRequest(
                "RequestInstanceUid is only available when OPAMP_AGENT_AUTH=off; "
                "with agent authentication the credential is the identity",
                instance_uid=original_uid,
            )
        # Keyed on the credential, not the uid: two agents behind one fleet token are
        # the same principal and must not fight over a socket, while a second *token*
        # taking over an agent's binding is exactly what this refuses (#94, #126).
        bound = WS_REGISTRY.principal_for(original_uid)
        if bound is not None and bound != principal.token_id:
            raise agent_auth.AgentAuthError(
                agent_auth.REASON_BINDING_CONFLICT,
                "instance_uid is bound to a different credential",
                principal=principal.token_id,
                claimed=agent_id,
            )

    # #73: the server may name the agent. The temporary uid the agent sent is not
    # registered at all; state for this message is applied under the new id. If the
    # temporary uid was already a real record (a restart reusing a placeholder), it
    # is cleaned up after the new identity exists, so no stale duplicate is left.
    effective_uid = original_uid
    minted_uid = None
    if wants_uid:
        minted_uid = protocol.uuid7()
        effective_uid = minted_uid
        agent_id = binascii.hexlify(effective_uid).decode('utf-8')
        logger.bind(
            event="agent.uid_minted",
            temporary_uid=original_uid.hex(),
            agent_id=agent_id,
            transport=transport,
        ).info(
            f"Minted instance_uid {agent_id} for agent that sent "
            f"temporary uid {original_uid.hex()}"
        )

    # #75: two agents can present one uid — a cloned VM, a restored backup, a broken
    # id generator. One agent renaming its `service.instance.id` in place must *not*
    # trigger this, so the detector fires on interleaving (see `DuplicateUidDetector`)
    # rather than on any change of identity.
    reissued_from = None
    reissue_reason = None
    if not wants_uid and duplicate_uid_detection_enabled():
        reason = DUPLICATE_UID_DETECTORS.observe(
            agent_id,
            protocol.host_identity(agent_dict.get("agentDescription")),
            agent_msg.sequence_num,
        )
        if reason is not None:
            # A replacement uid is only usable when the credential is not the
            # identity: a token is bound to the uid it was minted for (#94), so
            # handing an authenticated agent a new uid would get it refused on its
            # next message. Detect, count and log there; never silently break it.
            action = "detected" if principal is not None else "reissued"
            PROM_DUPLICATE_UIDS.labels(reason=reason, action=action).inc()
            if principal is None:
                reissued_from = agent_id
                reissue_reason = reason
                effective_uid = protocol.uuid7()
                agent_id = binascii.hexlify(effective_uid).decode('utf-8')
                DUPLICATE_UID_DETECTORS.confirm(reissued_from)
            logger.bind(
                event="agent.duplicate_uid",
                reason=reason,
                action=action,
                original_uid=reissued_from or agent_msg.instance_uid.hex(),
                agent_id=agent_id,
                transport=transport,
            ).warning(
                f"Duplicate instance_uid {agent_msg.instance_uid.hex()} "
                f"({reason}): "
                + (
                    f"issued replacement uid {agent_id}"
                    if reissued_from
                    else "not re-issuing, the credential is bound to the uid (#94)"
                )
            )

    # Bound here, not per log call: every line written while applying this message
    # (the offer decision, the audit write, the alert it may trigger) carries the
    # same `agent_id` without threading it through every function (#108).
    with logger.contextualize(agent_id=agent_id, transport=transport):
        return _apply_agent_message(
            agent_msg,
            agent_dict,
            agent_id,
            principal,
            transport,
            instance_uid=effective_uid,
            reply_uid=original_uid,
            minted_uid=minted_uid,
            reissued_from=reissued_from,
            reissue_reason=reissue_reason,
        )


def _apply_agent_message(
    agent_msg: AgentToServer,
    agent_dict: Dict[str, Any],
    agent_id: str,
    principal: "agent_auth.Principal | None",
    transport: str,
    *,
    instance_uid: bytes | None = None,
    reply_uid: bytes | None = None,
    minted_uid: bytes | None = None,
    reissued_from: str | None = None,
    reissue_reason: str | None = None,
) -> ProcessedMessage:
    """The state-mutating half of `_handle_agent_message`, inside the log/trace scope.

    `minted_uid` is a replacement uid the server chose for an agent that sent none
    (spec §Requesting instance_uid) and `reissued_from` is the *old* uid of an agent
    whose uid turned out to be a duplicate (#75). Both end up in
    `agent_identification.new_instance_uid`, and they differ in one thing that
    matters: a minted temporary uid was never a real record, so it is thrown away,
    while a re-issued uid belongs to a live agent whose own record must survive.
    """
    effective_uid = bytes(agent_msg.instance_uid) if instance_uid is None else instance_uid
    if minted_uid is None and reissued_from is not None:
        # #75: the replacement travels in `agent_identification`, addressed to the
        # uid the agent used in this message.
        minted_uid = effective_uid
    response = ServerToAgent()
    # The reply is addressed to the uid the agent used in *this* message; a minted
    # replacement travels separately in `agent_identification.new_instance_uid`.
    response.instance_uid = effective_uid if reply_uid is None else reply_uid

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
                instance_uid=effective_uid,
                agent_id=agent_id,
                capabilities=agent_msg.capabilities,
                description=agent_dict.get("agentDescription", {}),
                # #75: the fleet view has to explain why this agent's id changed,
                # which is the difference between "the server renamed me" and
                # "something is cloning VMs".
                uid_reissued_from=reissued_from,
                uid_reissue_reason=reissue_reason,
            ), persist=False)
            response.flags = ServerToAgentFlags.ServerToAgentFlags_ReportFullState
            PROM_FULL_STATE_REQUESTS.labels(reason="new_agent").inc()
            update_metrics()

            send_new_agent_alert(agent_id)

        # One write for the whole message (#109): `_apply_status_report` batches the
        # field groups and flushes the row at most once, including for a new agent.
        status = _apply_status_report(
            agent_id, agent_dict, sequence_num=agent_msg.sequence_num
        )
        full_state_reason = status.full_state_reason

        # #70: the agent told us — by its sequence number — that it omitted status we
        # never received, so ask for the whole picture. Never guess the missing
        # fields, and never ask a disconnecting agent for anything. The rate limit is
        # what stops a broken counter from becoming a ReportFullState storm; a
        # suppressed ask is not lost, because the next message is still a gap.
        if full_state_reason and 'agentDisconnect' not in agent_dict:
            if AGENT_REGISTRY.claim_full_state_request(
                agent_id, report_full_state_interval()
            ):
                response.flags |= ServerToAgentFlags.ServerToAgentFlags_ReportFullState
                PROM_FULL_STATE_REQUESTS.labels(reason=full_state_reason).inc()
                logger.bind(
                    event="opamp.report.incomplete",
                    reason=full_state_reason,
                    sequence_num=agent_msg.sequence_num,
                    agent_id=agent_id,
                    transport=transport,
                ).info(
                    f"Status report from {agent_id} is incomplete "
                    f"(sequence_num={agent_msg.sequence_num}); requesting full state"
                )

        # #77: the agent sent only the `hash` of its component list, so we cannot build
        # an OCB manifest (or a component report) from it. Ask for the details — once
        # per hash, because asking on every poll is a request loop the agent has no way
        # to leave. Independent of the #70 ask above: a message can need both.
        if status.components_reason and 'agentDisconnect' not in agent_dict:
            response.flags |= (
                ServerToAgentFlags.ServerToAgentFlags_ReportAvailableComponents
            )
            PROM_COMPONENTS_REQUESTS.labels(reason=status.components_reason).inc()
            requested = AGENT_REGISTRY.get(agent_id)
            logger.bind(
                event="opamp.components.requested",
                reason=status.components_reason,
                agent_id=agent_id,
                transport=transport,
                components_hash=(
                    requested.last_requested_components_hash if requested else None
                ),
            ).info(
                f"Agent {agent_id} reported only a component hash "
                f"({status.components_reason}); requesting full available_components"
            )

        # #77: it ignored the ask. Not an error the agent can see, so it has to be a
        # number and a log line or nobody ever finds out the agent's SDK is incomplete.
        if status.components_ignored and 'agentDisconnect' not in agent_dict:
            PROM_COMPONENTS_IGNORED.inc()
            logger.bind(
                event="opamp.components.ignored",
                agent_id=agent_id,
                transport=transport,
                reports=status.components_ignored,
            ).warning(
                f"Agent {agent_id} has ignored {status.components_ignored} "
                f"ReportAvailableComponents requests (still sending hash only); its "
                f"components stay unknown, so manifests and component reports are empty"
            )

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
            DUPLICATE_UID_DETECTORS.forget(agent_id)
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

        # #84: a queued command replaces the reply for exactly one poll. The spec is
        # explicit (specification.md:1094): when `command` is set the server sets no
        # other field besides instance_uid/capabilities, so the config offer above is
        # dropped for this message and re-offered on the next one — never both.
        if not disconnected:
            command = commands.take(agent_id)
            if command is not None:
                command_response = ServerToAgent()
                command_response.instance_uid = response.instance_uid
                command_response.capabilities = response.capabilities
                command_response.command.CopyFrom(command)
                tracing.set_span_attributes(
                    sp, **{"reply.command": command.type, "reply.remote_config_offered": False}
                )
                return ProcessedMessage(
                    command_response.SerializeToString(),
                    instance_uid=effective_uid,
                    disconnected=False,
                )

        # #85: a queued custom message rides this reply, and the config offer is
        # dropped for this poll so one reply carries one server-initiated action —
        # the same attribution rule as #84. The offer is simply re-made next poll.
        if not disconnected:
            custom = custom_messages.take(agent_id)
            if custom is not None:
                if offered:
                    response.ClearField("remote_config")
                    offered = False
                response.custom_message.CopyFrom(custom)
                PROM_CUSTOM_MESSAGES.labels(direction="delivered").inc()
                tracing.set_span_attributes(
                    sp,
                    **{
                        "reply.custom_message": custom.type,
                        "reply.remote_config_offered": False,
                    },
                )

        # Last line of defence for #81/#102: nothing may put remote_config on the
        # wire while the posture says config push is not allowed.
        posture.scrub_remote_config(response)

        # #73: a server-minted identity replaces the temporary uid the agent used.
        # The reply still carries that temporary uid (multiplexing), while state and
        # future messages are keyed by the minted one. The temporary record is removed
        # rather than migrated: it was a placeholder, never an established agent.
        # #75 uses the same field for a *re-issued* uid, but there the old record is a
        # live agent's own state and must never be deleted.
        if minted_uid is not None:
            response.agent_identification.new_instance_uid = minted_uid
            temporary_uid = effective_uid if reply_uid is None else reply_uid
            temporary_id = binascii.hexlify(temporary_uid).decode('utf-8')
            if (
                reissued_from is None
                and temporary_id != agent_id
                and AGENT_REGISTRY.get(temporary_id) is not None
            ):
                logger.bind(
                    event="agent.uid_minted_cleanup",
                    temporary_uid=temporary_id,
                    agent_id=agent_id,
                    transport=transport,
                ).info(
                    f"Removing temporary identity {temporary_id} after minting {agent_id}"
                )
                AGENT_REGISTRY.remove(temporary_id)
                PROM_AGENT_HEALTH.remove(temporary_id)
                _METRICS_STORE.delete(temporary_id)
                AGENT_METRICS.pop(temporary_id, None)
                update_metrics()

        tracing.set_span_attributes(
            sp,
            **{
                "reply.capabilities": response.capabilities,
                "reply.remote_config_offered": offered,
                "reply.instance_uid_minted": minted_uid is not None,
            },
        )

    return ProcessedMessage(
        response.SerializeToString(),
        instance_uid=effective_uid,
        disconnected=disconnected,
    )


# #76: read the `OpAMP-Instance-UID` header on every request. The body stays the
# identity; the header is the fallback for bodies we cannot read (413, BAD_REQUEST) and
# a cross-check for the rest.
INSTANCE_UID_HEADER = protocol.INSTANCE_UID_HEADER


def _instance_uid_header(request: Request) -> str:
    """The raw header value, for log lines that have no parsed body to name."""
    return request.headers.get(INSTANCE_UID_HEADER, "")


def _bad_request_response(exc: protocol.BadRequest, status_code: int = 400) -> Response:
    """400 carrying a spec-shaped `ServerErrorResponse`, so the agent can read why.

    `status_code` is 415 for an encoding we do not implement (#71): the body is still
    a `BAD_REQUEST` error_response, because that is the only shape the spec defines.
    """
    return Response(
        content=protocol.error_response(exc.detail, exc.instance_uid).SerializeToString(),
        status_code=status_code,
        media_type="application/x-protobuf",
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

    **Malformed input** (unparseable protobuf, empty `instance_uid`, corrupt gzip) is
    **400** with a `ServerToAgent.error_response` (`type: BAD_REQUEST`,
    human-readable `error_message`) and changes no state. A body over
    `OPAMP_MAX_MESSAGE_BYTES` — measured after gzip decompression — is **413**, and a
    `Content-Encoding` this server does not implement is **415**; both likewise
    change nothing.

    **Compression** (#71): a gzipped request body is unrolled *inside* the bounded
    read, so a decompression bomb is refused at the limit instead of being expanded
    first. The reply is gzipped when the client advertised `Accept-Encoding: gzip`
    and the body is large enough to be worth it.

    **Incomplete status** (#70): agents may omit unchanged sub-messages, so a
    `sequence_num` that is not exactly one greater than the previous one means the
    server is missing status. The reply then sets `ReportFullState` (at most once per
    `REPORT_FULL_STATE_MIN_INTERVAL_SECONDS` per agent).

    **Hash-only components** (#77): an agent may report only the `hash` of its
    `AvailableComponents`. The reply then sets `ReportAvailableComponents` — once per
    hash, because the agent has no other way to leave a request loop. `GET /agent/{id}`
    shows `last_requested_components_hash` and `components_request_ignored`.

    **Duplicate `instance_uid`** (#75): two agents presenting one uid (a cloned VM, a
    restored backup, a broken id generator) are detected from interleaved host
    identities or a counter that keeps going backwards. The newcomer is answered with
    `agent_identification.new_instance_uid` and keeps being served, under the new id,
    with `uid_reissued_from` / `uid_reissue_reason` explaining it in `GET /agent/{id}`.
    `OPAMP_DUPLICATE_UID_DETECTION=false` turns the check off. Re-issuing needs
    `OPAMP_AGENT_AUTH=off`: a token is bound to the uid it was minted for (#94), so in
    `token` mode a duplicate is logged and counted but never renamed.

    **`OpAMP-Instance-UID` header** (#76): read on every request and cross-checked
    against the body. A mismatch — or a value that is not a readable uuid — is logged
    with both uids and counted in `opamp_instance_uid_header_problems_total`; the body
    is always what identifies the agent. `OPAMP_UID_HEADER_STRICT=true` turns a
    mismatch into a 400. When a body is refused (413, 415, 400) the header is the only
    identity in the log line, because there is no parsed message to name.

    The same path also accepts a WebSocket upgrade, by default
    (`OPAMP_WS_ENABLED=false` refuses it).

    **Overload (#78):** when the gate is enabled (`OPAMP_THROTTLE_ENABLED`) and the
    event loop is behind by more than `OPAMP_THROTTLE_MAX_LAG_SECONDS`, this endpoint
    answers **503** with `Retry-After` (seconds, never below 30 — the spec's minimum
    recommendation) instead of accepting the work. Off by default. `/health`,
    `/metrics` and every admin route are exempt by construction: throttling your own
    monitoring turns a slow server into a blind one. 503 rather than 429 because 429
    means "you sent too much" and this is "I cannot take it".
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

    # #78: overload shedding, checked *after* identity and *before* the body is read,
    # so a server that cannot keep up does not decompress and parse the message it is
    # refusing. 503 (not 429: 429 means "you sent too much", 503 means "this server is
    # unavailable") plus `Retry-After`, which opamp-go honours; without the header it
    # falls back to exponential backoff. No state is touched: the agent is still the
    # agent it was, just unanswered.
    if throttle.should_shed():
        throttle.mark_throttled(throttle.TRANSPORT_HTTP)
        logger.bind(
            event="opamp.throttled",
            transport="http",
            client=client,
            instance_uid=_instance_uid_header(request),
            lag_seconds=round(throttle.last_lag_seconds(), 3),
            retry_after_seconds=throttle.retry_after_seconds(),
        ).warning(
            "refusing OpAMP request: server overloaded, retry after "
            f"{throttle.retry_after_seconds()}s"
        )
        return Response(status_code=503, headers=throttle.headers())

    try:
        body = await read_body(request)
    except BodyTooLarge as exc:
        # Refuse before parsing, so no registration, heartbeat or metric changes.
        # The uid header is the only identity available for a body we won't read.
        logger.bind(
            event="opamp.body.too_large",
            transport="http",
            client=client,
            instance_uid=_instance_uid_header(request),
            size=exc.size,
            limit=exc.limit,
        ).warning(f"refusing OpAMP request body: {exc}")
        raise HTTPException(status_code=413, detail="OpAMP request body too large")
    except UnsupportedEncoding as exc:
        # 415, not 400: the message is fine, the encoding is not one we implement.
        logger.bind(
            event="opamp.message.rejected",
            transport="http",
            reason="unsupported_encoding",
            client=client,
            instance_uid=_instance_uid_header(request),
            content_encoding=request.headers.get("content-encoding", ""),
        ).warning(f"refusing OpAMP message: {exc.detail}")
        return _bad_request_response(exc, status_code=415)
    except protocol.BadRequest as exc:
        logger.bind(
            event="opamp.message.rejected",
            transport="http",
            reason="bad_request",
            client=client,
            instance_uid=_instance_uid_header(request),
        ).warning(f"refusing OpAMP message: {exc.detail}")
        return _bad_request_response(exc)

    try:
        result = _handle_agent_message(
            body, principal, uid_header=_instance_uid_header(request)
        )
    except protocol.BadRequest as exc:
        logger.bind(
            event="opamp.message.rejected",
            transport="http",
            reason="bad_request",
            client=client,
            instance_uid=_instance_uid_header(request) or exc.instance_uid.hex(),
        ).warning(f"refusing OpAMP message: {exc.detail}")
        return _bad_request_response(exc)
    except agent_auth.AgentAuthError as exc:
        agent_auth.record_failure(
            exc.reason,
            principal=exc.principal,
            claimed=exc.claimed,
            transport="http",
            client=client,
        )
        raise HTTPException(status_code=403, detail=exc.detail)

    payload = result.response
    limit = message_limit()
    if len(payload) > limit:
        # Spec: MUST NOT send an oversized response. Nothing is lost by answering
        # with an empty ack — the desired state is stored and the next poll retries.
        logger.bind(
            event="opamp.response.too_large",
            transport="http",
            client=client,
            agent_id=result.instance_uid.hex() if result.instance_uid else "",
            size=len(payload),
            limit=limit,
        ).error("refusing to send oversized ServerToAgent; returning an empty ack")
        payload = b""

    return Response(
        content=payload,
        media_type="application/x-protobuf",
        headers={"Connection": "keep-alive"}
    )


@app.websocket("/v1/opamp")
async def opamp_ws_endpoint(websocket: WebSocket) -> None:
    """OpAMP WebSocket transport — same path as the HTTP endpoint.

    On by default. `OPAMP_WS_ENABLED=false` refuses the handshake with close code
    1008, so the server behaves exactly as an HTTP-only server; a client that
    never asks for the upgrade is unaffected either way. Each binary frame is a
    varint header (must be 0) followed by an `AgentToServer` protobuf, per spec
    §WebSocket Transport.

    Authentication runs **before** `accept()` (#94): with `OPAMP_AGENT_AUTH=token`
    a missing/unknown/revoked token fails the handshake (close 1008, which a
    client sees as a rejected upgrade), and a message claiming a uid that belongs
    to a different credential closes the socket with 1008 mid-stream. A browser
    `Origin` must also satisfy `CORS_ORIGINS`, because WebSocket upgrades are not
    covered by `CORSMiddleware`.

    **Overload (#78).** When the gate is on and the loop is behind, the upgrade is
    denied with **HTTP 503 + `Retry-After`** instead of being accepted. The spec's
    WebSocket half of §Throttling is the in-band
    `ServerErrorResponse{UNAVAILABLE, retry_info}`, and that shape was tried first:
    opamp-go v0.24 (the reference client, and what `otelcol-contrib` binds) ignores
    `retry_info` on an established socket, tears the socket down the moment it sees
    an `error_response`, and reconnects immediately — a restart stampede became ~300
    reconnects per second per agent, i.e. the throttle amplified the load it was
    meant to shed. The same client *does* honour `Retry-After` on a refused upgrade
    (verified: retries 30.0 s apart, matching the header), so the refusal is at the
    handshake. Consequence, stated plainly: an **already-open** socket is not cut
    off mid-stream — its next reconnect is shed instead.
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

    if throttle.should_shed():
        # #78: refuse the upgrade, not the message. See the docstring — a
        # mid-stream refusal makes the reference client hot-loop, and `Retry-After`
        # on the handshake is the one backoff opamp-go actually applies here.
        throttle.mark_throttled(throttle.TRANSPORT_WS)
        logger.bind(
            event="opamp.throttled",
            transport="ws",
            client=websocket.client.host if websocket.client else "",
            lag_seconds=round(throttle.last_lag_seconds(), 3),
            retry_after_seconds=throttle.retry_after_seconds(),
        ).warning(
            "refusing OpAMP WebSocket upgrade: server overloaded, retry after "
            f"{throttle.retry_after_seconds()}s"
        )
        await websocket.send_denial_response(
            Response(status_code=503, headers=throttle.headers())
        )
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
    q: Optional[str] = Query(
        default=None,
        description=(
            "SQL selection over the live fleet (#132), e.g. "
            "SELECT * FROM agents WHERE host_arch = 'amd64'. Wins over the "
            "healthy/status/attribute parameters; see GET /agents/query/schema."
        ),
    ),
):
    """List agents, optionally filtered.

    Reserved query params: ``healthy``, ``status`` (alias ``remote_config_status``).
    Any other query param filters on the agent's OpAMP description metadata
    (identifyingAttributes + nonIdentifyingAttributes), e.g. ``?environment=prod``.
    Repeated params match any value (OR): ``?environment=prod&environment=staging``.
    Agents missing an attribute are excluded.

    ``q`` runs a read-only SQL selection over a throwaway in-memory table built from
    live agents (#132); the other filter parameters are ignored when it is present.
    The schema and live values for writing a query are at ``GET /agents/query/schema``.

    Unauthenticated. Returns `{"agents": [...], "count": n, "filters": {...}}`.
    Each entry carries a `credential` block (#94) — `enrolled`, `active`,
    `last_seen_at`, `revoked_at` — so an operator can watch a fleet finish the
    token cutover. It never contains a token or a token hash.
    """
    remote_config_status = remote_config_status if remote_config_status is not None else status
    query_result: Optional[Dict[str, Any]] = None
    if q is not None:
        # A SQL query is the selector; mixing it with the ad-hoc params would mean two
        # filters whose combination nobody can predict from reading either one.
        try:
            query_result = agent_query.run(q, AGENT_REGISTRY.list_all())
        except agent_query.AgentQueryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        selected = list(query_result["agents"])
        attr_filters = {}
    else:
        reserved = {"healthy", "status", "remote_config_status", "q"}
        attr_filters = {}
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
    # `q` wins over the ad-hoc parameters, so reporting them as applied would be a
    # lie about how the result was selected.
    if query_result is None:
        if healthy is not None:
            filters["healthy"] = healthy
        if remote_config_status is not None:
            filters["remote_config_status"] = remote_config_status
        if attr_filters:
            filters["attributes"] = attr_filters

    response: Dict[str, Any] = {"agents": agents, "count": len(agents), "filters": filters}
    if query_result is not None:
        response["query"] = query_result["query"]
        response["query_columns"] = query_result["columns"]
    return response


@app.get(
    "/agents/query/schema",
    tags=["agents"],
    summary="SQL schema, live values and suggestions for agent queries",
)
def agent_query_schema():
    """What a selection query can name, with the values live agents actually report.

    This is the autosuggestion payload for the SQL search box (#132): core columns
    and example statements on one side, observed description attributes and their
    values on the other. `host_arch = 'amd64'` is suggested only because an agent
    reported `amd64` — a suggestion, not a guess.

    Unauthenticated for the same reason `GET /agents` is: it exposes descriptions,
    which that route already returns. An effective config is never read here (D8).
    The language guide is `docs/sql-agent-queries.md`.
    """
    return agent_query.schema(AGENT_REGISTRY.list_all())


@app.get(
    "/agent/types",
    tags=["agents"],
    summary="List the agent types this build can recognise",
)
def list_agent_types():
    """Every registered agent profile, in match order (#118). Unauthenticated.

    `otelcol` is what this server used to assume about *everything*; `generic` is
    the spec's baseline — an agent whose configuration the server stores and relays
    without interpreting. Each entry carries `validates_structure` (may the server
    judge a body's structure for this agent?), the `content_types` and download
    filename it accepts, which `reports` apply, and whether it can be `detectable`
    at all (a profile with no matchers can only be reached by a pin).

    This route is declared before `/agent/{agent_id}` so `types` is not read as an
    agent id.
    """
    return {"types": profiles.describe(), "count": len(profiles.PROFILES)}


class AgentTypeRequest(BaseModel):
    """Pin an agent's type, or return it to auto-detection."""

    agent_type: Optional[str] = Field(
        default=None,
        description="A registered profile name (see GET /agent/types), or null for auto",
    )


@app.put(
    "/agent/{agent_id}/type",
    tags=["agents"],
    summary="Pin (or unpin) an agent's type (operator)",
)
def put_agent_type(agent_id: str, request: Request, body: AgentTypeRequest):
    """Tell the server what this agent is, instead of letting it guess. Requires `operator`.

    **Why this exists**: every automatic signal is the agent's own claim —
    `service.name`, `content_type`, the shape of its payloads — and the OpAMP server
    authenticates the *channel*, not the payload. A pin is the server-side answer:
    it outranks detection, survives restarts (it is a column on the agent row) and is
    audited (`agent.type.pinned` / `agent.type.auto`).

    Body: `{"agent_type": "otelcol"}` to pin, `{"agent_type": null}` (or `""`) to
    return to auto-detection. Responds 404 for an unknown agent, **422 for a name no
    registered profile provides** — never a silent fall back to `generic`, because
    "pinned to a typo" and "deliberately opaque" must not look the same — and 403
    without an operator role.

    Returns the same four `agent_type*` fields `GET /agent/{id}` carries, so the
    caller sees the resolution and its provenance rather than just the value it sent.
    """
    principal = require_roles(request, ROLE_OPERATOR)

    agent = AGENT_REGISTRY.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    requested = (body.agent_type or "").strip() or None
    if requested is not None:
        # Store the canonical spelling: a pin arrives from a human as often as from
        # code, and two spellings of one profile in the database is a bug waiting for
        # a comparison.
        canonical = profiles.canonical(requested)
        if canonical is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown agent type {requested!r}; known types: "
                    f"{', '.join(profiles.names())}"
                ),
            )
        requested = canonical

    previous = agent.agent_type
    # #97: validated, then recorded, then acted. A pin changes what the server is
    # willing to claim about an agent, so it is a privileged mutation — and the audit
    # row names both ends of the change, because "changed" is not actionable.
    audit_action(
        principal,
        "agent.type.pinned" if requested else "agent.type.auto",
        target_kind="agent",
        target_id=agent_id,
        agent_type=requested,
        previous=previous,
    )
    AGENT_REGISTRY.update(agent_id, agent_type=requested)

    info = agent.type_info()
    return {
        "agent_id": agent_id,
        "changed": previous != requested,
        "previous": previous,
        **info,
    }


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
    """Optional body for bound-credential issuance (#94)."""

    label: str = ""
    retire_previous: bool = False
    # #126: a bound token can expire too. `None` means never.
    expires_in_days: int | None = None


class AgentTokenCreate(BaseModel):
    """Body for minting a fleet token (#126).

    `expires_in_days` is one of 1/7/30 or null (never); `max_agents` is a positive
    integer or null (unlimited). `agent_id` is absent on purpose: a fleet token is not
    bound to a uid, which is the whole point — an admin can mint it before any agent has
    ever contacted the server.
    """

    label: str = ""
    expires_in_days: int | None = None
    max_agents: int | None = None


@app.post(
    "/agent-tokens",
    tags=["agents"],
    summary="Mint a fleet agent token (admin)",
    status_code=201,
)
def create_fleet_token(
    request: Request,
    response: Response,
    body: AgentTokenCreate | None = None,
):
    """Mint a token that any number of agents may present, up to `max_agents` (#126).

    This is the token an operator generates *before* the fleet exists: it is not bound
    to an `instance_uid`, so nothing has to register first. Put it in each agent's
    config (`extensions.opamp.server.http.headers.Authorization`, or `server.ws.headers`)
    and the server accepts the uid each agent invents for itself.

    Body (all optional): `{"label": "prod", "expires_in_days": 7, "max_agents": 15}`.
    `expires_in_days` is 1-365, or `null` for never. `max_agents` is 1+, or `null` for
    unlimited; the agent that would exceed it is refused with 403 (`agent_limit`) and a
    line in the server log, so a leaked token cannot consume the fleet.

    The plaintext token is returned **once** — only its SHA-256 is stored — along with
    `token_id`, the public handle used to list and revoke it. It never appears in
    `GET /agent-tokens`, in logs or in audit entries. Responses are `Cache-Control:
    no-store`.
    """
    principal = require_roles(request, ROLE_ADMIN)
    body = body or AgentTokenCreate()
    try:
        token, record = agent_auth.issue_token(
            "",
            label=body.label,
            expires_in_days=body.expires_in_days,
            max_agents=body.max_agents,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    audit_action(
        principal,
        "agent.token.issued",
        target_kind="agent_token",
        target_id=record.get("token_id", ""),
        label=body.label,
        expires_at=record.get("expires_at"),
        max_agents=body.max_agents,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "token": token,
        "token_id": record.get("token_id"),
        "label": record.get("label", ""),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "max_agents": record.get("max_agents"),
        "collector_snippet": agent_auth.collector_snippet(token),
        "note": (
            "Shown once and not recoverable: put it in the agents' config now. "
            "The token's hash is all this server keeps. Revoke it with "
            f"DELETE /agent-tokens/{record.get('token_id')} if it leaks."
        ),
    }


@app.get("/agent-tokens", tags=["agents"], summary="List agent tokens (viewer)")
def list_agent_tokens(request: Request):
    """Every token, with its state and how much of its cap is used. Never a token.

    Read-only, so a `viewer` may see it: an operator watching a fleet needs to know
    which token is about to expire or has hit its agent cap, and neither fact is a
    secret. `token_id` is an opaque handle, not a verifier — the token and its SHA-256
    are absent from every row.

    `agents_used` and `max_agents` are reported together because a cap is only
    meaningful as "3 of 15"; `expires_at: null` means never; `expired` and `active`
    are computed server-side so no client has to re-derive them.
    """
    require_roles(request, ROLE_VIEWER)
    tokens = agent_auth.list_tokens()
    return {"tokens": tokens, "count": len(tokens)}


@app.delete(
    "/agent-tokens/{token_id}",
    tags=["agents"],
    summary="Revoke one agent token (admin)",
)
def revoke_fleet_token(token_id: str, request: Request):
    """Revoke a token by its public handle. Requires admin auth.

    Idempotent: `{"token_id", "revoked": bool}`. Revocation blocks new messages
    immediately, keeps the row (so the audit trail and the cap count survive) and does
    **not** touch any agent's state — revoking a leaked token must not delete the fleet
    it was used by.
    """
    principal = require_roles(request, ROLE_ADMIN)
    if not agent_auth.revoke_token(token_id):
        raise HTTPException(status_code=404, detail="no such token, or already revoked")
    audit_action(
        principal,
        "agent.token.revoked",
        target_kind="agent_token",
        target_id=token_id,
    )
    return {"token_id": token_id, "revoked": True}


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
            agent_id,
            label=body.label,
            retire_previous=body.retire_previous,
            expires_in_days=body.expires_in_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    response.headers["Cache-Control"] = "no-store"
    return {
        "agent_id": record.get("agent_id", agent_id),
        "token": token,
        "token_id": record.get("token_id"),
        "label": record.get("label", ""),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "collector_snippet": agent_auth.collector_snippet(
            token,
            agent_type=(
                AGENT_REGISTRY.get(agent_id).type_info()["agent_type"]
                if AGENT_REGISTRY.get(agent_id) is not None
                else None
            ),
        ),
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

    # #120: OCB is the *collector's* build tool. For anything else, "no components in
    # use" is true and misleading — the honest refusal names the profile.
    if not agent.profile.ocb_manifest:
        raise HTTPException(
            status_code=409,
            detail=(
                "the OCB manifest is an OpenTelemetry Collector Builder artefact; this "
                f"agent's profile is {agent.profile.name!r}, which is not built that way "
                "(#120)"
            ),
        )

    components = agent.components
    if not components or not any(
        comp.get("used") for group in components.values() for comp in group
    ):
        raise HTTPException(
            status_code=409,
            detail="Agent has no components in use; cannot generate a buildable OCB manifest",
        )

    return {
        # #122: the distro name is the profile's, not a literal in the manifest
        # module: "otelcol" + "-slim" reproduces the historical bytes for the
        # collector, and a fork gets its own name for free.
        "manifest_yaml": generate_manifest(
            components, version, dist_name=f"{agent.profile.name}-slim"
        ),
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
    # #120: "not applicable" is not "not heavy". The count is over the agents the report
    # is about, and the skipped ones are reported by profile so a mixed fleet cannot read
    # "Found 0" as a clean bill of health.
    applicable, skipped = reports.split_applicable(agents, reports.REPORT_HEAVY)
    heavy = [a for a in applicable if _is_heavy(a, threshold)]
    return {
        "report_markdown": generate_heavy_collectors_report({"agents": agents}, threshold),
        "heavy_count": len(heavy),
        "threshold": threshold,
        "applicable_count": len(applicable),
        "not_applicable": skipped,
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
    applicable, skipped = reports.split_applicable(agents, reports.REPORT_OUTDATED)
    collectors_count, components_count = _count_outdated_collectors(agents, version)
    return {
        "applicable_count": len(applicable),
        "not_applicable": skipped,
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
    """Body for a fleet-wide apply. Explicit targets, a filter or a group; `confirm` gates it."""

    agents: Optional[List[str]] = None
    filter: Optional[Dict[str, Any]] = None
    # #111: break-glass pushes obey the same group guards as a proposal (health
    # definition, shrink, empty), because bypassing them here would make the guards
    # advisory.
    group: Optional[str] = None
    use_stored_resolution: bool = False
    confirm_scope_change: bool = False
    confirm: bool = False


class GroupCreateRequest(BaseModel):
    """Body for `POST /groups`: a name for a selector, not a member list.

    A selector is **either** `query` (SQL, #132) or `definition` (a filter document,
    #111). Exactly one is required; sending both is refused.
    """

    name: str
    definition: Optional[Dict[str, Any]] = None
    query: Optional[str] = None
    include: List[str] = Field(default_factory=list)
    exclude: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    owner: Optional[str] = None


class GroupUpdateRequest(BaseModel):
    """Body for `PUT /groups/{id}`: only the fields that are present are changed."""

    name: Optional[str] = None
    definition: Optional[Dict[str, Any]] = None
    query: Optional[str] = None
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    description: Optional[str] = None
    owner: Optional[str] = None


class ValidateConfigRequest(BaseModel):
    """Body for `POST /config/validate` (#100)."""

    config_map: Dict[str, Any]
    agent_id: Optional[str] = None
    allow_endpoint_change: bool = False


class ConfigScope(BaseModel):
    """Who a proposal targets. Exactly one of `agent_id`, `agents`, `filter` or `group`.

    `group` (#111) names a saved selector and expands server-side into a `filter` plus
    the group's `include`/`exclude` pins, so a group can never be combined with the
    other three — the point of a named selector is that one definition decides the
    targets. `use_stored_resolution` is the escape hatch for a health-defined group:
    it pushes to the membership recorded at a stated time instead of re-matching.
    """

    agent_id: Optional[str] = None
    agents: Optional[List[str]] = None
    filter: Optional[Dict[str, Any]] = None
    group: Optional[str] = None
    include_future: bool = False
    use_stored_resolution: bool = False


class ProposeConfigRequest(BaseModel):
    """Body for `POST /config/revisions` (#99): a draft, not a delivery."""

    config_map: Dict[str, Any]
    scope: ConfigScope
    note: Optional[str] = None
    allow_endpoint_change: bool = False
    override_reason: Optional[str] = None
    # A group that resolves smaller than its last recorded resolution needs this
    # acknowledgement (#111): silent shrinkage is the failure the feature exists for.
    confirm_scope_change: bool = False


class ApproveConfigRequest(BaseModel):
    """Body for approving a proposal (#99). An override needs a recorded reason."""

    note: Optional[str] = None
    confirm_endpoint_change: bool = False
    # The selector is re-resolved at approve time; if it now covers agents the
    # approver never saw, the approval needs this explicit acknowledgement.
    confirm_scope_change: bool = False
    # #119: at least one target's profile owns no structural rules, so nothing judged
    # the body beyond its bytes. The approver has to say they know that.
    confirm_unchecked_structure: bool = False
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


class RestartRequest(BaseModel):
    """Body for `POST /agent/{id}/restart` (#84)."""

    reason: Optional[str] = None


class CustomMessageRequest(BaseModel):
    """Body for `POST /agent/{id}/custom-message` (#85).

    `data` is **base64** because JSON cannot carry bytes and the wire field is
    `bytes`. The server never interprets it: the payload contract belongs to the
    capability, and guessing would corrupt a third-party extension.
    """

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(..., description="Reverse FQDN the agent advertised")
    type: str = Field(..., description="Capability-scoped message type")
    data: str = Field("", description="Base64 payload (empty is valid)")


class RolloutRequest(BaseModel):
    """Body for `POST /config/rollouts` and `/config/revisions/{id}/apply` (#103).

    `extra="forbid"` on purpose: this repo is driven by AI agents and a mistyped
    `max_batch` that silently does nothing is a blast-radius bug, not a typo.
    """

    model_config = ConfigDict(extra="forbid")

    # `dry_run` is the default posture of every other control-plane surface: plan and
    # validate, change nothing. Creating a rollout is what an approver does after
    # reading the plan.
    dry_run: bool = False
    note: Optional[str] = None
    canary_percent: Optional[float] = Field(None, ge=0, le=100)
    max_agents_per_batch: Optional[int] = Field(None, ge=1)
    max_percent_per_batch: Optional[float] = Field(None, ge=0, le=100)
    health_gate_seconds: Optional[int] = Field(None, ge=0)
    ttl_minutes: Optional[int] = Field(None, ge=0)
    # #84: queue a restart for each agent once its batch passes the gate, so "push a
    # config that needs a restart" is one action instead of two steps in the right order.
    restart_after_apply: bool = False
    confirm_endpoint_change: bool = False
    confirm_scope_change: bool = False
    # #119: creating a rollout *is* the approval, so the same acknowledgement applies.
    confirm_unchecked_structure: bool = False
    override_reason: Optional[str] = None


class RolloutAbortRequest(BaseModel):
    """Body for `POST /config/rollouts/{id}/abort`."""

    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = None


class RolloutResumeRequest(BaseModel):
    """Body for `POST /config/rollouts/{id}/resume`."""

    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = None


class RolloutRollbackRequest(BaseModel):
    """Body for `POST /config/rollouts/{id}/rollback`."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = None
    health_gate_seconds: Optional[int] = Field(None, ge=0)


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
        # #101: assigned by an approval, still held by a staged rollout. `assignment`
        # (below) reports the derived delivery state; this is the one fact that
        # explains "why has my agent not received an approved config?".
        "held": bool(record.get("held")),
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

    group_scope: Dict[str, Any] = {}
    if body.agents and (body.filter or body.group):
        raise HTTPException(
            status_code=400, detail="provide exactly one of agents, filter or group"
        )
    if body.filter and body.group:
        raise HTTPException(
            status_code=400, detail="provide exactly one of agents, filter or group"
        )
    if body.agents:
        targets = []
        for raw_id in body.agents:
            try:
                targets.append(remote_config.normalize_agent_id(raw_id))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
    elif body.group:
        # #111: a group-targeted break-glass push obeys the same guards as a proposal
        # (health definition, empty group, silent shrinkage), because a guard that a
        # second route can skip is advisory.
        try:
            group_scope = config_control.parse_scope(
                {"group": body.group},
                agents=AGENT_REGISTRY.list_all(),
                actor=principal.name,
                use_stored_resolution=body.use_stored_resolution,
                confirm_scope_change=body.confirm_scope_change,
            )
            targets = config_control.resolve_targets(
                group_scope, AGENT_REGISTRY.list_all()
            )
        except config_control.ConfigControlError as exc:
            raise _control_error(exc)
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
            detail="provide \"agents\": [...], \"filter\": {...} or \"group\": \"<name>\"; "
            "a missing selector is never \"all agents\"",
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
        group_id=group_scope.get("group_id"),
        group_name=group_scope.get("group_name"),
    )
    if group_scope.get("group_id"):
        groups.record_push_resolution(
            group_scope, targets, actor=principal.name, reason=groups.REASON_PUSH
        )
        PROM_GROUP_MEMBERSHIP_CHANGES.labels(reason=groups.REASON_PUSH).inc()

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
                group_id=group_scope.get("group_id"),
                group_snapshot=group_scope.get("group_snapshot"),
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
            # #118: which profile's rules produced this verdict, and how it was
            # decided. `""` means the request named no agent, so nothing was assumed.
            "profile": verdict.profile,
            "profile_source": verdict.profile_source,
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
        "profile": verdict.profile,
        "profile_source": verdict.profile_source,
    }


def _config_size(config_map: Dict[str, Dict[str, Any]]) -> Optional[int]:
    if not config_map:
        return None
    return sum(len((entry.get("body") or "").encode("utf-8")) for entry in config_map.values())


# ------------------------------------------------------ collector groups (#111)


def _group_error(exc: GroupError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _require_group(name_or_id: str, *, include_archived: bool = False) -> Dict[str, Any]:
    """Resolve a group by name or id, 404 when absent, 409 when archived (#111)."""
    record = COLLECTOR_GROUPS.find(name_or_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"group {name_or_id!r} not found",
                "hint": "list the saved selectors with GET /groups",
            },
        )
    if record.get("archived_at") and not include_archived:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"group {record['name']!r} is archived",
                "archived_at": record.get("archived_at"),
                "hint": "POST /groups/{id}/restore first; an archived group is history, "
                "not a target",
            },
        )
    return record


def _publish_group_metrics(records: Optional[List[Dict[str, Any]]] = None) -> None:
    """Refresh the two group gauges. Cheap: one list + one resolve per group."""
    records = records if records is not None else COLLECTOR_GROUPS.list(include_archived=True)
    live = [record for record in records if not record.get("archived_at")]
    archived = [record for record in records if record.get("archived_at")]
    PROM_GROUPS.labels(state="active").set(len(live))
    PROM_GROUPS.labels(state="archived").set(len(archived))
    agents = AGENT_REGISTRY.list_all()
    for record in live:
        resolved = groups.resolve_members(record, agents)
        PROM_GROUP_MEMBERS.labels(group=record["name"]).set(resolved["count"])


@app.get(
    "/groups/attribute-keys",
    tags=["groups"],
    summary="Attribute keys and sample values from the live fleet (viewer)",
)
def list_group_attribute_keys(request: Request):
    """Keys an operator can build a group definition from. Requires `viewer`.

    Harvested from **live agents' descriptions only** — never from an effective
    config, which is where a fleet's credentials live (D8). This is the typo-proofing
    half of groups: a builder that offers `environment` cannot produce `env`, and a
    dropdown of observed values cannot produce `prod-*` (matching is exact,
    case-insensitive equality — no globs, no regex, no prefix).
    """
    require_roles(request, ROLE_VIEWER)
    return groups.attribute_keys(AGENT_REGISTRY.list_all())


@app.get("/groups", tags=["groups"], summary="List collector groups with live member counts (viewer)")
def list_groups(
    request: Request,
    include_archived: bool = Query(False, description="Include archived groups"),
):
    """Saved selectors with their **live** member count. Requires `viewer`.

    Membership is resolved on read, never stored, so a group cannot drift from the
    fleet it names. `last_resolution` is what the group meant the last time it was
    evaluated, which is the baseline a `preview` diff and the shrink guard use.
    """
    require_roles(request, ROLE_VIEWER)
    agents = AGENT_REGISTRY.list_all()
    records = COLLECTOR_GROUPS.list(include_archived=include_archived)
    items = [groups.view(record, agents, include_history=False) for record in records]
    _publish_group_metrics()
    return {
        "groups": items,
        "count": len(items),
        "matching": "exact, case-insensitive equality — no globs, no regex, no prefix",
        "health_definition_note": (
            "a group defined on healthy/status drops the agents that break; pushing to "
            "one requires use_stored_resolution"
        ),
    }


@app.post("/groups", tags=["groups"], summary="Create a collector group (operator)")
def create_group(
    body: GroupCreateRequest,
    request: Request,
    _audit=Depends(require_audit("group.create")),
):
    """Create a named selector. Requires `operator`. 409 when the name is taken.

    Names are unique **case-insensitively** and cannot collide with the reserved
    filter keys (`healthy`, `status`, `remote_config_status`), so `--group prod` and
    `--attr environment=prod` stay visibly different things on every surface.

    A group is a proposal of intent, not a push: creating one changes nothing that
    any agent runs. It also may have **zero** members — writing the definition down
    before the collectors exist is the point — but the definition may not be empty,
    because `{}` means the whole fleet.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    try:
        audit_action(
            principal,
            "group.create",
            target_kind="group",
            target_id=body.name,
            definition=body.definition,
            query=body.query,
            include=body.include,
            exclude=body.exclude,
        )
        record = COLLECTOR_GROUPS.create(
            name=body.name,
            definition=body.definition,
            query=body.query,
            include=body.include,
            exclude=body.exclude,
            description=body.description,
            owner=body.owner,
            source="api",
            created_by=principal.name,
        )
    except GroupError as exc:
        raise _group_error(exc)
    _publish_group_metrics()
    return groups.view(record, AGENT_REGISTRY.list_all())


@app.get("/groups/{group_id}", tags=["groups"], summary="One group: definition, members, history (viewer)")
def get_group(group_id: str, request: Request):
    """Definition, current members and resolution history. Requires `viewer`.

    `sources` shows which members came from the definition and which from
    `include`/`exclude`, `unknown_pins` names pins for agents that have never
    reported (a typo'd id would otherwise look like a working pin forever), and
    `membership_changes` counts how often the resolved set moved — the drift signal
    behind "resolution changed 3 times since".
    """
    require_roles(request, ROLE_VIEWER)
    record = _require_group(group_id, include_archived=True)
    view = groups.view(record, AGENT_REGISTRY.list_all())
    _publish_group_metrics()
    return view


@app.put("/groups/{group_id}", tags=["groups"], summary="Edit a group (operator)")
def update_group(
    group_id: str,
    body: GroupUpdateRequest,
    request: Request,
    _audit=Depends(require_audit("group.update")),
):
    """Edit a definition, its pins or its metadata. Requires `operator`.

    The audit row carries the **definition diff** (`added` / `removed` / `changed`
    with both values), not "changed": an edit silently changes the blast radius of
    every future push that names the group, and the diff is the only part of that an
    auditor can act on.

    Editing changes future pushes only. A revision approved against the old
    definition keeps its `group_snapshot` and its resolved agent list, and
    `GET /config/revisions` reports `group_definition_changed: true` for it — which
    is exactly why the snapshot is stored rather than the group id alone.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    current = _require_group(group_id)
    try:
        # #97 ordering: the diff is computed (and audited) before the write, so a
        # write that cannot be recorded does not happen.
        _, changes = COLLECTOR_GROUPS.compute_changes(
            current,
            name=body.name,
            definition=body.definition,
            query=body.query,
            include=body.include,
            exclude=body.exclude,
            description=body.description,
            owner=body.owner,
        )
        if changes:
            audit_action(
                principal,
                "group.update",
                target_kind="group",
                target_id=current["group_id"],
                group_name=current.get("name"),
                changes=changes,
            )
        record, changes = COLLECTOR_GROUPS.update(
            current["group_id"],
            actor=principal.name,
            name=body.name,
            definition=body.definition,
            query=body.query,
            include=body.include,
            exclude=body.exclude,
            description=body.description,
            owner=body.owner,
        )
    except GroupError as exc:
        raise _group_error(exc)
    _publish_group_metrics()
    view = groups.view(record, AGENT_REGISTRY.list_all())
    view["changes"] = changes
    return view


@app.delete("/groups/{group_id}", tags=["groups"], summary="Archive a group (operator)")
def archive_group(
    group_id: str,
    request: Request,
    _audit=Depends(require_audit("group.archive")),
):
    """**Archive** a group. Requires `operator`. There is no delete.

    Revisions, rollouts and the audit log all reference `group_id`, so a hard delete
    would leave history pointing at nothing. An archived group is absent from
    pickers, refuses edits and pushes, and can be restored.

    A **live rollout** that was approved against this group refuses with 409 first:
    archiving it mid-flight would hide the definition from the operator who has to
    explain what is being delivered.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    current = _require_group(group_id, include_archived=True)
    if current.get("archived_at"):
        return groups.view(current, AGENT_REGISTRY.list_all())
    live = rollout.live_rollouts_for_group(current["group_id"])
    if live:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"group {current['name']!r} is in use by a live rollout",
                "rollouts": live,
                "hint": "let the rollout finish, abort it, or (for a stuck rollout) "
                "cancel it, then archive",
            },
        )
    audit_action(
        principal,
        "group.archive",
        target_kind="group",
        target_id=current["group_id"],
        group_name=current.get("name"),
    )
    record = COLLECTOR_GROUPS.archive(current["group_id"], actor=principal.name)
    _publish_group_metrics()
    return groups.view(record, AGENT_REGISTRY.list_all())


@app.post("/groups/{group_id}/restore", tags=["groups"], summary="Restore an archived group (operator)")
def restore_group(
    group_id: str,
    request: Request,
    _audit=Depends(require_audit("group.restore")),
):
    """Clear `archived_at`. Requires `operator`. The definition comes back unchanged."""
    principal = require_roles(request, ROLE_OPERATOR)
    current = _require_group(group_id, include_archived=True)
    audit_action(
        principal,
        "group.restore",
        target_kind="group",
        target_id=current["group_id"],
        group_name=current.get("name"),
    )
    record = COLLECTOR_GROUPS.restore(current["group_id"], actor=principal.name)
    _publish_group_metrics()
    return groups.view(record, AGENT_REGISTRY.list_all())


@app.post("/groups/{group_id}/preview", tags=["groups"], summary="Preview a group's membership (viewer)")
def preview_group(group_id: str, request: Request):
    """Members now, versus the last recorded resolution. Requires `viewer`, no side effects.

    `added` / `dropped` / `unchanged` compare against the group's last resolution and
    the evaluation is recorded as the next baseline (that is what makes the diff, and
    the shrink guard, mean anything). Nothing is pushed, no revision is created.

    A definition built on `healthy`/`status` is legal to preview and is exactly how
    `use_stored_resolution` gets a membership to push to.
    """
    principal = require_roles(request, ROLE_VIEWER)
    record = _require_group(group_id, include_archived=True)
    result = groups.preview(record, AGENT_REGISTRY.list_all(), actor=principal.name)
    PROM_GROUP_MEMBERSHIP_CHANGES.labels(reason=groups.REASON_PREVIEW).inc()
    _publish_group_metrics()
    return result


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
        scope = config_control.parse_scope(
            body.scope.model_dump(),
            agents=AGENT_REGISTRY.list_all(),
            actor=principal.name,
            use_stored_resolution=body.scope.use_stored_resolution,
            confirm_scope_change=body.confirm_scope_change,
        )
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
        # #111: the group is part of what the approval covers, so the audit row names
        # it and how many agents it resolved to at propose time.
        group_id=prepared["scope"].get("group_id"),
        group_name=prepared["scope"].get("group_name"),
    )
    result = config_control.propose(
        prepared, author=principal.name, proposal_id=proposal_id, note=body.note
    )
    if prepared["scope"].get("group_id"):
        groups.record_push_resolution(
            prepared["scope"],
            prepared["targets"],
            actor=principal.name,
            reason=groups.REASON_PROPOSE,
        )
        PROM_GROUP_MEMBERSHIP_CHANGES.labels(reason=groups.REASON_PROPOSE).inc()
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
        # #119: the audit row records that the approver accepted an unchecked body.
        confirm_unchecked_structure=body.confirm_unchecked_structure,
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
            confirm_unchecked_structure=body.confirm_unchecked_structure,
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
    # #111: record what the group meant at the moment it was approved. That is the
    # baseline the shrink guard and the preview diff compare against from now on.
    scope = config_control.proposal_view(proposal_id).get("scope") or {}
    if scope.get("group_id"):
        groups.record_push_resolution(
            scope,
            [revision["agent_id"] for revision in result.get("revisions", [])],
            actor=principal.name,
            reason=groups.REASON_APPROVE,
        )
        PROM_GROUP_MEMBERSHIP_CHANGES.labels(reason=groups.REASON_APPROVE).inc()
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
    "spec_version": "0.20.0", "capabilities": int, "opa_enabled": bool,
    "opa_url": str|null, "alerts_enabled": true, "config_push": {"enabled",
    "insecure", "agent_auth", "audit", "tls", "config_at_rest"},
    "desired_configs": n, "throttle": {"enabled", "max_lag_seconds",
    "retry_after_seconds", "lag_seconds", "shedding"}}`. `spec_version` and
    `capabilities` are #79's machine-readable
    answer to "will my agent interoperate?": the version is the spec release this build
    targets (README has the full matrix) and the bitmask is exactly what is advertised
    to agents (#74), so a client never has to guess.
    The `config_push` block is the public posture summary — `insecure: true` means
    config push is on despite an unsafe posture and the UI shows a banner.
    `config_at_rest` is `encrypted`, `plaintext` (explicit development opt-out) or
    `unconfigured` (#98): a plaintext config store must never be invisible.
    `throttle.shedding` is the live overload answer (#78) — when it is true, agent
    requests are being refused with 503 and the stale-agent reaper is standing down.
    Prometheus metrics are served separately at `GET /metrics`.
    """
    from server.opa_client import OPAClient
    opa_client = OPAClient()
    opa_available = OPA_ENABLED and opa_client.is_available()
    return {
        "status": "healthy",
        # #79: machine-readable, next to the other capability flags, so `opampctl`,
        # the MCP server and the UI can display it without hardcoding a version.
        "spec_version": protocol.SPEC_VERSION,
        "capabilities": remote_config.server_capabilities(),
        "agents_connected": AGENT_REGISTRY.count,
        "opa_enabled": opa_available,
        "opa_url": OPA_URL if OPA_ENABLED else None,
        "alerts_enabled": True,
        "config_push": posture.posture(),
        "desired_configs": remote_config.REGISTRY.count(),
        # #78: the live overload answer, next to the posture it belongs with — both
        # are "what is this server doing right now", not secrets.
        "throttle": throttle.status(),
    }


class UpdateSettingsRequest(BaseModel):
    """Body for `PUT /updates/settings`."""

    enabled: Optional[bool] = None


@app.get("/updates", tags=["ops"], summary="Cached update-check state")
def get_updates():
    """Cached update status. **Never performs network I/O.**

    Unauthenticated, like `/health`. Returns `{"current_version",
    "latest_version", "latest_tag", "update_available", "checked_at",
    "next_check_at", "enabled", "deployment_enabled", "admin_override",
    "source", "error", "releases_url"}`. `enabled` is the effective state;
    `deployment_enabled` is the `UPDATE_CHECK` env switch, which wins over the
    admin toggle. No feed URL, version, or instance identifier is sent
    anywhere by this endpoint — it only reads the local result.
    """
    return updates.state_dict()


@app.post("/updates/check", tags=["ops"], summary="Run an update check now (admin)")
def check_updates(
    request: Request,
    _audit=Depends(require_audit("updates.check")),
):
    """Run one check immediately. Requires `admin`.

    Refused with 409 when checks are disabled, and 429 with `Retry-After` when
    a check ran less than a minute ago. The request goes to GitHub without the
    current version or any instance identifier; the comparison is local.
    """
    require_roles(request, ROLE_ADMIN)
    if not updates.enabled():
        raise HTTPException(status_code=409, detail="update checks are disabled")
    try:
        return updates.check_once()
    except updates.TooSoon as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        )


@app.put("/updates/settings", tags=["ops"], summary="Enable/disable automatic update checks (admin)")
def put_update_settings(
    body: UpdateSettingsRequest,
    request: Request,
    _audit=Depends(require_audit("updates.settings")),
):
    """Persist the admin override. Requires `admin`.

    `{"enabled": false}` turns automatic checks off; `{"enabled": true}` turns
    them back on; `{"enabled": null}` clears the override and uses the
    deployer's `UPDATE_CHECK` default. The deployer's `off` always wins: the
    response reports `enabled: false` and `deployment_enabled: false`.
    """
    require_roles(request, ROLE_ADMIN)
    updates.set_admin_override(body.enabled)
    if body.enabled is True and updates.enabled():
        try:
            return updates.check_once()
        except updates.TooSoon:
            pass
    return updates.state_dict()


@app.post(
    "/agent/{agent_id}/restart",
    tags=["agents"],
    summary="Ask an agent to restart on its next poll (operator)",
)
def restart_agent(
    agent_id: str,
    body: RestartRequest,
    request: Request,
    _audit=Depends(require_audit("agent.restart")),
):
    """Queue a `ServerToAgentCommand{Restart}` for the agent's next message. Requires `operator`.

    Not gated by the config-push posture: a restart is not a config change, and the
    operator who is allowed to deliver a config must be able to make it take effect
    (#84).

    **HTTP has no server push**, so the command rides the agent's next poll and is
    delivered **exactly once** — the queue entry is cleared before the reply is built,
    because a restart command re-delivered on every poll is a crash loop. A reply that
    carries a command carries nothing else (the spec's own rule), so a config offer due
    in the same reply waits for the next poll.

    Refused with **409** when the agent does not advertise
    `AcceptsRestartCommand` (upstream defaults `accepts_restart_command` to `false`, so
    this is the common case) — a silent no-op would look like "the button is broken".
    `404` when the agent is unknown or `OPAMP_RESTART_COMMAND_ENABLED=false`.

    Audited as `agent.restart` **before** the command is queued (pre-write, #97); the
    agent view then reports `pending_command` and `command_delivered_at`.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        record = commands.request_restart(
            agent_id, principal=principal, reason=body.reason
        )
    except commands.CommandError as exc:
        audit_action(
            principal,
            "agent.restart",
            target_kind="agent",
            target_id=agent_id,
            reason=body.reason,
            error=str(exc.detail),
            result="error",
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    audit_action(
        principal,
        "agent.restart",
        target_kind="agent",
        target_id=agent_id,
        reason=body.reason,
    )
    state = AGENT_REGISTRY.get(agent_id)
    return {
        "agent_id": agent_id,
        "queued": True,
        "command": record,
        "capabilities": int(getattr(state, "capabilities", 0) or 0),
        "delivery": {
            "state": "queued",
            "channel": "reply",
            "detail": (
                "delivered on the agent's next message; a reply carrying a command "
                "carries nothing else"
            ),
        },
    }

@app.post(
    "/agent/{agent_id}/custom-message",
    tags=["agents"],
    summary="Send a custom message to one agent (operator)",
)
def send_custom_message(
    agent_id: str,
    body: CustomMessageRequest,
    request: Request,
    _audit=Depends(require_audit("agent.custom_message")),
):
    """Queue a `ServerToAgent.custom_message` for the agent's next reply. Requires `operator`.

    This is the spec's extension mechanism (#85): a capability is a reverse FQDN the
    agent advertises in `custom_capabilities`, `type` is scoped to that capability, and
    `data` is **opaque bytes** (base64 here, because JSON). The server does not
    interpret or rewrite the payload — the contract belongs to the capability — so
    this is a validated, audited transport rather than a feature layer.

    Refused with **400** when the agent does not advertise `capability`; the detail
    lists `supported_capabilities`, because an unadvertised message is one the agent
    is entitled to ignore, and "it did nothing" is indistinguishable from a bug.
    **422** for a missing capability/type or non-base64 `data`, **413** above 64 KiB,
    **404** for an unknown agent or when `OPAMP_CUSTOM_MESSAGES_ENABLED=false`.

    **HTTP has no server push**, so the message rides the next poll and is delivered
    **exactly once** (the queue is cleared before the reply is serialised — a
    re-delivered pause is not a pause). The reply's config offer waits one poll so a
    single reply carries one server-initiated action, and the agent view reports
    `pending_custom_message` / `custom_message_delivered_at`.

    Audited as `agent.custom_message` with the capability and type — never the payload.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    try:
        agent_id = remote_config.normalize_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        record = custom_messages.queue(
            agent_id,
            capability=body.capability,
            type=body.type,
            data=body.data,
            principal=principal,
        )
    except custom_messages.CustomMessageError as exc:
        audit_action(
            principal,
            "agent.custom_message",
            target_kind="agent",
            target_id=agent_id,
            capability=body.capability,
            type=body.type,
            error=str(exc.detail),
            result="error",
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    audit_action(
        principal,
        "agent.custom_message",
        target_kind="agent",
        target_id=agent_id,
        capability=record["capability"],
        type=record["type"],
        bytes=len(body.data or ""),
    )
    PROM_CUSTOM_MESSAGES.labels(direction="queued").inc()
    return {
        "agent_id": agent_id,
        "queued": True,
        "custom_message": {
            key: record[key] for key in ("capability", "type", "queued_at", "queued_by")
        },
        "supported_capabilities": custom_messages.supported(agent_id),
        "delivery": {
            "state": "queued",
            "channel": "reply",
            "detail": "delivered once on the agent's next message",
        },
    }


@app.post(
    "/config/rollouts",
    tags=["config"],
    summary="Create (approve + stage) or dry-run a rollout (approver)",
)
def create_config_rollout(
    body: RolloutRequest,
    proposal_id: str = Query(..., description="The proposal to roll out"),
    request: Request = None,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.rollout.create")),
):
    """Approve a proposal and promote it in gated batches. Requires `approver`.

    **Creating a rollout is the approval.** The proposal is approved, every revision is
    assigned but *held*, and only the canary batch is released — so nothing is
    delivered to the rest of the fleet until the gate has cleared, which is the point
    of #101. `health_gate_seconds`, `canary_percent`, `max_agents_per_batch`,
    `max_percent_per_batch` and `ttl_minutes` default to the `CONFIG_PUSH_*` settings
    and are recorded on the rollout so the plan is explainable afterwards.

    `restart_after_apply: true` queues a `ServerToAgentCommand{Restart}` for each agent
    once its batch passes the gate (#84), for a config that only takes effect on a
    restart.

    **`dry_run: true`** returns the exact batch plan, the caps in force and the stored
    validation verdict, and changes nothing: it does not approve, hold or deliver. It
    needs only `operator` (it is a question, not a decision), so a UI can show the plan
    to the person who proposed it before an approver clicks.

    Errors: **404** config push is disabled (#96) or the proposal is unknown; **409** the
    proposal was already decided, or re-validation at approval failed (`detail` carries
    the findings); **422** a bad parameter (extra fields are rejected, `extra="forbid"`).
    """
    # A dry run reads and plans; it approves, holds and delivers nothing, so it is an
    # `operator` question ("what would happen?") rather than an approval. Creating the
    # rollout below is the approval and needs `approver`.
    if body.dry_run:
        require_roles(request, ROLE_OPERATOR)
        try:
            info = config_control.summary(proposal_id)
        except config_control.ConfigControlError as exc:
            raise _control_error(exc)
        try:
            return rollout.preview_rollout(
                proposal_id,
                canary_percent_value=body.canary_percent,
                max_agents_per_batch_value=body.max_agents_per_batch,
                max_percent_per_batch_value=body.max_percent_per_batch,
                gate_seconds=body.health_gate_seconds,
                ttl_minutes_value=body.ttl_minutes,
                restart_after_apply=body.restart_after_apply,
            )
        except rollout.RolloutError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    principal = require_roles(request, ROLE_APPROVER)
    try:
        info = config_control.summary(proposal_id)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    audit_action(
        principal,
        "config.rollout.create",
        target_kind="proposal",
        target_id=proposal_id,
        config_hash=info["config_hash"],
        agent_count=len(info["agents"]),
        canary_percent=body.canary_percent,
        max_agents_per_batch=body.max_agents_per_batch,
        restart_after_apply=body.restart_after_apply,
        confirm_endpoint_change=body.confirm_endpoint_change,
        confirm_scope_change=body.confirm_scope_change,
        confirm_unchecked_structure=body.confirm_unchecked_structure,
        override_reason=body.override_reason,
        note=body.note,
    )
    try:
        return rollout.create_rollout(
            proposal_id,
            approver=principal.name,
            agents=AGENT_REGISTRY.list_all(),
            note=body.note,
            canary_percent_value=body.canary_percent,
            max_agents_per_batch_value=body.max_agents_per_batch,
            max_percent_per_batch_value=body.max_percent_per_batch,
            gate_seconds=body.health_gate_seconds,
            ttl_minutes_value=body.ttl_minutes,
            allow_endpoint_change_confirmed=body.confirm_endpoint_change,
            confirm_scope_change=body.confirm_scope_change,
            confirm_unchecked_structure=body.confirm_unchecked_structure,
            override_reason=body.override_reason,
            restart_after_apply=body.restart_after_apply,
        )
    except (rollout.RolloutError, config_control.ConfigControlError) as exc:
        audit_action(
            principal,
            "config.rollout.create",
            target_kind="proposal",
            target_id=proposal_id,
            config_hash=info["config_hash"],
            result="error",
            error=str(getattr(exc, "detail", exc))[:500],
        )
        raise HTTPException(
            status_code=getattr(exc, "status_code", 409),
            detail=getattr(exc, "detail", str(exc)),
        )


@app.post(
    "/config/revisions/{proposal_id}/apply",
    tags=["config"],
    summary="Apply an approved proposal as a staged rollout (approver)",
)
def apply_config_revision(
    proposal_id: str,
    body: RolloutRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.rollout.create")),
):
    """Start a rollout for a proposal. Requires `approver`. Same contract as `POST /config/rollouts`.

    Kept as its own path because "apply" is the verb in the issue's flow
    (propose → approve → apply) and because it reads correctly from a CLI
    (`opampctl config apply prop-…`). It creates the rollout, which *is* the approval;
    `dry_run: true` plans without approving.
    """
    return create_config_rollout(body, proposal_id, request, _gate, _audit)


@app.get(
    "/config/rollouts",
    tags=["config"],
    summary="List staged rollouts (viewer)",
)
def list_config_rollouts(
    request: Request,
    status: Optional[str] = Query(
        None, description="pending|running|paused|completed|aborted|cancelled"
    ),
    limit: int = Query(100, ge=1, le=1000),
):
    """Rollout listings, newest first, with per-state agent counts. Requires `viewer`.

    Metadata only — no config bodies (D8). `states` is the rollup an operator reads:
    how many agents are pending, delivered, applied, failed, reverted or paused.
    """
    require_roles(request, ROLE_VIEWER)
    try:
        return rollout.list_rollouts(status=status, limit=limit)
    except rollout.RolloutError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get(
    "/config/rollouts/{rollout_id}",
    tags=["config"],
    summary="One rollout: batch progress, per-agent state, pause reason (viewer)",
)
def get_config_rollout(rollout_id: str, request: Request):
    """Batch-by-batch rollout detail, including **why** it paused. Requires `viewer`.

    The three questions this answers: which batch is in flight, what state every agent
    is in (`pending|delivered|applying|applied|failed|reverted|paused`), and — when it
    is paused — the single reason (`failed`, `auto_reverted`, `agent_went_silent`,
    `unhealthy`, `config_conflict`, `delivery_unconfirmed`, `superseded`) plus the
    operator-facing summary
    "N applied, K failed, paused at batch 2". Bodies are never included.
    """
    require_roles(request, ROLE_VIEWER)
    try:
        return rollout.view(rollout_id)
    except rollout.RolloutError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post(
    "/config/rollouts/{rollout_id}/abort",
    tags=["config"],
    summary="Stop promoting a rollout (operator)",
)
def abort_config_rollout(
    rollout_id: str,
    body: RolloutAbortRequest,
    request: Request,
    _audit=Depends(require_audit("config.rollout.abort")),
):
    """Stop promotion immediately. Requires `operator` — stopping is never privileged.

    **It does not recall already-delivered configs** and it cannot: the config is on the
    agent and the agent is running it. What it guarantees is that no further batch is
    released, and that every un-promoted agent stays **held**, so an aborted rollout can
    never widen. Applied agents are left untouched; rolling them back is
    `POST /config/rollouts/{id}/rollback`, a separate gated decision.

    Takes effect within one engine tick (`CONFIG_PUSH_TICK_SECONDS`, default 5). The
    transition is audited by the engine itself; this route only preflights the log
    (`require_audit` writes nothing), so there is exactly one row per abort.
    """
    principal = require_roles(request, ROLE_OPERATOR)
    try:
        return rollout.abort(rollout_id, principal=principal, reason=body.reason)
    except rollout.RolloutError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post(
    "/config/rollouts/{rollout_id}/resume",
    tags=["config"],
    summary="Re-open a paused rollout after fixing the cause (approver)",
)
def resume_config_rollout(
    rollout_id: str,
    body: RolloutResumeRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.rollout.resume")),
):
    """Resume a paused rollout. Requires `approver`, because it can release the next batch.

    A pause is terminal until a human decides (D6), and until now the only decisions were
    abort or roll back. A `config_conflict` pause is neither: the revision applied and is
    the one we want — the agent host had a second config source scheduling the same
    query, which the operator fixes there (osquery: delete the duplicate from the local
    config file, or run the daemon with `--config_plugin=opamp --extensions_require=opamp
    --extensions_timeout=30` so the agent is the only source). Resume clears the pause and re-runs the
    **same** gate over the same facts within one tick (`CONFIG_PUSH_TICK_SECONDS`).

    Nothing is re-delivered and no revision is edited; if the conflict is still present
    the gate pauses again immediately, with the agent's own words in `pause_detail`.

    **409** when the rollout is not paused, when the pause reason is the revision itself
    (`failed`/`auto_reverted` are poisoned — reactivate or roll back — and `superseded`
    has nothing left to promote), or when the TTL has passed. The transition is audited by
    the engine (`config.rollout.resume`); this route only preflights the log.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        return rollout.resume(rollout_id, principal=principal, reason=body.reason)
    except rollout.RolloutError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post(
    "/config/rollouts/{rollout_id}/rollback",
    tags=["config"],
    summary="Roll back the agents a rollout applied, as its own gated rollout (approver)",
)
def rollback_config_rollout(
    rollout_id: str,
    body: RolloutRollbackRequest,
    request: Request,
    _gate=Depends(posture.config_push_gate),
    _audit=Depends(require_audit("config.rollout.rollback")),
):
    """Roll back everything this rollout applied, canary first and gated. Requires `approver`.

    A **new** revision per agent whose body is the previous one and which names its
    parent (D4), then the same staged treatment as any rollout — because rolling back
    three hundred agents at once is a second outage. This is deliberately *not* an
    automatic reaction to a failure: a pause leaves the decision to a human.

    **409** when nothing in the rollout was applied (nothing to roll back), when it is
    already a rollback (roll forward with a new proposal), or when the previous revisions
    no longer validate; **409** as well when a target has no previous revision.
    """
    principal = require_roles(request, ROLE_APPROVER)
    try:
        info = rollout.summary(rollout_id)
    except rollout.RolloutError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    audit_action(
        principal,
        "config.rollout.rollback",
        target_kind="rollout",
        target_id=rollout_id,
        config_hash=info.get("config_hash"),
        proposal_id=info.get("proposal_id"),
        note=body.note,
    )
    try:
        return rollout.create_rollback_rollout(
            rollout_id,
            approver=principal.name,
            agents=AGENT_REGISTRY.list_all(),
            note=body.note,
            gate_seconds=body.health_gate_seconds,
        )
    except rollout.RolloutError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post(
    "/config/revisions/{proposal_id}/validate",
    tags=["config"],
    summary="Re-run validation for a proposal without changing it (operator)",
)
def revalidate_config_revision(proposal_id: str, request: Request):
    """Re-run the validation pipeline for a proposal's targets. Requires `operator`.

    Read-only — it stores nothing, moves no pointer and clears no poison — because the
    question "does this still pass?" must be free to ask before committing to an
    approval. The pipeline is re-run against **each resolved agent**, so the inventory
    warnings are about the machines this would actually reach (#100). A policy
    violation is reported, not raised: an approver decides, and the override is
    recorded at approval time.
    """
    require_roles(request, ROLE_OPERATOR)
    try:
        proposal = config_control.proposal_view(proposal_id)
    except config_control.ConfigControlError as exc:
        raise _control_error(exc)
    agent_ids = sorted({row["agent_id"] for row in proposal["revisions"]})
    by_id = {agent.agent_id: agent for agent in AGENT_REGISTRY.list_all()}
    config_map = None
    for row in proposal["revisions"]:
        config_map = remote_config.REGISTRY.decode_revision(row["revision_id"])
        if config_map is not None:
            break
    if config_map is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"proposal {proposal_id} has no readable revision body",
                "hint": "retention may have pruned it, or CONFIG_AT_REST_KEY changed",
            },
        )
    verdict = config_control.validate_for_targets(
        config_map, agents=[by_id[agent_id] for agent_id in agent_ids if agent_id in by_id]
    )
    return {
        "proposal_id": proposal_id,
        "config_hash": proposal.get("config_hash"),
        "agents": len(agent_ids),
        "ok": verdict.ok,
        "verdict": verdict.to_dict(),
        "fingerprint": verdict.fingerprint(),
        "stored": (proposal["revisions"][0] or {}).get("verdict") or None,
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
            # #101: the staged-rollout bounds, so "why did this rollout pause / why is
            # this agent held?" is answerable from one endpoint.
            "rollout": {
                "canary_percent": rollout.canary_percent(),
                "max_agents_per_batch": rollout.max_agents_per_batch(),
                "max_percent_per_batch": rollout.max_percent_per_batch(),
                "health_gate_seconds": rollout.health_gate_seconds(),
                "ttl_minutes": rollout.ttl_minutes(),
                "tick_seconds": rollout.tick_seconds(),
            },
            "validation": {
                "max_aliases": config_validation.max_aliases(),
                "max_depth": config_validation.max_depth(),
                "max_nodes": config_validation.max_nodes(),
                "allowed_exporter_hosts": list(config_validation.allowed_exporter_hosts()),
            },
        },
    }


if __name__ == "__main__":
    import argparse
    import os
    import sys

    import uvicorn

    # #134: `--strict-profiles` is the CI spelling of `OPAMP_PROFILES_STRICT=true`.
    # The server's own default is to log an invalid profile file and carry on, because
    # a fleet must not go down over a YAML edit — but that is exactly how a typo ships,
    # so the strict form has to exist somewhere a build can call it.
    parser = argparse.ArgumentParser(description="OpAMP server")
    parser.add_argument(
        "--strict-profiles",
        action="store_true",
        help="refuse to start if any agent-profile dictionary file is invalid",
    )
    arguments = parser.parse_args()
    if arguments.strict_profiles:
        os.environ["OPAMP_PROFILES_STRICT"] = "true"

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
