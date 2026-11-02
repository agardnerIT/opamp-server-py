import os
import sys
import streamlit as st
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.shared import (
    ROLE_OPERATOR,
    SERVER_URL,
    format_local_time,
    get_agents,
    get_agent,
    _compliance_badge,
    admin_request,
    render_sidebar,
    require_admin,
    reissue_banner_text,
    show_slim_distro_builder_page,
    display_config_yaml,
    generate_manifest,
    generate_ocb_command,
    get_session,
    datetime,
    requests,
    pd,
)

st.set_page_config(
    page_title="Agents - OpAMP Server Dashboard",
    page_icon="📡",
    layout="wide"
)

render_sidebar()

agent_id_param = st.query_params.get("agent_id")

if agent_id_param:
    selected_id = agent_id_param
    
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        health_data = health_resp.json() if health_resp.status_code == 200 else {}
        opa_available = health_data.get("opa_enabled", False)
    except Exception:
        opa_available = False
    
    with st.sidebar:
        if st.button("← Back to Agents", key="back_to_agents", use_container_width=True):
            if "agent_id" in st.query_params:
                del st.query_params["agent_id"]
            st.rerun()
    
    data = get_agents()
    agents = data.get("agents", [])
    
    fleet_agent = next((a for a in agents if a.get("id") == selected_id), None)
    detailed_agent = get_agent(selected_id)
    
    if "error" not in detailed_agent:
        health = detailed_agent.get("healthy") or (fleet_agent.get("healthy") if fleet_agent else detailed_agent.get("healthy"))
        description = detailed_agent.get("description") or (fleet_agent.get("description") if fleet_agent else detailed_agent.get("description"))
        capability_tags = detailed_agent.get("capability_tags") or (fleet_agent.get("capability_tags") if fleet_agent else detailed_agent.get("capability_tags"))
        comps = detailed_agent.get("components") or (fleet_agent.get("components") if fleet_agent else detailed_agent.get("components"))
        last_heartbeat = detailed_agent.get("last_heartbeat") or (fleet_agent.get("last_heartbeat") if fleet_agent else detailed_agent.get("last_heartbeat"))
        effective_config = detailed_agent.get("effective_config")
        # #75: present only when the server renamed this agent because its uid was a
        # duplicate, so the page can explain an id change instead of it looking random.
        uid_reissued_from = detailed_agent.get("uid_reissued_from") or (fleet_agent.get("uid_reissued_from") if fleet_agent else None)
        uid_reissue_reason = detailed_agent.get("uid_reissue_reason") or (fleet_agent.get("uid_reissue_reason") if fleet_agent else None)
    else:
        agent = fleet_agent
        health = agent.get("healthy") if agent else None
        description = agent.get("description") if agent else {}
        capability_tags = agent.get("capability_tags") if agent else []
        comps = agent.get("components") if agent else {}
        last_heartbeat = agent.get("last_heartbeat") if agent else "N/A"
        effective_config = None
        uid_reissued_from = agent.get("uid_reissued_from") if agent else None
        uid_reissue_reason = agent.get("uid_reissue_reason") if agent else None
    
    health_color = "green" if health else "red" if health is False else "gray"
    health_text = "Healthy" if health else "Unhealthy" if health is False else "Unknown"
    health_icon = "✅" if health else "❌" if health is False else "⚪"
    
    st.markdown("### Agent Details")

    # #75: two agents presented one instance_uid, so the server moved this one to a
    # uid of its own. Worth a banner: an id change is a fleet anomaly an operator has
    # to be able to explain without grepping logs. Wording is in ui/shared.py.
    banner = reissue_banner_text(uid_reissued_from, uid_reissue_reason)
    if banner:
        st.warning(banner)
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Status", health_text)
        st.divider()
        st.markdown("**Agent ID**")
        st.code(selected_id[:32] + "..." if len(selected_id) > 32 else selected_id, height=60)
        st.markdown("**Last Heartbeat**")
        st.caption(format_local_time(last_heartbeat))
    
    with col2:
        comps = comps or {}
        total = sum(len(c) for c in comps.values()) if comps else 0
        used = sum(1 for cl in comps.values() for c in cl if c.get("used")) if comps else 0
        st.metric("Components", f"{used}/{total} in use")
        st.divider()
        st.markdown("**Attributes**")
        desc = description or {}
        if desc:
            attrs = desc.get("identifyingAttributes", []) + desc.get("nonIdentifyingAttributes", [])
            for attr in attrs:
                key = attr.get("key", "")
                value = list(attr.get("value", {}).values())[0] if attr.get("value") else "N/A"
                st.markdown(f"**{key}:** {value}")
        else:
            st.caption("No attributes")

    with col3:
        tags = capability_tags or []
        st.metric("Capabilities", len(tags))
        st.divider()
        st.markdown("**Capabilities**")
        if tags:
            for tag in tags:
                st.markdown(f"{tag.get('icon', '•')} {tag.get('label', 'Unknown')}")
        else:
            st.caption("No capabilities")

    with col4:
        compliance = fleet_agent.get("compliance") if fleet_agent else None
        comp_status = "Compliant" if compliance and compliance.get("compliant") is True else "Non-compliant" if compliance and compliance.get("compliant") is False else "Unknown"
        comp_color = "normal" if compliance and compliance.get("compliant") is True else "inverse" if compliance and compliance.get("compliant") is False else "off"
        st.metric("Compliance", comp_status, delta_color=comp_color)
        st.divider()
        st.markdown("**Violations**")
        if compliance and compliance.get("violations"):
            for v in compliance.get("violations", []):
                st.caption(f"⚠️ {v}")
        else:
            st.caption("None")
    
    st.divider()
    
    if "metrics_refresh" not in st.session_state:
        st.session_state.metrics_refresh = {}
    
    if st.session_state.metrics_refresh.get(selected_id):
        detailed_agent = get_agent(selected_id)
        st.session_state.metrics_refresh[selected_id] = False
    
    col_refresh, _ = st.columns([1, 5])
    with col_refresh:
        if st.button("↻ Refresh", key=f"refresh_metrics_{selected_id[:8]}"):
            st.session_state.metrics_refresh[selected_id] = True
            st.rerun()
    
    metrics_data = detailed_agent.get("metrics", {})
    if metrics_data and metrics_data.get("metrics"):
        m = metrics_data.get("metrics", {})
        
        st.markdown("**Collector Telemetry**")
        st.caption("Real-time metrics from the collector's internal telemetry. These show how data flows through the collector.")
        
        st.markdown("### Data Flow (Inputs)")
        st.caption("Data being received by the collector. Sudden drops may indicate connectivity issues.")
        
        cols = st.columns(4)
        with cols[0]:
            recv_logs = m.get("otelcol_receiver_accepted_log_records")
            st.metric("Logs In", f"{recv_logs:,.0f}" if recv_logs is not None else "-")
        with cols[1]:
            recv_spans = m.get("otelcol_receiver_accepted_spans")
            st.metric("Traces In", f"{recv_spans:,.0f}" if recv_spans is not None else "-")
        with cols[2]:
            recv_metrics = m.get("otelcol_receiver_accepted_metric_points")
            st.metric("Metrics In", f"{recv_metrics:,.0f}" if recv_metrics is not None else "-")
        with cols[3]:
            refused_logs = m.get("otelcol_receiver_refused_log_records")
            st.metric("Refused", f"{refused_logs:,.0f}" if refused_logs is not None else "-")
        
        failed_logs = m.get("otelcol_receiver_failed_log_records", 0) or 0
        if failed_logs > 0:
            st.error(f"⚠️ Failed: {failed_logs}")
        
        st.markdown("### Data Flow (Outputs)")
        st.caption("Data being sent to downstream systems. If this drops but inputs are normal, check exporter connectivity.")
        
        cols = st.columns(3)
        with cols[0]:
            sent_logs = m.get("otelcol_exporter_sent_log_records")
            st.metric("Logs Out", f"{sent_logs:,.0f}" if sent_logs is not None else "-")
        with cols[1]:
            sent_spans = m.get("otelcol_exporter_sent_spans")
            st.metric("Traces Out", f"{sent_spans:,.0f}" if sent_spans is not None else "-")
        with cols[2]:
            sent_metrics = m.get("otelcol_exporter_sent_metric_points")
            st.metric("Metrics Out", f"{sent_metrics:,.0f}" if sent_metrics is not None else "-")
        
        st.markdown("### Collector Health")
        st.caption("Resource usage of the collector process itself.")
        
        cols = st.columns(4)
        with cols[0]:
            cpu = m.get("otelcol_process_cpu_seconds")
            st.metric("CPU (sec)", f"{cpu:,.1f}" if cpu is not None else "-")
        with cols[1]:
            mem = m.get("otelcol_process_memory_rss")
            st.metric("Memory (MB)", f"{mem / 1024 / 1024:,.0f}" if mem is not None else "-")
        with cols[2]:
            heap = m.get("otelcol_process_runtime_heap_alloc_bytes")
            st.metric("Heap (MB)", f"{heap / 1024 / 1024:,.0f}" if heap is not None else "-")
        with cols[3]:
            uptime = m.get("otelcol_process_uptime")
            st.metric("Uptime (sec)", f"{uptime:,.0f}" if uptime is not None else "-")
        
        with st.expander("Show all raw metrics"):
            st.caption("All available internal metrics from the collector:")
            for k, v in sorted(m.items()):
                st.caption(f"**{k}:** {v}")
        
        st.caption(f"Last updated: {metrics_data.get('updated_at', '')}")
    else:
        st.caption("No telemetry data received.")
        
        if effective_config:
            new_yaml = display_config_yaml(effective_config)
            if new_yaml:
                col_btn, col_exp = st.columns([1, 2])

                with col_btn:
                    st.download_button(
                        "Download Config with Telemetry",
                        new_yaml,
                        file_name="collector-config.yaml",
                        mime="text/yaml",
                        key="download_telemetry_config",
                    )
                with col_exp:
                    with st.expander("Show config"):
                        st.code(new_yaml, language="yaml")
            else:
                st.caption("No config object in this agent's report is readable as YAML")
    
    st.divider()
    
    col1, col2 = st.columns([4, 1])
    with col1:
        st.markdown("**Components**")
    with col2:
        show_slim_distro_builder_inline = st.checkbox("Create Slim Distro", key="slim_distro_toggle")
    
    if show_slim_distro_builder_inline:
        show_slim_distro_builder_page(comps)
    
    COMPONENT_ORDER = ["receiver", "processor", "exporter", "extension", "connector"]
    
    all_components = []
    for comp_type in COMPONENT_ORDER:
        if comp_type in comps:
            for comp in comps[comp_type]:
                all_components.append({
                    "Type": comp_type.title(),
                    "Name": comp["id"],
                    "Version": comp.get("version", ""),
                    "In Use": "✅" if comp["used"] else "❌"
                })
    
    if all_components:
        show_in_use_only = st.toggle(
            "Show in-use components only",
            value=False,
            key=f"in_use_toggle_{selected_id[:8] if selected_id else 'none'}"
        )
        
        if show_in_use_only:
            df_comps = pd.DataFrame([c for c in all_components if c["In Use"] == "✅"])
        else:
            df_comps = pd.DataFrame(all_components)
        
        if not df_comps.empty:
            st.dataframe(df_comps, width='stretch', hide_index=True)
        else:
            st.write("No in-use components")
    else:
        st.write("No components reported")
    
    st.divider()
    
    if fleet_agent:
        compliance = fleet_agent.get("compliance")
    else:
        compliance = None
    st.markdown("**Compliance**")
    
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        health_data = health_resp.json() if health_resp.status_code == 200 else {}
        opa_enabled = health_data.get("opa_enabled", False)
    except Exception:
        opa_enabled = False
    
    if not opa_enabled:
        st.info("Compliance checking is disabled. Set `OPA_ENABLED=true` and configure `OPA_URL` to enable.")
    else:
        # #105: this page used to own a second copy of the login form, and held the
        # password in session state to build HTTP Basic headers. It now shares the one
        # implementation in `ui/shared.py` — the same form, the same token, the same
        # 401/403/429 handling as the Admin page.
        current = get_session()
        if current is None or not current.can(ROLE_OPERATOR):
            st.caption(
                "🔒 Checking compliance is audited and needs the `operator` role — "
                "you will be asked to sign in."
            )
        if st.button("🔍 Check Compliance", key=f"check_compliance_{selected_id[:8]}_btn", type="primary"):
            require_admin(ROLE_OPERATOR)  # renders sign-in and stops when needed
            with st.spinner("Evaluating compliance..."):
                resp = admin_request(
                    "POST", f"/compliance/check/{selected_id}",
                    required_role=ROLE_OPERATOR, timeout=30,
                )
                if resp is None:
                    st.error("Compliance check failed.")
                elif resp.status_code == 200:
                    st.success("Compliance check completed!")
                    st.session_state.metrics_refresh = st.session_state.get("metrics_refresh", {})
                    st.session_state.metrics_refresh[selected_id] = True
                    st.rerun()
                else:
                    st.error(f"Compliance check failed: {resp.text}")
        
        if compliance:
            policy_results = compliance.get("policy_results", [])
            if policy_results:
                df_policies = pd.DataFrame([
                    {
                        "Policy": p["name"],
                        "Status": "✅ Pass" if p["status"] == "pass" else "❌ Fail" if p["status"] == "fail" else "⚪ Unknown",
                        "Details": ", ".join(p["violations"]) if p["violations"] else "-",
                    }
                    for p in policy_results
                ])
                st.dataframe(df_policies, width='stretch', hide_index=True)
            else:
                if compliance.get("compliant") is True:
                    st.success("Agent is compliant with all policies")
                elif compliance.get("compliant") is False:
                    violations = compliance.get("violations", [])
                    if violations:
                        for v in violations:
                            st.error(f"Violation: {v}")
                    else:
                        st.error("Agent is not compliant")
                else:
                    st.warning("Compliance status unknown")
        else:
            st.caption("Compliance not yet evaluated. Click 'Check Compliance' to evaluate.")
    
    st.divider()
    
    col1, col2 = st.columns([4, 1])
    with col1:
        st.markdown("**Collector Configuration**")
    with col2:
        show_config = st.toggle("Show", value=False, key=f"show_config_{selected_id[:8] if selected_id else 'none'}")
    
    if show_config and effective_config:
        cleaned_yaml = display_config_yaml(effective_config)
        if cleaned_yaml:
            st.download_button(
                "Download Config",
                cleaned_yaml,
                file_name="collector-config.yaml",
                mime="text/yaml",
                key=f"download_config_{selected_id[:8] if selected_id else 'none'}",
            )
            st.code(cleaned_yaml, language="yaml", height=400)
        else:
            st.caption("No config object in this agent's report is readable as YAML")
    elif effective_config:
        st.caption("Toggle to show configuration")
    else:
        st.caption("No effective config available from this collector")

