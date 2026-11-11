"""Presentation of an agent's effective config (#68).

Deliberately **streamlit-free** so it can be tested without the UI dependency stack:
`ui/shared.py` imports this and the Agents page calls it. The parsing itself lives
in `server.effective_config` — this module only decides what the "Configuration"
panel shows and adds the telemetry pipeline the dashboard would configure for a
downloadable collector config.

Two kinds of agent report through this server: an agent whose config this server can
interpret (a collector's YAML, say) and everything else — osquery reports
`application/json` with a `schedule` and its `SELECT` statements. Both are displayed,
but only a body the agent's *profile* recognises gets the augmentation below.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import yaml

from server.effective_config import (
    is_json_content_type,
    objects,
    preferred_document,
    preferred_object,
)
from server.redaction import contains_redaction

DEFAULT_SERVER_URL = "{scheme}://{address}:{port}".format(
    scheme=os.environ.get("SERVER_HTTP_SCHEME", "http"),
    address=os.environ.get("SERVER_ADDRESS", "localhost"),
    port=os.environ.get("SERVER_PORT", "4320"),
)

#: Which top-level sections make a config *this* profile's was hardcoded here as a
#: collector's five (#121). It is now the profile's own `components.sections`, so a new
#: agent type gets the same treatment with no change to this module.


def _profile_sections(profile) -> Tuple[str, ...]:
    spec = getattr(profile, "components_spec", None)
    return tuple(spec.sections) if spec is not None else ()


def _is_profile_document(config: dict, profile) -> bool:
    """True when the body uses sections this profile names — so the panel is showing
    the config the agent is actually running, and augmenting it is not a fabrication."""
    sections = _profile_sections(profile)
    return bool(sections) and any(section in config for section in sections)


def _clean_config(value: Any) -> Any:
    """Drop empty/null leaves so the displayed YAML matches what the agent runs."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if item is None:
                continue
            if isinstance(item, str) and item == "":
                continue
            if isinstance(item, dict) and item == {}:
                continue
            result[key] = _clean_config(item)
        return result
    if isinstance(value, list):
        return [_clean_config(item) for item in value if item is not None and item != {}]
    return value


#: What the push editor can declare. The protocol allows any content-type string;
#: these are the two the form offers, so a prefill has to land inside them.
#:
#: They are the editor's **storage labels**, not a claim about a profile: `text/yaml`
#: and `application/json` are legal for any profile, and the profile's own declaration
#: (`config_formats`) is shown beside the box by `editor_note()` below. Turning the
#: selectbox into the profile's six YAML aliases would be noise, not fidelity (#121).
EDITOR_CONTENT_TYPES = ("text/yaml", "application/json")

#: `Capabilities.AcceptsRemoteConfig`: the agent takes a config *from the server*.
#: An agent without this bit can report what it runs but never accept a replacement,
#: so offering it an editor would be offering it a proposal it must refuse (#131).
ACCEPTS_REMOTE_CONFIG = 0x2


def accepts_remote_config(capabilities: Any) -> bool:
    """Whether an agent's capability bitmask includes `AcceptsRemoteConfig`.

    Tolerates a missing or non-numeric value: an older payload, or a hand-built dict in
    a test, must read as "cannot accept" rather than raising on the read path.
    """
    try:
        return bool(int(capabilities) & ACCEPTS_REMOTE_CONFIG)
    except (TypeError, ValueError):
        return False


def is_redacted(body: Any) -> bool:
    """True when a body carries a redaction artefact, so it must never be pushed (#98).

    Delegates to `server.redaction.contains_redaction` — the same check the server's
    validation pipeline refuses on. A local `"<redacted>" in body` test sees the
    sentinel and misses the #98 document marker and a base64 body that decodes to
    either, which is exactly the body an operator copies out of a raw report.
    """
    return contains_redaction(body)


@dataclass(frozen=True)
class EditableConfig:
    """One agent's reported config, **decoded**, ready for an editor (#131).

    The report arrives as protobuf `MessageToDict`, whose `bytes` bodies are base64.
    Prefilling that verbatim put base64 in the Remote Config box, so an operator
    edited something that looked like a config, proposed it, and the server delivered
    the base64 *string* as the agent's config body. `body` here is always text, decoded
    by `server.effective_config` — the same reader the "Configuration" panel uses, so
    what you read is what you edit.

    Editing is text in, text out: the API stores bodies as text and base64 appears only
    at the protobuf boundary, so nothing anywhere has to re-encode anything.
    """

    key: str
    body: str
    content_type: str
    role: str = ""
    #: `(key, reason)` for objects the agent also reported that the editor does not
    #: hold — a revision stores one object per key, and the rest may be alternative
    #: representations (`raw`) rather than something to push.
    others: Tuple[Tuple[str, str], ...] = ()


def _editor_content_type(declared: str) -> str:
    """The editor's spelling of a declared content type (JSON only when declared)."""
    return "application/json" if is_json_content_type(declared) else "text/yaml"


