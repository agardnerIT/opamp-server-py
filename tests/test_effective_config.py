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
    content_types,
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

    def test_msg_to_dict_spelling_of_content_type_is_read(self):
        """#118: agents report `contentType`, hand-written revisions say `content_type`.

        Reading one spelling was a silent, dangerous hole: a missing content type
        means "assume YAML", so a body declared `application/json` by an agent was
        parsed as YAML and skipped for a misleading reason.
        """
        camel = objects(
            _doc(**{"junk": {"body": "{}", "contentType": "application/json"}})
        )[0]
        assert camel.content_type == "application/json"
        assert "not YAML" in camel.skipped

        snake = objects(
            _doc(**{"junk": {"body": "{}", "content_type": "application/json"}})
        )[0]
        assert (camel.content_type, camel.skipped) == (snake.content_type, snake.skipped)

    def test_content_types_are_reported_without_parsing(self):
        """#118: identity inference needs the labels, not a parse."""
        document = _doc(
            **{
                "": {"body": _b64(CONFIG), "contentType": "text/yaml"},
                "raw": {"body": _b64(RAW), "content_type": "application/json; charset=utf-8"},
                "undeclared": {"body": _b64(RAW)},
            }
        )
        assert content_types(document) == ("text/yaml", "application/json")

    def test_content_types_is_empty_when_nothing_is_declared(self):
        assert content_types(_doc(**{"": CONFIG})) == ()
        assert content_types(None) == ()
        assert content_types("not json") == ()

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
        # #118: the cart went with the horse — component extraction (and therefore this
        # parse) moved from `server.state` into `server/profiles/otelcol.py`, which
        # calls the shared reader in `server.effective_config`. The assertion is
        # unchanged: one parse per reported config, whoever does the parsing.
        from server import effective_config as effective_config_module
        from server.state import AgentState

        calls = []
        original = effective_config_module.parse_effective_config

        def counting(document):
            calls.append(document)
            return original(document)

        monkeypatch.setattr(effective_config_module, "parse_effective_config", counting)

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

    def test_a_non_interpreting_profile_never_gets_a_pipeline_added(self):
        """#121: the augmentation is gated by the agent's *profile*, not by a shape.

        `_COLLECTOR_SECTIONS` made this module decide what a collector config is for
        every agent. The page now passes the resolved profile, so a `generic` agent's
        body — which this server does not interpret — is displayed exactly as it was
        reported: no injected `processors.batch`, no invented `traces` pipeline.
        """
        from server import profiles
        from ui.config_view import display_config_yaml

        rendered = display_config_yaml(
            json.dumps(_doc(collector=CONFIG)), profile=profiles.get("generic")
        )
        assert rendered is not None
        shown = yaml.safe_load(rendered)
        assert "processors" not in shown
        assert shown["service"] == yaml.safe_load(CONFIG)["service"]
        assert "telemetry" not in shown["service"]

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


#: What osquery reports as its effective config: `application/json`, a schedule, and
#: no `service`/`receivers` — i.e. not a collector config at all.
OSQUERY_CONFIG = json.dumps(
    {
        "schedule": {
            "battery_percentage": {
                "query": "SELECT percent_remaining FROM battery;",
                "interval": 30,
            }
        }
    }
)


def _osquery_doc():
    return _doc(
        **{
            "osquery.osquery.conf.json": {
                "body": _b64(OSQUERY_CONFIG),
                "content_type": "application/json",
            }
        }
    )


