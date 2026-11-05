"""The one-box SQL search editor used by the Agents page (#132).

Streamlit's native `text_input` cannot do context-aware completion, and a selectbox
next to it is a different interaction: the operator wants to type `SELECT`, press Tab
on `* FROM agents`, type `WHERE`, and see the fleet's columns — not pick a canned
statement from a list.

So the editor is a small custom component (`components/sql_search/index.html`) with no
build step: one input, a suggestion popup, Tab to accept, Enter to run. It computes
suggestions in the browser from the live schema (`GET /agents/query/schema`), which is
what makes `host_arch = 'amd64'` a suggestion only when an agent reported `amd64`.

The component returns `{"text": ..., "submit": bool, "nonce": ...}`. A submit is the
only thing the page acts on; typing does not rerun the Python script on every keystroke.

`value` is pushed *down* into the editor, and a component re-render is not a reliable
"the text changed" signal: the rerun that follows a submission still carries the value
from before it, and adopting that is what emptied the box as soon as a search ran. So
the page also passes `revision`, bumped whenever it deliberately sets the query, and the
editor only adopts a `value` that arrives with a revision it has not seen yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import streamlit.components.v1 as components

_COMPONENT_PATH = Path(__file__).resolve().parent / "components" / "sql_search"
_sql_search = components.declare_component("opamp_sql_search", path=str(_COMPONENT_PATH))


def sql_search(
    value: str = "",
    schema: Optional[Dict[str, Any]] = None,
    *,
    label: str = "Search agents with SQL",
    placeholder: str = "SELECT * FROM agents WHERE ...",
    revision: int = 0,
    key: Optional[str] = None,
) -> Dict[str, Any]:
    """Render the SQL search box and return its last event.

    Returns a dict with ``text`` (the current query), ``submit`` (true when the
    operator pressed Enter or the run button) and ``nonce`` (unique per submission, so
    a sticky component value is acted on once). The default value keeps the page
    deterministic on first render and in tests that cannot execute component JS.

    ``revision`` must be bumped by the caller every time it assigns ``value`` itself.
    Without it the editor cannot tell a deliberate change from a rerun echoing the text
    it already replaced.
    """
    try:
        revision = int(revision or 0)
    except (TypeError, ValueError):
        revision = 0
    result = _sql_search(
        value=value or "",
        revision=revision,
        schema=schema or {},
        label=label,
        placeholder=placeholder,
        key=key,
        default={"text": value or "", "submit": False},
    )
    if not isinstance(result, dict):
        return {"text": value or "", "submit": False, "nonce": None}
    return {
        "text": result.get("text") if isinstance(result.get("text"), str) else (value or ""),
        "submit": bool(result.get("submit")),
        "nonce": result.get("nonce"),
    }
