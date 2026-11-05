import json
import os
import re
import sys
import streamlit as st
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.sql_search import sql_search
from ui.shared import (
    AGENT_TOKEN_ENV,
    EXPIRY_CHOICES,
    MINTED_TOKEN_KEY,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    SERVER_URL,
    agent_auth_mode,
    agent_auth_note,
    agent_type_badge,
    collector_config_snippet,
    format_local_time,
    get_agents,
    get_agent,
    get_server_url,
    _compliance_badge,
    admin_request,
    render_sidebar,
    require_admin,
    reissue_banner_text,
    show_slim_distro_builder_page,
    display_config_yaml,
    generate_manifest,
    generate_ocb_command,
    mint_agent_token_form,
    get_session,
    datetime,
    requests,
    pd,
)


render_sidebar()


def _mint_token_and_snippet(in_container):
    """#126: mint the token the snippet needs, because the operator has none yet.

    On a `token` server the connect snippet is useless on its own, and the old page
    either printed a placeholder the operator could not fill in or (before #124) printed
    nothing at all and let the agent 401 forever. A fleet token is not bound to an
    `instance_uid`, so this can be done *before* any agent exists — which is exactly the
    state an empty fleet is in.

    The form itself is `ui.shared.mint_agent_token_form`, shared with the Admin page:
    this screen needs a token to make its snippet runnable, the Admin page needs one
    after a revocation, and both need the same defaults.
    """
    mint_agent_token_form(
        "empty_fleet",
        in_container=in_container,
        intro=(
            "**1. Generate a token for this fleet.** Agents present it on every "
            "connection, so it has to exist before the first one can report — and a "
            "fleet token is not tied to an agent id, so this works on an empty fleet."
        ),
    )


def _empty_fleet():
    """#116: an empty fleet is a setup question, so answer it here.

    "No agents connected" was a dead end for anyone who had never configured OpAMP:
    the next step is always the same ~30 lines of YAML, and the two lines people
    actually get wrong are `service.extensions: [opamp]` (without it the extension
    never starts) and the endpoint host (a containerised collector cannot reach the
    host's `localhost`). Both are called out next to the snippet instead of in docs a
    new user has to find.

    The refresh button is not decoration: this state is entered before the agent
    exists, so by the time it connects the operator is looking at a page that has no
    reason to re-run. Streamlit only re-runs on interaction, and nothing on this
    screen is an input, so without a button "no agents connected" outlives the
    connection until the operator navigates away.
    """
    notice, action = st.columns([4, 1], vertical_alignment="center")
    with notice:
        st.info("No agents connected yet — nothing is reporting to this server.")
    with action:
        if st.button(
            "↻ Refresh",
            key="empty_fleet_refresh",
            use_container_width=True,
            help="Ask the server again now. An agent that has just started may have "
            "registered since this page was rendered.",
        ):
            st.rerun()
    data = st.session_state.get("agents_data", {}) or {}
    if "error" in data:
        # A server we cannot reach is not a configuration problem, and the snippet
        # would send a new user looking in the wrong place.
        st.error("The server is unreachable, so the agent list cannot be loaded.")
        st.caption(f"`GET {get_server_url()}/agents` failed: {data['error']}")
        return
    st.markdown(
        "Copy this into `config.yaml`, run `otelcol-contrib --config config.yaml`, "
        "and this page fills in within a poll interval:"
    )
    in_container = st.toggle(
        "My collector runs in Docker",
        key="empty_fleet_in_container",
        help="Rewrites the endpoint to host.docker.internal, the host as seen from a container.",
    )
    # #124: the server's own mode decides whether the snippet carries a credential. The
    # page used to print the unauthenticated shape on every server, which 401s on any
    # deployment that requires a token — including every one that can push config.
    auth_mode = agent_auth_mode()

    # #126: on a `token` server the snippet is useless without a token, and the operator
    # has none — so the page mints one instead of printing a placeholder they cannot
    # fill in. This is the flow the whole issue exists for: an empty fleet on a fresh
    # install, where nothing has registered yet and the token is minted *first*.
    if auth_mode == "token":
        _mint_token_and_snippet(in_container)
        st.caption(
            "Still nothing after a minute? Check that `service.extensions` lists `opamp` "
            f"and that the collector can reach `{get_server_url()}` — the server sees "
            "every request it receives, so a missing agent is almost always a config or "
            "network question, not a server one."
        )
        return

    snippet = collector_config_snippet(
        get_server_url(), in_container=in_container, agent_auth=auth_mode
    )
    st.code(snippet, language="yaml")
    st.download_button(
        "Download config.yaml",
        snippet,
        file_name="config.yaml",
        mime="text/yaml",
        key="empty_fleet_download",
    )
    if in_container:
        st.caption(
            "On Linux, start the container with "
            "`--add-host=host.docker.internal:host-gateway` so that name resolves."
        )
    # A required credential is an instruction, not a footnote: it changes the order the
    # operator has to do things in.
    note = agent_auth_note(auth_mode)
    if note:
        (st.warning if auth_mode == "token" else st.caption)(note)
    st.caption(
        "Still nothing after a minute? Check that `service.extensions` lists `opamp` "
        f"and that the collector can reach `{get_server_url()}` — the server sees "
        "every request it receives, so a missing agent is almost always a config or "
        "network question, not a server one."
    )


