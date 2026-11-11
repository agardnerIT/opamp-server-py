"""The golden fence for #121: the agent-detail screen must not move by accident.

`tests/test_ui_smoke.py` proves the pages *run*; `tests/test_ui_pages.py` proves what
they must never do. Neither notices a panel that kept rendering while its wording, its
metrics or its buttons changed — and #121 restructures a 900-line page whose own
acceptance criterion is "otelcol renders unchanged after the move".

`tests/ui_golden.py` records what the detail screen says for one otelcol agent and one
generic agent. This file compares that capture to
`tests/fixtures/agent_detail_golden.json`, and — the part that makes it a fence rather
than a snapshot of nothing — runs the same capture against a page with one metric label
renamed and requires it to notice.

The `otelcol` half is the fence: it stayed byte-identical through #121. The `generic`
half used to record the *wrong* behaviour (a `collector-config.yaml` download and OTel
wording on an agent that is not a collector); it changed once, in Phase 3, and what it
records now is the absence of both — plus the profile's own sentence about telemetry.

Neither fixture is updated by a test run. Regenerate with::

    python -m tests.ui_golden > tests/fixtures/agent_detail_golden.json
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("streamlit", reason="the UI golden needs the optional [ui] extra")

from tests import ui_golden

FIXTURE = ui_golden.FIXTURE


@pytest.fixture(scope="module")
def golden() -> dict:
    assert FIXTURE.exists(), (
        f"{FIXTURE} is missing. Capture it from a tree you trust:\n"
        "    python -m tests.ui_golden > tests/fixtures/agent_detail_golden.json"
    )
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ------------------------------------------------------------ the two captures ----


def test_the_otelcol_detail_renders_exactly_what_was_recorded(golden):
    """The fence. If this fails during #121, the move was not a move."""
    assert ui_golden.capture("otelcol") == golden["otelcol"]


def test_the_generic_detail_renders_exactly_what_was_recorded(golden):
    """The before-picture for Phase 3: an agent with no telemetry gets OTel chrome."""
    assert ui_golden.capture("generic") == golden["generic"]


def test_the_open_config_panel_renders_exactly_what_was_recorded(golden):
    """#131: the panel with the editor, which the closed-panel captures cannot see.

    Both closed captures stayed byte-identical when the editor landed, on purpose: the
    editor only exists once the panel is opened. This half records what is *in* it —
    the body the agent reported, the propose button, and no read-only block beside them.
    """
    captured = ui_golden.capture("generic", show_config=True)
    assert captured == golden["generic_editable"]

    elements = captured["elements"]
    inputs = {(entry["kind"], entry["label"]): entry["value"] for entry in elements["input"]}
    # The box holds the body the agent reported, verbatim — that is what a proposal
    # pushes, so it is not pretty-printed or re-serialised on the way in.
    assert inputs[("text_area", "Config body")] == (
        ui_golden.EFFECTIVE_CONFIG["configMap"]["configMap"][""]["body"]
    )
    assert ("text_input", "Note (recorded in the audit log)") in inputs
    assert any(button["label"] == "Propose this change" for button in elements["button"])
    # #121 deliverable 4: the editor says the body is opaque for a profile the server
    # does not interpret, and that a pass is not "valid for this agent".
    assert any("opaque text" in line for line in elements["info"])
    # One account of the config on screen, not a code block beside the editor.
    assert [entry["value"] for entry in elements.get("code", [])] == [ui_golden.AGENT_ID]


# ------------------------------------------------- proving the fence is a fence ----


