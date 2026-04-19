import os
import sys
import streamlit as st
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.shared import render_sidebar

st.set_page_config(
    page_title="OpAMP Server Dashboard",
    page_icon="🏠",
    layout="wide"
)

render_sidebar()

st.title("OpAMP Server Dashboard")
st.caption("Welcome! Navigate to a page using the sidebar.")

st.info("👈 Use the navigation in the sidebar to get started.")

st.divider()

st.markdown("""
### Quick Links

- **Agents** — View and manage connected OpenTelemetry collectors
- **Reports** — Generate fleet analysis and compliance reports
- **Admin** — Configure alerts and compliance policies (requires password)
- **Help** — Collector configuration and setup guidance
""")

st.divider()

if st.button("Go to Agents →", type="primary"):
    st.switch_page(Path(__file__).parent / "pages" / "1_Agents.py")
