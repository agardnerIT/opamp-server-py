"""One redaction filter, used by every surface that can emit a config (#98).

Desired and effective collector configurations are the most sensitive data this
server holds: exporter auth headers, basic-auth extensions, TLS keys, internal
endpoints. They arrive from agents, are stored in SQLite, are rendered in the API,
the reports, the alerts and the UI, and can end up in a log line through nothing
more than a careless f-string. A second, slightly different filter per surface is
how one of those paths ends up unfiltered, so there is exactly one here.

Two operations, deliberately separate:

``redact_document(value)``
    **Replace** secret *values* with `SENTINEL`, preserving the document's shape,
    and return the list of paths that were touched. Display-safe. The shape is
    preserved because a consumer (the UI, `opampctl`, a diff view) needs to see
    *that* a password exists even when it may not see it.

``scrub_fields(fields)``
    **Drop** credential-shaped *keys* entirely. Used for audit detail and log
    lines, where a key name is enough and a placeholder would only add noise. A
    partially logged token is still a leaked token, so this drops rather than
    truncates.

The dangerous edge is the round trip: a redacted document is a *display artefact*
and must never be saved back as a desired config — pushing one silently wipes
credentials fleet-wide. ``contains_redaction()`` is the guard, and the propose
path calls it. That is the single most important function in this module.

Matching is by key name (case-insensitive, at any depth, exact or substring) plus
a value-shape heuristic for `headers`-style maps (`Authorization: Basic …`,
`Authorization: Bearer …`) and URL userinfo, because an auth header's key is
usually just `Authorization` and its value is the secret.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# What a human sees in place of a secret.
SENTINEL = "<redacted>"

# What a redacted *document* carries so a machine can tell the difference between
# "the operator really named a key <redacted>" and "this body has been redacted".
# Obscure on purpose: it must be something nobody types into a collector config by
# accident, because `contains_redaction()` refuses anything carrying it.
MARKER = "opamp:redacted:v1"

# Exact key names, and substrings for the variants nobody remembers to add here
# (`db_password`, `agent_token_hash`, `x-api-secret`, `aws_access_key_id`).
SECRET_KEYS = frozenset(
    {
        "authorization",
        "auth",
        "bearer",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "access_key",
        "accesskey",
        "private_key",
        "privatekey",
        "client_secret",
        "clientsecret",
        "credentials",
        "cookie",
        "set-cookie",
        "x-api-key",
        "tls_key",
        "certificate_key",
        "passphrase",
    }
)

SECRET_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "private_key",
    "privatekey",
    "access_key",
    "accesskey",
    "credential",
    "authorization",
    "passphrase",
    "tls_key",
)

# Header-shaped values: `Authorization: Basic dXNlcjpwYXNz`, `Bearer eyJ…`.
_HEADER_VALUE_RE = re.compile(
    r"^(?P<scheme>basic|bearer|digest|negotiate|token)\s+(?P<value>\S+)$",
    re.IGNORECASE,
)

# `scheme://user:password@host` in an endpoint or webhook URL. The scheme length is
# bounded (schemes are short): an unbounded `*` here scans to the end of the string
# from every start position, which is quadratic on a long log line.
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://)(?P<user>[^:/@\s]{1,256}):(?P<pw>[^@/\s]{1,256})@"
)

# Free-text scrubbing for log lines. These run over already-formatted text, which
# is why they have to cover the shapes that actually appear: JSON (`"password":
# "x"`), Python repr (`'password': 'x'`), env/dotenv (`ADMIN_PASSWORD=x`), header
# lines and URLs. The key pattern matches the *whole identifier* (so `db_password`
# and `ADMIN_PASSWORD` are covered, not just the bare word), and the value pattern
# refuses a value that is already the sentinel, so scrubbing is idempotent.
# The regex finds `identifier: value` / `identifier=value` pairs and the
# *predicate* decides whether the identifier is secret. That split is a performance
# requirement, not a style choice: an alternation of secret words inside the pattern
# makes it quadratic on a long run of ordinary characters (a 20 KB log line took 28
# seconds), and this runs on every log record. Here every class is bounded and the
# alternation is a plain `search` over a key that is at most 129 characters.
_TEXT_KEY_RE = re.compile(
    r"""(?P<key>(?<![A-Za-z0-9_.\-])["']?[A-Za-z0-9_.\-]{1,129}["']?\s*[:=]\s*)"""
    r"""(?!<redacted>)(?P<value>"[^"]{0,4096}"|'[^']{0,4096}'|[^\s,;\)}\]"'&]{1,4096})"""
)

_SECRET_WORD_RE = re.compile(
    r"""password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|authorization|bearer|passphrase|credentials?""",
    re.IGNORECASE,
)

_TEXT_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:Authorization|Proxy-Authorization|X-Api-Key|Api-Key)\s*:\s*)"
    r"(?:[A-Za-z]+\s+)?(?P<value>[^\s,;\"'}]+)",
    re.IGNORECASE,
)

