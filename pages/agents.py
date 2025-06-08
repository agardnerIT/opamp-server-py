import streamlit as st
import requests
import pandas as pd
from loguru import logger

def get_agents():
    resp = requests.get("http://localhost:4320/agents")
    if resp.status_code == 200:
        return resp.json()
    else:
        return list()

agents = get_agents()
logger.info(type(agents))
st.title("Agents Overview")
st.subheader(f"Agent Count: {len(agents)}")

logger.info(agents)

# agents = [
#     {
#         'id': 'agent-001',
#         'health_glyph': '🟢',
#         'tags': [{'key': 'env', 'value': 'prod'}, {'key': 'role', 'value': 'web'}]
#     },
#     {
#         'id': 'agent-002',
#         'health_glyph': '🔴',
#         'tags': [{'key': 'env', 'value': 'dev'}, {'key': 'role', 'value': 'db'}]
#     }
# ]

# st.write("### Agent Table")

# # Optional CSS for tag pill styling
# st.markdown("""
#     <style>
#     .pill {
#         display: inline-block;
#         padding: 0.25em 0.6em;
#         margin: 0.1em;
#         border-radius: 1em;
#         background-color: #eee;
#         color: #333;
#         font-size: 0.8em;
#     }
#     </style>
# """, unsafe_allow_html=True)

# for entry in agents:
#     cols = st.columns([5, 5, 5], gap='small', border=True)
    
#     # ID with hyperlink
#     id_link = f"<a href='https://example.com/{entry['id']}' target='_blank'>{entry['id']}</a>"
#     cols[0].markdown(id_link, unsafe_allow_html=True)

#     # Status glyph
#     cols[1].write(entry['health_glyph'])

#     # Tags as pills
#     tag_html = " ".join([f"<span class='pill'>{tag['key']}: {tag['value']}</span>" for tag in entry['tags']])
#     cols[2].markdown(tag_html, unsafe_allow_html=True)

# tags = ["env: prod", {"foo2", "bar2"}]
# st.pills(label="Pills", options=tags)

table_rows = []
for entry in agents:
    # Step 1: Flatten tags into a dataframe
    foo_tags = ["env: prod"]
    base = {
        'id': f"agent/?id={entry['id']}",
        'status': entry['health_glyph'],
        'tags': list()
    }
    for tag in entry['tags']:
        logger.info(f"Appending: {tag['key']}")
        base['tags'].append(f"{tag['key']}: {tag['value']}")
    table_rows.append(base)

# Step 2: Create DataFrame
df_flat = pd.DataFrame(table_rows)

# Step 3: Group by id and health_glyph, then combine values into one row
#df_final = df_flat.groupby(['id', 'health_glyph']).first().reset_index()
df = pd.DataFrame(data=df_flat)

# st.write(df.to_html(escape=False, index=True), unsafe_allow_html=True)
st.dataframe(data=df, column_config={
        "id": st.column_config.LinkColumn(label="Agent ID", display_text="agent/\?id=(\S+)"),
        "tags": st.column_config.ListColumn(label="Tags", width="large", help="Double click values to expand")
    },
    hide_index=True
)