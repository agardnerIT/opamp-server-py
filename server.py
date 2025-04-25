from fastapi import FastAPI, Request
from fastapi.responses import Response
import opamp_pb2
from typing import Dict
from loguru import logger
import binascii
from google.protobuf.json_format import MessageToDict
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from zoneinfo import ZoneInfo
import base64
import yaml

AGENT_STATES: Dict[str, object] = {}

app = FastAPI()

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Data comes in from agents to this endpoint
@app.post("/v1/opamp")
async def opamp_endpoint(request: Request):
    # Read the binary protobuf data from the request
    data = await request.body()
    
    try:
        # Parse the incoming message
        agent_msg = opamp_pb2.AgentToServer()
        agent_msg.ParseFromString(data)
        agent_msg_dict = MessageToDict(agent_msg)
        logger.info(agent_msg_dict)

        agent_id = binascii.hexlify(agent_msg.instance_uid).decode('utf-8')

        # Build a generic response
        # Process message and generate response
        response = opamp_pb2.ServerToAgent()

        response.instance_uid = agent_msg.instance_uid
        response.capabilities = (
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsStatus &
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsEffectiveConfig &
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsConnectionSettingsRequest &
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsPackagesStatus
        )

        # NEW LOGIC
        if not agent_id in AGENT_STATES.keys():
            logger.info(f"{agent_id} is not yet tracked. Requesting Agent to report full state")
            response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
            AGENT_STATES[agent_id] = {}

            # Temp...
            if agent_id == "1234abcd89094fe9aff9b16893516467":
                logger.info(f"A target agent has just connected. Let's send new config!")
                
                with open("collector/remoteConfig.yaml") as f:
                    file_content = yaml.load(f, Loader=yaml.SafeLoader)
                    opamp_pb2.AgentRemoteConfig()
                    agent_remote_config = opamp_pb2.AgentRemoteConfig(config=file_content)
                    response.command( opamp_pb2.CommandType_Restart )
                    response.remote_config = agent_remote_config

        # health is there, but it's empty. This is most likely the agent disconnecting...
        if 'health' in agent_msg_dict and not agent_msg_dict['health']:
            AGENT_STATES.pop(agent_id)
        elif 'agentDescription' in agent_msg_dict or 'health' in agent_msg_dict or 'effectiveConfig' in agent_msg_dict:
            #logger.info(f"Updating details for {agent_id}")
            AGENT_STATES[agent_id] = {
                "details": agent_msg_dict
            }
        else: # Already aware of the agent. Update config
            logger.info(f"Got empty heartbeart message for {agent_id}")
            response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState |
                              opamp_pb2.ServerCapabilities.ServerCapabilities_OffersRemoteConfig)
            
            
            
        # OLD LOGIC BELOW

        # # An agent is self-reporting its description
        # # Store it
        # if agent_msg.HasField("agent_description"):
        #     logger.info("Got a new agent_description. Will update it.")
        #     # START WORKS
        #     AGENT_STATES[agent_id] = {
        #         "agent_description": MessageToDict(agent_msg.agent_description),
        #         "latest_message": MessageToDict(agent_msg)
        #     }
        #     #logger.info("handshake_response required")
        # # An agent is self-reporting its current live "effective" config
        # # Store it
        # elif agent_msg.HasField("effective_config"):
        #     logger.info("Got a new effective config...")

        #     # An agent is reporting config, but we aren't yet tracking the agent
        #     # Request that it resends the full state
        #     if agent_id not in AGENT_STATES.keys():
        #         response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
        # # Agent is still starting
        # # Request a full health config
        # elif agent_msg_dict['health']['status'] is not None and agent_msg_dict['health']['status'] == "StatusStarting":
        #     logger.info("Agent is still starting. Request full report")
        #     response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
        # # Agents can send "keep alive" messages that only contain the agent_id
        # # If such a message is received AND we aren't already aware of this agent_id
        # # As the agent to send a fresh, full state
        # # So then we can properly track it.
        # # This can happen if an agent is already running when the server is restarted.
        # else:
        #     if not agent_id in AGENT_STATES.keys():
        #         logger.info(f"{agent_id} is not yet tracked. Requesting Agent to report full state")
        #         response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
    except Exception as e:
        logger.error(f"Error processing OpAMP message: {e}")
    
    # Return binary protobuf response with correct content type
    return Response(content=response.SerializeToString(), media_type="application/x-protobuf")

