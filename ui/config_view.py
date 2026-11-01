"""Presentation of an agent's effective config (#68).

Deliberately **streamlit-free** so it can be tested without the UI dependency stack:
`ui/shared.py` imports this and the Agents page calls it. The parsing itself lives
in `server.effective_config` — this module only decides what the "Collector
Configuration" panel shows and adds the telemetry pipeline the dashboard would
configure for a downloadable config.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import yaml

from server.effective_config import preferred_document

DEFAULT_SERVER_URL = "{scheme}://{address}:{port}".format(
    scheme=os.environ.get("SERVER_HTTP_SCHEME", "http"),
    address=os.environ.get("SERVER_ADDRESS", "localhost"),
    port=os.environ.get("SERVER_PORT", "4320"),
)


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


def display_config_yaml(
    effective_config: Optional[str],
    server_url: Optional[str] = None,
) -> Optional[str]:
    """The "Collector Configuration" YAML for one agent, or `None` (#68).

    Both panels that render an agent's config used to decode `configMap[""]` inline —
    the same 90 lines, twice — so an agent whose objects use any other key (or that
    also reports a `raw` object) showed nothing. The object displayed now comes from
    `server.effective_config.preferred_document`: `""` when it parses, otherwise the
    first parseable object.
    """
    if not effective_config:
        return None
    try:
        document = json.loads(effective_config)
    except (TypeError, ValueError):
        return None

    config = preferred_document(document)
    if not config:
        return None

    config = _clean_config(config)

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
