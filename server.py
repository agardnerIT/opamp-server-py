from fastapi import FastAPI, Request
from fastapi.responses import Response
import opamp_pb2
from typing import Dict
from loguru import logger
import binascii
from google.protobuf.json_format import MessageToDict
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
AGENT_STATES: Dict[str, object] = {}

app = FastAPI()

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
        # logger.info("GOT HERE 4")
        response.instance_uid = agent_msg.instance_uid
        # logger.info("GOT HERE 5")
        response.capabilities = (
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsStatus &
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsEffectiveConfig &
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsConnectionSettingsRequest &
                opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsPackagesStatus
        )

        # Handle initial handshake
        if agent_msg.HasField("agent_description"):
            logger.info("Got a new agent_description. Will update it.")
            # START WORKS
            AGENT_STATES[agent_id] = {
                "agent_description": agent_msg.agent_description,
                "latest_message": agent_msg
            }
            #logger.info("handshake_response required")
        elif agent_msg.HasField("effective_config"):
            logger.info("Got a new effective config...")
            # TODO: Only request full config if we don't have it. More checks required here.
            response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)
            #logger.info(type(agent_msg.effective_config))
        
    except Exception as e:
        logger.error(f"Error processing OpAMP message: {e}")
    
    # Return binary protobuf response with correct content type
    return Response(content=response.SerializeToString(), media_type="application/x-protobuf")

def create_handshake_response(agent_msg: opamp_pb2.AgentToServer) -> opamp_pb2.ServerToAgent:
    """Create initial handshake response"""

    logger.info("Handing handshake response")

    response = opamp_pb2.ServerToAgent()
    response.instance_uid = agent_msg.instance_uid
    
    # Set server capabilities
    response.capabilities = (opamp_pb2.ServerCapabilities.ServerCapabilities_AcceptsStatus)
    logger.info("Request a full state from agent...")
    response.flags = (opamp_pb2.ServerToAgentFlags.ServerToAgentFlags_ReportFullState)

    logger.info("Sending handshake response")
    
    return response

def handle_config_status(agent_msg: opamp_pb2.AgentToServer) -> opamp_pb2.ServerToAgent:
    """Handle configuration status updates"""

    logger.info("Handing config status")

    response = opamp_pb2.ServerToAgent()
    response.instance_uid = agent_msg.instance_uid
    
    # Here you would compare config hashes and send updates if needed
    return response

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
    agent_list = []
    for agent_id in AGENT_STATES.keys():
        agent = {
            "id": agent_id,
            "description": AGENT_STATES[agent_id]['agent_description'],
            "latest_message": MessageToDict(AGENT_STATES[agent_id]['latest_message'])
        }
        agent_list.append(agent)

    return templates.TemplateResponse(request=request, name="agents.html.j2", context={"agent_list": agent_list})

@app.get("/agent/{agent_id}")
def get_agent_details(agent_id: str):
    resp_obj = {}

    for agent_id_key in AGENT_STATES.keys():
        if agent_id_key != agent_id:
            logger.info("Agent key does not match incoming agent")
            continue
        else:
            logger.info("Found a match. Returning config")
            logger.info(AGENT_STATES)
            # logger.info(type(AGENT_STATES[agent_id_key]))
            # resp_obj = AGENT_STATES[agent_id_key]
        
    return resp_obj
