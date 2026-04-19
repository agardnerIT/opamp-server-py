import os
import sys
import json as json_module
import streamlit as st
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.shared import (
    SERVER_URL,
    get_agents,
    get_auth_status,
    render_sidebar,
    base64,
    requests,
    pd,
)

st.set_page_config(
    page_title="Admin - OpAMP Server Dashboard",
    page_icon="🔧",
    layout="wide"
)

render_sidebar()

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
            st.stop()
    else:
        password = st.session_state.get("admin_password")

if password:
    encoded = base64.b64encode(f":{password}".encode()).decode()
    headers = {"Authorization": f"Basic {encoded}"}
else:
    st.stop()

admin_tabs = st.tabs(["Alerts", "Compliance"])

with admin_tabs[0]:
    try:
        resp = requests.get(f"{SERVER_URL}/alerts", timeout=5, headers=headers)
        if resp.status_code == 401:
            st.session_state.pop("admin_password", None)
            st.session_state["admin_password_attempt"] = st.session_state.get("admin_password_attempt", 0) + 1
            st.session_state["admin_password_error"] = True
            st.rerun()
        alert_data = resp.json()
    except Exception as e:
        st.error(f"Failed to load alerts config: {e}")
        st.stop()

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

with admin_tabs[1]:
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
