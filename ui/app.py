import os
import sys
import json as json_module
import yaml
import base64
from pathlib import Path
import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone


def format_local_time(utc_str):
    if not utc_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str[:19] if len(utc_str) > 19 else utc_str

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.manifest import generate_manifest, generate_ocb_command


st.set_page_config(
    page_title="OpAMP Server Dashboard",
    layout="wide"
)

SERVER_HTTP_SCHEME = os.environ.get("SERVER_HTTP_SCHEME", "http")
SERVER_ADDRESS = os.environ.get("SERVER_ADDRESS", "localhost")
SERVER_PORT = os.environ.get("SERVER_PORT", "4320")
SERVER_URL = f"{SERVER_HTTP_SCHEME}://{SERVER_ADDRESS}:{SERVER_PORT}"


def get_server_url():
    return SERVER_URL


def get_auth_status():
    try:
        resp = requests.get(f"{SERVER_URL}/auth/status", timeout=5)
        return resp.json()
    except Exception:
        return {"password_required": False}


def get_auth_headers():
    if "admin_password_input" in st.session_state and st.session_state["admin_password_input"]:
        password = st.session_state["admin_password_input"]
        encoded = base64.b64encode(f":{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {}


def prompt_for_password(password_key, attempt_key, page_suffix=""):
    """Show password prompt. Returns password if valid, None otherwise."""
    if get_auth_status().get("password_required"):
        if password_key not in st.session_state:
            if attempt_key not in st.session_state:
                st.session_state[attempt_key] = 0
            show_form = True
            form_key = f"{password_key}_form_{page_suffix}_{st.session_state[attempt_key]}"
        else:
            show_form = False
            form_key = None
        
        if show_form:
            with st.form(form_key):
                st.markdown("**🔒 Admin Password Required**")
                if st.session_state.get("admin_password_error"):
                    st.error("Invalid password. Try again.")
                    st.session_state["admin_password_error"] = False
                password = st.text_input("Password", type="password", key=f"{password_key}_input_{page_suffix}")
                submitted = st.form_submit_button("Submit")
                
                if submitted and password:
                    st.session_state[password_key] = password
                    st.rerun()
                elif submitted and not password:
                    st.session_state[attempt_key] += 1
                    st.error("Password required")
                    st.rerun()
                return None
        else:
            return st.session_state.get(password_key)
    return None


def prompt_admin_password():
    """Show a modal to get admin password. Returns headers dict."""
    if not get_auth_status().get("password_required"):
        return {}
    
    with st.form("admin_password_form"):
        st.markdown("**🔒 Admin Password Required**")
        password = st.text_input("Password", type="password", key="admin_password_modal")
        submitted = st.form_submit_button("Submit")
        
        if submitted and password:
            return get_auth_headers()
    
    return {}


def get_agents():
    try:
        response = requests.get(f"{SERVER_URL}/agents", timeout=5)
        return response.json()
    except Exception as e:
        return {"agents": [], "count": 0, "error": str(e)}


def get_agent(agent_id):
    try:
        response = requests.get(f"{SERVER_URL}/agent/{agent_id}", timeout=5)
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def render_sidebar(data: dict):
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        health_data = health_resp.json() if health_resp.status_code == 200 else {}
        opa_available = health_data.get("opa_enabled", False)
    except Exception:
        opa_available = False
    
    with st.sidebar:
        st.markdown("**Server Status**")
        status = "🟢 Online" if "error" not in data else "🔴 Offline"
        st.caption(status)
        
        if opa_available:
            st.markdown("**Open Policy Agent Status**")
            st.caption("🟢 Available")
        
        st.markdown("**Agents Connected**")
        st.caption(f"{data.get('count', 0)}")


def show_setup_help_page():
    server_url = get_server_url()
    
    st.markdown("## Collector Configuration")
    st.markdown("Add this minimal config to your OpenTelemetry Collector to connect to this OpAMP server:")
    
    default_yaml = f"""extensions:
  opamp:
    server:
      http:
        endpoint: {server_url}/v1/opamp
    agent_description:
      non_identifying_attributes:
        "deployment.environment": "development"
    capabilities:
      reports_health: true
      reports_effective_config: true
      reports_available_components: true
service:
  extensions: [opamp]
"""
    
    st.code(default_yaml, language="yaml")
    
    st.download_button(
        "Download config.yaml",
        default_yaml,
        file_name="config.yaml",
        mime="text/yaml",
        key="download_config"
    )
    
    st.divider()
    
    st.markdown("### Agent Description")
    st.markdown("""
    The OpAMP extension supports both **identifying** and **non-identifying** attributes:
    
    - **Identifying attributes** uniquely identify the agent (e.g., `agent.id`, `host.name`)
    - **Non-identifying attributes** provide additional context (e.g., `environment`, `service.name`)
    
    These are used for filtering and grouping in the Agents view.
    """)
    
    st.divider()
    
    st.markdown("### Capabilities")
    st.markdown("""
    The OpAMP extension can report:
    
    | Capability | Description |
    |------------|-------------|
    | `reports_health` | Send periodic health status |
    | `reports_effective_config` | Report current collector configuration |
    | `reports_available_components` | Report which components (receivers, processors, exporters) are in use |
    """)
    
    st.divider()
    
    st.markdown("### Advanced: Custom Capabilities")
    st.markdown("""
    You can enable additional capabilities:
    
    ```yaml
    capabilities:
      reports_health: true
      reports_effective_config: true
      reports_available_components: true
      accepts_remote_config: true
      accepts_packages: true
    ```
    """)


def show_feedback_dialog():
    st.session_state.feedback_submitted = False
    st.session_state.feedback_error = None
    st.session_state.show_slim_distro_builder = False
    st.session_state.show_policies_modal = False
    
    @st.dialog("Feedback")
    def dialog_content():
        st.markdown("We'd love to hear your feedback!")
        title = st.text_input("Title", placeholder="Brief description...", key="feedback_title")
        feedback = st.text_area("Your feedback", height=150, key="feedback_text")

        if st.session_state.get("feedback_error") == "title":
            st.warning("Please enter a title first.")
        elif st.session_state.get("feedback_error") == "content":
            st.warning("Please enter some feedback first.")

        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Submit", type="primary", key="submit_feedback"):
                if title.strip() and feedback.strip():
                    ntfy_url = "https://ntfy.sh/agardnerit-opamp-server-py-feedback"
                    message = f"# {title}\n\n**Feedback:**\n{feedback}"
                    try:
                        requests.post(ntfy_url, data=message.encode("utf-8"), headers={"Content-Type": "text/plain"})
                        st.session_state.feedback_submitted = True
                    except Exception as e:
                        st.error(f"Failed to send feedback: {e}")
                        st.rerun()
                elif not title.strip():
                    st.session_state.feedback_error = "title"
                    st.rerun()
                else:
                    st.session_state.feedback_error = "content"
                    st.rerun()

        with col2:
            if st.button("Close", key="close_feedback"):
                st.session_state.show_feedback = False
                st.session_state.feedback_submitted = False
                st.rerun()

        if st.session_state.get("feedback_submitted"):
            st.success("Thank you for your feedback!")
            if st.button("Done", key="dismiss_feedback"):
                st.session_state.show_feedback = False
                st.session_state.feedback_submitted = False
                st.rerun()

    dialog_content()


def show_admin_page():
    st.markdown("**Admin Panel**")
    st.caption("Sensitive operations that require authentication.")
    st.divider()

    password = None
    if get_auth_status().get("password_required"):
        if "admin_password" not in st.session_state:
            if "admin_password_attempt" not in st.session_state:
                st.session_state["admin_password_attempt"] = 0
            show_form = True
            form_key = f"admin_password_form_{st.session_state['admin_password_attempt']}"
            with st.form(form_key):
                st.markdown("**🔒 Admin Password Required**")
                if st.session_state.get("admin_password_error"):
                    st.error("Invalid password. Try again.")
                    st.session_state["admin_password_error"] = False
                password = st.text_input("Password", type="password", key="admin_password_input_main")
                submitted = st.form_submit_button("Submit")

                if submitted and password:
                    st.session_state["admin_password"] = password
                    st.rerun()
                elif submitted and not password:
                    st.session_state["admin_password_attempt"] += 1
                    st.error("Password required")
                    st.rerun()
                return
        else:
            password = st.session_state.get("admin_password")

    if password:
        encoded = base64.b64encode(f":{password}".encode()).decode()
        headers = {"Authorization": f"Basic {encoded}"}
    else:
        return

    admin_tabs = st.tabs(["Alerts", "Compliance"])

    with admin_tabs[0]:
        show_alerts_content(headers)

    with admin_tabs[1]:
        show_compliance_content(headers)


def show_alerts_content(headers):
    try:
        resp = requests.get(f"{SERVER_URL}/alerts", timeout=5, headers=headers)
        if resp.status_code == 401:
            st.session_state.pop("admin_password", None)
            st.session_state["admin_password_attempt"] = st.session_state.get("admin_password_attempt", 0) + 1
            st.session_state["admin_password_error"] = True
            st.rerun()
            return
        alert_data = resp.json()
    except Exception as e:
        st.error(f"Failed to load alerts config: {e}")
        return

    config = alert_data.get("config", {})
    types = alert_data.get("types", [])
    events = alert_data.get("events", [])

    event_tabs = st.tabs([e.replace("_", " ").title() for e in events])

    event_configs = {}

    for idx, event in enumerate(events):
        with event_tabs[idx]:
            event_config = config.get("events", {}).get(event, {})

            event_enabled = st.checkbox("Enable", value=event_config.get("enabled", False), key=f"enabled_{event}")
            webhook_url = st.text_input("Webhook URL", value=event_config.get("webhook_url", ""), type="default", key=f"url_{event}")

            default_headers = json_module.dumps({"Content-Type": "application/cloudevents+json; charset=UTF-8"}, indent=2)
            headers_json = st.text_area(
                "Headers (JSON)",
                value=event_config.get("headers") or default_headers,
                key=f"headers_{event}",
                help="Request headers as JSON. Content-Type defaults to application/cloudevents+json; charset=UTF-8"
            )
            body_default = json_module.dumps({"specversion":"1.0","type":"io.opentelemetry.opamp.agent.{event_type}","source":"opamp-server","id":"{id}","time":"{time}","datacontenttype":"application/json","data":{"message":"{message}"}}, indent=2)
            st.caption("Available placeholders: **{event_type}**, **{message}**, **{id}** (auto-generated UUID), **{time}** (auto-generated UTC timestamp)")
            body_template = st.text_area(
                "Body Template",
                value=event_config.get("body_template") or body_default,
                key=f"body_{event}",
                height=200,
            )

            col1, col2 = st.columns(2)
            with col1:
                if st.button(f"Test {event}", key=f"test_{event}"):
                    test_event_config = {
                        "enabled": event_enabled,
                        "webhook_url": webhook_url,
                        "headers": headers_json,
                        "body_template": body_template,
                    }
                    save_payload = {"events": {event: test_event_config}}
                    requests.put(f"{SERVER_URL}/alerts", json=save_payload, timeout=5, headers=headers)
                    test_resp = requests.post(f"{SERVER_URL}/alerts/test", json={"event_type": event}, timeout=10, headers=headers)
                    try:
                        result = test_resp.json()
                        if test_resp.status_code == 401:
                            st.session_state.pop("admin_password", None)
                            st.session_state["admin_password_attempt"] = st.session_state.get("admin_password_attempt", 0) + 1
                            st.session_state["admin_password_error"] = True
                            st.rerun()
                        elif result.get("success"):
                            st.success("Test sent!")
                        else:
                            st.error(f"Failed: {result.get('error')}")
                    except:
                        st.success("Test sent! (server received request)")

            event_configs[event] = {
                "enabled": event_enabled,
                "webhook_url": webhook_url,
                "headers": headers_json,
                "body_template": body_template,
            }

    if st.button("Save & Apply", type="primary", key="save_alerts"):
        new_config = {
            "events": event_configs,
        }

        resp = requests.put(f"{SERVER_URL}/alerts", json=new_config, timeout=5, headers=headers)
        if resp.status_code == 401:
            st.session_state.pop("admin_password", None)
            st.session_state["admin_password_attempt"] = st.session_state.get("admin_password_attempt", 0) + 1
            st.session_state["admin_password_error"] = True
            st.rerun()
        elif resp.status_code == 200:
            st.success("Saved!")
        else:
            st.error("Failed to save")


def show_compliance_content(headers):
    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("Reload & Validate", key="admin_reload_validate"):
            try:
                resp = requests.post(f"{SERVER_URL}/compliance/reload", timeout=10, headers=headers)
                if resp.status_code == 401:
                    st.session_state.pop("admin_password", None)
                    st.session_state["admin_password_attempt"] = st.session_state.get("admin_password_attempt", 0) + 1
                    st.session_state["admin_password_error"] = True
                    st.rerun()
                elif resp.status_code == 200:
                    st.success("Policies reloaded!")
            except Exception as e:
                st.error(f"Error: {e}")
            st.rerun()

    try:
        resp = requests.get(f"{SERVER_URL}/compliance/validate", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            validation = data.get("policies", [])

            valid = [v for v in validation if v.get("valid")]
            invalid = [v for v in validation if not v.get("valid")]

            if valid:
                st.success(f"✅ {len(valid)} valid policy file(s)")
                st.markdown(f"**{len(valid)} policy rule(s)**")
                df_policies = pd.DataFrame([
                    {"Policy": p["name"], "Description": p.get("description", "-")}
                    for p in valid
                ])
                st.dataframe(df_policies, width='stretch', hide_index=True, use_container_width=True)

            if invalid:
                st.error(f"❌ {len(invalid)} invalid policy file(s)")
                for v in invalid:
                    st.markdown(f"**{v['filename']}**")
                    for err in v.get("errors", []):
                        st.caption(f"  • {err}")

            if not validation:
                st.info("No .rego files found in policies/tags/")
        else:
            st.error("Failed to load policies")
    except Exception as e:
        st.error(f"Error: {e}")

    st.divider()
    st.markdown("**Check Agent Compliance**")

    data = get_agents()
    agents = data.get("agents", [])
    if agents:
        selected_ids = st.multiselect(
            "Select agents",
            options=[a["id"] for a in agents],
            format_func=lambda x: x[:16] + "...",
            label_visibility="collapsed",
            key="admin_compliance_select"
        )
        if st.button("Check Compliance", type="primary", key="admin_check_compliance"):
            if selected_ids:
                results = []
                for agent_id in selected_ids:
                    try:
                        resp = requests.get(f"{SERVER_URL}/agent/{agent_id}/compliance", timeout=30, headers=headers)
                        if resp.status_code == 401:
                            st.session_state.pop("admin_password", None)
                            st.session_state["admin_password_attempt"] = st.session_state.get("admin_password_attempt", 0) + 1
                            st.session_state["admin_password_error"] = True
                            st.rerun()
                            break
                        elif resp.status_code == 200:
                            result = resp.json()
                            results.append((agent_id, result))
                    except Exception as e:
                        results.append((agent_id, {"error": str(e)}))
                
                if results:
                    for agent_id, result in results:
                        if "error" in result:
                            st.error(f"**{agent_id[:16]}**: {result['error']}")
                        elif result.get("compliant"):
                            st.success(f"**{agent_id[:16]}**: ✅ Compliant")
                        else:
                            violations = result.get("violations", [])
                            st.error(f"**{agent_id[:16]}**: ❌ {len(violations)} violation(s)")
                            for v in violations:
                                st.caption(f"  • {v}")
                    st.session_state["last_compliance_check"] = results
            else:
                st.warning("Select agents first")
    else:
        st.info("No agents connected")


def show_alerts_page():
    st.info("Alerts management has moved to the **Admin** tab.")


def generate_agent_report(data: dict, format: str = "markdown") -> str:
    agents = data.get("agents", [])
    
    if format == "markdown":
        lines = ["# Agent Report\n"]
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        lines.append(f"Total Agents: {len(agents)}\n\n")
        
        if agents:
            lines.append("## Component Versions\n")
            all_versions = {}
            for agent in agents:
                comps = agent.get("components", {})
                for comp_type, comp_list in comps.items():
                    for comp in comp_list:
                        vid = comp.get("version", "unknown")
                        if vid not in all_versions:
                            all_versions[vid] = 0
                        all_versions[vid] += 1
            
            for version, count in sorted(all_versions.items()):
                lines.append(f"- **{version}**: {count} components\n")
            
            lines.append("\n## Outdated Collectors\n")
            current_version = "0.149.0"
            outdated = []
            for agent in agents:
                comps = agent.get("components", {})
                versions = set()
                for comp_list in comps.values():
                    for comp in comp_list:
                        vid = comp.get("version", "")
                        if vid:
                            major = vid.split(".")[0] if "." in vid else vid
                            if major.isdigit():
                                versions.add((int(major), vid))
                for major, vid in versions:
                    if major < int(current_version.split(".")[0]):
                        outdated.append((agent.get("id", "")[:16], vid))
                        break
            
            if outdated:
                for aid, ver in outdated:
                    lines.append(f"- {aid}: {ver}")
            else:
                lines.append("All collectors are up to date.")
            
            lines.append("\n## Heavy Collectors (Unused Components)\n")
            for agent in agents:
                comps = agent.get("components", {})
                unused_count = 0
                total_count = 0
                for comp_list in comps.values():
                    for comp in comp_list:
                        total_count += 1
                        if not comp.get("used"):
                            unused_count += 1
                if total_count > 0 and unused_count > 0:
                    pct = int((unused_count / total_count) * 100)
                    lines.append(f"- **{agent.get('id', '')[:16]}**: {unused_count}/{total_count} components unused ({pct}%)\n")
            
            lines.append("\n## Detailed Agent List\n")
            for agent in agents:
                lines.append(f"### {agent.get('id', 'unknown')[:16]}...\n\n")
                lines.append(f"- Healthy: {agent.get('healthy', 'N/A')}\n")
                comps = agent.get("components", {})
                if comps:
                    for comp_type, comp_list in comps.items():
                        in_use = sum(1 for c in comp_list if c.get("used"))
                        lines.append(f"- {comp_type.title()}: {len(comp_list)} total, {in_use} in use\n")
                lines.append("\n")
        
        return "".join(lines)
    
    return ""


def _is_heavy(agent: dict, threshold: float = 0.5) -> bool:
    comps = agent.get("components", {})
    total_count = sum(len(c) for c in comps.values())
    unused_count = sum(1 for cl in comps.values() for c in cl if not c.get("used"))
    return total_count > 0 and (unused_count / total_count) > threshold


def parse_version(v: str) -> tuple:
    try:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())
    except (ValueError, AttributeError):
        return (0,)


def _count_outdated_collectors(agents: list, latest_version: str) -> int:
    latest = parse_version(latest_version)
    count = 0
    for agent in agents:
        comps = agent.get("components", {})
        for comp_list in comps.values():
            for comp in comp_list:
                vid = comp.get("version", "")
                if vid and parse_version(vid) < latest:
                    count += 1
                    break
    return count


def generate_outdated_collectors_report(data: dict, latest_version: str = "0.149.0") -> str:
    agents = data.get("agents", [])
    latest = parse_version(latest_version)
    
    lines = ["# Outdated Collectors Report\n"]
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"Latest version: v{latest_version}\n\n")
    
    outdated_agents = []
    for agent in agents:
        comps = agent.get("components", {})
        outdated_comps = []
        for comp_type, comp_list in comps.items():
            for comp in comp_list:
                vid = comp.get("version", "")
                if vid:
                    v = parse_version(vid)
                    if v < latest:
                        outdated_comps.append((comp_type, comp["id"], vid))
        
        if outdated_comps:
            oldest = min(outdated_comps, key=lambda x: parse_version(x[2]))
            outdated_agents.append((agent.get("id", "")[:16], outdated_comps, oldest[2]))
    
    lines.append(f"Found {len(outdated_agents)} outdated collector(s)\n\n")
    
    if outdated_agents:
        lines.append("## Outdated Collectors\n")
        for aid, comps, oldest in sorted(outdated_agents, key=lambda x: parse_version(x[2])):
            lines.append(f"### {aid}\n")
            lines.append(f"- Oldest component: v{oldest}\n")
            lines.append(f"- Outdated components:\n")
            for comp_type, comp_id, ver in comps:
                lines.append(f"  - {comp_type}/{comp_id}: v{ver}\n")
            lines.append("\n")
    
    return "".join(lines)


