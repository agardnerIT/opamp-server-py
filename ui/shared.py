import os
import sys
import json as json_module
import yaml
from pathlib import Path
import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.config_view import display_config_yaml  # #68: streamlit-free, so it is testable
from ui import session as admin_session  # #105: streamlit-free session/token logic
from ui.session import (  # re-exported so pages import one module
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    ROLES,
    AdminSession,
    AuthError,
)
from server.manifest import generate_manifest, generate_ocb_command
from server.posture import posture_warning
from server.reports import (  # re-exported: report logic lives in server/reports.py
    generate_agent_report,
    generate_heavy_collectors_report,
    generate_outdated_collectors_report,
    _count_outdated_collectors,
    parse_version,
    _is_heavy,
)

SERVER_HTTP_SCHEME = os.environ.get("SERVER_HTTP_SCHEME", "http")
SERVER_ADDRESS = os.environ.get("SERVER_ADDRESS", "localhost")
SERVER_PORT = os.environ.get("SERVER_PORT", "4320")
SERVER_URL = f"{SERVER_HTTP_SCHEME}://{SERVER_ADDRESS}:{SERVER_PORT}"


def get_server_url():
    return SERVER_URL


def format_local_time(utc_str):
    if not utc_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str[:19] if len(utc_str) > 19 else utc_str


def get_auth_status():
    """`GET /auth/status` — unauthenticated, no secrets (#95).

    `password_required` is really "a credential is configured"; when it is False the
    server is in **read-only mode** (reads open, every privileged write 403).
    """
    try:
        resp = requests.get(f"{SERVER_URL}/auth/status", timeout=5)
        return resp.json()
    except Exception:
        return {"password_required": False, "auth_mode": "unknown"}


# --------------------------------------------------------------- admin session
#
# #105: the UI holds an opaque **session token** from `POST /auth/session`, never the
# password. Everything below is the one implementation of "prompt, log in, call an
# admin endpoint, handle 401/403/429" — the copies that used to live in
# `pages/3_Admin.py`, `pages/1_Agents.py` and `prompt_for_password()` are gone.
#
# The password is used once, inside `admin_session.login()`, and is never written to
# `st.session_state`. `_forget_legacy_password()` deletes the old keys so a browser
# tab that survived the upgrade cannot keep re-sending HTTP Basic.

SESSION_KEY = "admin_session"
LOGIN_FLASH_KEY = "admin_login_flash"
SIDEBAR_FLASH_KEY = "admin_sidebar_flash"
REAUTH_KEY = "admin_reauth"
LEGACY_PASSWORD_KEYS = (
    "admin_password",
    "admin_password_input",
    "admin_password_input_main",
    "admin_password_error",
    "admin_password_attempt",
)


def _forget_legacy_password():
    """Drop any password left in session state by a pre-#105 UI."""
    for key in LEGACY_PASSWORD_KEYS:
        st.session_state.pop(key, None)


def _set_flash(key, message, level="info"):
    """Queue a one-shot message.

    `st.rerun()` straight after `st.error()` clears the error (`memory.md`), so
    messages that must survive a rerun — a failed login, an expired session, a
    confirmation — are queued and rendered on the next pass instead.
    """
    st.session_state[key] = {"message": message, "level": level}


def _render_flash(key):
    flash = st.session_state.pop(key, None)
    if not flash:
        return
    render = getattr(st, flash.get("level") or "info", st.info)
    render(flash.get("message") or "")


def get_session():
    """The current `AdminSession`, or None. Never a password."""
    _forget_legacy_password()
    session = st.session_state.get(SESSION_KEY)
    return session if isinstance(session, AdminSession) else None


def set_session(session):
    if not isinstance(session, AdminSession):
        raise TypeError("set_session() expects an AdminSession")
    st.session_state[SESSION_KEY] = session
    _forget_legacy_password()
    return session


def clear_session():
    st.session_state.pop(SESSION_KEY, None)
    _forget_legacy_password()


