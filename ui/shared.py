import os
import re
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
from server import protocol  # #75: the duplicate-uid reason vocabulary
from server import profiles  # #134: profile icons and defaults come from the dictionary
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
# #46: the project's release-email Worker is the default because the project
# controls it. `UPDATE_SUBSCRIBE_URL` overrides it for a fork or a self-hosted
# list, and `off` (or `none` / `false` / `disabled`) hides the box.
DEFAULT_UPDATE_SUBSCRIBE_URL = "https://opamp-updates.tame-lamp8409.workers.dev"


def resolve_update_subscribe_url(raw):
    value = (raw or "").strip()
    if value.lower() in ("off", "none", "false", "disabled"):
        return ""
    return value or DEFAULT_UPDATE_SUBSCRIBE_URL


UPDATE_SUBSCRIBE_URL = resolve_update_subscribe_url(
    os.environ.get("UPDATE_SUBSCRIBE_URL")
)


def get_server_url():
    return SERVER_URL


#: Where the snippet reads the agent credential from. An environment variable, not a
#: literal: the token must not land in the config file (git) and `configopaque` keeps
#: it out of the agent's own effective-config report, which this server stores.
AGENT_TOKEN_ENV = "OPAMP_AGENT_TOKEN"

#: The only mode the server accepts (#125). `unknown` is not one of them — it is what
#: this module reports when the server cannot be asked or answers with a mode this UI
#: does not support, so the UI never claims "no token needed" (#124).
AGENT_AUTH_MODES = ("token",)
AGENT_AUTH_UNKNOWN = "unknown"


def get_health():
    """`GET /health` as a dict, or `{}` when the server cannot be reached."""
    try:
        health_resp = requests.get(f"{SERVER_URL}/health", timeout=5)
        return health_resp.json() if health_resp.status_code == 200 else {}
    except Exception:
        return {}


#: Distinguishes "no payload supplied, go and ask" from "the payload said nothing".
#: `None` cannot do that job: a page passes the health dict it already has, and a test
#: must be able to pass an empty one without a network call.
_UNSET = object()


def config_push_state(health_data=_UNSET):
    """The server's public `config_push` posture block, from `GET /health` (#131).

    `enabled` is `OPAMP_CONFIG_PUSH_ENABLED` and `insecure` means it is on despite an
    unsafe posture. Read from `/health` rather than `/config/posture` on purpose: the
    agent detail screen renders for a viewer who has not signed in, and `/config/posture`
    needs the `viewer` role, so asking for a password to decide whether a box is editable
    would make reading an agent's config depend on signing in. `{}` when the server
    cannot be asked, which reads as "push off" — a missing answer never conjures an
    editor for a server that may refuse it.
    """
    data = get_health() if health_data is _UNSET else health_data
    return dict((data or {}).get("config_push") or {})


def agent_auth_mode(health_data=_UNSET):
    """The server's `OPAMP_AGENT_AUTH` mode, from the public posture block (#96).

    `GET /health` publishes it as `config_push.agent_auth`, so the UI can render the
    right connect snippet without a password. A server we cannot ask is `unknown`, not
    `off`: assuming "open" is exactly how a token deployment ends up pasting a config
    that 401s forever (#124).
    """
    data = get_health() if health_data is _UNSET else health_data
    mode = ((data or {}).get("config_push") or {}).get("agent_auth")
    return mode if mode in AGENT_AUTH_MODES else AGENT_AUTH_UNKNOWN


