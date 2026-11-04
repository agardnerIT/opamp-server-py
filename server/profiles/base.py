"""What kind of agent is on the other end (#118).

The OpAMP protocol is deliberately **type-blind**. `AgentDescription`,
`AvailableComponents`, `EffectiveConfig` and `ComponentHealth` are generic
containers: the spec says component status values are "defined by agent-specific
semantics and not at the protocol level", and component attributes are "outside the
concerns of the OpAMP protocol". A server that stores every config body as opaque
bytes plus its `content_type` is fully conformant — the `generic` profile is the
protocol baseline, not a degraded collector path.

The spec does give one type-shaped signal, and it is a semantic convention rather
than a field: `AgentDescription.identifying_attributes.service.name` SHOULD be "a
reverse FQDN that uniquely identifies the Agent type, e.g.
`io.opentelemetry.collector`". `AgentConfigObject.content_type` describes the
*format* of a body. Nothing else in the protocol says "which product is this", so
that is a **local** concept this server derives, records and shows.

Three rules follow, and they are the reason this module exists as a separate layer:

1. **A profile may only ever subtract from what the server claims or does.** Type
   never grants anything: it decides whether the server *shows* a component
   inventory, a "heavy"/"outdated" report, a structural verdict, an `otelcol_*`
   telemetry view and a collector-YAML snippet — or says "I cannot tell".
   Everything an agent sends is unverifiable anyway (the server authenticates the
   channel, not the payload), so a spoofed type can only buy a wrong *label*, never
   a privilege.
2. **Every resolution carries its provenance.** `source` is `pinned` (an operator
   said so, server-side), `declared` (the agent's `service.name`/`content_type`
   said so), `sniffed` (the payloads look like it) or `fallback` (`generic`).
   A verdict or badge that cannot name its source is not allowed to claim anything.
3. **Format-agnostic checks are never type-dependent.** Size, UTF-8, duplicate
   keys, the `<redacted>` refusal and approval-required apply to every profile.
   They live in `config_validation`, not behind a profile hook, so a later
   "collector fast path" cannot skip them.

Pure definitions: no FastAPI, no SQLite, no imports from the rest of `server/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

#: How a resolution was reached, weakest to strongest. `pinned` is the only one a
#: human controls (it lives on the server, in the `agents.agent_type` column).
SOURCES = ("fallback", "sniffed", "declared", "pinned")

#: The name used when nothing matched. Also the name an operator pins to say
#: "stop interpreting this agent".
FALLBACK_PROFILE = "generic"

#: Content types that describe a *format*, not a product. A profile may accept these
#: (`ConfigFormat.claims`) but they never *identify* it (`ConfigFormat.declares`):
#: a FluentBit config and a collector config are both `text/yaml`, so treating a
#: generic content type as "this is a collector" would be wrong for exactly the
#: agents this issue exists for.
YAML_CONTENT_TYPES = frozenset(
    {"", "text/yaml", "application/yaml", "text/x-yaml", "application/x-yaml"}
)


def normalise_content_type(value: Any) -> str:
    """`"Text/YAML; charset=utf-8"` -> `"text/yaml"`. Never raises."""
    if value is None or not isinstance(value, str):
        return ""
    return value.split(";")[0].strip().lower()


@dataclass(frozen=True)
class ConfigFormat:
    """One body format a profile accepts, and the filename to download it as.

    `distinctive` is the difference between "I can read this" and "this is mine".
    Only a distinctive content type may be used as *evidence of identity* — see
    :func:`server.profiles.registry.resolve`. Nothing ships with one today (there is
    no registered `application/x-otelcol-config` MIME type in the wild); the flag
    exists so a profile that really does own a MIME type can say so without a
    second mechanism.
    """

    content_types: Tuple[str, ...] = ()
    default_filename: str = "config.yaml"
    distinctive: bool = False

    def claims(self, content_type: Any) -> bool:
        """True when this format accepts `content_type` (empty means undeclared)."""
        return normalise_content_type(content_type) in self.content_types

    def declares(self, content_type: Any) -> bool:
        """True when `content_type` identifies *this* profile, not just its format."""
        return self.distinctive and self.claims(content_type)


# --------------------------------------------------------------- description ---


def flatten_description(description: Any) -> Dict[str, str]:
    """`{key: scalar}` from a `MessageToDict` `AgentDescription`.

    Both attribute lists are flattened into one namespace (they are both agent
    description to a matcher), non-scalar values are skipped, and ``None`` keys are
    dropped. Cheap on purpose: this runs once per agent per resolution attempt.
    """
    if not isinstance(description, Mapping):
        return {}
    attrs: Dict[str, str] = {}
    for group in ("identifyingAttributes", "nonIdentifyingAttributes"):
        entries = description.get(group)
        if not isinstance(entries, Sequence):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            key = entry.get("key")
            value = entry.get("value")
            if not isinstance(key, str) or not isinstance(value, Mapping):
                continue
            for scalar in value.values():
                if isinstance(scalar, (str, int, float, bool)):
                    attrs[key] = str(scalar)
                    break
    return attrs


def attr_prefix(key: str, prefixes: Iterable[str]) -> Callable[[Mapping[str, str]], bool]:
    """Matcher: attribute `key` starts with any of `prefixes` (case-insensitive).

    Prefix rather than equality because `service.name` carries the distro as well as
    the product (`otelcol`, `otelcol-contrib`, `unknown_service:otelcol`), and a new
    distro must not need a server release.
    """
    wanted = tuple(prefix.casefold() for prefix in prefixes)

    def matcher(attrs: Mapping[str, str]) -> bool:
        value = (attrs.get(key) or "").casefold()
        return any(value.startswith(prefix) for prefix in wanted)

    matcher.__name__ = f"attr_prefix({key!r}, {wanted!r})"
    return matcher


def attr_equals(key: str, values: Iterable[str]) -> Callable[[Mapping[str, str]], bool]:
    """Matcher: attribute `key` is exactly one of `values` (case-insensitive)."""
    wanted = frozenset(value.casefold() for value in values)

    def matcher(attrs: Mapping[str, str]) -> bool:
        return (attrs.get(key) or "").casefold() in wanted

    matcher.__name__ = f"attr_equals({key!r}, {sorted(wanted)!r})"
    return matcher


# -------------------------------------------------------------------- profile ---


@dataclass(frozen=True)
class AgentProfile:
    """One agent type's answer to "what may the server claim about this agent?".

    Every hook defaults to the opaque answer, so a new profile really is one file:
    name it, list its formats, and add whichever hooks it earns. A profile with no
    `sniff`/`description_matchers` can still be pinned by an operator.
    """

    name: str
    display_name: str
    summary: str = ""
    #: Formats this profile accepts. Empty means "anything, as opaque bytes".
    config_formats: Tuple[ConfigFormat, ...] = ()
    #: May the server judge the *structure* of a body for this agent? (#119 reads it)
    validates_structure: bool = False
    #: Evidence of identity from `AgentDescription` attributes.
    description_matchers: Tuple[Callable[[Mapping[str, str]], bool], ...] = ()
    #: Evidence of identity from the payloads (`AvailableComponents`, config).
    sniff: Optional[Callable[[Any], bool]] = None
    #: Component inventory, or `None` for "this profile has no such concept".
    components: Optional[Callable[[Any], Dict[str, list]]] = None
    #: Which fleet report generators apply. Empty means none of them do.
    reports: Tuple[str, ...] = ()
    #: Metric-name mapping for the telemetry view, or `None` for "not expected".
    telemetry: Any = None
    #: Bootstrap snippet builder: `(endpoint, token) -> str`.
    snippet: Optional[Callable[[str, str], str]] = None

    # -- formats ---------------------------------------------------------

    def claims(self, content_type: Any) -> bool:
        """True when this profile can read a body of `content_type`."""
        return any(fmt.claims(content_type) for fmt in self.config_formats)

    def declares(self, content_type: Any) -> bool:
        """True when `content_type` alone identifies this profile."""
        return any(fmt.declares(content_type) for fmt in self.config_formats)

    def filename_for(self, content_type: Any = "") -> str:
        """The download filename for a body of `content_type`."""
        for fmt in self.config_formats:
            if fmt.claims(content_type):
                return fmt.default_filename
        return self.config_formats[0].default_filename if self.config_formats else "config"

    # -- identity --------------------------------------------------------

    def matches_description(self, attrs: Mapping[str, str]) -> bool:
        """True when the agent's own description says it is this profile."""
        return any(matcher(attrs) for matcher in self.description_matchers)

    def sniffed_by(self, agent: Any) -> bool:
        """True when the agent's payloads look like this profile.

        Never raises: a malformed report is evidence of nothing, not an error.
        """
        if self.sniff is None:
            return False
        try:
            return bool(self.sniff(agent))
        except Exception:  # pragma: no cover - defensive, a sniffer must not 500
            return False

    # -- behaviour -------------------------------------------------------

    def extract_components(self, agent: Any) -> Dict[str, list]:
        """Component inventory, or `{}` when this profile has no such concept."""
        if self.components is None:
            return {}
        try:
            return self.components(agent)
        except Exception:  # pragma: no cover - a bad report must not 500 a listing
            return {}

    def bootstrap_snippet(self, endpoint: str, token: str) -> str:
        """Copy-paste onboarding text for this profile. Empty when unknown."""
        if self.snippet is None:
            return ""
        return self.snippet(endpoint, token)

    def describe(self) -> Dict[str, Any]:
        """The JSON shape `GET /agent/types` returns. No callables, no secrets."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "summary": self.summary,
            "validates_structure": self.validates_structure,
            "content_types": sorted(
                {ct for fmt in self.config_formats for ct in fmt.content_types}
            ),
            "default_filename": self.filename_for(),
            "reports": list(self.reports),
            "reports_telemetry": self.telemetry is not None,
            "detectable": bool(self.description_matchers or self.sniff),
        }


@dataclass(frozen=True)
class Resolution:
    """A profile plus how it was chosen. `source` is in :data:`SOURCES`."""

    profile: AgentProfile
    source: str

    @property
    def name(self) -> str:
        return self.profile.name
