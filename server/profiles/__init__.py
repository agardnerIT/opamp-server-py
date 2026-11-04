"""Agent profiles: what kind of agent the server is talking to (#118).

The protocol is type-blind (see `base.py`), so "which product is this" is a local
concept. `registry.resolve()` decides it, `registry.resolve_cached()` decides it
cheaply, and `AgentState` reads the answer — nothing else should import `otelcol` or
`generic` directly, or the next profile has to be wired in twice.
"""

from server.profiles.base import (
    FALLBACK_PROFILE,
    SOURCES,
    AgentProfile,
    ConfigFormat,
    Resolution,
    YAML_CONTENT_TYPES,
)
from server.profiles.registry import (
    FALLBACK,
    PROFILES,
    canonical,
    describe,
    exists,
    get,
    names,
    resolve,
    resolve_cached,
)

__all__ = [
    "AgentProfile",
    "ConfigFormat",
    "Resolution",
    "YAML_CONTENT_TYPES",
    "FALLBACK",
    "FALLBACK_PROFILE",
    "PROFILES",
    "SOURCES",
    "canonical",
    "describe",
    "exists",
    "get",
    "names",
    "resolve",
    "resolve_cached",
]
