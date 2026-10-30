"""Admin credential resolution and verification.

Today the admin surface is one shared password (`ADMIN_PASSWORD`, HTTP Basic).
#95 replaces this with principals, roles and sessions; this module exists so that
when it does, there is exactly one place to change and `server/main.py` keeps
asking the same questions.

Why the credential is *generated* rather than defaulted (#96): a static default —
`changeme`, `admin`, an empty string — is how fleets get taken over, because
nobody changes it. So there are three ways to get a password, in order:

1. `ADMIN_PASSWORD` (explicit, what CI and existing deployments use)
2. `ADMIN_PASSWORD_FILE` (secret mount)
3. `OPAMP_ADMIN_AUTOGENERATE_PASSWORD=true` → a random password is generated on
   first start and persisted to `$DATA_DIR/admin_password` (mode 0600), then
   reused on every restart. It is printed once, at startup, because there is no
   other way for the operator to learn it.

With none of those, the server runs in **read-only mode**: `ADMIN_PASSWORD` is
empty and privileged endpoints behave exactly as before (open). Closing that
door is #95's job; #96 only guarantees that the *deployment* path never ships a
static or empty admin password, and that config push is refused in that state.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from pathlib import Path
from typing import Tuple

from fastapi import Request
from loguru import logger

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
PASSWORD_FILE_NAME = "admin_password"
AUTOGENERATE_ENV = "OPAMP_ADMIN_AUTOGENERATE_PASSWORD"
PASSWORD_FILE_ENV = "ADMIN_PASSWORD_FILE"

# 24 random bytes -> 32 url-safe chars. Long enough that guessing is not a threat
# model, short enough to copy/paste out of a log line.
GENERATED_BYTES = 24
MIN_REASONABLE_LENGTH = 12
PLACEHOLDER_PASSWORDS = {"changeme", "password", "admin", "secret", "opamp", "test"}

# Resolved once at import; `ensure()` may replace it at startup (generation).
# Tests patch this attribute directly, and `server/main.py` reads it through the
# module so it never holds a stale copy.
ADMIN_PASSWORD: str = ""
PASSWORD_SOURCE: str = "none"


def autogenerate() -> bool:
    return (os.environ.get(AUTOGENERATE_ENV) or "").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def store_path() -> Path:
    """Where a generated password lives (survives restarts via the data volume)."""
    return DATA_DIR / PASSWORD_FILE_NAME


def _read_secret_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        logger.warning(f"Admin password file {path} is unreadable: {e}")
        return ""
    if not value:
        logger.warning(f"Admin password file {path} is empty; ignoring it")
        return ""
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            logger.warning(
                f"Admin password file {path} has mode {oct(mode)}; restrict it to 0600"
            )
    except OSError:
        pass
    return value


def _warn_if_weak(password: str, source: str) -> None:
    """Cheap sanity checks. Full password policy belongs to #95."""
    if password.lower() in PLACEHOLDER_PASSWORDS:
        logger.warning(
            f"admin password from {source} is a well-known placeholder; it will be "
            "tried first by anyone scanning this host — change it"
        )
    elif len(password) < MIN_REASONABLE_LENGTH:
        logger.warning(
            f"admin password from {source} is only {len(password)} characters; "
            f"use at least {MIN_REASONABLE_LENGTH}"
        )


def resolve() -> Tuple[str, str]:
    """Resolve the credential without generating one. Returns (password, source)."""
    env_password = os.environ.get("ADMIN_PASSWORD") or ""
    if env_password.strip():
        return env_password, "env"
    if env_password:
        logger.warning("ADMIN_PASSWORD is set but blank; ignoring it")

    explicit_file = (os.environ.get(PASSWORD_FILE_ENV) or "").strip()
    if explicit_file:
        password = _read_secret_file(Path(explicit_file))
        if password:
            return password, "file"
        logger.warning(
            f"{PASSWORD_FILE_ENV}={explicit_file} did not yield a password; "
            "falling back to the stored/generated credential"
        )

    if autogenerate():
        password = _read_secret_file(store_path())
        if password:
            return password, "generated"

    return "", "none"


def _write_private(path: Path, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (password + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure() -> str:
    """Startup hook: resolve the credential, generating one if asked to.

    Idempotent. Returns the password in effect (empty string = read-only mode).
    """
    global ADMIN_PASSWORD, PASSWORD_SOURCE

    password, source = resolve()
    if password:
        if source != PASSWORD_SOURCE:
            logger.info(f"admin auth enabled (credential source: {source})")
        ADMIN_PASSWORD, PASSWORD_SOURCE = password, source
        _warn_if_weak(password, source)
        return ADMIN_PASSWORD

    if autogenerate():
        password = secrets.token_urlsafe(GENERATED_BYTES)
        path = store_path()
        try:
            _write_private(path, password)
        except OSError as e:
            logger.error(
                f"could not persist the generated admin password to {path}: {e} — "
                "it will change on the next start"
            )
        ADMIN_PASSWORD, PASSWORD_SOURCE = password, "generated"
        logger.warning(
            "\n" + "-" * 78 + "\nADMIN PASSWORD GENERATED\n" + "-" * 78 + "\n"
            f"  password: {password}\n"
            f"  stored at: {path} (mode 0600)\n"
            "  Use it for the UI Admin page, `opampctl --password`, and any client "
            "(ADMIN_PASSWORD=...).\n"
            "  Set ADMIN_PASSWORD explicitly to choose your own; delete the file to "
            "go back to read-only mode."
        )
        return ADMIN_PASSWORD

    ADMIN_PASSWORD, PASSWORD_SOURCE = "", "none"
    logger.info(
        "admin auth not configured: read-only mode (privileged endpoints behave as "
        f"before). Set ADMIN_PASSWORD, {PASSWORD_FILE_ENV} or "
        f"{AUTOGENERATE_ENV}=true to enable them"
    )
    return ""


def configured() -> bool:
    """Is an admin credential in effect? (False = read-only mode.)"""
    return bool(ADMIN_PASSWORD)


def source() -> str:
    """`env` | `file` | `generated` | `none` — never the password itself."""
    return PASSWORD_SOURCE


def verify(supplied: str) -> bool:
    """Constant-time check of a supplied password against the configured one."""
    if not ADMIN_PASSWORD:
        return True
    return hmac.compare_digest(supplied or "", ADMIN_PASSWORD)


def require_admin(request: Request) -> bool:
    """HTTP Basic admin check used by privileged endpoints.

    When no credential is configured every caller passes: that is the legacy
    read-only behaviour, which #95 closes.
    """
    if not ADMIN_PASSWORD:
        return True

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        password = decoded.split(":", 1)[1] if ":" in decoded else decoded
    except Exception:
        return False

    return verify(password)


ADMIN_PASSWORD, PASSWORD_SOURCE = resolve()
