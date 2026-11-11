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

The `otelcol` half is the fence: keep it byte-identical through #121. The `generic`
half records today's *wrong* behaviour (a `collector-config.yaml` download and OTel
wording on an agent that is not a collector) and is expected to change once, in Phase 3,
with the diff as the review.

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


# ------------------------------------------------- proving the fence is a fence ----


def test_the_capture_notices_a_renamed_metric_panel(golden, tmp_path):
    """A capture that passes while a panel is renamed is a snapshot of nothing.

    This is the acceptance criterion from the plan: the fence has to fail when the
    thing it fences changes. The rename is applied to a *copy* of the real page, so the
    proof uses the same render path as the fixture — not a hand-built dict.
    """
    source = ui_golden.AGENTS_PAGE.read_text(encoding="utf-8")
    assert 'st.metric("Logs In"' in source, (
        "the panel this test renames is gone; pick another metric label rather than "
        "deleting the proof"
    )
    mutated = tmp_path / "1_Agents.py"
    mutated.write_text(
        source.replace('st.metric("Logs In"', 'st.metric("Records In"'), encoding="utf-8"
    )

    changed = ui_golden.capture("otelcol", page=mutated)

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


def test_the_generic_half_records_the_chrome_phase_3_must_delete(golden):
    """The point of capturing a non-collector: today it still gets collector artefacts.

    When #121 lands, this test fails — deliberately. Replace the assertions with the
    opposite ones ("no collector-config.yaml, no OTel wording") and regenerate the
    fixture; that swap is the proof the issue was fixed.
    """
    body = ui_golden.texts(golden["generic"])
    assert "collector-config.yaml" in body
    assert "otelcol" not in body.lower(), (
        "the generic agent now mentions otelcol: Phase 3 may already have landed. If it "
        "has, flip this test and regenerate the fixture."
    )
    # It does have its own shape, so the capture is not a copy of the otelcol one.
    assert golden["generic"] != golden["otelcol"]
