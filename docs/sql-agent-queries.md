# Selecting agents with SQL (#132)

A **selection filter** in this project is one read-only `SELECT` statement over the
live fleet. The same statement can be:

- typed in the **Agents / Groups** search box,
- run from the API (`GET /agents?q=...`),
- run from `opampctl agents list --query "..."`,
- run from the MCP tools `query_agents` / `list_agents(query=...)`,
- saved as a **group** (`POST /groups {"query": "..."}`), which is what a remote-config
  push targets.

A group is a *saved query*, not a member list. Membership is evaluated when the group
is read or used, so it cannot drift from the fleet.

This document is the language reference. If you are an agent asked to "write a
selection filter for macOS machines in Australia", read the **Schema** section, call
`GET /agents/query/schema` to learn the attribute names that fleet actually reports,
and then write the `WHERE` clause.

---

## 1. The shape

```sql
SELECT * FROM agents [WHERE <condition>] [ORDER BY <column> [ASC|DESC]] [LIMIT n]
```

Rules enforced by the server:

| Rule | Why |
|---|---|
| Exactly one statement, starting with `SELECT` | A group must never become a write path. |
| The only table is `agents` | No access to SQLite state, config revisions or credentials. |
| Read-only (`PRAGMA query_only`, authorizer) | `INSERT`, `UPDATE`, `DELETE`, `PRAGMA`, `ATTACH`, `CREATE`, … are refused. |
| The result must include `id` or `agent_id` | A projection that returns only attributes cannot be turned back into a membership. `SELECT *` does this automatically. |
| A **group** query may not use `LIMIT` or `OFFSET` | A group is a filter over the whole fleet. An arbitrary subset would make its blast radius depend on row order. Search may use them. |
| At most 4000 characters | A selection filter is a filter, not a report. |
| At most 10 000 result rows; runaway queries are stopped | `GET /agents?q=` is unauthenticated; a cross join must not be able to pin a worker. |

Comments (`--` and `/* */`) are allowed. String literals use single quotes; escape a
quote by doubling it (`'O''Brien'`).

---

## 2. Schema

The table is `agents`. Every row is one connected agent.

### Core columns

| Column | Type | Meaning |
|---|---|---|
| `id` | text | Agent instance uid (same value as `agent_id`) |
| `agent_id` | text | Agent instance uid |
| `healthy` | integer | `1` healthy, `0` unhealthy, `NULL` never reported |
| `health_status` | text | `ComponentHealth.status`, e.g. `ok`, `config_conflict` |
| `remote_config_status` | text | `UNSET`, `APPLIED`, `APPLYING`, `FAILED`, … |
| `status` | text | Alias for `remote_config_status` |
| `connected_at` | text | ISO-8601 timestamp |
| `last_heartbeat` | text | ISO-8601 timestamp |
| `agent_type` | text | Resolved profile, e.g. `otelcol`, `generic` |
| `service_name` | text | `service.name` description attribute |
| `service_version` | text | `service.version` description attribute |
| `stale` | integer | `1` when the agent is past its own silence budget |
| `attributes` | text | Every description attribute as a JSON object |

### Description attributes

Every key an agent reports in its OpAMP `AgentDescription` is **also a column**, named
exactly as the agent reports it. That is where fleet-specific values live:
`host_arch`, `environment`, `region`, `os_type`, `geo_country`, and so on.

- Bare identifiers work: `WHERE environment = 'prod'`.
- A key containing a dot, space or other non-identifier character must be
  double-quoted: `WHERE "os.type" = 'darwin'`.
- `attr('key')` is shorthand for the same column and is handy when the key is awkward:
  `WHERE attr('os.type') = 'darwin'`.
- Attribute values are **text**. Quote them: `WHERE host_arch = 'amd64'`. Matching is
  exact and case-sensitive in SQL; use `LOWER(host_arch) = 'amd64'` for case-insensitive
  matching.
- An attribute no agent currently reports still resolves to `NULL` (no rows), rather
  than failing the query. That is deliberate: a saved group whose attribute disappeared
  shows up as a membership drop, which the preview and the shrink guard make visible.

`service.name` and `service.version` have underscore aliases (`service_name`,
`service_version`) because their dotted names are easy to mistype.

---

## 3. Operators

```
=   !=   <>   <   <=   >   >=
IN (...)      NOT IN (...)
LIKE          NOT LIKE
IS NULL       IS NOT NULL
AND   OR   NOT   ( ... )
```

Useful functions (a whitelist; anything else is refused):

`lower`, `upper`, `length`, `substr`, `trim`, `ltrim`, `rtrim`, `replace`,
`coalesce`, `ifnull`, `nullif`, `instr`, `abs`, `round`, `typeof`,
`json_extract`, `json_type`, `json_array_length`.

`LIKE` is case-insensitive for ASCII in SQLite and supports `%` and `_`.

---

## 4. Examples

All agents:

```sql
SELECT * FROM agents
```

Healthy agents only:

```sql
SELECT * FROM agents WHERE healthy = 1
```

Agents that have never reported health:

```sql
SELECT * FROM agents WHERE healthy IS NULL
```

Production agents in EMEA:

```sql
SELECT * FROM agents
WHERE environment = 'prod' AND region = 'emea'
```

AMD64 agents:

```sql
SELECT * FROM agents WHERE host_arch = 'amd64'
```

Case-insensitive value match:

```sql
SELECT * FROM agents WHERE LOWER(host_arch) = 'amd64'
```

Agents whose remote config failed:

```sql
SELECT * FROM agents
WHERE remote_config_status = 'FAILED'
```

A generic OpAMP agent (not an OTel Collector):

