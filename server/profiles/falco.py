"""Falco structural rules and rulesfile inventory — the `falco` engine.

The `falco-opamp` sidecar (github.com/agardnerIT/falco-opamp) reports the files it
owns as `application/yaml` under two config object keys, and applies them through
Falco's own reload-control API:

* **`falco.yaml`** — the complete main config, written to the sidecar's `--config`;
* **`rules.yaml`** — the managed rules file, referenced by `rules_files` in the
  pushed main config.

So this engine judges the YAML shape of those two documents by their object key, and
the rules the managed file declares are the inventory: a `rule:` entry in `rules.yaml`
is something this agent runs, exactly as a collector's components are the things it
runs. Everything derived from the pushed documents is `used: true` by definition,
which is why this profile does not declare the "heavy" (unused components) report.

**Best effort, and it says so.** This server cannot run `falco --validate` on the
agent host — the sidecar does that itself when a falco binary is reachable — so the
rules here judge document shape, not rule semantics: a condition naming a macro that
does not exist is Falco's own load error, and it reaches us as a `rejected_generation`
in `GET /reload`, not as something this file could have caught. The rules that *are*
errors are the ones a rulesfile cannot be read without (`rule`, `condition`, `output`,
`desc`) and the shape mistakes the format layer would otherwise call broken collector
YAML.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from server.config_validation import Verdict

#: The config object keys the sidecar's contract names. A push with only other keys
#: is refused by the sidecar itself; the engine judges the two it accepts.
MAIN_CONFIG_KEY = "falco.yaml"
RULES_KEY = "rules.yaml"

#: The keys that make a parsed YAML document recognisably a Falco main config when
#: the object key is unhelpful (`""`, or a renamed push).
_FINGERPRINT_KEYS = frozenset(
    {"rules_files", "rules", "engine", "watch_config_files", "reload_control", "webserver"}
)

#: Top-level keys the engine judges or recognises. Falco's own config surface is far
#: larger than any list this file can keep honest, so the unknown-key warning the
#: osquery engine gives is deliberately **absent here**: an unseen key is far more
#: likely to be Falco's vocabulary than a typo.
_KNOWN_SECTIONS = frozenset(
    {
        "rules_files",
        "rules",
        "engine",
        "watch_config_files",
        "reload_control",
        "webserver",
        "rule",
        "outputs",
        "grpc",
        "metrics",
        "priority",
        "time_format_iso8601",
        "buffered_outputs",
        "outputs_queue",
        "plugins",
        "plugin_config",
        "load_plugins",
        "base_syscalls",
        "syscall_event_timeouts",
        "syscall_event_drops",
        "modern_ebpf",
        "kmod",
        "nodriver",
        "replay",
        "container_engines",
        "node_labels",
        "hostname",
    }
)

#: The keys a Falco rule entry cannot be read without (`falco --validate` refuses a
#: rule that lacks any of these; `priority` and the rest have defaults).
_RULE_REQUIRED_KEYS = ("rule", "condition", "output", "desc")


def _is_main_config(key: str, content_type: str, doc: Any) -> bool:
    """Whether this object is a Falco main config for this engine to judge."""
    if key == MAIN_CONFIG_KEY:
        return True
    if key == RULES_KEY:
        return False
    # An object pushed under a different key: judge it only if its vocabulary names
    # Falco's, so a renamed push is still understood and a foreign body is declined.
    return isinstance(doc, Mapping) and bool(_FINGERPRINT_KEYS.intersection(doc))


def _is_rulesfile(key: str, doc: Any) -> bool:
    if key == RULES_KEY:
        return True
    return (
        key not in (MAIN_CONFIG_KEY,)
        and isinstance(doc, List)
        and bool(doc)
        and all(isinstance(e, Mapping) for e in doc)
    )


def _judge_main_config(doc: Mapping, path: str, verdict: Verdict) -> None:
    """Shape rules for the pushed main config. Semantics are Falco's, not ours."""
    files = doc.get("rules_files")
    if files is not None:
        if not isinstance(files, list) or any(
            not isinstance(f, str) or not f.strip() for f in files
        ):
            verdict.add_error(
                f"{path}.rules_files" if path else "rules_files",
                "`rules_files` must be a list of non-empty file paths",
                "write `rules_files: [/etc/falco/falco_rules.yaml, …]`",
            )
        elif isinstance(files, list):
            # The sidecar's contract: the managed rules file must be loaded, or a
            # pushed `rules.yaml` lands on disk and runs nothing.
            if not any(str(f).endswith("rules.yaml") for f in files):
                verdict.add_warning(
                    f"{path}.rules_files" if path else "rules_files",
                    "`rules_files` does not load a managed `rules.yaml` file",
                    "the sidecar writes its managed rules to `--rules` and applies "
                    "them only if `rules_files` includes that path",
                )

    watch = doc.get("watch_config_files")
    if watch is True:
        # Not wrong for a fleet falco without a sidecar — but here it races the
        # apply protocol: the watcher reloads on every write, double-counting
        # generations, while the POST/GET cycle is the one with feedback.
        verdict.add_warning(
            f"{path}.watch_config_files" if path else "watch_config_files",
            "`watch_config_files: true` makes Falco reload on every file write",
            "the sidecar applies pushes through `POST /reload` and verifies them "
            "over `GET /reload`; run with `watch_config_files: false` so the "
            "reload with feedback is the only one",
        )

    reload_control = doc.get("reload_control")
    if reload_control is not None:
        if not isinstance(reload_control, Mapping):
            verdict.add_error(
                f"{path}.reload_control" if path else "reload_control",
                f"`reload_control` must be an object, not {type(reload_control).__name__}",
                "write `reload_control: {enabled: true, socket: /run/falco/control.sock}`",
            )
        elif reload_control.get("enabled") is False:
            # Not always an error: a deployment may start Falco with
            # `-o reload_control.enabled=true`, which is exactly the shape the
            # sidecar's `--require-reload-control=false` exists for. The approver
            # reads the warning; the sidecar's own guard is what refuses a brick.
            verdict.add_warning(
                f"{path}.reload_control.enabled" if path else "reload_control.enabled",
                "`reload_control.enabled: false` in the file",
                "a push can only be applied if Falco was started with "
                "`-o reload_control.enabled=true` overriding this; otherwise the "
                "sidecar has no reload surface and config delivery is dead",
            )

    embedded = doc.get("rules")
    if embedded is not None:
        if not isinstance(embedded, list):
            verdict.add_error(
                f"{path}.rules" if path else "rules",
                f"embedded `rules` must be a list, not {type(embedded).__name__}",
                "write `rules: [- rule: …, condition: …, output: …]` or use `rules_files`",
            )
        else:
            _judge_rules_entries(embedded, f"{path}.rules" if path else "rules", verdict)

    if files is None and embedded is None:
        verdict.add_warning(
            path or "<root>",
            "no `rules_files` and no `rules`: this config runs no rules",
            "a Falco config without rules detects nothing; add a `rules_files` list",
        )


