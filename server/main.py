import asyncio
import binascii
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
from server.alerts import get_alert_config, update_alert_config, send_test_alert, send_alert, ALERT_TYPES, ALERT_CONFIG, ALERT_EVENTS, send_new_agent_alert, send_stale_agent_alert, send_compliance_alert
from server import admin_auth, posture
from server.admin_auth import require_admin  # noqa: F401  (privileged routes below)
from server.framing import message_limit
from server.ws import (
    CLOSE_POLICY_VIOLATION,
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
        "**Auth:** endpoints marked as requiring auth use HTTP Basic with the admin "
        "password (`Authorization: Basic base64(':ADMIN_PASSWORD')`). With no admin "
        "credential configured the server runs in read-only mode — check "
        "`GET /auth/status`.\n\n"
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

    Returns `{"password_required": bool, "auth_mode": "password"|"disabled",
    "roles": [str]}`. `password` means `ADMIN_PASSWORD` (or a stored generated
    credential) is in effect and sensitive endpoints expect HTTP Basic
    credentials; `disabled` is **read-only mode**, where no credential exists and
    privileged endpoints behave as they always have (open — closed by #95).
    `roles` lists the roles a caller gets after authenticating; empty means no
    authentication happened. Unauthenticated — call this first to discover the
    auth mode.
    """
    required = admin_auth.configured()
    return {
        "password_required": required,
        "auth_mode": "password" if required else "disabled",
        "roles": ["admin"] if required else [],
    }


@app.get("/auth/verify", tags=["auth"], summary="Verify admin credentials")
def verify_auth(request: Request):
    """Verify admin credentials; returns 200 `{"authenticated": true}` or 401.

    Requires HTTP Basic auth when an admin credential is configured (401
    otherwise). Useful as a preflight credential check for agents before calling
    other admin endpoints. Always succeeds in read-only mode (no credential).
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"authenticated": True}


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


def _apply_status_report(agent_id: str, agent_dict: Dict[str, Any]) -> None:
    """Apply the status fields present in any valid agent message."""
    if 'health' in agent_dict and agent_dict['health']:
        healthy = agent_dict['health'].get('healthy', False)
        AGENT_REGISTRY.update(agent_id, healthy=healthy)
        PROM_AGENT_HEALTH.labels(agent_id=agent_id).set(1 if healthy else 0)
        PROM_MESSAGES_RECEIVED.labels(message_type="health").inc()

    if 'agentDescription' in agent_dict:
        AGENT_REGISTRY.update(agent_id, description=agent_dict['agentDescription'])

    if 'effectiveConfig' in agent_dict:
        import json
        config_str = json.dumps(agent_dict['effectiveConfig'])
        AGENT_REGISTRY.update(agent_id, effective_config=config_str)
        PROM_MESSAGES_RECEIVED.labels(message_type="effective_config").inc()

    if 'health' in agent_dict:
        AGENT_REGISTRY.update(agent_id, health=agent_dict['health'])

    if 'availableComponents' in agent_dict:
        AGENT_REGISTRY.update(agent_id, available_components=agent_dict['availableComponents'])
        PROM_MESSAGES_RECEIVED.labels(message_type="available_components").inc()

    if 'packageStatuses' in agent_dict:
        AGENT_REGISTRY.update(agent_id, package_statuses=agent_dict['packageStatuses'])
        PROM_MESSAGES_RECEIVED.labels(message_type="package_status").inc()

    if 'remoteConfigStatus' in agent_dict:
        status = agent_dict['remoteConfigStatus'].get('status', 'UNSET')
        AGENT_REGISTRY.update(agent_id, remote_config_status=status)

    PROM_MESSAGES_RECEIVED.labels(message_type="heartbeat").inc()


def _handle_agent_message(data: bytes) -> ProcessedMessage:
    """Apply one `AgentToServer` message and build the `ServerToAgent` reply.

    Shared by the HTTP and WebSocket transports so the two cannot drift; the
    transport only differs in framing and connection lifecycle. Returns the
    serialized reply plus the fields the socket layer needs for its registry.
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
        ))
        AGENT_REGISTRY.update(agent_id, last_heartbeat=utcnow())
        response.flags = ServerToAgentFlags.ServerToAgentFlags_ReportFullState
        update_metrics()
        
        send_new_agent_alert(agent_id)
    else:
        AGENT_REGISTRY.update(agent_id, last_heartbeat=utcnow())

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
    `opamp` extension (`endpoint: http://host:4320/v1/opamp`). Unauthenticated.

    The same path also accepts a WebSocket upgrade when `OPAMP_WS_ENABLED=true`.
    """
    result = _handle_agent_message(await request.body())
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
    Unauthenticated, but a browser `Origin` must satisfy `CORS_ORIGINS` because
    WebSocket upgrades are not covered by `CORSMiddleware`.
    """
    if not ws_enabled():
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, CORS_ORIGINS):
        logger.warning(f"Refusing OpAMP WebSocket upgrade from origin {origin}")
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()
    await serve_websocket(websocket, _handle_agent_message)


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
    """
    remote_config_status = remote_config_status if remote_config_status is not None else status
    reserved = {"healthy", "status", "remote_config_status"}
    attr_filters: Dict[str, list] = {}
    for key, value in request.query_params.multi_items():
        if key not in reserved:
            attr_filters.setdefault(key, []).append(value)

    agents = [
        agent.to_dict()
        for agent in AGENT_REGISTRY.list_all()
        if agent.matches_filters(
            healthy=healthy,
            remote_config_status=remote_config_status,
            attributes=attr_filters,
        )
    ]

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
    OTLP snapshot (empty dict if none received).
    """
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent_dict = agent.to_dict()
    agent_dict["metrics"] = _get_agent_metrics_entry(agent_id)
    
    return agent_dict


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


@app.post("/compliance/check/{agent_id}", tags=["compliance"], summary="Trigger a compliance check for one agent (admin)")
def check_compliance(agent_id: str, request: Request):
    """Force a compliance evaluation for an agent. Requires admin auth (401 otherwise).

    Unlike `GET /agent/{id}/compliance`, this is a mutating/admin action and fails
    with 503 when OPA is not enabled. Responds 404 if the agent is unknown.
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
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


@app.get("/alerts", tags=["alerts"], summary="Get alert configuration (admin)")
def get_alerts(request: Request):
    """Return the current alert configuration. Requires admin auth (401 otherwise).

    Returns `{"types": [...], "events": {...}, "config": {...}}` — the valid event
    types, the available dispatcher/event settings, and the live config (webhook
    URL + per-event enablement/templates).
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    config = get_alert_config()
    return {
        "types": ALERT_TYPES,
        "events": ALERT_EVENTS,
        "config": config,
    }


@app.put("/alerts", tags=["alerts"], summary="Update alert configuration (admin)")
def put_alerts(request: Request, body: dict):
    """Replace/merge the alert configuration. Requires admin auth (401 otherwise).

    Body is the config object (same shape `GET /alerts` returns under `config`);
    validation happens in `server/alerts.py`. Returns `{"config": {...}}` with the
    stored result. Note: currently in-memory only — see issue #50 for persistence.
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    config = update_alert_config(body)
    return {"config": config}


@app.post("/alerts/test", tags=["alerts"], summary="Send a test alert (admin)")
def test_alerts(request: Request, body: dict = None):
    """Fire a test alert through the configured dispatcher. Requires admin auth.

    Optional body: `{"event_type": "new_agent", "event_config": {...}}`. When
    `event_config` is supplied it is used for this one send and then reverted
    (handy for validating unsaved webhook settings). Returns
    `{"success": bool, "error": str|null}`.
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
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
    """Full posture detail for operators and the UI — admin auth required.

    `GET /health` carries the one-line summary a banner needs; this endpoint adds
    *why*: which requirements are met and which are not, where the admin
    credential came from, and whether the development override is in force. No
    secrets are returned (never the password, never a token). 401 without valid
    credentials whenever an admin credential is configured.
    """
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Invalid credentials")
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