##############################################
# ENDPOINTS
##############################################

@app.get("/")
def health_check(request: Request):
    return templates.TemplateResponse(
        request=request, name="root.html.j2", context={"status": "running", "connected_agents": len(AGENT_STATES)}
    )

@app.get("/agents")
def show_all_agents(request: Request):
    agent_list = get_agent_or_agents(filter="ALL")

    return templates.TemplateResponse(request=request, name="agents.html.j2", context={"agent_list": agent_list})

@app.get("/agent/{agent_id}")
def get_agent_details(request: Request, agent_id: str):
    agent = get_agent_or_agents(filter=agent_id)

    return templates.TemplateResponse(request=request, name="agent.html.j2", context={"agent": agent})

@app.get("/debug")
def debug():
    return AGENT_STATES

def get_agent_or_agents(filter="ALL"):

    agent_list = []

    for agent_id in AGENT_STATES.keys():

        # Skip this agent unless building ALL agents list
        # and the agent_id doesn't match
        if filter != "ALL" and agent_id != filter: continue

        # Determine agent health and set appropriate glyph
        agent_health_status_glyph = "NONE"
        try:
            logger.info(AGENT_STATES[agent_id]['details']['health'])
            logger.info(AGENT_STATES[agent_id]['details']['health']['healthy'])
            agent_health_status_bool = AGENT_STATES[agent_id]['details']['health']['healthy']
        except:
            agent_health_status_bool = False
            logger.warning("Agent had no healhy field. TODO: Investigate why")
        agent_health_status_glyph = _glyphifize(agent_health_status_bool)

        tags = []

        try:
            agent_identifying_attributes = AGENT_STATES[agent_id]['details']['agentDescription']['identifyingAttributes']
            agent_non_identifying_attributes = AGENT_STATES[agent_id]['details']['agentDescription']['nonIdentifyingAttributes']
        
            for item in agent_identifying_attributes:
                attr_key = item['key']
                # attr_value_type = next(iter(item['value'].keys()))
                attr_value = next(iter(item['value'].values()))

                # Special treatment for service.instance.id
                # Ignore it because it's already used int he first column
                if attr_key == "service.instance.id": continue

                tags.append({
                    "key": attr_key,
                    "value": attr_value,
                    "identifying": True
                })
            
            for item in agent_non_identifying_attributes:
                attr_key = item['key']
                attr_value_type = next(iter(item['value'].keys()))
                attr_value = next(iter(item['value'].values()))

                # Special treatment for service.instance.id
                # Ignore it because it's already used int he first column
                if attr_key == "service.instance.id": continue

                tags.append({
                    "key": attr_key,
                    "value": attr_value,
                    "identifying": False
                })
        except:
            logger.warning("Caught an exception")

        agent = {
            "id": agent_id,
            "health_glyph": agent_health_status_glyph,
            "tags": tags,
            "details": AGENT_STATES[agent_id]['details']
        }

        agent_list.append(agent)

    # Returning only one agent?
    # Send the single record back
    # Otherwise send a list
    if filter != "ALL" and len(agent_list) == 1: return agent_list[0]
    else:
        return agent_list


###################################
# Custom jinja2 filters
def format_unix_time(input: str):

    # Convert to UTC first
    dt_utc = datetime.fromtimestamp(int(input) / 1e9, tz=ZoneInfo("UTC"))

    # Convert to AEST (UTC+10)
    dt_aest = dt_utc.astimezone(ZoneInfo("Australia/Sydney"))  # Sydney uses AEST/AEDT

    print(dt_aest.strftime("%Y-%m-%d %H:%M:%S.%f %Z"))
    return dt_aest