def generate_heavy_collectors_report(data: dict, threshold: float = 0.5) -> str:
    agents = data.get("agents", [])
    
    lines = ["# Heavy Collectors Report\n"]
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"Showing collectors with >{int(threshold * 100)}% unused components\n\n")
    
    heavy_agents = []
    for agent in agents:
        comps = agent.get("components", {})
        total_count = 0
        unused_count = 0
        for comp_list in comps.values():
            for comp in comp_list:
                total_count += 1
                if not comp.get("used"):
                    unused_count += 1
        
        if total_count > 0 and (unused_count / total_count) > threshold:
            pct = int((unused_count / total_count) * 100)
            heavy_agents.append((agent.get('id', '')[:16], unused_count, total_count, pct))
    
    lines.append(f"Found {len(heavy_agents)} heavy collector(s)\n\n")
    
    if heavy_agents:
        lines.append("## Heavy Collectors\n")
        for aid, unused, total, pct in sorted(heavy_agents, key=lambda x: -x[3]):
            lines.append(f"- **{aid}**: {unused}/{total} unused ({pct}%)\n")
    
    return "".join(lines)


@st.cache_data(ttl=3600)


def _compliance_badge(compliance: dict) -> str:
    if compliance is None:
        return "⚪"
    if compliance.get("opa_disabled"):
        return "⚪"
    if compliance.get("compliant") is True:
        return "✅"
    if compliance.get("compliant") is False:
        return "❌"
    return "⚪"


