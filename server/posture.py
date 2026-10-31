"""Fail-closed enablement and security posture for remote config push (#96).

Read-only operation works in any posture: listing agents, reports, compliance and
metrics never depend on the flags below. Anything that can change *what a
collector runs* is gated on the whole posture, not on a single flag, because a
push endpoint in front of an unauthenticated, plaintext, admin-less control
plane is a fleet-takeover switch (epic #93, decision D2).

Everything here reads `os.environ` at call time, like `server.ws.ws_enabled()`,
so behaviour is testable without re-importing the app.

Requirements when `OPAMP_CONFIG_PUSH_ENABLED=true`:

===========================  ===================================================
Admin auth is configured     `ADMIN_PASSWORD`, `ADMIN_PASSWORD_FILE`, a stored
                             generated password, or an `admin_principals` row (#95)
Agents are authenticated     `OPAMP_AGENT_AUTH=token` and each agent has a
                             credential (#94). `mtls` is reported but **not**
                             enforced by this build, because certificates are
                             terminated by the proxy — see
                             `server/agent_auth.py`
The server speaks TLS        `PUBLIC_URL` is `https://`/`wss://`, or
                             `TLS_TERMINATED_UPSTREAM=true`
===========================  ===================================================

`OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true` is a local-development escape hatch: it
starts the server, but every surface (`/health`, the UI banner, the logs) keeps
reporting `insecure: true` until the posture is actually fixed.
"""

from __future__ import annotations

import os
from typing import Dict, List

from fastapi import HTTPException
from loguru import logger

from server import admin_auth

# Detail used by every gated push route. 404, not 403: a server with config push
# off should not confirm to a stranger that the surface exists at all.
GATE_DETAIL = "config push is disabled"

VALID_AGENT_AUTH_MODES = ("off", "token", "mtls")

# Agent auth modes this build actually *enforces*. #94 added token enforcement;
# "mtls" is reported (and warned about at startup) but deliberately excluded, so
# the gate cannot be opened by an env var that no code reads. mTLS belongs with
# the CA work in #83.
ENFORCED_AGENT_AUTH_MODES: frozenset = frozenset({"token"})

TLS_URL_PREFIXES = ("https://", "wss://")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def config_push_enabled() -> bool:
    """`OPAMP_CONFIG_PUSH_ENABLED` (default false — remote config is opt-in)."""
    return _env_flag("OPAMP_CONFIG_PUSH_ENABLED")


def allow_insecure_config_push() -> bool:
    """`OPAMP_ALLOW_INSECURE_CONFIG_PUSH` — local development only."""
    return _env_flag("OPAMP_ALLOW_INSECURE_CONFIG_PUSH")


def agent_auth_mode() -> str:
    """`OPAMP_AGENT_AUTH` normalised to `off|token|mtls` (unknown values = off)."""
    raw = (os.environ.get("OPAMP_AGENT_AUTH") or "").strip().lower()
    if not raw:
        return "off"
    if raw not in VALID_AGENT_AUTH_MODES:
        logger.warning(
            f"Unknown OPAMP_AGENT_AUTH={raw!r} (expected one of "
            f"{', '.join(VALID_AGENT_AUTH_MODES)}); treating agent auth as off"
        )
        return "off"
    return raw


def public_url() -> str:
    return (os.environ.get("PUBLIC_URL") or "").strip()


def tls_enabled() -> bool:
    """True when this server is reached over TLS, directly or via a terminator."""
    if public_url().lower().startswith(TLS_URL_PREFIXES):
        return True
    return _env_flag("TLS_TERMINATED_UPSTREAM")


def audit_enabled() -> bool:
    """Audit log availability. Reported now, implemented by #97 (always False)."""
    return _env_flag("OPAMP_AUDIT_ENABLED")


def posture() -> Dict[str, object]:
    """The public posture summary embedded in `GET /health`."""
    agent_auth = agent_auth_mode()
    return {
        "enabled": config_push_enabled(),
        "insecure": insecure(),
        "agent_auth": agent_auth,
        "audit": audit_enabled(),
        "tls": tls_enabled(),
    }


def unmet_requirements() -> List[str]:
    """Every unmet requirement for config push, in a stable order.

    Returns all of them (not just the first) so a failed start can name them in
    one go instead of starting a one-fix-per-restart loop.
    """
    return [
        f"{state['label']}: {state['detail']}"
        for state in requirements().values()
        if not state["met"]
    ]


def requirements() -> Dict[str, Dict[str, object]]:
    """Each posture requirement, whether it is met, and what to do when it is not."""
    admin_ok = admin_auth.configured()
    mode = agent_auth_mode()
    agent_ok = mode in ENFORCED_AGENT_AUTH_MODES
    tls_ok = tls_enabled()

    return {
        "admin_auth": {
            "label": "admin auth is not configured",
            "met": admin_ok,
            "detail": "set ADMIN_PASSWORD, ADMIN_PASSWORD_FILE, or "
            "OPAMP_ADMIN_AUTOGENERATE_PASSWORD=true",
        },
        "agent_auth": {
            "label": "agents are not authenticated",
            "met": agent_ok,
            "mode": mode,
            "detail": (
                "set OPAMP_AGENT_AUTH=token and give each agent a credential "
                "(`opampctl agents token create <agent-id>`; see README → "
                "Agent authentication)"
                if mode == "off"
                else (
                    "OPAMP_AGENT_AUTH=mtls is not enforced by this build: client "
                    "certificates are terminated upstream and trusting a forwarded "
                    "header would be forgeable. Use OPAMP_AGENT_AUTH=token until "
                    "mTLS lands with #83"
                    if mode == "mtls"
                    else f"OPAMP_AGENT_AUTH={mode} is not enforced by this build"
                )
            ),
        },
        "tls": {
            "label": "the server is not served over TLS",
            "met": tls_ok,
            "detail": "set PUBLIC_URL=https://<host> behind a TLS terminator, or "
            "TLS_TERMINATED_UPSTREAM=true once the proxy is in place (see "
            "docker/nginx.conf and docker/Caddyfile)",
        },
    }


