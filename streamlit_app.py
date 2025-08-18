import streamlit as st
import requests
from loguru import logger
from streamlit_utils import *
from env_vars import *

#
# This is the streamlit entry point
# It also serves as the homepage
# Usage: streamlit run streamlit_app.py --server.port 8502
#

st.set_page_config(page_title=PAGE_TITLE_HOME)

# just build the menu and run
# Streamlit magic handles the rest of the logic
# Sidebar navigation
build_menu()

st.title(PAGE_TITLE_HOME)

agents_connected = []
try:
    resp = requests.get(f"{SERVER_HTTP_SCHEME}://{SERVER_ADDR}:{SERVER_PORT}/agents")
    agents_connected = 0
    if resp.status_code == 200:
        agents_connected = len(resp.json())
        st.subheader("Status: Running ✅")
        st.link_button(label=f"Agents Connected {agents_connected}", url="agents", type="primary")
except:
    st.subheader("Status: Server not available ⚠️")
    st.markdown(f"""
                Please start the server on `localhost:4320` using protocol `http`
                or configure the following environment variables appropriately:
                
                * `SERVER_HTTP_SCHEME="http|https"`
                * `SERVER_ADDR="localhost"`
                * `SERVER_PORT=4320`
                ```
                """)

    

