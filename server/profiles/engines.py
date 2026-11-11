"""Compile declared specs into the callables `AgentProfile` holds (#134).

Before #134 these behaviours were Python functions inside `server/profiles/otelcol.py`.
Now they are read from a profile's YAML and built here, which is the whole point:
`otelcol` is one file, and the only Python that runs on it is in this module.

Every function here takes a spec as an argument and returns a closure. None of them
knows a product name — a knob that is genuinely collector-specific (`code.namespace`,
`pipeline:`, `subComponentMap`) is a *field with a default* on
`base.ComponentsSpec`, so a different product overrides it in its own file instead of
needing a code change.
"""

from __future__ import annotations

import json
import string
from collections import defaultdict
from typing import Any, Callable, Dict, Mapping, Sequence, Set, Tuple

from loguru import logger

from server.profiles.base import ComponentsSpec, SniffSpec


# --------------------------------------------------------------------- snippet ---


def transport_scheme(endpoint: str) -> str:
    """The YAML block name for an OpAMP endpoint: `ws` for a websocket URL, else `http`.

    A rule about the protocol rather than about one product, which is why a snippet
    template can ask for `{scheme}` instead of every dictionary file shipping both
    variants. Matches what `server.agent_auth.snippet_endpoint` builds.
    """
    return "ws" if (endpoint or "").startswith("ws") else "http"


def auth_block(token: str, indent: str = "        ") -> str:
    """The credential block a template's `{auth}` stands for, or `""` for none.

    This is the OpAMP spec's own shape (`server.<transport>.headers.Authorization`),
    so it is protocol knowledge like `{scheme}`, not product knowledge — which is why
    it is a template field the engine fills in rather than seven lines every
    dictionary file would have to repeat. A profile that wants no credential block
    simply does not use `{auth}` (#121).

    The indent belongs to the *block*, not to a product: a YAML endpoint sits at a
    fixed depth inside `extensions.opamp.server.<transport>`, and a template cannot
    indent a multi-line substitution by itself.
    """
    return f'{indent}headers:\n{indent}  Authorization: "Bearer {token}"\n'


def check_template(
    template: str, required: Sequence[str] = ("endpoint", "token")
) -> str:
    """`""` when `template` is usable, else the reason it is not.

    Checked at load time, not on first use: a snippet with a typo'd placeholder would
    otherwise raise the first time an operator opened the Help page, and "a bad file
    must not take the server down" has to include this.

    `required` is per-template because the two kinds of snippet carry different
    obligations (#121): the API's bootstrap block must show *where* and *with which
    credential*, while a full onboarding config carries the credential inside `{auth}`
    and so only has to show where (#121).
    """
    fields = set()
    try:
        for _literal, field_name, spec, _conversion in string.Formatter().parse(template):
            if field_name is None:
                continue
            if spec or _conversion:
                return f"snippet placeholder {{{field_name}}} may not use a format spec"
            if field_name == "":
                return "snippet contains a bare '{}' with no field name"
            # `{scheme}` is fine; `{a.b}` / `{a[0]}` are attribute/index access we do
            # not intend to support, and silently accepting them invites a surprise.
            if not field_name.isidentifier():
                return f"snippet placeholder {{{field_name}}} is not a plain field name"
            fields.add(field_name)
    except ValueError as exc:  # unbalanced braces
        return f"snippet is not a valid template: {exc}"

    unknown = sorted(fields - set(_TEMPLATE_FIELDS))
    if unknown:
        return (
            f"snippet uses unknown placeholder(s) {unknown}; supported: "
            f"{list(_TEMPLATE_FIELDS)}"
        )
    missing = [name for name in required if name not in fields]
    if missing:
        return f"snippet must use {{{'}}} and {{{'.join(missing)}}}"
    return ""


_TEMPLATE_FIELDS: Tuple[str, ...] = (
    # Filled in from the endpoint by the engine.
    "endpoint",
    "scheme",
    "token",
    # Filled in from the server's configuration by the caller.
    "auth",
    "agent_name",
)


def snippet_from(template: str) -> Callable[..., str]:
    """Compile a snippet template into `(endpoint, token, *, auth, agent_name) -> str`.

    One compiler for both snippet kinds (#121). A template that does not use a field
    is unaffected by it, so the API's bootstrap block and a profile's full onboarding
    config run through the same rules: same placeholder set, same load-time check,
    same "no code from data" guarantee.
    """

    def builder(
        endpoint: str, token: str, *, auth: bool = True, agent_name: str = ""
    ) -> str:
        return template.format(
            endpoint=endpoint,
            token=token,
            scheme=transport_scheme(endpoint),
            agent_name=agent_name,
            auth=auth_block(token) if auth else "",
        )

    builder.__name__ = "snippet_from(dictionary)"
    return builder


