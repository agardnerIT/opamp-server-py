"""The pre-push validation pipeline (#100): structural, invariants, policy.

The server is about to hand a remote process the YAML it will boot from. Upstream is
explicit that a bad config leaves a collector "down until it is manually restarted",
so validation is a safety control, not a formality. It is also **not** a proof: the
server cannot know a config will not crash a collector. What it can do is refuse the
mistakes that are recognisable before delivery — malformed YAML, a config that cannot
parse, a pipeline pointing at a component that does not exist, a config that deletes
the `opamp` extension (orphaning the agent with no remote way back), an exporter
pointed at a host nobody allowlisted, and a policy violation. Everything else belongs
to rollout staging (#101) and the supervisor's own auto-revert (#99).

**Pure functions, no FastAPI, no SQLite.** The API, `opampctl --db` and the tests all
use exactly this code (the same rule as `server/manifest.py` and `server/reports.py`).

Order matters and is deliberate — cheap and unambiguous first, so an oversized
document is refused before it is parsed and a parse error is reported before a
hundred structural complaints about a tree that does not exist:

1. shape + size (:func:`check_shape`)
2. decode + parse with alias/depth/duplicate-key guards (:func:`parse_document`)
3. format layer: what the declared ``content_type`` means, and whether a parse failure
   is blocking or advisory (:func:`looks_yaml_like`)
4. **the profile's** structural rules — ``profile.structure`` in
   ``server/profiles/otelcol.py``; the collector's pipeline, invariant and inventory
   checks live there, not here (#119)
5. config-object-name warnings (:func:`check_object_names`)
6. merge-collision warnings (:func:`check_merge_collisions`)
7. policy gate (:func:`check_policy`)
8. a :class:`Verdict` that the caller stores on the revision

Steps 1–3, 5–8 are true of any config body and stay here. Everything OpenTelemetry
knows — `receivers`/`processors`/`exporters`, `service.pipelines`, the `opamp`
extension's protected invariants, the `memory_limiter` advice — is a *profile's* rules,
because a FluentBit or ossuary agent is not a broken collector. A profile with no
structure engine is not judged at all: the body is stored and relayed as opaque text, the
verdict says so, and :attr:`Verdict.structure_checked` is `False` so the approve step can
demand an acknowledgement from a human.

Severity policy, because "everything is an error" is how a validator gets switched off:

* **format** failures (oversize, non-UTF-8, duplicate keys, an alias bomb, a redaction
  marker) are errors for every profile — they are true of the bytes, not of a product;
* a declared type that will not parse is an error **when a profile judges structure**,
  and a warning when none does: an opaque body may be INI, a certificate or a script,
  and refusing it would make the feature useless for the agents it is meant to serve;
* what a *profile's* engine does with a parsed document is that engine's business, and
  it reports through this same verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import yaml
from loguru import logger

from server.config_vocabulary import ADDRESS_SECTIONS

DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_MAX_ALIASES = 10_000
DEFAULT_MAX_DEPTH = 50
# Expanded-node ceiling for one parsed document. A real collector config is a few
# thousand nodes; this only ever trips on an alias bomb.
DEFAULT_MAX_NODES = 100_000
# `AgentConfigObject.role` names a representation ("raw", "expanded"), not a payload.
MAX_ROLE_CHARS = 128

# Top-level sections of a collector config. `service` is what makes it runnable.

# Pipeline component sections, mapped to the top-level section that must define them.


#: Sections whose children are **addresses** in the merged config rather than plain
#: settings: a component id, a pipeline id, a scheduled query name. Two config sources
#: that define the same address do not override each other — the daemon refuses the
#: merge and reports `config_conflict` — which is why this set drives the collision
#: warning. `service.telemetry.logs` is deliberately *not* one of these: a scalar leaf
#: supplied by a second source is merged, not duplicated.

REDACTION_MARKER = "<redacted>"

_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"


# ---------------------------------------------------------------------- settings


def _env_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    """Read a positive integer setting, warning (not guessing silently) on garbage."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; using {default}")
        return default
    if value < 0 or (value == 0 and not allow_zero):
        logger.warning(f"{name}={raw!r} must be positive; using {default}")
        return default
    return value


def max_bytes() -> int:
    return _env_int("CONFIG_MAX_BYTES", DEFAULT_MAX_BYTES)


def max_aliases() -> int:
    return _env_int("CONFIG_MAX_ALIASES", DEFAULT_MAX_ALIASES)


def max_depth() -> int:
    return _env_int("CONFIG_MAX_DEPTH", DEFAULT_MAX_DEPTH)


def max_nodes() -> int:
    """Maximum *expanded* node count for one parsed document (alias-bomb ceiling)."""
    return _env_int("CONFIG_MAX_NODES", DEFAULT_MAX_NODES)


# ----------------------------------------------------------------------- verdict


