"""Assert that a *real* collector is visible in the server (#113).

Every other test in this repository asserts that the server is self-consistent. This
script asserts the thing that cannot be proven that way: that a collector running the
upstream `opamp` extension registers itself, reports health, and — after the #77
handshake — hands over a full component list that is good enough to build an OCB
manifest from.

It is deliberately dumb HTTP: no server imports, no test fixtures, so CI can run it
against a server in another container and a developer can run it against a server on
their laptop. The pure helpers (`find_agent`, `check_agent`) are unit tested in
`tests/test_collector_e2e_script.py`.

Usage:

    python scripts/collector_e2e.py --server http://localhost:4320 \
        --agent-name e2e-collector --timeout 60
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_SERVER = "http://localhost:4320"
DEFAULT_AGENT_NAME = "e2e-collector"
DEFAULT_TIMEOUT = 90.0
POLL_SECONDS = 2.0

#: What `collector/e2e.yaml` declares. Asserted as a subset of the components the
#: server considers *used*, because that is the chain the feature depends on: the
#: agent's effective config is parsed, not just its component list stored. Extras are
#: allowed — a collector version may report more than the pipeline names.
EXPECTED_USED = (
    "receiver:otlp",
    "processor:batch",
    "exporter:debug",
    "extension:opamp",
)

#: Substrings the generated manifest must contain: the used components as gomod
#: lines, with the versions the agent reported. A 200 with an empty manifest would
#: otherwise pass.
EXPECTED_MANIFEST = ("otlpreceiver", "batchprocessor", "debugexporter", "opampextension")


def attribute(description: Dict[str, Any], key: str) -> Optional[str]:
    """Read one description attribute, identifying or not, as a string.

    The collector reports configured `non_identifying_attributes`, and the server
    serialises them as camelCase lists of `{key, value: {stringValue}}` — the shape
    this reads, not a mapping.
    """
    for group in ("identifyingAttributes", "nonIdentifyingAttributes"):
        for entry in description.get(group) or []:
            if entry.get("key") == key:
                value = entry.get("value") or {}
                if "stringValue" in value:
                    return value["stringValue"]
    return None


def find_agent(payload: Dict[str, Any], agent_name: str) -> Optional[Dict[str, Any]]:
    """The fleet entry whose `agent.name` (or `service.name`) is `agent_name`."""
    for agent in payload.get("agents") or []:
        description = agent.get("description") or {}
        if agent_name in (
            attribute(description, "agent.name"),
            attribute(description, "service.name"),
        ):
            return agent
    return None


def used_components(detail: Dict[str, Any]) -> List[str]:
    """`["receiver:otlp", ...]` for the components the server marked as used."""
    return [
        f"{group}:{component['id']}"
        for group, components in (detail.get("components") or {}).items()
        for component in components
        if component.get("used")
    ]


def check_manifest(payload: Dict[str, Any]) -> List[str]:
    """Problems with a generated manifest; empty means it is a buildable slim distro."""
    manifest = payload.get("manifest_yaml") or ""
    if not manifest:
        return ["manifest: empty"]
    missing = [needle for needle in EXPECTED_MANIFEST if needle not in manifest]
    return [f"manifest: missing {missing}"] if missing else []


def check_agent(detail: Dict[str, Any], agent_name: str) -> List[str]:
    """Problems with one agent detail payload; an empty list means conformant.

    Returned rather than raised so the failure message can name *every* problem at
    once: a CI log that makes you re-run to find the second issue wastes a day.
    """
    problems: List[str] = []
    description = detail.get("description") or {}

    if agent_name not in (
        attribute(description, "agent.name"),
        attribute(description, "service.name"),
    ):
        problems.append(
            f"agent name: expected {agent_name!r}, got "
            f"agent.name={attribute(description, 'agent.name')!r} "
            f"service.name={attribute(description, 'service.name')!r}"
        )

    if detail.get("healthy") is not True:
        problems.append(f"health: expected True, got {detail.get('healthy')!r}")

    # #77: a hash-only report is legal, but then nothing downstream can work. The real
    # client must answer `ReportAvailableComponents` — that is the whole handshake.
    reported = (detail.get("available_components") or {}).get("components") or {}
    if not reported:
        problems.append(
            "available_components: the agent reported no component list "
            "(hash-only report — ReportAvailableComponents was not answered)"
        )

    used = used_components(detail)
    if not used:
        problems.append(
            "components: no component is marked as used, so no manifest can be built"
        )
    else:
        missing = [name for name in EXPECTED_USED if name not in used]
        if missing:
            problems.append(f"components: expected used {missing}, got {used}")

    return problems


def wait_for_agent(
    client: httpx.Client,
    server: str,
    agent_name: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Poll the fleet until the collector appears *and* has reported components."""
    deadline = time.monotonic() + timeout
    last_seen: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        response = client.get(f"{server}/agents")
        response.raise_for_status()
        found = find_agent(response.json(), agent_name)
        if found is not None:
            detail = client.get(f"{server}/agent/{found['id']}")
            detail.raise_for_status()
            last_seen = detail.json()
            if not check_agent(last_seen, agent_name):
                return last_seen
        time.sleep(POLL_SECONDS)

    if last_seen is None:
        raise SystemExit(f"agent {agent_name!r} never appeared in GET /agents")
    problems = "\n  - ".join(check_agent(last_seen, agent_name))
    raise SystemExit(f"agent {agent_name!r} appeared but is not conformant:\n  - {problems}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    with httpx.Client(timeout=10.0) as client:
        detail = wait_for_agent(client, args.server, args.agent_name, args.timeout)

        manifest = client.post(f"{args.server}/agent/{detail['id']}/manifest")
        if manifest.status_code != 200:
            raise SystemExit(
                f"POST /agent/{detail['id']}/manifest: {manifest.status_code} "
                f"{manifest.text}"
            )

        payload = manifest.json()
        problems = check_manifest(payload)
        if problems:
            raise SystemExit("\n".join(problems))

        print(f"agent      : {detail['id']}")
        print(f"name       : {attribute(detail['description'], 'agent.name')}")
        print(f"healthy    : {detail['healthy']}")
        print(f"used       : {', '.join(used_components(detail))}")
        print(
            f"manifest   : 200, {len(payload['manifest_yaml'])} bytes, "
            f"{len(EXPECTED_MANIFEST)} components"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