def connect_snippet(
    server_url=None,
    *,
    profile=None,
    agent_name="my-agent",
    in_container=False,
    agent_auth="token",
    token_literal=None,
):
    """#116/#121: the copy-paste config for a *new* agent, in the profile's own shape.

    The empty fleet is the first thing a new user sees, and "No agents connected. Start
    an OpenTelemetry Collector with OpAMP extension" tells them nothing they can act on.
    The smallest config that actually reports used to be a 40-line f-string in this
    module, which meant every agent type got collector YAML (#121): a FluentBit user was
    handed a file their product does not read.

    The text is now the profile's `onboarding_snippet` (`server/profiles/data/*.yaml`),
    and this function only supplies the two things it cannot know: the endpoint this
    server is reachable at, and which credential block the server's mode needs. A
    profile with no onboarding snippet returns `""`, and its `bootstrap_snippet` — the
    fragment its product documents — is shown instead. Neither is invented here.

    The endpoint host is the one thing that genuinely changes between deployments: an
    agent on the same machine can use `localhost`, a containerised one cannot (it
    resolves inside its own network namespace), so `in_container` swaps in the host
    alias Docker Desktop provides and Linux can be told about.

    Agent authentication is mandatory (#125), so the snippet always carries the
    credential block. `token_literal` (#126) is a token that was *just minted on this
    page*: the operator is pasting it into a file they own, and a snippet they cannot
    run without editing is not a snippet. The `${env:…}` form stays the documented
    practice, and is what the snippet shows whenever no token is passed.
    """
    profile = profile or profiles.bootstrap_default()
    base = (server_url or SERVER_URL).rstrip("/")
    endpoint = f"{base}/v1/opamp"
    if in_container:
        for local in ("127.0.0.1", "localhost", "0.0.0.0"):
            endpoint = endpoint.replace(f"//{local}", "//host.docker.internal")
    credential = (
        token_literal if token_literal else f"${{env:{AGENT_TOKEN_ENV}}}"
    )
    return profile.onboarding_snippet(
        endpoint,
        credential,
        auth=agent_auth == "token",
        agent_name=agent_name,
    ) or profile.bootstrap_snippet(endpoint, credential)


def agent_auth_note(mode):
    """The sentence under the snippet, or `None` when there is nothing to say (#124).

    Wording lives here, not in the two pages that render it, for the same reason the
    snippet does: Help and the empty fleet must not drift. There is no "no token
    needed" answer any more (#125): if the server cannot be asked, or reports a mode
    this UI does not know, the operator is told to add the token rather than handed a
    config that 401s forever.
    """
    if mode == "token":
        return (
            "**This server requires an agent credential**, so the snippet reads "
            f"`{AGENT_TOKEN_ENV}` from the agent's environment. Mint a fleet token "
            "with `opampctl agent-tokens create` — it is not tied to an agent id, so it "
            "works before any agent has connected — and paste it into "
            f"`{AGENT_TOKEN_ENV}` (or the `headers:` block in the snippet). A token can "
            "expire and can cap the number of agents it admits; see docs/agent-tokens.md."
        )
    if mode == AGENT_AUTH_UNKNOWN:
        return (
            "Could not read this server's agent-auth mode from `GET /health`, or it "
            "reported a mode this UI does not support. Agent authentication is "
            "mandatory, so this config still carries a credential: if the endpoint "
            "answers 401, add the `headers:` block from docs/agent-tokens.md."
        )
    return None


#: The expiry presets the token form offers (#126). Keys are what an operator reads,
#: values are what the API takes (`None` = never).
EXPIRY_CHOICES = {"1 day": 1, "7 days": 7, "30 days": 30, "Never": None}

#: The minted token lives in session state until the operator dismisses it. It is never
#: written to disk, and a rerun that loses it means minting another one — which is the
#: honest consequence of the server keeping only a hash.
MINTED_TOKEN_KEY = "minted_agent_token"


