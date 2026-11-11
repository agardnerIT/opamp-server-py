"""OpenTelemetry Collector structural rules — the `otelcol` structure engine (#119).

Split out of `server/config_validation.py`, which now keeps only what is true of any
config body: the size ceiling, the bounded parse, the redaction refusal, the policy gate
and the `Verdict`. Everything in this module is OpenTelemetry's own vocabulary —
`receivers`/`processors`/`exporters`, `service.pipelines`, the `opamp` extension and its
protected invariants — so a FluentBit or ossuary profile is judged by *its* rules (or by
none, with the verdict saying so) instead of being graded as a broken collector.

Two things to know before editing:

* **The move was a move.** These functions were cut out verbatim, and
  `tests/test_config_validation.py` pins the verdict's `errors`/`warnings`/`normalised`
  for a corpus of good and bad collector configs, so a behaviour change here shows up as
  a diff rather than as a rollout that quietly stopped checking something.
* **A rule that is not OTel's does not belong here.** The format pipeline, the redaction
  marker and the policy gate are profile-independent and live in
  `server.config_validation`; adding one of them to this file would re-couple every
  profile to the collector's answer.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from loguru import logger

from server.config_validation import (
    Verdict,
    _as_list,
    _is_mapping,
    _path,
    looks_yaml_like,
)

COMPONENT_SECTIONS = (
    "receivers",
    "processors",
    "exporters",
    "extensions",
    "connectors",
)


CONFIGURATOR_SECTIONS = COMPONENT_SECTIONS + ("service",)


PIPELINE_SECTIONS = {
    "receivers": "receivers",
    "processors": "processors",
    "exporters": "exporters",
}


OPAMP_EXTENSION = "opamp"


ADDRESS_SECTIONS = frozenset(COMPONENT_SECTIONS) | {
    "pipelines",  # collector: service.pipelines.<id>
    "schedule",  # osquery: schedule.<query name>
    "packs",  # osquery: packs.<pack name>
}


def allowed_exporter_hosts() -> Tuple[str, ...]:
    """`CONFIG_ALLOWED_EXPORTER_HOSTS` (comma-separated); empty means "no opinion"."""
    raw = (os.environ.get("CONFIG_ALLOWED_EXPORTER_HOSTS") or "").strip()
    if not raw:
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def allowed_opamp_endpoints() -> Tuple[str, ...]:
    """Endpoints a pushed config may keep/point the `opamp` extension at.

    Deliberately built from *this* server's advertised addresses plus an explicit
    allowlist: moving a fleet's control channel is exactly the chicken-and-egg
    outage #83 warns about, so it needs a deliberate opt-in, not a typo.
    """
    from server import posture

    values: List[str] = []
    for name in ("OPAMP_SERVER_ENDPOINT", "OPAMP_WS_ENDPOINT", "OPAMP_HTTP_ENDPOINT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            values.append(value)
    public_url = (posture.public_url() or "").strip()
    if public_url:
        values.append(public_url)
        parsed = urlparse(public_url)
        if parsed.hostname:
            values.append(parsed.hostname)
    raw = (os.environ.get("CONFIG_ALLOWED_OPAMP_ENDPOINTS") or "").strip()
    values.extend(part.strip() for part in raw.split(",") if part.strip())
    return tuple(dict.fromkeys(values))


def check_structure(doc: Any, path: str, verdict: Verdict) -> None:
    """Structural rules for a complete collector config (#100 stage 3)."""
    if not _is_mapping(doc):
        verdict.add_error(
            path or "<root>",
            "config body must be a YAML/JSON mapping at the top level",
            "a collector config is a mapping of sections (`receivers`, `exporters`, `service`, …)",
        )
        return

    present = [section for section in CONFIGURATOR_SECTIONS if section in doc]
    if "service" not in doc:
        if present:
            verdict.add_warning(
                path or "<root>",
                "no `service` section: this is a config fragment, so pipeline and "
                "protected-invariant checks were skipped",
                "include the full config (with `service.pipelines`) to get the full checks",
            )
        return

    service = doc.get("service")
    if not _is_mapping(service):
        verdict.add_error(
            _path(path, "service"),
            "`service` must be a mapping",
            "e.g. `service: {pipelines: {traces: {receivers: [otlp], exporters: [debug]}}}`",
        )
        return

    for section in COMPONENT_SECTIONS:
        if section in doc and not _is_mapping(doc[section]):
            verdict.add_error(
                _path(path, section),
                f"`{section}` must be a mapping of id → configuration",
            )
    for section in COMPONENT_SECTIONS:
        if section not in doc:
            continue
        for comp_id in doc[section]:
            if not isinstance(comp_id, str) or not comp_id:
                verdict.add_error(
                    _path(path, section),
                    f"`{section}` keys must be non-empty strings (got {comp_id!r})",
                )

    pipelines = service.get("pipelines")
    if not _is_mapping(pipelines) or not pipelines:
        verdict.add_error(
            _path(path, "service", "pipelines"),
            "`service.pipelines` must be a non-empty mapping",
            "a pipeline is what makes the collector run: `traces: {receivers: [...], exporters: [...]}`",
        )
        return

    for name, pipeline in pipelines.items():
        pipeline_path = _path(path, "service", "pipelines", name)
        if not _is_mapping(pipeline):
            verdict.add_error(pipeline_path, "pipeline must be a mapping")
            continue
        for section, top_level in PIPELINE_SECTIONS.items():
            ids = _as_list(pipeline.get(section))
            required = section in ("receivers", "exporters")
            if not ids:
                if required:
                    verdict.add_error(
                        _path(pipeline_path, section),
                        f"pipeline `{name}` has no {section}",
                        f"a pipeline needs at least one entry in `{section}`",
                    )
                continue
            declared = doc.get(top_level) if _is_mapping(doc.get(top_level)) else {}
            for index, component in enumerate(ids):
                item_path = f"{_path(pipeline_path, section)}[{index}]"
                if not isinstance(component, str) or not component:
                    verdict.add_error(
                        item_path, f"{section} entries must be component ids (strings)"
                    )
                    continue
                if component not in declared:
                    verdict.add_error(
                        item_path,
                        f"`{component}` is referenced but not defined under `{top_level}`",
                        f"add `{top_level}.{component}` or remove it from the pipeline",
                    )

    # `service.extensions` is a list of ids that must exist under `extensions`.
    extensions = doc.get("extensions") if _is_mapping(doc.get("extensions")) else {}
    for index, extension in enumerate(_as_list(service.get("extensions"))):
        item_path = f"{_path(path, 'service', 'extensions')}[{index}]"
        if not isinstance(extension, str) or not extension:
            verdict.add_error(item_path, "extensions entries must be component ids (strings)")
        elif extension not in extensions:
            verdict.add_error(
                item_path,
                f"`{extension}` is enabled in `service.extensions` but not defined under `extensions`",
                f"add `extensions.{extension}` or remove it from `service.extensions`",
            )

    # Connectors are referenced from a pipeline end via `receivers`/`exporters` too;
    # catch a connector defined in a section it does not belong to (cheap sanity).
    for section in ("connectors",):
        if section in doc and not _is_mapping(doc[section]):
            verdict.add_error(_path(path, section), f"`{section}` must be a mapping")


def _iter_endpoints(value: Any, path: str) -> Iterable[Tuple[str, str, str]]:
    """Yield `(path, field, url)` for every `endpoint`/`url` string in a subtree."""
    if _is_mapping(value):
        for key, child in value.items():
            child_path = _path(path, key)
            if key in ("endpoint", "url", "server_url", "ws_endpoint") and isinstance(child, str):
                yield child_path, str(key), child
            else:
                yield from _iter_endpoints(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _iter_endpoints(child, f"{path}[{index}]")


def _normalise_endpoint(url: str) -> str:
    """`scheme://host[:port][/path]` lowercased, no trailing slash — for comparison."""
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").lower()
    port = parsed.port
    path = (parsed.path or "").rstrip("/")
    netloc = f"{host}:{port}" if port else host
    return f"{parsed.scheme.lower()}://{netloc}{path}" if parsed.scheme else f"{netloc}{path}"


def _endpoint_host(url: str) -> str:
    """`host[:port]`, lowercased — the part that decides *who* receives the traffic."""
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").lower()
    return f"{host}:{parsed.port}" if parsed.port else host


def _endpoint_allowed(pushed: str, allowed: Sequence[str]) -> bool:
    """True when the pushed endpoint is exactly allowed or lands on an allowed host.

    Host-level matching is the security-relevant half: moving a fleet's control
    channel to another machine is the outage #83 warns about, while a path or scheme
    change on the same host is not. An allowlist entry with no scheme is treated as
    a bare host[:port].
    """
    candidate = _normalise_endpoint(pushed)
    if not candidate:
        return True
    candidate_host = _endpoint_host(pushed)
    for entry in allowed:
        normalised = _normalise_endpoint(entry)
        if not normalised:
            continue
        if candidate == normalised:
            return True
        if candidate_host and candidate_host == _endpoint_host(entry):
            return True
    return False


def check_invariants(
    doc: Any,
    path: str,
    verdict: Verdict,
    *,
    allow_endpoint_change: bool = False,
    allowed_endpoints: Optional[Sequence[str]] = None,
    allowed_exporters: Optional[Sequence[str]] = None,
) -> None:
    """The protected invariants (D7): never let a push cut the control channel.

    Removing the `opamp` extension is the one mistake the server cannot fix from a
    distance — the agent stops talking, so the next push has nowhere to go. It is an
    error, not a warning.
    """
    if not _is_mapping(doc) or "service" not in doc:
        return
    allowed_endpoints = (
        tuple(allowed_endpoints) if allowed_endpoints is not None else allowed_opamp_endpoints()
    )
    allowed_exporters = (
        tuple(allowed_exporters) if allowed_exporters is not None else allowed_exporter_hosts()
    )

    extensions = doc.get("extensions") if _is_mapping(doc.get("extensions")) else {}
    service = doc.get("service") if _is_mapping(doc.get("service")) else {}
    enabled = [item for item in _as_list(service.get("extensions")) if isinstance(item, str)]

    if OPAMP_EXTENSION not in extensions:
        verdict.add_error(
            _path(path, "extensions", OPAMP_EXTENSION),
            "the `opamp` extension is missing from `extensions`",
            "without it the agent loses its control channel and can never be fixed "
            "remotely; keep the existing `extensions.opamp` block",
        )
    if OPAMP_EXTENSION not in enabled:
        verdict.add_error(
            _path(path, "service", "extensions"),
            "`opamp` is not listed in `service.extensions`",
            "an extension that is not enabled does not run: add `opamp` to `service.extensions`",
        )

    opamp_config = extensions.get(OPAMP_EXTENSION)
    endpoints = list(_iter_endpoints(opamp_config, _path(path, "extensions", OPAMP_EXTENSION)))
    if not endpoints:
        verdict.add_warning(
            _path(path, "extensions", OPAMP_EXTENSION),
            "no endpoint is set for the `opamp` extension in this config",
            "the agent will keep its local endpoint; state it explicitly if the "
            "control channel must be verifiable",
        )
    for endpoint_path, _, url in endpoints:
        if _endpoint_allowed(url, allowed_endpoints):
            continue
        if allow_endpoint_change:
            verdict.add_warning(
                endpoint_path,
                f"the `opamp` endpoint moves to {url!r}; the change was explicitly allowed",
                "an agent that cannot reach the new endpoint is unreachable until someone "
                "fixes it on the host",
            )
            continue
        verdict.add_error(
            endpoint_path,
            f"the `opamp` endpoint would move to {url!r}, which is not this server",
            "set `allow_endpoint_change: true` and confirm it as an approver if that is "
            "intended (it is the chicken-and-egg outage #83 warns about), or configure "
            "CONFIG_ALLOWED_OPAMP_ENDPOINTS",
        )

    # -- warnings: things that usually mean "this config will hurt" --------------
    if not _is_mapping(service.get("telemetry")):
        verdict.add_warning(
            _path(path, "service", "telemetry"),
            "`service.telemetry` is not set: the collector's own logs/metrics are not configured",
            "keeping telemetry is how you find out a rollout went wrong",
        )

    processors_used = set()
    for pipeline in (service.get("pipelines") or {}).values():
        if _is_mapping(pipeline):
            processors_used.update(
                item for item in _as_list(pipeline.get("processors")) if isinstance(item, str)
            )
    if _MEMORY_LIMITER not in processors_used:
        verdict.add_warning(
            _path(path, "processors", _MEMORY_LIMITER),
            "no `memory_limiter` processor is used in any pipeline",
            "without it a collector under load is OOM-killed, which looks like a crash loop",
        )
    if _BATCH not in processors_used:
        verdict.add_warning(
            _path(path, "processors", _BATCH),
            "no `batch` processor is used in any pipeline",
            "batching is what keeps exporter throughput and cost sane",
        )

    if allowed_exporters:
        exporters = doc.get("exporters") if _is_mapping(doc.get("exporters")) else {}
        for exporter_id, exporter_config in exporters.items():
            for endpoint_path, _, url in _iter_endpoints(
                exporter_config, _path(path, "exporters", exporter_id)
            ):
                if _endpoint_allowed(url, allowed_exporters):
                    continue
                verdict.add_warning(
                    endpoint_path,
                    f"exporter host is not on CONFIG_ALLOWED_EXPORTER_HOSTS: {url!r}",
                    "an exporter is an egress path; if this host is expected, allowlist it",
                )


def inventory_components(available_components: Any) -> Dict[str, set]:
    """`{section: {component ids}}` from an agent's reported `available_components`.

    Shape comes from the proto (`components.<section>.subComponentMap`); anything
    unexpected yields an empty map rather than an exception, because a malformed
    report must degrade the warning, not the push path.
    """
    result: Dict[str, set] = {}
    try:
        container = (available_components or {}).get("components", {})
    except AttributeError:
        return result
    if not isinstance(container, dict):
        return result
    for section, body in container.items():
        if not isinstance(body, dict):
            continue
        sub = body.get("subComponentMap") or body.get("sub_component_map") or {}
        if isinstance(sub, dict):
            result[str(section)] = {str(key) for key in sub}
    return result


def check_inventory(
    doc: Any,
    path: str,
    verdict: Verdict,
    agents: Sequence[Any],
) -> None:
    """Warn when the config references a component a target agent does not ship.

    The "you pushed a config for a distro that does not build `redismetricsreceiver`"
    class of outage. A warning, not an error: an inventory can be stale (an agent
    that just upgraded), and refusing on it would make a rollout depend on report
    timing rather than on the config. Findings are aggregated across agents, so a
    400-agent selector produces one warning naming a few affected agents instead of
    400 warnings nobody reads.
    """
    if not _is_mapping(doc):
        return
    service = doc.get("service") if _is_mapping(doc.get("service")) else {}
    referenced: Dict[str, set] = {}
    for section in COMPONENT_SECTIONS:
        if _is_mapping(doc.get(section)):
            referenced[section] = {str(key) for key in doc[section]}
    for section, top_level in PIPELINE_SECTIONS.items():
        referenced.setdefault(top_level, set())
        for pipeline in (service.get("pipelines") or {}).values():
            if _is_mapping(pipeline):
                referenced[top_level].update(
                    item for item in _as_list(pipeline.get(section)) if isinstance(item, str)
                )
    for extension in _as_list(service.get("extensions")):
        if isinstance(extension, str):
            referenced.setdefault("extensions", set()).add(extension)

    # (section, component) -> agent ids that do not ship it
    missing: Dict[tuple, List[str]] = {}
    for agent in agents or ():
        known = inventory_components(getattr(agent, "available_components", None))
        if not known:
            continue
        agent_id = str(getattr(agent, "agent_id", "") or "")
        for section, wanted in referenced.items():
            shipped = known.get(section)
            if shipped is None:
                continue
            for component in wanted - shipped:
                missing.setdefault((section, component), []).append(agent_id)

    for (section, component), agent_ids in sorted(missing.items()):
        affected = ", ".join(agent_id[:8] for agent_id in agent_ids[:3])
        more = f" (+{len(agent_ids) - 3} more)" if len(agent_ids) > 3 else ""
        verdict.add_warning(
            _path(path, section, component),
            f"`{component}` is not in the available components of {len(agent_ids)} "
            f"target agent(s): {affected}{more}",
            "the config will fail to start on that agent; check the collector "
            "distribution (`ocb` manifest) shipped there",
        )


def _looks_like_collector_config(doc: Any) -> bool:
    return _is_mapping(doc) and any(section in doc for section in CONFIGURATOR_SECTIONS)


_MEMORY_LIMITER = "memory_limiter"


_BATCH = "batch"


#: What a structured profile hands the core pipeline, and what it gets back.
#:
#: `validate()` is the whole hook: the core has already decoded the body and applied the
#: format layer (size, parse, redaction), so a rule here may assume `doc` parsed. The
#: verdict is the same object the core fills, so findings land in the same list, with the
#: same key paths and the same order they had before #119 split them out.
def validate(doc: Any, path: str, ctx: Any, verdict: Verdict) -> None:
    """Judge one parsed collector config (#100 stage 3+4)."""
    kind = looks_yaml_like(getattr(ctx, "content_type", "") or "")
    if kind == "unknown" and not _looks_like_collector_config(doc):
        # Undeclared type and not a collector shape: the body may be something else
        # entirely (a PEM certificate, a shell script), so the *format* layer's answer is
        # "advisory" and this engine has no opinion to add.
        verdict.add_warning(
            path,
            "no content type and not a recognisable collector config; "
            "structural checks skipped",
            "declare `content_type: text/yaml` to get the full checks",
        )
        # Declined, not checked: the caller records that no rules judged this body, which
        # is what the approver's acknowledgement is about (#119).
        return False
    if kind == "unknown" and _looks_like_collector_config(doc):
        verdict.add_warning(
            path,
            "content type is not declared; treating it as a collector config",
            "set `content_type: text/yaml` so nobody has to guess",
        )

    before = len(verdict.errors)
    check_structure(doc, path, verdict)
    if len(verdict.errors) == before:
        # Invariants and inventory warnings only make sense on a tree we could read.
        check_invariants(
            doc,
            path,
            verdict,
            allow_endpoint_change=getattr(ctx, "allow_endpoint_change", False),
            allowed_endpoints=getattr(ctx, "allowed_endpoints", None),
            allowed_exporters=getattr(ctx, "allowed_exporters", None),
        )
    check_inventory(doc, path, verdict, list(getattr(ctx, "agents", ()) or ()))
    return True


#: Sections whose children are **addresses** rather than settings, for this product.
#: The merge-collision check in the core uses it to tell "same key, two sources" from
#: "same scalar, merged": the collector refuses a duplicate component id, and osquery
#: refuses a duplicate scheduled query name.
address_sections = (
    "receivers",
    "processors",
    "exporters",
    "extensions",
    "connectors",
    "pipelines",  # service.pipelines.<id>
)

#: The engine name a profile file uses: `structure: {engine: otelcol}`.
def _register() -> None:
    from server.profiles.engines import register_structure_engine

    register_structure_engine("otelcol", validate)


_register()