def _parsed_report(effective_config: Any) -> Optional[dict]:
    """`effective_config` as the `MessageToDict` document, whether str or already dict."""
    if isinstance(effective_config, (bytes, bytearray)):
        effective_config = bytes(effective_config).decode("utf-8", "replace")
    if isinstance(effective_config, str):
        try:
            effective_config = json.loads(effective_config)
        except (TypeError, ValueError):
            return None
    return effective_config if isinstance(effective_config, dict) else None


def declared_content_types(profile) -> Tuple[str, ...]:
    """The content types a profile's own `config_formats` names (#121).

    `()` when the profile declares none, which is the honest answer for a profile that
    accepts anything as opaque bytes (`generic`) — not an empty declaration, and not a
    YAML default invented on its behalf.
    """
    formats = getattr(profile, "config_formats", ()) or ()
    found: list = []
    for fmt in formats:
        for content_type in getattr(fmt, "content_types", ()) or ():
            # `""` means "undeclared", which is legal in a report and useless as a label.
            if content_type and content_type not in found:
                found.append(content_type)
    return tuple(found)


def target_profiles(rows: Any) -> list:
    """The distinct profiles a selection of agent rows resolves to (#121).

    `rows` are `GET /agents` entries, each carrying `agent_type` — the *server's*
    resolution, which this module reads rather than repeats. A selection the server could
    not attribute (or one with no agents in it) answers `()`: "the page does not know" is
    a fact to render, never a guess to make.
    """
    from server import profiles as registry

    found: dict = {}
    for row in rows or []:
        name = (row or {}).get("agent_type")
        profile = registry.get(name) if isinstance(name, str) else None
        if profile is not None:
            found[profile.name] = profile
    return [found[name] for name in sorted(found)]


def editor_note(profiles: Any) -> Optional[str]:
    """What the push editor says about the format it is editing (#121, deliverable 4).

    `None` when every selected profile is one the server fully interprets — an `otelcol`
    fleet, where the editor's collector-shaped wording is already the truth and adding a
    paragraph would be noise (and would move the golden capture for no gain).

    For anything else the note is the difference between "valid" and "valid for *this*
    agent": the server stores, relays and displays a body it does not interpret, so a
    validation pass means the text is well-formed, not that the agent can use it. That is
    the UI half of #117d; the server-side half is `config_validation._annotate_profile`,
    which puts the same fact on the verdict as a warning.
    """
    profiles = [p for p in (profiles or ()) if p is not None]
    opaque = [p for p in profiles if not getattr(p, "validates_structure", False)]
    if not opaque:
        return None

    names = ", ".join(f"`{profile.name}`" for profile in opaque)
    if len(opaque) == len(profiles):
        sentence = (
            f"The server does **not** interpret the configuration of {names}: it stores, "
            "relays and shows this body as **opaque text**. Validation still runs the "
            "format pipeline, so a pass means the body is well-formed — not that it is "
            "valid for this agent."
        )
    else:
        interpreted = ", ".join(
            f"`{profile.name}`"
            for profile in profiles
            if getattr(profile, "validates_structure", False)
        )
        sentence = (
            f"This selection mixes profiles: {interpreted} is judged as its own product, "
            f"but the configuration of {names} is not interpreted at all — the body is "
            "stored and relayed as **opaque text**, so a finding below may be noise for it."
        )

    declared = sorted({ct for profile in opaque for ct in declared_content_types(profile)})
    declaration = (
        "Its profile declares " + ", ".join(f"`{ct}`" for ct in declared) + "."
        if declared
        else "Its profile declares no content type, so the body is opaque whatever it is."
    )
    return f"{sentence} {declaration}"


def config_map(body: str, content_type: str = "text/yaml", role: str = "", name: str = "") -> dict:
    """The one-object `config_map` a revision stores.

    `name` is the map key — the agent-side **config object name**, not decoration. The
    spec types it `map<string, AgentConfigFile>` and leaves the meaning to the agent: a
    collector is happy with `""`, osquery's OpAMP extension refuses anything but
    `osquery.json`. One implementation, because a second spelling of this shape is how
    a proposal gets mis-addressed and the rollout pauses at batch 0 (#131).
    """
    entry = {"body": body, "content_type": content_type or "text/yaml"}
    if role:
        entry["role"] = role
    return {(name or "").strip(): entry}


