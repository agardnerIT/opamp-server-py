import streamlit as st
import requests
import pandas as pd
from loguru import logger
import base64
from streamlit_utils import *

USE_DIVIDERS = True

# This MUST be called first
st.set_page_config(page_title=PAGE_TITLE_AGENTS)

# just build the menu and run
# Streamlit magic handles the rest of the logic
# Sidebar navigation
build_menu()

def get_agent(agent_id: str):
    resp = requests.get(f"http://localhost:4320/agent/{agent_id}")
    if resp.status_code == 200:
        return resp.json()
    else:
        return {}

def get_capabilities(agent: object, type: str):

    body = {
        "agent_id": agent['id'],
        "type": type
    }
    target = f"http://localhost:4320/agent/capabilities"
    resp = requests.post(target, json=body)
    if resp.status_code == 200:
        logger.info(resp.json())
        return resp.json()
    else:
        return {}

def get_agent_component_types(agent: object):
    component_types = []

    if agent['details']['availableComponents']:
        for ct in agent['details']['availableComponents']['components']:

            type_name = ct

            # Now get each item for each type
            # Eg. For `extensions` look in the `subComponentMap` and get
            # `zipkinencoding`, `asapclient` etc.

            for subComponent in agent['details']['availableComponents']['components'][ct]['subComponentMap']:
                #logger.info(f"Got [{type_name}] - {subComponent}")

                # Extract version
                dirty_value = agent['details']['availableComponents']['components'][ct]['subComponentMap'][subComponent]['metadata'][0]['value']['stringValue']
                #logger.info(f"Dirty Value for {subComponent}: {dirty_value}")
                dirty_value_parts = dirty_value.split(maxsplit=1)
                version = dirty_value_parts[1]

                component_types.append({
                    "type": ct,
                    "value": subComponent,
                    "version": version
                })
    
    return component_types

def get_currently_effective_configuration(agent: object):
    effective_config = "TODO"

    if agent['details']['effectiveConfig']:
        # The duplicate ref to configMap and an empty string
        # is deliberate. Weird, I know, but that's how it currently comes from the collector
        effective_config_dirty = agent['details']['effectiveConfig']['configMap']['configMap']['']['body']
        effective_config = base64.b64decode(effective_config_dirty).decode("utf-8")
    
    return effective_config

def count_pipelines(agent: object, filter: str):
    pipeline_count = 0
    for key in agent['details']['health']['componentHealthMap']:
         if key.startswith("pipeline:"):
              if filter.lower() == "all":
                    pipeline_count += 1
              else:
                cleaned_key = key.replace("pipeline:", "")
                key_parts = cleaned_key.split("/")
                pipeline_type = key_parts[0]

                logger.info(f"Filter: {filter.lower()}. pipeline_type: {pipeline_type}")

                if filter.lower() == "logs" and pipeline_type == "logs":
                    pipeline_count += 1
                elif filter.lower() == "metrics" and pipeline_type == "metrics":
                    pipeline_count += 1
                elif filter.lower() == "traces" and pipeline_type == "traces":
                    pipeline_count += 1
    return pipeline_count

###########################################################################################
# START MAIN PAGE OUTPUT
###########################################################################################

st.title(PAGE_TITLE_AGENT)

if 'id' not in st.query_params:
    st.subheader("No Agent Found", divider=USE_DIVIDERS)
    st.markdown("""
       You must pass an agent ID in the query like: `?id=abc1234`
""")
else:

    agent = get_agent(st.query_params['id'])

    if not agent:
        st.subheader("Invalid Agent", divider=USE_DIVIDERS)
    else:
        st.text(f"ID: {agent['id']}")
        st.text(f"Status: {agent['health_glyph']}")
        st.subheader(body="Tags", anchor="tags", divider=USE_DIVIDERS, help="Identifying tags can be used to uniquely identify this agent. Non identifying tags may be common across agents.")

        with st.expander(label="Identifying Tags", expanded=False, icon=":material/fingerprint:"):
            for tag in agent['tags']:
                if tag['identifying']:
                    st.badge(label=f"{tag['key']}: {tag['value']}")
        
        with st.expander(label="Non Identifying Tags", expanded=False, icon=":material/label:"):
            for tag in agent['tags']:
                if not tag['identifying']:
                    st.badge(label=f"{tag['key']}: {tag['value']}")

        st.subheader(body="Capabilities", anchor="capabilities", divider=USE_DIVIDERS, help="What can this agent do? What can the agent be instructed to do by the server?")
        
        with st.expander(label="Reporting", expanded=False, icon=":material/monitoring:"):
            st.subheader("Reporting", help="Things the agent reports **to** the server.", divider=USE_DIVIDERS, anchor="reporting")

            df = pd.DataFrame(get_capabilities(agent, type="reports"))
            st.dataframe(df, hide_index=True)
    
            # for item in get_reports_capabilities(agent):
            #     st.text(f"{item['capability']} | {item['status']}")

        with st.expander(label="Acceptance", expanded=False, icon=":material/table:"):
            st.subheader("Acceptance", help="Things the agent accepts **from** the server.", divider=USE_DIVIDERS, anchor="acceptance")
            df = pd.DataFrame(get_capabilities(agent, type="accepts"))
            st.dataframe(df, hide_index=True)

        st.subheader(body="Pipelines", anchor="pipelines", divider=USE_DIVIDERS)

        total_pipelines = count_pipelines(agent, filter="all")
        logs_pipelines = count_pipelines(agent, filter="logs")
        metrics_pipelines = count_pipelines(agent, filter="metrics")
        traces_pipelines = count_pipelines(agent, filter="traces")

        a, b, c, d = st.columns(4)

        a.metric(label="Total Pipelines", value=total_pipelines, border=True)
        b.metric(label="Logs Pipelines", value=logs_pipelines, border=True)
        c.metric(label="Metrics Pipelines", value=metrics_pipelines, border=True)
        d.metric(label="Traces Pipelines", value=traces_pipelines, border=True)

        for key in agent['details']['health']['componentHealthMap']:
            if key.startswith("pipeline:"):
                cleaned_key = key.replace("pipeline:", "")
                key_parts = cleaned_key.split("/")
                pipeline_type = key_parts[0]
                pipeline_name = key_parts[1]

                pipeline_health_glyph = ":material/sick:"
                if agent['details']['health']['componentHealthMap'][key]['healthy']:
                    pipeline_health_glyph = ":material/check_circle:"

                with st.expander(label=f"{pipeline_name} (type={pipeline_type})", expanded=False, icon=pipeline_health_glyph):
                    st.text("This section is a work in progess.")
        
        with st.expander(label="Currently Effective Configuration", expanded=False, icon=":material/tune:"):
            effective_config = get_currently_effective_configuration(agent)
            logger.info(type(effective_config))

            st.text_area(label="Currently Effective Configuration", label_visibility="hidden", height=500, value=effective_config, disabled=True)
        
        st.subheader(body="Components", anchor="components", divider=USE_DIVIDERS)

        if agent['details']['availableComponents']:
            agent_component_types = get_agent_component_types(agent=agent)

            df = pd.DataFrame(agent_component_types)
            st.dataframe(data=df)
            
        else:
            st.subheader(body="Agent does not report components")
            st.text("To make use of this section, set `reports_available_components` to `true`")
            st.code("""extensions:
                      opamp:
                        ...
                   capabilities:
                     reports_available_components: true
""")