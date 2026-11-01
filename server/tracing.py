"""OTLP tracing for the server (#108) — opt-in, no-op when disabled.

The server ingests OTLP metrics for its agents but has never emitted a span of its
own, which is a strange gap: the thing that can rewrite a fleet's config is the one
thing you cannot open in the tracing backend the fleet already runs.

Design rules, in order of importance:

1. **Off means off.** With `OPAMP_TRACING_ENABLED=false` (the default) no provider
   is constructed, no exporter is created, `span()` yields `None`, and the only
   cost is one environment lookup. Importing the SDK is also deferred, so the
   dependency is genuinely optional (`pip install -e ".[observability]"`).
2. **Spans that earn their keep.** `opamp.message` (one per *state-changing*
   AgentToServer message), `opamp.ws.connection`, `opa.evaluate`, `alert.send`.
   HTTP spans come from the FastAPI instrumentor, which gives the
   request → message → OPA → webhook tree for free.
3. **Never the body.** Attributes carry ids, capability bits, counts and outcomes.
   A config body, an Authorization header, a webhook URL query or an agent token
   never becomes a span attribute — the same rule as the log filter (#98), and for
   the same reason: span attributes are exported to a third-party collector.
4. **No span per heartbeat.** Heartbeats are the highest-volume event in the
   system; `message_span()` call sites pass `traceable=False` for them.

`current_ids()` is what the log formatter reads, so a JSON log line and its trace
share a `trace_id` without either module importing the other's state.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

from loguru import logger

# Kept out of module scope on purpose: importing the SDK when tracing is off would
# make an optional dependency mandatory at import time.
_TRACING: Any = None
_TRACER: Any = None
_PROVIDER: Any = None
_INITIALISED = False
_INSTRUMENTED_APP_ID: Optional[int] = None


def tracing_enabled() -> bool:
    """`OPAMP_TRACING_ENABLED` (default false), read at call time for tests."""
    raw = (os.environ.get("OPAMP_TRACING_ENABLED") or "").strip().lower()
    return raw in ("true", "1", "yes", "on")


def service_name() -> str:
    return (os.environ.get("OTEL_SERVICE_NAME") or "opamp-server").strip() or "opamp-server"


def service_version() -> str:
    try:
        from importlib.metadata import version

        return version("opamp-server-py")
    except Exception:  # pragma: no cover - not installed as a distribution
        return "0.0.0"


def _sampler_from_env() -> Any:
    """Honour `OTEL_TRACES_SAMPLER` / `_ARG` without requiring auto-instrumentation.

    The SDK does not read those variables by itself; the OTel auto-instrumentation
    agent does. Supporting the four common values keeps "the standard env vars are
    respected" true for a plain `uvicorn` start, and an unknown value falls back to
    `parentbased_always_on` rather than crashing the server over a typo.
    """
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    raw = (os.environ.get("OTEL_TRACES_SAMPLER") or "").strip().lower().replace("-", "_")
    arg = (os.environ.get("OTEL_TRACES_SAMPLER_ARG") or "").strip()
    try:
        ratio = float(arg) if arg else 1.0
    except ValueError:
        ratio = 1.0
    ratio_sampler = TraceIdRatioBased(ratio)

    if raw in ("always_off", "parentbased_always_off"):
        return ParentBased(ALWAYS_OFF) if raw.startswith("parentbased") else ALWAYS_OFF
    if raw in ("traceidratio",):
        return ratio_sampler
    if raw.startswith("parentbased_traceidratio"):
        return ParentBased(ratio_sampler)
    if raw in ("always_on", ""):
        return ALWAYS_ON
    return ParentBased(ALWAYS_ON)


def init_tracing(app: Any = None, exporter: Any = None) -> bool:
    """Configure the global tracer provider. Returns True when tracing is live.

    `exporter` is for tests (an in-memory exporter); production uses the OTLP/HTTP
    exporter configured by the standard `OTEL_EXPORTER_OTLP_*` variables.
    The call is idempotent — repeated app imports must not register two providers
    (the OTel API warns and ignores the second).
    """
    global _TRACING, _TRACER, _PROVIDER, _INITIALISED

    if not tracing_enabled():
        return False
    if _INITIALISED:
        if app is not None:
            _instrument_app(app)
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        logger.bind(event="tracing.unavailable").warning(
            "OPAMP_TRACING_ENABLED=true but the OpenTelemetry SDK is not installed; "
            'install it with: pip install -e ".[observability]"'
        )
        return False

    resource = Resource.create(
        {
            "service.name": service_name(),
            "service.version": service_version(),
            # Public posture, the same value /health reports. Handy when a trace
            # from a dev box and one from production look identical otherwise.
            "opamp.config_push.enabled": _config_push_enabled(),
        }
    )
    provider = TracerProvider(resource=resource, sampler=_sampler_from_env())
    try:
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter()
        # `SimpleSpanProcessor` for an injected exporter (tests) so spans are
        # visible without a flush; batch export in production so a slow collector
        # cannot block the message path.
        processor = (
            SimpleSpanProcessor(exporter)
            if exporter is not None
            else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)
    except Exception as exc:  # pragma: no cover - misconfigured collector
        logger.bind(event="tracing.export_failed", error=str(exc)).warning(
            "could not configure the OTLP span exporter; tracing stays off"
        )
        return False

    trace.set_tracer_provider(provider)
    _TRACING, _PROVIDER = trace, provider
    _TRACER = provider.get_tracer("opamp-server", service_version())
    _INITIALISED = True
    logger.bind(event="tracing.enabled", service=service_name()).info(
        f"tracing enabled: OTLP spans for service {service_name()!r}"
    )
    if app is not None:
        _instrument_app(app)
    return True


def _instrument_app(app: Any) -> None:
    """FastAPI/ASGI spans for the HTTP surface (optional dependency).

    Must run before the app's middleware stack is built (Starlette refuses to add
    middleware once it has started), which is why `server.main` calls
    `init_tracing(app)` at import rather than inside the lifespan. The app id guard
    makes a repeated call a no-op instead of double-wrapping every request.
    """
    global _INSTRUMENTED_APP_ID
    if _INSTRUMENTED_APP_ID == id(app):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Probes and the metrics scrape are the noisiest URLs in any fleet; they
        # get no spans unless an operator explicitly asks for them.
        excluded = os.environ.get("OPAMP_TRACING_EXCLUDED_URLS", "health,metrics,docs,openapi.json")
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls=excluded, tracer_provider=_PROVIDER
        )
        _INSTRUMENTED_APP_ID = id(app)
    except ImportError:
        logger.bind(event="tracing.instrumentation_missing").debug(
            "opentelemetry-instrumentation-fastapi is not installed; HTTP spans are off"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.bind(event="tracing.instrumentation_failed", error=str(exc)).warning(
            "FastAPI instrumentation failed; tracing continues without HTTP spans"
        )


def shutdown_tracing() -> None:
    """Flush and stop the provider (lifespan shutdown, tests)."""
    global _INITIALISED, _TRACER, _PROVIDER, _TRACING
    if _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception:  # pragma: no cover - best effort
            pass
    _TRACING = None
    _TRACER = None
    _PROVIDER = None
    _INITIALISED = False
_INSTRUMENTED_APP_ID: Optional[int] = None


def reset_tracing() -> None:
    """Test helper: drop the provider without shutting down the global API."""
    global _INITIALISED, _TRACER, _PROVIDER, _TRACING
    _TRACING = None
    _TRACER = None
    _PROVIDER = None
    _INITIALISED = False
_INSTRUMENTED_APP_ID: Optional[int] = None


def current_ids() -> Tuple[Optional[str], Optional[str]]:
    """`(trace_id, span_id)` of the active span, or `(None, None)` when off.

    Cheap enough for the log patcher: no SDK import, and one proxy lookup when
    tracing was never enabled.
    """
    global _TRACING
    if not _INITIALISED or _TRACING is None:
        return None, None
    try:
        span = _TRACING.get_current_span()
        context = span.get_span_context()
        if context is None or not context.is_valid:
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    except Exception:  # pragma: no cover - defensive
        return None, None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span, or yield `None` when tracing is off.

    Attribute values are coerced to the types OTel accepts (str/int/float/bool and
    lists of those); `None` is dropped so an absent id is not exported as the
    string "None". A span is never allowed to break the code it observes.
    """
    global _TRACER
    if not _INITIALISED or _TRACER is None:
        yield None
        return
    try:
        manager = _TRACER.start_as_current_span(name)
        current = manager.__enter__()
    except Exception:  # pragma: no cover - broken provider
        yield None
        return
    try:
        set_span_attributes(current, **attributes)
        yield current
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # pragma: no cover - defensive
            pass


