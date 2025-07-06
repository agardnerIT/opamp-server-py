import streamlit as st
import requests
from loguru import logger
from streamlit_utils import *

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
st.subheader("Status: Running ✅")

resp = requests.get("http://localhost:4320/agents")

agents_connected = 0
if resp.status_code == 200:
    agents_connected = len(resp.json())

st.link_button(label=f"Agents Connected {agents_connected}", url="agents", type="primary")