def test_the_capture_notices_a_renamed_metric_panel(golden, tmp_path):
    """A capture that passes while a panel is renamed is a snapshot of nothing.

    This is the acceptance criterion from the plan: the fence has to fail when the thing
    it fences changes. #121 moved the panel out of the page and into the dictionary, so
    the mutation moved with it — renaming a label in a *copy* of `otelcol.yaml` and
    reloading the registry now proves the harder claim: that the page really renders
    what the profile file says, rather than a copy of it that happens to agree today.
    """
    import shutil

    from server import profiles

    directory = tmp_path / "data"
    shutil.copytree(profiles.loader.directory(), directory)
    document = directory / "otelcol.yaml"
    text = document.read_text(encoding="utf-8")
    assert "Logs In" in text, (
        "the panel this test renames is gone; pick another metric label rather than "
        "deleting the proof"
    )
    document.write_text(text.replace("Logs In", "Records In"), encoding="utf-8")

    try:
        profiles.reload(directory, strict=True)
        changed = ui_golden.capture("otelcol")
    finally:
        profiles.reload()  # never leave the shipped dictionary mutated for other tests

    assert changed != golden["otelcol"], "renaming a metric panel changed nothing"
    assert "Records In" in ui_golden.texts(changed)
    assert "Logs In" not in ui_golden.texts(changed)


# ------------------------------------------------------ the fixture says something ----


def test_the_otelcol_half_really_exercises_the_collector_path(golden):
    """Guard against a fixture captured from an empty fleet or a broken fake.

    An all-empty capture would satisfy both comparisons above forever, and would then
    be relaxed into uselessness the first time it was inconvenient.
    """
    body = ui_golden.texts(golden["otelcol"])
    # The twelve reads #121 deletes, seen through the panel they light up.
    for label in ("Logs In", "Traces In", "Metrics In", "Refused", "Logs Out", "CPU (sec)"):
        assert f'"label": "{label}"' in body, label
    assert "otelcol_exporter_sent_log_records" in body, "the raw-metric expander is missing"
    assert "Collector Telemetry" in body
    # And the collector's own components, so the table is captured too.
    assert '"Name"' in body and "otlphttp" in body


def test_the_generic_half_has_no_collector_chrome_left(golden):
    """The point of capturing a non-collector, after #121: it gets none of it.

    This test used to assert the opposite (a `collector-config.yaml` download and OTel
    wording) and fail the moment Phase 3 landed. Flipping it *is* the proof the issue
    was fixed, and it is the mechanical version of the acceptance screenshot.
    """
    body = ui_golden.texts(golden["generic"])
    for artefact in (
        "collector-config.yaml",
        "Collector Telemetry",
        "Data Flow",
        "otelcol_",
        "Create Slim Distro",
        "manifest.yaml",
    ):
        assert artefact not in body, artefact
    assert "collector" not in body.lower()

    # The two sentences that replaced one: a product fact, not a fault, and this agent
    # reports no telemetry at all.
    assert "This profile does not report telemetry to the server." in body
    assert "No telemetry received yet." not in body
    # No component inventory for a profile that has no such concept — and no "0/0".
    assert "0/0 in use" not in body
    assert "No components reported" not in body
    # It has its own shape, so the capture is not a copy of the otelcol one.
    assert golden["generic"] != golden["otelcol"]


def test_a_profile_with_no_panels_renders_generic_chrome(tmp_path):
    """The plan's Phase 3 gate: a data-only profile needs no UI work to render.

    A brand-new agent type — a YAML file with an identity rule and nothing else — must
    come out as generic chrome: no OTel wording, no empty metric grid, no collector
    artefact, and no Python change. That is acceptance criterion 1 of #117 made
    executable at the UI layer instead of asserted in prose.
    """
    import shutil

    from server import profiles

    directory = tmp_path / "data"
    shutil.copytree(profiles.loader.directory(), directory)
    (directory / "fluentbit.yaml").write_text(
        """\
name: fluentbit
display_name: FluentBit
icon: "🪵"
priority: 50
summary: A log forwarder. This server does not interpret its configuration.
identity:
  match_any:
    - attr: service.name
      prefix: ["fluent-bit", "fluentbit"]
""",
        encoding="utf-8",
    )

    try:
        profiles.reload(directory, strict=True)
        captured = ui_golden.capture(
            "generic",
            server=ui_golden._GoldenServer(
                metrics={},
                components={},
                effective_config=None,
                description=ui_golden._description("fluent-bit"),
                agent_type="fluentbit",
            ),
        )
    finally:
        profiles.reload()

    body = ui_golden.texts(captured)
    assert "🪵 fluentbit" in body, "the badge did not take the profile's own icon"
    assert "collector" not in body.lower()
    assert "otelcol" not in body.lower()
    assert "This profile does not report telemetry to the server." in body