# ---------------------------------------------------------- SQL agent search (#132)


def _run_agent_query(sql):
    """Return `(agents, error)` for a SQL selection. Never raises into the page."""
    try:
        response = requests.get(
            f"{SERVER_URL}/agents", params={"q": sql}, timeout=10
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code == 200:
            return payload.get("agents") or [], None
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail)
        return [], str(detail or f"HTTP {response.status_code}")
    except Exception as exc:
        return [], str(exc)


#: Same rule the store enforces (`SQLiteGroupStore.NAME_PATTERN`), checked here only so
#: a typo is caught *before* the password prompt instead of after it. The server stays
#: the authority: this copy can be wrong without ever letting a bad name through.
def _group_name_ok(name):
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", (name or "").strip().lower()))


def _save_query_as_group(active_query):
    """#132 follow-up: save the query you just tuned as a group, from this page.

    The workflow the issue describes is *search, adjust, then save* — so the button
    belongs next to the result, not on a second page where the query has to be
    retyped. Saving is an `operator` action and the operator may not be signed in yet,
    so the sign-in form appears **after** the click (`require_admin`), not as a wall in
    front of the search box: browsing the fleet stays possible without a password.

    The payload is stashed in session state before the prompt because `require_admin`
    ends that run, and signing in reruns the script from the top. Without the stash the
    click would be lost and the operator would be asked for a password, then left with
    nothing saved.
    """
    notice = st.session_state.pop("agents_save_notice", None)
    if notice:
        st.success(notice)
        st.caption(
            "Membership is resolved on read, and saving a group pushes nothing. "
            "Preview, edit or archive it on the **Groups** page."
        )

    pending = st.session_state.get("agents_save_pending")
    if pending:
        # This is the password prompt: it renders *here*, where the operator clicked.
        require_admin(ROLE_OPERATOR)
        response = admin_request(
            "POST", "/groups", required_role=ROLE_OPERATOR, json=pending
        )
        st.session_state.pop("agents_save_pending", None)
        if response is not None and response.status_code == 200:
            try:
                created = response.json() or {}
            except Exception:
                created = {}
            st.session_state["agents_save_open"] = False
            st.session_state.pop("agents_save_name", None)
            st.session_state.pop("agents_save_description", None)
            st.session_state["agents_save_notice"] = (
                f"Saved `{created.get('name') or pending.get('name')}` with "
                f"{created.get('member_count', 0)} member(s)."
            )
            st.rerun()
        elif response is not None and response.status_code == 409:
            # `admin_request` already showed the server's message; this is the one
            # failure the operator can fix here, so say what to do about it.
            st.info(
                "Group names are unique regardless of case. Pick another name and "
                "save again."
            )
        # Any other failure was reported by `admin_request`; the form stays open with
        # the name still in it so the operator can fix and retry.
        return

    if st.button(
        "💾 Save this query as a group",
        key="agents_save_open_button",
        help=(
            "Name the query and it becomes a saved selection you can target with "
            "Remote Config. Saving pushes nothing."
        ),
    ):
        st.session_state["agents_save_open"] = True

    if not st.session_state.get("agents_save_open"):
        return

    with st.form("agents_save_group_form"):
        name = st.text_input(
            "Group name",
            key="agents_save_name",
            placeholder="macos-au",
            help=(
                "2–63 lowercase characters from a-z, 0-9, '.', '_' and '-', starting "
                "with a letter or digit. Unique regardless of case."
            ),
        )
        description = st.text_input(
            "Description (optional)",
            key="agents_save_description",
            placeholder="macOS gateways in Australia. Owner: platform-oncall.",
            help="Shown in the group list, for the next person who has to guess.",
        )
        st.caption(f"Saved as: `{active_query}`")
        save_col, cancel_col = st.columns([1, 5])
        with save_col:
            save_clicked = st.form_submit_button("Save group", type="primary")
        with cancel_col:
            cancel_clicked = st.form_submit_button("Cancel")

    if cancel_clicked:
        st.session_state["agents_save_open"] = False
        st.rerun()

    if save_clicked:
        if not _group_name_ok(name):
            st.error(
                "That name will be refused. Use 2–63 lowercase characters from a-z, "
                "0-9, '.', '_' and '-', starting with a letter or digit."
            )
            return
        st.session_state["agents_save_pending"] = {
            "name": (name or "").strip().lower(),
            "query": active_query,
            "description": (description or "").strip() or None,
        }
        # A clean run, so the pending payload is what asks for the password and then
        # saves — see the docstring: this click must survive the sign-in rerun.
        st.rerun()


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
        # #118: what the server thinks this agent is, and how it decided — a wrong
        # guess has to be visible *before* it becomes a config push. A `generic`
        # agent gets no component inventory at all, so "0/0 in use" needs its reason
        # next to it rather than looking like a collector with nothing installed.
        st.markdown("**Type**")
        st.caption(agent_type_badge(detailed_agent if "error" not in detailed_agent else fleet_agent))
        type_warning = (detailed_agent or {}).get("agent_type_warning") or (
            fleet_agent or {}
        ).get("agent_type_warning")
        if type_warning:
            st.warning(type_warning)
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
        # #85: the spec's extension namespace, straight from the agent. A capability
        # the agent does not advertise cannot be messaged at all (the API answers 400),
        # so showing the list is what makes the send form usable.
        custom = detailed_agent.get("custom_capabilities") or []
        st.markdown("**Custom capabilities**")
        if custom:
            for capability in custom:
                st.code(capability, language=None)
        else:
            st.caption("None advertised")
        pending = detailed_agent.get("pending_custom_message")
        if pending:
            st.caption(
                f"Queued: `{pending.get('capability')}` / `{pending.get('type')}` "
                f"(waiting for the agent's next poll)"
            )
        elif detailed_agent.get("custom_message_delivered_at"):
            st.caption(
                "Last custom message delivered "
                f"{format_local_time(detailed_agent.get('custom_message_delivered_at'))}"
            )

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
                        "Download Config",
                        new_yaml,
                        file_name="collector-config.yaml",
                        mime="text/yaml",
                        key="download_telemetry_config",
                    )
                with col_exp:
                    with st.expander("Show config"):
                        st.code(new_yaml, language="yaml")
            else:
                st.caption("No config object in this agent's report is readable as YAML or JSON")
    
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
        st.markdown("**Configuration**")
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
            st.caption("No config object in this agent's report is readable as YAML or JSON")
    elif effective_config:
        st.caption("Toggle to show configuration")
    else:
        st.caption("No effective config reported by this agent")

