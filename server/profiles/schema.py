"""The profile dictionary's file format (#134).

One YAML file per agent type. This module is the **schema** — the shape a file must
have before the loader will compile it into an `AgentProfile`. It is deliberately
strict: a dictionary that silently ignores a key it does not understand is a
dictionary that lies about what it controls.

Top-level shape, with the `otelcol` file as the worked example
(`server/profiles/data/otelcol.yaml`)::

    name: otelcol                  # the identifier stored in agents.agent_type
    display_name: OpenTelemetry Collector
    icon: "🧩"
    priority: 100                  # match order, high wins; ties are a load error
    summary: >                     # shown by GET /agent/types
      ...
    config_formats:
      - content_types: ["", "text/yaml", ...]
        default_filename: otelcol-config.yaml
        distinctive: false         # only a distinctive type may *identify* a profile
    validates_structure: true      # requires `components` and `structure` (rule 4)
    structure: {engine: otelcol}   # which rules judge a body (#119)
    identity:
      match_any:
        - attr: service.name
          prefix: ["otelcol", "io.opentelemetry.collector"]
      sniff:
        component_sections_any: [receivers, exporters]
        config_sections_any: [receivers, service]
    reports: [fleet, agent, heavy, outdated]
    telemetry:
      metric_prefix: "otelcol_"
    components:
      sections: {receivers: receiver, ...}
      known: {otlp: receiver, ...}
    snippet: |
      ...
      {scheme}:
        endpoint: {endpoint}
        headers:
          Authorization: "Bearer {token}"
    panels:
      - id: data-flow-inputs
        widget: metrics
        title: Data Flow (Inputs)
        metrics: [{label: Logs In, metric: otelcol_receiver_accepted_log_records}]
    bootstrap_default: true        # at most one profile; see AgentProfile

Three rules are enforced here rather than documented and hoped for:

1. **No code from data.** A file may not name a Python module to import. There is no
   `hooks` key at all: every behaviour a profile has is a field this schema
   understands, compiled by `server.profiles.engines`. An editable dictionary that
   could import arbitrary code is remote code execution with a YAML front end, and
   the cheapest way not to have that bug is not to have the key.
2. **A profile may only subtract** (`base.py`, rule 1). Nothing here can grant a
   capability, a trust level or a validation exemption; `validates_structure` is the
   one field that could make the server *less* safe, so it requires a component spec
   *and* a `structure` block naming a known engine (#119) — a profile may only claim
   to interpret bodies if it brings the rules to do it.
3. **A bad file must not take the server down.** Invalid document -> the profile is
   absent and the fleet is still served (see `loader.py`).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The report generators a profile may ask for. Closed: naming a report nobody
#: implements would silently promise a page section that never renders.
REPORTS = ("fleet", "agent", "heavy", "outdated")

#: The widgets a panel may use. Closed on purpose — a widget with no renderer in
#: `ui/` is a promise the UI cannot keep. This set grows only when #121 (or a later
#: issue) ships a renderer for the new member.
WIDGETS = ("metrics", "alert")

#: How a metric tile renders its value. `mebibytes` is the only one with arithmetic:
#: the collector reports bytes and operators read MB.
METRIC_FORMATS = ("number", "decimal", "mebibytes")

#: Conditions an `alert` panel can test. Kept to the one the UI actually shows today
#: ("failed > 0 is an error") rather than inventing a comparison language.
ALERT_WHENS = ("above_zero",)

#: Severity of an `alert` panel, mapped to a Streamlit call by the renderer.
ALERT_SEVERITIES = ("info", "warning", "error")

#: Placeholders a snippet template may use. `scheme` is the transport block name the
#: OpAMP spec's YAML examples use (`ws` for a websocket endpoint, `http` otherwise) —
#: that is a rule about the protocol, not about one product, so it lives here and not
#: in every profile file.
SNIPPET_FIELDS = ("endpoint", "token", "scheme")


class _Strict(BaseModel):
    """Reject unknown keys. A typo'd field that is silently ignored is a lie."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigFormatDocument(_Strict):
    content_types: Tuple[str, ...] = ()
    default_filename: str = "config.yaml"
    distinctive: bool = False

    @field_validator("content_types")
    @classmethod
    def _normalise(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        """`Text/YAML; charset=utf-8` and `text/yaml` are the same format."""
        from server.profiles.base import normalise_content_type

        return tuple(normalise_content_type(item) for item in value)


class IdentityRuleDocument(_Strict):
    """`{attr, prefix: [...]}` or `{attr, equals: [...]}` — exactly one."""

    attr: str
    prefix: Tuple[str, ...] = ()
    equals: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.prefix) == bool(self.equals):
            raise ValueError("identity.match_any entry needs exactly one of prefix/equals")
        if not self.attr.strip():
            raise ValueError("identity.match_any entry needs an attr")
        return self