def get_auth_headers():
    """`Authorization: Bearer <token>` for the current session, or `{}`.

    HTTP Basic is deliberately not reachable from here any more: a password on every
    request is exactly what #105 removed.
    """
    session = get_session()
    if session is None or session.expired():
        return {}
    return admin_session.session_headers(session.token)


def _login_form(status, required_role, replacing=None):
    """The **only** login form in the UI. Renders and `st.stop()`s until signed in."""
    attempts = int(st.session_state.get("admin_login_attempts", 0) or 0)
    principals_mode = (status or {}).get("auth_mode") == "principals"

    st.markdown("**🔒 Admin sign-in required**")
    if replacing is not None:
        st.caption(
            "Sign in again to extend this session — it is an audited re-auth and the "
            "current token is revoked once the new one is issued."
        )
    st.caption(
        f"This action needs the **{required_role}** role. The password is exchanged "
        "for a short-lived token and is never kept in this browser session."
    )
    _render_flash(LOGIN_FLASH_KEY)

    with st.form(f"admin_login_form_{attempts}"):
        name = ""
        if principals_mode:
            name = st.text_input("Name", key=f"admin_login_name_{attempts}")
        else:
            st.caption(
                "The shared `ADMIN_PASSWORD` is configured, so any name (or none) will "
                "do. Create a principal for per-person identity and roles."
            )
        password = st.text_input(
            "Password", type="password", key=f"admin_login_password_{attempts}"
        )
        submitted = st.form_submit_button("Sign in", type="primary")

    if replacing is not None and st.button("Cancel", key=f"admin_login_cancel_{attempts}"):
        # Abandon the re-auth: keep the session the user already had. It expires on
        # its own TTL, which is what extending was meant to postpone, not prevent.
        st.session_state.pop(REAUTH_KEY, None)
        st.rerun()

    if not submitted:
        return None

    if not password:
        st.session_state["admin_login_attempts"] = attempts + 1
        _set_flash(LOGIN_FLASH_KEY, "Enter your password.", "error")
        st.rerun()

    try:
        session = admin_session.login(SERVER_URL, name, password)
    except AuthError as exc:
        # The password is dropped here: it was never stored, and this local goes out
        # of scope on the rerun. Only the reason survives.
        st.session_state["admin_login_attempts"] = attempts + 1
        _set_flash(LOGIN_FLASH_KEY, exc.human(), "error")
        st.rerun()
    else:
        if replacing is not None:
            # Extend = re-auth: the superseded token is revoked, so a stolen copy of
            # it dies at the same moment the new session begins.
            admin_session.logout(SERVER_URL, replacing.token)
        st.session_state.pop(REAUTH_KEY, None)
        set_session(session)
        st.session_state["admin_login_attempts"] = attempts + 1
        _set_flash(
            LOGIN_FLASH_KEY,
            f"Signed in as {session.label()} ({session.expiry_label()}).",
            "success",
        )
        st.rerun()
    return None


def _read_only_notice():
    st.info(
        "This server has no admin credential configured, so it is **read-only**. "
        "Reads are open and every privileged action is refused. Set `ADMIN_PASSWORD` "
        "(deprecated) or create a principal with `opampctl auth principals create`, "
        "then restart the server."
    )


