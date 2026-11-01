"""Encryption at rest for configuration bodies (#98).

Once this server stores desired configs it is a secrets store, and a secrets store
that keeps plaintext in a SQLite file is one backup, one support bundle or one
`sqlite3 data/opamp.db` away from leaking every exporter credential in the fleet.

AES-256-GCM, one key, per-row AAD. AAD binds a ciphertext to the row it belongs to
(`agent:<id>`, `revision:<id>`), so copying an envelope from one row to another
fails to decrypt instead of silently handing an agent someone else's config. The
envelope is self-describing:

    enc:<key_id>:<base64url(nonce || ciphertext+tag)>

`key_id` is the first 8 hex characters of SHA-256(key). That is what makes
rotation possible without a separate `key_version` column: a row knows which key
sealed it, so ``needs_rotation()`` can find the stragglers and ``open_sealed()``
can say *"this row was sealed with a key you have not supplied"* instead of a
generic decryption error. `CONFIG_AT_REST_KEY_PREVIOUS` is accepted for reads only,
which is the whole rotation procedure: set the new key, keep the old one as
previous, run the rotation command, drop the previous key.

**The honest failure mode** (documented in the README, because operators ask):
losing the key loses *desired* config, not the fleet. Agents keep running their
last applied config, and the server can rebuild desired state from the
`effective_config` each agent reports. Losing the key while `previous` is also
gone means those rows fail closed with a clear error until the agents report
again — never a silent empty config.

When no key is configured the server stores plaintext, but only with the explicit
`OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE=true` opt-out; `server.posture` reports that
as an unmet requirement so config push stays off without it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from typing import Dict, Optional

from loguru import logger

KEY_ENV = "CONFIG_AT_REST_KEY"
PREVIOUS_KEY_ENV = "CONFIG_AT_REST_KEY_PREVIOUS"
ALLOW_PLAINTEXT_ENV = "OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE"

ENVELOPE_PREFIX = "enc:"
KEY_BYTES = 32
NONCE_BYTES = 12
KEY_ID_CHARS = 8

# Decoded keys are cached against the raw env value: base64-decoding and hashing a
# key on every agent write is pointless work on the message path (#109), and the
# env value is what changes when an operator rotates.
_key_cache: Dict[str, Optional[bytes]] = {}


class ConfigDecryptError(RuntimeError):
    """A sealed body could not be opened. Always names the reason and the row."""


class ConfigKeyError(RuntimeError):
    """`CONFIG_AT_REST_KEY` is set but unusable (wrong length, not base64)."""


def _decode_key(raw: str, env_name: str) -> bytes:
    if raw in _key_cache:
        cached = _key_cache[raw]
        if cached is None:
            raise ConfigKeyError(_key_error(env_name))
        return cached
    try:
        # Accept both standard and URL-safe base64: operators paste what `openssl
        # rand -base64 32` or `python -c 'base64...'` produced, and refusing one
        # spelling is a support ticket, not security.
        padded = raw.strip()
        padded += "=" * (-len(padded) % 4)
        key = base64.b64decode(padded, altchars=b"-_", validate=False)
    except (binascii.Error, ValueError):
        _key_cache[raw] = None
        raise ConfigKeyError(_key_error(env_name)) from None
    if len(key) != KEY_BYTES:
        _key_cache[raw] = None
        raise ConfigKeyError(_key_error(env_name))
    _key_cache[raw] = key
    return key


def _key_error(env_name: str) -> str:
    return (
        f"{env_name} must be {KEY_BYTES} bytes, base64-encoded "
        f"(generate one with: python -c \"import os,base64; "
        f"print(base64.b64encode(os.urandom({KEY_BYTES})).decode())\")"
    )


def current_key() -> Optional[bytes]:
    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        return None
    return _decode_key(raw, KEY_ENV)


def previous_key() -> Optional[bytes]:
    raw = (os.environ.get(PREVIOUS_KEY_ENV) or "").strip()
    if not raw:
        return None
    return _decode_key(raw, PREVIOUS_KEY_ENV)


def configured() -> bool:
    """Is a current key present and usable? (Unusable keys raise, not return False.)"""
    return bool(current_key())


def allow_plaintext() -> bool:
    raw = (os.environ.get(ALLOW_PLAINTEXT_ENV) or "").strip().lower()
    return raw in ("true", "1", "yes", "on")


def at_rest_mode() -> str:
    """`encrypted`, `plaintext` (explicit opt-out) or `unconfigured`.

    Reported in `/health` and in the config-push posture, so a plaintext store is
    never invisible: "we opted out" and "nobody set the key" are different states
    and only one of them is deliberate.
    """
    if configured():
        return "encrypted"
    if allow_plaintext():
        return "plaintext"
    return "unconfigured"


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:KEY_ID_CHARS]


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def _aad_bytes(aad: str) -> bytes:
    # Namespaced so an AAD for one kind of row can never collide with another.
    return f"opamp-config:{aad}".encode("utf-8")


def seal(plaintext: str, aad: str) -> str:
    """Encrypt `plaintext`, bound to `aad`. Raises `ConfigKeyError` without a key."""
    key = current_key()
    if key is None:
        raise ConfigKeyError(
            f"{KEY_ENV} is not set: refusing to store a config body in plaintext"
        )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad_bytes(aad))
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{ENVELOPE_PREFIX}{key_id(key)}:{payload}"


def open_sealed(envelope: str, aad: str) -> str:
    """Decrypt an envelope. Raises `ConfigDecryptError` with a diagnosable reason."""
    if not is_sealed(envelope):
        raise ConfigDecryptError("value is not a sealed config envelope")
    try:
        _, sealed_key_id, payload = envelope.split(":", 2)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise ConfigDecryptError(f"malformed config envelope: {exc}") from exc
    if len(raw) <= NONCE_BYTES:
        raise ConfigDecryptError("malformed config envelope: truncated ciphertext")

    nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    for label, key in (("current", current_key()), ("previous", previous_key())):
        if key is None or key_id(key) != sealed_key_id:
            continue
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad_bytes(aad))
        except InvalidTag:
            # Right key id, wrong key material — a copied or tampered row.
            raise ConfigDecryptError(
                f"config body failed authentication with the {label} key: the "
                "ciphertext is corrupt, tampered with, or was sealed for a "
                "different row"
            ) from None
        return plaintext.decode("utf-8")

    raise ConfigDecryptError(
        f"config body was sealed with key id {sealed_key_id}, which is neither the "
        f"current {KEY_ENV} nor {PREVIOUS_KEY_ENV}. Supply the key that sealed it, "
        "or let the agent re-report its effective config"
    )


def seal_if_configured(plaintext: Optional[str], aad: str) -> Optional[str]:
    """Seal when a key is configured, pass through otherwise (plaintext mode)."""
    if plaintext is None or is_sealed(plaintext):
        return plaintext
    if not configured():
        return plaintext
    return seal(plaintext, aad)


def open_if_sealed(value: Optional[str], aad: str) -> Optional[str]:
    """Open a sealed value; plaintext values pass through (pre-#98 rows)."""
    if value is None or not is_sealed(value):
        return value
    return open_sealed(value, aad)


def needs_rotation(value: Optional[str]) -> bool:
    """True when a sealed value is not under the current key."""
    if not is_sealed(value):
        return False
    key = current_key()
    if key is None:
        return False
    return not value.startswith(f"{ENVELOPE_PREFIX}{key_id(key)}:")


def rotate_sealed(value: str, aad: str) -> str:
    """Re-seal a value under the current key, decrypting with current-or-previous."""
    return seal(open_sealed(value, aad), aad)


def generate_key() -> str:
    """A fresh base64 key, for docs, tests and the setup helper."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def describe() -> Dict[str, object]:
    """Non-secret summary for `/health` and the posture report."""
    key = current_key()
    previous = previous_key()
    return {
        "mode": at_rest_mode(),
        "key_id": key_id(key) if key else None,
        "previous_key_id": key_id(previous) if previous else None,
        "plaintext_opt_out": allow_plaintext(),
    }


def reset_key_cache() -> None:
    """Drop cached decoded keys (tests, and after changing the env in-process)."""
    _key_cache.clear()


def warn_if_plaintext() -> None:
    """Startup line when config bodies would be stored in plaintext (#98)."""
    mode = at_rest_mode()
    if mode == "plaintext":
        logger.warning(
            f"config bodies are stored UNENCRYPTED ({ALLOW_PLAINTEXT_ENV}=true). "
            "That is a development setting: any copy of the database exposes every "
            f"exporter credential in the fleet. Set {KEY_ENV} before production."
        )
    elif mode == "unconfigured":
        logger.info(
            f"no {KEY_ENV} configured: config bodies will not be stored. Set a key "
            f"(or {ALLOW_PLAINTEXT_ENV}=true for local development) before enabling "
            "config push."
        )