else:
    all_agents = st.session_state.get("agents_data", {}).get("agents", [])

    if not all_agents:
        _empty_fleet()
        # Nothing below this line (view-mode radio, property grouping) means anything
        # for an empty fleet, and the old trailing "No agents connected" info was a
        # second, worse copy of the same message.
        st.stop()

    st.header("Agent List")

    # ---------------------------------------------------------- SQL search (#132)
    # The list is the search interface: type a SELECT, Tab through the live schema,
    # and see the matching agents. The component computes suggestions in the browser
    # (no rerun per keystroke); Python only hears about a submitted query.
    schema = {}
    try:
        schema_response = requests.get(
            f"{SERVER_URL}/agents/query/schema", timeout=5
        )
        if schema_response.status_code == 200:
            schema = schema_response.json() or {}
    except Exception:
        schema = {}

    if "agents_sql" not in st.session_state:
        st.session_state["agents_sql"] = ""
    if "agents_sql_revision" not in st.session_state:
        # Bumped every time Python sets the query itself. The editor only adopts a
        # pushed-down `value` that carries a new revision, so the rerun after a submit
        # (which still carries the pre-submit text) cannot empty the box.
        st.session_state["agents_sql_revision"] = 0

    st.markdown("**Search agents with SQL**")
    search_result = sql_search(
        value=st.session_state.get("agents_sql") or "",
        revision=st.session_state.get("agents_sql_revision") or 0,
        schema=schema,
        key="agents_sql_component",
        label="",
        placeholder="SELECT * FROM agents WHERE ...",
    )
    submit_nonce = search_result.get("nonce")
    if (
        search_result.get("submit")
        and submit_nonce is not None
        and submit_nonce != st.session_state.get("agents_last_submit_nonce")
    ):
        # A component value is sticky until it changes again, so the nonce is what
        # makes one Enter press mean one search instead of an infinite rerun.
        st.session_state["agents_last_submit_nonce"] = submit_nonce
        text = search_result.get("text") or ""
        st.session_state["agents_sql"] = text
        st.session_state["agents_sql_revision"] = (
            int(st.session_state.get("agents_sql_revision") or 0) + 1
        )
        st.session_state["agents_active_query"] = text.strip() or None
        st.rerun()
    elif search_result.get("text") and search_result["text"] != st.session_state.get("agents_sql"):
        # A blur sends the text without submitting, so a later rerun (view mode,
        # refresh) does not replace what the operator typed.
        st.session_state["agents_sql"] = search_result["text"]

    st.caption(
        "Read-only SQL over the live fleet. Description attributes are columns "
        "(`host_arch`, `environment`, `\"os.type\"`, …); suggestions come from the "
        "columns and values this fleet actually reports. When the query says what "
        "you mean, save it as a group below."
    )

    active_query = st.session_state.get("agents_active_query")
    agents = all_agents
    if active_query:
        matched, error = _run_agent_query(active_query)
        if error:
            st.error(f"SQL search refused: {error}")
            st.stop()
        agents = matched
        if not agents:
            st.caption(f"Showing 0 of {len(all_agents)} agent(s) matching `{active_query}`")
            st.info(
                "No agents matched. Check the attribute names in the suggestions — "
                "an unknown attribute matches nothing rather than failing the query."
            )
            st.stop()
        # The query has run and it matches something, which is exactly the moment the
        # operator is "happy with the query". Saving is offered here, next to the
        # result, instead of sending them to the Groups page to retype it.
        st.caption(
            f"Showing {len(agents)} of {len(all_agents)} agent(s) matching "
            f"`{active_query}`"
        )
        _save_query_as_group(active_query)
    else:
        st.caption(f"Showing {len(agents)} agent(s)")

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
            # A query-only link stays on the current page. The old href hardcoded
            # `/Agents`, but the nav declares the url_path `agents`, and Streamlit
            # matches paths case-sensitively: the router did not find the page and
            # raised "Page not found" over the agent detail that *had* rendered
            # (the query param was already applied to the page underneath).
            link = f'<a href="?agent_id={a["id"]}" target="_self" style="color:#0066cc;text-decoration:underline;cursor:pointer">{a["id"][:32]}{"..." if len(a["id"]) > 32 else ""}</a>'
            rows.append({
                "Agent": link,
                "Healthy": "✅" if a.get("healthy") else "❌" if a.get("healthy") is False else "⚪",
                # #118: the type and its provenance, so a mis-detection is visible in
                # the fleet view rather than only after a config push goes wrong.
                "Type": agent_type_badge(a),
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
                st.markdown(
                    f"**{health_icon}** [{agent['id'][:16]}...](?agent_id={agent['id']})"
                    f" — {agent_type_badge(agent)}"
                )
