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
    ROLE_ADMIN,
    ROLE_OPERATOR,
    SERVER_URL,
    admin_request,
    get_agents,
    pd,
    requests,
)

session = admin_section(
    "Compliance",
    "The OPA policy files this server loads, and per-agent checks.",
    role=ROLE_OPERATOR,
)

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

