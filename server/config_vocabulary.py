"""Section names that are *addresses* in a merged config, not settings (#119).

The merge-collision check needs one fact before it can warn: which keys in a document
name a thing (a component id, a pipeline id, a scheduled query) rather than hold a value.
Two config sources that define the same address do not override each other — the daemon
refuses the merge and reports `config_conflict` — while a scalar leaf from a second source
is simply merged.

This is a fact about the config **formats** this build can read, so it is deliberately not
in `server/config_validation.py` (which judges *bodies* through whatever profile owns
them) nor in a single profile (osquery's `schedule` has nothing to do with the collector's
`receivers`, yet both agents report through here). A profile with its own vocabulary
declares `structure.address_sections` and is preferred over this default.

No imports, on purpose: `server/effective_config.py` and `server/config_validation.py`
both need it and already import each other's modules.
"""

from __future__ import annotations

#: OpenTelemetry Collector component/pipeline sections, plus osquery's scheduled queries
#: and packs. Union rather than per-product files because the check is a warning about a
#: *merge*, and the agent's own report is what supplies the other half of the evidence.
ADDRESS_SECTIONS = frozenset(
    {"receivers", "processors", "exporters", "extensions", "connectors", "pipelines"}
) | {"schedule", "packs"}

__all__ = ["ADDRESS_SECTIONS"]