def require_admin(role=ROLE_OPERATOR):
    """Return an `AdminSession` with at least `role`, or render sign-in and `st.stop()`.

    Called at the top of any page that performs a privileged action. Important: this
    is **cosmetics plus ergonomics**, not the security boundary — the server
    re-authorises every request, and hides nothing from a caller who skips the UI
    entirely.

    Set `st.session_state["admin_reauth"] = True` to force a fresh sign-in ("Extend").
    The flag is read, **not** popped, here: a pop would consume the intent on the
    first render, and the form's own submit would then land back on the authenticated
    page without re-authenticating. It is cleared when the re-auth succeeds (or the
    user cancels).
    """
    _forget_legacy_password()
    reauth = bool(st.session_state.get(REAUTH_KEY))
    session = get_session()

    if session is not None and not reauth:
        if session.expired():
            clear_session()
            _set_flash(
                LOGIN_FLASH_KEY,
                "Your admin session expired. Sign in again to continue.",
                "warning",
            )
        elif not session.can(role):
            st.error(
                f"Your role is **{session.role}** and this page needs **{role}**. "
                "Ask an admin for a higher role — the server enforces the same rule."
            )
            st.caption("Use **Sign out** in the sidebar to sign in as a different user.")
            st.stop()
        else:
            # A queued "signed in as …" / "session extended" message has no form to
            # render into once the session is live, so show it here, exactly once.
            _render_flash(LOGIN_FLASH_KEY)
            return session

    status = get_auth_status()
    if not (status or {}).get("password_required"):
        _read_only_notice()
        st.stop()

    try:
        required = admin_session.normalize_role(role)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    _login_form(status, required, replacing=session if reauth else None)
    st.stop()


def admin_request(method, path, required_role="", timeout=10, **kwargs):
    """Call an admin endpoint with the session token, handling 401/403/429 centrally.

    Return value is the `requests.Response` for success **and** for recoverable
    failures the caller may want to explain (`4xx` other than auth); `None` when the
    request never happened (no session, transport error). A 401/429 rerun ends the
    page, so the caller never sees those.
    """
    session = get_session()
    if session is None or session.expired():
        # Not a `st.rerun()`: this function can sit at page top level (the Admin page),
        # and a rerun there would re-enter it with no session and loop forever. The
        # sign-in form stops the page instead, which is what the caller wanted anyway.
        clear_session()
        _set_flash(LOGIN_FLASH_KEY, "Sign in to continue.", "error")
        require_admin(required_role or ROLE_VIEWER)
        return None

    headers = dict(kwargs.pop("headers", None) or {})
    headers.update(admin_session.session_headers(session.token))
    url = path if str(path).startswith("http") else f"{SERVER_URL}{path}"

    try:
        response = requests.request(
            method, url, headers=headers, timeout=timeout, **kwargs
        )
    except Exception as exc:
        st.error(f"Could not reach the server: {exc}")
        return None

    try:
        payload = response.json()
    except Exception:
        payload = None

    outcome = admin_session.classify_response(
        response.status_code, response.headers, payload, required_role=required_role
    )
    if outcome.ok:
        return response

    if outcome.relogin:
        clear_session()
        _set_flash(LOGIN_FLASH_KEY, outcome.message, "error")
        st.rerun()

    st.error(outcome.message)
    return response


def logout(sign_out_all=False):
    """Sign out. With `sign_out_all`, also revoke every session of this principal.

    "Every session" is `GET /auth/sessions` + `DELETE /auth/sessions/{id}`, which
    needs `admin`; for a lower role the UI says so rather than silently doing less.
    Revoking *another person* is a principal revocation, not a sign-out button.
    """
    session = get_session()
    if session is None:
        clear_session()
        st.rerun()
        return

    revoked_others = 0
    note = ""
    note_level = "success"
    if sign_out_all:
        try:
            listing = requests.get(
                f"{SERVER_URL}/auth/sessions",
                headers=admin_session.session_headers(session.token),
                timeout=5,
            )
        except Exception:
            listing = None
        if listing is not None and listing.status_code == 200:
            try:
                payload = listing.json()
            except Exception:
                payload = {}
            for record in admin_session.sessions_of(session, payload):
                try:
                    revoked = requests.delete(
                        f"{SERVER_URL}/auth/sessions/{record['session_id']}",
                        headers=admin_session.session_headers(session.token),
                        timeout=5,
                    )
                except Exception:
                    continue
                if 200 <= revoked.status_code < 300:
                    revoked_others += 1
        elif listing is not None and listing.status_code in (401, 403):
            note = (
                "Only an **admin** can list and revoke other sessions; you were signed "
                "out of this one only."
            )
            note_level = "warning"

    admin_session.logout(SERVER_URL, session.token)
    clear_session()
    if revoked_others:
        # Our own session is in that listing too (it is counted before it is revoked),
        # so say so rather than overclaiming "other" sessions.
        _set_flash(
            SIDEBAR_FLASH_KEY,
            f"Signed out. Revoked {revoked_others} session(s) for {session.name}, "
            "including this one.",
            "success",
        )
    elif note:
        _set_flash(SIDEBAR_FLASH_KEY, f"Signed out. {note}", note_level)
    else:
        _set_flash(SIDEBAR_FLASH_KEY, "Signed out.", "success")
    st.rerun()


