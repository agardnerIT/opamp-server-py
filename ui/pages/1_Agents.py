import os
import sys
import streamlit as st
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.shared import (
    SERVER_URL,
    format_local_time,
    get_agents,
    get_agent,
    _compliance_badge,
    render_sidebar,
    show_slim_distro_builder_page,
    generate_manifest,
    generate_ocb_command,
    get_auth_headers,
    json_module,
    yaml,
    base64,
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
    else:
        agent = fleet_agent
        health = agent.get("healthy") if agent else None
        description = agent.get("description") if agent else {}
        capability_tags = agent.get("capability_tags") if agent else []
        comps = agent.get("components") if agent else {}
        last_heartbeat = agent.get("last_heartbeat") if agent else "N/A"
        effective_config = None
    
    health_color = "green" if health else "red" if health is False else "gray"
    health_text = "Healthy" if health else "Unhealthy" if health is False else "Unknown"
    health_icon = "✅" if health else "❌" if health is False else "⚪"
    
    st.markdown("### Agent Details")
    
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
            try:
                config_data = json_module.loads(effective_config)
                raw_body = config_data.get("configMap", {}).get("configMap", {}).get("", {}).get("body", "")
                if isinstance(raw_body, str):
                    try:
                        yaml_body = base64.b64decode(raw_body).decode("utf-8")
                    except Exception:
                        yaml_body = raw_body
                elif isinstance(raw_body, bytes):
                    yaml_body = raw_body.decode("utf-8")
                else:
                    yaml_body = str(raw_body)
                
                if yaml_body:
                    config_yaml = yaml.safe_load(yaml_body)
                    if config_yaml is None:
                        config_yaml = {}
                    
                    def clean_config(d):
                        if isinstance(d, dict):
                            result = {}
                            for k, v in d.items():
                                if v is None:
                                    continue
                                if isinstance(v, str) and v == '':
                                    continue
                                if isinstance(v, dict) and v == {}:
                                    continue
                                result[k] = clean_config(v)
                            return result
                        elif isinstance(d, list):
                            return [clean_config(i) for i in d if i is not None and i != {}]
                        return d
                    
                    config_yaml = clean_config(config_yaml)
                    
                    if "extensions" in config_yaml and "opamp" in config_yaml["extensions"]:
                        if "server" not in config_yaml["extensions"]["opamp"]:
                            config_yaml["extensions"]["opamp"]["server"] = {}
                        if "http" not in config_yaml["extensions"]["opamp"]["server"]:
                            config_yaml["extensions"]["opamp"]["server"]["http"] = {}
                        if "endpoint" not in config_yaml["extensions"]["opamp"]["server"]["http"]:
                            config_yaml["extensions"]["opamp"]["server"]["http"]["endpoint"] = "http://127.0.0.1:4320/v1/opamp"
                    
                    if "exporters" in config_yaml and "debug" in config_yaml["exporters"]:
                        if config_yaml["exporters"]["debug"].get("use_internal_logger"):
                            config_yaml["exporters"]["debug"].pop("output_paths", None)
                    
                    if "receivers" not in config_yaml:
                        config_yaml["receivers"] = {"otlp": {"protocols": {"grpc": {}, "http": {}}}}
                    if "processors" not in config_yaml:
                        config_yaml["processors"] = {"batch": {}}
                    if "exporters" not in config_yaml:
                        config_yaml["exporters"] = {"debug": {}}
                    
                    existing_pipelines = config_yaml.get("service", {}).get("pipelines", {})
                    existing_pipelines.setdefault("traces", {
                        "receivers": ["otlp"],
                        "processors": ["batch"],
                        "exporters": ["debug"]
                    })
                    
                    config_yaml["service"] = {
                        "extensions": ["opamp"],
                        "pipelines": existing_pipelines,
                        "telemetry": {"metrics": {
                            "level": "basic",
                            "readers": [{
                                "periodic": {
                                    "exporter": {
                                        "otlp": {
                                            "protocol": "http/protobuf",
                                            "endpoint": f"http://{SERVER_URL.split('://')[1] if '://' in SERVER_URL else SERVER_URL}/v1/metrics"
                                        }
                                    }
                                }
                            }]
                        }}
                    }
                    
                    new_yaml = yaml.dump(config_yaml, default_flow_style=False, sort_keys=False)
                    
                    col_btn, col_exp = st.columns([1, 2])

                    with col_btn:
                        st.download_button(
                            "Download Config with Telemetry",
                            new_yaml,
                            file_name="collector-config.yaml",
                            mime="text/yaml",
                            key="download_telemetry_config"
                        )
                    with col_exp:
                        with st.expander("Show config"):
                            st.code(new_yaml, language="yaml")
                else:
                    st.caption("Config body is empty")
            except Exception as e:
                st.caption(f"Could not parse config: {type(e).__name__}")
    
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
        has_password = bool(st.session_state.get("admin_password"))
        
        if not has_password:
            st.warning("🔒 Admin password required to check compliance")
            
            if "compliance_auth_attempt" not in st.session_state:
                st.session_state["compliance_auth_attempt"] = 0
            form_key = f"compliance_auth_{selected_id[:8]}_{st.session_state['compliance_auth_attempt']}"
            
            with st.form(key=form_key):
                if st.session_state.get("admin_password_error"):
                    st.error("Invalid password. Try again.")
                    st.session_state["admin_password_error"] = False
                password = st.text_input("Password", type="password", key=f"compliance_pwd_{selected_id[:8]}")
                col1, col2 = st.columns([1, 1])
                with col1:
                    submit = st.form_submit_button("Submit", type="primary")
                with col2:
                    skip = st.form_submit_button("Skip")
                
                if submit and password:
                    test_encoded = base64.b64encode(f":{password}".encode()).decode()
                    test_headers = {"Authorization": f"Basic {test_encoded}"}
                    try:
                        test_resp = requests.get(f"{SERVER_URL}/auth/verify", headers=test_headers, timeout=5)
                        if test_resp.status_code == 200:
                            st.session_state["admin_password"] = password
                            st.rerun()
                        elif test_resp.status_code == 401:
                            st.session_state["compliance_auth_attempt"] += 1
                            st.session_state["admin_password_error"] = True
                            st.rerun()
                        else:
                            st.error(f"Server error: {test_resp.status_code}")
                    except Exception as e:
                        st.error(f"Failed to verify password: {e}")
                elif submit and not password:
                    st.error("Please enter a password")
        else:
            if st.button("🔍 Check Compliance", key=f"check_compliance_{selected_id[:8]}_btn", type="primary"):
                with st.spinner("Evaluating compliance..."):
                    try:
                        headers = get_auth_headers()
                        resp = requests.post(f"{SERVER_URL}/compliance/check/{selected_id}", headers=headers, timeout=30)
                        if resp.status_code == 200:
                            st.success("Compliance check completed!")
                            st.session_state.metrics_refresh = st.session_state.get("metrics_refresh", {})
                            st.session_state.metrics_refresh[selected_id] = True
                            st.rerun()
                        elif resp.status_code == 401:
                            st.session_state.pop("admin_password", None)
                            st.session_state["compliance_auth_attempt"] = st.session_state.get("compliance_auth_attempt", 0) + 1
                            st.session_state["admin_password_error"] = True
                            st.rerun()
                        else:
                            st.error(f"Compliance check failed: {resp.text}")
                    except Exception as e:
                        st.error(f"Failed to check compliance: {e}")
        
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
        try:
            config_data = json_module.loads(effective_config)
            raw_body = config_data.get("configMap", {}).get("configMap", {}).get("", {}).get("body", "")
            if isinstance(raw_body, str):
                try:
                    yaml_body = base64.b64decode(raw_body).decode("utf-8")
                except Exception:
                    yaml_body = raw_body
            elif isinstance(raw_body, bytes):
                yaml_body = raw_body.decode("utf-8")
            else:
                yaml_body = str(raw_body)
            
            if yaml_body:
                config_yaml = yaml.safe_load(yaml_body)
                if config_yaml is None:
                    config_yaml = {}
                
                def clean_config(d):
                    if isinstance(d, dict):
                        result = {}
                        for k, v in d.items():
                            if v is None:
                                continue
                            if isinstance(v, str) and v == '':
                                continue
                            if isinstance(v, dict) and v == {}:
                                continue
                            result[k] = clean_config(v)
                        return result
                    elif isinstance(d, list):
                        return [clean_config(i) for i in d if i is not None and i != {}]
                    return d
                
                config_yaml = clean_config(config_yaml)
                
                if "extensions" in config_yaml and "opamp" in config_yaml["extensions"]:
                    if "server" not in config_yaml["extensions"]["opamp"]:
                        config_yaml["extensions"]["opamp"]["server"] = {}
                    if "http" not in config_yaml["extensions"]["opamp"]["server"]:
                        config_yaml["extensions"]["opamp"]["server"]["http"] = {}
                    if "endpoint" not in config_yaml["extensions"]["opamp"]["server"]["http"]:
                        config_yaml["extensions"]["opamp"]["server"]["http"]["endpoint"] = "http://127.0.0.1:4320/v1/opamp"
                
                if "exporters" in config_yaml and "debug" in config_yaml["exporters"]:
                    if config_yaml["exporters"]["debug"].get("use_internal_logger"):
                        config_yaml["exporters"]["debug"].pop("output_paths", None)
                
                if "receivers" not in config_yaml:
                    config_yaml["receivers"] = {"otlp": {"protocols": {"grpc": {}, "http": {}}}}
                if "processors" not in config_yaml:
                    config_yaml["processors"] = {"batch": {}}
                if "exporters" not in config_yaml:
                    config_yaml["exporters"] = {"debug": {}}
                
                existing_pipelines = config_yaml.get("service", {}).get("pipelines", {})
                existing_pipelines.setdefault("traces", {
                    "receivers": ["otlp"],
                    "processors": ["batch"],
                    "exporters": ["debug"]
                })
                
                config_yaml["service"] = {
                    "extensions": ["opamp"],
                    "pipelines": existing_pipelines,
                    "telemetry": {"metrics": {
                        "level": "basic",
                        "readers": [{
                            "periodic": {
                                "exporter": {
                                    "otlp": {
                                        "protocol": "http/protobuf",
                                        "endpoint": f"http://{SERVER_URL.split('://')[1] if '://' in SERVER_URL else SERVER_URL}/v1/metrics"
                                    }
                                }
                            }
                        }]
                    }}
                }
                
                cleaned_yaml = yaml.dump(config_yaml, default_flow_style=False, sort_keys=False)
                
                st.download_button(
                    "Download Config",
                    cleaned_yaml,
                    file_name="collector-config.yaml",
                    mime="text/yaml",
                    key=f"download_config_{selected_id[:8] if selected_id else 'none'}"
                )
                st.code(cleaned_yaml, language="yaml", height=400)
            else:
                st.caption("Failed to parse configuration")
        except Exception as e:
            st.caption(f"Error: {e}")
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