# ----------------------------------------------------------------------- sniff ---


def _report(agent: Any) -> Any:
    """The stored `EffectiveConfig`, decoded from its JSON text form.

    `AgentState.effective_config` holds the report as the JSON text the agent sent, so
    every reader of it has to decode first (`effective_config.objects` takes the
    mapping). Decoded defensively and never raising: this runs on read paths, once per
    agent per resolution attempt.
    """
    stored = getattr(agent, "effective_config", None)
    if not stored:
        return None
    if isinstance(stored, (bytes, bytearray)):
        try:
            stored = bytes(stored).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(stored, str):
        try:
            return json.loads(stored)
        except ValueError:
            return None
    return stored


def _config_documents(agent: Any) -> Tuple[Mapping[str, Any], ...]:
    """Parsed config objects the agent reported.

    YAML objects only (`effective_config.objects` default): identity inference must
    keep seeing what the agent declared as its own format, never a JSON body it
    happened to expose for display.
    """
    from server import effective_config as effective_config_module

    found = []
    for obj in effective_config_module.objects(_report(agent)):
        if obj.parsed and isinstance(obj.document, Mapping):
            found.append(obj.document)
    return tuple(found)


def config_sections(agent: Any) -> Tuple[str, ...]:
    """Top-level keys of every config document the agent reported, sorted."""
    sections: Set[str] = set()
    for document in _config_documents(agent):
        sections.update(str(key) for key in document)
    return tuple(sorted(sections))


def sniff_from(spec: SniffSpec) -> Callable[[Any], bool]:
    """Compile an `identity.sniff` block into the predicate `AgentProfile` calls.

    Two signals, both already in the agent's report and both emptiness tests:
    the component sections it labelled (the names are the *agent's* per spec), and the
    top-level sections of the config it reported. An agent that reports nothing but a
    heartbeat matches neither and falls back to `generic`, which is the honest answer.
    """
    component_sections = frozenset(spec.component_sections_any)
    wanted_sections = frozenset(spec.config_sections_any)

    def sniff(agent: Any) -> bool:
        if component_sections:
            available = getattr(agent, "available_components", None) or {}
            sections = available.get("components")
            if isinstance(sections, Mapping) and component_sections.intersection(sections):
                return True
        if wanted_sections and wanted_sections.intersection(config_sections(agent)):
            return True
        return False

    sniff.__name__ = "sniff_from(dictionary)"
    return sniff


# ------------------------------------------------------------------ components ---


def _version_from(component: Any, spec: ComponentsSpec) -> str:
    """The version out of a component's metadata, or `""`. Never raises."""
    try:
        for entry in component.get("metadata", []) or []:
            if entry.get("key") != spec.version_metadata_key:
                continue
            value = entry.get("value", {}) or {}
            text = value.get("stringValue", "") or ""
            if not text:
                return ""
            if not spec.version_separator:
                return text
            if spec.version_separator in text:
                return text.rsplit(spec.version_separator, 1)[1]
            return ""
        return ""
    except Exception:  # pragma: no cover - a malformed component must not 500 a listing
        return ""


def _used_from_config(agent: Any) -> Dict[str, Set[str]]:
    """Components the reported config uses, cached against its exact text (#68).

    The cache slot (`AgentState._usage_cache`) is a memo, not product knowledge: a new
    report replaces the stored text, and the key is that text.
    """
    effective_config = getattr(agent, "effective_config", None)
    if not effective_config:
        return {}
    cached = getattr(agent, "_usage_cache", None)
    if cached is not None and cached[0] == effective_config:
        return cached[1]
    from server import effective_config as effective_config_module

    try:
        usage = effective_config_module.parse_effective_config(_report(agent))
    except Exception as exc:  # a malformed report must not 500 a listing
        logger.warning(f"Failed to parse effective_config: {exc}")
        usage = {}
    try:
        agent._usage_cache = (effective_config, usage)
    except AttributeError:  # pragma: no cover - duck-typed callers stay read-only
        pass
    return usage