def _judge_rules_entries(entries: Any, path: str, verdict: Verdict) -> None:
    """Shape rules for a list of rule/list/macro entries. Falco's format, best effort."""
    for index, entry in enumerate(entries):
        entry_path = f"{path}[{index}]"
        if not isinstance(entry, Mapping):
            verdict.add_error(
                entry_path,
                f"entry {index} is {type(entry).__name__}, not an object",
                "a rulesfile is a list of `rule`/`list`/`macro` objects",
            )
            continue
        kind = entry.get("rule")
        if kind is None and "list" not in entry and "macro" not in entry:
            verdict.add_error(
                entry_path,
                "entry has no `rule`, `list` or `macro` key",
                "every entry in a rulesfile declares one of those three",
            )
            continue
        if kind is None:
            continue  # lists and macros are shape-checked by falco --validate on the host
        for required in _RULE_REQUIRED_KEYS:
            value = entry.get(required)
            if not isinstance(value, str) or not value.strip():
                verdict.add_error(
                    f"{entry_path}.{required}",
                    f"`rule` entry {index} has no `{required}`",
                    f"write `{required}: …` — a rule without it is refused by "
                    "`falco --validate` and reported as a rejected reload",
                )


def validate(doc: Any, path: str, ctx: Any, verdict: Verdict) -> bool:
    """Judge one pushed object by its Falco shape. Returns whether it was judged.

    `path` is the config object key (`falco.yaml` or `rules.yaml` per the sidecar's
    contract) and `ctx.body` is the raw text. Declining (`False`) is the honest
    answer for a body that is neither: the caller then records that no structural
    check ran rather than inventing findings about a collector config.
    """
    key = path or ""
    if doc is None:
        # Not a parseable document at all. The format layer already has the parse
        # error; declining leaves that message standing.
        return False

    if _is_rulesfile(key, doc):
        if not isinstance(doc, list):
            return False
        _judge_rules_entries(doc, key, verdict)
        return True

    # A main config: the right key, or a document whose vocabulary names Falco's.
    # `application/yaml` is shared with the collector, so the content type can
    # never be the discriminator — the document's own keys are.
    fingerprinted = isinstance(doc, Mapping) and bool(
        _FINGERPRINT_KEYS.intersection(doc)
    )
    if key != MAIN_CONFIG_KEY and not fingerprinted:
        return False

    if not isinstance(doc, Mapping):
        verdict.add_error(
            key,
            f"a Falco main config is a YAML object, not {type(doc).__name__}",
            "the document is `rules_files: […]` plus Falco's own settings",
        )
        return True

    if not fingerprinted:
        # The right key, none of Falco's vocabulary: the same honest decline the
        # osquery engine gives (`#141`) — no structural check ran.
        verdict.add_warning(
            key,
            "not a Falco configuration (none of `rules_files`, `rules`, `engine`, "
            "`watch_config_files`, `reload_control`, `webserver`); structural "
            "checks skipped",
            "a Falco config is a YAML object with `rules_files` and Falco's own "
            "settings",
        )
        return False

    unknown = sorted(str(k) for k in doc if k not in _KNOWN_SECTIONS)
    if unknown:
        # Falco's config surface is enormous and version-dependent; an unknown key
        # is far more likely its vocabulary than a typo. Advisory, and only for
        # the keys this engine has actually heard of — a collector vocabulary body
        # was declined above, so anything judged here plausibly is a Falco config.
        verdict.add_warning(
            key,
            "unrecognised top-level key(s): " + ", ".join(f"`{k}`" for k in unknown),
            "Falco's config surface is large and version-dependent; check the "
            "version the agent reports (`falco.version` attribute) before trusting "
            "these are typos",
        )

    _judge_main_config(doc, key, verdict)
    return True