def edit_blocker(push: Optional[dict], agent: Optional[dict]) -> Optional[str]:
    """Why this agent's config cannot be **edited** here, or `None` when it can (#131).

    Two facts decide it, and both have to be true:

    * `push` — the server's `config_push` block from `GET /health`. With push off the
      server refuses every proposal (404 on the whole surface), so an editor here would
      be a form whose submit button is a lie.
    * the agent's own `AcceptsRemoteConfig` capability. Remote config is something an
      agent opts into: a bare agent reports what it runs and would refuse the push.

    `agent` is the `GET /agent/{id}` payload, so a **redacted** report is refused too —
    it is a display artefact and pushing it back wipes the fleet's credentials (#98).
    The reason is returned as markdown for the panel to render; the security boundary
    stays on the server (`config_validation` refuses the same bodies).
    """
    push = push or {}
    agent = agent or {}
    if not push.get("enabled"):
        return (
            "Config push is **off** on this server (`OPAMP_CONFIG_PUSH_ENABLED=false`), "
            "so this config is read-only. With it on, this box is where a change starts."
        )
    if agent.get("effective_config_redacted"):
        return (
            "This agent's reported config is **redacted** (`<redacted>` markers), so it is "
            "not loaded into an editor: proposing a redacted body back would wipe the "
            "fleet's credentials (#98). Read the real document on the **Remote Config** "
            "page, or with `opampctl agents config get <id> --reveal` as an approver."
        )
    if not accepts_remote_config(agent.get("capabilities")):
        return (
            "This agent does not advertise `AcceptsRemoteConfig`: it reports what it runs "
            "but cannot accept a replacement, so there is nothing to propose to it."
        )
    return None


def editable_config(effective_config: Any) -> Optional[EditableConfig]:
    """The object an operator should edit, decoded, plus what was left out (#131).

    The object chosen is `preferred_object`'s — the same one `display_config_yaml`
    shows — so the panel an operator reads to decide on a change and the box they make
    it in cannot disagree. `None` when nothing in the report parses, which is the one
    case where there is nothing to prefill.
    """
    document = _parsed_report(effective_config)
    if not document:
        return None
    chosen = preferred_object(document, accept_json=True)
    if chosen is None or not (chosen.body or "").strip():
        return None

    others = tuple(
        (obj.key, obj.skipped or "one object per revision")
        for obj in objects(document, accept_json=True)
        if obj.key != chosen.key
    )
    return EditableConfig(
        key=chosen.key,
        body=chosen.body,
        content_type=_editor_content_type(chosen.content_type),
        role=chosen.role,
        others=others,
    )


def display_config_yaml(
    effective_config: Optional[str],
    server_url: Optional[str] = None,
    *,
    profile=None,
) -> Optional[str]:
    """The config YAML for one agent, or `None` (#68).

    Both panels that render an agent's config used to decode `configMap[""]` inline —
    the same 90 lines, twice — so an agent whose objects use any other key (or that
    also reports a `raw` object) showed nothing. The object displayed now comes from
    `server.effective_config.preferred_document`: `""` when it parses, otherwise the
    first parseable object.

    JSON is accepted (`accept_json=True`) because an osquery agent reports its config
    as `application/json`, and refusing it made the panel claim there was no config at
    all. A document that is not shaped like one of this profile's configs is returned
    as-is: the telemetry pipeline below is something this dashboard would add *to a
    profile that has those sections*, not a rewrite every agent's config should survive.

    `profile` is the agent's own (`GET /agent/{id}.agent_type`, #121): the page passes
    it, so a FluentBit agent's body is never given a collector's pipeline. With no
    profile the bootstrap default is used, which is what the tests and any standalone
    caller get.
    """
    from server import profiles

    if profile is None:
        profile = profiles.bootstrap_default()
    document = _parsed_report(effective_config)
    if not document:
        return None

    config = preferred_document(document, accept_json=True)
    if not config:
        return None

    config = _clean_config(config)

    if not _is_profile_document(config, profile):
        return yaml.dump(config, default_flow_style=False, sort_keys=False)

    if "extensions" in config and "opamp" in config["extensions"]:
        opamp = config["extensions"]["opamp"]
        if not isinstance(opamp, dict):
            opamp = config["extensions"]["opamp"] = {}
        server = opamp.setdefault("server", {})
        http = server.setdefault("http", {})
        http.setdefault("endpoint", "http://127.0.0.1:4320/v1/opamp")

    if "exporters" in config and "debug" in config["exporters"]:
        debug = config["exporters"]["debug"]
        if isinstance(debug, dict) and debug.get("use_internal_logger"):
            debug.pop("output_paths", None)

    if "receivers" not in config:
        config["receivers"] = {"otlp": {"protocols": {"grpc": {}, "http": {}}}}
    if "processors" not in config:
        config["processors"] = {"batch": {}}
    if "exporters" not in config:
        config["exporters"] = {"debug": {}}

    existing_pipelines = config.get("service", {}).get("pipelines", {})
    existing_pipelines.setdefault("traces", {
        "receivers": ["otlp"],
        "processors": ["batch"],
        "exporters": ["debug"],
    })

    base_url = server_url or DEFAULT_SERVER_URL
    host = base_url.split("://")[1] if "://" in base_url else base_url
    config["service"] = {
        "extensions": ["opamp"],
        "pipelines": existing_pipelines,
        "telemetry": {"metrics": {
            "level": "basic",
            "readers": [{
                "periodic": {
                    "exporter": {
                        "otlp": {
                            "protocol": "http/protobuf",
                            "endpoint": f"http://{host}/v1/metrics",
                        }
                    }
                }
            }],
        }},
    }

    return yaml.dump(config, default_flow_style=False, sort_keys=False)
