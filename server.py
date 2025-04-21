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

        # An agent is self-reporting its description
        # Store it
        if agent_msg.HasField("agent_description"):
            logger.info("Got a new agent_description. Will update it.")
            # START WORKS
            AGENT_STATES[agent_id] = {
                "agent_description": MessageToDict(agent_msg.agent_description),
                "latest_message": MessageToDict(agent_msg)
            }
            #logger.info("handshake_response required")
        # An agent is self-reporting its current live "effective" config
        # Store it
        elif agent_msg.HasField("effective_config"):
            logger.info("Got a new effective config...")

            # An agent is reporting config, but we aren't yet tracking the agent
            # Request that it resends the full state
            if agent_id not in AGENT_STATES.keys():
                response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
        # Agents can send "keep alive" messages that only contain the agent_id
        # If such a message is received AND we aren't already aware of this agent_id
        # As the agent to send a fresh, full state
        # So then we can properly track it.
        # This can happen if an agent is already running when the server is restarted.
        else:
            if not agent_id in AGENT_STATES.keys():
                logger.info(f"{agent_id} is not yet tracked. Requesting Agent to report full state")
                response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
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
    logger.info(f"Agent == {agent}")

    # for agent_id in AGENT_STATES.keys():
    #     agent = {
    #         "id": agent_id,
    #         "health_glyph": agent_health_status_glyph,
    #         "tags": tags,
    #         "description": AGENT_STATES[agent_id]['agent_description'],
    #         "latest_message": AGENT_STATES[agent_id]['latest_message']
    #     }
    #     agent_list.append(agent)

    # logger.info("GOT HERE 1")
    # try:
    #     agent_obj = AGENT_STATES[agent_id]
    #     logger.info(f"Agent ID: {agent_obj['id']}")
    # except:
    #     logger.info("GOT EXCEPTION")
    #     agent = {}

    #agent_latest_message = agent['latest_message']
    # Agent first seen
    #agent_health_details = agent['latest_message']['health']['startTimeUnixNano']

    return templates.TemplateResponse(request=request, name="agent.html.j2", context={"agent": agent})

def get_agent_or_agents(filter="ALL"):

    agent_list = []

    for agent_id in AGENT_STATES.keys():

        # Skip this agent unless building ALL agents list
        # and the agent_id doesn't match
        if filter != "ALL" and agent_id != filter: continue

        # Determine agent health and set appropriate glyph
        agent_health_status_glyph = "❌"
        agent_health_status_bool = AGENT_STATES[agent_id]['latest_message']['health']['healthy']
        if agent_health_status_bool: agent_health_status_glyph = "✅"

        agent_identifying_attributes = AGENT_STATES[agent_id]['agent_description']['identifyingAttributes']
        agent_non_identifying_attributes = AGENT_STATES[agent_id]['agent_description']['nonIdentifyingAttributes']
        tags = []

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

        agent = {
            "id": agent_id,
            "health_glyph": agent_health_status_glyph,
            "tags": tags,
            "description": AGENT_STATES[agent_id]['agent_description'],
            "latest_message": AGENT_STATES[agent_id]['latest_message']
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

# Add filter to jinja app / template
templates.env.filters["format_unix_time"] = format_unix_time