def render_identity_panel():
    """Sidebar: who you are, what your role is, when the session dies, sign out.

    The role is shown on purpose (#105): a missing button is confusing, a labelled
    role is not.
    """
    _forget_legacy_password()
    session = get_session()
    st.markdown("**Admin Session**")

    if session is None:
        status = get_auth_status()
        if (status or {}).get("password_required"):
            st.caption("Not signed in — sign in on the Admin page.")
        else:
            st.caption("Read-only mode (no admin credential configured).")
        _render_flash(SIDEBAR_FLASH_KEY)
        return

    if session.expired():
        clear_session()
        _set_flash(
            LOGIN_FLASH_KEY,
            "Your admin session expired. Sign in again on the Admin page.",
            "warning",
        )
        st.rerun()

    st.caption(f"{session.name} · **{session.role}**")
    st.caption(session.expiry_label())
    if st.button("Sign out", key="sidebar_sign_out"):
        logout()
    if session.can(ROLE_ADMIN) and st.button(
        "Sign out everywhere", key="sidebar_sign_out_all"
    ):
        logout(sign_out_all=True)
    _render_flash(SIDEBAR_FLASH_KEY)


def get_agents():
    try:
        response = requests.get(f"{SERVER_URL}/agents", timeout=5)
        return response.json()
    except Exception as e:
        return {"agents": [], "count": 0, "error": str(e)}


def get_agent(agent_id):
    try:
        response = requests.get(f"{SERVER_URL}/agent/{agent_id}", timeout=5)
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def _compliance_badge(compliance):
    if compliance is None:
        return "⚪"
    if compliance.get("opa_disabled"):
        return "⚪"
    if compliance.get("compliant") is True:
        return "✅"
    if compliance.get("compliant") is False:
        return "❌"
    return "⚪"


@st.cache_data(ttl=3600)
def get_collector_versions():
    try:
        import urllib.request
        url = "https://api.github.com/repos/open-telemetry/opentelemetry-collector-releases/tags?per_page=250"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read().decode()
            tags = json_module.loads(data)
            versions = []
            for tag in tags:
                name = tag.get("name", "")
                if name and "-nightly" not in name:
                    versions.append(name.lstrip("v"))
            return versions if versions else ["0.149.0"]
    except Exception:
        return ["0.149.0"]


@st.cache_data(ttl=3600)
def get_latest_collector_version():
    versions = get_collector_versions()
    return versions[0] if versions else "0.149.0"


def show_slim_distro_builder_page(comps):
    st.markdown("### What is this?")
    st.markdown("""
    This tool analyzes your collector's components and generates a **manifest.yaml** for the 
    OpenTelemetry Collector Builder (OCB). Use OCB to build a slim collector 
    binary containing ONLY the components you actually use.
    """)
    
    st.markdown("### What is OCB?")
    st.markdown("""
    The **OpenTelemetry Collector Builder (OCB)** is a CLI tool that builds custom
    collector binaries from a manifest. [Learn more](https://opentelemetry.io/docs/collector/extend/ocb/)
    """)
    
    st.markdown("### How to use")
    st.markdown("""
    1. Copy the manifest.yaml below
    2. Install OCB: `go install go.opentelemetry.io/collector/cmd/builder@latest`
    3. Run: `ocb build --config manifest.yaml`
    4. Your slim collector is in `./_build/`
    
    **Docker alternative:** `docker run --rm -v $(pwd):/workspace ghcr.io/open-telemetry/otel-collector-builder --config manifest.yaml`
    """)
    
    st.divider()
    
    version = st.text_input("Collector version", value="1.0.0", key="slim_version")
    distro_manifest = generate_manifest(comps, version)
    
    st.markdown("#### manifest.yaml")
    st.code(distro_manifest, language="yaml")
    
    st.download_button(
        "Download manifest.yaml",
        distro_manifest,
        file_name="manifest.yaml",
        mime="text/yaml",
        key="download_manifest"
    )
    
    st.markdown("#### OCB Command")
    st.code(generate_ocb_command(version), language="bash")


