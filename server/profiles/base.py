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

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

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


# ------------------------------------------------------------- data shapes ----
#
# Everything below is a *declaration*, not behaviour: #134 moved the dictionary out
# of Python callables so a new agent type is one YAML file and a restart. These
# dataclasses are the compiled, frozen form the rest of the server reads, and they
# are also what `GET /agent/types` serialises — so a field that is not here cannot
# be promised in the API.


@dataclass(frozen=True)
class IdentityRule:
    """One `AgentDescription` attribute test: `{attr, prefix: [...]}` or `{attr, equals: [...]}`.

    Prefix rather than equality is the common case because `service.name` carries the
    distro as well as the product (`otelcol`, `otelcol-contrib`,
    `unknown_service:otelcol`), and a new distro must not need a server release.
    Exactly one of the two may be set; the loader rejects a rule with both or neither.
    """

    attr: str
    prefix: Tuple[str, ...] = ()
    equals: Tuple[str, ...] = ()

    def matches(self, attrs: Mapping[str, str]) -> bool:
        """Case-insensitive, both sides (#123).

        The attribute value is folded and so are the declared strings: `Fluent Bit` in a
        dictionary file is how an operator writes a product name, and a matcher that only
        worked for lower-case entries would fail silently — the agent would just resolve
        to the fallback profile with no error anywhere.
        """
        value = (attrs.get(self.attr) or "").casefold()
        if self.prefix:
            return any(value.startswith(wanted.casefold()) for wanted in self.prefix)
        return value in {entry.casefold() for entry in self.equals}

    def to_dict(self) -> Dict[str, Any]:
        rule: Dict[str, Any] = {"attr": self.attr}
        if self.prefix:
            rule["prefix"] = list(self.prefix)
        if self.equals:
            rule["equals"] = list(self.equals)
        return rule


@dataclass(frozen=True)
class SniffSpec:
    """Evidence of identity from the payloads, when the agent does not say.

    Both halves are emptiness tests on data the agent already sent: the section names
    it used in `AvailableComponents`, and the top-level sections of its config body.
    """

    component_sections_any: Tuple[str, ...] = ()
    config_sections_any: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.component_sections_any:
            payload["component_sections_any"] = list(self.component_sections_any)
        if self.config_sections_any:
            payload["config_sections_any"] = list(self.config_sections_any)
        return payload


@dataclass(frozen=True)
class TelemetrySpec:
    """Which internal metrics this profile's agents report, and what to call them (#122).

    `metric_prefix` is the coarse declaration (`otelcol_*` -> `otelcol_`);
    `metric_map` is the escape hatch for a profile whose metric names carry no common
    prefix. A profile with no `telemetry` at all is saying "this agent reports nothing
    to this server", which is a different sentence from "nothing arrived yet".

    `title` / `caption` / `raw_caption` are the words the agent-detail page uses
    (#121). They are data for the same reason `panels` are: "Collector Telemetry" is
    correct for a collector and wrong for every other agent, and the UI may not keep a
    product's vocabulary in Python.
    """

    metric_prefix: str = ""
    metric_map: Mapping[str, str] = field(default_factory=dict)
    title: str = "Telemetry"
    caption: str = ""
    raw_caption: str = ""

    def owns(self, name: Any) -> bool:
        """True when `name` is one of this profile's metrics."""
        if not isinstance(name, str):
            return False
        if self.metric_map and name in self.metric_map:
            return True
        return bool(self.metric_prefix) and name.startswith(self.metric_prefix)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "title": self.title,
            "caption": self.caption,
            "raw_caption": self.raw_caption,
        }
        if self.metric_prefix:
            payload["metric_prefix"] = self.metric_prefix
        if self.metric_map:
            payload["metric_map"] = dict(self.metric_map)
        return payload


