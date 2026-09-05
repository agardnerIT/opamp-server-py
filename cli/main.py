"""opampctl — CLI for the OpAMP server, built for humans and AI agents alike.

Every documented REST endpoint has a CLI equivalent. Output is **JSON by
default** (agents parse JSON best); use ``--raw`` where a command produces a
markdown/YAML payload to print just that text.

Configuration (flags beat env vars):
- ``--server URL`` / ``OPAMP_SERVER_URL``       server base URL (default http://localhost:4320)
- ``--password PW`` / ``ADMIN_PASSWORD``        admin password (Basic auth)
- ``--db PATH``                                 offline mode: read SQLite state directly,
                                                no server required (read-only commands only)

Examples::

    opampctl health
    opampctl agents list --healthy true --attr environment=prod
    opampctl agents manifest <agent-id> --raw
    opampctl reports fleet --raw
    opampctl alerts set @alerts.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from client.opamp_client import (
    OpampApiError,
    OpampClient,
    OpampClientError,
    OpampConnectionError,
)

app = typer.Typer(
    name="opampctl",
    help="Inspect OpAMP agents, build OCB manifests, run compliance checks, manage alerts.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

agents_app = typer.Typer(help="Agent inspection commands", no_args_is_help=True)
reports_app = typer.Typer(help="Markdown fleet reports", no_args_is_help=True)
compliance_app = typer.Typer(help="OPA policy compliance", no_args_is_help=True)
alerts_app = typer.Typer(help="Alert configuration (admin)", no_args_is_help=True)
auth_app = typer.Typer(help="Admin auth status", no_args_is_help=True)

app.add_typer(agents_app, name="agents", help="Agent inspection commands")
app.add_typer(reports_app, name="reports", help="Markdown fleet reports")
app.add_typer(compliance_app, name="compliance", help="OPA policy compliance")
app.add_typer(alerts_app, name="alerts", help="Alert configuration (admin)")
app.add_typer(auth_app, name="auth", help="Admin auth status")


class _Settings:
    """Parsed global options shared by all commands."""

    server: Optional[str] = None
    password: Optional[str] = None
    db: Optional[Path] = None
    pretty: bool = False

    @property
    def offline(self) -> bool:
        return self.db is not None


_settings = _Settings()


@app.callback()
def _main(
    server: Optional[str] = typer.Option(
        None, "--server", "-s", envvar="OPAMP_SERVER_URL",
        help="Server base URL [default: OPAMP_SERVER_URL or http://localhost:4320]",
    ),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", envvar="ADMIN_PASSWORD",
        help="Admin password [default: ADMIN_PASSWORD]",
    ),
    db: Optional[Path] = typer.Option(
        None, "--db",
        help="Offline mode: read this SQLite state file directly (no server). "
        "Supports read-only commands (agents, reports, manifest); live commands "
        "(health, compliance, alerts) exit with an error.",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", help="Pretty-print JSON with indentation",
    ),
) -> None:
    _settings.server = server
    _settings.password = password
    _settings.db = db
    _settings.pretty = pretty


# --------------------------------------------------------------------- output

def _emit(data: Any, raw_field: Optional[str] = None) -> None:
    """Print payload: raw text of data[raw_field], else JSON (pretty optional)."""
    if raw_field is not None:
        if isinstance(data, dict) and raw_field in data:
            text = data[raw_field]
            if not isinstance(text, str):
                text = json.dumps(text, indent=2 if _settings.pretty else None, default=str)
            typer.echo(text)
            return
        _fail(f"Response has no {raw_field!r} field to print with --raw", data)
    typer.echo(json.dumps(data, indent=2 if _settings.pretty else None, default=str))


def _fail(message: str, data: Any = None) -> None:
    """Structured error to stderr, JSON shape so agents can parse failures too."""
    payload: Dict[str, Any] = {"error": message}
    if data is not None:
        payload["response"] = data
    typer.echo(json.dumps(payload, indent=2 if _settings.pretty else None, default=str), err=True)
    raise typer.Exit(code=1)


def _client() -> OpampClient:
    if _settings.offline:
        _fail(
            "This command requires a live server and is unavailable with --db; "
            "drop --db to talk to the server."
        )
    try:
        return OpampClient(base_url=_settings.server, password=_settings.password)
    except OpampClientError as exc:
        _fail(str(exc))


def _run(action, raw_field: Optional[str] = None) -> None:
    """Execute a client call, mapping client errors to structured CLI errors."""
    try:
        with _client() as oc:
            _emit(action(oc), raw_field=raw_field)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))
    except OpampClientError as exc:
        _fail(str(exc))


# ------------------------------------------------------------------ offline db

def _offline_states() -> List[Any]:
    """Load agent states from the SQLite file given via --db."""
    from server.state import SQLiteAgentStore

    store = SQLiteAgentStore(_settings.db)
    states = list(store.load_all().values())
    if not states:
        _fail(f"No agents found in {_settings.db}")
    return states


def _offline_metrics(agent_id: str) -> Dict[str, Any]:
    from server.state import SQLiteMetricsStore

    return SQLiteMetricsStore(_settings.db).get(agent_id) or {}


def _offline_get_state(agent_id: str) -> Any:
    for state in _offline_states():
        if state.agent_id == agent_id:
            return state
    _fail(f"Agent {agent_id!r} not found in {_settings.db}")


# ----------------------------------------------------------------------- ops

@app.command()
def health() -> None:
    """Server health: status, connected agents, OPA availability."""
    _run(lambda oc: oc.health())


@auth_app.command("status")
def auth_status() -> None:
    """Whether the server requires an admin password."""
    _run(lambda oc: oc.auth_status())


@auth_app.command("verify")
def auth_verify() -> None:
    """Verify the configured admin password against the server (exit 0 = ok)."""
    try:
        with _client() as oc:
            result = oc.auth_verify(oc.password or "")
            _emit(result)
            if not result.get("verified"):
                raise typer.Exit(code=1)
    except OpampApiError as exc:
        _fail(f"HTTP {exc.status_code}", data={"detail": exc.detail})
    except OpampConnectionError as exc:
        _fail(str(exc))


# --------------------------------------------------------------------- agents

@agents_app.command("list")
def agents_list(
    healthy: Optional[str] = typer.Option(
        None, "--healthy", help="Filter: true / false / unknown",
    ),
    status: Optional[str] = typer.Option(
        None, "--status", help="Remote config status filter (UNSET/APPLIED/APPLYING/FAILED)",
    ),
    attr: List[str] = typer.Option(
        [], "--attr", "-a", help="Metadata filter, repeatable: --attr environment=prod",
    ),
) -> None:
    """List agents with optional health/status/metadata filters."""
    if _settings.offline:
        from server.state import AgentState

        attr_filters: Dict[str, list] = {}
        for item in attr:
            key, _, value = item.partition("=")
            attr_filters.setdefault(key, []).append(value)
        dicts = [
            s.to_dict() for s in _offline_states()
            if s.matches_filters(healthy=healthy, remote_config_status=status,
                                 attributes=attr_filters)
        ]
        _emit({"agents": dicts, "count": len(dicts),
               "filters": {"offline_db": str(_settings.db)}})
        return
    kwargs: Dict[str, Any] = {}
    for item in attr:
        key, _, value = item.partition("=")
        kwargs.setdefault(key, []).append(value)
    _run(lambda oc: oc.list_agents(healthy=healthy, status=status, **kwargs))


@agents_app.command("get")
def agents_get(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Full details for one agent, including latest metrics."""
    if _settings.offline:
        state = _offline_get_state(agent_id)
        data = state.to_dict()
        data["metrics"] = _offline_metrics(agent_id)
        _emit(data)
        return
    _run(lambda oc: oc.get_agent(agent_id))


