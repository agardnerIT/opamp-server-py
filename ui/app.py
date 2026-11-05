"""The app entrypoint: it only declares the navigation.

Everything is a page with its own URL, including what used to be the Admin tabs.
The reason is in ``ui/admin_common.py``, but the short version is that tabs are
stateless — a rerun re-selects the first one, so an action that reruns (minting a
token, revoking a session) used to move the operator somewhere they had not asked to
go and render the result out of sight.

``st.navigation`` owns the sidebar, the URL and the default page, which is why this
file calls ``st.set_page_config``: with navigation in play the page scripts must not
(a second call in the same run is an error), so the layout is set once, here.

Escape hatch: the pages are ordinary Streamlit scripts, so ``AppTest.from_file()`` on
any one of them still works without this file. That is what the UI tests rely on.
"""

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGES = ROOT / "ui" / "pages"

st.set_page_config(
    page_title="OpAMP Server Dashboard",
    page_icon="🏠",
    layout="wide",
)


# `url_path` is flat on purpose: Streamlit rejects nested paths, so the Admin
# grouping lives in the sidebar section rather than in front of every URL.
def page(path, title, icon, url_path, **kwargs):
    return st.Page(
        str(PAGES / path), title=title, icon=icon, url_path=url_path, **kwargs
    )


nav = st.navigation(
    {
        "": [
            # Agents is the default because it is what an operator opens the app to
            # look at. The old landing page only told the reader to use the sidebar.
            page("1_Agents.py", "Agents", "📡", "agents", default=True),
            page("2_Reports.py", "Reports", "📄", "reports"),
            # #132: groups are saved SQL selections, so they get their own page
            # instead of a section under Remote Config (which only consumes them).
            page("3_Groups.py", "Groups", "🗂️", "groups"),
            page("5_Remote_Config.py", "Remote Config", "🎛️", "remote-config"),
            page("4_Help.py", "Help", "❓", "help"),
        ],
        "Admin": [
            page("admin/alerts.py", "Alerts", "🔔", "alerts"),
            page("admin/compliance.py", "Compliance", "✅", "compliance"),
            page("admin/agent_tokens.py", "Agent tokens", "🔑", "agent-tokens"),
            page("admin/sessions.py", "Sessions", "👥", "sessions"),
            page("admin/updates.py", "Updates", "⬆️", "updates"),
            page(
                "admin/release_emails.py",
                "Release emails",
                "✉️",
                "release-emails",
            ),
        ],
    }
)

nav.run()
