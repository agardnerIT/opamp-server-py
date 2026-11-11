import os
import sys
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.shared import (
    SERVER_URL,
    get_agents,
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
from server import profiles


render_sidebar()


#: How a report id (`server/profiles/schema.py:REPORTS`) is spelled on this page.
#: Neutral wording on purpose (#121): "Heavy Collectors" was a page literal that
#: described one profile's report as if it were a fact about every agent.
REPORT_LABELS = {
    "fleet": "Fleet Summary",
    "heavy": "Heavy agents (>50% unused components)",
    "outdated": "Outdated components",
}


def _offered_reports(agents):
    """Which reports describe *this* fleet: the union of what its profiles declare.

    Report generators are per-profile data (`reports:` in the dictionary), so a fleet
    of agents whose profiles declare none must not be offered a "Heavy agents" option
    that would report on a concept they do not have. `fleet` is always offered — it
    describes the server's own view, which exists for every profile.
    """
    offered = ["fleet"]
    for agent in agents:
        profile = profiles.get(agent.get("agent_type")) or profiles.FALLBACK
        for report in profile.reports:
            if report not in offered:
                offered.append(report)
    # Stable, readable order: fleet, heavy, outdated — not the order agents happened
    # to arrive in.
    return [name for name in REPORT_LABELS if name in offered]


data = get_agents()

if data.get("agents"):
    agents = data["agents"]
    offered = _offered_reports(agents)
    labels = {REPORT_LABELS[name]: name for name in offered}

    report_label = st.selectbox(
        "Report Type", list(labels), index=0
    )
    report_type = labels[report_label]

    if report_type == "fleet":
        report_md = generate_agent_report(data, "markdown")
        st.caption(f"Full fleet analysis - {len(agents)} agent(s)")
    elif report_type == "heavy":
        report_md = generate_heavy_collectors_report(data)
        heavy_count = sum(1 for a in agents if _is_heavy(a))
        st.caption(f"Agents with >50% unused components - {heavy_count} found")
    else:
        # The version feed is the collector releases' — it belongs to the report that
        # reads component versions, not to this page, which is why the dropdown only
        # appears when a profile in the fleet asks for that report (#121).
        version_options = get_collector_versions()
        threshold_version = st.selectbox(
            "Minimum version",
            version_options,
            index=0
        )
        report_md = generate_outdated_collectors_report(data, threshold_version)
        outdated_count, components_count = _count_outdated_collectors(agents, threshold_version)
        st.caption(
            f"{outdated_count} agent{'s' if outdated_count != 1 else ''} with components "
            f"below v{threshold_version} ({components_count} component"
            f"{'s' if components_count != 1 else ''} found)"
        )

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Download Markdown",
            report_md,
            file_name=f"{report_label.lower().replace(' ', '-')}.md",
            mime="text/markdown",
            key="download_report_md"
        )

    with col2:
        st.download_button(
            "Download CSV",
            report_md,
            file_name=f"{report_label.lower().replace(' ', '-')}.csv",
            mime="text/csv",
            key="download_report_csv"
        )
else:
    st.info("No agents connected. Connect agents to generate reports.")
