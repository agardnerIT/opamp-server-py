"""Effective-config reads cover every config object, not just `""` (#68).

Config-map keys are agent-implementation-defined and `""` has no privileged meaning
(opamp-spec v0.20.0 §Configuration Objects). The old code read `configMap[""]` only,
so `otelcol-contrib` with `reports_raw_config: true` (which also reports a `"raw"`
object) or any agent using `"collector"` silently reported zero components — and
statically disabled the OCB manifest builder and the heavy/outdated reports.

Every case below is one of the issue's acceptance criteria.
"""

import base64
import json

import yaml

from server import effective_config
from server.effective_config import (
    parse_effective_config,
    preferred_document,
    objects,
)

CONFIG = """
receivers:
  otlp:
    protocols:
      grpc:
exporters:
  debug:
extensions:
  opamp:
service:
  extensions: [opamp]
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
"""

RAW = """
receivers:
  otlp:
    protocols:
      grpc:
exporters:
  debug:
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
"""


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _doc(**entries):
    """`{key: body}` (or `{key: full entry dict}`) -> an `EffectiveConfig` document."""
    config_map = {}
    for key, value in entries.items():
        config_map[key] = value if isinstance(value, dict) else {"body": _b64(value)}
    return {"configMap": {"configMap": config_map}}


class TestAllObjectsAreRead:
    def test_expanded_plus_raw_objects_detect_components_once(self):
        """otelcol-contrib with `reports_raw_config: true`: keys `""` and `"raw"`."""
        result = parse_effective_config(_doc(**{"": CONFIG, "raw": RAW}))

        assert result["receiver"] == {"otlp"}
        assert result["exporter"] == {"debug"}
        # `opamp` appears only in the expanded object; a "first object wins" read
        # would still find it, but a per-object accumulation without a union would
        # hand back duplicates — these are sets, so there is exactly one of each.
        assert result["extension"] == {"opamp"}

    def test_a_non_empty_key_is_detected(self):
        """The bug: any agent that does not use `""` reported nothing at all."""
        result = parse_effective_config(_doc(collector=CONFIG))

        assert result["receiver"] == {"otlp"}
        assert result["exporter"] == {"debug"}
        assert result["extension"] == {"opamp"}
        assert "processor" not in result  # only types that actually appear

    def test_partial_objects_are_unioned(self):
        """An object need not be a whole config: fragments contribute too."""
        fragment = "service:\n  pipelines:\n    metrics:\n      receivers: [prometheus]\n"
        result = parse_effective_config(_doc(**{"": CONFIG, "metrics-fragment": fragment}))

        assert result["receiver"] == {"otlp", "prometheus"}
        assert result["exporter"] == {"debug"}

    def test_withdraw_text_bodies_and_bytes_are_both_accepted(self):
        as_bytes = {"configMap": {"configMap": {"": {"body": CONFIG.encode()}}}}
        assert parse_effective_config(as_bytes) == parse_effective_config(_doc(**{"": CONFIG}))


class TestSkippingNotCrashing:
    def test_json_content_type_is_skipped(self):
        result = parse_effective_config(
            _doc(**{"": CONFIG, "junk": {"body": '{"not": "yaml config"}', "content_type": "application/json"}})
        )
        assert result["receiver"] == {"otlp"}
        skipped = {obj.key: obj.skipped for obj in objects(_doc(**{
            "junk": {"body": "{}", "content_type": "application/json"},
        }))}
        assert "not YAML" in skipped["junk"]

    def test_yaml_content_type_with_charset_is_accepted(self):
        result = parse_effective_config(
            _doc(**{"": {"body": _b64(CONFIG), "content_type": "text/yaml; charset=utf-8"}})
        )
        assert result["receiver"] == {"otlp"}

    def test_body_that_is_not_yaml_is_skipped(self):
        result = parse_effective_config(_doc(**{"": "not: valid: yaml: [[[", "raw": RAW}))
        # The unparseable object does not take the readable one down with it.
        assert result["receiver"] == {"otlp"}
        assert result["exporter"] == {"debug"}

    def test_non_utf8_body_is_skipped(self):
        document = {"configMap": {"configMap": {"": {"body": b"\xff\xfe\x00raw"}}}}
        assert parse_effective_config(document) == {}

    def test_a_yaml_list_is_not_a_config(self):
        assert parse_effective_config(_doc(**{"": "- just\n- a\n- list\n"})) == {}

    def test_empty_and_missing_maps_are_empty_results(self):
        assert parse_effective_config({}) == {}
        assert parse_effective_config({"configMap": {}}) == {}
        assert parse_effective_config({"configMap": {"configMap": {}}}) == {}
        assert parse_effective_config({"configMap": {"configMap": {"": {}}}}) == {}
        assert parse_effective_config(None) == {}

    def test_unhashable_component_ids_do_not_raise(self):
        result = parse_effective_config(
            _doc(**{"": "service:\n  pipelines:\n    traces:\n      receivers:\n        - {a: b}\n        - otlp\n"})
        )
        assert result["receiver"] == {"otlp"}

    def test_duplicate_keys_are_refused_rather_than_silently_last_wins(self):
        duplicated = "service:\n  pipelines: {}\n  pipelines: {}\n"
        assert parse_effective_config(_doc(**{"": duplicated})) == {}


