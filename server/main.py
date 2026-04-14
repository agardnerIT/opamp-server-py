import asyncio
import binascii
import os
from pathlib import Path
import prometheus_client as prom_client
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
from google.protobuf.json_format import MessageToDict
from loguru import logger
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from proto.opamp_pb2 import AgentToServer, ServerToAgent, ServerCapabilities, ServerToAgentFlags
from server.state import AgentRegistry, AgentState, AGENT_REGISTRY, utcnow
from server.opa_client import evaluate_agent_compliance, get_available_policies, get_policy_validation, OPA_ENABLED, OPA_URL
from server.alerts import get_alert_config, update_alert_config, send_test_alert, send_alert, ALERT_TYPES, ALERT_CONFIG, ALERT_EVENTS, send_new_agent_alert, send_stale_agent_alert

AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", 60))
_cleanup_task = None


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

logger.info(f"Loaded {AGENT_REGISTRY.count} agents from persistent store")

metrics_app = prom_client.make_asgi_app()
app.mount("/metrics", metrics_app)


def update_metrics():
    PROM_CONNECTED_AGENTS.set(len(AGENT_REGISTRY._agents))


def cleanup_stale_agents():
    now = datetime.now(timezone.utc)
    timeout = timedelta(seconds=AGENT_TIMEOUT_SECONDS)
    stale = []
    
    for agent in AGENT_REGISTRY.list_all():
        if now - agent.last_heartbeat > timeout:
            stale.append(agent.agent_id)
    
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
        
        if 'health' in agent_dict and not agent_dict['health']:
            logger.info(f"Agent sent empty health (disconnect): {agent_id}")
            AGENT_REGISTRY.remove(agent_id)
            PROM_AGENT_HEALTH.remove(agent_id)
            update_metrics()
            return Response(content=response.SerializeToString(), media_type="application/x-protobuf")
        
        PROM_MESSAGES_RECEIVED.labels(message_type="heartbeat").inc()
    
    if 'agentDisconnect' in agent_dict:
        logger.info(f"Agent disconnecting: {agent_id}")
        AGENT_REGISTRY.remove(agent_id)
        PROM_AGENT_HEALTH.remove(agent_id)
        update_metrics()
    
    cleanup_stale_agents()
    
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
    return agent.to_dict()


@app.get("/agent/{agent_id}/compliance")
def get_agent_compliance(agent_id: str):
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    if OPA_ENABLED:
        compliance_result = evaluate_agent_compliance(agent)
        AGENT_REGISTRY.update(agent_id, compliance=compliance_result)
        return compliance_result
    else:
        return {
            "compliant": None,
            "violations": [],
            "opa_enabled": False,
            "message": "OPA not enabled. Set OPA_ENABLED=true and configure OPA_URL"
        }


@app.post("/compliance/check/{agent_id}")
def check_compliance(agent_id: str):
    agent = AGENT_REGISTRY.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    if OPA_ENABLED:
        compliance_result = evaluate_agent_compliance(agent)
        AGENT_REGISTRY.update(agent_id, compliance=compliance_result)
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
def reload_policies():
    """Trigger OPA to reload policies from disk"""
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
def get_alerts():
    config = get_alert_config()
    return {
        "types": ALERT_TYPES,
        "events": ALERT_EVENTS,
        "config": config,
    }


@app.put("/alerts")
def put_alerts(request: dict):
    config = update_alert_config(request)
    return {"config": config}


@app.post("/alerts/test")
def test_alerts(request: dict = None):
    if request is None:
        request = {}
    event_type = request.get("event_type", "new_agent")
    event_config = request.get("event_config")
    
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
    return {
        "status": "healthy",
        "agents_connected": AGENT_REGISTRY.count,
        "opa_enabled": OPA_ENABLED,
        "opa_url": OPA_URL if OPA_ENABLED else None,
        "alerts_enabled": ALERT_CONFIG["enabled"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=4320,
        keepalive_timeout=300,
        timeout_keep_alive=300,
    )