def mint_agent_token_form(
    key_prefix,
    *,
    in_container=False,
    show_snippet=True,
    intro=None,
):
    """Mint a fleet token and show it once — the one implementation (#126).

    Two pages need this and for the same reason: an operator with no token cannot do
    anything else. The empty-fleet screen needs it to make the connect snippet runnable
    (the old page printed a placeholder credential and the agent 401'd forever), and the
    Admin page needs it because revoking a token used to leave no way to mint a
    replacement — the form only existed inside the empty-fleet branch, which is
    unreachable the moment one agent has ever connected.

    The form is the same form in both places, so the safe defaults do not depend on
    which page an operator landed on.

    After a successful mint the page reruns so the form is replaced by the token. Every
    page that renders this form is now a page in its own right (no tabs), so that rerun
    cannot land the operator somewhere else — which is what used to hide the token.
    """
    minted = st.session_state.get(MINTED_TOKEN_KEY)
    if minted is None:
        if intro:
            st.markdown(intro)

        # The gate comes **before** the form, not inside the submit branch. Gating on
        # submit ran `require_admin` after the operator had clicked, which rendered the
        # sign-in form and stopped the run: the POST never happened, the label and
        # expiry they had typed were thrown away, and after signing in they were back
        # at an empty form with no token. Click, sign in, nothing, click again — that
        # loop is why the gate is here. A form nobody can submit is not offered.
        require_admin(ROLE_ADMIN)

        with st.form(f"{key_prefix}_mint_token"):
            label = st.text_input("Label", value="", placeholder="prod")
            expiry = st.selectbox(
                "Expires",
                list(EXPIRY_CHOICES),
                index=1,
                help="A leaked token is only useful until it expires. 'Never' is for a "
                "token you rotate by hand.",
            )
            max_agents = st.number_input(
                "Max agents",
                min_value=1,
                max_value=10000,
                value=15,
                help="The agent over this limit is refused and logged, so a leaked token "
                "cannot consume the server.",
            )
            submitted = st.form_submit_button("Generate token", type="primary")
        if not submitted:
            return

        # No second auth check: a submit is a fresh run, so the gate above already ran
        # for it and returned the session that made this form reachable.
        body = {"label": label, "max_agents": int(max_agents)}
        days = EXPIRY_CHOICES[expiry]
        if days is not None:
            body["expires_in_days"] = days
        response = admin_request(
            "POST", "/agent-tokens", required_role=ROLE_ADMIN, json=body
        )
        if response is None:
            st.error("Could not reach the server to mint a token.")
            return
        if response.status_code != 201:
            st.error(f"Minting a token failed: {response.text}")
            return
        st.session_state[MINTED_TOKEN_KEY] = response.json()
        st.rerun()
        return

    st.success(
        "Token created. **Copy it now** — the server stores only its hash, so this "
        "is the one and only time it is shown."
    )
    st.code(minted["token"], language="text")
    if show_snippet:
        snippet = connect_snippet(
            get_server_url(),
            in_container=in_container,
            agent_auth="token",
            token_literal=minted["token"],
        )
        st.code(snippet, language="yaml")
        st.download_button(
            "Download config.yaml",
            snippet,
            file_name="config.yaml",
            mime="text/yaml",
            key=f"{key_prefix}_download",
        )
    cap = minted.get("max_agents")
    st.caption(
        f"Token `{minted.get('token_id')}` · expires "
        + (minted.get("expires_at") or "never")
        + " · max agents "
        + (str(cap) if cap is not None else "unlimited")
        + ". Revoke it on the **Admin** page if it leaks. The snippet embeds the "
        f"token because you are pasting it into a file you own; `{AGENT_TOKEN_ENV}` "
        "(Help page) keeps it out of the file, which is better once it works."
    )
    if st.button("Done", key=f"{key_prefix}_dismiss_token"):
        st.session_state.pop(MINTED_TOKEN_KEY, None)
        st.rerun()


def agent_token_state(token):
    """One token's state as a word: `revoked`, `expired`, `at cap` or `active`.

    Ordered by what stops a connection first: a revoked token is dead however fresh it
    is, and an expired one is dead even if it has slots left. `at cap` is not a failure
    of the token — it is full — and an operator watching a fleet needs to tell those
    apart without reading `agents_used` against `max_agents`.
    """
    if token.get("revoked_at"):
        return "revoked"
    if token.get("expired"):
        return "expired"
    cap = token.get("max_agents")
    if cap is not None and int(token.get("agents_used") or 0) >= int(cap):
        return "at cap"
    return "active"


def agent_token_rows(tokens):
    """The Admin table: one row per token, never a token or a hash (#126)."""
    rows = []
    for token in tokens or []:
        cap = token.get("max_agents")
        rows.append(
            {
                "State": agent_token_state(token),
                "Label": token.get("label") or "—",
                "Token": token.get("token_id") or "",
                "Bound to": token.get("agent_id") or "fleet",
                "Expires": format_local_time(token.get("expires_at"))
                if token.get("expires_at")
                else "never",
                "Agents": (
                    f"{token.get('agents_used') or 0} of {cap}"
                    if cap is not None
                    else f"{token.get('agents_used') or 0} of unlimited"
                ),
                "Last used": format_local_time(token.get("last_seen_at"))
                if token.get("last_seen_at")
                else "never",
            }
        )
    return rows


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


