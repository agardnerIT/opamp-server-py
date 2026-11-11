"""A deterministic capture of the agent-detail screen, for #121's refactor fence.

Why this exists
---------------
`tests/test_profiles.py::test_the_move_changed_no_output` proved #118 was a pure move
by byte-comparing every profile's output against a fixture. #121 restructures
`ui/pages/1_Agents.py` and its own acceptance criterion is "otelcol renders unchanged
after the move" — and nothing in the tree could check that. `tests/test_ui_smoke.py`
proves pages *run*, not that a panel kept its wording, its metrics or its buttons.

So this module records what the detail screen actually renders — elements in render
order, grouped by kind: text, metrics, tables, buttons, code — for two agents:

    otelcol   a collector: internal telemetry arrives, components are reported, and
              the twelve hardcoded `otelcol_*` reads light up.
    generic   a non-collector: no telemetry, no components, one effective config.
              This is the case #121 fixed, so the capture changed once, on purpose:
              before Phase 3 it recorded a `collector-config.yaml` download and OTel
              wording on an agent that is not a collector; what it records now is the
              *absence* of both, plus the profile's own sentence about telemetry.

`otelcol` is the fence: its half of `tests/fixtures/agent_detail_golden.json` stayed
byte-identical across the move, which is "otelcol renders unchanged" made checkable.
The `generic` half changed once, in Phase 3, and the diff was the review.

What it is not
--------------
Not pixels and not layout: no column order, no widths, no theme. A capture that
asserted those would fail on every Streamlit upgrade and be relaxed until it proved
nothing. What it does assert is the text an operator reads and the values the panels
show — which is what a bad refactor changes.

Regenerate (only from a tree you believe):

    python -m tests.ui_golden > tests/fixtures/agent_detail_golden.json

Requires the `[ui]` extra; `tests/test_ui_golden.py` skips without it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

AppTest = pytest.importorskip(
    "streamlit.testing.v1", reason="streamlit is the optional [ui] extra"
).AppTest

import requests

from tests.test_ui_smoke import FakeResponse, FakeServer, fake_agent

REPO = Path(__file__).resolve().parent.parent
AGENTS_PAGE = REPO / "ui" / "pages" / "1_Agents.py"
FIXTURE = REPO / "tests" / "fixtures" / "agent_detail_golden.json"

AGENT_ID = "ab" * 16

#: The one timestamp in the capture is fixed and non-empty, so the "Last updated"
#: caption is asserted but the local-time rendering of the heartbeat is not: that
#: string depends on the reader's timezone and would rot the fixture per machine.
LAST_UPDATED = "2026-01-01T00:00:00+00:00"

OTELCOL_METRICS = {
    "otelcol_receiver_accepted_log_records": 1200,
    "otelcol_receiver_accepted_spans": 340,
    "otelcol_receiver_accepted_metric_points": 56,
    "otelcol_receiver_refused_log_records": 0,
    "otelcol_receiver_failed_log_records": 0,
    "otelcol_exporter_sent_log_records": 1199,
    "otelcol_exporter_sent_spans": 340,
    "otelcol_exporter_sent_metric_points": 56,
    "otelcol_process_cpu_seconds": 12.5,
    "otelcol_process_memory_rss": 314572800,
    "otelcol_process_runtime_heap_alloc_bytes": 209715200,
    "otelcol_process_uptime": 3600,
}

#: Singular keys: that is what the server serialises, and what the page's
#: `COMPONENT_ORDER` loops over (a plural fixture renders "No components reported"
#: beside a "3/4 in use" metric — the kind of fake-vs-real gap a golden must not have).
OTELCOL_COMPONENTS = {
    "receiver": [{"id": "otlp", "used": True, "version": "v0.1"}],
    "processor": [{"id": "batch", "used": True, "version": "v0.1"}],
    "exporter": [
        {"id": "debug", "used": True, "version": "v0.1"},
        {"id": "otlphttp", "used": False, "version": "v0.1"},
    ],
}

GENERIC_COMPONENTS: dict = {}

#: A one-pipeline collector config, in the shape `display_config_yaml` accepts.
EFFECTIVE_CONFIG = {
    "configMap": {
        "configMap": {
            "": {
                "contentType": "text/yaml",
                "body": (
                    "receivers:\n  otlp:\n    protocols:\n      grpc: {}\n"
                    "exporters:\n  debug: {}\nservice:\n  pipelines:\n"
                    "    traces:\n      receivers: [otlp]\n      exporters: [debug]\n"
                ),
            }
        }
    }
}


class _GoldenServer(FakeServer):
    """The smoke test's fake, plus the two fields its detail view left empty.

    `metrics` and `components` are what separate an otelcol panel from a generic one,
    so they are the knobs this capture turns — the rest of the fake is reused rather
    than copied, because a second fake would be a second thing to keep true.
    """

    def __init__(
        self, metrics, components, effective_config, description=None, agent_type="generic"
    ):
        super().__init__()
        self.detail_metrics = metrics
        self.detail_components = components
        self.detail_effective_config = effective_config
        self.detail_description = description
        self.detail_agent_type = agent_type

    def _agent_detail(self, url):
        agent_id = url.rsplit("/", 1)[-1].split("?")[0]
        detail = fake_agent(agent_id)
        if self.detail_description is not None:
            detail["description"] = self.detail_description
        # The badge (and, today, the icon in `ui/shared.py:913`) is driven by these two,
        # so the capture has to carry them or the OTel special case is invisible to it.
        detail["agent_type"] = self.detail_agent_type
        detail["agent_type_source"] = "sniffed"
        detail.update(
            {
                "effective_config": self.detail_effective_config,
                "metrics": {"metrics": self.detail_metrics, "updated_at": LAST_UPDATED}
                if self.detail_metrics
                else {},
                "components": self.detail_components,
                "capability_tags": [],
                "custom_capabilities": [],
                # Not "now": a heartbeat renders in the reader's timezone, which would
                # make this fixture machine-specific for no gain.
                "last_heartbeat": None,
            }
        )
        return FakeResponse(200, detail)


def _description(service_name: str) -> dict:
    return {
        "identifyingAttributes": [
            {"key": "service.name", "value": {"stringValue": service_name}}
        ],
        "nonIdentifyingAttributes": [
            {"key": "deployment.environment", "value": {"stringValue": "production"}}
        ],
    }


def _server(case: str) -> _GoldenServer:
    if case == "otelcol":
        return _GoldenServer(
            metrics=OTELCOL_METRICS,
            components=OTELCOL_COMPONENTS,
            effective_config=None,
            description=_description("otelcol"),
            agent_type="otelcol",
        )
    if case == "generic":
        # A YAML-only, non-collector agent: it reports an effective config and nothing
        # else. Exactly the profile #121 says must stop being offered collector chrome.
        return _GoldenServer(
            metrics={},
            components=GENERIC_COMPONENTS,
            effective_config=EFFECTIVE_CONFIG,
            description=_description("fluent-bit"),
            agent_type="generic",
        )
    raise AssertionError(f"unknown capture case {case!r}")


#: `file_name`s offered by `st.download_button`, in render order. The proto that
#: `AppTest` exposes has no `file_name` field, and the hardcoded
#: `collector-config.yaml` (#121) is exactly the thing this fence has to see, so the
#: file name is recorded at the call site instead of being lost.
_download_names: list = []


@contextmanager
def _transport(server):
    """Point every `requests` verb the page can reach at the fake."""
    import streamlit

    _download_names.clear()
    real_download_button = streamlit.download_button

    def recording_download_button(*args, **kwargs):
        _download_names.append(str(kwargs.get("file_name") or ""))
        return real_download_button(*args, **kwargs)

    with mock.patch.object(requests, "get", server.get), mock.patch.object(
        requests, "post", server.post
    ), mock.patch.object(requests, "delete", server.delete), mock.patch.object(
        requests, "request", server.request
    ), mock.patch.object(
        requests, "put", server.request
    ), mock.patch.object(
        streamlit, "download_button", recording_download_button
    ):
        yield server


_TEXT_KINDS = (
    "title",
    "header",
    "subheader",
    "markdown",
    "caption",
    "info",
    "success",
    "warning",
    "error",
)


def _rows(value) -> list:
    """A dataframe as JSON-safe lists of strings, so a diff reads as a table."""
    columns = [str(column) for column in value.columns]
    return [columns] + [
        [str(cell) for cell in row] for row in value.itertuples(index=False, name=None)
    ]


def elements(app) -> dict:
    """Everything the page rendered, grouped by kind.

    Grouped rather than interleaved on purpose: Streamlit's own ordering depends on
    container structure, and this capture is about what was said, not where. Both are
    in `app.main` order inside each kind, so a reordering of two panels still shows.
    """
    captured: dict = {}

    for kind in _TEXT_KINDS:
        values = [str(element.value) for element in getattr(app, kind)]
        if values:
            captured[kind] = values

    code = []
    for element in app.code:
        entry = {"value": str(element.value)}
        language = getattr(element, "language", None)
        if language:
            entry["language"] = str(language)
        code.append(entry)
    if code:
        captured["code"] = code

    metrics = [
        {"label": str(m.label), "value": str(m.value), "delta": str(m.delta or "")}
        for m in app.metric
    ]
    if metrics:
        captured["metric"] = metrics

    tables = [_rows(element.value) for element in app.dataframe]
    if tables:
        captured["dataframe"] = tables

    buttons = [
        {"label": str(b.label), "disabled": bool(getattr(b, "disabled", False))}
        for b in app.button
    ]
    if buttons:
        captured["button"] = buttons

    downloads = [
        {"label": str(element.label), "file_name": file_name}
        for element, file_name in zip(app.download_button, _download_names)
    ]
    if downloads:
        captured["download_button"] = downloads

    toggles = [
        {"label": str(w.label), "value": bool(w.value)}
        for w in list(app.toggle) + list(app.checkbox)
    ]
    if toggles:
        captured["toggle"] = toggles

    expanders = [str(w.label) for w in app.expander]
    if expanders:
        captured["expander"] = expanders

    # Inputs are not on the detail screen today. They are captured anyway, because a
    # later panel that adds one must not be invisible to the fence.
    inputs = [
        {"kind": kind, "label": str(w.label), "value": str(w.value)}
        for kind in ("text_input", "text_area", "number_input", "selectbox", "radio")
        for w in getattr(app, kind)
    ]
    if inputs:
        captured["input"] = inputs

    return captured


def capture(case: str, page: Path | None = None, server=None) -> dict:
    """Render one agent-detail view and return what it said.

    `page` exists so the fence can be proved: `tests/test_ui_golden.py` runs the same
    capture with one metric label renamed and requires the result to differ. A capture
    that ignores text would pass that mutation, and would be a snapshot of nothing.

    `server` lets a test point the same capture at an agent of a *different* profile
    (one loaded from a tmp dictionary), which is how "a new agent type needs no UI
    release" is checked rather than asserted in a docstring.
    """
    server = server or _server(case)
    with _transport(server):
        app = AppTest.from_file(str(page or AGENTS_PAGE), default_timeout=60)
        app.query_params["agent_id"] = AGENT_ID
        app.run()
    assert not app.exception, app.exception
    return {"case": case, "elements": elements(app)}


def capture_all() -> dict:
    """The committed fixture: one otelcol agent, one generic agent."""
    return {case: capture(case) for case in ("otelcol", "generic")}


def texts(captured: dict) -> str:
    """Every string in a capture, flattened — for "this wording is gone" assertions.

    `ensure_ascii=False` on purpose: a JSON-escaped surrogate pair is not the text
    an operator reads, and a test asserting on the escape would pass while the glyph
    on screen was wrong (the badge's icon comes from the profile, so it is worth
    asserting).
    """
    return json.dumps(captured, indent=2, sort_keys=True, ensure_ascii=False)


def main() -> None:
    print(json.dumps(capture_all(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
