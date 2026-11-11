"""Read the profile dictionary off disk and compile it (#134).

A directory of YAML files, one per agent type. `otelcol` is one of them; it used to
be `server/profiles/otelcol.py`. Adding a type is: drop a file in, restart. No Python,
no release.

Two failure modes matter more than the happy path, and they pull in opposite
directions:

* **A malformed file must not take the server down** (guardrail 5). One bad document
  is logged, that profile is absent, and the fleet is still served. The alternative —
  500 on every route because someone edited a YAML file at 03:00 — is worse than a
  missing profile, and a missing profile is visible: `GET /agent/types` will not list
  it and pinned agents naming it fall through to detection with a warning.
* **A malformed file must not pass unnoticed in CI** (same guardrail). Logging and
  carrying on is exactly how a typo ships. So `strict=True` turns every problem into a
  raise, and `--strict-profiles` / `OPAMP_PROFILES_STRICT=true` wire that to startup
  and to the tests.

Ordering is data too: `priority`, high to low. Never filename order — a locale, a
rename or a parallel checkout would each change fleet behaviour silently, and that is
a bug nobody would think to look for.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml
from loguru import logger
from pydantic import ValidationError

from server.profiles import engines
from server.profiles.base import (
    FALLBACK_PROFILE,
    AgentProfile,
    ComponentsSpec,
    ConfigFormat,
    IdentityRule,
    Panel,
    PanelMetric,
    SniffSpec,
    TelemetrySpec,
)
from server.profiles.schema import ProfileDocument

#: Where the shipped dictionary lives. `OPAMP_PROFILES_DIR` points elsewhere (an
#: operator's own directory, or a test's tmpdir).
DEFAULT_DIRECTORY = Path(__file__).parent / "data"

#: A file that is deliberately not a profile can be called `.example.yaml` / `.md`.
IGNORED_SUFFIXES = (".example.yaml", ".example.yml", ".md", ".txt")


class ProfileLoadError(Exception):
    """One document that could not be compiled. Never raised unless `strict`."""

    def __init__(self, source: str, problem: str):
        super().__init__(f"{source}: {problem}")
        self.source = source
        self.problem = problem


def directory() -> Path:
    """The dictionary directory in force, env-overridable."""
    override = (os.environ.get("OPAMP_PROFILES_DIR") or "").strip()
    return Path(override) if override else DEFAULT_DIRECTORY


def strict_default() -> bool:
    """`OPAMP_PROFILES_STRICT` — the env spelling of `--strict-profiles`."""
    return (os.environ.get("OPAMP_PROFILES_STRICT") or "").strip().casefold() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ------------------------------------------------------------------- compile ---


def compile_document(document: ProfileDocument) -> AgentProfile:
    """`ProfileDocument` -> the frozen `AgentProfile` the rest of the server reads.

    Only compile what the document declares: an absent `sniff` stays `None` so the
    profile genuinely has no sniffer, rather than one that returns False.
    """
    formats = tuple(
        ConfigFormat(
            content_types=fmt.content_types,
            default_filename=fmt.default_filename,
            distinctive=fmt.distinctive,
        )
        for fmt in document.config_formats
    )

    rules = tuple(
        IdentityRule(attr=rule.attr, prefix=rule.prefix, equals=rule.equals)
        for rule in document.identity.match_any
    )

    sniff_spec = None
    sniff = None
    if document.identity.sniff is not None:
        sniff_spec = SniffSpec(
            component_sections_any=document.identity.sniff.component_sections_any,
            config_sections_any=document.identity.sniff.config_sections_any,
        )
        sniff = engines.sniff_from(sniff_spec)

    components_spec = None
    components = None
    if document.components is not None:
        components_spec = ComponentsSpec(
            sections=dict(document.components.sections),
            known=dict(document.components.known),
            version_metadata_key=document.components.version_metadata_key,
            version_separator=document.components.version_separator,
            health_scope_prefix=document.components.health_scope_prefix,
            health_extensions_scope=document.components.health_extensions_scope,
            health_child_separator=document.components.health_child_separator,
        )
        components = engines.components_from(components_spec)

    structure = None
    if document.structure is not None:
        try:
            structure = engines.structure_from(document.structure.engine)
        except KeyError as exc:
            raise ProfileLoadError(document.name, str(exc)) from exc

    telemetry = None
    if document.telemetry is not None:
        telemetry = TelemetrySpec(
            metric_prefix=document.telemetry.metric_prefix,
            metric_map=dict(document.telemetry.metric_map),
            title=document.telemetry.title,
            caption=document.telemetry.caption,
            raw_caption=document.telemetry.raw_caption,
        )

    panels = tuple(
        Panel(
            id=panel.id,
            widget=panel.widget,
            title=panel.title,
            caption=panel.caption,
            metrics=tuple(
                PanelMetric(label=m.label, metric=m.metric, format=m.format)
                for m in panel.metrics
            ),
            metric=panel.metric,
            when=panel.when,
            severity=panel.severity,
            message=panel.message,
        )
        for panel in document.panels
    )

    return AgentProfile(
        name=document.name,
        display_name=document.display_name,
        summary=document.summary,
        guidance=document.guidance,
        icon=document.icon,
        priority=document.priority,
        config_formats=formats,
        validates_structure=document.validates_structure,
        structure=structure,
        address_sections=tuple(document.structure.address_sections)
        if document.structure is not None
        else (),
        identity_match_any=rules,
        sniff_spec=sniff_spec,
        sniff=sniff,
        components=components,
        components_spec=components_spec,
        reports=document.reports,
        telemetry=telemetry,
        panels=panels,
        snippet_template=document.snippet,
        snippet=engines.snippet_from(document.snippet) if document.snippet else None,
        # #121: the whole config for a *new* agent is a separate artefact from the
        # fragment an existing one gains, so it is a separate field — and it may be
        # absent, which is the honest state for a product whose config this server
        # cannot write.
        onboarding_template=document.onboarding_snippet,
        onboarding=(
            engines.snippet_from(document.onboarding_snippet)
            if document.onboarding_snippet
            else None
        ),
        bootstrap_default=document.bootstrap_default,
    )


# ---------------------------------------------------------------------- read ---


def load_document(path: Path) -> ProfileDocument:
    """Parse and validate one file. Raises `ProfileLoadError` with the file named."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileLoadError(path.name, f"could not be read ({exc.strerror or exc})") from exc
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ProfileLoadError(path.name, f"is not valid YAML ({exc})") from exc
    if payload is None:
        raise ProfileLoadError(path.name, "is empty")
    if not isinstance(payload, dict):
        raise ProfileLoadError(
            path.name, f"must be a mapping, not {type(payload).__name__}"
        )
    try:
        return ProfileDocument.model_validate(payload)
    except ValidationError as exc:
        raise ProfileLoadError(path.name, _explain(exc)) from exc