def b64decode(input: str):
    return base64.b64decode(input).decode('utf-8')

def get_component_version(input: object):
    metadata = input['metadata']

    component_version = "v0.0.0"

    for item in metadata:
        if item['key'] == "code.namespace":
            value = item['value']
            if "stringValue" in value:
                code_namespace_string_value = value['stringValue']

                # Split at the space to get the version
                component_version = code_namespace_string_value.split()[1]

    
    return component_version

# TODO: This is horrible code. Re-do
def get_reports_capabilities(agent: str):

    capabilities = []
    try:
        capability_int = int(agent['details']['capabilities'])
    except: # Agent hasn't reported capabilities yet (or perhaps never will)
        return capabilities

    reports_status = (capability_int & 0x00000001) > 0
    capabilities.append({
        "capability": "reports_status",
        "status": _glyphifize(reports_status)
    })
    reports_effective_config = (capability_int & 0x00000004) > 0
    capabilities.append({
        "capability": "reports_effective_config",
        "status": _glyphifize(reports_effective_config)
    })
    reports_package_statuses = (capability_int & 0x00000010) > 0
    capabilities.append({
        "capability": "reports_package_statuses",
        "status": _glyphifize(reports_package_statuses)
    })
    reports_own_traces = (capability_int & 0x00000020) > 0
    capabilities.append({
        "capability": "reports_own_traces",
        "status": _glyphifize(reports_own_traces)
    })
    reports_own_metrics = (capability_int & 0x00000040) > 0
    capabilities.append({
        "capability": "reports_own_metrics",
        "status": _glyphifize(reports_own_metrics)
    })
    reports_own_logs = (capability_int & 0x00000080) > 0
    capabilities.append({
        "capability": "reports_own_logs",
        "status": _glyphifize(reports_own_logs)
    })
    reports_health = (capability_int & 0x00000800) > 0
    capabilities.append({
        "capability": "reports_health",
        "status": _glyphifize(reports_health)
    })
    reports_remote_config = (capability_int & 0x00001000) > 0
    capabilities.append({
        "capability": "reports_remote_config",
        "status": _glyphifize(reports_remote_config)
    })
    reports_heartbeat = (capability_int & 0x00002000) > 0
    capabilities.append({
        "capability": "reports_heartbeat",
        "status": _glyphifize(reports_heartbeat)
    })

    return capabilities

# TODO: This is horrible code. Re-do
def get_accepts_capabilities(agent: str):

    capabilities = []
    try:
        capability_int = int(agent['details']['capabilities'])
    except: # Agent hasn't reported capabilities yet (or perhaps never will)
        return capabilities

    accepts_remote_config = (capability_int & 0x00000002) > 0
    capabilities.append({
        "capability": "accepts_remote_config",
        "status": _glyphifize(accepts_remote_config)
    })
    accepts_packages = (capability_int & 0x00000008) > 0
    capabilities.append({
        "capability": "accepts_packages",
        "status": _glyphifize(accepts_packages)
    })
    accepts_opamp_connection_settings = (capability_int & 0x00000100) > 0
    capabilities.append({
        "capability": "accepts_opamp_connection_settings",
        "status": _glyphifize(accepts_opamp_connection_settings)
    })
    accepts_other_connection_settings = (capability_int & 0x00000200) > 0
    capabilities.append({
        "capability": "accepts_other_connection_settings",
        "status": _glyphifize(accepts_other_connection_settings)
    })
    accepts_restart_command = (capability_int & 0x00000400) > 0
    capabilities.append({
        "capability": "accepts_restart_command",
        "status": _glyphifize(accepts_restart_command)
    })
    

    return capabilities

def _glyphifize(input: bool):
    return "✅" if input else "❌"

# Add filters to jinja app / template
templates.env.filters["format_unix_time"] = format_unix_time
templates.env.filters["b64decode"] = b64decode
templates.env.filters["get_component_version"] = get_component_version
templates.env.filters["get_reports_capabilities"] = get_reports_capabilities
templates.env.filters["get_accepts_capabilities"] = get_accepts_capabilities