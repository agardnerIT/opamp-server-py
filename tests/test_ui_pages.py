"""#105: source-level guards on the Streamlit UI.

The pages are not unit-tested (there is no Streamlit in the test environment, and
driving one headless would still not tell us the thing that matters here). What the
issue actually asks for is *absence*: no page may grow its own login form, no page may
hold the password in session state, and no privileged call may bypass the one transport
that understands 401/403/429. Absence is exactly what a grep test can prove, so that is
what these are — regression fences, not coverage.

If one of these fails, the fix is almost never to relax the test.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
REPO = UI_DIR.parent

PAGES = sorted(UI_DIR.glob("pages/*.py"))
UI_MODULES = sorted(p for p in UI_DIR.glob("*.py"))
ALL_UI_FILES = UI_MODULES + PAGES

ADMIN_PAGE = UI_DIR / "pages" / "3_Admin.py"
AGENTS_PAGE = UI_DIR / "pages" / "1_Agents.py"
SHARED = UI_DIR / "shared.py"
SESSION = UI_DIR / "session.py"

# Routes that require a role. A bare `requests.*` call to one of these is the bug
# this issue exists to prevent: it would skip the shared 401/403/429 handling.
PRIVILEGED_ROUTES = (
    "/alerts",
    "/compliance/check",
    "/compliance/reload",
    "/auth/sessions",
    "/auth/principals",
)


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def lines(path: Path):
    return source(path).splitlines()


def test_the_ui_files_we_think_are_here():
    names = {path.name for path in ALL_UI_FILES}
    assert {"shared.py", "session.py", "1_Agents.py", "3_Admin.py"} <= names


# ------------------------------------------------- "one login implementation"


def test_the_login_form_exists_exactly_once():
    """A password input outside `shared.py` is a second login implementation."""
    offenders = [
        path.relative_to(UI_DIR)
        for path in ALL_UI_FILES
        if path != SHARED and re.search(r"""["']Password["']\s*,\s*type\s*=\s*["']password["']""", source(path))
    ]
    assert offenders == [], f"password form(s) outside ui/shared.py: {offenders}"


def test_shared_owns_require_admin_and_admin_request():
    text = source(SHARED)
    for name in ("def require_admin(", "def admin_request(", "def logout(", "def render_identity_panel("):
        assert name in text, f"ui/shared.py must define {name}"


@pytest.mark.parametrize("page", [ADMIN_PAGE, AGENTS_PAGE])
def test_privileged_pages_use_require_admin(page):
    assert "require_admin(" in source(page), f"{page.name} must gate on require_admin()"


def test_pages_do_not_reimplement_login():
    for page in PAGES:
        text = source(page)
        assert "auth/verify" not in text, f"{page.name} still verifies credentials itself"
        assert "b64encode" not in text, f"{page.name} still builds an HTTP Basic header"
        assert "prompt_for_password" not in text


# --------------------------------------------------- "no password in the UI state"


def test_no_password_is_ever_kept_in_session_state():
    """The only acceptable mention of the legacy keys is the cleanup list."""
    offenders = []
    for path in ALL_UI_FILES:
        for number, line in enumerate(lines(path), start=1):
            lowered = line.lower()
            if "session_state" not in lowered:
                continue
            if "admin_password" in lowered or re.search(r"session_state\[[\"']password", lowered):
                offenders.append(f"{path.relative_to(UI_DIR)}:{number}: {line.strip()}")
    assert offenders == [], f"password stored in session state: {offenders}"


def test_shared_actively_deletes_legacy_password_keys():
    """A tab that survived the upgrade must stop sending HTTP Basic on next render."""
    tree = ast.parse(source(SHARED))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "_forget_legacy_password"
        for node in tree.body
    )

    def calls_cleanup(name):
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name != name:
                continue
            return any(
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", None) == "_forget_legacy_password"
                for inner in ast.walk(node)
            )
        pytest.fail(f"{name} is not defined in ui/shared.py")

    for entry_point in ("get_session", "set_session", "require_admin", "render_identity_panel"):
        assert calls_cleanup(entry_point), f"{entry_point} must call _forget_legacy_password()"