else:
    agents = st.session_state.get("agents_data", {}).get("agents", [])
    
    if agents:
        st.header("Agent List")
        st.caption(f"Showing {len(agents)} agent(s)")
    else:
        st.info("No agents connected")

    view_mode_options = ["Table", "By Property"]
    saved_view_mode = st.query_params.get("view_mode", "Table")
    default_index = view_mode_options.index(saved_view_mode) if saved_view_mode in view_mode_options else 0

    view_mode = st.radio(
        "View Mode",
        view_mode_options,
        horizontal=True,
        index=default_index,
        key="view_mode"
    )
    
    if view_mode != saved_view_mode:
        st.query_params["view_mode"] = view_mode
    
    if view_mode == "Table":
        rows = []
        for a in agents:
            link = f'<a href="/Agents?agent_id={a["id"]}" target="_self" style="color:#0066cc;text-decoration:underline;cursor:pointer">{a["id"][:32]}{"..." if len(a["id"]) > 32 else ""}</a>'
            rows.append({
                "Agent": link,
                "Healthy": "✅" if a.get("healthy") else "❌" if a.get("healthy") is False else "⚪",
                "Compliance": _compliance_badge(a.get("compliance")),
                "Last Heartbeat": format_local_time(a.get("last_heartbeat")),
            })
        df = pd.DataFrame(rows)
        st.markdown(df.to_html(escape=False, index=False), unsafe_allow_html=True)
    
    elif view_mode == "By Property":
        # Get all unique attribute keys from agents for dynamic grouping options
        attr_keys = set()
        for agent in agents:
            desc = agent.get("description", {})
            attrs = desc.get("identifyingAttributes", []) + desc.get("nonIdentifyingAttributes", [])
            for attr in attrs:
                key = attr.get("key", "")
                if key:
                    attr_keys.add(key)
        
        # Sort attribute keys and add to options
        attr_options = sorted(attr_keys)
        group_options = ["Healthy", "Compliance"] + attr_options
        
        # Get saved group_by from query params, default to "Healthy"
        saved_group_by = st.query_params.get("group_by", "Healthy")
        # Ensure saved value is valid
        if saved_group_by not in group_options:
            saved_group_by = "Healthy"
        
        # Initialize session state for the selectbox if not set or if query param changed externally
        if "group_by_select" not in st.session_state or st.session_state.group_by_select not in group_options:
            st.session_state.group_by_select = saved_group_by
        
        def on_group_change():
            # Update query params when selection changes
            st.query_params["group_by"] = st.session_state.group_by_select
        
        group_by = st.selectbox(
            "Group by",
            group_options,
            key="group_by_select",
            on_change=on_group_change
        )
        
        groups = {}
        for agent in agents:
            if group_by == "Healthy":
                key = "Healthy" if agent.get("healthy") else "Unhealthy" if agent.get("healthy") is False else "Unknown"
            elif group_by == "Compliance":
                compliance = agent.get("compliance")
                if compliance is None:
                    key = "Not Evaluated"
                elif compliance.get("opa_disabled"):
                    key = "OPA Disabled"
                elif compliance.get("compliant") is True:
                    key = "Compliant"
                elif compliance.get("compliant") is False:
                    key = "Non-compliant"
                else:
                    key = "Unknown"
            else:
                # Group by attribute value
                desc = agent.get("description", {})
                attrs = desc.get("identifyingAttributes", []) + desc.get("nonIdentifyingAttributes", [])
                key = "Not Set"
                for attr in attrs:
                    if attr.get("key") == group_by:
                        value = attr.get("value", {})
                        if value:
                            # Extract value from the oneOf structure
                            key = list(value.values())[0] if value else "Not Set"
                        break
            groups.setdefault(key, []).append(agent)
        
        # Sort groups alphabetically, but put "Not Set" and "Unknown" last
        def sort_key(item):
            name = item[0]
            if name in ("Not Set", "Unknown", "Not Evaluated"):
                return (1, name)
            return (0, name)
        
        for group_name, group_agents in sorted(groups.items(), key=sort_key):
            st.subheader(f"{group_name} ({len(group_agents)})")
            for agent in group_agents:
                health_icon = "✅" if agent.get("healthy") else "❌" if agent.get("healthy") is False else "⚪"
                st.markdown(f"**{health_icon}** [{agent['id'][:16]}...](?agent_id={agent['id']})")
    if not st.session_state.get("agents_data", {}).get("agents"):
        if "error" in st.session_state.get("agents_data", {}):
            st.error("Server offline — no agents")
        else:
            st.info("No agents connected. Start an OpenTelemetry Collector with OpAMP extension to see it here.")
