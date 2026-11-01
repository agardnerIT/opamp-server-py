"""The pre-push validation pipeline (#100), stage by stage.

Every stage has a fixture in `tests/fixtures/configs/` and a test asserting the
*structured* error path (a YAML path a UI/CLI can point at), not just "it failed".

The pipeline's honest limit is asserted too: it cannot prove a config will not crash
a collector, so what is tested is the set of mistakes that are recognisable before
delivery — malformed YAML, an alias bomb, duplicate keys, a pipeline pointing at a
component that does not exist, a config that orphans the agent by deleting the
`opamp` extension, and a config that moves the control channel to another host.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from server import config_validation

FIXTURES = Path(__file__).parent / "fixtures" / "configs"


def body(name: str) -> str:
    return (FIXTURES / name).read_text()


def check(name: str, **kwargs) -> config_validation.Verdict:
    return config_validation.check_config_map(
        {"": {"body": body(name), "content_type": "text/yaml"}}, **kwargs
    )


def paths(verdict: config_validation.Verdict) -> list:
    return [finding.path for finding in verdict.errors]


def messages(verdict: config_validation.Verdict) -> str:
    return " | ".join(verdict.error_messages)


@pytest.fixture(autouse=True)
def _opamp_url(monkeypatch):
    """The endpoint the valid fixture points at is this server's (posture #96)."""
    from server import posture

    monkeypatch.setattr(posture, "public_url", lambda: "https://opamp.example.com")


# ------------------------------------------------------------------------ stage 0


def test_a_valid_config_passes_with_no_findings():
    verdict = check("valid.yaml")
    assert verdict.ok, verdict.error_messages
    assert verdict.errors == []
    assert verdict.warnings == []
    assert verdict.policy["status"] == "skipped"


# ------------------------------------------------------------------------ stage 1


def test_oversized_config_is_refused_before_parsing(monkeypatch):
    monkeypatch.setenv("CONFIG_MAX_BYTES", "10")
    verdict = check("valid.yaml")
    assert not verdict.ok
    assert any("CONFIG_MAX_BYTES" in finding.message for finding in verdict.errors)


def test_an_empty_config_map_is_refused_because_withdrawal_is_not_a_push():
    verdict = config_validation.check_config_map({})
    assert not verdict.ok
    assert "config_map is empty" in verdict.errors[0].message


def test_a_redacted_body_can_never_be_pushed():
    verdict = config_validation.check_config_map(
        {"": {"body": "exporters:\n  x:\n    password: <redacted>\n", "content_type": "text/yaml"}}
    )
    assert not verdict.ok
    assert "redaction marker" in verdict.errors[0].message


def test_a_non_utf8_body_is_refused_by_shape_checks():
    verdict = config_validation.check_config_map({"": {"body": "\ud800", "content_type": ""}})
    assert not verdict.ok
    assert "UTF-8" in messages(verdict)


# ------------------------------------------------------------------------ stage 2


def test_yaml_that_does_not_parse_is_an_error_with_a_hint():
    verdict = check("invalid_yaml.yaml")
    assert not verdict.ok
    finding = verdict.errors[0]
    assert "not valid" in finding.message
    assert "stays down" in finding.hint


def test_an_undeclared_content_type_that_does_not_parse_only_warns():
    """A config object is not obliged to be YAML (a PEM file, a shell fragment)."""
    verdict = config_validation.check_config_map(
        {"cert.pem": {"body": "-----BEGIN CERTIFICATE-----\nnot yaml: [", "content_type": ""}}
    )
    assert verdict.ok
    assert verdict.warnings
    assert "not valid" in verdict.warnings[0].message


def test_duplicate_keys_are_refused_instead_of_last_wins():
    """PyYAML silently keeps the last value; a reviewed config would differ from the
    running one, which is exactly the surprise this pipeline exists to stop."""
    verdict = check("duplicate_keys.yaml")
    assert not verdict.ok
    assert "duplicate key" in messages(verdict)
    assert "debug" in paths(verdict)[0] + messages(verdict)


def test_a_yaml_merge_key_is_not_a_duplicate():
    text = (
        "defaults: &defaults\n"
        "  verbosity: basic\n"
        "exporters:\n"
        "  debug:\n"
        "    <<: *defaults\n"
        "    verbosity: detailed\n"
    )
    verdict = config_validation.check_config_map(
        {"": {"body": text, "content_type": "text/yaml"}}
    )
    assert not any("duplicate key" in finding.message for finding in verdict.errors)


