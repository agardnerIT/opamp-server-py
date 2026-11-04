"""Read an agent's reported effective config (#68).

`EffectiveConfig.config_map` is a map of **configuration objects**, and the keys are
agent-implementation-defined: `""` is valid and has no privileged meaning
(opamp-spec v0.20.0 §Configuration Objects). `otelcol-contrib` with
`reports_raw_config: true` sends two objects — the expanded config under `""` and the
raw config under `"raw"` — and other agents use `"collector"`, `"config"`, …

Reading only `configMap[""]` therefore reported **zero components** for any agent
that does not happen to use the empty key, which silently disabled the OCB manifest
builder and the heavy/outdated reports. This module is the one place that reads the
map; `server/state.py` and the Streamlit UI both go through it, so they cannot drift
again (the UI used to reimplement the lookup inline, twice).

Rules:

* every object is considered, and the components found across all of them are
  **unioned** — objects may be partial or alternative representations of the same
  config, so picking one would under-report;
* an object that is not collector YAML is **skipped, never raised**: a content type
  that is set and is not YAML, a body that is not UTF-8, a body that does not parse,
  a body that parses to something that is not a mapping;
* the `""` object stays the **preferred** source for "show the config as YAML",
  falling back to the first parseable object (untagged objects first — a `role` marks
  an alternative representation, per spec);
* parsing uses the bounded loader from `config_validation` (alias/depth/expansion
  budgets, duplicate keys refused) because the document comes from an agent.

Display can also read **JSON** bodies (`accept_json=True`): osquery reports its
config as `application/json`, and it is not a collector config at all. The default
stays YAML-only because component extraction and agent-type inference must not
mistake an arbitrary JSON document for collector YAML.

Pure functions only: no FastAPI, no SQLite, no Streamlit.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set

import yaml
from loguru import logger

from server import config_validation
from server.profiles.base import YAML_CONTENT_TYPES as _YAML_CONTENT_TYPES
from server.redaction import decode_text_body

#: Content types that mean "this body is a collector config". An empty value is
#: treated as YAML: older agents do not set it, and the field is optional by spec.
#: Defined in `server.profiles.base` because profile identity inference (#118) needs
#: the same set without importing the collector-shaped module.
YAML_CONTENT_TYPES = _YAML_CONTENT_TYPES

#: Document types the *display* path accepts on request, never by default. A JSON
#: body is still a config an operator may need to read (osquery's schedule and its
#: `SELECT` statements), but `application/json` must not become "YAML" for the
#: component or identity paths — an osquery agent would then be classified as the
#: collector profile, which is the bug the content-type set exists to prevent.
JSON_CONTENT_TYPES = frozenset({"application/json", "text/json"})


@dataclass(frozen=True)
class ConfigObject:
    """One entry of `EffectiveConfig.config_map`, decoded and classified."""

    key: str
    role: str = ""
    content_type: str = ""
    body: Optional[str] = None
    document: Optional[Dict[str, Any]] = None
    skipped: Optional[str] = None  # why it was not used as a config, if it was not

    @property
    def parsed(self) -> bool:
        return self.document is not None


def is_yaml_content_type(content_type: Any) -> bool:
    """True for `text/yaml`, `application/yaml`, `""` and their `; charset=` forms."""
    if content_type is None:
        return True
    if not isinstance(content_type, str):
        return False
    return content_type.split(";")[0].strip().lower() in YAML_CONTENT_TYPES


def is_json_content_type(content_type: Any) -> bool:
    """True for `application/json`, `text/json` and their `; charset=` forms."""
    if content_type is None:
        return False
    if not isinstance(content_type, str):
        return False
    return content_type.split(";")[0].strip().lower() in JSON_CONTENT_TYPES


def _config_map(effective_config: Any) -> Mapping[str, Any]:
    """The inner `{key: {body, content_type, role}}` map, or `{}`.

    Accepts what `MessageToDict` produces:
    `{"configMap": {"configMap": {...}}}`. Anything else (missing, `None`, a list,
    a string) is an agent that has not reported a config: no error, no objects.
    """
    if not isinstance(effective_config, Mapping):
        return {}
    outer = effective_config.get("configMap")
    if not isinstance(outer, Mapping):
        return {}
    inner = outer.get("configMap")
    return inner if isinstance(inner, Mapping) else {}


def _ordered_keys(config_map: Mapping[str, Any]) -> List[str]:
    """`""` first (the spec's conventional "the" object), then the rest by name."""
    return sorted(config_map, key=lambda key: (key != "", str(key)))


def _decode_body(entry: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """`(text, problem)`. `MessageToDict` renders `bytes` as base64, hence both paths."""
    body = entry.get("body")
    if body is None:
        return None, "no body"
    if isinstance(body, (bytes, bytearray)):
        try:
            return bytes(body).decode("utf-8"), None
        except UnicodeDecodeError:
            return None, "body is not valid UTF-8"
    if not isinstance(body, str):
        return None, f"body is {type(body).__name__}, not text"
    if not body:
        return "", None
    # `decode_text_body` is deliberately conservative: a plain YAML string that merely
    # looks like base64 must not be rewritten. It returns None when it is not base64.
    return decode_text_body(body) or body, None


def _entry_field(entry: Mapping[str, Any], name: str) -> Any:
    """Read a config-object field in either spelling.

    Two callers, two conventions: the agent path is `MessageToDict` output
    (`contentType`, `role`) and the API/revision path is a hand-written document
    (`content_type`). Reading only one spelling is not a cosmetic bug — a missing
    `content_type` silently means "YAML", so a JSON body would be *parsed as YAML*
    and skipped with a misleading reason (#118 hit this while reading content types
    for identity inference).
    """
    camel = name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
    for key in (name, camel):
        if key in entry:
            return entry.get(key)
    return None


def load_object(key: str, entry: Any, *, accept_json: bool = False) -> ConfigObject:
    """Decode and parse one map entry. Never raises; says why in `skipped`.

    `accept_json` widens the gate to JSON bodies for the display path. It does not
    change the default: the component and identity readers must keep seeing only
    what the agent declared as collector-shaped YAML.
    """
    if not isinstance(entry, Mapping):
        return ConfigObject(key=key, skipped=f"entry is {type(entry).__name__}, not an object")

    content_type = _entry_field(entry, "content_type") or ""
    role = _entry_field(entry, "role") or ""
    readable = is_yaml_content_type(content_type) or (
        accept_json and is_json_content_type(content_type)
    )
    if not readable:
        return ConfigObject(
            key=key,
            role=str(role),
            content_type=str(content_type),
            skipped=f"content_type {content_type!r} is not YAML",
        )

    body, problem = _decode_body(entry)
    if problem is not None:
        return ConfigObject(key=key, role=str(role), content_type=str(content_type), skipped=problem)
    if not body or not body.strip():
        return ConfigObject(key=key, role=str(role), content_type=str(content_type), skipped="empty body")

    try:
        document = config_validation.parse_document(body)
    except (config_validation.ConfigParseError, yaml.YAMLError, RecursionError) as exc:
        return ConfigObject(
            key=key,
            role=str(role),
            content_type=str(content_type),
            body=body,
            skipped=f"not valid YAML ({type(exc).__name__})",
        )

    if not isinstance(document, dict):
        # A list or scalar is a valid YAML document but not a collector config (a PEM
        # blob, a routing table). Skipping keeps "not a config" from looking like
        # "config with no components".
        return ConfigObject(
            key=key,
            role=str(role),
            content_type=str(content_type),
            body=body,
            skipped="not a YAML mapping",
        )

    return ConfigObject(
        key=key,
        role=str(role),
        content_type=str(content_type),
        body=body,
        document=document,
    )


def objects(effective_config: Any, *, accept_json: bool = False) -> List[ConfigObject]:
    """Every object in the map, decoded, with `""` first. Never raises."""
    config_map = _config_map(effective_config)
    return [
        load_object(key, config_map[key], accept_json=accept_json)
        for key in _ordered_keys(config_map)
    ]


def content_types(effective_config: Any) -> tuple:
    """The normalised, de-duplicated `content_type`s the agent declared (#118).

    Deliberately **no parsing**: this answers "how did the agent label its bodies",
    which is what identity inference needs, and it runs on read paths. An undeclared
    (`""`) content type is omitted rather than reported as `""` — per spec the field
    is optional, so its absence is not a declaration of anything, and treating it as
    "YAML" would make every pre-v0.20.0 agent look like it had declared a format.
    """
    found: List[str] = []
    if isinstance(effective_config, (bytes, bytearray)):
        try:
            effective_config = bytes(effective_config).decode("utf-8")
        except UnicodeDecodeError:
            return ()
    if isinstance(effective_config, str):
        # The agent's report is stored as JSON text (that is what `AgentState` holds),
        # and this is called from a read path, so decode defensively and never raise.
        try:
            effective_config = json.loads(effective_config)
        except ValueError:
            return ()
    config_map = _config_map(effective_config)
    for key in _ordered_keys(config_map):
        entry = config_map[key]
        if not isinstance(entry, Mapping):
            continue
        normalised = (
            str(_entry_field(entry, "content_type") or "")
            .split(";")[0]
            .strip()
            .lower()
        )
        if normalised and normalised not in found:
            found.append(normalised)
    return tuple(found)


def _scalar_ids(value: Any) -> Set[Any]:
    """Component ids as-written, minus anything unhashable (`receivers: [{a: b}]`)."""
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, (str, int, float, bool))}


