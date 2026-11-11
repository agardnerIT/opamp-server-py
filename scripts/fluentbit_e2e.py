"""Assert that a *real* Fluent Bit agent works end to end (#123).

`scripts/collector_e2e.py` proves the OTel path with the upstream `opamp` extension.
This proves the other half of #117's claim: a product that is not a collector registers,
is **identified**, is **inventoried from its own config**, and takes a pushed config that
its own rules judged — with no OpenTelemetry vocabulary anywhere in the exchange.

Deliberately dumb HTTP (same rule as the collector script): no server imports, no
fixtures, so CI can run it against a server in another process and a developer can run it
against a server on their laptop. The pure helpers are unit tested in
`tests/test_fluentbit_e2e_script.py`.

Usage:

    python scripts/fluentbit_e2e.py --server http://localhost:4320 \
        --password "$ADMIN_PASSWORD" --timeout 120

The agent side is `opamp-observe/opamp-core`'s consumer supervising Fluent Bit; see
`.github/workflows/fluentbit-e2e.yml` for the pinned versions and the launch command.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_SERVER = "http://localhost:4320"
DEFAULT_TIMEOUT = 120.0
POLL_SECONDS = 2.0

#: The identity the supervisor declares (`service.type`), which is what the profile's
#: matcher reads. Asserted so a dictionary regression cannot silently make this agent
#: `generic` again — that is exactly the failure #123 exists to catch.
EXPECTED_TYPE = "fluentbit"

#: A minimal Fluent Bit configuration, in the product's own dialect. The e2e pushes this
#: and expects it back verbatim; `[SERVICE]`/`[INPUT]`/`[OUTPUT]` are what the profile's
#: rules require, so a broken validator shows up as a 422 here.
PUSHED_CONFIG = """\
[SERVICE]
    Flush    2
    Daemon   off
    Log_Level info

[INPUT]
    Name  dummy
    Tag   dummy

[OUTPUT]
    Name  stdout
    Match *
