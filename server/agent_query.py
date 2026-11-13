"""SQL selection over the live fleet (#132).

Groups used to be a JSON filter document. That is a fine storage format and a poor
authoring format: an operator who wants "macOS agents in Australia" has to learn which
keys this server treats as reserved, which attributes are description metadata, and
that matching is exact equality. SQL is the language they already know, so it is the
language the search box and the saved group speak now.

**This is not a SQLite database query.** The fleet is in memory, so each query is run
against a throwaway in-memory table built from the agents in front of us. That keeps
the feature honest in two ways:

* the query can only ever read the columns we put in that table (descriptions and
  liveness), never `agents`, `config_revisions` or any other table on disk — the
  authorizer refuses everything that is not a `SELECT` from `agents`;
* it is read-only by construction (`PRAGMA query_only`), so a saved group can never
  become a write path.

The schema is deliberately small. Core columns cover what every OpAMP agent reports;
each description attribute is also exposed as a column, named exactly as the agent
reports it (quote it when it contains a dot or a space). ``attr('key')`` is shorthand
for a quoted identifier and is useful when a key cannot be written bare.

The guide for humans and other agents is `docs/sql-agent-queries.md`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Sequence

from server import profiles  # #134: the example query names a real profile

if TYPE_CHECKING:  # pragma: no cover - typing only, no import cycle at runtime
    from server.state import AgentState

MAX_QUERY_LENGTH = 4000
#: A selection filter is not a report. The cap also bounds a cartesian join: an
#: unauthenticated `GET /agents?q=` must not be able to make the server materialise a
#: million rows, and a fleet large enough to hit this needs a narrower filter anyway.
MAX_RESULT_ROWS = 10000

#: Columns every query can rely on, whatever the fleet happens to report.
CORE_COLUMNS: Dict[str, str] = {
    "id": "TEXT",
    "agent_id": "TEXT",
    # #137: the declared `agent.name`, so a selection can filter by the name an agent
    # showed up under without knowing that it is a description attribute (and without
    # the quoting the dotted spelling needs). Not an identity — see AgentState.name.
    "name": "TEXT",
    "agent_name": "TEXT",
    "healthy": "INTEGER",
    "health_status": "TEXT",
    "remote_config_status": "TEXT",
    "status": "TEXT",
    "connected_at": "TEXT",
    "last_heartbeat": "TEXT",
    "agent_type": "TEXT",
    # `type` is the name operators use in conversation; `agent_type` stays because it
    # is the API field. Both point at the same resolved profile.
    "type": "TEXT",
    "compliant": "TEXT",
    "is_compliant": "INTEGER",
    "heartbeat_age_seconds": "INTEGER",
    "capabilities": "INTEGER",
    "capability_tags": "TEXT",
    "service_name": "TEXT",
    "service_version": "TEXT",
    # The dotted spelling is what an OpAMP description calls it. SQLite needs it
    # quoted, which is exactly what the editor suggests.
    "service.name": "TEXT",
    "service.version": "TEXT",
    "stale": "INTEGER",
    "attributes": "TEXT",
}

CORE_DESCRIPTIONS = {
    "id": "Agent instance uid (same value as agent_id)",
    "agent_id": "Agent instance uid (same value as id)",
    "name": "The agent's declared agent.name (NULL when it declared none)",
    "agent_name": "Alias for name (the agent.name description attribute)",
    "healthy": "1 healthy, 0 unhealthy, NULL when the agent never reported health",
    "health_status": "ComponentHealth.status, e.g. ok or config_conflict",
    "remote_config_status": "Remote config status, e.g. UNSET, APPLIED, FAILED",
    "status": "Alias for remote_config_status",
    "connected_at": "When the server first saw this agent (UTC, YYYY-MM-DD HH:MM:SS)",
    "last_heartbeat": "Last OpAMP message (UTC, YYYY-MM-DD HH:MM:SS)",
    "agent_type": "Resolved profile name, e.g. otelcol or generic",
    "type": "Alias for agent_type",
    "compliant": "'yes', 'no' or 'unknown' (OPA verdict)",
    "is_compliant": "1 compliant, 0 not, NULL never evaluated / OPA disabled",
    "heartbeat_age_seconds": "Seconds since the last OpAMP message",
    "capabilities": "OpAMP capability bitmask",
    "capability_tags": "Advertised capability labels as a JSON array",
    "service_name": "service.name description attribute",
    "service_version": "service.version description attribute",
    "service.name": "service.name (quoted identifier)",
    "service.version": "service.version (quoted identifier)",
    "stale": "1 when the agent is past its own silence budget",
    "attributes": "All description attributes as a JSON object",
}

#: SQL keywords that must never be treated as attribute names when we are being
#: permissive about columns referenced by a saved query.
_KEYWORDS = frozenset(
    """
    all and as asc between by case cast collate desc distinct else end escape except
    exists false from glob group having in is isnull join like limit not notnull null
    offset on or order select then true union using when where with
    """.split()
)

#: Functions a query may call. Deliberately small: the point of the feature is
#: filtering, not computing, and a whitelist means a future SQLite feature cannot
#: become reachable by accident.
ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "coalesce",
        "date",
        "datetime",
        "ifnull",
        "instr",
        "json_array_length",
        "json_extract",
        "json_type",
        "julianday",
        "length",
        "like",
        "glob",
        "lower",
        "ltrim",
        "nullif",
        "replace",
        "round",
        "rtrim",
        "strftime",
        "substr",
        "time",
        "trim",
        "typeof",
        "upper",
    }
)

_HEALTH_TOKENS = frozenset(
    {
        "healthy",
        "health_status",
        "remote_config_status",
        "status",
        "stale",
        "last_heartbeat",
        "heartbeat_age_seconds",
    }
)

_ATTR_CALL = re.compile(r"\battr\s*\(\s*(['\"])(.*?)\1\s*\)", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTED_IDENTIFIER = re.compile(r'"((?:[^"]|"")*)"')


def capability_columns() -> Dict[str, int]:
    """Capability label -> bit, in the spelling an operator would type.

    ``AcceptsRemoteConfig`` rather than ``accepts_remote_config`` because that is the
    OpAMP vocabulary the UI already shows; SQLite identifiers are case-insensitive, so
    the exact camel case is not required, only the spelling.
    """
    from server.state import CAPABILITY_TAGS

    return {
        label.replace(" ", ""): bit
        for bit, (label, _icon) in CAPABILITY_TAGS.items()
    }


def _known_columns() -> Dict[str, str]:
    columns = dict(CORE_COLUMNS)
    for label in capability_columns():
        columns[label] = "INTEGER"
    return columns


class AgentQueryError(ValueError):
    """A refused query, carrying the HTTP status the route should use."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def _detail(message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _without_literals(sql: str) -> str:
    """SQL with string literals and comments blanked, for token-level checks.

    Keeping the length identical is not required; we only ask "does this contain a
    forbidden token", never "at which offset".
    """
    out: List[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'" and index + 1 < length and sql[index + 1] == "'":
                    index += 2
                    continue
                if sql[index] == "'":
                    index += 1
                    break
                index += 1
            out.append(" ")
            continue
        if char == '"':
            index += 1
            while index < length:
                if sql[index] == '"' and index + 1 < length and sql[index + 1] == '"':
                    index += 2
                    continue
                if sql[index] == '"':
                    index += 1
                    break
                index += 1
            out.append(" ")
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            index = sql.find("\n", index)
            if index == -1:
                break
            out.append(" ")
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _identifier_tokens(sql: str) -> List[str]:
    """Bare and double-quoted identifiers referenced by a query.

    Used only to be permissive: a saved query that names an attribute no current
    agent reports should resolve to *zero* agents (and trip the shrink guard), not
    fail with "no such column" months later. String literals are blanked first so a
    value that happens to look like a column is not added.
    """
    text = _without_literals(sql)
    tokens = set(_IDENTIFIER.findall(text))
    tokens.update(match.replace('""', '"') for match in _QUOTED_IDENTIFIER.findall(text))
    return sorted(token for token in tokens if token.lower() not in _KEYWORDS)


def _expand_attr_calls(sql: str) -> str:
    """Rewrite ``attr('host_arch')`` as ``json_extract(attributes, '$."host_arch"')``.

    A one-argument ``attr()`` is the one function SQLite cannot give us for free: a
    scalar UDF does not see the current row. Rewriting to JSON extraction keeps the
    query readable without teaching the database a per-row lookup.
    """

    def replace(match: re.Match) -> str:
        key = match.group(2)
        if not key or "'" in key:
            raise AgentQueryError(
                422,
                _detail(
                    "attr() needs a non-empty key without a single quote",
                    hint="use attr('environment') or the bare column name",
                ),
            )
        path = '$."' + key.replace('"', '\\"') + '"'
        return "json_extract(attributes, '" + path.replace("'", "''") + "')"

    return _ATTR_CALL.sub(replace, sql)


def normalize_query(sql: str, *, for_group: bool = False) -> str:
    """Validate the shape of a query and return its canonical text.

    A group is a *filter*, so a group query may not use ``LIMIT``/``OFFSET``: a saved
    group that silently means "the first 10 agents" is the blast-radius failure this
    feature exists to prevent. Search may use them.
    """
    text = (sql or "").strip()
    if not text:
        raise AgentQueryError(
            422,
            _detail(
                "query must not be empty",
                hint="start with SELECT * FROM agents",
            ),
        )
    if len(text) > MAX_QUERY_LENGTH:
        raise AgentQueryError(
            422,
            _detail(
                f"query is longer than {MAX_QUERY_LENGTH} characters",
                hint="a selection filter should be a WHERE clause, not a report",
            ),
        )
    body = text.rstrip().rstrip(";").strip()
    if not body:
        raise AgentQueryError(422, _detail("query must not be empty"))
    stripped = _without_literals(body)
    if ";" in stripped:
        raise AgentQueryError(
            422,
            _detail(
                "only one statement is allowed",
                hint="a selection filter is one SELECT",
            ),
        )
    if not re.match(r"(?is)^select\b", body):
        raise AgentQueryError(
            422,
            _detail(
                "query must start with SELECT",
                hint="only read-only SELECT statements are accepted, e.g. "
                "SELECT * FROM agents WHERE healthy = 1",
            ),
        )
    if for_group and re.search(r"(?is)\b(limit|offset)\b", stripped):
        raise AgentQueryError(
            422,
            _detail(
                "a group query may not use LIMIT or OFFSET",
                hint="a group is a filter over the whole fleet; an arbitrary subset "
                "would make its blast radius depend on row order",
            ),
        )
    return body


def references_health(sql: str) -> List[str]:
    """Health keys a query filters on (empty when it does not)."""
    tokens = {token.lower() for token in _identifier_tokens(sql)}
    return sorted(tokens & _HEALTH_TOKENS)


def quote_identifier(name: str) -> str:
    """Render a column name for a suggestion, quoting only when it is necessary."""
    if _IDENTIFIER.fullmatch(name) and name.lower() not in _KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _attribute_values(agent: "AgentState") -> Dict[str, str]:
    attributes: Dict[str, str] = {}
    description = agent.description or {}
    for entry in list(description.get("identifyingAttributes") or []) + list(
        description.get("nonIdentifyingAttributes") or []
    ):
        key = (entry or {}).get("key")
        if not key:
            continue
        value = agent.get_attr(str(key))
        if value is not None:
            attributes[str(key)] = str(value)
    return attributes


def _agent_row(agent: "AgentState") -> Dict[str, Any]:
    from server.state import utcnow

    attributes = _attribute_values(agent)
    health = agent.health if isinstance(agent.health, dict) else {}
    health_status = health.get("status")
    type_info = agent.type_info()
    compliance = agent.compliance if isinstance(agent.compliance, dict) else {}
    compliant = compliance.get("compliant")
    compliant_text = (
        "unknown" if compliant is None else ("yes" if compliant is True else "no")
    )
    heartbeat_age = max(0, int((utcnow() - agent.last_heartbeat).total_seconds()))
    row: Dict[str, Any] = {
        "id": agent.agent_id,
        "agent_id": agent.agent_id,
        # #137: `name` is a label, never a key — `id` stays the only projection the
        # result is resolved back through (see `run`).
        "name": agent.name,
        "agent_name": agent.name,
        "healthy": None if agent.healthy is None else int(bool(agent.healthy)),
        "health_status": health_status if isinstance(health_status, str) else None,
        "remote_config_status": agent.remote_config_status,
        "status": agent.remote_config_status,
        # SQLite datetime form (`YYYY-MM-DD HH:MM:SS`, UTC), not the API's ISO
        # spelling: `datetime('now', '-1 hour')` compares against it directly.
        "connected_at": agent.connected_at.strftime("%Y-%m-%d %H:%M:%S"),
        "last_heartbeat": agent.last_heartbeat.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_type": type_info.get("agent_type"),
        "type": type_info.get("agent_type"),
        "compliant": compliant_text,
        "is_compliant": None if compliant is None else int(bool(compliant)),
        "heartbeat_age_seconds": heartbeat_age,
        "capabilities": int(agent.capabilities or 0),
        "capability_tags": json.dumps(
            [tag["label"] for tag in agent.capability_tags], sort_keys=True
        ),
        "service_name": attributes.get("service.name"),
        "service_version": attributes.get("service.version"),
        "service.name": attributes.get("service.name"),
        "service.version": attributes.get("service.version"),
        "stale": int(agent.is_stale()),
        "attributes": json.dumps(attributes, sort_keys=True),
    }
    for label, bit in capability_columns().items():
        row[label] = int(bool(int(agent.capabilities or 0) & bit))
    for key, value in attributes.items():
        if key not in _known_columns():
            row[key] = value
    return row


def _table_columns(agents: Sequence["AgentState"], sql: str) -> List[str]:
    columns = list(_known_columns())
    seen = set(columns)
    for agent in agents:
        for key in _attribute_values(agent):
            if key not in seen:
                seen.add(key)
                columns.append(key)
    # A saved query may name an attribute the current fleet no longer reports. Add it
    # as a nullable column so the query resolves to no rows instead of erroring; the
    # group shrink guard then reports the drop.
    for token in _identifier_tokens(sql):
        if token not in seen and token.lower() not in ALLOWED_FUNCTIONS:
            seen.add(token)
            columns.append(token)
    return columns


def _connect(sql: str, agents: Sequence["AgentState"]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    columns = _table_columns(agents, sql)
    definitions = []
    for name in columns:
        column_type = _known_columns().get(name, "TEXT")
        definitions.append(f"{quote_identifier(name)} {column_type}")
    connection.execute(f"CREATE TABLE agents ({', '.join(definitions)})")
    if agents:
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(quote_identifier(name) for name in columns)
        rows = []
        for agent in agents:
            row = _agent_row(agent)
            rows.append(tuple(row.get(name) for name in columns))
        connection.executemany(
            f"INSERT INTO agents ({column_sql}) VALUES ({placeholders})", rows
        )

    # Read-only *after* the table is built: the connection is ours, the query is not.
    connection.execute("PRAGMA query_only = ON")
    # Abort a runaway query rather than letting a cross join pin a worker.
    connection.set_progress_handler(lambda: 1, 2_000_000)
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }

    def authorizer(action, arg1, arg2, dbname, trigger):
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and str(arg1).lower() != "agents":
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() not in ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    return connection


def _translate_error(exc: sqlite3.Error, sql: str) -> AgentQueryError:
    message = str(exc)
    if "no such column" in message:
        return AgentQueryError(
            422,
            _detail(
                message,
                hint="check the column against GET /agents/query/schema; quote a "
                "description attribute that contains a dot or a space",
                query=sql,
            ),
        )
    if "no such table" in message:
        return AgentQueryError(
            422,
            _detail(
                message,
                hint="the only table is agents; start with SELECT * FROM agents",
                query=sql,
            ),
        )
    if "interrupted" in message:
        return AgentQueryError(
            422,
            _detail(
                "query took too long and was stopped",
                hint="narrow the WHERE clause; a group is a filter, not a report",
                query=sql,
            ),
        )
    if "not authorized" in message:
        return AgentQueryError(
            422,
            _detail(
                message,
                hint="only SELECT from agents is allowed; no writes, PRAGMA or ATTACH",
                query=sql,
            ),
        )
    return AgentQueryError(422, _detail(message, query=sql))


def run(sql: str, agents: Sequence["AgentState"], *, for_group: bool = False) -> Dict[str, Any]:
    """Run a selection query and return the matched agents plus the result metadata.

    The query must return an ``id`` (or ``agent_id``) column. ``SELECT *`` does that
    by default; a query that projects only attributes is refused, because there is no
    honest way to turn a display column back into a membership.
    """
    normalized = normalize_query(sql, for_group=for_group)
    expanded = _expand_attr_calls(normalized)
    connection = _connect(expanded, agents)
    try:
        cursor = connection.execute(expanded)
        columns = [entry[0] for entry in (cursor.description or [])]
        fetched = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        if len(fetched) > MAX_RESULT_ROWS:
            raise AgentQueryError(
                422,
                _detail(
                    f"query returned more than {MAX_RESULT_ROWS} rows",
                    hint="a selection filter should narrow the fleet; add a WHERE "
                    "clause or select fewer columns",
                    query=normalized,
                ),
            )
        rows = [dict(zip(columns, row)) for row in fetched]
    except sqlite3.Error as exc:
        raise _translate_error(exc, normalized)
    finally:
        connection.close()

    id_column = next(
        (name for name in ("id", "agent_id") if name in columns),
        None,
    )
    if id_column is None:
        raise AgentQueryError(
            422,
            _detail(
                "query must return an id column",
                columns=columns,
                hint="use SELECT * FROM agents ... or include id in the projection",
            ),
        )
    ids: List[str] = []
    for row in rows:
        value = row.get(id_column)
        if value is not None and str(value) not in ids:
            ids.append(str(value))
    by_id = {agent.agent_id: agent for agent in agents}
    return {
        "query": normalized,
        "columns": columns,
        "rows": rows,
        "agent_ids": sorted(ids),
        "agents": [by_id[agent_id] for agent_id in sorted(ids) if agent_id in by_id],
        "count": len(ids),
    }


def select_ids(sql: str, agents: Sequence["AgentState"], *, for_group: bool = False) -> List[str]:
    """Just the matching agent ids, sorted. The membership answer a group needs."""
    return run(sql, agents, for_group=for_group)["agent_ids"]


def validate(sql: str, *, for_group: bool = False) -> str:
    """Validate a query against an empty fleet (shape, syntax and columns).

    Used on create/update, so a group cannot be saved with a typo that only surfaces
    at push time. Columns referenced by the query are added as nullable placeholders,
    so an attribute that exists only on a real fleet still validates here.
    """
    normalized = normalize_query(sql, for_group=for_group)
    expanded = _expand_attr_calls(normalized)
    connection = _connect(expanded, [])
    try:
        connection.execute(expanded)
    except sqlite3.Error as exc:
        raise _translate_error(exc, normalized)
    finally:
        connection.close()
    return normalized


def _suggestion_rows(agents: Sequence["AgentState"]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}

    def add(name: str, kind: str, description: str, values: Iterable[Any] = ()):
        bucket = seen.setdefault(
            name,
            {
                "name": name,
                "sql_name": quote_identifier(name),
                "kind": kind,
                "description": description,
                "values": [],
                "agent_count": 0,
            },
        )
        for value in values:
            text = str(value)
            if text not in bucket["values"]:
                bucket["values"].append(text)
        return bucket

    for name, description in CORE_DESCRIPTIONS.items():
        add(name, "core", description)
    for label in capability_columns():
        add(
            label,
            "capability",
            "OpAMP capability flag: 1 advertised, 0 not advertised",
        )
    for agent in agents:
        # Values for the core columns operators actually filter on. A suggestion is
        # only useful when it is a value this fleet reported, so these are sampled
        # from the same agents the query will run against.
        type_info = agent.type_info()
        compliance = agent.compliance if isinstance(agent.compliance, dict) else {}
        compliant = compliance.get("compliant")
        for name, value in (
            ("type", type_info.get("agent_type")),
            ("agent_type", type_info.get("agent_type")),
            # #137: suggest the names this fleet actually declared, so
            # `WHERE name = 'e2e-collector'` is a suggestion and not a guess.
            ("name", agent.name),
            ("agent_name", agent.name),
            ("healthy", None if agent.healthy is None else int(bool(agent.healthy))),
            (
                "compliant",
                "unknown" if compliant is None else ("yes" if compliant is True else "no"),
            ),
            (
                "is_compliant",
                None if compliant is None else int(bool(compliant)),
            ),
            ("service.name", agent.get_attr("service.name")),
            ("service_name", agent.get_attr("service.name")),
            ("service.version", agent.get_attr("service.version")),
            ("service_version", agent.get_attr("service.version")),
            ("capabilities", int(agent.capabilities or 0)),
        ):
            if value is not None:
                add(name, "core", CORE_DESCRIPTIONS.get(name, ""))["values"].append(
                    str(value)
                )
        for label, bit in capability_columns().items():
            add(label, "capability", "")["values"].append(
                "1" if int(agent.capabilities or 0) & bit else "0"
            )
        for key, value in _attribute_values(agent).items():
            if key in _known_columns():
                continue
            bucket = add(
                key,
                "attribute",
                "Description attribute reported by live agents",
            )
            bucket["agent_count"] += 1
            if value not in bucket["values"]:
                bucket["values"].append(value)
    for bucket in seen.values():
        bucket["values"] = sorted(set(bucket["values"]), key=str.lower)[:12]
    return sorted(seen.values(), key=lambda bucket: (bucket["kind"] != "core", bucket["name"].lower()))


def schema(agents: Sequence["AgentState"]) -> Dict[str, Any]:
    """What can be queried, with the values the live fleet actually reports.

    This is the autosuggestion payload: static columns and example statements on one
    side, observed attribute keys and values on the other. A UI can offer
    ``host_arch = 'amd64'`` only because an agent reported ``amd64``, which is the
    difference between a suggestion and a guess.
    """
    columns = _suggestion_rows(agents)
    examples = ["SELECT * FROM agents"]
    if any(agent.healthy is not None for agent in agents):
        examples.append("SELECT * FROM agents WHERE healthy = 1")
    examples.append("SELECT * FROM agents WHERE remote_config_status = 'APPLIED'")
    # #134: the example names a profile this build actually has, in match order,
    # rather than a literal 'otelcol' that a deployment without the collector
    # dictionary would not recognise.
    for profile_name in profiles.names():
        if profile_name == profiles.FALLBACK_PROFILE:
            continue
        examples.append(f"SELECT * FROM agents WHERE type = '{profile_name}'")
        break
    examples.append("SELECT * FROM agents WHERE compliant = 'no'")
    examples.append("SELECT * FROM agents WHERE AcceptsRemoteConfig = 1")
    examples.append(
        "SELECT * FROM agents WHERE \"service.name\" = 'collector-prod'"
    )
    # #137: only when a live agent declared one — the example exists to show the
    # column, not to suggest a name nobody in this fleet has.
    named = next((agent.name for agent in agents if agent.name), None)
    if named is not None:
        examples.append(f"SELECT * FROM agents WHERE name = {quote_literal(named)}")
    examples.append(
        "SELECT * FROM agents WHERE last_heartbeat < datetime('now', '-1 hour')"
    )
    examples.append("SELECT * FROM agents WHERE heartbeat_age_seconds > 300")
    attribute_columns = [column for column in columns if column["kind"] == "attribute"]
    for column in attribute_columns[:6]:
        for value in column["values"][:2]:
            examples.append(
                f"SELECT * FROM agents WHERE {column['sql_name']} = {quote_literal(value)}"
            )
    if len(attribute_columns) >= 2:
        first, second = attribute_columns[0], attribute_columns[1]
        if first["values"] and second["values"]:
            examples.append(
                "SELECT * FROM agents WHERE "
                f"{first['sql_name']} = {quote_literal(first['values'][0])} "
                f"AND {second['sql_name']} = {quote_literal(second['values'][0])}"
            )
    return {
        "table": "agents",
        "columns": columns,
        "count": len(columns),
        "examples": list(dict.fromkeys(examples)),
        "functions": {
            "attr": "attr('key') is shorthand for the attribute column named key",
            "lower/upper": "case folding",
            "coalesce": "fall back when a column is NULL",
            "json_extract": "read a value out of the attributes JSON column",
        },
        "operators": [
            "=",
            "!=",
            "<>",
            "<",
            "<=",
            ">",
            ">=",
            "IN (...)",
            "NOT IN (...)",
            "LIKE",
            "IS NULL",
            "IS NOT NULL",
            "AND",
            "OR",
            "NOT",
        ],
        "notes": [
            "Only SELECT statements are accepted.",
            "The only table is agents.",
            "Description attributes are text; quote their values.",
            "healthy is 1, 0 or NULL; use IS NULL for 'never reported'.",
            "A group query may not use LIMIT or OFFSET.",
        ],
        "guide": "docs/sql-agent-queries.md",
        "agents": len(agents),
    }