class TestAJsonAgentConfigIsReadable:
    """An osquery agent's config is JSON, and the panel used to show none of it.

    The server only read bodies the agent declared as YAML, so the "Configuration"
    panel said "No config object in this agent's report is readable as YAML" for an
    agent that was demonstrably running a schedule. Display opts in to JSON; the
    component/identity paths must not, or osquery looks like a collector profile.
    """

    def test_the_component_path_still_refuses_json(self):
        assert parse_effective_config(_osquery_doc()) == {}
        assert objects(_osquery_doc())[0].parsed is False

    def test_display_accepts_the_json_body(self):
        from ui.config_view import display_config_yaml

        rendered = display_config_yaml(json.dumps(_osquery_doc()))

        assert rendered is not None
        shown = yaml.safe_load(rendered)
        assert shown["schedule"]["battery_percentage"]["query"] == (
            "SELECT percent_remaining FROM battery;"
        )
        assert shown["schedule"]["battery_percentage"]["interval"] == 30

    def test_a_non_collector_config_is_not_rewritten_as_one(self):
        from ui.config_view import display_config_yaml

        shown = yaml.safe_load(display_config_yaml(json.dumps(_osquery_doc())))

        # The collector augmentation would invent these. Showing (and offering for
        # download) a config the agent never ran is worse than showing nothing.
        for invented in ("receivers", "processors", "exporters", "service", "extensions"):
            assert invented not in shown


#: The body an operator copied out of the UI before #131 was fixed: base64, because
#: that is how `MessageToDict` renders a protobuf `bytes` body. It is not a mistake
#: the operator made — it is what the panel showed them.
BATTERY_SCHEDULE = {
    "schedule": {
        "battery_stats": {
            "query": "SELECT percent_remaining FROM battery;",
            "interval": 32,
        }
    }
}


class TestTheEditorGetsDecodedText:
    """Prefill must hand the editor the document, never the base64 of it (#131).

    The prefill path read `configMap.configMap[*].body` straight off the report, so a
    proposal made from a prefilled box stored and delivered the base64 line as the
    agent's config. The decoded text is what an operator edits, and it is what the
    API stores — base64 exists only at the protobuf boundary.
    """

    def _report(self, **entries):
        return json.dumps(_doc(**entries))

    def _json_object(self, document):
        """A config object the agent declared as JSON, as the report renders it."""
        return {"body": _b64(document), "content_type": "application/json"}

    def test_the_body_is_the_document_not_the_base64(self):
        from ui.config_view import editable_config

        body = json.dumps(BATTERY_SCHEDULE)
        editable = editable_config(self._report(**{"": self._json_object(body)}))

        assert editable is not None
        assert editable.body == body
        assert "percent_remaining" in editable.body
        # The whole point: nothing that only decodes as base64 survives into the box.
        assert editable.body.strip().startswith("{")

    def test_the_declared_content_type_is_carried_into_the_editor(self):
        """A JSON body prefilled under `text/yaml` stores the wrong type (#118)."""
        from ui.config_view import EDITOR_CONTENT_TYPES, editable_config

        editable = editable_config(
            self._report(**{"": self._json_object(json.dumps(BATTERY_SCHEDULE))})
        )

        assert editable.content_type == "application/json"
        assert editable.content_type in EDITOR_CONTENT_TYPES

    def test_a_collector_body_still_prefills_as_yaml(self):
        from ui.config_view import editable_config

        editable = editable_config(self._report(collector=CONFIG))

        assert editable.content_type == "text/yaml"
        assert "opamp" in yaml.safe_load(editable.body)["extensions"]

    def test_the_object_shown_is_the_object_edited(self):
        """The panel and the editor agree, or an operator edits a config they cannot see."""
        from ui.config_view import editable_config

        editable = editable_config(self._report(**{"": CONFIG, "raw": RAW}))

        assert editable.key == ""
        assert yaml.safe_load(editable.body)["service"]["extensions"] == ["opamp"]
        # The other object is named, not silently dropped.
        assert [key for key, _ in editable.others] == ["raw"]

    def test_nothing_parseable_means_nothing_to_prefill(self):
        from ui.config_view import editable_config

        assert editable_config(self._report(**{"": "nope: [["})) is None
        assert editable_config("") is None
        assert editable_config("not json") is None
        assert editable_config(None) is None

    def test_a_dict_report_is_accepted(self):
        """`GET /agent/{id}` hands back a string, but the fake and JSON paths a dict."""
        from ui.config_view import editable_config

        editable = editable_config(
            _doc(**{"": self._json_object(json.dumps(BATTERY_SCHEDULE))})
        )
        assert editable is not None and "percent_remaining" in editable.body
