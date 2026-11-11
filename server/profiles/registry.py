"""The one place an agent's type is decided (#118, made data-driven in #134).

Precedence, strongest first — and the provenance is kept, not discarded, because a
badge or a verdict that cannot say *why* it believes something is worse than none:

1. **pinned** — `agents.agent_type`, set by an operator through
   `PUT /agent/{id}/type`. Server-side and audited, so this is the answer to "the
   client's claim is spoofable": pin it and stop guessing.
2. **declared** — evidence the agent supplied about itself, cheapest first:
   a *distinctive* `content_type` (the protocol's own field for "what is this
   body"), then an `identity.match_any` rule over the `AgentDescription`
   attributes — `service.name`, which the spec says SHOULD be "a reverse FQDN that
   uniquely identifies the Agent type". A generic content type like `text/yaml`
   is never treated as identity: FluentBit and a collector both send it.
3. **sniffed** — the payloads look like it (`AvailableComponents` sections, a
   config that uses the profile's sections).
4. **fallback** — `generic`: stored but not interpreted.

Order within (2) and (3) is `priority`, highest first, from the dictionary. Not
filename order: a locale, a rename or a parallel checkout would each change fleet
behaviour silently. Two profiles sharing a priority is a load error, not a coin flip.

Since #134 the dictionary is YAML on disk (`loader.py`), so this module's job is
narrower than before: hold the compiled tuple, keep the resolution memo honest across
a reload (`_GENERATION`), and answer "which profiles could this have been?"
(`candidates`) so `type_info()` can say when a match was ambiguous.

Nothing here is a security boundary. Every signal but (1) comes from the agent, and
the server authenticates the channel rather than the payload, so a profile can only
ever *suppress* what the server shows or claims — never grant anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from server.profiles import generic, loader
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
    "bootstrap_default",
    "candidates",
    "canonical",
    "describe",
    "exists",
    "get",
    "names",
    "reload",
    "resolve",
    "resolve_cached",
    "SOURCE_ORDER",
]


def _build(dictionary: Tuple[AgentProfile, ...]) -> Tuple[AgentProfile, ...]:
    """Dictionary profiles + the built-in fallback, in match order.

    `generic` is appended rather than loaded: it is the answer when nothing else
    works, so it cannot be the thing that failed to load. It declares no formats, no
    matchers and no sniffer, so it cannot win a match either — its position is
    cosmetic, and last is the honest place for a fallback.
    """
    ordered = tuple(
        profile for profile in dictionary if profile.name != FALLBACK_PROFILE
    )
    return ordered + (generic.PROFILE,)


def _index(
    profiles: Tuple[AgentProfile, ...]
) -> Tuple[Dict[str, AgentProfile], Dict[str, AgentProfile], AgentProfile]:
    by_name = {profile.name: profile for profile in profiles}
    by_fold = {name.casefold(): profile for name, profile in by_name.items()}
    return by_name, by_fold, by_name[FALLBACK_PROFILE]


#: Monotonic counter, bumped by `reload()`. It is part of every resolution
#: fingerprint: without it `resolve_cached()` would keep serving decisions made
#: against the old dictionary, and the only symptom would be a profile that "did not
#: take effect until the server was restarted".
_GENERATION = 0

#: Registered profiles in match order (priority, high to low), `generic` last.
PROFILES: Tuple[AgentProfile, ...] = _build(loader.load_all())
_BY_NAME, _BY_FOLD, FALLBACK = _index(PROFILES)
SOURCE_ORDER = SOURCES


def generation() -> int:
    """How many times the dictionary has been (re)loaded in this process."""
    return _GENERATION


def reload(
    directory=None, *, strict: Optional[bool] = None
) -> Tuple[AgentProfile, ...]:
    """Re-read the dictionary and swap it in atomically, bumping the generation.

    Read the whole set, validate it, then swap once: a half-loaded registry must
    never be visible to a request. `strict=True` raises instead of logging, which is
    what `--strict-profiles` and the tests use.
    """
    global PROFILES, _BY_NAME, _BY_FOLD, FALLBACK, _GENERATION

    profiles = _build(loader.load_all(directory, strict=strict))
    by_name, by_fold, fallback = _index(profiles)

    _GENERATION += 1
    PROFILES, _BY_NAME, _BY_FOLD, FALLBACK = profiles, by_name, by_fold, fallback
    logger.info(
        "agent profile dictionary reloaded (generation "
        f"{_GENERATION}): {', '.join(names())}"
    )
    return PROFILES


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


def bootstrap_default() -> AgentProfile:
    """The profile whose snippet to show when the agent is not known yet.

    A token is usually minted *before* an agent first connects, so there is no
    resolution to ask. The dictionary answers with `bootstrap_default: true` (at most
    one file may set it); when nobody claims it, the honest default is the fallback
    profile's plain-text snippet rather than an invented YAML document.
    """
    for profile in PROFILES:
        if profile.bootstrap_default:
            return profile
    return FALLBACK


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

    The generation is first and costs nothing: it is the only entry that can change
    without the *agent* changing, and leaving it out means a reload cannot invalidate
    a cached decision.

    The rest is cheap by construction: two memoised/short values and three O(1)
    comparisons. The `effective_config` entry is compared by value (it is the stored
    string the agent holds until a new report replaces it) so a 10 KB body is never
    hashed or re-serialised on a read path.
    """
    attrs = identity_attrs(agent)
    return (
        _GENERATION,
        getattr(agent, "agent_type", None),
        tuple(sorted(attrs.items())),
        _declared_content_types(agent),
        _component_sections(agent),
        getattr(agent, "effective_config", None),
    )


