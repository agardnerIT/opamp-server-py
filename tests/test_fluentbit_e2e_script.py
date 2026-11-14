"""The Fluent Bit e2e script's pure helpers (#123).

`scripts/fluentbit_e2e.py` runs in CI against a real agent, where a failure is expensive
to debug. These tests pin the *judgements* it makes — which agent counts as Fluent Bit,
what a good inventory looks like, what a verdict from the wrong profile looks like — so a
red workflow means the agent misbehaved, not that the script's own logic drifted.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "fluentbit_e2e.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("fluentbit_e2e", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD_AGENT = {
    "agent_type": "fluentbit",
    "agent_type_source": "declared",
    "components_status": "reported",
    "components": {"input": [{"id": "dummy"}], "output": [{"id": "stdout"}]},
}


def test_a_good_agent_has_no_problems(script):
    assert script.check_agent(GOOD_AGENT) == []


def test_an_agent_that_resolved_to_generic_is_the_failure_this_exists_for(script):
    """The dictionary is the thing under test: a regression makes this agent `generic`."""
    problems = script.check_agent({**GOOD_AGENT, "agent_type": "generic"})

    assert any("agent_type" in problem for problem in problems)
    assert script.find_agent({"agents": [{**GOOD_AGENT, "agent_type": "generic"}]}) is None


def test_an_unreported_inventory_is_a_problem(script):
    problems = script.check_agent({**GOOD_AGENT, "components": {}, "components_status": "unreported"})

    assert any("components_status" in problem for problem in problems)
    assert any("input and output" in problem for problem in problems)


def test_a_verdict_must_be_the_profiles_own(script):
    assert script.check_verdict(
        {"valid": True, "profile": "fluentbit", "structure_checked": True, "errors": []}
    ) == []

    wrong_profile = script.check_verdict(
        {"valid": True, "profile": "otelcol", "structure_checked": True, "errors": []}
    )
    assert any("otelcol" in problem for problem in wrong_profile)

    unchecked = script.check_verdict(
        {"valid": True, "profile": "fluentbit", "structure_checked": False, "errors": []}
    )
    assert any("structure_checked" in problem for problem in unchecked)


def test_collector_vocabulary_in_a_fluent_bit_verdict_is_a_failure(script):
    problems = script.check_verdict(
        {
            "valid": True,
            "profile": "fluentbit",
            "structure_checked": True,
            "errors": [],
            "warnings": ["no receivers section"],
        }
    )

    assert any("collector word" in problem for problem in problems)


def test_the_pushed_config_is_what_the_profile_requires(script):
    """The fixture config must pass the profile it is pushed to, or CI debugs the wrong thing."""
    from server.config_validation import check_config_map

    verdict = check_config_map(
        {"./fluent-bit.conf": {"body": script.PUSHED_CONFIG, "content_type": "text/plain"}},
        profile="fluentbit",
        run_policy=False,
    )

    assert verdict.ok, verdict.error_messages
    assert verdict.structure_checked is True


def test_the_reported_config_key_is_read_from_the_report(script):
    """The key the agent reports is the key a revision must use (#131)."""
    detail = {
        "effective_config": (
            '{"configMap": {"configMap": {"./fluent-bit.conf": {"body": "", '
            '"contentType": "text/plain"}}}}'
        )
    }
    assert script.reported_config_key(detail) == "./fluent-bit.conf"
    assert script.reported_config_key({}) is None
    assert script.reported_config_key({"effective_config": "not json"}) is None


class _FakeClient:
    """A server that registers the agent one poll before it reports its inventory."""

    def __init__(self, details):
        self.details = list(details)
        self.polls = 0

    def get(self, path):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        if path == "/agents":
            self.polls += 1
            return _Response({"agents": [{"id": "abc123def456", "agent_type": "fluentbit",
                                          "components_status": "unreported"}], "count": 1})
        # Every `/agent/<id>` call pops the next detail: unreported first, then reported.
        detail = self.details[min(self.polls - 1, len(self.details) - 1)]
        return _Response(detail)


def test_wait_for_agent_waits_for_the_inventory_not_just_registration(script):
    """Registration lands a heartbeat before the config the profile inventories (#138)."""
    unreported = {"id": "abc123def456", "agent_type": "fluentbit",
                  "agent_type_source": "declared",
                  "components_status": "unreported", "components": {}}
    reported = dict(unreported, components_status="reported",
                    components={"input": [{"id": "dummy"}], "output": [{"id": "stdout"}]})

    client = _FakeClient([unreported, reported])
    detail = script.wait_for_agent(client, timeout=5)

    assert detail["components_status"] == "reported"
    assert script.check_agent(detail) == []
    assert client.polls >= 2


def test_wait_for_agent_says_what_it_saw_when_it_gives_up(script):
    """A timeout has to name the agents it saw, or CI red is a guessing game."""
    unreported = {"id": "abc123def456", "agent_type": "fluentbit",
                  "agent_type_source": "declared",
                  "components_status": "unreported", "components": {}}

    with pytest.raises(SystemExit) as failure:
        script.wait_for_agent(_FakeClient([unreported]), timeout=0.01)

    assert "reported inventory" in str(failure.value)
