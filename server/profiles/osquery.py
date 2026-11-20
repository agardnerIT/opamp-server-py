"""osquery structural rules and scheduled-query inventory — the `osquery` engine (#141).

osquery's OpAMP extension reports the document osquery is **running** as
`application/json` under the config object key `osquery.json` (the extension refuses any
other key). That document is osquery's own config format:

```json
{
  "options": {"config_refresh": 10},
  "schedule": {
    "battery_stats": {"query": "SELECT * FROM battery;", "interval": 60}
  },
  "packs": {"baseline": {"queries": {"users": {"query": "SELECT * FROM users;", "interval": 3600}}}}
}
```

So this engine judges the JSON shape of that document, and the scheduled queries are the
inventory: `schedule` names and `packs` are the things the agent runs, exactly as a
collector's components are the things it runs. Everything derived from the live document
is `used: true` by definition, which is why this profile does not declare the "heavy"
(unused components) report.

**Best effort, and it says so.** This server cannot run `osqueryd --config_check` on the
agent host, and a future release may accept a top-level key this file has not heard of,
so an unknown key is a warning. The rules that *are* errors are the ones a JSON config
cannot be read without: a `schedule`/`packs` that is not an object, a scheduled query
with no `query`, an interval that is not a positive integer.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

from server.config_validation import Verdict

#: Top-level keys osquery's config parser knows. A new one in a later release is a
#: warning, not a refusal — the agent accepted the document, or it would not report it.
KNOWN_SECTIONS = (
    "options",
    "schedule",
    "packs",
    "decorators",
    "file_paths",
    "exclude_paths",
    "events",
    "logger",
    "auto_table_construction",
    "feature_vectors",
    "views",
)

#: The sections an osquery config uses to say *what to run*. A document with neither has
#: nothing scheduled; that is legal (ad-hoc queries still work) but worth saying.
SCHEDULE_SECTIONS = ("schedule", "packs")

#: The keys that make a parsed JSON document recognisably an osquery config when the
#: content type is unhelpful (`text/plain`, or nothing at all).
_FINGERPRINT_KEYS = frozenset(SCHEDULE_SECTIONS) | {"options", "decorators"}


def _accepts_json(content_type: str, doc: Any) -> bool:
    """Whether this body should be read as osquery's JSON by this engine (#141).

    The declared content type is the first signal — `application/json` is what the
    extension reports. The second is the document's own vocabulary, for an agent that
    labels the body `text/plain` or leaves the type empty: a mapping with `schedule`,
    `packs`, `options` or `decorators` is an osquery config, and treating it as a broken
    collector YAML would be the exact "someone else's rules" bug #117 exists to remove.
    """
    value = (content_type or "").casefold()
    if "json" in value:
        return True
    return isinstance(doc, Mapping) and bool(_FINGERPRINT_KEYS.intersection(doc))


def _is_number(value: Any) -> bool:
    """JSON numbers only: a bool is an `int` in Python and is not an interval."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _judge_queries(
    section: str, name: str, queries: Any, path: str, verdict: Verdict
) -> None:
    """Check one `schedule`/pack-queries mapping. Shared so both spellings agree."""
    if not isinstance(queries, Mapping):
        verdict.add_error(
            path,
            f"`{name}` must be an object of query name -> query, not "
            f"{type(queries).__name__}",
            f"write `\"{name}\": {{\"query_name\": {{\"query\": \"SELECT …\", "
            "\"interval\": 60}}}`",
        )
        return
    for query_name, entry in queries.items():
        query_path = f"{path}.{query_name}" if path else str(query_name)
        if not str(query_name).strip():
            verdict.add_error(
                query_path or "<root>",
                f"a `{section}` query has an empty name",
                "every scheduled query needs a name: that is the address a second "
                "config source collides with, and what the agent reports",
            )
            continue
        if not isinstance(entry, Mapping):
            verdict.add_error(
                query_path,
                f"`{query_name}` must be an object with a `query` and an `interval`, "
                f"not {type(entry).__name__}",
                f"write `\"{query_name}\": {{\"query\": \"SELECT …\", \"interval\": 60}}`",
            )
            continue
        query = entry.get("query")
        if not isinstance(query, str) or not query.strip():
            verdict.add_error(
                query_path,
                f"`{query_name}` has no `query` SQL",
                "a scheduled query with no SQL does nothing; osquery refuses it",
            )
        interval = entry.get("interval")
        if interval is None:
            # osquery applies `--schedule_default_interval` when it is omitted, so this
            # is a fact about the effective schedule, not a broken document.
            verdict.add_warning(
                query_path,
                f"`{query_name}` sets no `interval`",
                "the agent's default interval applies; state it so the pushed schedule "
                "is explicit",
            )
        elif not _is_number(interval) or interval <= 0:
            verdict.add_error(
                query_path,
                f"`{query_name}` has interval {interval!r}, which is not a positive "
                "number of seconds",
                "an interval is an integer count of seconds, e.g. `\"interval\": 60`",
            )