# Bound on the recursion, so a crafted config cannot turn redaction into a stack
# overflow (the YAML alias-bomb concern from #100, one level up).
MAX_DEPTH = 64

MAX_PATHS = 200


def is_secret_key(key: Any) -> bool:
    """Does this key name look like it holds a credential?"""
    if not isinstance(key, str) or not key:
        return False
    lowered = key.strip().lower()
    if lowered in SECRET_KEYS:
        return True
    return any(part in lowered for part in SECRET_KEY_SUBSTRINGS)


def _is_header_key(key: str) -> bool:
    lowered = key.strip().lower()
    return lowered in ("headers", "header", "http_headers", "request_headers", "metadata")


def _redact_value(value: str) -> Optional[str]:
    """Return the scrubbed form of a string value, or None if it is not secret."""
    match = _HEADER_VALUE_RE.match(value.strip())
    if match:
        return f"{match.group('scheme')} {SENTINEL}"
    if _URL_USERINFO_RE.search(value):
        return _URL_USERINFO_RE.sub(
            lambda m: f"{m.group('scheme')}{m.group('user')}:{SENTINEL}@", value
        )
    return None


_B64_RE = re.compile(r"^[A-Za-z0-9+/]{8,}={0,2}$")


def decode_text_body(value: str) -> Optional[str]:
    """Decode a base64 body to text when it plausibly *is* a base64 body.

    OpAMP bodies are `bytes`, and `MessageToDict` renders them base64 — so an
    effective config reported by an agent arrives as
    `{"configMap": {"": {"body": "cmVjZWl2ZXJzOi..."}}}`. A key-name filter sees
    nothing secret in that, and neither does a text scrub, which is why this exists.

    Conservative on purpose: a plain string that merely happens to be base64
    alphabet must not be decoded, re-encoded and handed back changed. The guards are
    length, padding, a strict decode and a printable-ratio check.
    """
    if len(value) < 8 or len(value) % 4 or not _B64_RE.match(value):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not text:
        return None
    printable = sum(1 for char in text if char.isprintable() or char in "\n\r\t")
    if printable / len(text) < 0.95:
        return None
    return text


def redact_document(value: Any) -> Tuple[Any, List[str]]:
    """Return `(redacted_copy, paths)`; `value` is never mutated.

    `paths` are dotted/bracketed locations (`config.receivers.otlp.protocols
    .grpc.tls.key`, `headers[Authorization]`) for the caller to show an operator
    *what* was hidden without showing it.
    """
    paths: List[str] = []
    redacted = _walk(value, "", paths, 0)
    return redacted, paths


