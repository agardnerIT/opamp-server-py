"""Shared chrome for the Admin subpages.

Admin used to be one page with six ``st.tabs``. Two things were wrong with that, and
both were structural:

* **Tabs are stateless.** Every rerun re-renders with the first tab selected, so any
  action that reruns moves the operator — minting a token landed them on "Alerts"
  with the one-time secret rendered into a tab nobody was looking at, and revoking
  did the same;
* **Everything renders at once.** Five screens of forms and two network calls on
  every visit, for the one section the operator came for.

Each section is a page now: one job, its own entry in the sidebar, its own URL. That
is also why nothing here needs a tab key or a "preserve the tab" rerun helper — the
class of bug is gone rather than patched.

A page gets its sign-in requirement, heading, and the session bar from
``admin_section``, so all six look and behave the same without six copies of the
guard. The server re-authorises every call; this is ergonomics, not the boundary.
"""

from __future__ import annotations

import streamlit as st

from ui.shared import (
    REAUTH_KEY,
    ROLE_OPERATOR,
    logout,
    render_sidebar,
    require_admin,
)


def render_session_bar(session):
    """Who you are signed in as, with the audited re-auth and sign-out actions."""
    with st.container(border=True):
        left, middle, right = st.columns([3, 1, 1])
        with left:
            st.markdown(f"Signed in as **{session.name}** · role `{session.role}`")
            st.caption(f"{session.expiry_label()} · token only, the password was discarded")
        with middle:
            if st.button(
                "Extend",
                key="admin_extend_session",
                help=(
                    "Sign in again to extend. This is an audited re-auth: the server "
                    "issues a new token and the old one is revoked."
                ),
            ):
                st.session_state[REAUTH_KEY] = True
                st.rerun()
        with right:
            if st.button("Sign out", key="admin_sign_out_button"):
                logout()


def admin_section(title, caption="", role=ROLE_OPERATOR):
    """Render one admin page's chrome and return the authenticated session.

    ``role`` is what the page needs: ``operator`` for configuration an operator may
    change, ``admin`` for credentials and sessions. ``require_admin`` renders the
    shared sign-in form and ``st.stop()``s when the requirement is not met, so a
    subpage body only ever runs for a caller the server would also accept.
    """
    render_sidebar()
    session = require_admin(role)
    st.header(title)
    if caption:
        st.caption(caption)
    render_session_bar(session)
    st.divider()
    return session


__all__ = ["admin_section", "render_session_bar"]
