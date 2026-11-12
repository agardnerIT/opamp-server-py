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
    agent_token_rows,
    mint_agent_token_form,
    pd,
)

session = admin_section(
    "Agent tokens",
    "The credentials agents present on `/v1/opamp`. A **fleet** token is not tied to "
    "an agent id and serves up to its cap; a **bound** token is one agent's identity.",
    role=ROLE_ADMIN,
)

# The form is a page-level thing, not something hidden behind a disclosure: an
# operator arrives here either to mint a credential (usually the first one, before
# any agent exists) or to replace one they just revoked — the two moments when
# making it find the form is the entire task. Agent auth is mandatory (#125), so
# there is no deployment where this page has nothing to offer.
st.subheader("Mint a token")
mint_agent_token_form("admin_token")

st.divider()
st.subheader("Issued tokens")
st.caption(
    "Tokens are never shown again — only the handle used to revoke them. A revoked "
    "token blocks its agents immediately; their state and config are kept."
)

tokens_resp = admin_request("GET", "/agent-tokens", required_role=ROLE_ADMIN)
if tokens_resp is None or tokens_resp.status_code != 200:
    st.error("Failed to list agent tokens.")
else:
    tokens = tokens_resp.json().get("tokens") or []
    if not tokens:
        st.info(
            "No tokens yet. Agents cannot connect until one exists — mint the first "
            "one above, or with `opampctl agent-tokens create`."
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
                    # No tab to preserve: this page is the only thing being rendered.
                    st.rerun()
                elif revoke_resp is not None:
                    st.error(f"Revoking failed: {revoke_resp.text}")
