"""OpenTelemetry Collector: the profile this server was born as (#118).

Everything here was moved out of `server/state.py` and `server/agent_auth.py`
**verbatim** — not rewritten — so the golden no-change test can byte-compare
components, reports and validation output against the pre-#118 behaviour. The
move is the point: this is the only module allowed to assume that a payload is
collector-shaped.

Lazy imports are deliberate. `config_validation` and `effective_config` may both
import `server.profiles`, and a module-level import of either here would close the
cycle (`profiles -> otelcol -> effective_config -> config_validation -> profiles`).
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, Mapping, Set

from loguru import logger

from server.profiles.base import AgentProfile, ConfigFormat, YAML_CONTENT_TYPES, attr_prefix

#: Top-level sections of a collector config. `service` is what makes it runnable.
COLLECTOR_SECTIONS = ("receivers", "processors", "exporters", "extensions", "connectors")

COMPONENT_TYPES = {
    "receivers/": "receiver",
    "processors/": "processor",
    "exporters/": "exporter",
    "extensions/": "extension",
    "connectors/": "connector",
}

COMPONENT_SHORT_NAMES = {
    "receiver": "receiver",
    "receivers": "receiver",
    "processor": "processor",
    "processors": "processor",
    "exporter": "exporter",
    "exporters": "exporter",
    "extension": "extension",
    "extensions": "extension",
    "connector": "connector",
    "connectors": "connector",
}

KNOWN_COMPONENTS = {
    "otlp": "receiver",
    "jaeger": "receiver",
    "prometheus": "receiver",
    "zipkin": "receiver",
    "statsd": "receiver",
    "host_metrics": "receiver",
    "batch": "processor",
    "memory_limiter": "processor",
    "filter": "processor",
    "transform": "processor",
    "resourcedetection": "processor",
    "debug": "exporter",
    "logging": "exporter",
    "prometheusremotewrite": "exporter",
    "otlphttp": "exporter",
    "opamp": "extension",
    "zpages": "extension",
    "health_check": "extension",
    "pprof": "extension",
}

#: `AvailableComponents.components` keys that are collector section names (plural —
#: the *keys* are `receivers`/`exporters`/..., the component *types* are singular).
_SECTION_KEYS = frozenset(COLLECTOR_SECTIONS)


def detect_type(comp_id: str) -> str:
    """Component type from its id (`receivers/otlp` -> `receiver`)."""
    for prefix, type_name in COMPONENT_TYPES.items():
        if comp_id.startswith(prefix):
            return type_name

    if comp_id in KNOWN_COMPONENTS:
        return KNOWN_COMPONENTS[comp_id]

    if "/" in comp_id:
        first_part = comp_id.split("/")[0]
        if first_part in COMPONENT_SHORT_NAMES:
            return COMPONENT_SHORT_NAMES[first_part]
        if first_part in KNOWN_COMPONENTS:
            return KNOWN_COMPONENTS[first_part]

    return "unknown"


def extract_version(comp_data: Any) -> str:
    """The version out of a component's `code.namespace` metadata, or `""`."""
    try:
        metadata = comp_data.get("metadata", [])
        for meta in metadata:
            if meta.get("key") == "code.namespace":
                value = meta.get("value", {})
                string_value = value.get("stringValue", "")
                if string_value:
                    parts = string_value.rsplit(" v", 1)
                    if len(parts) == 2:
                        return parts[1]
                return string_value.split(" v")[-1] if " v" in string_value else ""
        return ""
    except Exception:
        return ""


def config_usage(agent: Any) -> Dict[str, Set[str]]:
    """Components used by the reported config, cached against its exact text (#68).

    Reading every config object is more work than reading `configMap[""]`, and
    `components` is recomputed for every agent on every `GET /agents`. The cache is
    keyed on the stored JSON itself, so a new report invalidates it and nothing
    else has to remember to.

    The cache slot (`AgentState._usage_cache`) is the only thing left of this in
    `state.py`: it is a memo, not collector knowledge.
    """
    effective_config = getattr(agent, "effective_config", None)
    if not effective_config:
        return {}
    cached = getattr(agent, "_usage_cache", None)
    if cached is not None and cached[0] == effective_config:
        return cached[1]
    from server import effective_config as effective_config_module

    try:
        report = (
            json.loads(effective_config)
            if isinstance(effective_config, (str, bytes, bytearray))
            else effective_config
        )
        usage = effective_config_module.parse_effective_config(report)
    except Exception as exc:  # a malformed report must not 500 a listing
        logger.warning(f"Failed to parse effective_config: {exc}")
        usage = {}
    agent._usage_cache = (effective_config, usage)
    return usage


