"""Overload shedding: answer 503 + `Retry-After` instead of accepting work (#78).

Spec §Throttling (`specification.md:3545-3585`): the server MAY answer 503 (or 429)
with an optional `Retry-After`, and the minimum recommended retry interval is 30
seconds. Clients already implement that half — opamp-go's `httpsender` honours
429/503 and the header — so the server is the only missing side.

The spec's WebSocket variant is an in-band `ServerErrorResponse{type: UNAVAILABLE,
retry_info{retry_after_nanoseconds}}`. It was implemented and measured, then moved to
the handshake: opamp-go v0.24 (what `otelcol-contrib` binds) ignores `retry_info` on
an established socket, drops the socket on any `error_response` and reconnects
immediately, so an in-band refusal becomes a reconnect storm. A refused *upgrade*
carrying `Retry-After` is honoured (measured 30.0 s between attempts). Hence: both
transports are refused at the same place — the HTTP request, or the upgrade request —
and `server.main`'s WebSocket endpoint holds the detail.

**One signal: event-loop lag.** This server runs one worker on one event loop by
design (README → "Capacity and deployment topology"), and the message handler is
synchronous, so:

* an in-flight request counter never rises above ~1 and measures nothing;
* registry size measures fleet size, not load.

Lag does mean the thing we care about. A sampler wakes every
`SAMPLE_INTERVAL_SECONDS` and compares when it should have woken with when it did;
that difference is exactly the backlog in front of the agents, and it is the same
number that makes every request — including `/health` — slow.

**Two predicates, not one.** `should_shed()` looks at the *current* lag only, so a
recovered server starts serving again immediately. `hold_reaper()` additionally
remembers the last time a refusal was actually served, because an agent that was
told to back off is silent on purpose and must not be reaped as dead (#69).

Off by default: nothing is sampled, counted or refused unless
`OPAMP_THROTTLE_ENABLED=true`. Throttling a server that is already in trouble is a
politeness, not a fix, and it must never be a surprise.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Optional

import prometheus_client as prom_client
from loguru import logger

# Spec §Throttling: "The minimum recommended retry interval is 30 seconds." A
# configured value below this is raised to it, because telling a client to retry
# sooner than the spec recommends is how a stampede becomes a hot loop.
SPEC_MIN_RETRY_AFTER_SECONDS = 30

DEFAULT_MAX_LAG_SECONDS = 1.0
DEFAULT_RETRY_AFTER_SECONDS = 30
# A throttled agent cannot move its heartbeat until it gets through, so the reaper
# holds off for at least this long after the last refusal. Sized to the agent
# timeout floor (AGENT_TIMEOUT_SECONDS default 180): any agent that could have been
# made stale by a refusal is covered.
DEFAULT_REAPER_GRACE_SECONDS = 180

# How often the loop measures its own punctuality. 100 ms is fine-grained enough to
# see a real backlog and cheap enough to leave running.
SAMPLE_INTERVAL_SECONDS = 0.1

# The metric labels are a closed vocabulary: `http` and `ws` only.
TRANSPORT_HTTP = "http"
TRANSPORT_WS = "ws"

PROM_THROTTLED_REQUESTS = prom_client.Counter(
    "opamp_throttled_requests_total",
    "OpAMP requests refused because the server is overloaded (#78), by transport",
    ["transport"],
)

PROM_EVENT_LOOP_LAG = prom_client.Gauge(
    "opamp_event_loop_lag_seconds",
    "How late the event loop woke from a 100ms sleep: the overload signal (#78)",
)

_last_lag_seconds: float = 0.0
# Monotonic time of the last sample that tripped the threshold, or the last refusal
# actually served. `None` until something has happened, so a fresh process cannot
# look "recently overloaded".
_last_overload_at: Optional[float] = None
_sampler: Optional[asyncio.Task] = None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _env_number(name: str, default: float, cast=float) -> float:
    """Read a positive number from the environment, warning and defaulting."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using {default}")
        return default
    if value <= 0:
        logger.warning(f"{name}={raw!r} must be positive; using {default}")
        return default
    return value


