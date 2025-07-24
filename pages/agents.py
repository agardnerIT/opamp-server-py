import streamlit as st
import requests
import pandas as pd
from loguru import logger
from streamlit_utils import *

# This MUST be called first
st.set_page_config(page_title=PAGE_TITLE_AGENTS)

# just build the menu and run
# Streamlit magic handles the rest of the logic
# Sidebar navigation
build_menu()

def get_agents():
    try:
        resp = requests.get("http://localhost:4320/agents")
        
        if resp.status_code == 200:
            return resp.json()
        else:
            return list()
    except:
        logger.info("Server is not available...")

agents = get_agents()
st.title(PAGE_TITLE_AGENTS)
st.subheader(f"Agent Count: {len(agents)}")

table_rows = []
for entry in agents:
    # Step 1: Flatten tags into a dataframe
    base = {
        'id': f"agent/?id={entry['id']}",
        'agent name': None,
        'status': entry['health_glyph'],
        'tags': list()
    }
    for tag in entry['tags']:
        logger.info(f"Appending: {tag['key']}")
        base['tags'].append(f"{tag['key']}: {tag['value']}")
        # If the agent passes a `name` 
        # eg agent_description.non_identifying_attributes.name = "Sample Collector"
        # Then set the name to a value
        if tag['key'] == "agent.name":
            base['agent name'] = tag['value']
    
    logger.info(base)
    table_rows.append(base)

# Step 2: Create DataFrame
df_flat = pd.DataFrame(table_rows)

# Check and drop empty columns
# This is primarily currently used to hide the `name` column if it is empty
columns_to_drop = [col for col in df_flat.columns if df_flat[col].isnull().all()]
df_flat = df_flat.drop(columns=columns_to_drop)

# Step 3: Group by id and health_glyph, then combine values into one row
df = pd.DataFrame(data=df_flat)

st.dataframe(data=df, column_config={
        "id": st.column_config.LinkColumn(label="Agent ID", display_text="agent/\?id=(\S+)"),
        "tags": st.column_config.ListColumn(label="Tags", width="large", help="Double click values to expand")
    },
    hide_index=True
)