class TestPreferredObject:
    def test_empty_key_is_preferred(self):
        chosen = preferred_document(_doc(**{"": CONFIG, "raw": RAW}))
        assert chosen["service"]["extensions"] == ["opamp"]

    def test_falls_back_to_the_first_parseable_object(self):
        chosen = preferred_document(_doc(**{"collector": CONFIG, "raw": "nope: [["}))
        assert chosen["receivers"] == {"otlp": {"protocols": {"grpc": None}}}

    def test_a_role_tagged_object_yields_to_an_untagged_one(self):
        """`role` marks an alternative representation (the raw config), per spec."""
        tagged = {"body": _b64(RAW), "content_type": "text/yaml", "role": "raw"}
        chosen = preferred_document(_doc(**{"aaa-raw": tagged, "collector": CONFIG}))
        assert chosen["service"]["extensions"] == ["opamp"]  # the untagged object

    def test_the_empty_key_still_wins_when_role_tagged(self):
        tagged = {"body": _b64(CONFIG), "content_type": "text/yaml", "role": "expanded"}
        chosen = preferred_document(_doc(**{"": tagged, "raw": RAW}))
        assert chosen["service"]["extensions"] == ["opamp"]

    def test_no_parseable_object_means_no_document(self):
        assert preferred_document({"configMap": {"configMap": {"": {"body": ""}}}}) is None
        assert preferred_document(None) is None


class TestAgentStateComponents:
    def test_two_object_config_marks_components_used_without_duplicates(self):
        """`GET /agent/{id}` → `components`: one entry per component, marked used."""
        from server.state import AgentState

        state = AgentState(instance_uid=b"\x00" * 16, agent_id="a" * 32)
        state.effective_config = json.dumps(_doc(**{"": CONFIG, "raw": RAW}))
        state.available_components = {
            "components": {
                "receivers": {
                    "subComponentMap": {
                        "otlp": {"metadata": []},
                        "unused_receiver": {"metadata": []},
                    }
                },
                "exporters": {"subComponentMap": {"debug": {"metadata": []}}},
            }
        }

        components = state.components
        ids = [component["id"] for component in components["receiver"]]
        assert ids.count("otlp") == 1, ids  # union, not one entry per object
        assert {c["id"]: c["used"] for c in components["receiver"]} == {
            "otlp": True,
            "unused_receiver": False,
        }
        assert components["exporter"] == [{"id": "debug", "version": "", "used": True}]

    def test_components_are_detected_from_a_non_empty_key(self):
        from server.state import AgentState

        state = AgentState(instance_uid=b"\x00" * 16, agent_id="b" * 32)
        state.effective_config = json.dumps(_doc(collector=CONFIG))
        state.available_components = {
            "components": {"receivers": {"subComponentMap": {"otlp": {"metadata": []}}}}
        }

        assert state.components["receiver"] == [{"id": "otlp", "version": "", "used": True}]


class TestComponentReadCost:
    def test_usage_is_parsed_once_per_reported_config(self, monkeypatch):
        """`GET /agents` walks every agent; reading the map must not re-parse each time."""
        from server import state as state_module
        from server.state import AgentState

        calls = []
        original = state_module.parse_effective_config

        def counting(document):
            calls.append(document)
            return original(document)

        monkeypatch.setattr(state_module, "parse_effective_config", counting)

        agent = AgentState(instance_uid=b"\x00" * 16, agent_id="c" * 32)
        agent.effective_config = json.dumps(_doc(collector=CONFIG))
        agent.components
        agent.components
        agent.to_dict()
        assert len(calls) == 1

        # A new report invalidates the cache, and only a new report does.
        agent.effective_config = json.dumps(_doc(**{"": CONFIG, "raw": RAW}))
        agent.components
        assert len(calls) == 2


class TestUiCanRenderANonEmptyKey:
    def test_display_yaml_uses_the_collector_object(self):
        """The panel used to read `configMap[""]` inline, twice — so it showed nothing."""
        from ui.config_view import display_config_yaml

        rendered = display_config_yaml(json.dumps(_doc(collector=CONFIG)))
        assert rendered is not None
        assert "otlp" in rendered
        # The display adds the telemetry pipeline the dashboard would configure.
        assert "telemetry:" in rendered

    def test_display_yaml_prefers_the_expanded_object(self):
        from ui.config_view import display_config_yaml

        rendered = display_config_yaml(json.dumps(_doc(**{"": CONFIG, "raw": RAW})))
        shown = yaml.safe_load(rendered)
        # If the `raw` object had won, there would be no `extensions` section at all.
        assert shown["service"]["extensions"] == ["opamp"]

    def test_display_yaml_is_none_when_nothing_parses(self):
        from ui.config_view import display_config_yaml

        assert display_config_yaml(json.dumps(_doc(**{"": "nope: [["}))) is None
        assert display_config_yaml("") is None
        assert display_config_yaml("not json") is None


class TestNoDriftBetweenCallers:
    def test_state_re_exports_the_shared_function(self):
        """`from server.state import parse_effective_config` must be the same object."""
        from server.state import parse_effective_config as from_state

        assert from_state is effective_config.parse_effective_config

    def test_the_old_hardcoded_lookup_is_gone(self):
        """Guard against the empty-key lookup being re-inlined somewhere."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in list(root.glob("server/*.py")) + list(root.glob("ui/**/*.py")):
            if path.name == "effective_config.py":
                continue
            text = path.read_text()
            if '.get("", {})' in text or "[''][\"body\"]" in text:
                offenders.append(path.name)
        assert offenders == []