def enabled() -> bool:
    """Read `OPAMP_THROTTLE_ENABLED` at call time, so a test can flip it."""
    return _env_flag("OPAMP_THROTTLE_ENABLED", False)


def max_lag_seconds() -> float:
    return float(_env_number("OPAMP_THROTTLE_MAX_LAG_SECONDS", DEFAULT_MAX_LAG_SECONDS))


def retry_after_seconds() -> int:
    """`Retry-After` in seconds, never below the spec's 30-second recommendation."""
    configured = int(
        _env_number("OPAMP_THROTTLE_RETRY_AFTER_SECONDS", DEFAULT_RETRY_AFTER_SECONDS)
    )
    return max(SPEC_MIN_RETRY_AFTER_SECONDS, configured)


def reaper_grace_seconds() -> float:
    return float(
        _env_number("OPAMP_THROTTLE_REAPER_GRACE_SECONDS", DEFAULT_REAPER_GRACE_SECONDS)
    )


def last_lag_seconds() -> float:
    return _last_lag_seconds


def record_sample(lag_seconds: float) -> None:
    """Publish one lag measurement. The sampler calls this; tests call it directly."""
    global _last_lag_seconds, _last_overload_at
    _last_lag_seconds = max(0.0, float(lag_seconds))
    PROM_EVENT_LOOP_LAG.set(_last_lag_seconds)
    if _last_lag_seconds >= max_lag_seconds():
        _last_overload_at = time.monotonic()


def overloaded() -> bool:
    """Is the server behind *right now*?"""
    return enabled() and _last_lag_seconds >= max_lag_seconds()


def should_shed() -> bool:
    """Should this request be refused with 503 + `Retry-After`?

    Current lag only: a server that has caught up must start accepting work again
    immediately, or `Retry-After` turns into a self-inflicted outage.
    """
    return overloaded()


def mark_throttled(transport: str) -> None:
    """Record that a refusal was actually served: metric plus the reaper's memory."""
    global _last_overload_at
    _last_overload_at = time.monotonic()
    PROM_THROTTLED_REQUESTS.labels(transport=transport).inc()


def hold_reaper(now: Optional[float] = None) -> bool:
    """Must the stale-agent reaper stand down this tick? (#69)

    True while the server is overloaded, and for `OPAMP_THROTTLE_REAPER_GRACE_SECONDS`
    after the last refusal: an agent that is being told to retry later is silent by
    design, and deleting it would reset `connected_at`, drop its persisted metrics and
    fire `stale_agent` + `new_agent` for a client that is doing the right thing.
    """
    if not enabled():
        return False
    if overloaded():
        return True
    if _last_overload_at is None:
        return False
    now = time.monotonic() if now is None else now
    return (now - _last_overload_at) < reaper_grace_seconds()


def headers() -> Dict[str, str]:
    """Response headers for a shedding reply."""
    return {"Retry-After": str(retry_after_seconds())}


def status() -> Dict[str, object]:
    """The `/health` block: what the gate is set to, and what it is seeing."""
    return {
        "enabled": enabled(),
        "max_lag_seconds": max_lag_seconds(),
        "retry_after_seconds": retry_after_seconds(),
        "lag_seconds": round(_last_lag_seconds, 4),
        "shedding": should_shed(),
    }


def reset() -> None:
    """Forget all measurements (tests only)."""
    global _last_lag_seconds, _last_overload_at
    _last_lag_seconds = 0.0
    _last_overload_at = None
    PROM_EVENT_LOOP_LAG.set(0.0)


async def _sample_loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        scheduled = loop.time() + SAMPLE_INTERVAL_SECONDS
        await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        record_sample(loop.time() - scheduled)


def start() -> None:
    """Start the sampler task (idempotent). Called from the app lifespan."""
    global _sampler
    if _sampler is None or _sampler.done():
        _sampler = asyncio.create_task(_sample_loop())


def stop() -> None:
    global _sampler
    if _sampler is not None:
        _sampler.cancel()
        _sampler = None