def components(agent: Any) -> Dict[str, List[Dict[str, Any]]]:
    """`{rulesfile: [{id}], rule: [{id}]}` from the documents the sidecar pushes.

    The sidecar reports no `AvailableComponents`, so the pushed documents are the
    evidence: every entry in `rules_files` is a rules file this agent loads, and
    every `rule:` entry in the managed `rules.yaml` is a rule it runs. Everything is
    `used: true` because it came from the pushed config.

    The bodies come from `server.effective_config.raw_bodies()`, the one reader that
    decodes the stored JSON *and* the base64 `body` field.
    """
    from server import effective_config as effective_config_module

    import yaml

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
            document = yaml.safe_load(body or "")
        except yaml.YAMLError:
            continue
        if isinstance(document, Mapping):
            files = document.get("rules_files")
            if isinstance(files, list):
                bucket = found.setdefault("rulesfile", [])
                for name in files:
                    if isinstance(name, str) and name.strip():
                        entry = {"id": name, "version": "", "used": True}
                        if entry not in bucket:
                            bucket.append(entry)
            continue  # a main config contributes files, not rules
        if isinstance(document, list):
            bucket = found.setdefault("rule", [])
            for entry in document:
                if not isinstance(entry, Mapping):
                    continue
                name = entry.get("rule")
                if not isinstance(name, str) or not name.strip():
                    continue
                item = {"id": name, "version": "", "used": True}
                if item not in bucket:
                    bucket.append(item)
    for bucket in found.values():
        bucket.sort(key=lambda item: item["id"])
    return found


def _register() -> None:
    from server.profiles.engines import register_components_engine, register_structure_engine

    register_structure_engine("falco", validate)
    register_components_engine("falco", components)


_register()
