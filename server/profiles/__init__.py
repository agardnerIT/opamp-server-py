"""Agent profiles: what kind of agent the server is talking to (#118, #134).

The protocol is type-blind (see `base.py`), so "which product is this" is a local
concept. `registry.resolve()` decides it, `registry.resolve_cached()` decides it
cheaply, and `AgentState` reads the answer — nothing else should import a profile by
name, or the next type has to be wired in twice.

Since #134 the dictionary is **data**: `server/profiles/data/*.yaml`, one file per
agent type, compiled by `loader.py` and executed by `engines.py`. Adding a type is a
file and a restart. `generic` is the exception — it is built in (`generic.py`),
because the answer for "nothing matched" cannot be the thing that failed to load.

`PROFILES` and `FALLBACK` are re-exported through PEP 562 so `registry.reload()` does
not have to keep a second copy of them in sync: read `profiles.PROFILES` and you get
the current tuple, whichever generation that is.
"""

from server.profiles.base import (
    FALLBACK_PROFILE,
    SOURCES,
    AgentProfile,
    ComponentsSpec,
    ConfigFormat,
    IdentityRule,
    Panel,
    PanelMetric,
    Resolution,
    SniffSpec,
    TelemetrySpec,
    YAML_CONTENT_TYPES,
)
from server.profiles.loader import DEFAULT_DIRECTORY, ProfileLoadError
from server.profiles.registry import (
    canonical,
    candidates,
    candidates_cached,
    bootstrap_default,
    describe,
    exists,
    get,
    names,
    reload,
    resolve,
    resolve_cached,
)

__all__ = [
    "AgentProfile",
    "ConfigFormat",
    "ComponentsSpec",
    "DEFAULT_DIRECTORY",
    "FALLBACK",
    "FALLBACK_PROFILE",
    "GENERATION",
    "IdentityRule",
    "PROFILES",
    "Panel",
    "PanelMetric",
    "ProfileLoadError",
    "Resolution",
    "SOURCES",
    "SniffSpec",
    "TelemetrySpec",
    "YAML_CONTENT_TYPES",
    "bootstrap_default",
    "canonical",
    "candidates",
    "candidates_cached",
    "describe",
    "exists",
    "get",
    "names",
    "reload",
    "resolve",
    "resolve_cached",
]


#: Names that change when the dictionary is reloaded. Read through `__getattr__` so
#: there is one copy of the truth (`registry`) rather than two that can drift.
_LAZY = {"PROFILES", "FALLBACK", "SOURCE_ORDER", "GENERATION"}


def __getattr__(name: str):
    """`profiles.PROFILES` etc. read from the registry, never snapshotted.

    Without this, `profiles.PROFILES` would keep pointing at the tuple that existed at
    import time and `reload()` would look like it did nothing — the class of bug
    #134's generation counter exists to prevent, one layer up.
    """
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from server.profiles import registry

    if name == "GENERATION":
        return registry.generation()
    return getattr(registry, name)
