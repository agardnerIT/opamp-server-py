import os
import sys

import streamlit as st

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
)

from ui.admin_common import admin_section
from ui.shared import (  # noqa: E402 - after the sys.path fix-up
    ROLE_OPERATOR,
    admin_request,
    json_module,
    st,
)

session = admin_section(
    "Alerts",
    "Webhook alerts for fleet events. The templates and headers live on the "
    "server; nothing here is stored in the browser.",
    role=ROLE_OPERATOR,
)

resp = admin_request("GET", "/alerts", required_role=ROLE_OPERATOR)
if resp is None or resp.status_code != 200:
    st.error("Failed to load alerts config.")
    st.stop()

alert_data = resp.json()
config = alert_data.get("config", {})
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
                # Autosave first (memory.md: an unsaved template would otherwise be
                # ignored), then fire. Both go through the one admin transport.
                admin_request("PUT", "/alerts", required_role=ROLE_OPERATOR, json=save_payload)
                test_resp = admin_request(
                    "POST", "/alerts/test", required_role=ROLE_OPERATOR,
                    json={"event_type": event}, timeout=10,
                )
                if test_resp is not None:
                    try:
                        result = test_resp.json()
                    except Exception:
                        result = {}
                    if result.get("success"):
                        st.success("Test sent!")
                    else:
                        st.error(f"Failed: {result.get('error')}")

        event_configs[event] = {
            "enabled": event_enabled,
            "webhook_url": webhook_url,
            "headers": headers_json,
            "body_template": body_template,
        }

if st.button("Save & Apply", type="primary", key="save_alerts"):
    save_resp = admin_request(
        "PUT", "/alerts", required_role=ROLE_OPERATOR, json={"events": event_configs}
    )
    if save_resp is not None and save_resp.status_code == 200:
        st.success("Saved!")
    elif save_resp is not None:
        st.error("Failed to save")

