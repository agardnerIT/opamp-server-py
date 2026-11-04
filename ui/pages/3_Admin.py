import os
import sys
import json as json_module
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.shared import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    SERVER_URL,
    REAUTH_KEY,
    UPDATE_SUBSCRIBE_URL,
    admin_request,
    agent_auth_mode,
    agent_token_rows,
    get_agents,
    get_updates,
    logout,
    mint_agent_token_form,
    render_sidebar,
    require_admin,
    requests,
    pd,
)

st.set_page_config(
    page_title="Admin - OpAMP Server Dashboard",
    page_icon="🔧",
    layout="wide"
)

render_sidebar()

# #105: one login implementation, one session token, no password in session state.
# This page needs `operator`: alerts are the only thing here an operator may change.
# `require_admin` renders the sign-in form and `st.stop()`s if the requirement is not
# met, so everything below runs for an authenticated caller only.
session = require_admin(ROLE_OPERATOR)

st.markdown("**Admin Panel**")
st.caption(
    "Sensitive operations. The server enforces your role on every call — the buttons "
    "this page hides are also refused by the API."
)

# ------------------------------------------------------------------ session bar
with st.container(border=True):
    left, middle, right = st.columns([3, 1, 1])
    with left:
        st.markdown(f"Signed in as **{session.name}** · role `{session.role}`")
        st.caption(f"{session.expiry_label()} · token only, the password was discarded")
    with middle:
        if st.button("Extend", key="admin_extend_session", help=(
            "Sign in again to extend. This is an audited re-auth: the server issues a "
            "new token and the old one is revoked."
        )):
            st.session_state[REAUTH_KEY] = True
            st.rerun()
    with right:
        if st.button("Sign out", key="admin_sign_out_button"):
            logout()

st.divider()

tab_names = ["Alerts", "Compliance"]
if session.can(ROLE_ADMIN):
    tab_names.append("Agent tokens")
    tab_names.append("Sessions")
tab_names.append("Updates")
tab_names.append("Release emails")
admin_tabs = st.tabs(tab_names)

with admin_tabs[0]:
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

with admin_tabs[1]:
    col1, col2 = st.columns([1, 4])
    with col1:
        if session.can(ROLE_ADMIN):
            if st.button("Reload & Validate", key="admin_reload_validate"):
                reload_resp = admin_request("POST", "/compliance/reload", required_role=ROLE_ADMIN)
                if reload_resp is not None and reload_resp.status_code == 200:
                    st.success("Policies reloaded!")
                st.rerun()
        else:
            st.caption("Reloading policies needs the `admin` role.")

    # Policy listing is unauthenticated and read-only, so it needs no token.
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
                    # POST /compliance/check audits and writes the verdict onto the
                    # agent, so it is a mutation: operator, via the shared transport.
                    check = admin_request(
                        "POST", f"/compliance/check/{agent_id}",
                        required_role=ROLE_OPERATOR, timeout=30,
                    )
                    if check is not None and check.status_code == 200:
                        results.append((agent_id, check.json()))

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

if session.can(ROLE_ADMIN):
    with admin_tabs[tab_names.index("Agent tokens")]:
        st.markdown("**Agent tokens**")
        st.caption(
            "The credentials agents present on `/v1/opamp`. A **fleet** token is not "
            "tied to an agent id and serves up to its cap; a **bound** token is one "
            "agent's identity. Tokens are never shown here — only the handle used to "
            "revoke them."
        )
        # Minting lives here as well as on the empty-fleet screen, because revoking is
        # the one thing an operator does *on this page* and it used to leave no way to
        # replace what they had just revoked: the form only existed inside the
        # empty-fleet branch, which is unreachable once any agent has connected.
        if agent_auth_mode() == "token":
            with st.expander("Generate a new token", expanded=False):
                mint_agent_token_form("admin_token")
        else:
            st.caption(
                "This deployment does not require an agent credential "
                f"(`config_push.agent_auth` is not `token`), so there is nothing to "
                "mint here. Set `OPAMP_AGENT_AUTH=token` to require one."
            )
        tokens_resp = admin_request("GET", "/agent-tokens", required_role=ROLE_ADMIN)
        if tokens_resp is None or tokens_resp.status_code != 200:
            st.error("Failed to list agent tokens.")
        else:
            tokens = tokens_resp.json().get("tokens") or []
            if not tokens:
                st.info(
                    "No tokens yet. Agents cannot connect until one exists — mint the "
                    "first one above, or with `opampctl agent-tokens create`."
                )
            else:
                st.dataframe(
                    pd.DataFrame(agent_token_rows(tokens)),
                    width="stretch",
                    hide_index=True,
                )
                revocable = [t for t in tokens if not t.get("revoked_at")]
                if revocable:
                    labels = {
                        f"{t['token_id']} · {t.get('label') or 'unlabelled'} · "
                        f"{t.get('agents_used') or 0} agent(s)": t
                        for t in revocable
                    }
                    chosen = st.selectbox(
                        "Revoke a token", list(labels), key="admin_revoke_token_choice"
                    )
                    confirmed = st.checkbox(
                        "I understand this blocks those agents immediately (their state "
                        "and config are kept)",
                        key="admin_revoke_token_confirm",
                    )
                    if st.button(
                        "Revoke token",
                        key="admin_revoke_token_button",
                        disabled=not confirmed,
                        type="primary",
                    ):
                        token_id = labels[chosen]["token_id"]
                        revoke_resp = admin_request(
                            "DELETE",
                            f"/agent-tokens/{token_id}",
                            required_role=ROLE_ADMIN,
                        )
                        if revoke_resp is not None and revoke_resp.status_code == 200:
                            st.success(f"Revoked `{token_id}`.")
                            st.rerun()
                        elif revoke_resp is not None:
                            st.error(f"Revoking failed: {revoke_resp.text}")

    with admin_tabs[tab_names.index("Sessions")]:
        st.markdown("**Active admin sessions**")
        st.caption(
            "Every session the server currently honours, including other browsers. "
            "Revoking one is audited against your principal."
        )
        sessions_resp = admin_request("GET", "/auth/sessions", required_role=ROLE_ADMIN)
        if sessions_resp is None or sessions_resp.status_code != 200:
            st.error("Failed to list sessions.")
        else:
            records = sessions_resp.json().get("sessions", [])
            if not records:
                st.info("No sessions.")
            else:
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Session": r.get("session_id"),
                            "Principal": r.get("principal", ""),
                            "Started": r.get("created_at"),
                            "Last seen": r.get("last_seen_at"),
                            "Expires": r.get("expires_at"),
                            "Client IP": r.get("client_ip", ""),
                            "Expired": "yes" if r.get("expired") else "no",
                        }
                        for r in records
                    ]),
                    width='stretch',
                    hide_index=True,
                )
                revoke_id = st.text_input(
                    "Revoke session id", key="admin_revoke_session_id",
                    help="The short id from the Session column (at least 6 characters).",
                )
                if st.button("Revoke session", key="admin_revoke_session_button"):
                    if not revoke_id.strip():
                        st.warning("Enter a session id first.")
                    else:
                        revoke = admin_request(
                            "DELETE", f"/auth/sessions/{revoke_id.strip()}",
                            required_role=ROLE_ADMIN,
                        )
                        if revoke is not None and revoke.status_code == 200:
                            st.success("Session revoked.")
                            st.rerun()
                        elif revoke is not None and revoke.status_code == 404:
                            st.error("No session with that id.")


