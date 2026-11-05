"""Behaviour tests for the one-box SQL editor (#132 follow-up).

The editor is a custom component: plain JS in `ui/components/sql_search/index.html`
with no build step, so there is nothing to import and `AppTest` can only see the page
wiring around it. Three defects lived in exactly that blind spot:

1. **Escape left a blank band.** It hid the popup but never sent the smaller frame
   height back, so the iframe stayed tall and pushed the results down the page.
2. **The box emptied after a search.** The rerun a submission triggers still carries
   the *pre-submit* value; the editor adopted it, wiping the query it had just run.
3. **`FROM` was offered at the first position,** where no statement can start with it.

And one that only appears against the real frontend: Streamlit re-sends
`streamlit:render` on every frame-height change (`useEffect(..., [height])` in
`ComponentInstance.tsx`), so opening the popup grows the frame, which renders, and a
component that closes on "a render" shuts the list it just opened — a flash and
nothing else. `settle()` in the harness replays that loop, one render per height
change, and fails the scenario if the height never converges.

The harness (`tests/js/sql_search_harness.js`) runs the real `<script>` against a small
DOM stub and drives it like a browser: type, Tab, Enter, Escape, and the
`streamlit:render` messages the page sends. It needs `node`, which the Python test job
does not have, so this module skips there rather than pretending to pass — the point is
that the assertions are executable and cheap to run locally, not that CI renders a
browser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "js" / "sql_search_harness.js"
COMPONENT = REPO / "ui" / "components" / "sql_search" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; the component is JS"
)


def test_the_editor_behaves():
    """One node run covers every scenario; a failure names the scenario and the got/want."""
    assert COMPONENT.exists(), "the component must ship with the UI"
    assert HARNESS.exists(), "the harness must ship with the tests"
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "all scenarios passed" in output, output
    # The scenarios the three reported defects depend on must actually have run.
    for name in (
        "Escape closes the popup and restores the frame height",
        "submitting keeps the query in the box",
        "the empty first position offers SELECT, never FROM",
    ):
        assert f"ok   {name}" in output, output


def test_the_harness_reads_the_real_component():
    """A harness that drifted to a copy of the script would silently stop testing it."""
    harness = HARNESS.read_text()
    assert "ui/components/sql_search/index.html" in harness
    script = COMPONENT.read_text()
    for marker in ("streamlit:setFrameHeight", "streamlit:render", "streamlit:setComponentValue"):
        assert marker in script, marker
