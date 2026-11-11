"""Fluent Bit structural rules and component inventory — the `fluentbit` engine (#123).

Written against **real** payloads, not documentation: `opamp-observe/opamp-core` 0.6.0
supervising Fluent Bit 5.1.5 against this server (evidence recorded on #123). What that
spike showed, and what this module is shaped by:

* the config is classic **INI** (`[SERVICE]`, `[INPUT] Name cpu`, …), reported under the
  key **`./fluent-bit.conf`** with `content_type: text/plain`;
* the agent advertises **`ReportsEffectiveConfig`** but **not**
  `ReportsAvailableComponents`, so the component inventory has to come from the config
  document — which is what `components()` below does;
* the OpAMP settings live in the *consumer's* `opamp.json`, not in `fluent-bit.conf`, so
  there is no protected invariant to enforce here. Removing something from this body can
  stop the agent's *data plane*, but it cannot orphan the control channel — that is what
  the #119 approver acknowledgement is for when a profile has nothing to check.

**Best effort, and it says so.** Fluent Bit is not YAML-schema'd, this server cannot run
`fluent-bit --dry-run` on the agent host, and plugin options are free-form. So the rules
here are the ones that are true of every configuration the daemon accepts (a `SERVICE`
section, at least one input and one output, a `Name` per plugin section) and everything
else is a warning. Pretending otherwise would be exactly the "collector-shaped rules for
someone else's product" problem #117 exists to remove.
"""

from __future__ import annotations

import configparser
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from server.config_validation import Verdict, accepts_ini

#: Sections Fluent Bit's configuration parser knows. Anything else is a warning, not an
#: error: the daemon ignores unknown sections, and a newer release may add one.
KNOWN_SECTIONS = (
    "SERVICE",
    "INPUT",
    "FILTER",
    "OUTPUT",
    "PARSER",
    "MULTILINE_PARSER",
    "CUSTOM",
)

#: Sections that name a plugin, and must therefore carry a `Name` key.
PLUGIN_SECTIONS = ("INPUT", "FILTER", "OUTPUT")

#: The sections a runnable configuration cannot do without: a service block and at least
#: one input feeding at least one output. Without these the daemon exits at startup.
REQUIRED_SECTIONS = ("SERVICE",)
REQUIRED_ANY_OF = ("INPUT",)
REQUIRED_ANY_OF_2 = ("OUTPUT",)


def _sections(body: str) -> List[Tuple[str, Dict[str, str]]]:
    """`[(section name, {key: value})]` from an INI document, or `[]` when unreadable.

    Fluent Bit allows the same section header more than once (`[INPUT]` twice is normal),
    so this is deliberately not `configparser`'s dict-of-sections: it keeps the order and
    the duplicates, which is what "how many inputs does this config have" needs.
    Duplicate *keys inside one section* are kept as the last value, which is what the
    daemon does.
    """
    sections: List[Tuple[str, Dict[str, str]]] = []
    current: Optional[Dict[str, str]] = None
    name: Optional[str] = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        header = re.fullmatch(r"\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]", line)
        if header:
            name = header.group(1).upper()
            current = {}
            sections.append((name, current))
            continue
        if current is None:
            # A key before any section header: not a file Fluent Bit will start with.
            return []
        if "=" in line:
            key, _, value = line.partition("=")
        else:
            parts = line.split(None, 1)
            key, value = parts[0], (parts[1] if len(parts) > 1 else "")
        current[key.strip()] = value.strip().strip('"').strip("'")
    return sections


def _section_keys(sections, wanted: str) -> List[str]:
    return [values.get("Name", "") for name, values in sections if name == wanted and values.get("Name")]