@dataclass(frozen=True)
class ComponentsSpec:
    """How to read a component inventory out of `AvailableComponents` (#120 reuses this).

    Every knob here is a convention *of the agent's payload*, not of this server, which
    is why it is data: `sections` says which report keys exist and what they are called
    (the keys are plural, the types singular), `known` names components that report no
    section at all, and `version` says where a version hides in a component's metadata.
    """

    sections: Mapping[str, str] = field(default_factory=dict)
    known: Mapping[str, str] = field(default_factory=dict)
    version_metadata_key: str = "code.namespace"
    version_separator: str = " v"
    health_scope_prefix: str = "pipeline:"
    health_extensions_scope: str = "extensions"
    health_child_separator: str = ":"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sections": dict(self.sections),
            "version_metadata_key": self.version_metadata_key,
            "health_scope_prefix": self.health_scope_prefix,
            "health_extensions_scope": self.health_extensions_scope,
        }


@dataclass(frozen=True)
class PanelMetric:
    """One tile: a label, a metric name, and how to render its value."""

    label: str
    metric: str
    format: str = "number"

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "metric": self.metric, "format": self.format}


@dataclass(frozen=True)
class Panel:
    """One block of the agent-detail page, declared instead of hardcoded (#121 renders it).

    `widget` is a member of `server.profiles.schema.WIDGETS`, which is closed on
    purpose: a profile cannot invent a widget, because there would be no renderer.
    """

    id: str
    widget: str
    title: str = ""
    caption: str = ""
    metrics: Tuple[PanelMetric, ...] = ()
    #: `alert` widget: the metric to watch, the condition and how to shout about it.
    metric: str = ""
    when: str = ""
    severity: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"id": self.id, "widget": self.widget}
        for name in ("title", "caption", "when", "severity", "message", "metric"):
            value = getattr(self, name)
            if value:
                payload[name] = value
        if self.metrics:
            payload["metrics"] = [metric.to_dict() for metric in self.metrics]
        return payload


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
    #: Sidebar/badge glyph. Data, not a `if name == "otelcol"` branch (#121).
    icon: str = "📦"
    #: Match order, high to low. Explicit because filesystem order is not a policy:
    #: a locale, a rename or a parallel checkout would each change fleet behaviour
    #: silently. Two profiles sharing a priority is a load error, not a coin flip.
    priority: int = 0
    #: Formats this profile accepts. Empty means "anything, as opaque bytes".
    config_formats: Tuple[ConfigFormat, ...] = ()
    #: May the server judge the *structure* of a body for this agent?
    validates_structure: bool = False
    #: The rules that do it, when `validates_structure` is set (#119):
    #: `validate(doc, path, ctx, verdict) -> bool`, where the return value says whether
    #: this body was actually judged (an engine may decline a document that is not its
    #: product's shape — that is a fact the approve step needs, not an error).
    structure: Optional[Callable] = None
    #: This product's "address" sections for the merge-collision warning (#119); empty
    #: means the shipped defaults in `server.config_vocabulary`.
    address_sections: Tuple[str, ...] = ()
    #: Whether an OCB build manifest is a meaningful artefact for this product (#120).
    ocb_manifest: bool = False
    #: Evidence of identity from `AgentDescription` attributes.
    identity_match_any: Tuple[IdentityRule, ...] = ()
    #: The same evidence in declared form, for the API and for `--strict-profiles`.
    sniff_spec: Optional[SniffSpec] = None
    #: Evidence of identity from the payloads (`AvailableComponents`, config).
    sniff: Optional[Callable[[Any], bool]] = None
    #: Component inventory, or `None` for "this profile has no such concept".
    components: Optional[Callable[[Any], Dict[str, list]]] = None
    #: The component-reading conventions behind `components`, for the API.
    components_spec: Optional[ComponentsSpec] = None
    #: Which fleet report generators apply. Empty means none of them do.
    reports: Tuple[str, ...] = ()
    #: Metric-name mapping for the telemetry view, or `None` for "not expected".
    telemetry: Optional[TelemetrySpec] = None
    #: Agent-detail panels this profile declares. Empty means generic chrome (#121).
    panels: Tuple[Panel, ...] = ()
    #: Bootstrap snippet template, compiled into `snippet`. Data, for the API.
    snippet_template: str = ""
    #: Bootstrap snippet builder: `(endpoint, token) -> str`.
    snippet: Optional[Callable[..., str]] = None
    #: The profile's *whole config* for a new agent, compiled (#121). Empty when the
    #: profile has no such artefact, in which case `bootstrap_snippet` is the answer.
    onboarding_template: str = ""
    onboarding: Optional[Callable[..., str]] = None
    #: Per-profile prose for the Help page. The product's own caveats, not the UI's.
    guidance: str = ""
    #: Which profile's snippet to show when the agent is not known yet (token minting
    #: precedes the first connection). At most one profile may say yes; the loader
    #: enforces it, and `generic` is used when nobody does.
    bootstrap_default: bool = False

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
        return any(rule.matches(attrs) for rule in self.identity_match_any)

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

    def onboarding_snippet(
        self,
        endpoint: str,
        token: str,
        *,
        auth: bool = True,
        agent_name: str = "",
    ) -> str:
        """The profile's whole config for a new agent, or `""` when it has none.

        `auth` is the server's mode (#124): with it the credential block is rendered,
        without it the config is exactly what an `off` server always showed. An empty
        return is not a failure — it means "this product's configuration is not
        something this server can write", and the caller shows the bootstrap block
        instead of inventing one (#121).
        """
        if self.onboarding is None:
            return ""
        return self.onboarding(endpoint, token, auth=auth, agent_name=agent_name)

    @property
    def has_components(self) -> bool:
        """True when a component inventory is a concept this profile has at all.

        The UI asks this before drawing an empty table: "no components reported" is
        information for a collector, and meaningless for an agent whose product has
        no components to report (#121).

        True when the profile has *any* extractor (#123): `components_spec` reads the
        agent's report, a `components_engine` reads its config. A product that reports no
        inventory but whose config names its plugins still has components.
        """
        return self.components is not None

    def detect_component_type(self, component_id: str) -> str:
        """Component type from its id (`receivers/otlp` -> `receiver`), or `unknown`.

        Data-driven (#134): a profile without a `components` spec has no such concept,
        so the honest answer is `unknown` rather than an empty collector.
        """
        spec = self.components_spec
        if spec is None or not isinstance(component_id, str):
            return "unknown"
        for section, type_name in spec.sections.items():
            if component_id.startswith(f"{section}/"):
                return type_name
        if component_id in spec.known:
            return spec.known[component_id]
        if "/" in component_id:
            head = component_id.split("/")[0]
            for section, type_name in spec.sections.items():
                if head in (section, type_name):
                    return type_name
            if head in spec.known:
                return spec.known[head]
        return "unknown"

    def describe(self) -> Dict[str, Any]:
        """The JSON shape `GET /agent/types` returns. No callables, no secrets.

        #134 added `icon`, `priority`, `identity` and `panels`, so the four layers
        (client, CLI, MCP, skill) have to carry them in the same change as the schema —
        an API field only the server knows about is a field nobody can test.
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "icon": self.icon,
            "priority": self.priority,
            "summary": self.summary,
            "guidance": self.guidance,
            "validates_structure": self.validates_structure,
            # #119: which rules judge a body, if any. A name, not a callable: the API
            # says *that* this profile interprets bodies, and the code is loaded anyway.
            "structure_engine": getattr(self.structure, "ENGINE", None),
            "address_sections": list(self.address_sections),
            "content_types": sorted(
                {ct for fmt in self.config_formats for ct in fmt.content_types}
            ),
            "default_filename": self.filename_for(),
            "reports": list(self.reports),
            "reports_telemetry": self.telemetry is not None,
            "telemetry": self.telemetry.to_dict() if self.telemetry else None,
            "detectable": bool(self.identity_match_any or self.sniff),
            "has_components": self.has_components,
            # #120: OCB is a builder, not a validator: "unsupported" is an answer.
            "ocb_manifest": self.ocb_manifest,
            "has_onboarding_snippet": self.onboarding is not None,
            "identity": {
                "match_any": [rule.to_dict() for rule in self.identity_match_any],
                "sniff": self.sniff_spec.to_dict() if self.sniff_spec else None,
            },
            "panels": [panel.to_dict() for panel in self.panels],
            "components": self.components_spec.to_dict() if self.components_spec else None,
            "bootstrap_default": self.bootstrap_default,
        }


@dataclass(frozen=True)
class Resolution:
    """A profile plus how it was chosen. `source` is in :data:`SOURCES`."""

    profile: AgentProfile
    source: str

    @property
    def name(self) -> str:
        return self.profile.name
