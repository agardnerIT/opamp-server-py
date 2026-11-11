"""The profile dictionary: schema, loader and the generation counter (#134).

Four claims are being pinned here, and the first is the issue's whole acceptance
criterion:

1. **A new agent type is one YAML file and a restart.** `test_a_new_type_is_one_file`
   drops a file for a product this code has never heard of into a tmpdir and requires
   `resolve()` to find it — no Python, no release.
2. **A bad file cannot take the server down, and cannot pass unnoticed either.** One
   document is logged and absent (the fleet is still served); `strict=True` raises, and
   that is what `--strict-profiles` / `OPAMP_PROFILES_STRICT` wire to CI.
3. **Match order is data.** `priority`, high to low, with a tie as a load error — never
   filename order, because a locale or a rename would then change fleet behaviour
   silently.
4. **A reload invalidates cached resolutions.** Without the generation counter,
   `resolve_cached()` keeps serving decisions made against the old dictionary and the
   only symptom is "the profile did not take effect until a restart".

The rest is negative testing: every field that could go wrong gets a file that goes
wrong, because a dictionary that silently ignores a key it does not understand is a
dictionary that lies about what it controls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import profiles
from server.profiles import loader, schema
from server.state import AgentState

SHIPPED = Path(profiles.DEFAULT_DIRECTORY)


# ------------------------------------------------------------------ helpers ---


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


MINIMAL = """
name: {name}
display_name: {display_name}
priority: {priority}
"""


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(body)
    return path


@pytest.fixture()
def dictionary(tmp_path, monkeypatch):
    """An empty dictionary directory. Yields the path; the registry is restored after."""
    monkeypatch.delenv("OPAMP_PROFILES_STRICT", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _restore_dictionary(monkeypatch):
    """`registry.reload()` is process-global, so a reloading test must put it back.

    Otherwise "a reload empties the dictionary" leaks into every later test in the
    run — the same class of cross-test contamination that made `set_agent_type` tests
    order-dependent in `test_agent_types.py`.
    """
    generation = profiles.GENERATION
    yield
    if profiles.GENERATION != generation:
        monkeypatch.undo()
        profiles.reload(strict=True)


# -------------------------------------------------------------------- schema ---


class TestTheSchemaIsStrict:
    def test_a_minimal_document_loads(self, dictionary):
        _write(dictionary, "tiny", MINIMAL.format(name="tiny", display_name="Tiny", priority=1))
        loaded = loader.load_all(dictionary, strict=True)
        assert [profile.name for profile in loaded] == ["tiny"]

    def test_an_unknown_key_is_a_load_error(self, dictionary):
        """A typo'd field that is silently ignored is a lie about what it controls."""
        _write(
            dictionary,
            "typo",
            MINIMAL.format(name="typo", display_name="Typo", priority=1) + "icons: '🧩'\n",
        )
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "icons" in str(caught.value)

    def test_the_old_hooks_key_does_not_exist(self, dictionary):
        """Guardrail 3: no code from data, so there is no key that names a module."""
        assert "hooks" not in schema.ProfileDocument.model_fields

    def test_structural_validation_requires_a_components_block(self, dictionary):
        """Guardrail 4 — the one place a data file could make the server less safe."""
        _write(
            dictionary,
            "claims",
            MINIMAL.format(name="claims", display_name="Claims", priority=1)
            + "validates_structure: true\n",
        )
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "components" in str(caught.value)

    @pytest.mark.parametrize(
        "block",
        [
            "identity:\n  match_any:\n    - attr: service.name\n",  # neither prefix nor equals
            "identity:\n  match_any:\n    - attr: service.name\n      prefix: [a]\n      equals: [a]\n",
            "identity:\n  sniff: {}\n",  # an empty sniffer is dead weight
            "reports: [invented]\n",
            "telemetry: {}\n",
            "panels:\n  - id: p\n    widget: graph\n",
            "panels:\n  - id: p\n    widget: metrics\n",  # no metrics
            "panels:\n  - id: p\n    widget: alert\n    metric: m\n    when: above_zero\n"
            "    severity: error\n",  # no message
            "config_formats:\n  - distinctive: true\n",  # claims a type it cannot name
            "snippet: 'no placeholders here'\n",
            "snippet: 'endpoint: {endpoint} token: {token} oops: {nope}'\n",
        ],
        ids=[
            "identity-rule-empty",
            "identity-rule-both",
            "sniff-empty",
            "unknown-report",
            "telemetry-empty",
            "unknown-widget",
            "metrics-without-metrics",
            "alert-without-message",
            "distinctive-without-type",
            "snippet-without-fields",
            "snippet-unknown-placeholder",
        ],
    )
    def test_a_broken_field_is_a_load_error(self, dictionary, block):
        _write(
            dictionary,
            "broken",
            MINIMAL.format(name="broken", display_name="Broken", priority=1) + block,
        )
        with pytest.raises(loader.ProfileLoadError):
            loader.load_all(dictionary, strict=True)

    def test_a_name_must_be_a_boring_key(self, dictionary):
        """`name` is stored in `agents.agent_type` and typed by operators."""
        _write(dictionary, "loud", MINIMAL.format(name="OTelCol", display_name="X", priority=1))
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "lower case" in str(caught.value)

    def test_a_duplicate_panel_id_is_refused(self, dictionary):
        _write(
            dictionary,
            "panels",
            MINIMAL.format(name="panels", display_name="Panels", priority=1)
            + "panels:\n"
            "  - id: same\n    widget: metrics\n    metrics: [{label: A, metric: a}]\n"
            "  - id: same\n    widget: metrics\n    metrics: [{label: B, metric: b}]\n",
        )
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "duplicate panel id" in str(caught.value)


