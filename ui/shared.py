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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.manifest import generate_manifest, generate_ocb_command
from server.reports import (  # re-exported: report logic lives in server/reports.py
    generate_agent_report,
    generate_heavy_collectors_report,
    generate_outdated_collectors_report,
    _count_outdated_collectors,
    parse_version,
    _is_heavy,
)

SERVER_HTTP_SCHEME = os.environ.get("SERVER_HTTP_SCHEME", "http")
SERVER_ADDRESS = os.environ.get("SERVER_ADDRESS", "localhost")
SERVER_PORT = os.environ.get("SERVER_PORT", "4320")
SERVER_URL = f"{SERVER_HTTP_SCHEME}://{SERVER_ADDRESS}:{SERVER_PORT}"


def get_server_url():
    return SERVER_URL


def format_local_time(utc_str):
    if not utc_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str[:19] if len(utc_str) > 19 else utc_str


def get_auth_status():
    try:
        resp = requests.get(f"{SERVER_URL}/auth/status", timeout=5)
        return resp.json()
    except Exception:
        return {"password_required": False}


def get_auth_headers():
    # Check for stored password from Admin page first, then current input
    password = st.session_state.get("admin_password") or st.session_state.get("admin_password_input")
    if password:
        encoded = base64.b64encode(f":{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {}


def prompt_for_password(password_key, attempt_key, page_suffix=""):
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
                    test_encoded = base64.b64encode(f":{password}".encode()).decode()
                    test_headers = {"Authorization": f"Basic {test_encoded}"}
                    try:
                        test_resp = requests.get(f"{SERVER_URL}/auth/verify", headers=test_headers, timeout=5)
                        if test_resp.status_code == 200:
                            st.session_state[password_key] = password
                            st.rerun()
                        elif test_resp.status_code == 401:
                            st.session_state[attempt_key] += 1
                            st.session_state["admin_password_error"] = True
                            st.rerun()
                        else:
                            st.error(f"Server error: {test_resp.status_code}")
                    except Exception as e:
                        st.error(f"Failed to verify password: {e}")
                elif submitted and not password:
                    st.session_state[attempt_key] += 1
                    st.error("Password required")
                    st.rerun()
                return None
        else:
            return st.session_state.get(password_key)
    return None


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


def _compliance_badge(compliance):
    if compliance is None:
        return "⚪"
    if compliance.get("opa_disabled"):
        return "⚪"
    if compliance.get("compliant") is True:
        return "✅"
    if compliance.get("compliant") is False:
        return "❌"
    return "⚪"


@st.cache_data(ttl=3600)
def get_collector_versions():
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
def get_latest_collector_version():
    versions = get_collector_versions()
    return versions[0] if versions else "0.149.0"


def show_slim_distro_builder_page(comps):
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


def render_sidebar():
    # Hide default "app" pages header
    st.markdown("""
    <style>
    [data-testid="stSidebarNav"] > div:first-child > div:first-child {
        display: none;
    }
    </style>
    """, unsafe_allow_html=True)
    
    ui_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logo_path = f"{ui_dir}/ui/otel-logo.png"
    
    data = get_agents()
    st.session_state["agents_data"] = data
    
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        health_data = health_resp.json() if health_resp.status_code == 200 else {}
        opa_available = health_data.get("opa_enabled", False)
    except Exception:
        opa_available = False
    
    with st.sidebar:
        if os.path.exists(logo_path):
            st.image(logo_path, width=200)
        
        st.markdown("**Server Status**")
        status = "🟢 Online" if "error" not in data else "🔴 Offline"
        st.caption(status)
        
        if opa_available:
            st.markdown("**Open Policy Agent Status**")
            st.caption("🟢 Available")
        
        st.markdown("**Agents Connected**")
        st.caption(f"{data.get('count', 0)}")