def parse_health_components(agent: Any) -> Dict[str, Set[str]]:
    """Components named by the agent's `health.componentHealthMap`.

    The collector reports `pipeline:<name>` and `extensions` scopes with
    `receiver:otlp`-style children, which is the fallback "in use" signal when no
    config was reported (or it could not be parsed).
    """
    used_by_type: Dict[str, Set[str]] = defaultdict(set)
    health = getattr(agent, "health", None) or {}
    health_map = health.get("componentHealthMap", {})

    for key, value in health_map.items():
        if key.startswith("pipeline:"):
            nested_map = value.get("componentHealthMap", {})
            for comp_key in nested_map:
                if ":" in comp_key:
                    comp_type, comp_id = comp_key.split(":", 1)
                    used_by_type[comp_type].add(comp_id)
        elif key == "extensions":
            nested_map = value.get("componentHealthMap", {})
            for comp_key in nested_map:
                if ":" in comp_key:
                    _, comp_id = comp_key.split(":", 1)
                    used_by_type["extension"].add(comp_id)

    return dict(used_by_type)


def extract_components(agent: Any) -> Dict[str, list]:
    """The collector's component inventory, grouped by type, with "in use" flags."""
    groups: Dict[str, list] = defaultdict(list)

    type_mapping = {
        "receivers": "receiver",
        "processors": "processor",
        "exporters": "exporter",
        "extensions": "extension",
        "connectors": "connector",
    }

    used_by_type = config_usage(agent)

    if not used_by_type:
        used_by_type = parse_health_components(agent)

    available = getattr(agent, "available_components", None) or {}
    components_container = available.get("components", {})
    for comp_type_key, comp_type in type_mapping.items():
        if comp_type_key in components_container:
            comps_dict = components_container[comp_type_key].get("subComponentMap", {})
            used_in_pipeline = used_by_type.get(comp_type, set())
            for comp_id, comp_data in comps_dict.items():
                is_used = comp_id in used_in_pipeline
                version = extract_version(comp_data)
                groups[comp_type].append(
                    {"id": comp_id, "version": version, "used": is_used}
                )

    for comp_type in groups:
        groups[comp_type].sort(key=lambda c: (not c["used"], c["id"]))

    return dict(groups)


def looks_like_collector_config(document: Any) -> bool:
    """True when a parsed document has at least one collector section."""
    return isinstance(document, Mapping) and any(
        section in document for section in COLLECTOR_SECTIONS
    )


def sniff(agent: Any) -> bool:
    """Does this agent's payload look like a collector, without asking it to say so?

    Two signals, both already available and both cheap:

    * `AvailableComponents.components` carries one of the collector section names
      (the names are the *agent's* labels per spec, but a collector is the only thing
      that emits exactly these five);
    * the reported config uses collector components (`service.pipelines`), which
      reuses the `config_usage` memo rather than parsing a second time.

    An agent that reports nothing but a heartbeat is *not* sniffed — there is no
    evidence either way, so it falls back to `generic` and says so.
    """
    available = getattr(agent, "available_components", None) or {}
    sections = available.get("components")
    if isinstance(sections, Mapping) and _SECTION_KEYS.intersection(sections):
        return True
    return bool(config_usage(agent))


def bootstrap_snippet(endpoint: str, token: str) -> str:
    """A paste-ready `extensions.opamp` block carrying the token.

    This is the v1 bootstrap: the token goes into the agent's config, because the
    collector extension cannot receive one in-band (see `server/agent_auth.py`).
    """
    block = "ws" if endpoint.startswith("ws") else "http"
    return (
        "extensions:\n"
        "  opamp:\n"
        "    server:\n"
        f"      {block}:\n"
        f"        endpoint: {endpoint}\n"
        "        headers:\n"
        f'          Authorization: "Bearer {token}"\n'
    )


PROFILE = AgentProfile(
    name="otelcol",
    display_name="OpenTelemetry Collector",
    summary=(
        "Collector YAML: components are read from AvailableComponents, structural "
        "validation applies, and the heavy/outdated reports and otelcol_* telemetry "
        "views are meaningful."
    ),
    config_formats=(
        ConfigFormat(
            content_types=tuple(sorted(YAML_CONTENT_TYPES | {"text/plain"})),
            default_filename="otelcol-config.yaml",
        ),
    ),
    validates_structure=True,
    # The spec's own convention: service.name is "a reverse FQDN that uniquely
    # identifies the Agent type, e.g. io.opentelemetry.collector". Prefix matching
    # covers distros (otelcol-contrib) and the `unknown_service:` fallback.
    description_matchers=(
        attr_prefix(
            "service.name",
            ("otelcol", "io.opentelemetry.collector", "opentelemetry-collector"),
        ),
    ),
    sniff=sniff,
    components=extract_components,
    reports=("fleet", "agent", "heavy", "outdated"),
    telemetry="otelcol_*",
    snippet=bootstrap_snippet,
)