def get_collector_versions() -> list:
    try:
        import urllib.request
        url = "https://api.github.com/repos/open-telemetry/opentelemetry-collector-releases/tags?per_page=250"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read().decode()
            tags = json_module.loads(data)
            versions = []
            for tag in tags:
                name = tag.get("name", "")
                if name and "-nightly" not in name:
                    versions.append(name.lstrip("v"))
            return versions if versions else ["0.149.0"]
    except Exception:
        return ["0.149.0"]


@st.cache_data(ttl=3600)
def get_latest_collector_version() -> str:
    versions = get_collector_versions()
    return versions[0] if versions else "0.149.0"


def show_reports_page():
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
            outdated_count = _count_outdated_collectors(data["agents"], threshold_version)
            st.caption(f"Collectors with components below v{threshold_version} - {outdated_count} found")
        
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


def show_slim_distro_builder_page(comps: dict):
    st.markdown("### What is this?")
    st.markdown("""
    This tool analyzes your collector's components and generates a **manifest.yaml** for the 
    OpenTelemetry Collector Builder (OCB). Use OCB to build a slim collector 
    binary containing ONLY the components you actually use.
    """)
    
    st.markdown("### What is OCB?")
    st.markdown("""
    The **OpenTelemetry Collector Builder (OCB)** is a CLI tool that builds custom
    collector binaries from a manifest. [Learn more](https://opentelemetry.io/docs/collector/extend/ocb/)
    """)
    
    st.markdown("### How to use")
    st.markdown("""
    1. Copy the manifest.yaml below
    2. Install OCB: `go install go.opentelemetry.io/collector/cmd/builder@latest`
    3. Run: `ocb build --config manifest.yaml`
    4. Your slim collector is in `./_build/`
    
    **Docker alternative:** `docker run --rm -v $(pwd):/workspace ghcr.io/open-telemetry/otel-collector-builder --config manifest.yaml`
    """)
    
    st.divider()
    
    version = st.text_input("Collector version", value="1.0.0", key="slim_version")
    distro_manifest = generate_manifest(comps, version)
    
    st.markdown("#### manifest.yaml")
    st.code(distro_manifest, language="yaml")
    
    st.download_button(
        "Download manifest.yaml",
        distro_manifest,
        file_name="manifest.yaml",
        mime="text/yaml",
        key="download_manifest"
    )
    
    st.markdown("#### OCB Command")
    st.code(generate_ocb_command(version), language="bash")


