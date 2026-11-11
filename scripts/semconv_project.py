"""Project the profile dictionary into a weaver registry, and check it (#135).

Weaver is a **semantic-convention** tool: it validates attribute groups, metric groups,
key syntax, stability and cross-references. The profile dictionary is mostly neither of
those things (`config_formats`, `panels`, `snippet`, `structure` are document shapes), so
this is a *projection*, not a conversion — and the projection covers exactly what weaver
can judge:

    identity.match_any   ->  one attribute_group per profile
    telemetry            ->  one metric_group per profile

Everything else stays the loader's job (`server/profiles/schema.py`). That split is the
point: a green `weaver registry check` means "the identity and telemetry projection is
well-formed", never "the dictionary is valid".

Determinism is a requirement, not a nicety: CI diffs this output, so the emission is
sorted everywhere and carries no timestamps. Two runs, or a directory whose files were
reordered, produce byte-identical files.

Usage:

    python scripts/semconv_project.py            # write the registry + docs
    python scripts/semconv_project.py --check     # exit 1 when the committed copy is stale

The generated files are committed (`docs/semconv/`, `docs/agent-profiles-projection.md`)
so a reviewer can read the diff without running weaver.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

REGISTRY_DIR = REPO / "docs" / "semconv"
DOC_PATH = REPO / "docs" / "agent-profiles-projection.md"

#: The attribute namespace for dictionary claims. Deliberately **not** a vendor's and not
#: `service.*`: a profile match is a statement about *our* management plane, and squatting
#: in someone else's namespace is how a semantic convention becomes uncorrectable. The
#: agent's own `service.name` stays an existing semconv attribute that a rule *reads*.
NAMESPACE = "opamp.agent"

#: Pinned on purpose: a linter that changes under us is not a gate. Bump both together, in
#: a commit that also regenerates `docs/semconv/` and the projection doc.
SEMCONV_VERSION = "1.30.0"
SCHEMA_URL = "https://opentelemetry.io/schemas/1.30.0"

#: Semconv key rules weaver enforces, mirrored here so a malformed key fails in *our*
#: tests too, not only in a CI job that needs Docker (#135's acceptance criterion).
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class ProjectionError(ValueError):
    """A dictionary entry that cannot be projected, with the reason."""


def _slug(value: str) -> str:
    """A semconv-safe segment: lower case, dots and dashes become underscores."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_").lower()
    return cleaned or "unnamed"


def check_key(key: str) -> None:
    """Raise when `key` is not a legal semconv attribute key.

    Two failures matter and both are silent in the dictionary itself: an upper-case or
    dash-separated segment (`Service_Name`) and a key with no namespace (`service_name`).
    Weaver rejects both, and this raises first so the failure is attributable.
    """
    if key != key.lower():
        raise ProjectionError(f"attribute key {key!r} must be lower case")
    if "-" in key or " " in key:
        raise ProjectionError(
            f"attribute key {key!r} must use underscores, not dashes or spaces"
        )
    if not KEY_RE.match(key):
        raise ProjectionError(
            f"attribute key {key!r} must be namespaced and snake_case "
            "(e.g. opamp.agent.otelcol.service_name)"
        )


def identity_attributes(profiles: Iterable[Any]) -> List[Dict[str, Any]]:
    """One `attribute_group` per profile, from its `identity.match_any` rules.

    A rule that matches nothing (no prefix, no equals) is a dictionary bug, but it is the
    loader's bug — skipped here rather than raised, because weaver cannot say anything
    about a rule with no value to project.
    """
    groups: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for profile in sorted(profiles, key=lambda item: item.name):
        attributes: List[Dict[str, Any]] = []
        for rule in profile.identity_match_any:
            if not (rule.prefix or rule.equals):
                continue
            attribute_id = f"{NAMESPACE}.{_slug(profile.name)}.match.{_slug(rule.attr)}"
            check_key(attribute_id)
            if attribute_id in seen:
                raise ProjectionError(
                    f"duplicate attribute id {attribute_id!r} "
                    f"(profiles {seen[attribute_id]!r} and {profile.name!r})"
                )
            seen[attribute_id] = profile.name
            values = list(rule.prefix) or list(rule.equals)
            attributes.append(
                {
                    "id": attribute_id,
                    "type": "string",
                    "stability": "development",
                    "brief": (
                        f"The `{rule.attr}` value that identifies a {profile.display_name} "
                        f"agent ({profile.name}'s profile, "
                        f"{'prefix' if rule.prefix else 'exact'} match)."
                    ),
                    "note": (
                        "Projected from the profile dictionary by "
                        "`scripts/semconv_project.py`; the dictionary is the source of "
                        "truth and this attribute is never emitted by an agent."
                    ),
                    "examples": sorted(values)[:3],
                }
            )
        if not attributes:
            continue
        groups.append(
            {
                "id": f"registry.{NAMESPACE}.{_slug(profile.name)}",
                "type": "attribute_group",
                "display_name": f"{profile.display_name} identity",
                "brief": (
                    f"Evidence the server uses to recognise a {profile.display_name} agent, "
                    f"declared in `server/profiles/data/{profile.name}.yaml`."
                ),
                "attributes": attributes,
            }
        )
    return groups


