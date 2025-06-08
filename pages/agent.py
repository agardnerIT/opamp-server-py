import streamlit as st
import requests
import pandas as pd
from loguru import logger

USE_DIVIDERS = True

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

st.title(f"Agent Details")

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
        # with st.expander(label="Pipelines", expanded=False, icon=":material/route:"):
        #     st.text("todo")
        #     with st.expander(label="gfbkdsl", expanded=False):
        #         st.text("inner")
        
        with st.expander(label="Currently Active Configuration", expanded=False, icon=":material/tune:"):
            st.text("This section is a work in progess.")
        
        st.subheader(body="Components", anchor="components", divider=USE_DIVIDERS)

        logger.info(">>> Agent Details >>>")
        logger.info(agent['details'])

        if agent['details']['availableComponents']:
            st.text("This section is a work in progess.")
            
            df = pd.DataFrame(agent['details']['availableComponents'])
            st.dataframe(df, column_config={
                "id": st.column_config.LinkColumn(label="Agent ID", display_text="agent/\?id=(\S+)"),
                "tags": st.column_config.ListColumn(label="Tags", width="large", help="Double click values to expand")
            }, hide_index=True)
        else:
            st.subheader(body="Agent does not report components")
            st.text("To make use of this section, set `reports_available_components` to `true`")
            st.code("""extensions:
                      opamp:
                        ...
                   capabilities:
                     reports_available_components: true
""")
        #     st.text("""<h3>Agent does not report components.</h3>
        # <p>To make use of this section, set <code>reports_available_components</code> to <code>true</code> in your collector config.yaml like this:</p>
        # <pre>
        # <code>
        # extensions:
        #   opamp:
        #     ...
        #     capabilities:
        #       reports_available_components: true
        # </code>
        # </pre>""")

        with st.expander(label="Components List", expanded=False, icon=":material/tune:"):
            number_of_components = len(agent)
            st.text("This section is a work in progess.")