def test_the_session_object_carries_a_token_and_no_password():
    tree = ast.parse(source(SESSION))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AdminSession":
            fields = [
                target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                for target in [statement.target]
                if isinstance(target, ast.Name)
            ]
            assert "token" in fields
            assert not [name for name in fields if "password" in name.lower()]
            return
    pytest.fail("AdminSession is not defined in ui/session.py")


# -------------------------------------------- "privileged calls use one transport"


@pytest.mark.parametrize("page", ALL_UI_FILES)
def test_no_bare_requests_call_to_a_privileged_route(page):
    offenders = []
    for number, line in enumerate(lines(page), start=1):
        if "requests." not in line or "def " in line:
            continue
        if any(route in line for route in PRIVILEGED_ROUTES):
            offenders.append(f"{page.relative_to(UI_DIR)}:{number}: {line.strip()}")
    assert offenders == [], f"privileged call bypassing admin_request(): {offenders}"


def test_privileged_routes_are_reached_through_admin_request():
    text = source(ADMIN_PAGE)
    assert 'admin_request("GET", "/alerts"' in text
    assert 'admin_request("PUT", "/alerts"' in text
    assert 'admin_request("POST", "/compliance/reload"' in text
    assert 'admin_request("GET", "/auth/sessions"' in text
    assert 'admin_request(\n                        "POST", f"/compliance/check/{agent_id}"' in text


def test_admin_request_is_the_only_place_that_attaches_the_token():
    tree = ast.parse(source(SHARED))
    sites = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                func = inner.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "session_headers":
                    sites.add(node.name)
    assert sites == {"get_auth_headers", "admin_request", "logout"}, (
        f"the token is attached in unexpected places: {sorted(sites)}"
    )
    for page in PAGES:
        assert "session_headers(" not in source(page), "pages must not build auth headers"


# --------------------------------------------------------------- role awareness


def test_admin_only_actions_are_hidden_from_lower_roles():
    text = source(ADMIN_PAGE)
    assert "if session.can(ROLE_ADMIN):" in text
    assert "tab_names.append(\"Sessions\")" in text
    # And they are still declared to the server, so a hidden button is not the boundary.
    assert 'required_role=ROLE_ADMIN' in text
    assert 'required_role=ROLE_OPERATOR' in text


def test_pages_add_the_repository_root_to_sys_path():
    """A page in ui/pages must add the repo root, not ui/, or `from ui.shared` fails."""
    for page in PAGES:
        tree = ast.parse(source(page))
        inserts = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "insert"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
        ]
        assert inserts, f"{page.name} has no sys.path.insert"
        resolved = eval(
            compile(ast.Expression(inserts[0].args[1]), str(page), "eval"),
            {"os": os, "__file__": str(page)},
        )
        assert resolved == str(REPO), (
            f"{page.name} adds {resolved}, not the repository root {REPO}; "
            "`from ui.shared` will fail in the container"
        )


def test_update_notice_and_admin_toggle_are_wired():
    """#46: the sidebar reads /updates, and the Admin page owns the toggle."""
    shared = source(SHARED)
    assert 'requests.get(f"{SERVER_URL}/updates"' in shared
    assert "def update_commands(" in shared
    assert "def render_update_notice(" in shared
    assert "render_update_notice()" in shared

    admin = source(ADMIN_PAGE)
    assert 'tab_names.append("Updates")' in admin
    assert 'admin_tabs[tab_names.index("Updates")]' in admin
    assert "Check for updates automatically" in admin
    assert '"/updates/settings"' in admin
    assert '"/updates/check"' in admin
    assert "deployment_enabled" in admin


def test_update_commands_are_generated_only_from_a_valid_tag():
    pytest.importorskip("streamlit")
    from ui.shared import update_commands

    assert update_commands("v0.2.0") == [
        "docker pull ghcr.io/agardnerit/opamp-server-py-server:0.2.0",
        "pip install --upgrade opamp-server-py==0.2.0",
        "git fetch --tags && git checkout v0.2.0",
    ]
    assert update_commands("0.2.0")[2] == "git fetch --tags && git checkout v0.2.0"
    assert update_commands("v0.2.0-rc.1")[0].endswith(":0.2.0-rc.1")
    for bad in ("latest", "", "<script>", None, "v1.2"):
        assert update_commands(bad) == []