def response_payload(response, default=None):
    """A response body as JSON, tolerating an empty or non-JSON body."""
    if response is None:
        return default
    try:
        return response.json()
    except Exception:
        return default


def response_detail(response, default="the request was refused"):
    """The server's `detail`, as text an operator can read instead of a Python repr.

    A refusal is not always one sentence: `POST /config/revisions` answers 422 with
    `{"message", "errors": [{path, message, severity}], "warnings": [...]}`, and
    `str(detail)` renders that as a dict on one line. Flattening it puts the message and
    each finding on its own line, which is what the pages show (#131).
    """
    if response is None:
        return default
    body = response_payload(response, None)
    if body is None:
        text = (getattr(response, "text", "") or "").strip()
        return text or default
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        lines = []
        if detail.get("message"):
            lines.append(str(detail["message"]))
        for finding in detail.get("errors") or []:
            if isinstance(finding, dict):
                lines.append(
                    f"{finding.get('path') or 'config'}: {finding.get('message') or ''}"
                )
            else:
                lines.append(str(finding))
        if detail.get("hint"):
            lines.append(str(detail["hint"]))
        return "\n".join(lines) or json_module.dumps(detail, indent=2)
    if detail:
        return str(detail)
    return json_module.dumps(body, indent=2)


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


def description_attribute(description, *keys):
    """First scalar value among `keys` in an agent's description, or None.

    The API serialises `AgentState.description` as camelCase **lists** of
    `{key, value: {stringValue}}` entries (`identifyingAttributes` +
    `nonIdentifyingAttributes`, #75). A plain mapping is also accepted, because
    that is what a hand-written test fixture tends to be — but the list shape is
    the real one, and reading only the mapping shape is how the Remote Config
    blast radius silently said "3 × unknown" while the data was right there (#104).
    """
    description = description or {}
    for field in (
        "identifyingAttributes",
        "nonIdentifyingAttributes",
        "identifying_attributes",
        "non_identifying_attributes",
    ):
        attrs = description.get(field)
        if isinstance(attrs, dict):
            for key in keys:
                if attrs.get(key):
                    return attrs[key]
        elif isinstance(attrs, list):
            for entry in attrs:
                if not isinstance(entry, dict) or entry.get("key") not in keys:
                    continue
                value = entry.get("value")
                if isinstance(value, dict):
                    for value_key in ("stringValue", "string_value"):
                        if value.get(value_key):
                            return value[value_key]
                elif value:
                    return str(value)
    return None


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


UPDATE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
RELEASES_URL = "https://github.com/agardnerIT/opamp-server-py/releases"


def update_commands(latest_tag):
    """Copyable commands for a validated release tag. Never trust the feed blindly."""
    if not isinstance(latest_tag, str):
        return []
    tag = latest_tag.strip()
    if not UPDATE_TAG_RE.fullmatch(tag):
        return []
    version = tag.lstrip("v")
    checkout = tag if tag.startswith("v") else f"v{tag}"
    return [
        f"docker pull ghcr.io/agardnerit/opamp-server-py-server:{version}",
        f"pip install --upgrade opamp-server-py=={version}",
        f"git fetch --tags && git checkout {checkout}",
    ]


@st.cache_data(ttl=60)
def get_updates():
    """Cached `GET /updates`. No network from the browser; one read per minute."""
    try:
        response = requests.get(f"{SERVER_URL}/updates", timeout=5)
        return response.json() if response.status_code == 200 else {}
    except Exception:
        return {}


def render_update_notice():
    """Sidebar notice: current version, and an update banner when one exists."""
    state = get_updates()
    current = state.get("current_version")
    if not current:
        return
    latest_tag = state.get("latest_tag")
    if state.get("update_available") and latest_tag:
        st.warning(f"Update available: **{latest_tag}** (you are on {current})")
        with st.expander("How to upgrade"):
            for command in update_commands(latest_tag):
                st.code(command, language="bash")
            st.markdown(f"[Release notes]({RELEASES_URL})")
    elif state.get("enabled") is False:
        st.caption(f"v{current} · update checks off")
    else:
        st.caption(f"v{current}")


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