# -------------------------------------------------------------------- loader ---


class TestTheShippedDictionary:
    def test_otelcol_is_data_and_has_no_module_any_more(self):
        """Acceptance: the collector is expressible as YAML, with no Python hooks."""
        assert (SHIPPED / "otelcol.yaml").is_file()
        assert not (Path(profiles.__file__).parent / "otelcol.py").exists()
        with pytest.raises(ImportError):
            __import__("server.profiles.otelcol")

    def test_otelcol_keeps_everything_it_had(self):
        otelcol = profiles.get("otelcol")
        assert otelcol.icon == "🧩"
        assert otelcol.priority == 100
        assert otelcol.validates_structure is True
        assert otelcol.reports == ("fleet", "agent", "heavy", "outdated")
        assert otelcol.telemetry.metric_prefix == "otelcol_"
        assert otelcol.filename_for("text/yaml") == "otelcol-config.yaml"
        # #134 moved the snippet into a template; the bytes must not change.
        assert otelcol.bootstrap_snippet("http://h/v1/opamp", "tok") == (
            "extensions:\n"
            "  opamp:\n"
            "    server:\n"
            "      http:\n"
            "        endpoint: http://h/v1/opamp\n"
            "        headers:\n"
            '          Authorization: "Bearer tok"\n'
        )

    def test_the_snippet_scheme_follows_the_endpoint(self):
        """`{scheme}` is the one substitution with a rule behind it, so it is tested."""
        otelcol = profiles.get("otelcol")
        websocket = otelcol.bootstrap_snippet("ws://h/v1/opamp", "tok")
        assert "      ws:\n" in websocket
        assert "      http:\n" not in websocket

    def test_generic_is_built_in_not_a_file(self):
        """A fallback that can fail to load is not a fallback."""
        assert profiles.get("generic") is not None
        assert not (SHIPPED / "generic.yaml").exists()

    def test_the_fallback_profile_never_wins_a_match(self):
        generic = profiles.get("generic")
        assert generic.identity_match_any == ()
        assert generic.sniff is None
        assert generic.config_formats == ()

    def test_only_one_profile_claims_the_bootstrap_default(self):
        claiming = [profile.name for profile in profiles.PROFILES if profile.bootstrap_default]
        assert claiming == ["otelcol"]

    def test_the_dictionary_order_is_priority_not_filename(self):
        assert [profile.priority for profile in profiles.PROFILES] == sorted(
            (profile.priority for profile in profiles.PROFILES), reverse=True
        )