with admin_tabs[tab_names.index("Updates")]:
    # #46: the check is on by default; the deployer can force it off with
    # UPDATE_CHECK=off, and an admin can toggle the persisted override.
    update_state = get_updates()
    st.markdown("**Update checks**")
    st.caption(
        f"Current: `{update_state.get('current_version', 'unknown')}` · "
        f"Latest: `{update_state.get('latest_tag') or 'unknown'}` · "
        f"Last checked: {update_state.get('checked_at') or 'never'}"
    )
    if update_state.get("error"):
        st.warning(f"The last check failed: `{update_state['error']}`")

    deployment_enabled = bool(update_state.get("deployment_enabled", True))
    can_admin = session.can(ROLE_ADMIN)
    if not deployment_enabled:
        st.info(
            "Automatic update checks are disabled by the deployment "
            "(`UPDATE_CHECK=off`). The admin toggle cannot override that."
        )

    desired = st.toggle(
        "Check for updates automatically",
        value=bool(update_state.get("enabled", True)),
        disabled=not deployment_enabled or not can_admin,
        key="updates_auto",
    )
    if st.button(
        "Apply",
        key="updates_apply",
        disabled=not deployment_enabled or not can_admin,
    ):
        response = admin_request(
            "PUT",
            "/updates/settings",
            required_role=ROLE_ADMIN,
            json={"enabled": desired},
        )
        if response is not None and response.status_code == 200:
            st.success("Saved.")
            get_updates.clear()
            st.rerun()
        elif response is not None:
            st.error(f"Failed to save: HTTP {response.status_code}")

    if st.button(
        "Use deployment default",
        key="updates_default",
        disabled=not deployment_enabled or not can_admin,
    ):
        response = admin_request(
            "PUT",
            "/updates/settings",
            required_role=ROLE_ADMIN,
            json={"enabled": None},
        )
        if response is not None and response.status_code == 200:
            st.success("Cleared the admin override.")
            get_updates.clear()
            st.rerun()
        elif response is not None:
            st.error(f"Failed to clear: HTTP {response.status_code}")

    if st.button(
        "Check now",
        key="updates_check",
        disabled=not update_state.get("enabled", False) or not can_admin,
    ):
        response = admin_request(
            "POST",
            "/updates/check",
            required_role=ROLE_ADMIN,
            timeout=10,
        )
        if response is not None and response.status_code == 200:
            st.success("Checked.")
            get_updates.clear()
            st.rerun()
        elif response is not None and response.status_code == 429:
            st.warning("A check ran less than a minute ago. Try again shortly.")
        elif response is not None and response.status_code == 409:
            st.warning("Update checks are disabled.")
        else:
            st.error("The check failed.")


with admin_tabs[tab_names.index("Release emails")]:
    # #46: the address is submitted to the project's Cloudflare Worker and stored
    # in the project's Resend account. This server never sees or stores it, which
    # is why the form is an iframe rather than a server-side POST.
    st.markdown("**Release emails**")
    st.caption(
        "Get one email when a new opamp-server-py release is published. The "
        "address is stored by the project's Resend account, not by this server."
    )
    if not UPDATE_SUBSCRIBE_URL:
        st.info(
            "Release email subscriptions are not configured for this deployment. "
            "Set `UPDATE_SUBSCRIBE_URL` to the public update Worker URL to show "
            "the signup form."
        )
    else:
        components.iframe(UPDATE_SUBSCRIBE_URL, height=620, scrolling=True)