def test_release_email_box_is_config_gated():
    """#46: the Worker URL is opt-in, and the box is an iframe, not a server POST."""
    text = source(ADMIN_PAGE)
    assert 'tab_names.append("Release emails")' in text
    assert "if not UPDATE_SUBSCRIBE_URL:" in text
    assert "components.iframe(UPDATE_SUBSCRIBE_URL" in text


def test_update_subscribe_url_defaults_and_can_be_overridden():
    """#46: the project Worker is the default; env overrides or turns it off."""
    pytest.importorskip("streamlit")
    from ui.shared import (
        DEFAULT_UPDATE_SUBSCRIBE_URL,
        resolve_update_subscribe_url,
    )

    assert DEFAULT_UPDATE_SUBSCRIBE_URL.startswith("https://")
    assert resolve_update_subscribe_url(None) == DEFAULT_UPDATE_SUBSCRIBE_URL
    assert resolve_update_subscribe_url("") == DEFAULT_UPDATE_SUBSCRIBE_URL
    assert (
        resolve_update_subscribe_url(" https://example.test/updates ")
        == "https://example.test/updates"
    )
    for disabled in ("off", "none", "false", "disabled", " OFF "):
        assert resolve_update_subscribe_url(disabled) == ""


def test_sidebar_shows_the_current_role():
    text = source(SHARED)
    assert "session.role" in text
    assert "expiry_label()" in text


def test_reauth_intent_is_read_not_consumed_on_first_render():
    """Regression fence for a bug the render test caught (`test_ui_smoke.py`).

    `require_admin` used to `pop()` the re-auth flag. The first render then dropped the
    form and the flag together, so the form's own submit re-rendered the authenticated
    page and "Extend" silently did nothing. The flag must survive until the re-auth
    succeeds or the user cancels.
    """
    text = source(SHARED)
    assert "st.session_state.get(REAUTH_KEY)" in text
    assert "st.session_state.pop(REAUTH_KEY, False)" not in text
    assert 'st.button("Cancel"' in text
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_login_form":
            assert any(
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "pop"
                and bool(inner.args)
                and getattr(inner.args[0], "id", None) == "REAUTH_KEY"
                for inner in ast.walk(node)
            ), "the login form must clear the re-auth flag once it succeeds"
            return
    pytest.fail("_login_form is not defined in ui/shared.py")


# ------------------------------------------------------------ no secrets in URLs


def test_no_secret_lands_in_a_query_parameter():
    """Query strings end up in proxy logs; agent ids are fine, config bodies never."""
    offenders = []
    for path in ALL_UI_FILES:
        for number, line in enumerate(lines(path), start=1):
            if "query_params" not in line:
                continue
            if not re.search(r"query_params\[[^\]]*\]\s*=", line):
                continue
            lowered = line.lower()
            if any(word in lowered for word in ("password", "token", "secret", "config_body", "webhook")):
                offenders.append(f"{path.relative_to(UI_DIR)}:{number}: {line.strip()}")
    assert offenders == [], f"secret in a query parameter: {offenders}"


# --------------------------------------------------- streamlit-free, testable core


@pytest.mark.parametrize("module", ["session.py", "config_view.py"])
def test_core_ui_modules_stay_streamlit_free(module):
    """Both are tested without the UI stack; importing `streamlit` would end that."""
    text = source(UI_DIR / module)
    assert not re.search(r"^\s*(import|from)\s+streamlit", text, re.MULTILINE)


def test_session_module_does_not_import_server_auth():
    """`server.auth` constructs SQLite stores at import; the UI mounts data read-only."""
    tree = ast.parse(source(SESSION))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert "server.auth" not in imported
    assert not any(name.startswith("server.") for name in imported)


# ------------------------------------------------------- deployment posture (#105)