def tls_terminated_upstream() -> bool:
    return _env_flag("TLS_TERMINATED_UPSTREAM")


def insecure() -> bool:
    """True when config push is on despite an unsafe posture (override in use)."""
    return config_push_enabled() and bool(unmet_requirements())


def push_allowed() -> bool:
    """May the server deliver remote config at all?

    True only with the flag on *and* the posture satisfied, or with the explicit
    development override. Use this — not `config_push_enabled()` — anywhere the
    answer reaches an agent or a client.
    """
    if not config_push_enabled():
        return False
    return allow_insecure_config_push() or not unmet_requirements()


class PostureError(RuntimeError):
    """Startup refusal: config push enabled on an insecure posture."""

    def __init__(self, missing: List[str]):
        self.missing = list(missing)
        super().__init__(format_refusal(self.missing))


def format_refusal(missing: List[str]) -> str:
    lines = ["refusing to start: OPAMP_CONFIG_PUSH_ENABLED=true but the server posture is insecure:"]
    lines += [f"  - {item}" for item in missing]
    lines.append(
        "fix the above, or set OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true for local "
        "development only (the server will report insecure: true)"
    )
    return "\n".join(lines)


def _override_banner(missing: List[str]) -> str:
    # Plain text on purpose: loguru colour markup does not survive every sink
    # (docker logs, files), and a warning nobody can read is not a warning.
    body = "\n".join(f"  - {item}" for item in missing)
    bar = "!" * 78
    return (
        f"\n{bar}\n"
        "INSECURE CONFIG PUSH ENABLED - LOCAL DEVELOPMENT ONLY\n"
        f"{bar}\n"
        f"{body}\n"
        "Anyone who can reach this server can rewrite your fleet's collector config.\n"
        "Every surface reports insecure: true until the posture is fixed.\n"
        f"{bar}\n"
    )


def posture_warning(push: Dict[str, object] | None = None) -> str | None:
    """Markdown warning for the UI when push runs insecure; None when it is fine.

    Lives here, not in the Streamlit page, so the wording is testable without a
    browser and every future surface (CLI, MCP, reports) can reuse it.
    """
    push = posture() if push is None else push
    if not push.get("insecure"):
        return None

    agent_auth = push.get("agent_auth", "off")
    if agent_auth == "off":
        agent_auth_label = "off — any client can claim any agent identity"
    elif agent_auth in ENFORCED_AGENT_AUTH_MODES:
        agent_auth_label = f"{agent_auth} (enforced)"
    else:
        agent_auth_label = f"{agent_auth} (configured, not enforced by this build)"
    tls_label = "yes" if push.get("tls") else "no — config travels in cleartext"

    return (
        "### ⚠️ Insecure config push is enabled\n"
        "This server can push remote configuration to collectors while its posture is "
        "unsafe (`OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true`). **Anyone who can reach this "
        "server can rewrite your fleet's configuration.**\n\n"
        f"- Agent authentication: **{agent_auth_label}**\n"
        f"- TLS: **{tls_label}**\n"
        f"- Audit log: **{'on' if push.get('audit') else 'off'}**\n\n"
        "Fix the posture (admin auth + agent auth + TLS) and unset the override. "
        "Local development only."
    )


def assert_startup_posture() -> None:
    """Refuse to start push on an insecure posture unless the override is set.

    A warning would be logged and forgotten; a failed start is the only signal an
    operator reliably sees (epic #93, D2). Called from the FastAPI lifespan and
    again from `__main__` for a clean non-zero exit under uvicorn.
    """
    if not config_push_enabled():
        logger.info(
            "config push disabled (OPAMP_CONFIG_PUSH_ENABLED=false): read-only mode, "
            "no remote config is delivered and push routes return 404"
        )
        return

    missing = unmet_requirements()
    if not missing:
        logger.info("config push enabled: posture checks passed")
        return

    if allow_insecure_config_push():
        logger.warning(_override_banner(missing))
        return

    raise PostureError(missing)


def scrub_remote_config(message) -> bool:
    """Strip `remote_config` from an outgoing `ServerToAgent` if push is not allowed.

    Belt and braces for layers added later (#81/#102): the flag must never be
    re-enabled for a client just because a code path forgot to check. Returns True
    if something was stripped.
    """
    if push_allowed() or not message.HasField("remote_config"):
        return False
    message.ClearField("remote_config")
    logger.warning("stripped remote_config from ServerToAgent: config push is not allowed")
    return True


def config_push_gate() -> None:
    """FastAPI dependency for every config push route (#103).

    Raises 404 — identical to a route that does not exist — when config push is
    off or the posture is insecure without the override. Never 403: a stranger
    should not be able to tell the surface apart from a typo.
    """
    if not push_allowed():
        raise HTTPException(status_code=404, detail=GATE_DETAIL)