def test_an_alias_bomb_is_refused_in_bounded_time_and_memory():
    started = time.monotonic()
    verdict = check("alias_bomb.yaml")
    elapsed = time.monotonic() - started
    assert not verdict.ok
    assert "alias bomb" in messages(verdict)
    # 9^9 nodes would take minutes to expand; the guard is a counter, not a timeout.
    assert elapsed < 2.0, f"alias guard took {elapsed:.1f}s"


def test_deep_nesting_is_refused(monkeypatch):
    monkeypatch.setenv("CONFIG_MAX_DEPTH", "5")
    verdict = config_validation.check_config_map(
        {"": {"body": "a: " + "[" * 8 + "]" * 8, "content_type": "text/yaml"}}
    )
    assert not verdict.ok
    assert "nests deeper" in messages(verdict)


def test_the_parser_only_uses_safe_tags():
    """`!!python/object/apply` must not execute: SafeLoader has no such constructor."""
    verdict = config_validation.check_config_map(
        {
            "": {
                "body": "x: !!python/object/apply:os.system ['echo pwned']\n",
                "content_type": "text/yaml",
            }
        }
    )
    assert not verdict.ok
    assert "could not determine a constructor" in messages(verdict)


# ------------------------------------------------------------------------ stage 3


def test_a_pipeline_referencing_an_undefined_component_is_refused_with_its_path():
    verdict = check("missing_component.yaml")
    assert not verdict.ok
    assert "service.pipelines.traces.processors[0]" in paths(verdict)
    assert "`memory_limiter` is referenced but not defined" in messages(verdict)


def test_empty_pipelines_are_refused():
    verdict = check("empty_pipelines.yaml")
    assert not verdict.ok
    assert "service.pipelines" in paths(verdict)


def test_a_pipeline_without_an_exporter_is_refused():
    text = body("valid.yaml").replace("      exporters: [otlphttp]\n", "")
    verdict = config_validation.check_config_map(
        {"": {"body": text, "content_type": "text/yaml"}}
    )
    assert not verdict.ok
    assert "has no exporters" in messages(verdict)


def test_an_id_enabled_in_service_extensions_must_exist():
    text = body("valid.yaml").replace("extensions: [opamp]", "extensions: [opamp, pprof]")
    verdict = config_validation.check_config_map(
        {"": {"body": text, "content_type": "text/yaml"}}
    )
    assert not verdict.ok
    assert "`pprof` is enabled in `service.extensions` but not defined" in messages(verdict)


def test_a_fragment_without_service_is_not_structurally_judged():
    verdict = check("no_service.yaml")
    assert verdict.ok
    assert any("no `service` section" in finding.message for finding in verdict.warnings)


# ------------------------------------------------------------------------ stage 4


def test_removing_the_opamp_extension_is_refused_with_an_explanation():
    verdict = check("opamp_removed.yaml")
    assert not verdict.ok
    assert "extensions.opamp" in paths(verdict)
    assert "control channel" in verdict.errors[0].hint


def test_removing_opamp_from_service_extensions_is_refused():
    verdict = check("opamp_not_enabled.yaml")
    assert not verdict.ok
    assert "service.extensions" in paths(verdict)


def test_changing_the_opamp_endpoint_is_refused_by_default():
    verdict = check("opamp_endpoint_changed.yaml")
    assert not verdict.ok
    assert "would move to" in messages(verdict)
    assert "allow_endpoint_change" in verdict.errors[0].hint


def test_changing_the_opamp_endpoint_is_allowed_when_explicitly_permitted():
    verdict = check("opamp_endpoint_changed.yaml", allow_endpoint_change=True)
    assert verdict.ok, verdict.error_messages
    assert any("explicitly allowed" in finding.message for finding in verdict.warnings)


def test_an_exporter_host_outside_the_allowlist_warns_but_does_not_block(monkeypatch):
    monkeypatch.setenv("CONFIG_ALLOWED_EXPORTER_HOSTS", "backend.example.com")
    verdict = check("disallowed_exporter.yaml")
    assert verdict.ok, verdict.error_messages
    assert any("CONFIG_ALLOWED_EXPORTER_HOSTS" in finding.message for finding in verdict.warnings)


