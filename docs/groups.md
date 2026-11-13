# Named collector groups (#111, SQL since #132)

A filter you retype every push is a filter you eventually mistype. A **group** saves
a **SQL selection** under a name. Search first, then save the query:

```bash
opampctl agents query-schema                            # columns + values live agents report
opampctl agents list --query "SELECT * FROM agents WHERE host_arch = 'amd64'"
opampctl --password "$ADMIN_PASSWORD" groups create -n macos-au \
    --query "SELECT * FROM agents WHERE \"os.type\" = 'darwin' AND \"geo.country\" = 'AU'" \
    -d "macOS gateways in Australia. Owner: platform-oncall."
opampctl groups preview macos-au                        # members now vs last resolution
opampctl --password "$ADMIN_PASSWORD" config propose --file c.yaml --group macos-au
```

A group is a **named selector**, not a member list: membership is resolved at use
time against the live fleet, so `--group macos-au` hits exactly what the query hits
now, and cannot go stale. `include` forces one agent in, `exclude` forces one out
(both audited); there is no nesting. The full language reference is
[`sql-agent-queries.md`](sql-agent-queries.md).

The SQL schema is deliberately small: `agents` with core columns (`id`, `type`,
`healthy`, `compliant`, `last_heartbeat`, `heartbeat_age_seconds`,
`remote_config_status`, `capabilities`, …), one boolean column per OpAMP capability
(`AcceptsRemoteConfig`, `ReportsHealth`, …), and **one column per description
attribute** (`environment`, `host_arch`, `"os.type"`, …). Only `SELECT` is accepted;
writes, `PRAGMA`, `ATTACH` and unknown functions are refused.
`GET /agents/query/schema` returns the columns plus the values live agents actually
report, which is what powers the UI's suggestions. The **Agents** page has a
one-box SQL editor with context-aware completion (`SELECT` → `* FROM agents` →
`WHERE` → columns → values, Tab to accept, Enter to run), and the query you settle on
saves as a group from the same page: **💾 Save this query as a group** prompts for the
admin password *after* the click, so searching the fleet never needs one. Legacy #111
filter-document groups still work and can be rewritten as SQL with
`groups update --query`.

Four deliberate refusals, each closing a way a "saved filter" feature turns into an
outage:

- **A group query may not use `LIMIT`/`OFFSET`.** A group is a filter over the whole
  fleet; an arbitrary subset would make its blast radius depend on row order. Search
  may use them.
- **A group defined on `healthy`/`health_status`/`status` is refused as a push target**
  (400): the moment an agent breaks it leaves the group, so the push that would fix it
  skips exactly the agents that need it. `use_stored_resolution: true` sends to the
  membership recorded by a `preview` instead.
- **Silent shrinkage is loud.** Resolving smaller than the last recorded resolution
  returns the `dropped` diff and requires `confirm_scope_change: true`.
- **An empty group can be saved but not pushed to** (404), and an **archived** group
  is absent from pickers, refuses edits and pushes (409), and stays readable because
  revisions and the audit log still reference it. Archive is the only delete, and a
  live rollout blocks it.

Edits change **future** pushes only: every revision records a `group_snapshot`, and
`GET /config/revisions` reports `group_definition_changed: true` once the name no
longer means what it did.