class TestAddingATypeIsOneFile:
    """Acceptance criterion 3 — the reason #134 exists."""

    def test_a_new_type_is_one_file(self, dictionary, monkeypatch):
        _write(
            dictionary,
            "fluentbit",
            """
name: fluentbit
display_name: Fluent Bit
icon: "🪵"
priority: 50
summary: A log processor that speaks OpAMP.
config_formats:
  - content_types: ["text/yaml"]
    default_filename: fluent-bit.conf
identity:
  match_any:
    - attr: service.name
      prefix: ["fluent-bit", "fluentbit"]
  sniff:
    config_sections_any: [service, pipeline, input, output]
telemetry: null
panels: []
snippet: |
  service:
    {scheme}:
      endpoint: {endpoint}
      headers: {{Authorization: "Bearer {token}"}}
""",
        )
        # The shipped file is untouched: a new type is an *addition*, so the test
        # copies the collector in rather than stubbing the registry — this is the
        # real reload path an operator would take, not a monkeypatched one.
        (dictionary / "otelcol.yaml").write_text((SHIPPED / "otelcol.yaml").read_text())
        profiles.reload(dictionary, strict=True)

        resolution = profiles.resolve(_named("fluent-bit"))
        assert (resolution.name, resolution.source) == ("fluentbit", "declared")
        # Priority still decides: 100 (otelcol) beats 50 (fluentbit) for its own name.
        assert profiles.resolve(_named("otelcol")).name == "otelcol"
        # The new type's own declaration wins on its own attributes...
        assert profiles.get("fluentbit").icon == "🪵"
        assert profiles.get("fluentbit").filename_for("text/yaml") == "fluent-bit.conf"
        # ...and it declares no telemetry, so the UI is told "not expected", not "empty".
        assert profiles.get("fluentbit").telemetry is None


class TestABadFileDoesNotTakeTheServerDown:
    def test_one_bad_file_leaves_the_others_and_the_fleet_served(self, dictionary):
        _write(dictionary, "good", MINIMAL.format(name="good", display_name="Good", priority=10))
        _write(dictionary, "bad", "name: [unclosed\n")
        loaded = loader.load_all(dictionary)  # non-strict: the default
        assert [profile.name for profile in loaded] == ["good"]

    def test_strict_turns_the_same_problem_fatal(self, dictionary):
        _write(dictionary, "bad", "name: [unclosed\n")
        with pytest.raises(loader.ProfileLoadError):
            loader.load_all(dictionary, strict=True)

    def test_the_env_var_is_the_flag(self, dictionary, monkeypatch):
        _write(dictionary, "bad", "nonsense: true\n")
        monkeypatch.setenv("OPAMP_PROFILES_STRICT", "true")
        with pytest.raises(loader.ProfileLoadError):
            loader.load_all(dictionary)

    def test_an_empty_directory_is_reported_rather_than_silently_empty(self, dictionary):
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "no usable profile documents" in str(caught.value)


class TestCrossFileRules:
    def test_a_priority_tie_is_a_load_error_not_a_coin_flip(self, dictionary):
        _write(dictionary, "one", MINIMAL.format(name="one", display_name="One", priority=5))
        _write(dictionary, "two", MINIMAL.format(name="two", display_name="Two", priority=5))
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "priority 5" in str(caught.value)

    def test_two_profiles_may_not_own_the_same_mime_type(self, dictionary):
        body = MINIMAL.format(name="{name}", display_name="X", priority="{priority}") + (
            "config_formats:\n"
            "  - content_types: [application/x-shared]\n"
            "    distinctive: true\n"
        )
        _write(dictionary, "a", body.format(name="a", priority=2))
        _write(dictionary, "b", body.format(name="b", priority=1))
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "distinctive content type" in str(caught.value)

    def test_generic_may_not_be_redefined_by_a_file(self, dictionary):
        _write(dictionary, "generic", MINIMAL.format(name="generic", display_name="G", priority=1))
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "fallback" in str(caught.value)

    def test_two_bootstrap_defaults_are_refused(self, dictionary):
        body = MINIMAL.format(name="{name}", display_name="X", priority="{priority}")
        _write(dictionary, "a", body.format(name="a", priority=2) + "bootstrap_default: true\n")
        _write(dictionary, "b", body.format(name="b", priority=1) + "bootstrap_default: true\n")
        with pytest.raises(loader.ProfileLoadError) as caught:
            loader.load_all(dictionary, strict=True)
        assert "bootstrap_default" in str(caught.value)

    def test_an_ignored_suffix_is_not_a_profile(self, dictionary):
        _write(dictionary, "good", MINIMAL.format(name="good", display_name="G", priority=1))
        (dictionary / "notes.example.yaml").write_text("this: is not a profile\n")
        (dictionary / "README.md").write_text("neither is this\n")
        assert [profile.name for profile in loader.load_all(dictionary, strict=True)] == ["good"]


# ---------------------------------------------------------------- generation ---