def test_missing_memory_limiter_and_batch_only_warn():
    verdict = check("no_batch.yaml")
    assert verdict.ok, verdict.error_messages
    messages_all = " ".join(finding.message for finding in verdict.warnings)
    assert "memory_limiter" in messages_all or "batch" in messages_all


# ------------------------------------------------------------------------ stage 5


class FakeOpa:
    def __init__(self, violations=None, error=None, compliant=True):
        self.violations = violations or []
        self.error = error
        self.compliant = compliant

    def evaluate(self, input_data):
        if self.error:
            return {"compliant": None, "violations": [], "error": self.error}
        return {
            "compliant": not self.violations,
            "violations": self.violations,
            "policy_results": [{"name": "require_x", "status": "fail" if self.violations else "pass"}],
        }


def test_policy_violations_block_and_name_the_violation(monkeypatch):
    monkeypatch.setattr(
        "server.opa_client.get_opa_client",
        lambda: FakeOpa(violations=[{"msg": "exporters must use TLS"}]),
    )
    verdict = check("valid.yaml")
    assert not verdict.ok
    assert verdict.policy["status"] == "violations"
    assert "exporters must use TLS" in messages(verdict)


def test_a_policy_gate_error_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "server.opa_client.get_opa_client", lambda: FakeOpa(error="OPA is down")
    )
    verdict = check("valid.yaml")
    assert not verdict.ok
    assert "could not run" in messages(verdict)


def test_opa_disabled_is_reported_as_skipped_never_as_a_pass(monkeypatch):
    monkeypatch.setattr("server.opa_client.get_opa_client", lambda: None)
    verdict = check("valid.yaml")
    assert verdict.ok
    assert verdict.policy["status"] == "skipped"
    assert "did not run" in verdict.policy["reason"]


# ------------------------------------------------------------------- stage 6/7


def test_inventory_warnings_name_the_agents_that_lack_the_component():
    class FakeAgent:
        def __init__(self, agent_id, components):
            self.agent_id = agent_id
            self.available_components = {
                "components": {
                    section: {"subComponentMap": {name: {} for name in names}}
                    for section, names in components.items()
                }
            }

    agent = FakeAgent(
        "ab" * 16,
        {"receivers": ["otlp"], "exporters": ["debug"], "processors": ["batch"]},
    )
    verdict = config_validation.check_config_map(
        {"": {"body": body("valid.yaml"), "content_type": "text/yaml"}}, agents=[agent]
    )
    assert verdict.ok
    messages_all = " ".join(finding.message for finding in verdict.warnings)
    assert "otlphttp" in messages_all
    assert "memory_limiter" in messages_all
    assert "abababab" in messages_all


def test_the_verdict_fingerprint_changes_when_the_outcome_changes():
    ok = check("valid.yaml")
    bad = check("opamp_removed.yaml")
    assert ok.fingerprint() != bad.fingerprint()
    assert ok.fingerprint() == check("valid.yaml").fingerprint()


def test_the_verdict_is_json_safe_and_never_contains_a_body():
    verdict = config_validation.check_config_map(
        {"": {"body": body("valid.yaml"), "content_type": "text/yaml"}}
    )
    payload = verdict.to_dict()
    assert payload["ok"] is True
    assert "body" not in str(payload)
    assert "otlphttp" not in str(payload)


def test_drift_subset_comparison():
    contains = config_validation.contains_document
    assert contains({"a": 1}, {"a": 1, "b": 2}) == []
    assert contains({"a": 1}, {"a": 2}) == ["a"]
    assert contains({"a": {"b": 1}}, {"a": {"c": 2}}) == ["a.b"]
    assert contains([1, 2], [2, 1, 3]) == []
    assert contains([1, 4], [1, 2]) == ["[1]"]


def test_document_drift_matches_any_reported_body():
    desired = [("", "service:\n  telemetry:\n    logs:\n      level: info\n")]
    assert config_validation.document_drift(desired, ["service:\n  telemetry: {}\n"]) == [
        "service.telemetry.logs"
    ]
    assert (
        config_validation.document_drift(
            desired,
            [
                "receivers:\n  otlp: {}\n",
                "service:\n  telemetry:\n    logs:\n      level: info\n",
            ],
        )
        == []
    )