#: How much of an agent id this dashboard shows when the agent declared no name (#137).
#: Unchanged from the pre-#137 fleet table, so an unnamed agent does not become *less*
#: identifiable because a named one became readable.
AGENT_ID_LABEL_LENGTH = 32


def agent_display_name(agent, id_length=AGENT_ID_LABEL_LENGTH):
    """The label for one agent: its declared `agent.name`, else an abbreviated id (#137).

    `name` is the server's field (`GET /agents`, derived from `agent.name`) and is
    `None` when the agent declared none. A label and only a label: every link, selector
    and API call still addresses `id`, because a name is a string the agent wrote in its
    own config — two agents may choose the same one, and a restart may change it.
    """
    agent = agent or {}
    name = str(agent.get("name") or "").strip()
    if name:
        return name
    agent_id = str(agent.get("id") or "")
    return agent_id[:id_length] + ("..." if len(agent_id) > id_length else "")


def agent_labels(agents, id_length=AGENT_ID_LABEL_LENGTH):
    """`{id: label}` for a fleet, with colliding names disambiguated (#137).

    The server does not enforce unique names: `agent.name` is a label an agent wrote in
    its own config, and refusing a second collector called `collector` would be a fleet
    constraint the agent cannot see. So two rows may share a name — and a table showing
    "collector" twice with two different link targets is one an operator has to click to
    tell apart. A repeated name gets `#` plus the first 8 characters of the id, which is
    what the id looks like everywhere else.
    """
    agents = list(agents or [])
    counts = {}
    for agent in agents:
        name = str((agent or {}).get("name") or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    labels = {}
    for agent in agents:
        agent = agent or {}
        label = agent_display_name(agent, id_length=id_length)
        if str(agent.get("name") or "").strip() and counts.get(label, 0) > 1:
            label = f"{label} #{str(agent.get('id') or '')[:8]}"
        labels[str(agent.get("id") or "")] = label
    return labels


def agent_type_badge(agent):
    """How the server reads an agent's type, and how sure it is (#118).

    `otelcol` / `generic` with the *provenance* in brackets: an operator's pin is a
    fact, a declaration is the agent's claim, and a sniff is inference. The badge is
    the whole point of keeping `agent_type_source` — a wrong guess has to be visible
    before it becomes a config push.

    #134: the glyph is the profile's `icon`, not a `if name == "otelcol"` branch —
    that branch was the reason a new agent type needed a UI release. `📦` stays as the
    fallback for a name this build does not have (a downgrade, a hand-edited row).
    """
    if not agent:
        return "⚪ unknown"
    name = agent.get("agent_type") or "unknown"
    source = agent.get("agent_type_source") or ""
    profile = profiles.get(name)
    icon = (profile.icon if profile is not None else "") or "📦"
    label = f"{icon} {name}"
    if source and source != "fallback":
        label += f" ({source})"
    # #134/#121: several profiles matched and this one won on priority. A tie is a
    # decision nobody can audit, so the badge says so in one glyph — the detail page
    # spells out the candidates (`agent_type_ambiguous`), because a table cell cannot.
    if agent.get("agent_type_ambiguous"):
        label += " ⚠️"
    return label


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

        st.markdown("**Version**")
        render_update_notice()

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


def reissue_banner_text(reissued_from, reason=None):
    """The sentence shown when the server renamed an agent's `instance_uid` (#75).

    Returns `None` for every agent whose uid was never re-issued, so the page reads
    `if text := reissue_banner_text(...)`. Wording lives here rather than in the page
    so it is testable without rendering Streamlit, and the reason vocabulary comes
    from `server.protocol` so the UI cannot invent a second set of names.
    """
    if not reissued_from:
        return None
    reason_text = {
        protocol.REASON_HOST_IDENTITY: (
            "two agents reported different host identities under the same uid"
        ),
        protocol.REASON_SEQUENCE_REGRESSION: (
            "its status counter kept going backwards, which two agents alternating "
            "on one uid produce"
        ),
    }.get(reason, reason or "duplicate instance_uid")
    return (
        f"**instance_uid re-issued by the server.** This agent presented "
        f"`{reissued_from}`, which another agent is using ({reason_text}). "
        "It keeps reporting under the id above; counted in "
        "`opamp_duplicate_instance_uid_total`."
    )
