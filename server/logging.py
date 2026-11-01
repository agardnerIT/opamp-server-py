"""Structured logging, correlation and redaction for the server (#108).

One module configures loguru. Every sink — stderr, an optional rotated file — gets
the same treatment, so "the log line looked different in Docker" cannot happen:

* **JSON by default** whenever stderr is not a TTY (containers, CI, systemd), plain
  text for local development. `LOG_FORMAT=json|text` forces either.
* **Event names, not sentences**: `logger.bind(event="agent.registered", ...)`.
  The JSON line always carries an `event` key, so `grep '"event":"admin.auth.failed"'`
  works and a log-based alert needs no regex. A record with no event falls back to
  the logger name rather than an empty field.
* **Correlation**: `RequestContextMiddleware` accepts an inbound `X-Request-ID`
  (generating one otherwise), echoes it on the response and binds it to every log
  line written while the request is in flight. `server.tracing` adds `trace_id` and
  `span_id` when tracing is on, which is what makes "show me the trace for this
  log line" possible in any backend.
* **Redaction is not optional**: the patcher from `server.redaction` is installed
  here, for every sink, before any log line is written. A sink added later still
  gets scrubbed records because the patcher runs on the record, not the handler
  (#98).
* **No per-request INFO line for probes**: `/health` and `/metrics` log at `DEBUG`.
  A Kubernetes probe every 10s would otherwise be the loudest thing in the log.

The middleware is a raw ASGI application rather than a `BaseHTTPMiddleware`
subclass: it must not buffer request or response bodies (`/v1/opamp` posts
protobuf, `/metrics` streams), and it must pass WebSocket scopes through untouched.

Nothing here decides *what* to log. `configure_logging()` is called once at import
of `server.main` (before anything can log) and is idempotent, so a second call with
the same environment is free.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional

from loguru import logger

REQUEST_ID_HEADER = "x-request-id"
# A caller-supplied id is echoed back and put in a log line, so it is bounded and
# character-restricted: a header is attacker-controlled input.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# Probes: answer them, do not make them the loudest line in the log.
QUIET_PATHS = frozenset({"/health", "/metrics", "/v1/metrics", "/favicon.ico"})

# Bound to this request/correlation scope. A context var (not a global) so
# concurrent requests and sockets do not inherit each other's ids.
_request_id: ContextVar[Optional[str]] = ContextVar("opamp_request_id", default=None)


def is_quiet_path(path: str) -> bool:
    """Probe/scrape traffic: answered normally, logged at DEBUG (#108)."""
    if path in QUIET_PATHS:
        return True
    # `/metrics` is a mounted ASGI app: Starlette redirects `/metrics` to
    # `/metrics/`, and that second request must be quiet too.
    return any(path.startswith(f"{quiet}/") for quiet in QUIET_PATHS)

_VALID_FORMATS = ("json", "text")
_VALID_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")

_configured_signature: Optional[tuple] = None
# Which renderer the installed sinks expect. The patcher renders the final line
# into a private extra field (`_rendered`) and the handlers print
# `{extra[_rendered]}`. Two reasons not to use a *callable* format: loguru treats
# one as a dynamic template and runs `.format_map()` over its return value (so a
# message containing braces explodes), and rewriting `record["message"]` would
# also change what a sink added by a test or a library sees.
_active_format = "json"


# ---------------------------------------------------------------------- settings


def log_format() -> str:
    """`LOG_FORMAT=json|text`; JSON when the sink is not a TTY (containers, CI)."""
    raw = (os.environ.get("LOG_FORMAT") or "").strip().lower()
    if raw in _VALID_FORMATS:
        return raw
    try:
        return "text" if sys.stderr.isatty() else "json"
    except Exception:  # pragma: no cover - exotic stderr replacements
        return "json"


def log_level() -> str:
    """`LOG_LEVEL` (default `INFO`); an unknown value falls back, never crashes."""
    raw = (os.environ.get("LOG_LEVEL") or "").strip().upper()
    return raw if raw in _VALID_LEVELS else "INFO"


def log_file() -> Optional[str]:
    """`LOG_FILE` — optional rotated file sink, since containers rarely ship logs."""
    raw = (os.environ.get("LOG_FILE") or "").strip()
    return raw or None


# --------------------------------------------------------------------- formatters


def current_request_id() -> Optional[str]:
    return _request_id.get()


def valid_request_id(value: Optional[str]) -> bool:
    return bool(value) and bool(_REQUEST_ID_RE.match(value or ""))


def _fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Public bound fields: everything except loguru's private `_`-prefixed keys."""
    return {
        key: value
        for key, value in (record.get("extra") or {}).items()
        if not key.startswith("_")
    }


def json_formatter(record: Dict[str, Any]) -> str:
    """One JSON object per line — the format a log shipper expects.

    Returns the line *without* a trailing newline; loguru adds one per record.
    Serialisation is defensive: a bound field may be a datetime, a protobuf object
    or a dict of mixed types, and a log line must never raise. `default=str` is the
    last resort, and the message has already been redacted (#98).
    """
    fields = _fields(record)
    payload: Dict[str, Any] = {
        "ts": record["time"].isoformat().replace("+00:00", "Z"),
        "level": record["level"].name,
        "event": fields.pop("event", None) or record.get("name") or "log",
        "message": record.get("message", ""),
        "logger": record.get("name"),
        "function": record.get("function"),
        "line": record.get("line"),
    }
    payload.update(fields)
    try:
        return json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        return json.dumps(
            {"level": payload["level"], "event": payload["event"], "message": str(payload)},
            default=str,
        )


def text_formatter(record: Dict[str, Any]) -> str:
    """Human-readable single line for local development; colours are never emitted."""
    fields = _fields(record)
    event = fields.pop("event", None) or record.get("name") or "log"
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    message = str(record.get("message", ""))
    base = f"{record['time']:%Y-%m-%d %H:%M:%S.%f}"[:-3]
    line = f"{base} | {record['level'].name:<8} | {event:<28} | {message}"
    return f"{line} | {suffix}" if suffix else line


# ----------------------------------------------------------------------- patcher


def _context_patcher(record: Dict[str, Any]) -> None:
    """Attach correlation ids and a default event to every record.

    Runs before any sink (it is installed as loguru's global patcher) and before
    the redaction patcher, which scrubs the message and `extra` afterwards. A
    bound `event` always wins, so a call site that names its event is never
    overwritten by the fallback.
    """
    try:
        extra = record.setdefault("extra", {})
        if not extra.get("event"):
            extra["event"] = record.get("name") or "log"
        if not extra.get("request_id"):
            request_id = current_request_id()
            if request_id:
                extra["request_id"] = request_id
        if not extra.get("trace_id") or not extra.get("span_id"):
            from server import tracing

            trace_id, span_id = tracing.current_ids()
            if trace_id and not extra.get("trace_id"):
                extra["trace_id"] = trace_id
            if span_id and not extra.get("span_id"):
                extra["span_id"] = span_id
        # Render the final line *last*, after redaction has scrubbed the record.
        # Stored in a private field rather than `message` so other sinks (a test's
        # capture, a library's handler) still see the message they expect.
        extra["_rendered"] = (
            json_formatter(record) if _active_format == "json" else text_formatter(record)
        )
    except Exception:  # pragma: no cover - logging must never raise
        pass


# ------------------------------------------------------------------- configuration


def configure_logging(*, force: bool = False) -> None:
    """Install the sinks, formatters, correlation patcher and redaction filter.

    Idempotent: the same `(LOG_FORMAT, LOG_LEVEL, LOG_FILE)` is configured once.
    `force=True` re-applies it (tests that change the environment mid-session).
    """
    global _configured_signature
    signature = (log_format(), log_level(), log_file())
    if _configured_signature == signature and not force:
        return

    global _active_format
    _active_format = signature[0]
    level = signature[1]

    handlers = [
        {
            "sink": sys.stderr,
            "level": level,
            # The patcher renders the line; see `_active_format`.
            "format": "{extra[_rendered]}",
            "colorize": False,
            # A traceback is rendered by the redaction patcher (#98); loguru's own
            # diagnosis dumps local variables, which is precisely what must not
            # reach a log file when a config body is in scope.
            "backtrace": False,
            "diagnose": False,
        }
    ]
    if signature[2]:
        handlers.append(
            {
                "sink": signature[2],
                "level": level,
                "format": "{extra[_rendered]}",
                "colorize": False,
                "backtrace": False,
                "diagnose": False,
                "rotation": os.environ.get("LOG_FILE_ROTATION", "10 MB"),
                "retention": os.environ.get("LOG_FILE_RETENTION", "7 days"),
                "compression": "gz",
                "enqueue": True,
            }
        )

    logger.configure(handlers=handlers)
    # Order is load-bearing: the redaction patcher replaces loguru's global
    # patcher, so the correlation patcher has to be composed into it rather than
    # installed before it (see `install_redaction`).
    install_redaction()
    _configured_signature = signature


def install_redaction() -> None:
    """Install the #98 scrubbing patcher, keeping the correlation patcher.

    `redaction.install_log_filter()` replaces the global patcher. Installing it
    last would silently drop `request_id`, so the two are composed here: redaction
    runs first (it scrubs `extra` wholesale), then the correlation fields are
    added from the context — neither can leak, and the line still carries its ids.
    """
    from server import redaction

    redaction.install_log_filter()
    core = getattr(logger, "_core", None)
    scrubbing = getattr(core, "patcher", None) if core is not None else None

    def _combined(record: Dict[str, Any]) -> None:
        if scrubbing is not None:
            scrubbing(record)
        _context_patcher(record)

    logger.configure(patcher=_combined)


def reset_logging() -> None:
    """Forget the configured signature (tests; does not remove existing sinks)."""
    global _configured_signature
    _configured_signature = None


# ---------------------------------------------------------------------- middleware


def new_request_id() -> str:
    return uuid.uuid4().hex


def request_id_from(header: Optional[str]) -> str:
    """Accept a well-formed inbound id, otherwise generate one.

    Never trust the header verbatim: it is echoed to the client and written into
    every log line for the request. A malformed id is replaced, not rejected — the
    request itself is still legitimate.
    """
    candidate = (header or "").strip()
    return candidate if valid_request_id(candidate) else new_request_id()


class RequestContextMiddleware:
    """Correlate one HTTP request: id in, id out, one line at the end.

    A raw ASGI middleware: bodies pass through untouched, WebSocket scopes are
    handed straight to the app (the WS layer binds its own `conn_id`), and the
    response headers are extended in place without re-serialising the body.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        request_id = request_id_from(headers.get(REQUEST_ID_HEADER))
        method = scope.get("method", "")
        path = scope.get("path", "")
        status = {"code": 0}
        ids: Dict[str, Optional[str]] = {"trace_id": None, "span_id": None}
        started = time.perf_counter()
        token = _request_id.set(request_id)

        async def send_with_id(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message.get("status", 0)
                # The instrumentor's span is still open here; after the response is
                # sent it is finished, and the ids would be gone by the time the
                # completion line is written.
                from server import tracing

                ids["trace_id"], ids["span_id"] = tracing.current_ids()
                response_headers = list(message.get("headers") or [])
                response_headers.append(
                    (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))
                )
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            level = "DEBUG" if is_quiet_path(path) else "INFO"
            logger.bind(
                event="http.request",
                request_id=request_id,
                method=method,
                path=path,
                status=status["code"],
                duration_ms=duration_ms,
                **{key: value for key, value in ids.items() if value},
            ).log(level, f"{method} {path} {status['code']} {duration_ms}ms")
            _request_id.reset(token)


__all__ = [
    "REQUEST_ID_HEADER",
    "QUIET_PATHS",
    "RequestContextMiddleware",
    "configure_logging",
    "current_request_id",
    "is_quiet_path",
    "install_redaction",
    "json_formatter",
    "log_file",
    "log_format",
    "log_level",
    "new_request_id",
    "request_id_from",
    "reset_logging",
    "text_formatter",
    "valid_request_id",
]
