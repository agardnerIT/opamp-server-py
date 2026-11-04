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
    admin_request,
    pd,
)

session = admin_section(
    "Sessions",
    "Every admin session the server honours, including other browsers.",
    role=ROLE_ADMIN,
)

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


