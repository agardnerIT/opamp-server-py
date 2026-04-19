import asyncio
import binascii
import os
import base64
from pathlib import Path
import prometheus_client as prom_client
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
from google.protobuf.json_format import MessageToDict
from loguru import logger
from typing import Optional, Dict, Any
from functools import wraps

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from proto.opamp_pb2 import AgentToServer, ServerToAgent, ServerCapabilities, ServerToAgentFlags
from server.state import AgentRegistry, AgentState, AGENT_REGISTRY, utcnow
from server.opa_client import evaluate_agent_compliance, get_available_policies, get_policy_validation, OPA_ENABLED, OPA_URL
from server.alerts import get_alert_config, update_alert_config, send_test_alert, send_alert, ALERT_TYPES, ALERT_CONFIG, ALERT_EVENTS, send_new_agent_alert, send_stale_agent_alert, send_compliance_alert

AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", 60))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_cleanup_task = None


def require_admin(request: Request):
    if not ADMIN_PASSWORD:
        return True
    
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    
    try:
        encoded = auth_header[6:]
        decoded = base64.b64decode(encoded).decode("utf-8")
        if ":" in decoded:
            password = decoded.split(":", 1)[1]
        else:
            password = decoded
        return password == ADMIN_PASSWORD
    except Exception:
        return False


async def periodic_cleanup():
    while True:
        await asyncio.sleep(15)
        cleanup_stale_agents()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task
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


app = FastAPI(title="OpAMP Server", lifespan=lifespan)


@app.get("/auth/status")
def auth_status():
    return {"password_required": bool(ADMIN_PASSWORD)}


@app.get("/auth/verify")
def verify_auth(request: Request):
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"authenticated": True}


AGENT_METRICS: Dict[str, Dict[str, Any]] = {}

logger.info(f"Loaded {AGENT_REGISTRY.count} agents from persistent store")

metrics_app = prom_client.make_asgi_app()
app.mount("/metrics", metrics_app)


@app.post("/v1/metrics")
async def receive_metrics(request: Request):
    """Receive OTLP metrics from collectors (JSON or protobuf format)"""
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
                
                AGENT_METRICS[agent_id] = {
                    "metrics": metrics_data,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
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
                
                AGENT_METRICS[agent_id] = {
                    "metrics": metrics_data,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
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
        send_stale_agent_alert(agent_id)
    
    if stale:
        update_metrics()


@app.post("/v1/opamp")
async def opamp_endpoint(request: Request) -> Response:
    data = await request.body()
    
    response = ServerToAgent()
    response.capabilities = 0
    
    if not data:
        response.capabilities = ServerCapabilities.ServerCapabilities_AcceptsStatus
        return Response(
            content=response.SerializeToString(),
            media_type="application/x-protobuf",
            headers={"Connection": "keep-alive"}
        )
    
    try:
        agent_msg = AgentToServer()
        agent_msg.ParseFromString(data)
    except Exception as e:
        logger.error(f"Failed to parse OpAMP message: {e}")
        response.capabilities = ServerCapabilities.ServerCapabilities_AcceptsStatus
        return Response(
            content=response.SerializeToString(),
            media_type="application/x-protobuf",
            headers={"Connection": "keep-alive"}
        )
    
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
    
    if 'agentDisconnect' in agent_dict:
        logger.info(f"Agent disconnecting: {agent_id}")
        AGENT_REGISTRY.remove(agent_id)
        PROM_AGENT_HEALTH.remove(agent_id)
        update_metrics()
    
    return Response(
        content=response.SerializeToString(),
        media_type="application/x-protobuf",
        headers={"Connection": "keep-alive"}
    )


@app.get("/agents")
def list_agents():
    agents = []
    for agent in AGENT_REGISTRY.list_all():
        agents.append(agent.to_dict())
    return {"agents": agents, "count": len(agents)}


@app.get("/agent/{agent_id}")
def get_agent(agent_id: str):
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent_dict = agent.to_dict()
    agent_dict["metrics"] = AGENT_METRICS.get(agent_id, {})
    
    return agent_dict


@app.get("/agent/{agent_id}/metrics")
def get_agent_metrics(agent_id: str):
    if agent_id not in AGENT_REGISTRY._agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AGENT_METRICS.get(agent_id, {})


@app.get("/agent/{agent_id}/compliance")
def get_agent_compliance(agent_id: str):
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


@app.post("/compliance/check/{agent_id}")
def check_compliance(agent_id: str, request: Request):
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


@app.get("/compliance/summary")
def compliance_summary():
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


@app.get("/compliance/policies")
def list_policies():
    if not OPA_ENABLED:
        return {"opa_enabled": False, "policies": []}
    
    policies = get_available_policies()
    return {
        "opa_enabled": True,
        "policies": policies,
    }


@app.post("/compliance/reload")
def reload_policies(request: Request):
    """Trigger OPA to reload policies from disk"""
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


@app.get("/compliance/validate")
def validate_policies():
    """Validate all policy files and return results"""
    if not OPA_ENABLED:
        return {"opa_enabled": False, "policies": []}
    
    validation = get_policy_validation()
    return {
        "opa_enabled": True,
        "policies": validation,
    }


@app.get("/alerts")
def get_alerts(request: Request):
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    config = get_alert_config()
    return {
        "types": ALERT_TYPES,
        "events": ALERT_EVENTS,
        "config": config,
    }


@app.put("/alerts")
def put_alerts(request: Request, body: dict):
    if not require_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    config = update_alert_config(body)
    return {"config": config}


@app.post("/alerts/test")
def test_alerts(request: Request, body: dict = None):
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


@app.get("/health")
def health_check():
    from server.opa_client import OPAClient
    opa_client = OPAClient()
    opa_available = OPA_ENABLED and opa_client.is_available()
    return {
        "status": "healthy",
        "agents_connected": AGENT_REGISTRY.count,
        "opa_enabled": opa_available,
        "opa_url": OPA_URL if OPA_ENABLED else None,
        "alerts_enabled": True,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=4320,
        keepalive_timeout=300,
        timeout_keep_alive=300,
        limit_concurrency=1000,
        backlog=1024,
    )