def show_policies_page():
    st.markdown("**How to add a new policy:**")
    st.info("Use the **Create Policy** tab for an easy form, or write in code manually")
    st.code("""1. Create policies/tags/require_MYPOLICY.rego
2. Package: package opamp.agent.compliance.MYPOLICY
3. Add violations rule with checks
4. Save file - changes auto-reload! (may take up to 10s)""", language="text")
    st.divider()
    
    pol_tab1, pol_tab2, pol_tab3 = st.tabs(["Policies", "Input Fields", "Create Policy"])
    
    with pol_tab1:
        try:
            resp = requests.get(f"{SERVER_URL}/compliance/validate", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                validation = data.get("policies", [])
                
                valid = [v for v in validation if v.get("valid")]
                invalid = [v for v in validation if not v.get("valid")]
                
                if valid:
                    st.success(f"✅ {len(valid)} valid policy file(s)")
                    st.markdown(f"**{len(valid)} policy rule(s)**")
                    df_policies = pd.DataFrame([
                        {"Policy": p["name"], "Description": p.get("description", "-")}
                        for p in valid
                    ])
                    st.dataframe(df_policies, width='stretch', hide_index=True, use_container_width=True)
                
                if invalid:
                    st.error(f"❌ {len(invalid)} invalid policy file(s)")
                    for v in invalid:
                        st.markdown(f"**{v['filename']}**")
                        for err in v.get("errors", []):
                            st.caption(f"  • {err}")
                
                if not validation:
                    st.info("No .rego files found in policies/tags/")
            else:
                st.error("Failed to load policies")
        except Exception as e:
            st.error(f"Error: {e}")
    
    
    with pol_tab2:
        st.markdown("**Available Input Fields**")
        st.caption("These fields are available in your policy's `input` object:")
        
        st.markdown("""
        - **agent_id**: unique agent identifier
        - **description.identifyingAttributes**: key-value pairs identifying the agent  
        - **description.non_identifyingAttributes**: additional key-value pairs
        """)
        
        with st.expander("Example: How to access attributes"):
            st.code('''# Check if agent has a specific attribute
attr := input.description.identifyingAttributes[_]
attr.key == "agent.version"

# Get the value
version := attr.value.stringValue''', language="rego")
        
        with st.expander("Common attribute keys"):
            st.markdown("""
            - `agent.name` - Name of the agent (e.g., "otelcol-contrib")
            - `agent.version` - Version string (e.g., "0.100.0")
            - `environment` - Deployment environment (e.g., "production")
            - `host.name` - Host where agent is running
            - `os.type` - Operating system type
""")
    
    with pol_tab3:
        st.markdown("**Create New Policy**")
        st.caption("Generate a policy template and save it to policies/tags/")
        
        policy_name = st.text_input("Policy name", placeholder="my_policy", help="This will be the filename: require_<name>.rego")
        policy_desc = st.text_input("Description", placeholder="Agent must have a version", help="Shown in compliance results")
        
        attr_to_check = st.selectbox("Attribute to check", [
            "agent.version",
            "agent.name", 
            "environment",
            "host.name",
            "custom"
        ], help="Which agent attribute to validate")
        
        if attr_to_check == "custom":
            attr_to_check = st.text_input("Custom attribute key", placeholder="my.custom.attr")
        
        condition = st.selectbox("Condition", [
            "must exist",
            "must not be empty"
        ], help="When should a violation be raised?")
        
        if st.button("Generate Policy", use_container_width=True):
            if not policy_name:
                st.error("Please enter a policy name")
            else:
                import re
                pkg_name = policy_name.replace(" ", "_").replace("-", "_")
                rego_template = f'''package opamp.agent.compliance.{pkg_name}

violations contains msg if {{
    not val
    msg := "{policy_desc}"
}}

violations contains msg if {{
    val
    count(val) == 0
    msg := "{policy_desc}"
}}

val := attr.value.stringValue if {{
    attr := input.description.nonIdentifyingAttributes[_]
    attr.key == "{attr_to_check}"
}}
'''
                policies_dir = "policies/tags"
                safe_name = policy_name.replace(" ", "_").replace("-", "_")
                filepath = f"{policies_dir}/require_{safe_name}.rego"
                
                try:
                    os.makedirs(policies_dir, exist_ok=True)
                    with open(filepath, 'w') as f:
                        f.write(rego_template)
                    st.success(f"Created {filepath}")
                    
                    try:
                        resp = requests.post(f"{SERVER_URL}/compliance/reload", timeout=10)
                        if resp.status_code == 401:
                            st.warning("Policy created but reload requires admin password. Use the Admin tab.")
                        elif resp.status_code == 200:
                            st.success("Policy created and reloaded!")
                    except Exception:
                        st.success("Policy created! Reload policies in Admin tab.")
                except Exception as e:
                    st.error(f"Could not write file: {e}")
                    st.code(rego_template, language="rego")
                    st.info("Copy the code above and save it manually.")


def show_agent_details_page(agent_id):
    ui_dir = os.path.dirname(os.path.abspath(__file__))
    st.image(f"{ui_dir}/otel-logo.png", width=200)
    
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        health_data = health_resp.json() if health_resp.status_code == 200 else {}
        opa_available = health_data.get("opa_enabled", False)
    except Exception:
        opa_available = False
    
    with st.sidebar:
        st.markdown("**Server Status**")
        status = "🟢 Online"
        st.caption(status)
        
        if opa_available:
            st.markdown("**Open Policy Agent Status**")
            st.caption("🟢 Available")
        
        st.markdown('<a href="/Agents" target="_self">← Back to Agents</a>', unsafe_allow_html=True)
    
    selected_id = agent_id
    
    data = get_agents()
    agents = data.get("agents", [])
    
    fleet_agent = next((a for a in agents if a.get("id") == selected_id), None)
    detailed_agent = get_agent(selected_id)
    
    if "error" not in detailed_agent:
        health = detailed_agent.get("healthy") or fleet_agent.get("healthy") if fleet_agent else detailed_agent.get("healthy")
        description = detailed_agent.get("description") or fleet_agent.get("description") if fleet_agent else detailed_agent.get("description")
        capability_tags = detailed_agent.get("capability_tags") or fleet_agent.get("capability_tags") if fleet_agent else detailed_agent.get("capability_tags")
        comps = detailed_agent.get("components") or fleet_agent.get("components") if fleet_agent else detailed_agent.get("components")
        last_heartbeat = detailed_agent.get("last_heartbeat") or fleet_agent.get("last_heartbeat") if fleet_agent else detailed_agent.get("last_heartbeat")
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
            import base64
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
    elif compliance:
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
            import base64
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

agent_id_param = st.query_params.get("agent_id")

if agent_id_param:
    show_agent_details_page(agent_id_param)
else:
    ui_dir = os.path.dirname(os.path.abspath(__file__))
    st.image(f"{ui_dir}/otel-logo.png", width=200)
    
    tabs = st.tabs(["Agents", "Reports", "Admin", "Help"])
    
    tab_agents = tabs[0]
    tab_reports = tabs[1]
    tab_admin = tabs[2]
    tab_help = tabs[3]
    
    data = get_agents()
    
    render_sidebar(data)

    with tab_agents:
        agents = data.get("agents", [])
        
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
            group_by = st.selectbox("Group by", ["Healthy", "Compliance"])
            groups = {}
            for agent in agents:
                if group_by == "Healthy":
                    key = "Healthy" if agent.get("healthy") else "Unhealthy" if agent.get("healthy") is False else "Unknown"
                else:
                    key = _compliance_badge(a.get("compliance"))
                groups.setdefault(key, []).append(agent)
            
            for group_name, group_agents in groups.items():
                st.subheader(f"{group_name} ({len(group_agents)})")
                for agent in group_agents:
                    health_icon = "✅" if agent.get("healthy") else "❌" if agent.get("healthy") is False else "⚪"
                    st.markdown(f"**{health_icon}** [{agent['id'][:16]}...](/Agents?agent_id={agent['id']})")
        if not data["agents"]:
            if "error" in data:
                st.error("Server offline — no agents")
            else:
                st.info("No agents connected. Start an OpenTelemetry Collector with OpAMP extension to see it here.")
    
    with tab_reports:
        show_reports_page()
    
    with tab_admin:
        show_admin_page()

    with tab_help:
        show_setup_help_page()