REPO_ROOT = UI_DIR.parent


def test_streamlit_keeps_xsrf_on_and_cors_off():
    """Acceptance criterion #105: XSRF protection enabled, CORS off.

    Read as data rather than text so a commented-out or misspelled key cannot pass.
    """
    tomllib = pytest.importorskip("tomllib")
    with open(REPO_ROOT / ".streamlit" / "config.toml", "rb") as handle:
        config = tomllib.load(handle)
    server = config.get("server") or {}
    assert server.get("enableXsrfProtection") is True
    assert server.get("enableCORS") is False


def test_the_ui_image_ships_the_streamlit_config():
    """Streamlit reads `./.streamlit/config.toml`, so the image must contain it.

    Without the COPY the XSRF/CORS posture silently does not apply inside the
    container — the kind of bug that looks fine in review and is absent at runtime.
    """
    dockerfile = source(REPO_ROOT / "Dockerfile.ui")
    assert re.search(r"^COPY\s+\.streamlit/?\s+", dockerfile, re.MULTILINE)


def test_compose_binds_the_ui_to_loopback_with_a_reason():
    """The UI has no TLS of its own; publishing 8501 must stay a deliberate act."""
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load(source(REPO_ROOT / "docker-compose.yml"))
    ui = compose["services"]["ui"]
    ports = ui.get("ports") or []
    assert ports, "the ui service must publish its port explicitly"
    assert all(str(port).startswith("127.0.0.1:") for port in ports), ports
    assert "loopback" in source(REPO_ROOT / "docker-compose.yml").lower()


# ------------------------------------------------------ empty fleet onboarding (#116)


def test_the_empty_fleet_shows_a_snippet_built_by_the_shared_helper():
    """#116: the dead-end info is gone, and one snippet serves both pages.

    Help and the Agents empty state must not each own a copy of the YAML — that is how
    the Help page and the docs drifted apart before. Both call the helper, so a fix
    lands once.
    """
    agents = source(AGENTS_PAGE)
    help_page = source(UI_DIR / "pages" / "4_Help.py")
    assert "collector_config_snippet(" in agents
    assert "collector_config_snippet(" in help_page
    assert "No agents connected. Start an OpenTelemetry Collector" not in agents
    # The snippet is useless without the line people forget, so it is asserted below
    # in the helper test rather than trusted to prose.


def test_the_empty_fleet_snippet_is_runnable_yaml_for_this_server():
    pytest.importorskip("streamlit")
    yaml = pytest.importorskip("yaml")
    from ui.shared import collector_config_snippet

    document = yaml.safe_load(collector_config_snippet("http://opamp.example:4320/"))

    assert document["extensions"]["opamp"]["server"]["http"]["endpoint"] == (
        "http://opamp.example:4320/v1/opamp"
    )
    # Without this the extension never starts — the single most common setup mistake.
    assert document["service"]["extensions"] == ["opamp"]
    capabilities = document["extensions"]["opamp"]["capabilities"]
    for flag in (
        "reports_health",
        "reports_effective_config",
        "reports_available_components",
    ):
        assert capabilities[flag] is True
    # A config with no pipeline is rejected by a real collector, so "copy, paste, run"
    # would otherwise be a lie.
    assert document["service"]["pipelines"]["traces"]["receivers"] == ["otlp"]


def test_the_container_variant_rewrites_only_the_host():
    pytest.importorskip("streamlit")
    yaml = pytest.importorskip("yaml")
    from ui.shared import collector_config_snippet

    def endpoint(url, **kwargs):
        return yaml.safe_load(collector_config_snippet(url, **kwargs))["extensions"][
            "opamp"
        ]["server"]["http"]["endpoint"]

    for local in ("http://localhost:4320", "http://127.0.0.1:4320"):
        assert endpoint(local, in_container=True) == (
            "http://host.docker.internal:4320/v1/opamp"
        )
    # A collector that already points at a real hostname must not be rewritten.
    assert endpoint("http://collector-host:4320", in_container=True) == (
        "http://collector-host:4320/v1/opamp"
    )