def show_feedback_dialog():
    st.session_state.feedback_submitted = False
    st.session_state.feedback_error = None
    st.session_state.show_slim_distro_builder = False
    st.session_state.show_policies_modal = False
    
    @st.dialog("Feedback")
    def dialog_content():
        st.markdown("We'd love to hear your feedback!")
        title = st.text_input("Title", placeholder="Brief description...", key="feedback_title")
        feedback = st.text_area("Your feedback", height=150, key="feedback_text")

        if st.session_state.get("feedback_error") == "title":
            st.warning("Please enter a title first.")
        elif st.session_state.get("feedback_error") == "content":
            st.warning("Please enter some feedback first.")

        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Submit", type="primary", key="submit_feedback"):
                if title.strip() and feedback.strip():
                    ntfy_url = "https://ntfy.sh/agardnerit-opamp-server-py-feedback"
                    message = f"# {title}\n\n**Feedback:**\n{feedback}"
                    try:
                        requests.post(ntfy_url, data=message.encode("utf-8"), headers={"Content-Type": "text/plain"})
                        st.session_state.feedback_submitted = True
                    except Exception as e:
                        st.error(f"Failed to send feedback: {e}")
                        st.rerun()
                elif not title.strip():
                    st.session_state.feedback_error = "title"
                    st.rerun()
                else:
                    st.session_state.feedback_error = "content"
                    st.rerun()

        with col2:
            if st.button("Close", key="close_feedback"):
                st.session_state.show_feedback = False
                st.session_state.feedback_submitted = False
                st.rerun()

        if st.session_state.get("feedback_submitted"):
            st.success("Thank you for your feedback!")
            if st.button("Done", key="dismiss_feedback"):
                st.session_state.show_feedback = False
                st.session_state.feedback_submitted = False
                st.rerun()

    dialog_content()


def render_sidebar():
    # Hide default "app" pages header
    st.markdown("""
    <style>
    [data-testid="stSidebarNav"] > div:first-child > div:first-child {
        display: none;
    }
    </style>
    """, unsafe_allow_html=True)
    
    ui_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logo_path = f"{ui_dir}/ui/otel-logo.png"
    
    data = get_agents()
    st.session_state["agents_data"] = data

    health_data = {}
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        health_data = health_resp.json() if health_resp.status_code == 200 else {}
        opa_available = health_data.get("opa_enabled", False)
    except Exception:
        opa_available = False
    
    with st.sidebar:
        if os.path.exists(logo_path):
            st.image(logo_path, width=200)

        render_identity_panel()

        st.markdown("**Server Status**")
        status = "🟢 Online" if "error" not in data else "🔴 Offline"
        st.caption(status)
        
        if opa_available:
            st.markdown("**Open Policy Agent Status**")
            st.caption("🟢 Available")
        
        st.markdown("**Agents Connected**")
        st.caption(f"{data.get('count', 0)}")

    render_posture_banner(health_data)


def render_posture_banner(health_data):
    """Permanent, unmissable banner while config push runs on an insecure posture.

    Reads the *public* posture block of `GET /health`, so it renders on every page
    of the UI without a password and cannot be dismissed (#96). A server that is
    one flag away from letting anyone rewrite fleet configuration should never
    look normal. Wording lives in `server/posture.py` so it is testable.
    """
    warning = posture_warning((health_data or {}).get("config_push") or {})
    if warning:
        st.error(warning)