def candidates(agent: Any) -> Tuple[str, ...]:
    """Every profile the agent's own evidence points at, in match order.

    More than one is not an error — it is a phrase an operator needs to see ("this
    looks like A or B"), because after #134 a wrong profile is a file someone dropped
    in, and a first-match-wins tie is a decision nobody can audit. Empty means no
    evidence at all, which is the fallback's whole reason for existing.
    """
    declared = _declared_content_types(agent)
    attrs = identity_attrs(agent)
    matched: List[str] = []
    for profile in PROFILES:
        if profile.name == FALLBACK_PROFILE:
            continue
        if any(profile.declares(content_type) for content_type in declared):
            matched.append(profile.name)
        elif profile.matches_description(attrs):
            matched.append(profile.name)
        elif profile.sniffed_by(agent):
            matched.append(profile.name)
    return tuple(matched)


def candidates_cached(agent: Any) -> Tuple[str, ...]:
    """`candidates()`, memoised on the agent like `resolve_cached()`.

    Same key, so `type_info()` costs nothing extra on the read path: an agent's
    evidence either has not changed (cache hit) or has, in which case the resolution
    is being recomputed anyway.
    """
    fingerprint = _fingerprint(agent)
    cached = getattr(agent, "_profile_candidates_cache", None)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    found = candidates(agent)
    try:
        agent._profile_candidates_cache = (fingerprint, found)
    except AttributeError:  # pragma: no cover - duck-typed callers stay read-only
        pass
    return found


def resolve(agent: Any, *, pinned: Optional[str] = None) -> Resolution:
    """Decide the profile for `agent` — see the module docstring for the precedence."""
    pin = getattr(agent, "agent_type", None) if pinned is None else pinned
    if pin:
        profile = get(pin)
        if profile is not None:
            return Resolution(profile, "pinned")
        # A pin naming a profile this build does not have (a downgrade, a typo in a
        # hand-edited database, a dictionary file that failed to load). Guess nothing:
        # fall through to the evidence and let `type_info()` report the source, so the
        # operator can see the pin is not it.
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

    Same idiom as `AgentState._usage_cache`: the key is derived from the inputs — plus
    the dictionary generation, so a reload invalidates every entry — and nothing has
    to remember to.
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
