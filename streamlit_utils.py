import streamlit as st

PAGE_TITLE_HOME = "Homepage"
PAGE_TITLE_AGENTS = "Agents Overview"
PAGE_TITLE_AGENT = "Agent Details"

def build_menu():
    st.sidebar.page_link('streamlit_app.py', label='Home', icon=":material/home:")
    st.sidebar.page_link('pages/agents.py', label='Agents', icon=":material/groups:")