```sql
SELECT * FROM agents WHERE agent_type = 'generic'
```

Agents that are stale or unhealthy:

```sql
SELECT * FROM agents
WHERE stale = 1 OR healthy = 0
```

Search by substring in any attribute value:

```sql
SELECT * FROM agents WHERE attributes LIKE '%australia%'
```

**macOS machines in Australia.** The exact attribute names depend on what the fleet
reports. Ask the schema endpoint first, then use the reported names. If the keys are
`os.type` and `geo.country`:

```sql
SELECT * FROM agents
WHERE "os.type" = 'darwin'
  AND "geo.country" = 'AU'
```

If the fleet reports them as `os_type` and `geo_country`:

```sql
SELECT * FROM agents
WHERE os_type = 'darwin'
  AND geo_country = 'AU'
```

Do not guess: `GET /agents/query/schema` lists the keys and observed values. A value
like `darwin` appears there only because an agent reported it.

Ordered search (search only; a saved group may not use `LIMIT`):

```sql
SELECT id, service_name, host_arch
FROM agents
WHERE environment = 'prod'
ORDER BY service_name
LIMIT 20
```

---

## 5. Autosuggestion

`GET /agents/query/schema` returns:

```json
{
  "table": "agents",
  "columns": [
    {"name": "healthy", "sql_name": "healthy", "kind": "core", "values": []},
    {"name": "host_arch", "sql_name": "host_arch", "kind": "attribute",
     "values": ["amd64", "arm64"], "agent_count": 12}
  ],
  "examples": [
    "SELECT * FROM agents",
    "SELECT * FROM agents WHERE healthy = 1",
    "SELECT * FROM agents WHERE host_arch = 'amd64'"
  ],
  "operators": ["=", "!=", "IN (...)", "LIKE", "AND", "OR", "..."],
  "guide": "docs/sql-agent-queries.md"
}
```

The UI uses this for the suggestion buttons in the SQL editor. An agent should use it
the same way: the `columns` list is the authoritative set of names, and `values` are
values observed on connected agents. `sql_name` is already quoted correctly for use in
a statement.

---

## 6. Saving a query as a group

A group is a name for one selection, plus optional pins:

```bash
opampctl groups create --name macos-au \
  --query "SELECT * FROM agents WHERE \"os.type\" = 'darwin' AND \"geo.country\" = 'AU'"

opampctl groups preview macos-au     # members now vs last recorded resolution
opampctl groups show macos-au        # the query, members, pins and history
```

Edit the query after the fact:

```bash
opampctl groups update macos-au \
  --query "SELECT * FROM agents WHERE \"os.type\" = 'darwin' AND \"geo.country\" IN ('AU','NZ')"
```

REST:

```bash
curl -X POST http://localhost:4320/groups \
  -u admin:$ADMIN_PASSWORD -H 'Content-Type: application/json' \
  -d '{"name": "macos-au", "query": "SELECT * FROM agents WHERE \"os.type\" = '\''darwin'\''"}'
```

### Group rules that are not SQL rules

- **One selector per group.** Send `query` (SQL, recommended) or `definition` (the
  older filter document), never both. `GET /groups` reports `selector_kind` as
  `query` or `definition`.
- **No `LIMIT`/`OFFSET`** in a group query. A group is a filter over the whole fleet.
- **Health-defined groups are refused as push targets by default.** A query that
  filters on `healthy`, `health_status`, `remote_config_status` or `status` gets the
  same guard as a filter document: agents drop out exactly when they break, so the
  push that would fix them skips them. Preview the group first, then use
  `use_stored_resolution: true` to push to the membership recorded at preview time.
- **Silent shrinkage is refused.** If the group resolves smaller than its last
  recorded resolution, the push returns `added`/`dropped`/`unchanged` and needs
  `confirm_scope_change: true`.
- **Pins still apply.** `include` forces an agent in even when the query does not match
  it; `exclude` forces one out even when it does.
- **An empty result is valid to save, not to push.** A push to a group with zero
  members is refused (404), because a push that ships nothing is almost always a
  mistake.
- **Groups are archived, never deleted.** Revisions and rollouts reference the id.
- **Edits affect future pushes only.** A revision keeps the query snapshot it was
  approved against, and `group_definition_changed: true` says the name no longer means
  what it did.

---

## 7. Errors

Queries fail with `422` and a structured detail:

```json
{
  "detail": {
    "message": "no such column: host_arch",
    "hint": "check the column against GET /agents/query/schema; quote a description attribute that contains a dot or a space",
    "query": "SELECT * FROM agents WHERE host_arch = 'amd64'"
  }
}
```

| Situation | Answer |
|---|---|
| Not a `SELECT`, or more than one statement | `422` |
| Unknown table or column | `422` with a hint to read the schema |
| `LIMIT`/`OFFSET` in a group query | `422` |
| Write, `PRAGMA`, `ATTACH`, or a non-whitelisted function | `422` (`not authorized`) |
| Query returns no `id`/`agent_id` column | `422` |
| Group has both `query` and `definition` | `422` |
| Group has neither | `422` |
| Empty group used as a push target | `404` |
| Archived group | `409` |

---

## 8. Security notes

- Queries run against a throwaway in-memory table built from live agents, not against
  the server's SQLite database. `config_revisions`, `desired_configs`,
  `agent_credentials` and the audit log are not reachable.
- Only agent **descriptions** are exposed. The effective config is never read into the
  query table (D8: that is where a fleet's credentials live).
- The authorizer allows only `SELECT`/`READ` on `agents` and the function whitelist.
  `PRAGMA query_only` is a second, independent barrier.
- A group query is stored as text and re-validated on create and update, so a saved
  group cannot be a query that was never checked.