def telemetry_metrics(profiles: Iterable[Any]) -> List[Dict[str, Any]]:
    """One `metric` group per metric a profile declares (#121/#122).

    Semconv v2 has no "metric group with children": every metric is its own group
    (`type: metric`, `metric_name`, `instrument`, `unit`), which weaver then checks for
    name syntax, unit and stability. The dictionary's `metric_prefix`/`metric_map` is the
    agent's own naming, so the *projected* name is namespaced and the agent's name is
    carried in the note — that is the difference between "a convention we publish" and
    "a string another product happens to send".
    """
    groups: List[Dict[str, Any]] = []
    seen_ids: Dict[str, str] = {}
    for profile in sorted(profiles, key=lambda item: item.name):
        telemetry = getattr(profile, "telemetry", None)
        if telemetry is None:
            continue
        names: List[str] = []
        if telemetry.metric_prefix:
            names.append(f"{telemetry.metric_prefix}*")
        names.extend(sorted(telemetry.metric_map))
        for name in names:
            metric_id = f"metric.{NAMESPACE}.{_slug(profile.name)}.{_slug(name)}"
            check_key(metric_id.removeprefix("metric."))
            if metric_id in seen_ids:
                raise ProjectionError(
                    f"duplicate metric id {metric_id!r} "
                    f"(profiles {seen_ids[metric_id]!r} and {profile.name!r})"
                )
            seen_ids[metric_id] = profile.name
            groups.append(
                {
                    "id": metric_id,
                    "type": "metric",
                    "metric_name": f"{NAMESPACE}.{_slug(profile.name)}.{_slug(name)}",
                    "instrument": "gauge",
                    "unit": "1",
                    "stability": "development",
                    "brief": (
                        f"A metric {profile.display_name} reports through this server's "
                        f"OTLP sink (`{name}`)."
                    ),
                    "note": (
                        "The name the agent sends is the source of truth "
                        f"(`telemetry` in {profile.name}.yaml); the unit is the agent's "
                        "and is not normalised here."
                    ),
                }
            )
    return groups


def _yaml() -> Any:
    import yaml

    return yaml


def render_registry(profiles: Iterable[Any]) -> Dict[str, str]:
    """`{relative path: file text}` for the generated registry, sorted and stable."""
    yaml = _yaml()
    profiles = list(profiles)
    manifest = {
        # Weaver 0.28 assumes V2 without this, with a warning on every run; stating it
        # makes a version bump fail loudly instead of silently reinterpreting the files.
        "file_format": "definition_manifest/2.0",
        "schema_url": SCHEMA_URL,
        "semconv_version": SEMCONV_VERSION,
        "name": "opamp-agent-profiles",
    }
    attribute_groups = identity_attributes(profiles)
    metric_groups = telemetry_metrics(profiles)

    files = {
        "manifest.yaml": yaml.safe_dump(manifest, sort_keys=True, width=100),
        "attributes.yaml": yaml.safe_dump(
            {"groups": attribute_groups}, sort_keys=True, width=100
        ),
        "metrics.yaml": yaml.safe_dump({"groups": metric_groups}, sort_keys=True, width=100),
    }
    return {name: text for name, text in files.items()}


