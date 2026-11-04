"""The one place an agent's type is decided (#118).

Precedence, strongest first — and the provenance is kept, not discarded, because a
badge or a verdict that cannot say *why* it believes something is worse than none:

1. **pinned** — `agents.agent_type`, set by an operator through
   `PUT /agent/{id}/type`. Server-side and audited, so this is the answer to "the
   client's claim is spoofable": pin it and stop guessing.
2. **declared** — evidence the agent supplied about itself, cheapest first:
   a *distinctive* `content_type` (the protocol's own field for "what is this
   body"), then `service.name`, which the spec says SHOULD be "a reverse FQDN that
   uniquely identifies the Agent type". A generic content type like `text/yaml`
   is never treated as identity: FluentBit and a collector both send it.
3. **sniffed** — the payloads look like it (`AvailableComponents` sections, a
   config that uses the profile's components).
4. **fallback** — `generic`: stored but not interpreted.

Nothing here is a security boundary. Every signal but (1) comes from the agent, and
the server authenticates the channel rather than the payload, so a profile can only
ever *suppress* what the server shows or claims — never grant anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from server.profiles import generic, otelcol
from server.profiles.base import (
    FALLBACK_PROFILE,
    SOURCES,
    AgentProfile,
    ConfigFormat,
    Resolution,
    flatten_description,
)

__all__ = [
    "PROFILES",
    "FALLBACK",
    "Resolution",
    "AgentProfile",
    "ConfigFormat",
    "exists",
    "get",
    "names",
    "describe",
    "resolve",
    "resolve_cached",
    "SOURCE_ORDER",
]

#: Registered profiles in match order. `generic` cannot win a match — it declares no
#: formats, no description matchers and no sniffer — so its position is cosmetic.
PROFILES: Tuple[AgentProfile, ...] = (otelcol.PROFILE, generic.PROFILE)
_BY_NAME: Dict[str, AgentProfile] = {profile.name: profile for profile in PROFILES}
#: Case-insensitive lookup, because a type name arrives from a human (`opampctl agents
#: set-type <id> OTelCol`) as often as from code. The canonical name is what gets
#: stored, so the database never holds two spellings of one profile.
_BY_FOLD: Dict[str, AgentProfile] = {name.casefold(): profile for name, profile in _BY_NAME.items()}
FALLBACK: AgentProfile = _BY_NAME[FALLBACK_PROFILE]
SOURCE_ORDER = SOURCES


def names() -> Tuple[str, ...]:
    """Every registered profile name, in match order."""
    return tuple(profile.name for profile in PROFILES)


def get(name: Optional[str]) -> Optional[AgentProfile]:
    """The profile called `name`, or `None`. Never raises: names come from users."""
    if not name or not isinstance(name, str):
        return None
    return _BY_FOLD.get(name.strip().casefold())


def exists(name: Optional[str]) -> bool:
    return get(name) is not None


def canonical(name: Optional[str]) -> Optional[str]:
    """The canonical spelling of `name`, or `None` when no profile provides it."""
    profile = get(name)
    return profile.name if profile is not None else None


def describe() -> List[Dict[str, Any]]:
    """`GET /agent/types` payload: what this build can call an agent."""
    return [profile.describe() for profile in PROFILES]


# ------------------------------------------------------------------ resolution ---


def _declared_content_types(agent: Any) -> Tuple[str, ...]:
    """The `content_type`s the agent put on its config objects (never `""`)."""
    effective_config = getattr(agent, "effective_config", None)
    if not effective_config:
        return ()
    from server import effective_config as effective_config_module

    try:
        return effective_config_module.content_types(effective_config)
    except Exception:  # a malformed report is evidence of nothing
        return ()


def _component_sections(agent: Any) -> Tuple[str, ...]:
    """Keys of `AvailableComponents.components` — part of the sniff fingerprint."""
    available = getattr(agent, "available_components", None) or {}
    sections = available.get("components")
    if not isinstance(sections, dict):
        return ()
    return tuple(sorted(str(key) for key in sections))


def identity_attrs(agent: Any) -> Dict[str, str]:
    """`flatten_description`, memoised while the agent's description object lives.

    The cache holds a **strong reference** to the description dict, so `is` can never
    be a recycled id: either it is the same object or it is a new report. That makes
    a per-read stampede over `GET /agents` O(1) per agent after its first report,
    without re-serialising a description to fingerprint it.
    """
    description = getattr(agent, "description", None)
    cached = getattr(agent, "_profile_attrs_cache", None)
    if cached is not None and cached[0] is description:
        return cached[1]
    attrs = flatten_description(description)
    try:
        agent._profile_attrs_cache = (description, attrs)
    except AttributeError:  # pragma: no cover - duck-typed callers stay read-only
        pass
    return attrs


def _fingerprint(agent: Any) -> tuple:
    """Everything a resolution decision depends on, in one comparable tuple.

    Cheap by construction: two memoised/short values and three O(1) comparisons. The
    `effective_config` entry is compared by identity (it is the *same* string object
    the agent holds until a new report replaces it) so a 10 KB body is never hashed
    or re-serialised on a read path.
    """
    attrs = identity_attrs(agent)
    return (
        getattr(agent, "agent_type", None),
        tuple(sorted(attrs.items())),
        _declared_content_types(agent),
        _component_sections(agent),
        getattr(agent, "effective_config", None),
    )


def resolve(agent: Any, *, pinned: Optional[str] = None) -> Resolution:
    """Decide the profile for `agent` — see the module docstring for the precedence."""
    pin = getattr(agent, "agent_type", None) if pinned is None else pinned
    if pin:
        profile = get(pin)
        if profile is not None:
            return Resolution(profile, "pinned")
        # A pin naming a profile this build does not have (a downgrade, a typo in a
        # hand-edited database). Guess nothing: fall through to the evidence and let
        # `type_info()` report the source, so the operator can see the pin is not it.
        logger.warning(
            f"agent {getattr(agent, 'agent_id', '?')} is pinned to unknown agent "
            f"type {pin!r}; falling back to detection (known: {', '.join(names())})"
        )

    for content_type in _declared_content_types(agent):
        for profile in PROFILES:
            if profile.declares(content_type):
                return Resolution(profile, "declared")

    attrs = identity_attrs(agent)
    for profile in PROFILES:
        if profile.matches_description(attrs):
            return Resolution(profile, "declared")

    for profile in PROFILES:
        if profile.sniffed_by(agent):
            return Resolution(profile, "sniffed")

    return Resolution(FALLBACK, "fallback")


def resolve_cached(agent: Any) -> Resolution:
    """`resolve()`, memoised on the agent until one of its inputs changes.

    Same idiom as `AgentState._usage_cache`: the key is derived from the inputs, so a
    new report invalidates the entry by itself and nothing has to remember to.
    """
    fingerprint = _fingerprint(agent)
    cached = getattr(agent, "_profile_cache", None)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    resolution = resolve(agent)
    try:
        agent._profile_cache = (fingerprint, resolution)
    except AttributeError:  # pragma: no cover - duck-typed callers stay read-only
        pass
    return resolution