def _walk(value: Any, path: str, paths: List[str], depth: int) -> Any:
    if depth > MAX_DEPTH:
        # Too deep to reason about: refuse to echo the subtree at all rather than
        # hand back something that might contain a secret.
        paths.append(path or "$")
        return SENTINEL
    if isinstance(value, dict):
        return _walk_mapping(value, path, paths, depth)
    if isinstance(value, (list, tuple)):
        return [
            _walk(item, f"{path}[{index}]" if path else f"[{index}]", paths, depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        scrubbed = _redact_value(value)
        if scrubbed is not None:
            paths.append(path or "$")
            return scrubbed
        # A config *body* is a string, not a mapping: an OpAMP `AgentConfigFile.body`
        # (or a JSON-encoded effective config inside one) hides its secrets from a
        # key-name filter. Scrub the text too, so `password: x` inside a body is
        # masked even though the surrounding document has no `password` key.
        text = scrub_text(value)
        if text != value:
            if len(paths) < MAX_PATHS:
                paths.append(path or "$")
            return text
        decoded = decode_text_body(value)
        if decoded is not None:
            scrubbed = scrub_text(decoded)
            if scrubbed != decoded:
                if len(paths) < MAX_PATHS:
                    paths.append(path or "$")
                return base64.b64encode(scrubbed.encode("utf-8")).decode("ascii")
    return value


def _walk_mapping(value: Dict[Any, Any], path: str, paths: List[str], depth: int) -> Dict[Any, Any]:
    out: Dict[Any, Any] = {}
    header_map = False
    for key in value:
        if isinstance(key, str) and _is_header_key(key):
            header_map = True
            break
    for key, item in value.items():
        child = f"{path}.{key}" if path else str(key)
        if is_secret_key(key):
            out[key] = SENTINEL
            if len(paths) < MAX_PATHS:
                paths.append(child)
            continue
        if header_map and isinstance(item, str):
            # A headers map is *all* credentials by nature: an `Authorization` key
            # is caught by name above, but `X-Custom-Auth` and friends are not, and
            # the value shape is what identifies them.
            scrubbed = _redact_value(item)
            out[key] = scrubbed if scrubbed is not None else item
            if scrubbed is not None and len(paths) < MAX_PATHS:
                paths.append(f"{path}.{key}" if path else str(key))
            continue
        out[key] = _walk(item, child, paths, depth + 1)
    return out


def redacted_envelope(value: Any) -> Dict[str, Any]:
    """Redact a document and wrap it with the metadata a client needs (#98).

    Consumers get `redacted: true`, the list of paths that were masked, and the
    `MARKER` so `contains_redaction()` can recognise a round-tripped body even if
    the sentinel itself was edited out.
    """
    redacted, paths = redact_document(value)
    return {
        "redacted": bool(paths),
        "redacted_paths": paths,
        "marker": MARKER if paths else None,
        "document": redacted,
    }


def contains_redaction(value: Any) -> bool:
    """True if a document carries a redaction artefact — i.e. is un-pushable.

    Accepts a dict/list (walked), a string (scanned, including a JSON string) or
    bytes. Deliberately blunt: a false positive costs an operator one confusing
    error message, a false negative wipes the fleet's credentials.
    """
    if value is None:
        return False
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return True
    if isinstance(value, str):
        if SENTINEL in value or MARKER in value:
            return True
        decoded = decode_text_body(value)
        return decoded is not None and (SENTINEL in decoded or MARKER in decoded)
    if isinstance(value, dict):
        return any(
            contains_redaction(key) or contains_redaction(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(contains_redaction(item) for item in value)
    return False


def redact_effective_config(raw: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """Redact a JSON-encoded effective config, returning `(json, paths)`.

    `AgentState.effective_config` is stored as a JSON string (it comes from
    protobuf `MessageToDict`), so the API needs a string-in/string-out form. The
    shape is preserved: the UI parses this to work out which components are in
    use, and a redacted config that no longer parses would break the fleet view.
    """
    if not raw:
        return raw, []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        # Not a JSON document. Do not guess: returning it unchanged would leak,
        # and returning the sentinel would destroy the row. Mark it, so the API
        # shows a single placeholder instead of a blob of unknown content.
        return json.dumps({"unparseable": True, "body": SENTINEL}), ["$"]
    redacted, paths = redact_document(parsed)
    return json.dumps(redacted), paths


def scrub_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Drop credential-shaped keys, recursively (#97's audit detail, log lines).

    Shared with `server.audit`, which imports this: one list of secret key names,
    taught once.
    """
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or is_secret_key(key):
            continue
        if isinstance(value, dict):
            clean[key] = scrub_fields(value)
        elif isinstance(value, (list, tuple)):
            clean[key] = [
                scrub_fields(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            clean[key] = value
    return clean


# ------------------------------------------------------------------- log lines


def _redact_key_value(match: "re.Match[str]", depth: int = 0) -> str:
    """Mask the value of an `identifier: value` pair whose identifier is secret.

    When the identifier is *not* secret, the value is re-scanned once. The generic
    regex is leftmost-first, so `RuntimeError: password=hunter2` matches the whole
    phrase with `RuntimeError: ` as the key — without this, the secret hides inside
    the value of a match that was rejected, which is precisely the traceback case a
    log filter must catch. The recursion is depth-limited and operates on a shorter
    string each time.
    """
    key = match.group("key")
    if _SECRET_WORD_RE.search(key):
        return f"{key}{SENTINEL}"
    if depth >= 4:
        return match.group(0)
    value = match.group("value")
    rescanned = scrub_text(value, _depth=depth + 1)
    return match.group(0) if rescanned == value else f"{key}{rescanned}"


def scrub_text(text: str, _depth: int = 0) -> str:
    """Scrub secrets out of already-formatted text (log messages, tracebacks).

    Regex-based and therefore best-effort: it exists to catch the careless
    `logger.info(f"config={cfg}")`, not to be a parser. The structured paths (the
    API, audit `detail_json`, the config store) use `redact_document()` and do not
    depend on this.
    """
    if not text:
        return text
    scrubbed = _URL_USERINFO_RE.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:{SENTINEL}@", text
    )
    scrubbed = _TEXT_HEADER_RE.sub(
        lambda m: f"{m.group('prefix')}{SENTINEL}", scrubbed
    )
    return _TEXT_KEY_RE.sub(lambda match: _redact_key_value(match, _depth), scrubbed)


def install_log_filter() -> None:
    """Scrub every loguru record, for every sink, including tracebacks (#98).

    A patcher rather than a sink filter because the requirement is *no sink ever
    sees the secret* — including sinks added by a future library, and including
    the formatted traceback, which a sink filter cannot rewrite. The traceback is
    rendered, scrubbed and appended to the message, and `record["exception"]` is
    cleared so the handler does not print the original.

    Idempotent: `logger.configure` replaces the patcher, so calling twice is safe.
    """
    from loguru import logger

    def _patcher(record: Dict[str, Any]) -> None:
        try:
            record["message"] = scrub_text(record["message"])
            extra = record.get("extra")
            if isinstance(extra, dict) and extra:
                record["extra"] = scrub_fields(extra)
            exc = record.get("exception")
            if exc is not None:
                import traceback

                rendered = "".join(
                    traceback.format_exception(exc.type, exc.value, exc.traceback)
                )
                record["exception"] = None
                record["message"] = f"{record['message']}\n{scrub_text(rendered)}"
        except Exception:  # pragma: no cover - logging must never raise
            pass

    logger.configure(patcher=_patcher)


def uninstall_log_filter() -> None:
    """Remove the patcher (tests, so a global hook does not leak between them)."""
    from loguru import logger

    # `patcher=[]`, not `patcher=None`: loguru treats None as "leave it alone".
    logger.configure(patcher=[])


def redact_all(values: Iterable[Any]) -> List[Any]:
    """Convenience for redacting a list of documents (reports, fleet listings)."""
    return [redact_document(value)[0] for value in values]
