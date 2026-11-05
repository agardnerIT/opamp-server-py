"""Groups (#132): search the fleet with SQL, then save the query as a group.

A group used to be a JSON filter document edited under "Remote Config". It is now a
**saved SQL selection**, which is why it has its own page: the workflow is *search
first, name it second*, and that is a different job from pushing a config.

The page is deliberately read-mostly. Running a query and previewing a group are
`viewer` actions; saving, editing, archiving and restoring call `operator` endpoints
and the server re-authorises every one of them.

Autosuggestion is the part that makes the language usable without a reference open:
the suggestions are built from `GET /agents/query/schema`, so `host_arch = 'amd64'`
is offered only because an agent reported `amd64`. A text box cannot autocomplete in
Streamlit, so the practical equivalent is a suggestion picker that inserts a whole
statement into the editor.
"""

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.shared import (  # noqa: E402
    ROLE_OPERATOR,
    ROLE_VIEWER,
    admin_request,
    description_attribute,
    format_local_time,
    render_sidebar,
    require_admin,
)

render_sidebar()
require_admin(ROLE_VIEWER)

SQL_KEY = "groups_sql"
GROUP_KEY = "groups_selected"
EDIT_KEY = "groups_edit_query"

DEFAULT_SQL = "SELECT * FROM agents WHERE healthy = 1"


def _payload(response, default=None):
    if response is None:
        return default
    try:
        return response.json()
    except Exception:
        return default


def _detail(response):
    body = _payload(response, {})
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("message") or json.dumps(detail, indent=2)
        if detail:
            return str(detail)
        return json.dumps(body, indent=2)
    return str(body)


def _agent_table(agents):
    return [
        {
            "id": agent.get("id", "")[:16],
            "service": description_attribute(agent.get("description"), "service.name")
            or agent.get("id", "")[:8],
            "healthy": {True: "yes", False: "no", None: "unknown"}.get(
                agent.get("healthy"), "unknown"
            ),
            "status": agent.get("remote_config_status") or "",
            "type": agent.get("agent_type") or "",
            "environment": description_attribute(agent.get("description"), "environment")
            or "",
            "host_arch": description_attribute(agent.get("description"), "host_arch")
            or "",
        }
        for agent in agents
    ]


st.header("Groups")
st.caption(
    "A group is a **saved SQL selection**: search first, then name the query. "
    "Membership is resolved on read, so a group cannot drift from the fleet. "
    "The language guide is `docs/sql-agent-queries.md`."
)

schema = _payload(
    admin_request("GET", "/agents/query/schema", required_role=ROLE_VIEWER), {}
) or {}
groups_payload = _payload(
    admin_request(
        "GET", "/groups", required_role=ROLE_VIEWER, params={"include_archived": True}
    ),
    {},
) or {}
group_rows = groups_payload.get("groups") or []
by_name = {row.get("name"): row for row in group_rows if row.get("name")}

if SQL_KEY not in st.session_state:
    st.session_state[SQL_KEY] = DEFAULT_SQL

# ---------------------------------------------------------------- search


st.subheader("1. Search agents")
st.caption(
    "`SELECT * FROM agents WHERE ...` — only reads agent descriptions and liveness. "
    "Use the suggestion picker to see the columns and values this fleet actually "
    "reports; it is built from the live schema, not a static list."
)

find_cols = st.columns([2, 1])
with find_cols[0]:
    find_group = st.selectbox(
        "Find by group (loads its query into the editor)",
        options=[""] + sorted(by_name),
        key="groups_find_group",
        format_func=lambda value: "— choose a saved group —" if not value else value,
    )
with find_cols[1]:
    st.write("")
    load_clicked = st.button("Load query", key="groups_find_load")
if load_clicked and find_group:
    selected = by_name.get(find_group) or {}
    if selected.get("query"):
        st.session_state[SQL_KEY] = selected["query"]
    else:
        st.session_state[SQL_KEY] = ""
        st.info(
            "This is a legacy filter-document group; it has no SQL query to load. "
            "Preview it below, or replace its definition with a query under "
            "**Saved groups**."
        )
    st.rerun()

examples = schema.get("examples") or [DEFAULT_SQL]
suggest_cols = st.columns([4, 1])
with suggest_cols[0]:
    suggestion = st.selectbox(
        "Suggestions from the live fleet",
        options=examples,
        key="groups_suggestion",
        help="Core columns, observed attribute values, and an example of combining two "
        "attributes. Built from GET /agents/query/schema.",
    )
