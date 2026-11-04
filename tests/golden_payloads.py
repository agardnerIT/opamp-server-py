"""Deterministic payloads used to prove the #118 refactor changed nothing.

`tests/test_profiles.py` compares this module's output against
`tests/fixtures/agent_type_golden.json`, which was captured **before** the move (from
the pre-#118 working tree). The point of a verbatim move is that the bytes do not
change, and "the other 1300 tests still pass" is not the same claim as "this exact
output is identical".

So this module deliberately imports only modules that exist on both sides of the
refactor (`server.state`, `server.reports`, `server.config_validation`), never
`server.profiles`, and strips the `agent_type*` / `profile*` keys the refactor added
before returning. Regenerate with::

    python -m tests.golden_payloads > tests/fixtures/agent_type_golden.json

Three agents, because one would let a mistake hide: a full collector (components +
effective config + health), a collector that only reports an effective config (no
component list), and an agent that looks like nothing at all.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict

from server import config_validation, reports
from server.state import AgentState

FIXED_NOW = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

COLLECTOR_CONFIG = """\
receivers:
  otlp:
    protocols:
      grpc: {}
  prometheus:
    config:
      scrape_configs: []
processors:
  batch: {}
exporters:
  debug: {}
  otlphttp:
    endpoint: http://example.invalid:4318
extensions:
  opamp: {}
  health_check: {}
service:
  extensions: [opamp, health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
"""


def _description(service_name: str) -> Dict[str, Any]:
    return {
        "identifyingAttributes": [
            {"key": "service.name", "value": {"stringValue": service_name}},
            {"key": "service.version", "value": {"stringValue": "0.149.0"}},
        ],
        "nonIdentifyingAttributes": [
            {"key": "os.type", "value": {"stringValue": "linux"}},
            {"key": "environment", "value": {"stringValue": "prod"}},
        ],
    }


def _effective_config(body: str, content_type: str = "text/yaml") -> str:
    import base64

    return json.dumps(
        {
            "configMap": {
                "configMap": {
                    "": {
                        "body": base64.b64encode(body.encode()).decode(),
                        "contentType": content_type,
                    }
                }
            }
        }
    )


def _component(version: str) -> Dict[str, Any]:
    return {
        "metadata": [
            {
                "key": "code.namespace",
                "value": {"stringValue": f"go.opentelemetry.io/collector/receiver/otlpreceiver v{version}"},
            }
        ]
    }


def agents() -> list:
    """The three fixtures, built the way the server builds them."""
    full = AgentState(
        instance_uid=b"\x01" * 16,
        agent_id="1" * 32,
        connected_at=FIXED_NOW,
        last_heartbeat=FIXED_NOW,
        healthy=True,
        capabilities=0x0A0B,
        description=_description("otelcol-contrib"),
        effective_config=_effective_config(COLLECTOR_CONFIG),
        remote_config_status="APPLIED",
        package_statuses={"packages": {"collector": {"name": "collector", "agentHasVersion": "0.149.0"}}},
        available_components={
            "hash": "aGFzaA==",
            "components": {
                "receivers": {"subComponentMap": {"otlp": _component("0.149.0"), "prometheus": _component("0.140.0")}},
                "processors": {"subComponentMap": {"batch": _component("0.149.0")}},
                "exporters": {"subComponentMap": {"debug": _component("0.149.0")}},
                "extensions": {"subComponentMap": {"opamp": _component("0.149.0")}},
            },
        },
        health={
            "healthy": True,
            "componentHealthMap": {
                "pipeline:traces": {"componentHealthMap": {"receiver:otlp": {"healthy": True}}},
                "extensions": {"componentHealthMap": {"extension:opamp": {"healthy": True}}},
            },
        },
    )

    config_only = AgentState(
        instance_uid=b"\x02" * 16,
        agent_id="2" * 32,
        connected_at=FIXED_NOW,
        last_heartbeat=FIXED_NOW,
        healthy=False,
        capabilities=0x0004,
        description=_description("my-collector"),
        effective_config=_effective_config(COLLECTOR_CONFIG),
        health={"healthy": False, "lastError": "boom"},
    )

    nothing = AgentState(
        instance_uid=b"\x03" * 16,
        agent_id="3" * 32,
        connected_at=FIXED_NOW,
        last_heartbeat=FIXED_NOW,
        description={"identifyingAttributes": [], "nonIdentifyingAttributes": []},
    )
    return [full, config_only, nothing]


def _stable(text: str) -> str:
    """Drop the wall-clock line every report starts with."""
    return re.sub(r"^Generated: .*$", "Generated: <captured>", text, flags=re.MULTILINE)


def capture() -> Dict[str, Any]:
    """Every output the refactor was not allowed to change."""
    built = agents()
    payloads = []
    for agent in built:
        payload = agent.to_dict()
        # Added by #118; absent on the pre-refactor side of the comparison.
        for key in (
            "agent_type",
            "agent_type_source",
            "agent_type_pinned",
            "agent_type_warning",
        ):
            payload.pop(key, None)
        payloads.append(payload)

    verdict = config_validation.check_config_map(
        {
            "": {
                "body": COLLECTOR_CONFIG,
                "content_type": "text/yaml",
            }
        },
        agents=built[:1],
        run_policy=False,
        # The fixture agent reports the very config this revision contains, because the
        # server is the source that pushed it. Without this the merge-collision warning
        # would fire by construction (every address would look like "another source's")
        # and the move snapshot would stop being a byte comparison of unrelated output;
        # the stage itself is covered in `test_config_validation` and `test_rollout`.
        previous_bodies={"1" * 32: [COLLECTOR_CONFIG]},
    )
    verdict_dict = verdict.to_dict()
    verdict_dict["checked_at"] = "<captured>"
    verdict_dict.pop("profile", None)
    verdict_dict.pop("profile_source", None)

    fleet = {"agents": payloads, "count": len(payloads)}
    return {
        "agents": payloads,
        "components": [agent.components for agent in built],
        "fleet_report": _stable(reports.generate_agent_report(fleet)),
        "heavy_report": _stable(reports.generate_heavy_collectors_report(fleet)),
        "outdated_report": _stable(reports.generate_outdated_collectors_report(fleet)),
        "verdict": verdict_dict,
    }


if __name__ == "__main__":  # pragma: no cover - regeneration helper
    print(json.dumps(capture(), indent=2, sort_keys=True))