class SniffDocument(_Strict):
    component_sections_any: Tuple[str, ...] = ()
    config_sections_any: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.component_sections_any and not self.config_sections_any:
            raise ValueError(
                "identity.sniff must name component_sections_any or config_sections_any; "
                "an empty sniffer is dead weight"
            )
        return self


class IdentityDocument(_Strict):
    match_any: Tuple[IdentityRuleDocument, ...] = ()
    sniff: Optional[SniffDocument] = None


class TelemetryDocument(_Strict):
    """What this profile's agents report to `POST /v1/metrics`, and what to call it.

    The mapping half (`metric_prefix` / `metric_map`) is the declaration; the three
    text fields are the heading the agent-detail page renders above its panels. They
    are here rather than in `ui/` because the wording is a fact about the product:
    "Collector Telemetry" is accurate for a collector and a lie for anything else,
    and #121's whole job is that no non-collector is ever shown the collector's words.
    """

    metric_prefix: str = ""
    metric_map: Dict[str, str] = Field(default_factory=dict)
    #: Heading above the panels, and the one-line explanation under it.
    title: str = "Telemetry"
    caption: str = ""
    #: The expander that dumps every reported metric, and what it says it is dumping.
    raw_caption: str = ""

    @model_validator(mode="after")
    def _some_mapping(self):
        if not self.metric_prefix and not self.metric_map:
            raise ValueError("telemetry must declare metric_prefix or metric_map")
        return self


class ComponentsDocument(_Strict):
    """The agent's payload conventions, so the loader can build the inventory reader."""

    sections: Dict[str, str] = Field(default_factory=dict)
    known: Dict[str, str] = Field(default_factory=dict)
    version_metadata_key: str = "code.namespace"
    version_separator: str = " v"
    health_scope_prefix: str = "pipeline:"
    health_extensions_scope: str = "extensions"
    health_child_separator: str = ":"

    @model_validator(mode="after")
    def _has_sections(self):
        if not self.sections:
            raise ValueError(
                "components.sections is required: without it the inventory is always empty"
            )
        return self


