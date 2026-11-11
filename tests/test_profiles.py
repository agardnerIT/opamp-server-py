"""Agent profiles: resolution, provenance and the generic fallback (#118).

Three things are being pinned here, in the order they matter:

1. **The move changed nothing.** `test_the_move_changed_no_output` byte-compares
   components, `to_dict()` payloads, all three fleet reports and a validation verdict
   against `tests/fixtures/agent_type_golden.json`, captured from the pre-refactor
   tree (see `tests/golden_payloads.py`).
2. **Precedence and provenance.** pinned > declared (`content_type`, then
   `service.name`) > sniffed > fallback, with the `source` reported alongside, because
   a resolution nobody can attribute is a resolution nobody can trust.
3. **Type may only subtract.** A spoofed or wrong type can change a *label*, never a
   permission: `generic` has no components and no reports, and the format-agnostic
   checks in `config_validation` run for every registered profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import config_validation, profiles
from server.state import AgentState

FIXTURE = Path(__file__).parent / "fixtures" / "agent_type_golden.json"

COLLECTOR_CONFIG = """\
receivers:
  otlp:
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
"""


def _config_body(body: str, content_type: str = "") -> str:
    """An `EffectiveConfig` body in `MessageToDict` shape (as stored on the agent)."""
    import base64

    entry = {
        "body": base64.b64encode(body.encode()).decode(),
    }
    if content_type:
        entry["contentType"] = content_type
    return json.dumps({"configMap": {"configMap": {"": entry}}})


def _agent(**kwargs) -> AgentState:
    kwargs.setdefault("instance_uid", b"\x00" * 16)
    kwargs.setdefault("agent_id", "a" * 32)
    return AgentState(**kwargs)


def _named(service_name: str) -> AgentState:
    return _agent(
        description={
            "identifyingAttributes": [
                {"key": "service.name", "value": {"stringValue": service_name}}
            ]
        }
    )


# --------------------------------------------------------------------- move ----


def test_the_move_changed_no_output():
    """The refactor was a move, so every byte of output must be identical."""
    from tests import golden_payloads

    expected = json.loads(FIXTURE.read_text())
    assert golden_payloads.capture() == expected


# ---------------------------------------------------------------- detection ----


class TestComponentTypeDetection:
    """Moved from `tests/test_state.py`, then from otelcol.py to the dictionary (#134).

    The table itself is data now (`components.sections` / `components.known` in
    `server/profiles/data/otelcol.yaml`); this pins that the compiled lookup still
    answers the same way.
    """

    detector = staticmethod(lambda comp_id: profiles.get("otelcol").detect_component_type(comp_id))

    def test_receiver(self):
        assert self.detector("receivers/otlp") == "receiver"

    def test_processor(self):
        assert self.detector("processors/batch") == "processor"

    def test_exporter(self):
        assert self.detector("exporters/otlp") == "exporter"

    def test_extension(self):
        assert self.detector("extensions/zpages") == "extension"

    def test_connector(self):
        assert self.detector("connectors/forward") == "connector"

    def test_a_bare_known_component(self):
        assert self.detector("batch") == "processor"

    def test_unknown(self):
        assert self.detector("some/random") == "unknown"

    def test_a_generic_agent_has_no_component_vocabulary(self):
        """#134: a profile with no `components` block answers `unknown`, not "collector"."""
        assert profiles.get("generic").detect_component_type("receivers/otlp") == "unknown"


class TestResolutionPrecedence:
    def test_a_pin_beats_every_other_signal(self):
        agent = _named("otelcol-contrib")
        agent.agent_type = "generic"
        assert profiles.resolve(agent).name == "generic"
        assert profiles.resolve(agent).source == "pinned"

    def test_a_distinctive_content_type_declares_the_profile(self, monkeypatch):
        """The mechanism exists for a profile that owns a MIME type; `otelcol` does not.

        `text/yaml` is a *format* claim (FluentBit and a collector both send it), so it
        must never be read as identity — that is the whole reason `ConfigFormat` has a
        `distinctive` flag. A profile that really owns a MIME type can declare one.
        """
        from server.profiles.base import AgentProfile, ConfigFormat

        own_mime = AgentProfile(
            name="testable",
            display_name="Testable",
            config_formats=(
                ConfigFormat(content_types=("application/x-testable",), distinctive=True),
            ),
        )
        otelcol = profiles.get("otelcol")
        monkeypatch.setattr(profiles.registry, "PROFILES", (otelcol, own_mime))
        monkeypatch.setattr(
            profiles.registry,
            "_BY_FOLD",
            {"otelcol": otelcol, "testable": own_mime},
        )

        agent = _agent(
            effective_config=_config_body("a: 1\n", "application/x-testable; charset=utf-8")
        )
        assert profiles.resolve(agent).name == "testable"
        assert profiles.resolve(agent).source == "declared"

    def test_a_generic_content_type_is_not_identity(self):
        """`text/yaml` alone must not make an agent a collector (#118, A1)."""
        agent = _agent(effective_config=_config_body("key: value\n", "text/yaml"))
        assert profiles.resolve(agent).name == "generic"
        assert profiles.resolve(agent).source == "fallback"

    def test_service_name_declares_the_profile(self):
        """The spec's own convention: `service.name` SHOULD identify the agent type."""
        for name in ("otelcol", "otelcol-contrib", "io.opentelemetry.collector"):
            resolution = profiles.resolve(_named(name))
            assert (resolution.name, resolution.source) == ("otelcol", "declared"), name

    def test_service_name_matching_is_case_insensitive(self):
        assert profiles.resolve(_named("OTelCol-Contrib")).name == "otelcol"

    def test_an_unrelated_service_name_is_not_otelcol(self):
        """`fluent-bit` is a *known* product with its own profile (#123), not a collector.

        The assertion is deliberately "not otelcol" rather than "generic": the fallback
        was the right answer until the dictionary learned the product, and a test that
        pinned `generic` would have to be rewritten every time a profile is added.
        """
        assert profiles.resolve(_named("fluent-bit")).name == "fluentbit"
        assert profiles.resolve(_named("acme-agent")).name == "generic"

    def test_available_components_sniff_the_collector(self):
        agent = _agent(
            available_components={
                "components": {"receivers": {"subComponentMap": {"otlp": {}}}}
            }
        )
        resolution = profiles.resolve(agent)
        assert (resolution.name, resolution.source) == ("otelcol", "sniffed")

    def test_a_pipeline_config_sniffs_the_collector(self):
        """No component list at all: the config's own components are the evidence."""
        agent = _agent(effective_config=_config_body(COLLECTOR_CONFIG))
        resolution = profiles.resolve(agent)
        assert (resolution.name, resolution.source) == ("otelcol", "sniffed")

    def test_a_heartbeat_only_agent_is_generic(self):
        resolution = profiles.resolve(_agent())
        assert (resolution.name, resolution.source) == ("generic", "fallback")

    def test_an_unknown_pin_falls_through_to_detection(self):
        """A pin naming a profile this build does not have must not be trusted."""
        agent = _named("otelcol")
        agent.agent_type = "no-such-product"  # a downgrade, or a typo in the database
        resolution = profiles.resolve(agent)
        assert (resolution.name, resolution.source) == ("otelcol", "declared")


class TestResolutionCache:
    def test_a_resolution_is_memoised_on_the_report(self, monkeypatch):
        calls = []
        original = profiles.registry.resolve

        def counting(agent, **kwargs):
            calls.append(agent.agent_id)
            return original(agent, **kwargs)

        monkeypatch.setattr(profiles.registry, "resolve", counting)
        agent = _named("otelcol")
        for _ in range(5):
            agent.profile_resolution()
        assert len(calls) == 1

    def test_a_new_report_invalidates_the_cache(self, monkeypatch):
        calls = []
        original = profiles.registry.resolve

        def counting(agent, **kwargs):
            calls.append(agent.agent_id)
            return original(agent, **kwargs)

        monkeypatch.setattr(profiles.registry, "resolve", counting)
        agent = _named("otelcol")
        assert agent.profile_resolution().name == "otelcol"

        agent.available_components = {
            "components": {"exporters": {"subComponentMap": {"debug": {}}}}
        }
        assert agent.profile_resolution().name == "otelcol"
        assert len(calls) == 2

    def test_pinning_invalidates_the_cache(self, monkeypatch):
        calls = []
        original = profiles.registry.resolve

        def counting(agent, **kwargs):
            calls.append(agent.agent_id)
            return original(agent, **kwargs)

        monkeypatch.setattr(profiles.registry, "resolve", counting)
        agent = _named("otelcol")
        assert agent.profile_resolution().name == "otelcol"
        agent.agent_type = "generic"
        assert agent.profile_resolution().name == "generic"
        assert len(calls) == 2

    def test_a_pinned_agent_never_sniffs(self, monkeypatch):
        import dataclasses

        sniffed = []
        # A frozen dataclass field: `replace` is how a test swaps a hook.
        noisy = dataclasses.replace(
            profiles.get("otelcol"), sniff=lambda agent: sniffed.append(agent) or True
        )
        monkeypatch.setattr(profiles.registry, "PROFILES", (noisy, profiles.FALLBACK))
        agent = _agent()
        agent.agent_type = "generic"
        assert agent.profile_resolution().source == "pinned"
        # A heartbeat-only agent would otherwise sniff; the pin short-circuits it.
        assert agent.components == {}
        assert sniffed == []


class TestTypeIsSubstractiveOnly:
    """A profile may suppress what the server claims. It may never grant anything."""

    def test_generic_has_no_components_and_no_reports(self):
        generic = profiles.get("generic")
        assert generic.extract_components(_agent()) == {}
        assert generic.reports == ()
        assert generic.telemetry is None
        assert generic.validates_structure is False

    def test_otelcol_keeps_its_components(self):
        agent = _agent(
            available_components={
                "components": {"receivers": {"subComponentMap": {"otlp": {}}}}
            }
        )
        assert agent.components == {"receiver": [{"id": "otlp", "version": "", "used": False}]}

    def test_a_generic_agent_gets_a_warning_and_a_reason(self):
        info = _agent().type_info()
        assert info["agent_type"] == "generic"
        assert info["agent_type_source"] == "fallback"
        assert info["agent_type_pinned"] is None
        assert "does not interpret its configuration" in info["agent_type_warning"]

    def test_an_otelcol_agent_has_no_warning(self):
        info = _named("otelcol").type_info()
        assert info["agent_type"] == "otelcol"
        assert info["agent_type_warning"] is None

    def test_the_pin_is_reported_separately_from_the_resolution(self):
        agent = _named("otelcol")
        agent.agent_type = "otelcol"
        info = agent.type_info()
        assert info["agent_type"] == "otelcol"
        assert info["agent_type_source"] == "pinned"
        assert info["agent_type_pinned"] == "otelcol"

    def test_to_dict_carries_the_type_for_every_read_surface(self):
        payload = _agent().to_dict()
        for key in (
            "agent_type",
            "agent_type_source",
            "agent_type_pinned",
            "agent_type_warning",
        ):
            assert key in payload


class TestFormatAgnosticChecksAreTypeIndependent:
    """#118/A3: the dangerous checks are never behind a profile hook.

    Size, UTF-8, duplicate keys, the `<redacted>` refusal and approval-required are
    properties of *any* config body, so they must fire for every registered profile —
    including `otelcol`, which is where a future "fast path" would skip them.
    """

    @pytest.mark.parametrize("profile_name", profiles.names())
    def test_a_redacted_body_is_refused_for_every_profile(self, profile_name):
        agent = _agent()
        agent.agent_type = profile_name
        verdict = config_validation.check_config_map(
            {"": {"body": "exporters:\n  otlp:\n    headers:\n      x: <redacted>\n",
                 "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        assert not verdict.ok
        assert any("redacted" in message.lower() for message in verdict.error_messages)

    @pytest.mark.parametrize("profile_name", profiles.names())
    def test_an_oversized_body_is_refused_for_every_profile(self, profile_name, monkeypatch):
        monkeypatch.setenv("CONFIG_MAX_BYTES", "64")
        agent = _agent()
        agent.agent_type = profile_name
        verdict = config_validation.check_config_map(
            {"": {"body": "x: " + "y" * 200, "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        assert not verdict.ok

    @pytest.mark.parametrize("profile_name", profiles.names())
    def test_duplicate_keys_are_refused_for_every_profile(self, profile_name):
        agent = _agent()
        agent.agent_type = profile_name
        verdict = config_validation.check_config_map(
            {"": {"body": "a: 1\nb: 2\na: 3\n", "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        assert not verdict.ok

    @pytest.mark.parametrize("profile_name", profiles.names())
    def test_an_empty_body_is_refused_for_every_profile(self, profile_name):
        agent = _agent()
        agent.agent_type = profile_name
        verdict = config_validation.check_config_map(
            {"": {"body": "", "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        assert not verdict.ok


class TestVerdictAttribution:
    """#118/A2: a verdict must name the profile that produced it."""

    def test_the_verdict_names_the_profile_and_the_source(self):
        agent = _named("otelcol")
        verdict = config_validation.check_config_map(
            {"": {"body": COLLECTOR_CONFIG, "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        assert verdict.profile == "otelcol"
        assert verdict.profile_source == "declared"
        assert verdict.to_dict()["profile"] == "otelcol"
        assert verdict.to_dict()["profile_source"] == "declared"

    def test_an_unattributed_verdict_says_so(self):
        """"No agent named" is not the same as "otelcol" — do not invent one."""
        verdict = config_validation.check_config_map(
            {"": {"body": COLLECTOR_CONFIG, "content_type": "text/yaml"}},
            run_policy=False,
        )
        assert verdict.profile == ""
        assert verdict.profile_source == ""

    def test_a_generic_target_gets_a_warning_about_the_findings(self):
        agent = _agent()
        verdict = config_validation.check_config_map(
            {"": {"body": COLLECTOR_CONFIG, "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        assert verdict.profile == "generic"
        assert any("does not interpret" in w for w in verdict.warning_messages)

    def test_a_mixed_target_set_is_not_averaged(self):
        """One agent's profile must not speak for another's config."""
        collector = _named("otelcol")
        other = _agent()
        verdict = config_validation.check_config_map(
            {"": {"body": COLLECTOR_CONFIG, "content_type": "text/yaml"}},
            agents=[collector, other],
            run_policy=False,
        )
        assert verdict.profile == "mixed"
        assert any("more than one profile" in w for w in verdict.warning_messages)

    def test_the_fingerprint_ignores_the_attribution(self):
        """A stored revision approved before #118 must stay appliable."""
        agent = _named("otelcol")
        with_profile = config_validation.check_config_map(
            {"": {"body": COLLECTOR_CONFIG, "content_type": "text/yaml"}},
            agents=[agent],
            run_policy=False,
        )
        without = config_validation.check_config_map(
            {"": {"body": COLLECTOR_CONFIG, "content_type": "text/yaml"}},
            run_policy=False,
        )
        assert with_profile.profile != without.profile
        assert with_profile.fingerprint() == without.fingerprint()


class TestRegistrySurface:
    def test_every_profile_has_a_unique_name(self):
        assert len(set(profiles.names())) == len(profiles.PROFILES)

    def test_get_and_exists(self):
        assert profiles.get("otelcol").name == "otelcol"
        assert profiles.get("OTELCOL") is not None  # forgiving of case/space
        assert profiles.get("  generic ") is not None
        assert profiles.get(None) is None
        assert profiles.get("nope") is None
        assert profiles.exists("otelcol")
        assert not profiles.exists("nope")

    def test_describe_is_json_safe_and_has_no_callables(self):
        described = profiles.describe()
        json.dumps(described)  # must not raise
        names = [entry["name"] for entry in described]
        assert names == list(profiles.names())
        otel = next(entry for entry in described if entry["name"] == "otelcol")
        assert otel["validates_structure"] is True
        assert otel["detectable"] is True
        assert otel["default_filename"] == "otelcol-config.yaml"

    def test_generic_cannot_be_inferred(self):
        """Only a pin or the fallback may select it — no matcher, no sniffer."""
        generic = profiles.get("generic")
        assert generic.identity_match_any == ()
        assert generic.sniff is None
        assert generic.config_formats == ()


class TestBootstrapSnippets:
    def test_otelcol_snippet_is_collector_yaml(self):
        from server import agent_auth

        snippet = agent_auth.collector_snippet("tok", agent_type="otelcol")
        assert snippet.startswith("extensions:\n  opamp:\n")
        assert 'Authorization: "Bearer tok"' in snippet

    def test_generic_snippet_is_plain_text_not_invented_yaml(self):
        """There is no such thing as a generic agent config file."""
        from server import agent_auth

        snippet = agent_auth.collector_snippet("tok", agent_type="generic")
        assert "extensions:" not in snippet
        assert "Authorization: Bearer tok" in snippet
        assert "/v1/opamp" in snippet