with suggest_cols[1]:
    st.write("")
    if st.button("Insert", key="groups_insert_suggestion"):
        st.session_state[SQL_KEY] = suggestion
        st.rerun()

attribute_columns = [
    column for column in (schema.get("columns") or []) if column.get("kind") == "attribute"
]
if attribute_columns:
    with st.expander("Columns and values observed on connected agents", expanded=False):
        st.dataframe(
            [
                {
                    "column": column.get("sql_name"),
                    "agents": column.get("agent_count"),
                    "values": ", ".join(column.get("values") or [])[:120],
                }
                for column in attribute_columns
            ],
            use_container_width=True,
            hide_index=True,
        )

sql = st.text_area(
    "SQL selection",
    key=SQL_KEY,
    height=170,
    help="Only SELECT from agents. A saved group may not use LIMIT or OFFSET.",
)
if st.button("Run search", type="primary", key="groups_run_search"):
    response = admin_request(
        "GET", "/agents", required_role=ROLE_VIEWER, params={"q": sql}
    )
    result = _payload(response, {}) or {}
    if response is not None and response.status_code == 200:
        agents = result.get("agents") or []
        st.session_state["groups_search_result"] = {
            "count": result.get("count", len(agents)),
            "agents": agents,
            "query": result.get("query") or sql,
        }
    else:
        st.session_state["groups_search_result"] = None
        st.error(f"Query refused ({getattr(response, 'status_code', 'transport')}):")
        st.code(_detail(response))