@dataclass(frozen=True)
class Finding:
    """One problem, with a YAML path so a UI/CLI can point at the line (#100)."""

    path: str
    message: str
    hint: str = ""
    severity: str = "error"

    def to_dict(self) -> Dict[str, str]:
        return {
            "path": self.path,
            "message": self.message,
            "hint": self.hint,
            "severity": self.severity,
        }

    def as_text(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


@dataclass
class Verdict:
    """The answer to "may this be pushed?", stored on the revision (#99).

    `fingerprint` is a digest of everything that must not change between approve and
    apply (the findings and the policy outcome). #100 requires the pipeline to be
    re-run at apply time and the apply to be refused when the verdict moved — the
    schema, the policies and the agent's inventory can all have changed since Monday.
    """

    errors: List[Finding] = field(default_factory=list)
    warnings: List[Finding] = field(default_factory=list)
    policy: Dict[str, Any] = field(
        default_factory=lambda: {"status": "skipped", "violations": []}
    )
    normalised: Dict[str, Dict[str, str]] = field(default_factory=dict, repr=False)
    parsed: Dict[str, Any] = field(default_factory=dict, repr=False)
    #: Did a profile actually judge the **structure** of every accepted body? (#119)
    #: `False` for an agent whose profile has no structure engine: its config is opaque
    #: text to this server, so "valid" means well-formed and nothing more, and the approve
    #: step makes a human say they know that. Not part of `fingerprint()` — a body does
    #: not become different because a profile stopped interpreting it.
    structure_checked: bool = False
    checked_at: str = field(default_factory=lambda: _now())
    # #118: *which* rules produced this verdict, and how that was decided
    # (`pinned`/`declared`/`sniffed`/`fallback`, or `mixed` for a target set that
    # spans profiles). A verdict that cannot name its profile is a verdict nobody can
    # audit — and a structural "pass" for an agent the server never interpreted is the
    # one dangerous thing a spoofable type could buy. Deliberately **not** part of
    # `fingerprint()`: a stored revision approved before #118 must not become
    # un-appliable because the attribution appeared.
    profile: str = ""
    profile_source: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def error_messages(self) -> List[str]:
        return [finding.as_text() for finding in self.errors]

    @property
    def warning_messages(self) -> List[str]:
        return [finding.as_text() for finding in self.warnings]

    def add_error(self, path: str, message: str, hint: str = "") -> None:
        self.errors.append(Finding(path, message, hint, "error"))

    def add_warning(self, path: str, message: str, hint: str = "") -> None:
        self.warnings.append(Finding(path, message, hint, "warning"))

    def fingerprint(self) -> str:
        payload = {
            "errors": [(f.path, f.message) for f in self.errors],
            "warnings": [(f.path, f.message) for f in self.warnings],
            "policy": self.policy.get("status"),
            "violations": sorted(
                str(item) for item in (self.policy.get("violations") or [])
            ),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe verdict. Bodies are never included — hashes and paths only (D8)."""
        return {
            "ok": self.ok,
            "errors": [finding.to_dict() for finding in self.errors],
            "warnings": [finding.to_dict() for finding in self.warnings],
            "policy": dict(self.policy),
            "fingerprint": self.fingerprint(),
            "checked_at": self.checked_at,
            "profile": self.profile,
            "profile_source": self.profile_source,
            "structure_checked": self.structure_checked,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfigValidationError(ValueError):
    """The payload is not a config map we are willing to store or deliver."""

    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# -------------------------------------------------------------------------- parse


class ConfigParseError(ValueError):
    """A body that must not be loaded at all (bomb, duplicate key, depth)."""


class _BoundedSafeLoader(yaml.SafeLoader):
    """`SafeLoader` with three guards and no eval-shaped tags (#100).

    * an **alias budget**, counted while composing, so a document that resolves
      thousands of anchors is refused rather than trusted;
    * a **depth budget**, so a deeply nested document cannot exhaust the recursion
      stack (a `RecursionError` inside PyYAML is not catchable usefully and would
      surface as a 500);
    * **duplicate keys are an error**: PyYAML silently keeps the last one, which is
      how a reviewed config and a running config can differ. A mapping that uses a
      `<<` merge key is exempt, because a merge plus an explicit override is legal
      and indistinguishable from a duplicate after flattening.
    """

    def __init__(self, stream: Any):
        super().__init__(stream)
        self.alias_budget = max_aliases()
        self.depth_budget = max_depth()
        self._aliases = 0
        self._depth = 0
        self._path: List[str] = []

    # -- compose (aliases + depth) -------------------------------------------

    def compose_node(self, parent: Any, index: Any):  # noqa: D401 - PyYAML hook
        from yaml.events import AliasEvent

        if self.check_event(AliasEvent):
            self._aliases += 1
            if self._aliases > self.alias_budget:
                raise ConfigParseError(
                    f"document resolves more than {self.alias_budget} YAML aliases "
                    "(possible alias bomb); raise CONFIG_MAX_ALIASES only if the "
                    "document is trusted"
                )
        self._depth += 1
        try:
            if self._depth > self.depth_budget:
                raise ConfigParseError(
                    f"document nests deeper than {self.depth_budget} levels "
                    "(possible resource-exhaustion attempt)"
                )
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    # -- construct (duplicate keys) ------------------------------------------

    def construct_mapping(self, node: Any, deep: bool = False) -> Dict[Any, Any]:
        from yaml.constructor import ConstructorError

        if not isinstance(node, yaml.MappingNode):
            raise ConstructorError(
                None, None, f"expected a mapping node, but found {node.id}", node.start_mark
            )
        merged = any(key_node.tag == _YAML_MERGE_TAG for key_node, _ in node.value)
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
        mapping: Dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable key",
                    key_node.start_mark,
                )
            if not merged and key in mapping:
                path = ".".join(part for part in (self._path + [str(key)]) if part)
                raise ConfigParseError(
                    f"duplicate key at {path or '<root>'}: {key!r} appears twice and the "
                    "collector would silently use the last value, so a reviewed config "
                    "could differ from the running one — remove one of them"
                )
            self._path.append(str(key))
            try:
                mapping[key] = self.construct_object(value_node, deep=deep)
            finally:
                self._path.pop()
        return mapping


def parse_document(text: str) -> Any:
    """Parse one YAML/JSON document with :class:`_BoundedSafeLoader`.

    Raises :class:`ConfigParseError` for the shapes that must never be loaded.
    Everything else PyYAML raises comes back as `yaml.YAMLError`.
    """
    document = yaml.load(text, Loader=_BoundedSafeLoader)
    check_expansion(document)
    return document


def check_expansion(document: Any, budget: Optional[int] = None) -> None:
    """Refuse a document that would explode when *expanded* (alias bomb, #100).

    PyYAML composes anchors into shared nodes, so a small document does not consume
    memory at parse time — but the moment anything expands it (a JSON dump, a deep
    copy, the drift comparison) a billion-laughs document becomes a billion nodes.
    Counting the expanded size with an explicit stack and an early cutoff measures
    exactly that, in bounded time and memory.
    """
    limit = budget if budget is not None else max_nodes()
    stack = [document]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > limit:
            raise ConfigParseError(
                f"document expands to more than {limit} nodes through YAML aliases "
                "(possible alias bomb); raise CONFIG_MAX_NODES only if the document "
                "is trusted"
            )
        if isinstance(node, Mapping):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


#: Push-side allowlists: settings about *this server*, not about a product (#122).
#: They live here because `GET /config/posture` reports them; the collector's
#: rules are what consult them, and those are the profile's.
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


#: Content types that mean "INI", which is what Fluent Bit's config is (#123). Matched
#: by substring like `looks_yaml_like`, because `text/plain` and `""` are both in the wild
#: and a body that starts with a `[SECTION]` header is INI whatever it was labelled.
INI_CONTENT_TYPES = ("ini", "text/plain", "x-conf", "conf")


def accepts_ini(content_type: str, body: str = "") -> bool:
    """Whether a body should be read as INI by a profile that speaks INI (#123).

    Two signals, either of which is enough: the declared content type, or a leading
    `[SECTION]` header. The second one matters because `opamp-core` reports
    `content_type: text/plain` for a Fluent Bit config — a label that says nothing.
    """
    value = (content_type or "").lower()
    if any(marker in value for marker in INI_CONTENT_TYPES):
        return True
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        return bool(re.match(r"^\[[A-Za-z_][A-Za-z0-9_]*\]$", stripped))
    return False


def looks_yaml_like(content_type: str) -> str:
    """`"yaml"`, `"json"` or `"unknown"` from a content type string.

    An undeclared type is parsed anyway but a failure is only a warning: a body may
    legitimately be a PEM certificate or a shell script, and refusing those would
    make multi-object config maps unusable.
    """
    value = (content_type or "").lower()
    if "yaml" in value or "yml" in value:
        return "yaml"
    if "json" in value:
        return "json"
    return "unknown"


# ----------------------------------------------------------------------- structure


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _path(*parts: Any) -> str:
    return ".".join(str(part) for part in parts if str(part) != "")


# ---------------------------------------------------------------------- invariants


# ---------------------------------------------------------------------- inventory


def _reported_object_names(agent: Any) -> List[str]:
    """The `config_map` keys an agent reports, or `[]` when it has not reported one.

    The report is what the agent *received and applied*, so it is the only evidence the
    server has for the name an agent looks its config up by. `AgentState.effective_config`
    is the stored JSON **string** (or a `enc:` sealed body when a config key is set), so
    both spellings are accepted and anything unreadable is simply no evidence — a
    damaged or sealed report must not fail a validation.
    """
    report = getattr(agent, "effective_config", None)
    if not report:
        return []
    if isinstance(report, (str, bytes, bytearray)):
        try:
            report = json.loads(report)
        except (ValueError, TypeError):
            return []
    if not isinstance(report, Mapping):
        return []
    from server import effective_config as effective_config_module

    try:
        return [obj.key for obj in effective_config_module.objects(report)]
    except Exception:  # a damaged report is not a reason to fail a validation
        return []


def check_object_names(
    normalised: Mapping[str, Dict[str, Any]],
    verdict: Verdict,
    agents: Sequence[Any],
) -> None:
    """Warn when a config object is **keyed** differently from what the agent reports.

    The `config_map` key is not decoration: the spec types it
    `map<string, AgentConfigFile>` and leaves what the name means to the agent. An
    OpenTelemetry collector takes the unnamed object (`""`), while osquery's OpAMP
    extension accepts exactly one object and demands the key `osquery.json` — handed
    `""` it reports `expected a single osquery.json object, got ['']` and FAILS, which
    poisons the revision and pauses its rollout at batch 0.

    That failure is the protocol working, but it is a mis-addressed push rather than a
    bad config, and the server has the evidence to say so before delivery: what the
    target agents reported. A warning, not an error, because keying a *new* object is a
    legitimate thing to do on an agent that reports several.
    """
    present = [agent for agent in (agents or ()) if agent is not None]
    if not present:
        return
    proposed = {str(key) for key in normalised}
    # key -> agents that report a different name; the agent's names are kept for the hint.
    mismatched: Dict[str, List[str]] = {}
    reported_names: Dict[str, List[str]] = {}
    compared = 0
    for agent in present:
        names = _reported_object_names(agent)
        if not names:
            continue
        compared += 1
        agent_id = str(getattr(agent, "agent_id", "") or "")
        for key in sorted(proposed - set(names)):
            mismatched.setdefault(key, []).append(agent_id)
        reported_names.setdefault(agent_id, names)
    if not compared or not mismatched:
        return

    for key, agent_ids in sorted(mismatched.items()):
        affected = ", ".join(agent_id[:8] for agent_id in agent_ids[:3])
        more = f" (+{len(agent_ids) - 3} more)" if len(agent_ids) > 3 else ""
        theirs = sorted({name for name in reported_names[agent_ids[0]]})
        shown = ", ".join(
            "the unnamed object" if name == "" else f"`{name}`" for name in theirs
        )
        verdict.add_warning(
            key or "<empty key>",
            f"{len(agent_ids)} target agent(s) report a different config object "
            f"name: {affected}{more} report {shown}",
            "the agent looks its config up by this name, so a differently keyed "
            "revision is refused (osquery: `expected a single osquery.json object`) "
            "and pauses the rollout at batch 0 — key the revision to the name the "
            "agent reports",
        )


# ------------------------------------------------------------- merge collisions


def _addresses(
    document: Any, sections: Sequence[str] = ADDRESS_SECTIONS, path: str = ""
) -> set:
    """Every address a document defines, as dotted paths (see `address_sections`)."""
    found: set = set()
    if isinstance(document, Mapping):
        for key, value in document.items():
            child = f"{path}.{key}" if path else str(key)
            if path.rsplit(".", 1)[-1] in sections:
                found.add(child)
            found |= _addresses(value, sections, child)
    elif isinstance(document, (list, tuple)):
        for item in document:
            found |= _addresses(item, sections, path)
    return found


def _parse_or_none(body: Any) -> Any:
    """Parse a body for comparison only — anything unreadable is no evidence."""
    if not isinstance(body, (str, bytes, bytearray)):
        return None
    try:
        return parse_document(bytes(body).decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except (ConfigParseError, yaml.YAMLError, UnicodeDecodeError):
        return None


def _reported_documents(agent: Any) -> List[Any]:
    """The documents an agent reports as its effective config, JSON included.

    `accept_json=True` is what makes this check work for osquery: it reports its
    config as `application/json`, and `schedule.<query name>` is exactly the address
    that collides with a local config file.
    """
    report = getattr(agent, "effective_config", None)
    if not report:
        return []
    if isinstance(report, (str, bytes, bytearray)):
        try:
            report = json.loads(report)
        except (ValueError, TypeError):
            return []
    if not isinstance(report, Mapping):
        return []
    from server import effective_config as effective_config_module

    try:
        return [
            obj.document
            for obj in effective_config_module.objects(report, accept_json=True)
            if obj.parsed
        ]
    except Exception:  # a damaged report is not a reason to fail a validation
        return []


def check_merge_collisions(
    normalised: Mapping[str, Dict[str, Any]],
    verdict: Verdict,
    agents: Sequence[Any],
    previous_bodies: Optional[Mapping[str, Sequence[str]]] = None,
    address_sections: Sequence[str] = ADDRESS_SECTIONS,
) -> None:
    """Warn when this revision defines an address another source already provides.

    Remote config is **per source and additive** (opamp-spec §Configuration Objects):
    the agent merges every source it is handed, so an address defined twice is not an
    override, it is an ambiguity. osquery reports `config_conflict` with "the same
    query name is scheduled more than once: battery_stats"; the collector refuses the
    duplicate component id. Either way the config applied, the agent may still report
    `healthy: true`, and the effective config is **not** the one approved here — the
    rollout pauses with no config to blame.

    The server cannot remove a key it does not own, but it does have the evidence to
    warn before delivery: the agent's reported effective config, minus the addresses
    *this* server already pushes (`previous_bodies`, the assignment currently in
    force). Whatever is left in the report was supplied by someone else — a local
    flag, a mounted file, another controller — and re-defining it here collides.

    A warning, not an error: a server-side `config_map` mutation may legitimately
    address an agent whose merge semantics *is* last-source-wins, and the evidence is
    only as fresh as the last report.
    """
    present = [agent for agent in (agents or ()) if agent is not None]
    if not present:
        return

    proposed: Dict[str, set] = {}
    for key, entry in normalised.items():
        document = _parse_or_none(entry.get("body"))
        addresses = (
            _addresses(document, address_sections) if document is not None else set()
        )
        if addresses:
            proposed[str(key)] = addresses
    if not proposed:
        return

    previous = previous_bodies or {}
    # key -> address -> agent ids that define it from another source
    collisions: Dict[str, Dict[str, List[str]]] = {}
    compared = 0
    for agent in present:
        documents = _reported_documents(agent)
        if not documents:
            continue
        compared += 1
        agent_id = str(getattr(agent, "agent_id", "") or "")
        reported: set = set()
        for document in documents:
            reported |= _addresses(document, address_sections)
        owned: set = set()
        for body in previous.get(agent_id, ()) or ():
            document = _parse_or_none(body)
            if document is not None:
                owned |= _addresses(document, address_sections)
        foreign = reported - owned
        for key, addresses in proposed.items():
            for address in addresses & foreign:
                collisions.setdefault(key, {}).setdefault(address, []).append(agent_id)

    if not compared or not collisions:
        return

    for key in sorted(collisions):
        by_address = collisions[key]
        addresses = sorted(by_address)
        agent_ids = sorted({agent for ids in by_address.values() for agent in ids})
        shown = ", ".join(f"`{address}`" for address in addresses[:3])
        shown += f" (+{len(addresses) - 3} more)" if len(addresses) > 3 else ""
        affected = ", ".join(agent_id[:8] for agent_id in agent_ids[:3])
        affected += f" (+{len(agent_ids) - 3} more)" if len(agent_ids) > 3 else ""
        verdict.add_warning(
            key or "<empty key>",
            f"{len(addresses)} address(es) in this config are also defined by a config "
            f"source the server does not manage, on {len(agent_ids)} target agent(s): "
            f"{shown} ({affected})",
            "the agent merges every source it is handed, so the same address in two of "
            "them is a conflict rather than an override: it applies this revision and "
            "reports `config_conflict` (osquery: 'the same query name is scheduled "
            "more than once'), then the rollout pauses. Remove the address from the "
            "other source — a local config file or flag on the agent host — or from "
            "this revision; this server cannot delete a key it does not own",
        )


# ------------------------------------------------------------------------ policy


def check_policy(
    documents: Mapping[str, Any],
    *,
    agent_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run the project's OPA policies against the **proposed** config (#100 stage 5).

    Three outcomes, and they are deliberately distinguishable:

    * ``skipped`` — OPA is disabled. Never reported as a pass: a silent "compliant"
      from a gate that did not run is worse than no gate.
    * ``violations`` — policies ran and complained; the caller blocks.
    * ``error`` — policies were enabled but could not be evaluated. Fail closed: an
      unavailable policy engine is a reason to stop, and an approver can override it
      with a recorded reason.
    """
    from server.opa_client import get_opa_client

    client = get_opa_client()
    if client is None:
        return {
            "status": "skipped",
            "violations": [],
            "reason": "OPA is disabled (OPA_ENABLED=false) — the policy gate did not run",
        }

    violations: List[Any] = []
    evaluated: List[str] = []
    for key, doc in documents.items():
        result = client.evaluate(
            {
                "config": doc,
                "config_key": key,
                "agent_ids": list(agent_ids or []),
            }
        )
        if result.get("compliant") is None:
            return {
                "status": "error",
                "violations": [],
                "reason": result.get("error") or "OPA evaluation failed",
            }
        evaluated.extend(item.get("name", "") for item in result.get("policy_results") or [])
        for violation in result.get("violations") or []:
            message = violation.get("msg") if isinstance(violation, dict) else str(violation)
            violations.append(message or str(violation))

    if violations:
        return {"status": "violations", "violations": violations, "policies": evaluated}
    return {"status": "pass", "violations": [], "policies": evaluated}


# ----------------------------------------------------------------------- pipeline


def check_shape(
    config_map: Any,
    verdict: Verdict,
    *,
    allow_empty: bool = False,
) -> Dict[str, Dict[str, str]]:
    """Stage 1: shape, size and the redaction foot-gun. Returns the normalised map."""
    if not isinstance(config_map, dict):
        verdict.add_error(
            "",
            "config_map must be an object mapping names to config bodies",
            'the spec shape is `{"": {"body": "...", "content_type": "text/yaml"}}`',
        )
        return {}
    if not config_map and not allow_empty:
        verdict.add_error(
            "",
            "config_map is empty: use DELETE to withdraw a config, not an empty push",
            "an empty remote config tells the agent to run its local config",
        )
        return {}

    normalised: Dict[str, Dict[str, str]] = {}
    total = 0
    for raw_key, entry in config_map.items():
        key = str(raw_key) if raw_key is not None else ""
        if not isinstance(entry, dict):
            verdict.add_error(key, "expected an object with a 'body' field")
            continue
        body = entry.get("body")
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        if not isinstance(body, str):
            verdict.add_error(key, "'body' is required and must be a string")
            continue
        content_type = entry.get("content_type") or ""
        if not isinstance(content_type, str):
            verdict.add_error(key, "'content_type' must be a string")
            continue
        # v0.20.0 `AgentConfigObject.role` (#66/#68): agent-defined, so it is carried
        # through uninterpreted — but it is bounded, because the size cap below only
        # counts bodies and an unbounded role string would ride to the agent outside it.
        role = entry.get("role") or ""
        if not isinstance(role, str):
            verdict.add_error(key, "'role' must be a string")
            continue
        if len(role) > MAX_ROLE_CHARS:
            verdict.add_error(
                key,
                f"'role' is {len(role)} characters, over MAX_ROLE_CHARS={MAX_ROLE_CHARS}",
                "a role names a representation ('raw', 'expanded'); it is not data",
            )
            continue
        try:
            encoded = body.encode("utf-8")
        except UnicodeEncodeError as exc:
            verdict.add_error(
                key,
                f"body is not valid UTF-8 ({exc.reason})",
                "a config body is text; send bytes as base64 of a text document instead",
            )
            continue
        total += len(encoded)
        normalised[key] = {"body": body, "content_type": content_type}
        if role:
            # Only when set, so a role-less map hashes and stores exactly as before.
            normalised[key]["role"] = role

    limit = max_bytes()
    if total > limit:
        verdict.add_error(
            "",
            f"config map is {total} bytes, over CONFIG_MAX_BYTES={limit}",
            "a collector config is kilobytes; a megabyte means something is embedded "
            "that does not belong in a config",
        )

    # #98: the most dangerous foot-gun in the whole feature.
    if config_map and _detect_redaction(config_map):
        verdict.add_error(
            "",
            "config body contains a redaction marker ('<redacted>' or the #98 marker): "
            "a redacted document is a display artefact and pushing it would wipe "
            "credentials across the fleet. Submit the real body.",
        )

    # The mirror image of the check above, and it comes from the same place: the
    # protocol's `AgentConfigFile.body` is `bytes`, so `MessageToDict` renders every
    # body it is reported back as base64. A body copied from a raw report or an API
    # response is therefore base64 *of* a config — stored as-is it is delivered as-is,
    # and the agent boots a config that is one base64 line.
    #
    # A warning, not an error: by the protocol's letter the body is text, and a body
    # that genuinely is base64 is not impossible for a non-collector agent. The point
    # is that the operator reads why the config looks wrong before a rollout, not that
    # the server guesses. `decode_text_body` is deliberately conservative (length,
    # padding, printable ratio), so real YAML/JSON never matches.
    for key, entry in normalised.items():
        if _decoded_base64(entry["body"]) is not None:
            verdict.add_warning(
                key,
                "body is base64 of a text document: the agent would receive the base64, "
                "not the config inside it",
                "a config body is text here — base64 is only how a protobuf `bytes` "
                "field is rendered when a report is read back. Push the decoded "
                "document; the UI's Prefill decodes an agent report for you.",
            )
    return normalised


def _decoded_base64(body: str) -> Optional[str]:
    """The text a base64-shaped body decodes to, or `None` when it is not base64."""
    from server import redaction

    decoded = redaction.decode_text_body((body or "").strip())
    return decoded if decoded and decoded.strip() else None


def _detect_redaction(config_map: Any) -> bool:
    from server import redaction

    return bool(redaction.contains_redaction(config_map))


def _target_profile(agents: Optional[Sequence[Any]]) -> Tuple[str, str]:
    """Which profile the target agents resolve to, and how it was decided (#118).

    Returns `("", "")` when the verdict is not agent-scoped (`POST /config/validate`
    with no `agent_id`) — an unattributed verdict is honest about being unattributed —
    and `("mixed", "mixed")` when the targets disagree, because picking the majority
    would let one agent's type speak for another's config.
    """
    present = [agent for agent in (agents or ()) if agent is not None]
    if not present:
        return "", ""
    from server import profiles

    counts: Dict[Tuple[str, str], int] = {}
    for agent in present:
        resolution = profiles.resolve_cached(agent)
        key = (resolution.name, resolution.source)
        counts[key] = counts.get(key, 0) + 1
    if len({name for name, _ in counts}) > 1:
        return "mixed", "mixed"
    (name, source), _ = max(counts.items(), key=lambda item: item[1])
    return name, source


def _annotate_profile(
    verdict: Verdict, profile_name: str, source: str, agents: Sequence[Any]
) -> None:
    """Record the profile on the verdict, and say so when it is not interpreted.

    #118 attributed the verdict to a profile; #119 made that attribution *decide* the
    rules. So the non-interpreting case no longer means "the findings below are
    collector-specific" — there are no findings below, because no engine ran — it means
    "nothing judged this body's structure, so a pass is only about the bytes". For every
    target whose profile has an engine this adds nothing, which is what keeps the
    otelcol verdict byte-identical.
    """
    verdict.profile = profile_name
    verdict.profile_source = source
    if not profile_name:
        return
    from server import profiles

    if profile_name == "mixed":
        verdict.add_warning(
            "",
            "the target agents resolve to more than one profile "
            f"({', '.join(sorted({profiles.resolve_cached(a).name for a in agents}))}); "
            "the findings below are collector-specific and may not describe every target",
            "pin the agents that are not OpenTelemetry Collectors "
            "(PUT /agent/{id}/type) so their configs are judged on their own terms",
        )
        return
    profile = profiles.get(profile_name)
    if profile is not None and not profile.validates_structure:
        verdict.add_warning(
            "",
            f"the target profile is {profile_name!r}: no structural checks ran — the "
            "server does not interpret this agent's configuration, so the body is stored "
            "and relayed as opaque text. The format checks (size, encoding, duplicate "
            "keys, redaction markers) and the policy gate still apply",
            "an approver must confirm an unchecked body before it is applied "
            "(confirm_unchecked_structure); to judge this agent's config on its own "
            "terms, its profile needs a structure engine (#119)",
        )


@dataclass(frozen=True)
class StructureContext:
    """What a profile's structural rules get besides the document itself (#119).

    Deliberately a value object: an engine is a pure function of `(document, context,
    verdict)`, so the collector's rules can be exercised without a server, a registry or
    a request — the same property that made them testable where they used to live.
    """

    content_type: str = ""
    #: The body as the agent/server stored it, and why it did not parse as YAML/JSON
    #: (`""` when it did). A profile whose product does not use YAML judges this instead:
    #: Fluent Bit's config is INI, and `fluent-bit.conf` is a perfectly valid body that
    #: PyYAML refuses (#123).
    body: str = ""
    parse_error: str = ""
    allow_endpoint_change: bool = False
    allowed_endpoints: Optional[Tuple[str, ...]] = None
    allowed_exporters: Optional[Tuple[str, ...]] = None
    agents: Tuple[Any, ...] = ()


def _structure_engine(profile_name: str) -> Tuple[Optional[Callable], Any]:
    """The rules that judge this fleet's bodies, and the profile they belong to (#119).

    Unattributed deployments keep the historical behaviour: `POST /config/validate` with
    no `agent_id` is the common "just check this config" call, and resolving *no* profile
    must not turn it into a no-op. The shipped bootstrap profile (the same one whose
    snippet a new agent is given) answers in that case — and if that profile has no
    structure engine, then nothing does, which is the honest answer for a dictionary with
    no interpreter in it.
    """
    from server import profiles

    name = (profile_name or "").strip()
    profile = profiles.get(name) if name and name != "mixed" else None
    if profile is None:
        profile = profiles.bootstrap_default()
    return getattr(profile, "structure", None), profile


#: The profile a verdict was produced under, and how it was decided (#118).


def check_config_map(
    config_map: Any,
    *,
    allow_empty: bool = False,
    allow_endpoint_change: bool = False,
    allowed_endpoints: Optional[Sequence[str]] = None,
    allowed_exporters: Optional[Sequence[str]] = None,
    agents: Optional[Sequence[Any]] = None,
    run_policy: bool = True,
    previous_bodies: Optional[Mapping[str, Sequence[str]]] = None,
    profile: Optional[str] = None,
) -> Verdict:
    """Run the whole pipeline and return the verdict. **Never raises.**

    `agents` is a sequence of `AgentState` (or anything with `agent_id` and
    `available_components`) so the inventory warnings are per-target.
    `previous_bodies` maps an agent id to the config bodies this server already
    pushes to it (its current assignment), so :func:`check_merge_collisions` can tell
    *our* keys from another source's.

    `profile` names the rules to judge the body by (#119). Without it the profile is
    resolved from `agents` — the existing call sites (API, `opampctl --db`, MCP, the
    tests) keep working unchanged — and with neither, the shipped bootstrap profile
    answers, because "validate this for me" must not become a no-op.
    """
    verdict = Verdict()
    normalised = check_shape(config_map, verdict, allow_empty=allow_empty)
    if not normalised:
        return verdict

    agent_list = list(agents or ())
    if profile:
        _annotate_profile(verdict, profile, "declared", agent_list)
        engine, engine_profile = _structure_engine(profile)
    else:
        resolved, source = _target_profile(agent_list)
        _annotate_profile(verdict, resolved, source, agent_list)
        engine, engine_profile = _structure_engine(resolved)
    # Which keys are addresses is the product's vocabulary when it declares one (#119),
    # and the shipped defaults otherwise: osquery's `schedule` has nothing to do with a
    # collector's `receivers`, so a single global answer was always a compromise.
    address_sections = (
        tuple(getattr(engine_profile, "address_sections", ()) or ()) or ADDRESS_SECTIONS
    )
    # A body judged by no engine is only ever well-formed, and the approve step says so.
    verdict.structure_checked = engine is not None

    check_object_names(normalised, verdict, agent_list)
    check_merge_collisions(
        normalised, verdict, agent_list, previous_bodies, address_sections
    )

    context = StructureContext(
        allow_endpoint_change=allow_endpoint_change,
        allowed_endpoints=tuple(allowed_endpoints) if allowed_endpoints is not None else None,
        allowed_exporters=tuple(allowed_exporters) if allowed_exporters is not None else None,
        agents=tuple(agent_list),
    )

    for key, entry in normalised.items():
        body = entry["body"]
        kind = looks_yaml_like(entry["content_type"])
        try:
            doc = parse_document(body)
        except ConfigParseError as exc:
            # Format-level refusals (bytes, duplicate keys, an alias bomb) are errors for
            # every profile: they are true of the bytes, not of a product.
            verdict.add_error(key, str(exc), "fix the document and re-validate")
            continue
        except yaml.YAMLError as exc:
            # The historical wording, kept because callers and tests match on it. It is
            # only ever emitted when a body really could not be read as *any* config the
            # target understands: a profile whose own engine reads the body (Fluent Bit's
            # INI, #123) returns above and never reaches this line.
            message = f"body is not valid {entry['content_type'] or 'YAML'}: {exc}"
            if engine is None:
                # A profile that does not interpret the body at all: it may be INI, a
                # certificate or a shell script. Advisory by contract.
                verdict.add_warning(key, message)
                continue
            # A declared type that will not parse. Before refusing, give the profile's
            # engine the raw body: a product that does not use YAML (Fluent Bit reads
            # INI) is judged by its own rules, and only a profile that declines leaves
            # the parse failure standing.
            if engine(
                None,
                key,
                replace(
                    context, content_type=entry["content_type"] or "", body=body,
                    parse_error=str(exc),
                ),
                verdict,
            ):
                continue
            # The engine declined. Nobody judged this object, so the approver is told
            # exactly that (they are asked to acknowledge an unchecked body).
            verdict.structure_checked = False
            if kind == "unknown":
                # Historical behaviour for an undeclared/opaque type: advisory. A PEM
                # certificate or a shell script alongside the real config is legal and
                # was never refusable — #123 must not make it fatal on the way past.
                verdict.add_warning(key, message)
                continue
            verdict.add_error(
                key,
                message,
                "the agent errors out and stays down until it is restarted by hand, "
                "so an unparseable body is refused",
            )
            continue

        if doc is None:
            # Nothing but whitespace or comments is not a document, whatever the product
            # is: format-level, so it is refused for every profile instead of being left
            # to whichever engine happens to look at it.
            verdict.add_error(key, "body is empty", "a revision must carry a document")
            continue

        verdict.parsed[key] = doc
        if engine is None:
            # Opaque by declaration: the "no structural checks ran" warning is on the
            # verdict already (`_annotate_profile`), and there is nothing to add per key.
            continue
        if not engine(
            doc,
            key,
            replace(context, content_type=entry["content_type"] or "", body=body),
            verdict,
        ):
            # The engine looked at it and declined (not its product's shape), which is
            # the same fact for the approver as "no engine at all".
            verdict.structure_checked = False

    if run_policy and verdict.parsed and not verdict.errors:
        policy = check_policy(
            verdict.parsed,
            agent_ids=[getattr(agent, "agent_id", "") for agent in (agents or ())],
        )
        verdict.policy = policy
        if policy.get("status") == "violations":
            for index, violation in enumerate(policy.get("violations") or []):
                verdict.add_error(
                    f"policy[{index}]",
                    f"policy violation: {violation}",
                    "an approver may override with a recorded reason if this is intentional",
                )
        elif policy.get("status") == "error":
            verdict.add_error(
                "policy",
                f"the policy gate could not run: {policy.get('reason')}",
                "fail closed: fix OPA/OPA_URL, or an approver may override with a recorded reason",
            )
    elif run_policy and verdict.parsed:
        verdict.policy = {
            "status": "skipped",
            "violations": [],
            "reason": "structural errors are blocking; policies were not evaluated",
        }

    verdict.normalised = normalised
    return verdict


def validate_config_map(
    config_map: Any,
    allow_empty: bool = False,
    **kwargs: Any,
) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Compatibility wrapper: raise on any error, return `(normalised, warnings)`.

    Raises :class:`ConfigValidationError` naming **every** problem at once — an
    operator fixing one error per request is how a config push takes an afternoon.
    """
    verdict = check_config_map(config_map, allow_empty=allow_empty, **kwargs)
    if not verdict.ok:
        raise ConfigValidationError(verdict.error_messages)
    return verdict.normalised, verdict.warning_messages


# -------------------------------------------------------------------------- drift


def contains_document(desired: Any, actual: Any, path: str = "") -> List[str]:
    """YAML paths in `desired` that `actual` does not satisfy (drift detection, #99).

    Used to answer "is the agent running the config we asked for?" without comparing
    hashes of two documents that are legitimately different: the supervisor merges
    and re-serialises, so a byte comparison would report drift on every healthy agent.
    Mappings must contain every desired key; lists must contain every desired item;
    scalars must be equal.
    """
    missing: List[str] = []
    if isinstance(desired, Mapping):
        if not isinstance(actual, Mapping):
            return [path or "<root>"]
        for key, value in desired.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in actual:
                missing.append(child)
                continue
            missing.extend(contains_document(value, actual[key], child))
        return missing
    if isinstance(desired, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            return [path or "<root>"]
        actual_items = list(actual)
        for index, value in enumerate(desired):
            if not any(not contains_document(value, candidate, path) for candidate in actual_items):
                missing.append(f"{path}[{index}]")
        return missing
    if desired != actual:
        return [path or "<root>"]
    return []


def document_drift(desired_bodies: Sequence[Tuple[str, str]], effective_bodies: Sequence[str]) -> List[str]:
    """Compare every desired body with the agent's reported effective bodies.

    A desired body matches when *some* effective body contains it. That tolerates the
    single-file collector case and the composed-config case without pretending to
    understand either.
    """
    parsed_effective: List[Any] = []
    for body in effective_bodies:
        try:
            parsed_effective.append(parse_document(body))
        except (ConfigParseError, yaml.YAMLError):
            continue
    if not parsed_effective:
        return []

    missing: List[str] = []
    for label, body in desired_bodies:
        try:
            desired = parse_document(body)
        except (ConfigParseError, yaml.YAMLError):
            continue
        prefix = label or ""
        best: Optional[List[str]] = None
        for actual in parsed_effective:
            candidate = contains_document(desired, actual, prefix)
            if not candidate:
                best = []
                break
            if best is None or len(candidate) < len(best):
                best = candidate
        missing.extend(best if best is not None else [prefix or "<root>"])
    return missing


def __getattr__(name: str):
    """Keep the pre-#119 names importable from here, and resolve them to the engine.

    The collector's rules moved to `server/profiles/otelcol.py`, but `check_structure`,
    `check_invariants`, `check_inventory`, `inventory_components`,
    `allowed_exporter_hosts` and `allowed_opamp_endpoints` were semi-public — the docs,
    the old module path and a year of muscle memory point at this module. Resolving them lazily
    keeps those callers working without copying a single rule back into the core.
    """
    if name in _PROFILE_OWNED:
        from server.profiles import otelcol

        return getattr(otelcol, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Names that moved to a profile engine in #119 and are still importable from here.
_PROFILE_OWNED = frozenset(
    {
        "check_invariants",
        "check_inventory",
        "check_structure",
        "inventory_components",
    }
)

__all__ = [
    "ADDRESS_SECTIONS",
    "ConfigParseError",
    "ConfigValidationError",
    "Finding",
    "StructureContext",
    "Verdict",
    "check_config_map",
    "check_merge_collisions",
    "check_policy",
    "check_shape",
    "accepts_ini",
    "contains_document",
    "document_drift",
    "looks_yaml_like",
    "max_bytes",
    "parse_document",
    "validate_config_map",
] + sorted(_PROFILE_OWNED)