def set_span_attributes(span: Any, **attributes: Any) -> None:
    """Attach attributes defensively: an unexportable value must not raise."""
    if span is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            if isinstance(value, (list, tuple, set, frozenset)):
                value = [item for item in value if isinstance(item, (str, int, float, bool))]
                if not value:
                    continue
            elif not isinstance(value, (str, int, float, bool)):
                value = str(value)
            span.set_attribute(key, value)
        except Exception:  # pragma: no cover - defensive
            continue


def _config_push_enabled() -> bool:
    from server import posture

    return posture.config_push_enabled()


def message_is_heartbeat(
    agent_dict: Dict[str, Any],
    *,
    is_new: bool,
    health_changed: bool = False,
) -> bool:
    """True when a message has nothing of interest for a span.

    A pure heartbeat carries health and capability bits and nothing else. Health is
    part of every heartbeat, so it only counts as a state change when the value
    actually moves — otherwise every poll of a healthy fleet would produce a span.
    """
    if is_new or health_changed:
        return False
    interesting = (
        "remoteConfigStatus",
        "agentDisconnect",
        "agentDescription",
        "effectiveConfig",
        "availableComponents",
        "packageStatuses",
        "connectionSettingsRequest",
    )
    return not any(key in agent_dict for key in interesting)


__all__ = [
    "current_ids",
    "init_tracing",
    "message_is_heartbeat",
    "reset_tracing",
    "service_name",
    "service_version",
    "set_span_attributes",
    "shutdown_tracing",
    "span",
    "tracing_enabled",
]