def _explain(exc: ValidationError) -> str:
    """A pydantic error as one readable line — this ends up in a log at 03:00."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "<document>"
        parts.append(f"{location}: {error.get('msg')}")
    return "; ".join(parts)


def load_all(
    directory_path: Optional[Path] = None, *, strict: Optional[bool] = None
) -> Tuple[AgentProfile, ...]:
    """Every valid profile in the directory, in match order, with the cross-file checks.

    Cross-file rules that a single document cannot enforce:

    * two profiles claiming the same `priority` is a load error — match order is a
      decision and it must be visible, not settled by file order;
    * two profiles claiming the same *distinctive* content type is a load error — the
      declared-identity path would otherwise pick one at random;
    * at most one `bootstrap_default`, because a token is minted before the agent's
      first connection and there is only one snippet to show.
    """
    directory_path = Path(directory_path) if directory_path is not None else directory()
    if strict is None:
        strict = strict_default()

    problems: List[ProfileLoadError] = []
    compiled: List[AgentProfile] = []

    if not directory_path.is_dir():
        problems.append(ProfileLoadError(str(directory_path), "is not a directory"))
        return _finish(compiled, problems, strict)

    for path in sorted(directory_path.iterdir()):
        if not path.is_file() or path.suffix not in (".yaml", ".yml"):
            continue
        if path.name.endswith(IGNORED_SUFFIXES):
            continue
        try:
            compiled.append(compile_document(load_document(path)))
        except ProfileLoadError as exc:
            problems.append(exc)

    if not compiled:
        problems.append(
            ProfileLoadError(str(directory_path), "contains no usable profile documents")
        )

    seen_names: Dict[str, str] = {}
    seen_priorities: Dict[int, str] = {}
    seen_distinctive: Dict[str, str] = {}
    bootstrap_defaults: List[str] = []
    kept: List[AgentProfile] = []
    for profile in compiled:
        problem = None
        if profile.name == FALLBACK_PROFILE:
            # The fallback is the one profile that must always exist, so it may not
            # be a file: a fallback that can fail to load is not a fallback.
            problem = (
                f"{FALLBACK_PROFILE!r} is the fallback and is built in; a dictionary "
                "file may not redefine it"
            )
        clash = seen_priorities.get(profile.priority)
        if problem is None and clash:
            problem = (
                f"priority {profile.priority} is also claimed by {clash!r}; match order "
                "must be explicit, so two profiles may not share one"
            )
        if problem is None and profile.name in seen_names:
            problem = f"name {profile.name!r} is also used by {seen_names[profile.name]}"
        if problem is None:
            for fmt in profile.config_formats:
                for content_type in fmt.content_types:
                    if not fmt.distinctive:
                        continue
                    owner = seen_distinctive.get(content_type)
                    if owner:
                        problem = (
                            f"distinctive content type {content_type!r} is also claimed "
                            f"by {owner!r}; only one profile may own a MIME type"
                        )
                        break
                if problem:
                    break
        if problem:
            problems.append(ProfileLoadError(profile.name, problem))
            continue
        kept.append(profile)
        seen_names[profile.name] = profile.name
        seen_priorities[profile.priority] = profile.name
        for fmt in profile.config_formats:
            if fmt.distinctive:
                for content_type in fmt.content_types:
                    seen_distinctive[content_type] = profile.name
        if profile.bootstrap_default:
            bootstrap_defaults.append(profile.name)

    if len(bootstrap_defaults) > 1:
        problems.append(
            ProfileLoadError(
                ", ".join(bootstrap_defaults),
                "more than one profile sets bootstrap_default; a token is minted "
                "before the agent is known, so there is only one snippet to show",
            )
        )
        kept = [profile for profile in kept if not profile.bootstrap_default]

    kept.sort(key=lambda profile: (-profile.priority, profile.name))
    return _finish(kept, problems, strict)


def _finish(
    profiles: List[AgentProfile], problems: Sequence[ProfileLoadError], strict: bool
) -> Tuple[AgentProfile, ...]:
    """Log-and-carry-on, or raise. `strict` is for CI and tests, never for a fleet."""
    if problems and strict:
        raise ProfileLoadError(
            "profile dictionary",
            "; ".join(f"{problem.source}: {problem.problem}" for problem in problems),
        )
    for problem in problems:
        logger.error(
            f"agent profile {problem.source}: {problem.problem} — the profile is "
            "absent, the rest of the dictionary and the fleet are unaffected "
            "(run with --strict-profiles to make this fatal)"
        )
    return tuple(profiles)