def render_doc(profiles: Iterable[Any]) -> str:
    """The committed, human-readable projection — `docs/agent-profiles-projection.md`."""
    profiles = list(profiles)
    attribute_groups = identity_attributes(profiles)
    metric_groups = telemetry_metrics(profiles)

    lines = [
        "# Agent profiles: what weaver can check (#135)",
        "",
        "**Generated — do not edit.** `python scripts/semconv_project.py` writes this file",
        "and `docs/semconv/`; CI fails when the committed copy is stale.",
        "",
        "This is a *projection*, not the dictionary. Weaver validates semantic conventions",
        f"(attribute and metric groups), so it is given exactly the parts of each profile",
        f"that are semantic conventions: identity evidence and telemetry names. Pinned to",
        f"schema `{SCHEMA_URL}` and `semconv_version {SEMCONV_VERSION}`.",
        "",
        "## What this check proves, and what it does not",
        "",
        "It proves the **identity and telemetry projection is well-formed**: attribute keys",
        "are namespaced and snake_case, no two profiles claim the same attribute id, and the",
        "metric groups are syntactically valid.",
        "",
        "It proves **nothing** about the rest of a profile — `config_formats`, `structure`,",
        "`components`, `panels`, `snippet`, `reports`. Those are document shapes and",
        "behaviour, and the loader schema (`server/profiles/schema.py`, run with",
        "`--strict-profiles`) is what validates them. A green `weaver registry check` that",
        "is read as \"the dictionary is valid\" is the failure mode this paragraph exists to",
        "prevent.",
        "",
        f"Namespace: `{NAMESPACE}.*` — deliberately not a vendor's namespace and not",
        "`service.*`. A profile match is a statement about this management plane; the",
        "agent's own `service.name` stays an existing semantic convention that a rule",
        "*reads*.",
        "",
        "## Identity evidence",
        "",
        "| Profile | Attribute | Match | Values |",
        "|---|---|---|---|",
    ]
    for group in attribute_groups:
        profile = group["display_name"].replace(" identity", "")
        for attribute in group["attributes"]:
            rule_kind = "prefix" if "match." in attribute["id"] else "equal"
            values = ", ".join(f"`{value}`" for value in attribute["examples"])
            lines.append(
                f"| {profile} | `{attribute['id']}` | {rule_kind} | {values} |"
            )

    lines += ["", "## Telemetry", ""]
    if metric_groups:
        lines += ["| Profile | Metric id | Agent's own name |", "|---|---|---|"]
        for group in metric_groups:
            profile = group["id"].split(".")[3]
            agent_name = group["brief"].rsplit("(`", 1)[-1].rstrip("`).")
            lines.append(f"| {profile} | `{group['id']}` | `{agent_name}` |")
    else:
        lines.append("No profile declares telemetry.")

    lines += [
        "",
        "## Running the check",
        "",
        "```bash",
        "python scripts/semconv_project.py           # regenerate",
        "docker run --rm -v \"$PWD/docs/semconv:/registry\" \\",
        f"  otel/weaver:{WEAVER_TAG} registry check -r /registry",
        "```",
        "",
        "The weaver image is pinned by tag and digest in the CI workflow; it is a dev/CI",
        "dependency only — not in the server image, not in `pyproject.toml`.",
        "",
    ]
    return "\n".join(lines)


#: The weaver release this projection is checked against. Pinned like the proto pin.
WEAVER_TAG = "0.28.0"


def load_profiles() -> List[Any]:
    """The dictionary, through the server's own loader (never a second YAML reader)."""
    from server import profiles as registry

    return list(registry.PROFILES)


def write(registry_dir: Path = REGISTRY_DIR, doc_path: Path = DOC_PATH) -> List[Path]:
    profiles = load_profiles()
    registry_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name, text in render_registry(profiles).items():
        path = registry_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    doc_path.write_text(render_doc(profiles), encoding="utf-8")
    written.append(doc_path)
    return written


def stale(registry_dir: Path = REGISTRY_DIR, doc_path: Path = DOC_PATH) -> List[str]:
    """Files whose committed content differs from what the dictionary produces now."""
    profiles = load_profiles()
    expected: Dict[Path, str] = {
        registry_dir / name: text for name, text in render_registry(profiles).items()
    }
    expected[doc_path] = render_doc(profiles)
    problems: List[str] = []
    for path, text in expected.items():
        if not path.exists():
            problems.append(f"{path.relative_to(REPO)} is missing")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"{path.relative_to(REPO)} is stale")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed projection differs from the dictionary",
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = stale()
        if problems:
            print("\n".join(problems))
            print("run: python scripts/semconv_project.py")
            return 1
        print("the committed projection matches the dictionary")
        return 0

    for path in write():
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