def validate(doc: Any, path: str, ctx: Any, verdict: Verdict) -> bool:
    """Judge one osquery configuration (#141). Returns whether it was judged.

    `doc` is the parsed YAML/JSON document — JSON is a YAML subset, so the shared parser
    reads it — and `ctx.body` is the raw text. Declining (`False`) is the honest answer
    for a body that is not this product's: the caller then records that no structural
    check ran rather than inventing findings about a collector config.
    """
    if doc is None:
        # Not a parseable JSON/YAML document at all. The format layer already has the
        # parse error, and it is clearer than anything this engine could add; declining
        # leaves that message standing and marks the body unjudged.
        return False

    if not isinstance(doc, Mapping):
        if _accepts_json(getattr(ctx, "content_type", "") or "", doc):
            verdict.add_error(
                path or "<root>",
                "an osquery configuration is a JSON object, not "
                f"{type(doc).__name__}",
                'the document is `{"schedule": {…}, "options": {…}}`',
            )
            return True
        return False

    if not _accepts_json(getattr(ctx, "content_type", "") or "", doc):
        verdict.add_warning(
            path or "<root>",
            "not an osquery configuration (no `schedule`/`packs`/`options` and no JSON "
            "content type); structural checks skipped",
            'an osquery config is a JSON object with `schedule`, `packs` or `options`',
        )
        return False

    unknown = sorted(str(key) for key in doc if key not in KNOWN_SECTIONS)
    if unknown:
        verdict.add_warning(
            path or "<root>",
            "unknown top-level key(s): " + ", ".join(f"`{key}`" for key in unknown),
            "osquery ignores a key it does not know, so this is either a typo or a "
            "newer release's feature — check the version the agent reports",
        )

    options = doc.get("options")
    if options is not None and not isinstance(options, Mapping):
        verdict.add_error(
            path or "<root>",
            f"`options` must be an object of option -> value, not "
            f"{type(options).__name__}",
            'write `"options": {"config_refresh": 10}`',
        )

    schedule = doc.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, Mapping):
            verdict.add_error(
                path or "<root>",
                f"`schedule` must be an object of query name -> query, not "
                f"{type(schedule).__name__}",
                'write `"schedule": {"battery_stats": {"query": "SELECT …", '
                '"interval": 60}}`',
            )
        else:
            _judge_queries(
                "schedule", "schedule", schedule, path or "<root>", verdict
            )

    packs = doc.get("packs")
    if packs is not None:
        if not isinstance(packs, Mapping):
            verdict.add_error(
                path or "<root>",
                f"`packs` must be an object of pack name -> pack, not "
                f"{type(packs).__name__}",
                'write `"packs": {"baseline": {"queries": {…}}}`',
            )
        else:
            for pack_name, pack in packs.items():
                pack_path = f"{path}.packs.{pack_name}" if path else f"packs.{pack_name}"
                if isinstance(pack, str):
                    # osquery accepts a pack *file* path here; this server cannot read
                    # the file, so it is not evidence of a broken document.
                    continue
                if not isinstance(pack, Mapping):
                    verdict.add_error(
                        pack_path,
                        f"pack `{pack_name}` must be an object or a pack file path, "
                        f"not {type(pack).__name__}",
                        'write `"baseline": {"queries": {…}}` or a path to a pack file',
                    )
                    continue
                queries = pack.get("queries")
                if queries is None:
                    verdict.add_warning(
                        pack_path,
                        f"pack `{pack_name}` has no `queries`",
                        "a pack with no queries schedules nothing; inline a `queries` "
                        "object or point the pack at a file",
                    )
                else:
                    _judge_queries(
                        "packs", str(pack_name), queries, pack_path, verdict
                    )

    if not any(section in doc for section in SCHEDULE_SECTIONS):
        verdict.add_warning(
            path or "<root>",
            "no `schedule` and no `packs`: this agent has nothing scheduled",
            "add a `schedule` or a `packs` block if the pushed config is meant to run "
            "queries on a cadence",
        )
    return True


def components(agent: Any) -> Dict[str, List[Dict[str, Any]]]:
    """`{query: [{id}], pack: [{id}]}` from the document the agent runs (#141).

    The extension reports no `AvailableComponents`, so the config document is the
    evidence — and it is good evidence: a named entry under `schedule` is a query this
    agent runs, and a named entry under `packs` is a pack it loads. Everything is
    `used: true` because it came from the live config.

    The bodies come from `server.effective_config.raw_bodies()`, the one reader that
    decodes the stored JSON *and* the base64 `body` field — the extension reports its
    config as `application/json`, which `objects()` deliberately does not parse for the
    component paths.
    """
    from server import effective_config as effective_config_module

    report = getattr(agent, "effective_config", None)
    if not report:
        return {}
    found: Dict[str, List[Dict[str, Any]]] = {}
    try:
        bodies = effective_config_module.raw_bodies(report)
    except Exception:  # a damaged report is not a reason to fail a read path
        return {}
    for body in bodies:
        try:
            document = json.loads(body or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(document, Mapping):
            continue
        for section, component_type in (("schedule", "query"), ("packs", "pack")):
            entries = document.get(section)
            if not isinstance(entries, Mapping):
                continue
            bucket = found.setdefault(component_type, [])
            for name in entries:
                entry = {"id": str(name), "version": "", "used": True}
                if entry not in bucket:
                    bucket.append(entry)
    for bucket in found.values():
        bucket.sort(key=lambda item: item["id"])
    return found


def _register() -> None:
    from server.profiles.engines import register_components_engine, register_structure_engine

    register_structure_engine("osquery", validate)
    register_components_engine("osquery", components)


_register()
