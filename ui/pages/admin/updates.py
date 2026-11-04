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
    admin_request,
    get_updates,
)

session = admin_section(
    "Updates",
    "Whether this deployment checks for new releases, and running a check.",
    role=ROLE_OPERATOR,
)

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


