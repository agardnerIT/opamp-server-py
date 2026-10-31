import asyncio
import binascii
import json
import os
from pathlib import Path
import prometheus_client as prom_client
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from google.protobuf.json_format import MessageToDict
from loguru import logger
from typing import Optional, Dict, Any, Literal
from functools import wraps

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from proto.opamp_pb2 import AgentToServer, ServerToAgent, ServerCapabilities, ServerToAgentFlags
from server.state import AgentRegistry, AgentState, AGENT_REGISTRY, SQLiteMetricsStore, utcnow
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
from server import admin_auth, agent_auth, auth, posture
from server.auth import (  # noqa: F401  (privileged routes below)
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    require_admin,
    require_roles,
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

AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", 60))
_cleanup_task = None


async def periodic_cleanup():
    while True:
        await asyncio.sleep(15)
        cleanup_stale_agents()


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
    _cleanup_task = asyncio.create_task(periodic_cleanup())
    yield
    if _cleanup_task:
        _cleanup_task.cancel()

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
        "Check `GET /health` → `config_push`."
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
            
            logger.info(f"JSON metrics received: agent_id={agent_id}, found={agent_id and agent_id in AGENT_REGISTRY._agents}")
            
            if agent_id and agent_id in AGENT_REGISTRY._agents:
                metrics_data = {}
                for resource in data.get("resourceMetrics", []):
                    for scope in resource.get("scopeMetrics", []):
                        for metric in scope.get("metrics", []):
                            val = _extract_json_metric_value(metric)
                            if val is not None:
                                metrics_data[metric.get("name", "")] = val
                
                _record_agent_metrics(agent_id, metrics_data)
                logger.info(f"JSON: Stored metrics for {agent_id}")
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
            
            logger.info(f"Protobuf metrics received: agent_id={agent_id}, found={agent_id and agent_id in AGENT_REGISTRY._agents}")
            
            if agent_id and agent_id in AGENT_REGISTRY._agents:
                metrics_data = {}
                for resource in metrics_req.resource_metrics:
                    for scope in resource.scope_metrics:
                        for metric in scope.metrics:
                            val = _extract_proto_metric_value(metric)
                            if val is not None:
                                metrics_data[metric.name] = val
                
                _record_agent_metrics(agent_id, metrics_data)
                logger.info(f"Protobuf: Stored metrics for {agent_id}, log_records={metrics_data.get('otelcol_receiver_accepted_log_records')}, all={metrics_data}")
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to process metrics: {e}")
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


def cleanup_stale_agents():
    now = datetime.now(timezone.utc)
    timeout = timedelta(seconds=AGENT_TIMEOUT_SECONDS)
    stale = []
    
    for agent in AGENT_REGISTRY.list_all():
        if now - agent.last_heartbeat > timeout:
            stale.append(agent.agent_id)
    
    logger.info(f"Found {len(stale)} stale agents")
    
    for agent_id in stale:
        logger.info(f"Removing stale agent: {agent_id}")
        AGENT_REGISTRY.remove(agent_id)
        PROM_AGENT_HEALTH.remove(agent_id)
        _METRICS_STORE.delete(agent_id)
        AGENT_METRICS.pop(agent_id, None)
        send_stale_agent_alert(agent_id)
    
    if stale:
        update_metrics()


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
        updates['remote_config_status'] = agent_dict['remoteConfigStatus'].get('status', 'UNSET')

    PROM_MESSAGES_RECEIVED.labels(message_type="heartbeat").inc()

    changed = AGENT_REGISTRY.update(agent_id, persist=False, **updates)
    if not persist:
        return changed

    # Steady state: nothing changed, so skip the write unless the stored
    # heartbeat is old enough that a restart would look like a stale fleet.
    if changed or AGENT_REGISTRY.heartbeat_persist_due(agent_id, HEARTBEAT_PERSIST_SECONDS):
        AGENT_REGISTRY.persist(agent_id)
        return True
    return False


def _handle_agent_message(
    data: bytes,
    principal: "agent_auth.Principal | None" = None,
) -> ProcessedMessage:
    """Apply one `AgentToServer` message and build the `ServerToAgent` reply.

    Shared by the HTTP and WebSocket transports so the two cannot drift; the
    transport only differs in framing and connection lifecycle. Returns the
    serialized reply plus the fields the socket layer needs for its registry.

    `principal` is who presented the credential (#94); `None` in `off` mode. A
    credential that does not match the claimed `instance_uid`, or a uid already
    bound to a different principal, raises `AgentAuthError` — before any state is
    read or written, so a refusal can never leave a half-applied report behind.
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
        logger.error(f"Failed to parse OpAMP message: {e}")
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
    
    response.instance_uid = agent_msg.instance_uid
    
    response.capabilities = (
        ServerCapabilities.ServerCapabilities_AcceptsStatus |
        ServerCapabilities.ServerCapabilities_AcceptsEffectiveConfig |
        ServerCapabilities.ServerCapabilities_AcceptsPackagesStatus |
        ServerCapabilities.ServerCapabilities_OffersConnectionSettings |
        ServerCapabilities.ServerCapabilities_AcceptsConnectionSettingsRequest
    )

    # Only advertise remote config when the server may actually deliver it (#96):
    # a capability bit is a promise, and an agent supervisor that sees it will
    # wait for a config that is never coming. `OffersRemoteConfig` is the server
    # side of the agent's `AcceptsRemoteConfig`.
    if posture.push_allowed():
        response.capabilities |= ServerCapabilities.ServerCapabilities_OffersRemoteConfig
    
    if agent_id not in AGENT_REGISTRY._agents:
        logger.info(f"New agent connecting: {agent_id}")
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
    
    disconnected = 'agentDisconnect' in agent_dict
    if disconnected:
        logger.info(f"Agent disconnecting: {agent_id}")
        AGENT_REGISTRY.remove(agent_id)
        PROM_AGENT_HEALTH.remove(agent_id)
        _METRICS_STORE.delete(agent_id)
        AGENT_METRICS.pop(agent_id, None)
        update_metrics()
    
    # Last line of defence for #81/#102: nothing may put remote_config on the
    # wire while the posture says config push is not allowed.
    posture.scrub_remote_config(response)

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
    """
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent_dict = agent.to_dict()
    agent_dict["metrics"] = _get_agent_metrics_entry(agent_id)
    agent_dict["credential"] = agent_auth.credential_summary(agent_id)
    
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
    require_roles(request, ROLE_OPERATOR)
    
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
        raise HTTPException(status_code=503, detail="OPA not enabled")


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
    require_roles(request, ROLE_OPERATOR)
    config = update_alert_config(merge_redacted(body))
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
    require_roles(request, ROLE_OPERATOR)
    
    if body is None:
        body = {}
    event_type = body.get("event_type", "new_agent")
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


@app.get("/health", tags=["ops"], summary="Health check")
def health_check():
    """Liveness/readiness probe: server status, agent count, OPA availability, posture.

    Unauthenticated. Returns `{"status": "healthy", "agents_connected": n,
    "opa_enabled": bool, "opa_url": str|null, "alerts_enabled": true,
    "config_push": {"enabled", "insecure", "agent_auth", "audit", "tls"}}`.
    The `config_push` block is the public posture summary — `insecure: true` means
    config push is on despite an unsafe posture and the UI shows a banner.
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