class PanelMetricDocument(_Strict):
    label: str
    metric: str
    format: str = "number"

    @field_validator("format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        if value not in METRIC_FORMATS:
            raise ValueError(f"panel metric format {value!r} is not one of {METRIC_FORMATS}")
        return value


class PanelDocument(_Strict):
    id: str
    widget: str
    title: str = ""
    caption: str = ""
    metrics: Tuple[PanelMetricDocument, ...] = ()
    metric: str = ""
    when: str = ""
    severity: str = ""
    message: str = ""

    @field_validator("widget")
    @classmethod
    def _known_widget(cls, value: str) -> str:
        if value not in WIDGETS:
            raise ValueError(f"panel widget {value!r} is not one of {WIDGETS}")
        return value

    @model_validator(mode="after")
    def _widget_shape(self):
        if self.widget == "metrics":
            if not self.metrics:
                raise ValueError("a metrics panel needs at least one metric")
        elif self.widget == "alert":
            if not self.metric:
                raise ValueError("an alert panel needs a metric")
            if self.when not in ALERT_WHENS:
                raise ValueError(f"alert when {self.when!r} is not one of {ALERT_WHENS}")
            if self.severity not in ALERT_SEVERITIES:
                raise ValueError(
                    f"alert severity {self.severity!r} is not one of {ALERT_SEVERITIES}"
                )
            if not self.message:
                raise ValueError("an alert panel needs a message")
        return self


class StructureDocument(_Strict):
    """Which rules judge a body's **structure**, for this product (#119).

    `engine` names a closed set the loader knows (`server.profiles.engines`); a file
    cannot name a Python module, because an editable dictionary that could import code
    is remote code execution with a YAML front end (`schema.py` rule 1). Adding a
    product that the server interprets means adding an engine — that is the price of
    judging a config, and it is paid once per product rather than once per push.

    `address_sections` is this product's vocabulary for "this key names a thing, so a
    second config source defining it is a merge conflict rather than an override". Empty
    means "use the shipped defaults" (`server.config_vocabulary`).
    """

    engine: str
    address_sections: Tuple[str, ...] = ()


class ProfileDocument(_Strict):
    """One agent type, as authored. The loader compiles this; nothing else reads it."""

    name: str
    display_name: str
    icon: str = "📦"
    priority: int = 0
    summary: str = ""
    config_formats: Tuple[ConfigFormatDocument, ...] = ()
    validates_structure: bool = False
    #: The rules that judge this product's config structure (#119). `validates_structure`
    #: says *that* a body is judged; this says *by what*, and the two are checked
    #: against each other so a profile cannot claim one without the other.
    structure: Optional[StructureDocument] = None
    #: A named extractor for products that report their inventory **in the config**
    #: rather than through `AvailableComponents` (#123). Empty means the `components`
    #: block's engine (which reads the report) is what applies.
    components_engine: str = ""
    #: Is this product built by the OpenTelemetry Collector Builder? "Generate a slim
    #: distro manifest" is that builder's concept, so it is data: a product that is not
    #: built that way answers 409 instead of "you have no components in use", which is
    #: true and misleading at the same time (#120).
    ocb_manifest: bool = False
    identity: IdentityDocument = Field(default_factory=IdentityDocument)
    reports: Tuple[str, ...] = ()
    telemetry: Optional[TelemetryDocument] = None
    components: Optional[ComponentsDocument] = None
    snippet: str = ""
    #: A complete, runnable config for a *new* agent of this type (#121). `snippet` is
    #: the fragment an existing config gains; this is the whole file an operator with
    #: an empty fleet pastes and runs. Empty means "this profile has no such thing" —
    #: the UI then falls back to `snippet`, which is the honest answer for a product
    #: whose config this server cannot write.
    onboarding_snippet: str = ""
    #: Per-profile prose for the Help page: what this product does with a pushed
    #: config, what happens when one is wrong, what it must advertise. Claims like
    #: "an invalid config leaves the collector down" are true of one product, so they
    #: live in that product's file instead of being shown as general truth (#121).
    guidance: str = ""
    panels: Tuple[PanelDocument, ...] = ()
    bootstrap_default: bool = False

    @field_validator("name")
    @classmethod
    def _name_is_a_key(cls, value: str) -> str:
        """`name` is stored in `agents.agent_type` and typed by operators: keep it boring."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name is required")
        if cleaned != cleaned.casefold():
            raise ValueError("name must be lower case (it is compared case-insensitively)")
        if not all(char.isalnum() or char in "-_" for char in cleaned):
            raise ValueError("name may only contain letters, digits, '-' and '_'")
        return cleaned

    @field_validator("reports")
    @classmethod
    def _known_reports(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        unknown = [report for report in value if report not in REPORTS]
        if unknown:
            raise ValueError(f"unknown report(s) {unknown}; expected some of {REPORTS}")
        return value

    @field_validator("panels")
    @classmethod
    def _unique_panels(cls, value: Tuple[PanelDocument, ...]) -> Tuple[PanelDocument, ...]:
        seen = set()
        for panel in value:
            if panel.id in seen:
                raise ValueError(f"duplicate panel id {panel.id!r}")
            seen.add(panel.id)
        return value

    @model_validator(mode="after")
    def _snippet_placeholders(self):
        """A snippet must carry the endpoint and the credential, and nothing else."""
        from server.profiles.engines import check_template

        if self.snippet:
            problem = check_template(self.snippet)
            if problem:
                raise ValueError(problem)
        if self.onboarding_snippet:
            # The credential arrives inside `{auth}`, so only the endpoint is required
            # to appear literally in a full config.
            problem = check_template(self.onboarding_snippet, required=("endpoint",))
            if problem:
                raise ValueError(f"onboarding_snippet: {problem}")
        return self

    @model_validator(mode="after")
    def _structure_needs_components_and_an_engine(self):
        """Guardrail 4, after #119: a claim to judge structure needs rules to judge by.

        A profile that says `validates_structure: true` without naming an engine would be
        claiming the authority and having none — either a load error or, worse, a silent
        fallback to someone else's rules. The reverse is refused too: shipping an engine
        no field points at is a typo, not a feature.
        """
        if self.validates_structure and self.structure is None:
            raise ValueError(
                "validates_structure: true requires a structure block naming its engine "
                "(#119); a profile that judges bodies must say which rules do it"
            )
        if self.structure is not None and not self.validates_structure:
            raise ValueError(
                "a structure block requires validates_structure: true — naming an engine "
                "without claiming the judgement is a field nobody reads"
            )
        if self.validates_structure and self.components is None and not self.components_engine:
            raise ValueError(
                "validates_structure: true requires a components block (or a "
                "components_engine): a profile that judges a config's structure also "
                "knows what that config is made of"
            )
        return self

    @model_validator(mode="after")
    def _one_component_source(self):
        """Two ways to inventory components is one ambiguity per agent (#123).

        `components` reads what the agent *reports*; `components_engine` reads what the
        agent *runs*, for a product that reports no inventory. A profile that names both
        would have two answers to "which components does this agent have", so it is a
        load error rather than a precedence rule nobody remembers.
        """
        if self.components is not None and self.components_engine:
            raise ValueError(
                "declare either `components` (from the agent's report) or "
                "`components_engine` (from its config), not both"
            )
        if self.components_engine and self.components is None:
            # A product with components has a component concept; the UI keys off it.
            pass
        return self

    @model_validator(mode="after")
    def _distinctive_needs_a_type(self):
        for fmt in self.config_formats:
            if fmt.distinctive and not fmt.content_types:
                raise ValueError("a distinctive config format needs at least one content type")
        return self


def known_fields() -> List[str]:
    """Top-level keys a file may use — the documentation and the error message agree."""
    return list(ProfileDocument.model_fields)