search_result = st.session_state.get("groups_search_result")
if search_result:
    st.metric("Agents matched", search_result.get("count", 0))
    if search_result.get("agents"):
        st.dataframe(
            _agent_table(search_result["agents"]),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info(
            "No agents matched. Check the attribute names in the suggestions above — "
            "an unknown attribute matches nothing rather than failing the query."
        )
    st.code(search_result.get("query") or "", language="sql")

# ------------------------------------------------------- save as a group


st.subheader("2. Save this query as a group")
st.caption(
    "Saving a group pushes nothing. A group with zero members is valid to save and "
    "impossible to push to. `include`/`exclude` are human pins: they still apply when "
    "the query does not match."
)

with st.form("groups_save_form"):
    save_cols = st.columns([2, 2])
    with save_cols[0]:
        new_name = st.text_input(
            "Name", key="groups_new_name", placeholder="macos-au",
            help="Lowercase a-z, 0-9, '.', '_' and '-'; unique regardless of case.",
        )
    with save_cols[1]:
        new_owner = st.text_input(
            "Owner", key="groups_new_owner", placeholder="platform-oncall"
        )
    save_query = st.text_area(
        "Query", value=st.session_state.get(SQL_KEY) or DEFAULT_SQL, height=140
    )
    pin_cols = st.columns(2)
    with pin_cols[0]:
        include_text = st.text_input(
            "Force in (agent ids, comma separated)", key="groups_new_include"
        )
    with pin_cols[1]:
        exclude_text = st.text_input(
            "Force out (agent ids, comma separated)", key="groups_new_exclude"
        )
    description = st.text_input(
        "Description", key="groups_new_description",
        placeholder="macOS gateways in Australia. Owner: platform-oncall.",
    )
    save_submitted = st.form_submit_button("Save group", type="primary")

if save_submitted:
    response = admin_request(
        "POST",
        "/groups",
        required_role=ROLE_OPERATOR,
        json={
            "name": new_name,
            "query": save_query,
            "include": [item.strip() for item in include_text.split(",") if item.strip()],
            "exclude": [item.strip() for item in exclude_text.split(",") if item.strip()],
            "description": description or None,
            "owner": new_owner or None,
        },
    )
    if response is not None and response.status_code == 200:
        created = _payload(response, {}) or {}
        st.success(
            f"Saved `{created.get('name')}` with {created.get('member_count', 0)} "
            "member(s). Saving a group pushes nothing."
        )
        st.rerun()
    elif response is not None:
        st.error(f"Group refused ({response.status_code}):")
        st.code(_detail(response))

# ------------------------------------------------------------ saved groups


st.subheader("3. Saved groups")
if not group_rows:
    st.caption("No groups saved yet. Run a query above and save it to create one.")
else:
    st.dataframe(
        [
            {
                "group": row.get("name"),
                "kind": row.get("selector_kind") or "definition",
                "members": row.get("member_count"),
                "health-defined": ", ".join(row.get("health_defined") or []) or "",
                "owner": row.get("owner") or "",
                "updated": format_local_time(row.get("updated_at")),
                "archived": format_local_time(row.get("archived_at"))
                if row.get("archived_at")
                else "",
            }
            for row in group_rows
        ],
        use_container_width=True,
        hide_index=True,
    )

    selected_name = st.selectbox(
        "Inspect a group",
        options=sorted(by_name),
        key=GROUP_KEY,
    )
    selected = by_name.get(selected_name) or {}
    detail_cols = st.columns([2, 1, 1])
    with detail_cols[0]:
        st.metric("Members now", selected.get("member_count", 0))
        if selected.get("selector_kind") == "query":
            st.code(selected.get("query") or "", language="sql")
        else:
            st.caption("Legacy filter-document group (no SQL query yet).")
            st.code(json.dumps(selected.get("definition") or {}, indent=2), language="json")
        sources = selected.get("sources") or {}
        selector_bucket = sources.get("query") or sources.get("definition") or []
        st.caption(
            f"{len(selector_bucket)} from the selector · "
            f"{len(sources.get('included') or [])} pinned in · "
            f"{len(sources.get('excluded') or [])} pinned out"
        )
        if selected.get("unknown_pins"):
            st.warning(
                "Pins for agents that have never reported: "
                + ", ".join(selected["unknown_pins"])
            )
        if selected.get("health_defined"):
            st.warning(
                "This group filters on **"
                + ", ".join(selected["health_defined"])
                + "**. Pushing to it is refused unless you use the stored membership "
                "(agents drop out exactly when they break)."
            )
    with detail_cols[1]:
        st.write("")
        if st.button("Preview membership", key="groups_preview"):
            response = admin_request(
                "POST",
                f"/groups/{selected_name}/preview",
                required_role=ROLE_VIEWER,
                json={},
            )
            preview = _payload(response, {}) or {}
            if response is not None and response.status_code == 200:
                st.metric("Members now", preview.get("count", 0))
                st.caption(
                    f"Previously {preview.get('previous_count', 0)} · "
                    f"{len(preview.get('dropped') or [])} dropped · "
                    f"{len(preview.get('added') or [])} added"
                )
                for label, key_name in (("Dropped", "dropped"), ("Added", "added")):
                    if preview.get(key_name):
                        st.write(f"**{label}:** " + ", ".join(preview[key_name]))
            else:
                st.error(f"Preview refused ({getattr(response, 'status_code', 'transport')}):")
                st.code(_detail(response))
    with detail_cols[2]:
        st.write("")
        if selected.get("archived_at"):
            if st.button("Restore", key="groups_restore"):
                response = admin_request(
                    "POST",
                    f"/groups/{selected_name}/restore",
                    required_role=ROLE_OPERATOR,
                    json={},
                )
                if response is not None and response.status_code == 200:
                    st.success(f"Restored `{selected_name}`.")
                    st.rerun()
                else:
                    st.error(f"Restore refused ({getattr(response, 'status_code', 'transport')}):")
                    st.code(_detail(response))
        else:
            if st.button("Archive", key="groups_archive"):
                response = admin_request(
                    "DELETE", f"/groups/{selected_name}", required_role=ROLE_OPERATOR
                )
                if response is not None and response.status_code == 200:
                    st.success(f"Archived `{selected_name}`.")
                    st.rerun()
                else:
                    st.error(f"Archive refused ({getattr(response, 'status_code', 'transport')}):")
                    st.code(_detail(response))

    with st.form("groups_edit_form"):
        st.markdown("**Edit the query** — future pushes only; a revision keeps its snapshot.")
        edit_query = st.text_area(
            "Query",
            value=selected.get("query") or "",
            height=140,
            key=f"groups_edit_{selected_name}",
            help="Replace the selector with SQL. Leave blank to keep the current query.",
        )
        edit_description = st.text_input(
            "Description", value=selected.get("description") or "",
            key=f"groups_edit_desc_{selected_name}",
        )
        edit_submitted = st.form_submit_button("Save query")
    if edit_submitted:
        body = {}
        if edit_query.strip():
            body["query"] = edit_query
        if edit_description != (selected.get("description") or ""):
            body["description"] = edit_description
        if not body:
            st.info("Nothing to change.")
        else:
            response = admin_request(
                "PUT",
                f"/groups/{selected_name}",
                required_role=ROLE_OPERATOR,
                json=body,
            )
            if response is not None and response.status_code == 200:
                st.success(f"Updated `{selected_name}`.")
                st.rerun()
            else:
                st.error(f"Edit refused ({getattr(response, 'status_code', 'transport')}):")
                st.code(_detail(response))