def _used_from_health(agent: Any, spec: ComponentsSpec) -> Dict[str, Set[str]]:
    """Components named by `health.componentHealthMap`.

    The fallback "in use" signal when no config was reported (or it could not be
    parsed): a health report groups components under pipeline scopes and an
    extensions scope, with `type:id`-style children.
    """
    used: Dict[str, Set[str]] = defaultdict(set)
    health = getattr(agent, "health", None) or {}
    health_map = health.get("componentHealthMap", {}) or {}
    separator = spec.health_child_separator

    for key, value in health_map.items():
        if not isinstance(value, Mapping):
            continue
        if not key.startswith(spec.health_scope_prefix) and key != spec.health_extensions_scope:
            continue
        nested = value.get("componentHealthMap", {}) or {}
        for child in nested:
            if separator not in child:
                continue
            scope, component_id = child.split(separator, 1)
            if key == spec.health_extensions_scope:
                used["extension"].add(component_id)
            else:
                used[scope].add(component_id)

    return dict(used)


#: The engines a profile file may name in its `structure` block (#119). A closed set, by
#: name: `schema.py` rule 1 forbids importing code from data, so "which rules judge this
#: product" is a field the loader understands, not a module path a file can point at.
STRUCTURE_ENGINES: Dict[str, Callable[..., bool]] = {}


#: The engines a profile may name in `components_engine` (#123). Same closed-registry
#: rule as `structure`: a file may name one, never import one.
COMPONENTS_ENGINES: Dict[str, Callable[[Any], Dict[str, list]]] = {}


def register_components_engine(name: str, extract: Callable[[Any], Dict[str, list]]) -> None:
    """Add a component extractor to the registry (called by the engine modules)."""
    if name in COMPONENTS_ENGINES:
        raise RuntimeError(f"components engine {name!r} is registered twice")
    extract.ENGINE = name  # type: ignore[attr-defined]
    COMPONENTS_ENGINES[name] = extract


def components_engine_from(name: Optional[str]) -> Optional[Callable[[Any], Dict[str, list]]]:
    """The extractor called `name`, or `None`.

    Lazy import for the same reason as `structure_from`: engines import the validator.
    """
    if not name:
        return None
    from server.profiles import fluentbit  # noqa: F401  (registers itself)

    engine = COMPONENTS_ENGINES.get(name)
    if engine is None:
        raise KeyError(
            f"unknown components engine {name!r}; known engines: "
            f"{', '.join(sorted(COMPONENTS_ENGINES)) or 'none'}"
        )
    return engine


def register_structure_engine(name: str, validate: Callable[..., bool]) -> None:
    """Add one engine to the registry (called by the engine modules themselves)."""
    if name in STRUCTURE_ENGINES:
        raise RuntimeError(f"structure engine {name!r} is registered twice")
    validate.ENGINE = name  # type: ignore[attr-defined]
    STRUCTURE_ENGINES[name] = validate


def structure_from(name: Optional[str]) -> Optional[Callable[..., bool]]:
    """The engine called `name`, or `None` when the profile does not name one.

    Imports the engine modules lazily: they import `server.config_validation` for the
    `Verdict`, and `config_validation` resolves profiles at call time, so a module-level
    import here would close the cycle the other way.
    """
    if not name:
        return None
    from server.profiles import otelcol  # noqa: F401  (registers itself)

    engine = STRUCTURE_ENGINES.get(name)
    if engine is None:
        raise KeyError(
            f"unknown structure engine {name!r}; known engines: "
            f"{', '.join(sorted(STRUCTURE_ENGINES)) or 'none'}"
        )
    return engine


def components_from(spec: ComponentsSpec) -> Callable[[Any], Dict[str, list]]:
    """Compile a `components` block into `agent -> {type: [{id, version, used}]}`.

    The shape of the *report* (`components: {<section>: {subComponentMap: {...}}}`) is
    the protocol's, so it is not a knob; which sections exist and what they are called
    is the product's, so it is.
    """

    def extract(agent: Any) -> Dict[str, list]:
        groups: Dict[str, list] = defaultdict(list)
        used_by_type = _used_from_config(agent) or _used_from_health(agent, spec)

        available = getattr(agent, "available_components", None) or {}
        container = available.get("components", {}) or {}
        for section, component_type in spec.sections.items():
            if section not in container:
                continue
            entry = container[section] or {}
            components = entry.get("subComponentMap", {}) or {}
            used_in_pipeline = used_by_type.get(component_type, set())
            for component_id, component_data in components.items():
                groups[component_type].append(
                    {
                        "id": component_id,
                        "version": _version_from(component_data, spec),
                        "used": component_id in used_in_pipeline,
                    }
                )

        for component_type in groups:
            groups[component_type].sort(key=lambda c: (not c["used"], c["id"]))

        return dict(groups)

    extract.__name__ = "components_from(dictionary)"
    return extract