def validate(doc: Any, path: str, ctx: Any, verdict: Verdict) -> bool:
    """Judge one Fluent Bit configuration (#123). Returns whether it was judged.

    `doc` is `None` when the body did not parse as YAML/JSON — which is the normal case
    for this product, since its config is INI and its content type is `text/plain`. The
    raw text is on `ctx.body`, so the engine parses in its *own* dialect instead of
    asking PyYAML to understand `[SERVICE]`.
    """
    body = getattr(ctx, "body", "") or ""
    if not accepts_ini(getattr(ctx, "content_type", "") or "", body):
        # Not a Fluent Bit config as far as this engine can tell (a JSON body, a PEM
        # certificate, someone else's document): decline, so the caller records that no
        # structural check ran rather than inventing findings about a foreign body.
        verdict.add_warning(
            path,
            "not a Fluent Bit configuration (no `[SECTION]` header and no `.conf` "
            "content type); structural checks skipped",
            "a Fluent Bit config is INI: [SERVICE], [INPUT], [OUTPUT] …",
        )
        return False

    sections = _sections(body)
    if not sections:
        verdict.add_error(
            path or "<root>",
            "no `[SECTION]` header found: Fluent Bit cannot start on this file",
            "a Fluent Bit config is INI, e.g. `[SERVICE]` followed by `[INPUT]`/`[OUTPUT]`",
        )
        return True

    present = {name for name, _ in sections}
    for wanted in REQUIRED_SECTIONS:
        if wanted not in present:
            verdict.add_error(
                path or "<root>",
                f"no `[{wanted}]` section: the daemon exits at startup",
                "state `[SERVICE]` explicitly so the config runs where it is pushed",
            )
    for wanted, label in (
        (REQUIRED_ANY_OF, "input"),
        (REQUIRED_ANY_OF_2, "output"),
    ):
        if not any(name in present for name in wanted):
            verdict.add_error(
                path or "<root>",
                f"no `[{wanted[0]}]` section: a pipeline with no {label} does nothing",
                f"add at least one `[{wanted[0]}]` so the agent has somewhere to {label}",
            )

    unknown = sorted(present - set(KNOWN_SECTIONS))
    if unknown:
        verdict.add_warning(
            path or "<root>",
            "unknown section(s): " + ", ".join(f"`[{name}]`" for name in unknown),
            "Fluent Bit ignores a section it does not know, so this is either a typo or "
            "a newer release's feature — check the version the agent reports",
        )

    for name, values in sections:
        if name in PLUGIN_SECTIONS and not values.get("Name"):
            verdict.add_error(
                path or "<root>",
                f"a `[{name}]` section has no `Name` (the plugin to load)",
                f"every `[{name}]` needs `Name <plugin>`, e.g. `Name  cpu`",
            )

    # What a run-of-the-mill config usually forgets. Warnings: none of these stop the
    # daemon, and this server cannot know the operator's intent.
    service = next((values for name, values in sections if name == "SERVICE"), None) or {}
    if not service.get("Flush"):
        verdict.add_warning(
            path or "<root>",
            "`[SERVICE]` sets no `Flush` interval",
            "without it the default applies; state it so the pushed config is explicit",
        )
    for name, values in sections:
        if name == "OUTPUT" and not values.get("Match"):
            verdict.add_warning(
                path or "<root>",
                "an `[OUTPUT]` section has no `Match` pattern",
                "an output with no `Match` matches nothing: add `Match *` or a tag glob",
            )
    return True


def components(agent: Any) -> Dict[str, List[Dict[str, Any]]]:
    """`{input: [{id: cpu}], output: [{id: stdout}]}` from the config the agent runs (#123).

    `opamp-core` does not report `AvailableComponents`, so the *document* is the only
    evidence — and it is good evidence: `[INPUT] Name cpu` names a plugin this agent
    loads. Everything derived here is `used: true` by definition (it came from the live
    config), which is why the "heavy collectors" report (unused components) is not a
    report this profile declares.

    The bodies come from `server.effective_config.objects()`, the one reader that knows a
    reported body is base64 of `bytes` and decodes it — re-deriving that here is how the
    UI's config panel once showed base64 to an operator (#68/#131).
    """
    from server import effective_config as effective_config_module

    report = getattr(agent, "effective_config", None)
    if not report:
        return {}
    found: Dict[str, List[Dict[str, Any]]] = {}
    try:
        # `raw_bodies` decodes the stored JSON *and* the base64 `body` field, and does
        # not require the content type to be one this build can parse — an INI body
        # reported as `text/plain` is exactly the case it exists for.
        bodies = effective_config_module.raw_bodies(report)
    except Exception:  # a damaged report is not a reason to fail a read path
        return {}
    for body in bodies:
        for section, values in _sections(body or ""):
            if section not in PLUGIN_SECTIONS:
                continue
            plugin = values.get("Name")
            if not plugin:
                continue
            bucket = found.setdefault(section.lower(), [])
            entry = {"id": plugin, "version": "", "used": True}
            if entry not in bucket:
                bucket.append(entry)
    for bucket in found.values():
        bucket.sort(key=lambda item: item["id"])
    return found


def _register() -> None:
    from server.profiles.engines import register_components_engine, register_structure_engine

    register_structure_engine("fluentbit", validate)
    register_components_engine("fluentbit", components)


_register()
