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
    UPDATE_SUBSCRIBE_URL,
)

session = admin_section(
    "Release emails",
    "Subscribe to one email per release. Stored by the project, not this server.",
    role=ROLE_OPERATOR,
)

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
