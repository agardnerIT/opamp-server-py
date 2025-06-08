import streamlit as st
import requests
from loguru import logger

def show_homepage():
    st.title("System Overview")

    st.subheader("Status: Running ✅")

    resp = requests.get("http://localhost:4320/agents")

    agents_connected = 0
    if resp.status_code == 200:
        agents_connected = len(resp.json())

    st.link_button(label=f"Agents Connected {agents_connected}", url="agents", type="primary")

if __name__ == "__main__":
    show_homepage()