@agents_app.command("metrics")
def agents_metrics(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Latest ingested OTLP metric values for one agent."""
    if _settings.offline:
        _emit(_offline_metrics(agent_id))
        return
    _run(lambda oc: oc.get_agent_metrics(agent_id))


@agents_app.command("manifest")
def agents_manifest(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    version: Optional[str] = typer.Option(None, "--version", help="Distro version (semver)"),
    raw: bool = typer.Option(False, "--raw", help="Print just the manifest.yaml text"),
) -> None:
    """Generate an OCB manifest.yaml for a slim collector build."""
    if _settings.offline:
        from server.manifest import generate_manifest, generate_ocb_command, validate_manifest_version, DEFAULT_VERSION

        try:
            resolved = validate_manifest_version(version or DEFAULT_VERSION)
        except ValueError as exc:
            _fail(str(exc))
        comps = _offline_get_state(agent_id).components
        if not comps or not any(c.get("used") for g in comps.values() for c in g):
            _fail("Agent has no components in use; cannot generate a buildable OCB manifest")
        result = {
            "manifest_yaml": generate_manifest(comps, resolved),
            "ocb_command": generate_ocb_command(resolved),
            "collector_version": resolved,
        }
        _emit(result, raw_field="manifest_yaml" if raw else None)
        return
    _run(lambda oc: oc.generate_manifest(agent_id, version=version),
         raw_field="manifest_yaml" if raw else None)


@agents_app.command("compliance")
def agents_compliance(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Evaluate one agent against OPA policies (live server only)."""
    _run(lambda oc: oc.get_compliance(agent_id))


@agents_app.command("report")
def agents_report(
    agent_id: str = typer.Argument(..., help="Agent instance UID (hex)"),
    raw: bool = typer.Option(False, "--raw", help="Print just the markdown report"),
) -> None:
    """Markdown report for one agent."""
    if _settings.offline:
        state = _offline_get_state(agent_id)
        from server.reports import generate_agent_report

        _emit({"report_markdown": generate_agent_report({"agents": [state.to_dict()]})},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.agent_report(agent_id), raw_field="report_markdown" if raw else None)


# -------------------------------------------------------------------- reports

@reports_app.command("fleet")
def reports_fleet(raw: bool = typer.Option(False, "--raw", help="Print just the markdown")) -> None:
    """Fleet-wide agent report (versions, outdated/heavy collectors, detail)."""
    if _settings.offline:
        from server.reports import generate_agent_report

        dicts = [s.to_dict() for s in _offline_states()]
        _emit({"report_markdown": generate_agent_report({"agents": dicts}),
               "agent_count": len(dicts)},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.fleet_report(), raw_field="report_markdown" if raw else None)


@reports_app.command("heavy")
def reports_heavy(
    threshold: float = typer.Option(0.5, "--threshold", min=0.0, max=1.0,
                                    help="Unused-component ratio above which a collector is 'heavy'"),
    raw: bool = typer.Option(False, "--raw", help="Print just the markdown"),
) -> None:
    """Report collectors with many unused components."""
    if _settings.offline:
        from server.reports import generate_heavy_collectors_report, _is_heavy

        dicts = [s.to_dict() for s in _offline_states()]
        heavy = [a for a in dicts if _is_heavy(a, threshold)]
        _emit({"report_markdown": generate_heavy_collectors_report({"agents": dicts}, threshold),
               "heavy_count": len(heavy), "threshold": threshold},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.heavy_collectors_report(threshold=threshold),
         raw_field="report_markdown" if raw else None)


@reports_app.command("outdated")
def reports_outdated(
    version: str = typer.Option("0.149.0", "--version", help="Reference collector version (semver)"),
    raw: bool = typer.Option(False, "--raw", help="Print just the markdown"),
) -> None:
    """Report collectors with components older than a reference version."""
    if _settings.offline:
        from server.manifest import validate_manifest_version
        from server.reports import generate_outdated_collectors_report, _count_outdated_collectors

        try:
            resolved = validate_manifest_version(version)
        except ValueError as exc:
            _fail(str(exc))
        dicts = [s.to_dict() for s in _offline_states()]
        collectors, components = _count_outdated_collectors(dicts, resolved)
        _emit({"report_markdown": generate_outdated_collectors_report({"agents": dicts}, resolved),
               "collectors_count": collectors, "components_count": components,
               "version": resolved},
              raw_field="report_markdown" if raw else None)
        return
    _run(lambda oc: oc.outdated_collectors_report(version=version),
         raw_field="report_markdown" if raw else None)


# ----------------------------------------------------------------- compliance

@compliance_app.command("summary")
def compliance_summary() -> None:
    """Fleet-wide compliance counts."""
    _run(lambda oc: oc.compliance_summary())


@compliance_app.command("policies")
def compliance_policies() -> None:
    """List available OPA policies."""
    _run(lambda oc: oc.list_policies())


@compliance_app.command("validate")
def compliance_validate() -> None:
    """Validate OPA policy files."""
    _run(lambda oc: oc.validate_policies())


@compliance_app.command("reload")
def compliance_reload() -> None:
    """Ask OPA to reload policies (admin)."""
    _run(lambda oc: oc.reload_policies())


@compliance_app.command("check")
def compliance_check(agent_id: str = typer.Argument(..., help="Agent instance UID (hex)")) -> None:
    """Force a compliance evaluation for one agent (admin)."""
    _run(lambda oc: oc.check_compliance(agent_id))


# --------------------------------------------------------------------- alerts

def _load_config(spec: str) -> Dict[str, Any]:
    """Parse a JSON config from a literal string or '@path/to/file.json'."""
    if spec.startswith("@"):
        path = Path(spec[1:])
        if not path.exists():
            _fail(f"Config file not found: {path}")
        try:
            return json.loads(path.read_text())
        except ValueError as exc:
            _fail(f"Invalid JSON in {path}: {exc}")
    try:
        return json.loads(spec)
    except ValueError as exc:
        _fail(f"Invalid JSON config: {exc}")


@alerts_app.command("get")
def alerts_get() -> None:
    """Get the current alert configuration (admin)."""
    _run(lambda oc: oc.get_alerts())


@alerts_app.command("set")
def alerts_set(
    config: str = typer.Argument(..., help="Alert config as JSON, or '@file.json'"),
) -> None:
    """Update the alert configuration (admin). Body: same shape GET returns under 'config'."""
    _run(lambda oc: oc.update_alerts(_load_config(config)))


@alerts_app.command("test")
def alerts_test(
    event_type: str = typer.Option("new_agent", "--event-type", help="Event type to test"),
    config: Optional[str] = typer.Option(None, "--config", help="Optional event config JSON (used once, not saved)"),
) -> None:
    """Send a test alert through the configured dispatcher (admin)."""
    event_config = _load_config(config) if config is not None else None
    _run(lambda oc: oc.test_alerts(event_type=event_type, event_config=event_config))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