def component_usage(document: Mapping[str, Any]) -> Dict[str, Set[str]]:
    """Which components the document's `service` section actually uses."""
    service = document.get("service")
    if not isinstance(service, Mapping):
        return {}

    used: Dict[str, Set[Any]] = defaultdict(set)
    used["extension"].update(_scalar_ids(service.get("extensions")))

    pipelines = service.get("pipelines")
    if isinstance(pipelines, Mapping):
        for pipeline in pipelines.values():
            if not isinstance(pipeline, Mapping):
                continue
            for section, component_type in (
                ("receivers", "receiver"),
                ("processors", "processor"),
                ("exporters", "exporter"),
            ):
                used[component_type].update(_scalar_ids(pipeline.get(section)))

    return {component_type: ids for component_type, ids in used.items() if ids}


def parse_effective_config(effective_config: Any) -> Dict[str, Set[str]]:
    """Union of the components used by **every** config object in the map.

    Unions rather than "the first object wins": an agent may expose the same
    configuration under several keys, and a partial object (a fragment, a raw
    representation) may still name components the others do not.
    """
    found: Dict[str, Set[Any]] = defaultdict(set)
    parsed: List[ConfigObject] = []
    for obj in objects(effective_config):
        if not obj.parsed:
            if obj.skipped != "empty body":  # an empty map entry is not a problem
                logger.debug(f"effective config object {obj.key!r} skipped: {obj.skipped}")
            continue
        parsed.append(obj)
        for component_type, ids in component_usage(obj.document or {}).items():
            found[component_type].update(ids)

    if parsed:
        # The next "my collector shows no components" report is answerable from this.
        logger.debug(
            "effective config objects parsed: "
            + ", ".join(f"{obj.key!r}(role={obj.role or '-'})" for obj in parsed)
        )
    return {component_type: ids for component_type, ids in found.items() if ids}


def preferred_object(
    effective_config: Any, *, accept_json: bool = False
) -> Optional[ConfigObject]:
    """The object to show as "the configuration".

    `""` when it parses — that key is what agents conventionally use for the full
    config, and it is what this server always showed before #68. Otherwise the first
    parseable object, untagged before role-tagged: a `role` marks an alternative
    representation (the raw config, a fragment), not the primary one.
    """
    parsed = [
        obj for obj in objects(effective_config, accept_json=accept_json) if obj.parsed
    ]
    if not parsed:
        return None
    return min(parsed, key=lambda obj: (obj.key != "", bool(obj.role), obj.key))


def preferred_document(
    effective_config: Any, *, accept_json: bool = False
) -> Optional[Dict[str, Any]]:
    """The parsed document of :func:`preferred_object`, or `None`."""
    chosen = preferred_object(effective_config, accept_json=accept_json)
    return chosen.document if chosen else None


def preferred_body(effective_config: Any, *, accept_json: bool = False) -> Optional[str]:
    """The text body of :func:`preferred_object` (the un-augmented config)."""
    chosen = preferred_object(effective_config, accept_json=accept_json)
    return chosen.body if chosen else None