class TestReloadInvalidatesResolution:
    """Acceptance: `resolve_cached()` may not serve a decision from the old dictionary."""

    def test_a_reload_bumps_the_generation(self, monkeypatch):
        before = profiles.GENERATION
        monkeypatch.setattr(
            "server.profiles.loader.load_all", lambda *a, **k: (profiles.get("otelcol"),)
        )
        profiles.reload(strict=True)
        assert profiles.GENERATION == before + 1
        assert profiles.names() == ("otelcol", "generic")

    def test_a_cached_resolution_is_dropped_by_a_reload(self, monkeypatch):
        agent = _named("otelcol")
        assert agent.profile_resolution().name == "otelcol"

        # A dictionary without the collector profile: the agent must stop being one.
        monkeypatch.setattr(
            "server.profiles.loader.load_all", lambda *a, **k: ()
        )
        profiles.reload(strict=True)

        resolution = agent.profile_resolution()
        assert (resolution.name, resolution.source) == ("generic", "fallback")

    def test_the_generation_is_part_of_the_fingerprint(self):
        from server.profiles import registry

        agent = _named("otelcol")
        before = registry._fingerprint(agent)
        assert before[0] == registry.generation()


# ------------------------------------------------------------------ describe ---


class TestTheAPIShapeCarriesTheDictionary:
    def test_describe_has_the_fields_134_added(self):
        entry = next(item for item in profiles.describe() if item["name"] == "otelcol")
        assert entry["icon"] == "🧩"
        assert entry["priority"] == 100
        assert entry["identity"]["match_any"][0]["attr"] == "service.name"
        assert entry["identity"]["sniff"]["config_sections_any"]
        assert entry["telemetry"] == {"metric_prefix": "otelcol_"}
        assert [panel["id"] for panel in entry["panels"]] == [
            "data-flow-inputs",
            "data-flow-inputs-failed",
            "data-flow-outputs",
            "collector-health",
        ]
        json.dumps(entry)  # must stay JSON-safe: no callables, no specs objects

    def test_panel_metrics_keep_their_format(self):
        entry = next(item for item in profiles.describe() if item["name"] == "otelcol")
        health = next(panel for panel in entry["panels"] if panel["id"] == "collector-health")
        formats = {metric["label"]: metric["format"] for metric in health["metrics"]}
        assert formats["CPU (sec)"] == "decimal"
        assert formats["Memory (MB)"] == "mebibytes"

    def test_every_panel_metric_names_a_collector_metric(self):
        """The panels are data, so a typo'd metric name is a silently blank tile."""
        entry = next(item for item in profiles.describe() if item["name"] == "otelcol")
        for panel in entry["panels"]:
            for metric in panel.get("metrics", []):
                assert metric["metric"].startswith("otelcol_"), metric

    def test_generic_promises_nothing(self):
        entry = next(item for item in profiles.describe() if item["name"] == "generic")
        assert entry["panels"] == []
        assert entry["telemetry"] is None
        assert entry["reports_telemetry"] is False
        assert entry["identity"] == {"match_any": [], "sniff": None}
        assert entry["detectable"] is False


# ---------------------------------------------------------------- ambiguity ---


class TestAmbiguityIsVisible:
    """#134's risk section: after this, a wrong profile is a file someone dropped in."""

    def test_two_matching_profiles_are_both_reported(self, monkeypatch):
        from server.profiles import registry

        # A second profile that claims the same attribute: the honest answer is "either".
        import dataclasses

        twin = dataclasses.replace(profiles.get("otelcol"), name="twin", priority=1)
        monkeypatch.setattr(registry, "PROFILES", (profiles.get("otelcol"), twin, profiles.FALLBACK))

        agent = _named("otelcol")
        assert profiles.candidates(agent) == ("otelcol", "twin")
        info = agent.type_info()
        assert info["agent_type"] == "otelcol"  # highest priority still wins
        assert info["agent_type_ambiguous"] == ["otelcol", "twin"]

    def test_one_match_is_not_ambiguous(self):
        info = _named("otelcol").type_info()
        assert info["agent_type_ambiguous"] is None

    def test_a_pin_is_never_ambiguous(self):
        """An operator's decision outranks the evidence, so there is nothing to warn about."""
        agent = _named("otelcol")
        agent.agent_type = "generic"
        info = agent.type_info()
        assert info["agent_type_source"] == "pinned"
        assert info["agent_type_ambiguous"] is None

    def test_the_read_surface_carries_the_key(self):
        assert "agent_type_ambiguous" in _agent().to_dict()
