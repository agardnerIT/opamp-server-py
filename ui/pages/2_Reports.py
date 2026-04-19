import os
import sys
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.shared import (
    SERVER_URL,
    get_agents,
    get_latest_collector_version,
    get_collector_versions,
    generate_agent_report,
    generate_heavy_collectors_report,
    generate_outdated_collectors_report,
    _is_heavy,
    _count_outdated_collectors,
    render_sidebar,
    requests,
    pd,
)

st.set_page_config(
    page_title="Reports - OpAMP Server Dashboard",
    page_icon="📊",
    layout="wide"
)

render_sidebar()

data = get_agents()
latest_version = get_latest_collector_version()

if data.get("agents"):
    report_type = st.selectbox(
        "Report Type",
        ["Fleet Summary", "Heavy Collectors (>50% unused)", "Outdated Collectors"],
        index=0
    )
    
    if report_type == "Fleet Summary":
        report_md = generate_agent_report(data, "markdown")
        st.caption(f"Full fleet analysis - {len(data['agents'])} agent(s)")
    elif report_type == "Heavy Collectors (>50% unused)":
        report_md = generate_heavy_collectors_report(data)
        heavy_count = sum(1 for a in data["agents"] if _is_heavy(a))
        st.caption(f"Collectors with >50% unused components - {heavy_count} found")
    else:
        version_options = get_collector_versions()
        threshold_version = st.selectbox(
            "Minimum version",
            version_options,
            index=0
        )
        report_md = generate_outdated_collectors_report(data, threshold_version)
        collectors_count, components_count = _count_outdated_collectors(data["agents"], threshold_version)
        st.caption(f"{collectors_count} collector{'s' if collectors_count != 1 else ''} with components below v{threshold_version} ({components_count} component{'s' if components_count != 1 else ''} found)")
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Download Markdown",
            report_md,
            file_name=f"{report_type.lower().replace(' ', '-')}.md",
            mime="text/markdown",
            key="download_report_md"
        )
    
    with col2:
        st.download_button(
            "Download CSV",
            report_md,
            file_name=f"{report_type.lower().replace(' ', '-')}.csv",
            mime="text/csv",
            key="download_report_csv"
        )
else:
    st.info("No agents connected. Connect agents to generate reports.")