"""


def find_agent(payload: Dict[str, Any], agent_type: str = EXPECTED_TYPE) -> Optional[Dict[str, Any]]:
    """The first agent the server resolved to `agent_type`, or None.

    Keyed on the *server's* resolution rather than on a hostname: an agent that names
    itself `Fluent Bit` but resolves to `generic` is the bug this asserts against.
    """
    for agent in payload.get("agents") or []:
        if agent.get("agent_type") == agent_type:
            return agent
    return None


def check_agent(detail: Dict[str, Any]) -> List[str]:
    """Every failed expectation about a registered Fluent Bit agent.

    An empty list is a pass. Messages are written for a log a human reads when CI is red.
    """
    problems: List[str] = []
    if detail.get("agent_type") != EXPECTED_TYPE:
        problems.append(
            f"agent_type is {detail.get('agent_type')!r}, expected {EXPECTED_TYPE!r} "
            "(the dictionary's identity rules did not match the real supervisor)"
        )
    if detail.get("agent_type_source") not in ("declared", "sniffed", "pinned"):
        problems.append(
            f"agent_type_source is {detail.get('agent_type_source')!r} — the agent's own "
            "description should be enough to identify it"
        )

    # The inventory comes from the config document, because opamp-core sends no
    # `AvailableComponents` at all.
    status = detail.get("components_status")
    if status != "reported":
        problems.append(f"components_status is {status!r}, expected 'reported'")
    components = detail.get("components") or {}
    kinds = set(components)
    if not {"input", "output"} <= kinds:
        problems.append(
            f"components {sorted(kinds)} do not include input and output "
            "(the profile reads them from the INI document)"
        )
    return problems


def check_verdict(verdict: Dict[str, Any]) -> List[str]:
    """The validation verdict must be the *profile's* judgement, not a collector's."""
    problems: List[str] = []
    if not verdict.get("valid"):
        problems.append(f"the pushed config was refused: {verdict.get('errors')}")
    if verdict.get("profile") != EXPECTED_TYPE:
        problems.append(
            f"the verdict was produced by profile {verdict.get('profile')!r}, expected "
            f"{EXPECTED_TYPE!r}"
        )
    if verdict.get("structure_checked") is not True:
        problems.append(
            "structure_checked is not true: the profile's rules did not run, so an "
            "approver would have to acknowledge an unchecked body"
        )
    blob = str(verdict).lower()
    for word in ("receivers", "processors", "exporters", "pipelines"):
        if word in blob:
            problems.append(f"a collector word ({word!r}) appeared in a Fluent Bit verdict")
    return problems


def wait_for_agent(
    client: httpx.Client, timeout: float, agent_type: str = EXPECTED_TYPE
) -> Dict[str, Any]:
    """Poll `/agents` until an agent resolves to `agent_type`, then return its detail."""
    deadline = time.monotonic() + timeout
    seen: List[str] = []
    while time.monotonic() < deadline:
        listing = client.get("/agents").json()
        for agent in listing.get("agents") or []:
            seen.append(f"{agent.get('id', '?')[:8]}={agent.get('agent_type')}")
        found = find_agent(listing, agent_type)
        if found is not None:
            return client.get(f"/agent/{found['id']}").json()
        time.sleep(POLL_SECONDS)
    raise SystemExit(
        f"no agent resolved to {agent_type!r} within {timeout:.0f}s; "
        f"agents seen: {', '.join(sorted(set(seen))) or 'none'}"
    )


def reported_config_key(detail: Dict[str, Any]) -> Optional[str]:
    """The `config_map` key the agent reports its config under, or None.

    `opamp-core` reports `./fluent-bit.conf` — a *path*, not `""`. A revision keyed
    differently is one the agent would write somewhere else (or refuse), so the e2e
    pushes under the reported key rather than inventing one.
    """
    import json

    report = detail.get("effective_config")
    if not report:
        return None
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except (TypeError, ValueError):
            return None
    inner = ((report or {}).get("configMap") or {}).get("configMap") or {}
    for key in inner:
        return str(key)
    return None


def _admin(client: httpx.Client, password: str) -> None:
    response = client.post("/auth/session", json={"name": "e2e", "password": password})
    if response.status_code != 201:
        raise SystemExit(f"admin sign-in failed: {response.status_code} {response.text}")
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


def push_and_confirm(client: httpx.Client, agent_id: str, key: str) -> Dict[str, Any]:
    """Propose, validate, approve and deliver one config change; return the proposal."""
    config_map = {key: {"body": PUSHED_CONFIG, "content_type": "text/plain"}}

    validated = client.post(
        "/config/validate", json={"config_map": config_map, "agent_id": agent_id}
    ).json()
    problems = check_verdict(validated)
    if problems:
        raise SystemExit("validation: " + "; ".join(problems))

    proposed = client.post(
        "/config/revisions",
        json={"config_map": config_map, "scope": {"agent_id": agent_id}, "note": "e2e"},
    )
    if proposed.status_code != 200:
        raise SystemExit(f"propose failed: {proposed.status_code} {proposed.text}")
    proposal = proposed.json()

    rollout = client.post(
        "/config/rollouts",
        params={"proposal_id": proposal["proposal_id"]},
        json={"canary_percent": 100, "health_gate_seconds": 0},
    )
    if rollout.status_code != 200:
        raise SystemExit(f"rollout failed: {rollout.status_code} {rollout.text}")
    return rollout.json()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--password", required=True, help="ADMIN_PASSWORD of the server")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    with httpx.Client(base_url=args.server, timeout=15.0) as client:
        detail = wait_for_agent(client, args.timeout)

        problems = check_agent(detail)
        if problems:
            print("\n".join(f"FAIL {problem}" for problem in problems))
            return 1
        print(
            f"agent {detail['id'][:12]} is {detail['agent_type']} "
            f"({detail['agent_type_source']}), components reported: "
            f"{sorted((detail.get('components') or {}))}"
        )

        # The key the *agent* reports is the key a revision must use (the #131 rule);
        # this is also what proves the profile reads the same document the agent runs.
        key = reported_config_key(detail) or "./fluent-bit.conf"

        _admin(client, args.password)
        created = push_and_confirm(client, detail["id"], key)
        print(f"rollout {created.get('rollout_id')} created ({created.get('status')})")

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            rollout = client.get(f"/config/rollouts/{created['rollout_id']}").json()
            states = (rollout.get("agents") or {}).get("states") or {}
            if rollout.get("status") == "completed" or states.get("applied"):
                # APPLIED is the agent's own word for "I wrote and reloaded this".
                print(f"rollout {rollout.get('status')}: {states}")
                return 0
            if rollout.get("status") in ("paused", "aborted", "cancelled"):
                raise SystemExit(
                    f"rollout {rollout['status']}: {rollout.get('paused_reason')} "
                    f"{states}"
                )
            time.sleep(POLL_SECONDS)

    raise SystemExit(f"the rollout did not reach the agent within {args.timeout:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
