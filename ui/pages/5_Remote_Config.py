"""Remote Config page (#104) — the operator surface for config push.

This page can take down a fleet, so it is built around **review before action**, not
around a big red "Push" button. The order is the epic's order, and each step is a
separate Streamlit form so a rerun cannot skip one:

    1 Target   → who? (filters, live count, blast-radius breakdown)
    2 Edit     → the YAML, prefilled from what an agent runs today
    3 Validate → the #100 pipeline, no storage
    4 Propose  → a draft revision (operator) + the dry-run batch plan
    5 Approve  → create the rollout (approver, separate form)
    6 Roll out → live per-batch / per-agent progress, gate countdown
    7 Stop     → abort (cannot recall) vs roll back (a gated rollout of its own)
    8 History  → rollouts and revisions, with who approved what

Three rules this page must not break, because the server cannot save us from them:

* a **redacted** body is never prefilled or pushed (#98) — the page says so instead of
  handing over a document whose passwords say `<redacted>`;
* **abort and rollback are visibly different actions with different roles**, because
  "stop promoting" and "put 300 agents back" are not the same decision;
* a **paused** rollout shows its one reason, because the operator's next action
  differs per reason and "paused" alone is not an answer.

Auth is the #105 session token via `ui/shared.admin_request`, so the buttons are role
gated in the UI *and* re-authorised by the server. The UI is ergonomics; the server is
the boundary.
"""

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.shared import (  # noqa: E402
    ROLE_APPROVER,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    admin_request,
    description_attribute,
    format_local_time,
    require_admin,
    render_sidebar,
)
from ui.config_view import EDITOR_CONTENT_TYPES, editable_config  # noqa: E402

render_sidebar()

TARGET_MODE_KEY = "rc_target_mode"
GROUP_KEY = "rc_group"
BODY_KEY = "rc_body"
CONTENT_TYPE_KEY = "rc_content_type"
OBJECT_KEY_KEY = "rc_object_key"
ROLE_KEY = "rc_object_role"
PROPOSAL_KEY = "rc_proposal"
PLAN_KEY = "rc_plan"
# The health gate the rollout is created with, and the server's configured default it
# was seeded from. Two keys, because the second is only ever read back as documentation
# of what the first started as.
GATE_KEY = "rc_health_gate"
GATE_DEFAULT_KEY = "rc_health_gate_default"
ROLLOUT_KEY = "rc_rollout"
ROLLOUT_AUTOFILL_KEY = "rc_rollout_autofill"
REVEAL_KEY = "rc_reveal"
PREFILL_KEY = "rc_prefill_message"

STATUS_ICON = {
    "pending": "⚪",
    "delivered": "📤",
    "applying": "⏳",
    "applied": "✅",
    "failed": "❌",
    "reverted": "↩️",
    "paused": "⏸️",
}

PAUSE_HELP = {
    "failed": "An agent reported FAILED. The revision is poisoned and will not be retried.",
    "auto_reverted": "An agent restored its last working config — the supervisor refused this one.",
    "agent_went_silent": "An agent stopped reporting after delivery, so its health cannot be verified.",
    "unhealthy": "An agent reports healthy=false after being delivered this config.",
    "config_conflict": (
        "An agent applied the config, but its daemon is merging a second config source "
        "that schedules the same query names — so the effective config is not what this "
        "server thinks it is. Fix it on the agent host: delete the duplicate from the "
        "local config file, or run osqueryd with `--config_plugin=opamp "
        "--extensions_require=opamp --extensions_timeout=30` so the agent is the only "
        "source (both flags are required — without `--extensions_require` osqueryd exits "
        "with `Unknown registry plugin: opamp`). Then **Resume**: the revision is fine, so "
        "nothing has to be re-proposed."
    ),
    "delivery_unconfirmed": "An agent never acknowledged its delivery (attempt budget exhausted).",
    "superseded": "Another revision replaced this one on at least one agent.",
    "ttl_expired": "Nobody acted within the rollout TTL, so it was cancelled.",
    "aborted": "An operator aborted the rollout. Already-delivered configs were not recalled.",
}


def _payload(response, default=None):
    """Parse a response body, tolerating an empty or non-JSON body."""
    if response is None:
        return default
    try:
        return response.json()
    except Exception:
        return default


def _detail(response):
    """The server's `detail`, rendered as text instead of a Python repr."""
    body = _payload(response, {})
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("message") or json.dumps(detail, indent=2)
        if detail:
            return str(detail)
        return json.dumps(body, indent=2)
    return str(body)


def _config_map(body, content_type, role="", name=""):
    """The one-object `config_map` a revision stores.

    `name` is the map key — the agent-side **config object name**, not decoration. The
    spec types it `map<string, AgentConfigFile>` and leaves the meaning to the agent: a
    collector is happy with `""`, osquery's OpAMP extension refuses anything but
    `osquery.json` (it reports `expected a single osquery.json object, got ['']`). It was
    hardcoded to `""` here, so every proposal made from this page was misaddressed and
    the agent failed it — correctly, and the rollout paused at batch 0.
    """
    entry = {"body": body, "content_type": content_type or "text/yaml"}
    if role:
        entry["role"] = role
    return {(name or "").strip(): entry}


def _is_redacted(text):
    return "<redacted>" in (text or "")


def _plan_body(gate=None):
    """The dry-run body, carrying a gate only when there is one to carry.

    `0` is a legal gate (`CONFIG_PUSH_HEALTH_GATE_SECONDS=0` in a lab), so the test is
    `is not None`, never truthiness: `gate or None` would turn "no waiting" back into
    "whatever the server defaults to".
    """
    body = {"dry_run": True}
    if gate is not None:
        body["health_gate_seconds"] = int(gate)
    return body


def _rollout_body(note, restart, gate=None):
    """The create body: the gate in the box is **the** gate, sent explicitly.

    Omitting it makes the server fall back to `CONFIG_PUSH_HEALTH_GATE_SECONDS`, so an
    operator who changed the number and then approved would silently get 90s again —
    the box would be a suggestion, not a decision.
    """
    body = {"note": note or None, "restart_after_apply": bool(restart)}
    if gate is not None:
        body["health_gate_seconds"] = int(gate)
    return body


def _gate_override():
    """The gate currently in the box, or `None` when the server has not reported one yet."""
    value = st.session_state.get(GATE_KEY)
    return None if value is None else int(value)


def _prefill_from_agent(agent_id):
    """Load an agent's reported config into the editor, **decoded** (#131).

    The report is protobuf `MessageToDict`, which renders every `bytes` body as
    base64. Prefilling that verbatim filled the box with base64, so the next proposal
    delivered base64 *as the config body* — an agent whose supervisor would then fail
    to start the config it was just handed. The decode is the shared one
    (`ui.config_view.editable_config` → `server.effective_config`), never a local one.

    Deliberately a callback: it writes the content-type/role widgets as well, which is
    only legal before those widgets are instantiated.
    """
    detail = _payload(
        admin_request("GET", f"/agent/{agent_id}", required_role=ROLE_VIEWER), {}
    ) or {}
    if detail.get("effective_config_redacted"):
        st.session_state[PREFILL_KEY] = (
            "warning",
            "That agent's reported config is **redacted** (`<redacted>` markers). It is "
            "deliberately not loaded into the editor: pushing a redacted body back would "
            "wipe the fleet's credentials (#98). Paste the real document, or read it with "
            "`agents config get --reveal` as an approver.",
        )
        return

    editable = editable_config(detail.get("effective_config"))
    if editable is None:
        st.session_state[PREFILL_KEY] = (
            "info",
            "That agent has not reported a readable effective config yet.",
        )
        return

    st.session_state[BODY_KEY] = editable.body
    st.session_state[CONTENT_TYPE_KEY] = editable.content_type
    # The name comes from the report: proposing under a different key is how a good body
    # gets refused by the agent (see `_config_map`).
    st.session_state[OBJECT_KEY_KEY] = editable.key or ""
    st.session_state[ROLE_KEY] = editable.role
    message = (
        f"Loaded object `{editable.key or '<empty key>'}` from {agent_id} as "
        f"{editable.content_type}, decoded; the revision will be keyed "
        f"`{editable.key or '<empty key>'}` to match."
    )
    if editable.others:
        message += " Not prefilled (one object per revision): " + ", ".join(
            f"`{key or '<empty key>'}`" for key, _ in editable.others
        )
    st.session_state[PREFILL_KEY] = ("success", message)


# ------------------------------------------------------------------ posture


st.title("⚙️ Remote Config")
st.caption(
    "Propose → approve → canary → gated batches. Nothing is delivered to a whole fleet "
    "without evidence that the canary came back healthy."
)

session = require_admin(ROLE_VIEWER)
posture = _payload(admin_request("GET", "/config/posture", required_role=ROLE_VIEWER), {}) or {}

requirements = posture.get("requirements") or {}
unmet = posture.get("unmet") or []
if unmet:
    st.error(
        "**Config push is blocked by the security posture.** Unmet: "
        + ", ".join(f"`{name}`" for name in unmet)
        + ". Push stays refused until every requirement is met — see `/config/posture`."
    )
elif (posture.get("config_push") or {}).get("insecure"):
    st.error(
        "**Config push is enabled on an insecure posture** (no TLS, agent auth or audit). "
        "Do not use this on a real fleet."
    )
elif not posture.get("allowed"):
    st.info(
        "Config push is **disabled**. Set `OPAMP_CONFIG_PUSH_ENABLED=true` on a server "
        "whose posture passes, or read the [Help page](/Help) for the agent setup."
    )
for name, state in sorted(requirements.items()):
    if state.get("met"):
        continue
    st.caption(f"✗ {name}: {state.get('detail')}")

# ------------------------------------------------------------------ 1 target


st.header("1. Target", divider="gray")

# #111: a saved selector and an ad-hoc filter are two different things, and the page
# says so: the picker shows the live member count, the definition and which members
# came from `include`/`exclude`. A group is submitted as `{"group": name}` — it is
# resolved again by the server, so the list shown here cannot disagree with the push.
target_mode = st.radio(
    "Target by",
    ["Ad-hoc filter", "Saved group"],
    horizontal=True,
    key=TARGET_MODE_KEY,
    help="A saved group is a named SQL selection (or a legacy filter document). It is "
    "the same filter spelled once, and it records what the name meant at approval time.",
)
saved_group = None
if target_mode == "Saved group":
    groups_response = admin_request("GET", "/groups", required_role=ROLE_VIEWER)
    group_rows = (_payload(groups_response, {}) or {}).get("groups") or []
    if not group_rows:
        st.info(
            "No saved groups yet. Search the fleet and save a query on the **Groups** "
            "page — a group stores a selector, not a member list."
        )
    else:
        by_name = {row["name"]: row for row in group_rows}
        picked = st.selectbox(
            f"Group ({len(group_rows)} saved)",
            options=list(by_name),
            format_func=lambda name: (
                f"{name} — {by_name[name].get('member_count', 0)} member(s)"
            ),
            key=GROUP_KEY,
        )
        saved_group = by_name.get(picked)
    if saved_group:
        if saved_group.get("selector_kind") == "query":
            st.code(saved_group.get("query") or "", language="sql")
        else:
            st.code(
                json.dumps(saved_group.get("definition") or {}, indent=2), language="json"
            )
        health_defined = saved_group.get("health_defined") or []
        if health_defined:
            st.warning(
                "This group is defined on **"
                + ", ".join(health_defined)
                + "**, so it drops the agents that break — exactly the ones a config "
                "push is usually meant to fix. The API refuses a push to it unless you "
                "send `use_stored_resolution`, and this page does not offer that: edit "
                "the query on the **Groups** page instead."
            )
        sources = saved_group.get("sources") or {}
        pinned_in = len(sources.get("included") or [])
        pinned_out = len(sources.get("excluded") or [])
        selector_members = sources.get("query") or sources.get("definition") or []
        st.caption(
            f"{len(selector_members)} from the selector · "
            f"{pinned_in} pinned in · {pinned_out} pinned out"
        )
        if saved_group.get("unknown_pins"):
            st.warning(
                "Pinned agent(s) that have never reported: "
                + ", ".join(saved_group["unknown_pins"])
            )
        if not saved_group.get("member_count"):
            st.warning(
                "This group has **zero** members right now. An empty group is a valid "
                "thing to save, but a push that ships nothing is refused — check the "
                "query against the live schema under **Groups**."
            )

filter_cols = st.columns([1, 1, 1, 1])
with filter_cols[0]:
    healthy_filter = st.selectbox(
        "Health", ["any", "true", "false", "unknown"], key="rc_filter_healthy",
        help="Same semantics as `GET /agents?healthy=`.",
    )
with filter_cols[1]:
    status_filter = st.text_input(
        "remote_config_status", key="rc_filter_status",
        placeholder="APPLIED",
        help="Case-insensitive; reserved names win over description attributes.",
    )
with filter_cols[2]:
    attr_key = st.text_input("Attribute key", key="rc_filter_attr_key", placeholder="environment")
with filter_cols[3]:
    attr_value = st.text_input("Attribute value", key="rc_filter_attr_value", placeholder="prod")

params = {}
if healthy_filter != "any":
    params["healthy"] = healthy_filter
if status_filter.strip():
    params["remote_config_status"] = status_filter.strip()
if attr_key.strip() and attr_value.strip():
    params[attr_key.strip()] = attr_value.strip()

agents_response = admin_request("GET", "/agents", required_role=ROLE_VIEWER, params=params)
agents = (agents_response or {}) and (_payload(agents_response, {}) or {}).get("agents", [])
agents = agents or []
if saved_group is not None:
    # Membership is the group's, not the (ignored) filter's: show exactly the agents
    # that group resolves to, including pinned-in ones that fail the definition.
    members = set(saved_group.get("members") or [])
    agents = [agent for agent in agents if agent.get("id") in members]

if not agents:
    # #121: whether an agent applies a pushed config is a property of its product, not
    # of this page — the profile dictionary says which agents accept one, so the page
    # does not claim to know what a "bare collector" does.
    st.warning(
        "No agents match this selector. A rollout needs at least one agent that "
        "advertises `AcceptsRemoteConfig`; which agents do is described under "
        "**Known agent types** on the Help page."
    )

counts = {}
for agent in agents:
    environment = description_attribute(
        agent.get("description"), "deployment.environment", "environment"
    ) or "unknown"
    counts[environment] = counts.get(environment, 0) + 1

metric_cols = st.columns(4)
metric_cols[0].metric("Agents matched", len(agents))
metric_cols[1].metric("Healthy", sum(1 for agent in agents if agent.get("healthy") is True))
metric_cols[2].metric("Accepts config", sum(
    1
    for agent in agents
    if (agent.get("capabilities") or 0) & 0x2
))
metric_cols[3].metric("Environments", len(counts))
if counts:
    st.caption(
        "Blast radius: "
        + ", ".join(f"{count} × {name}" for name, count in sorted(counts.items()))
    )

target_ids = [agent.get("id") for agent in agents if agent.get("id")]
with st.expander(f"Agents in scope ({len(target_ids)})", expanded=False):
    st.dataframe(
        [
            {
                "agent": agent.get("id"),
                "healthy": agent.get("healthy"),
                "config": agent.get("remote_config_status"),
                "accepts_config": bool((agent.get("capabilities") or 0) & 0x2),
            }
            for agent in agents
        ],
        use_container_width=True,
        hide_index=True,
    )

# -------------------------------------------------------------------- 2 edit


st.header("2. Edit", divider="gray")
edit_cols = st.columns([1, 1, 1, 2])
with edit_cols[0]:
    content_type = st.selectbox(
        "Content type", list(EDITOR_CONTENT_TYPES), key=CONTENT_TYPE_KEY
    )
with edit_cols[1]:
    object_key = st.text_input(
        "Config object name",
        key=OBJECT_KEY_KEY,
        placeholder="e.g. osquery.json",
        help=(
            "The `config_map` key the agent looks the object up by. Leave it blank for "
            "an agent that takes an unnamed object (the OpenTelemetry collector), and "
            "use the name the agent reports — `Prefill` fills it in. An osquery agent "
            "refuses anything but `osquery.json` and reports FAILED, which pauses the "
            "rollout at batch 0."
        ),
    )
with edit_cols[2]:
    object_role = st.text_input("Object role (optional)", key=ROLE_KEY, placeholder="")
with edit_cols[3]:
    prefill_id = st.selectbox(
        "Prefill from agent", [""] + target_ids, key="rc_prefill",
        help="Copies that agent's current effective config into the editor, decoded.",
    )

if prefill_id:
    st.button(
        "Prefill from this agent's effective config",
        on_click=_prefill_from_agent,
        args=(prefill_id,),
    )

prefill_message = st.session_state.pop(PREFILL_KEY, None)
if prefill_message:
    level, text = prefill_message
    getattr(st, level)(text)

if BODY_KEY not in st.session_state:
    st.session_state[BODY_KEY] = ""

body = st.text_area(
    "Config body",
    key=BODY_KEY,
    height=320,
    help=(
        "One config object, as text. Paste the document itself — a body that is base64 "
        "(as a raw report renders it) would be delivered to the agent as base64, so "
        "`Prefill` decodes it for you and `Validate` flags it if you paste it by hand."
    ),
)

if _is_redacted(body):
    st.error(
        "This body contains `<redacted>`. The server refuses it on purpose: pushing a "
        "redacted document would wipe every credential in the fleet (#98)."
    )

# ---------------------------------------------------------------- 3 validate


st.header("3. Validate", divider="gray")
# Validation runs on the button, never on every rerun: a per-keystroke POST to the
# validator is noise in the request log and in a shared server's CPU.
if st.button("Validate", type="secondary"):
    validate_body = {
        "config_map": _config_map(
            body, content_type, object_role.strip(), object_key
        ),
        "agent_id": target_ids[0] if target_ids else None,
    }
    response = admin_request(
        "POST", "/config/validate", required_role=ROLE_OPERATOR, json=validate_body
    )
    st.session_state["rc_verdict"] = _payload(response, {}) or {}

verdict = st.session_state.get("rc_verdict") or {}
if verdict:
    if verdict.get("valid"):
        st.success(
            f"Valid — {verdict.get('objects', 1)} object(s), "
            f"{verdict.get('size_bytes', 0)} bytes, hash `{(verdict.get('config_hash') or '')[:12]}…`"
        )
    else:
        st.error("Validation failed. Fix these before proposing:")
    for finding in verdict.get("details") or []:
        level = (finding.get("severity") or "error").lower()
        render = st.error if level == "error" else st.warning
        render(f"`{finding.get('path')}` — {finding.get('message')}")
    for warning in verdict.get("warnings") or []:
        if isinstance(warning, str):
            st.warning(warning)
    policy = verdict.get("policy") or {}
    if policy.get("status") == "violations":
        st.error("The policy gate rejected this config: " + json.dumps(policy, indent=2))
    elif policy.get("status") == "skipped":
        st.info("OPA is not enabled, so the policy gate was **skipped** — not passed.")

# ----------------------------------------------------------------- 4 propose


st.header("4. Propose", divider="gray")
st.caption(
    "A proposal stores one immutable revision per target and delivers **nothing**. "
    "An approver decides (or creates the rollout, which is the same decision)."
)

note = st.text_input("Note (recorded in the audit log)", key="rc_note")
# #111: a group that resolved smaller than its last recorded resolution needs the
# dropped list acknowledged; the checkbox is the UI spelling of `confirm_scope_change`.
confirm_scope_change = st.checkbox(
    "Confirm the membership change this group produced (required when it resolved smaller)",
    key="rc_confirm_scope_change",
)
if st.button("Propose revision", type="primary", disabled=not body.strip()):
    if saved_group is not None:
        scope = {"group": saved_group["name"]}
    else:
        scope = {"agents": target_ids} if target_ids else None
    if not scope:
        st.error("Nothing to propose to: no agents matched the selector above.")
    else:
        response = admin_request(
            "POST",
            "/config/revisions",
            required_role=ROLE_OPERATOR,
            json={
                "config_map": _config_map(
                    body, content_type, object_role.strip(), object_key
                ),
                "scope": scope,
                "note": note or None,
                "confirm_scope_change": bool(confirm_scope_change),
            },
        )
        if response is not None and response.status_code == 200:
            proposal = _payload(response, {}) or {}
            st.session_state[PROPOSAL_KEY] = proposal
            st.session_state.pop(PLAN_KEY, None)
            st.success(
                f"Proposed `{proposal.get('proposal_id')}` for {proposal.get('agents')} agent(s)."
            )
        elif response is not None:
            st.error(f"Proposal refused ({response.status_code}):")
            st.code(_detail(response))

proposal = st.session_state.get(PROPOSAL_KEY) or {}
proposal_id = proposal.get("proposal_id")
if proposal_id:
    st.info(
        f"Proposal `{proposal_id}` — {proposal.get('agents')} agent(s), hash "
        f"`{(proposal.get('config_hash') or '')[:12]}…`, status `{proposal.get('status')}`."
    )
    if st.button("Plan the rollout (dry run)"):
        response = admin_request(
            "POST", "/config/rollouts", required_role=ROLE_OPERATOR,
            params={"proposal_id": proposal_id},
            json=_plan_body(_gate_override()),
        )
        plan = _payload(response, {}) or {}
        if plan.get("dry_run"):
            st.session_state[PLAN_KEY] = plan
        else:
            st.error("Could not plan: " + _detail(response))

plan = st.session_state.get(PLAN_KEY) or {}
if plan:
    st.subheader("Rollout plan (nothing has been delivered)")
    plan_batch = plan.get("batch") or {}
    plan_cols = st.columns(4)
    plan_cols[0].metric("Batches", plan_batch.get("count"))
    plan_cols[1].metric("Canary", plan["batches"][0]["agents"] if plan.get("batches") else 0)
    plan_cols[2].metric("Batch cap", plan_batch.get("max_agents_per_batch"))
    plan_cols[3].metric("Health gate", f"{plan_batch.get('health_gate_seconds')}s")
    st.dataframe(
        [
            {
                "batch": batch["batch"] + 1,
                "agents": batch["agents"],
                "first agent": batch["agent_ids"][0] if batch["agent_ids"] else "",
            }
            for batch in plan.get("batches") or []
        ],
        use_container_width=True,
        hide_index=True,
    )

    # The health gate is the one number an operator sets per rollout, so it is a field
    # and not a fact read off the server. `CONFIG_PUSH_HEALTH_GATE_SECONDS` is only the
    # *seed*: the first plan sends no override and reports the value actually in force,
    # the box is filled from that, and from then on the box is what the rollout is
    # created with — always sent, so an empty box can never quietly mean "90 again".
    # Seeded once and never rewritten, so a re-plan cannot move the number under the
    # operator's hands mid-decision. Must happen before the widget exists (same rule as
    # the rollout-id autofill below).
    reported_gate = plan_batch.get("health_gate_seconds")
    if GATE_KEY not in st.session_state and isinstance(reported_gate, int):
        st.session_state[GATE_KEY] = int(reported_gate)
        st.session_state[GATE_DEFAULT_KEY] = int(reported_gate)
    if GATE_KEY in st.session_state:
        gate = st.number_input(
            "Health gate (seconds the whole batch must stay green)",
            min_value=0,
            max_value=86400,
            step=5,
            key=GATE_KEY,
            help=(
                "Counted from the last delivery in the batch. The rollout is created "
                "with **this** number — it is not a hint and it does not follow the "
                "server's default once the plan exists. 0 promotes as soon as every "
                "agent reports APPLIED with our hash, with no settling time at all."
            ),
        )
        server_default = st.session_state.get(GATE_DEFAULT_KEY)
        if server_default is not None and int(gate) != int(server_default):
            st.caption(
                f"The server default is {server_default}s; this rollout will use "
                f"**{int(gate)}s**."
            )
        if isinstance(reported_gate, int) and int(gate) != int(reported_gate):
            st.warning(
                f"The plan above used {reported_gate}s. Re-plan to check it at "
                f"{int(gate)}s before approving — the batches do not change, the "
                "waiting does."
            )
        if st.button("Re-plan with this gate (dry run)"):
            response = admin_request(
                "POST", "/config/rollouts", required_role=ROLE_OPERATOR,
                params={"proposal_id": proposal_id},
                json=_plan_body(_gate_override()),
            )
            replanned = _payload(response, {}) or {}
            if replanned.get("dry_run"):
                st.session_state[PLAN_KEY] = replanned
                st.rerun()
            else:
                st.error("Could not re-plan: " + _detail(response))

    restart = st.checkbox(
        "Restart each agent once its batch passes the gate",
        key="rc_restart",
        help="Queues a restart command (#84). Only agents advertising "
        "AcceptsRestartCommand receive it; the others are skipped and logged.",
    )

    st.caption(
        f"Approving and rolling out needs the **approver** role. You are "
        f"**{session.role}**." if not session.can(ROLE_APPROVER) else
        "You may approve this rollout."
    )
    if st.button(
        "Approve & roll out",
        type="primary",
        disabled=not session.can(ROLE_APPROVER),
        help="Creates the rollout: approves the proposal, releases the canary only.",
    ):
        response = admin_request(
            "POST", "/config/rollouts", required_role=ROLE_APPROVER,
            params={"proposal_id": proposal_id},
            json=_rollout_body(note, restart, _gate_override()),
        )
        if response is not None and response.status_code == 200:
            st.session_state[ROLLOUT_KEY] = _payload(response, {}) or {}
            st.success("Rollout started — only the canary batch has been released.")
        elif response is not None:
            st.error(f"Rollout refused ({response.status_code}):")
            st.code(_detail(response))

# ---------------------------------------------------------------- 6 progress

# #104: a rollout created in this page must appear in the live-progress field. The
# field is a keyed widget, so its stored value wins over `value=` on every rerun —
# without this, approving starts a rollout but the field stays empty and the operator
# has to copy the id from the History tab. Set it *before* the widget is created;
# setting it in the approve handler, further down, is a StreamlitAPIException.
_created_rollout_id = (st.session_state.get(ROLLOUT_KEY) or {}).get("rollout_id")
if _created_rollout_id and st.session_state.get(ROLLOUT_AUTOFILL_KEY) != _created_rollout_id:
    st.session_state["rc_rollout_id"] = _created_rollout_id
    st.session_state[ROLLOUT_AUTOFILL_KEY] = _created_rollout_id

st.header("5. Roll out", divider="gray")
rollout_id = st.text_input(
    "Rollout id", value=(st.session_state.get(ROLLOUT_KEY) or {}).get("rollout_id", ""),
    key="rc_rollout_id",
)
if rollout_id:
    if st.button("Refresh"):
        st.rerun()
    rollout = _payload(
        admin_request("GET", f"/config/rollouts/{rollout_id}", required_role=ROLE_VIEWER), {}
    ) or {}
    if not rollout.get("rollout_id"):
        st.error(f"Rollout `{rollout_id}` not found.")
    else:
        batch_info = rollout.get("batch") or {}
        states = (rollout.get("agents") or {}).get("states") or {}
        status = rollout.get("status")

        status_cols = st.columns(4)
        status_cols[0].metric("Status", status)
        status_cols[1].metric(
            "Batch", f"{(batch_info.get('current') or 0) + 1}/{batch_info.get('count')}"
        )
        status_cols[2].metric("Applied", states.get("applied", 0))
        status_cols[3].metric("Failed", states.get("failed", 0) + states.get("reverted", 0))

        if status == "paused":
            reason = rollout.get("paused_reason")
            st.error(
                f"**Paused: {reason}.** {PAUSE_HELP.get(reason, '')}\n\n"
                f"{states.get('applied', 0)} applied, "
                f"{states.get('failed', 0)} failed at batch "
                f"{(batch_info.get('current') or 0) + 1}. Nothing is retried and nothing "
                "is rolled back automatically — decide below."
            )
            if rollout.get("pause_detail"):
                st.caption(rollout["pause_detail"])
            # A pause whose cause was on the agent (a second config source, a failed
            # health report that has cleared) can be re-opened instead of aborted and
            # re-proposed. A poisoned revision cannot: that is reactivate or rollback.
            if reason not in ("failed", "auto_reverted", "superseded"):
                resume_reason = st.text_input(
                    "What changed on the agent? (goes in the audit log)", key="rc_resume_reason"
                )
                if st.button("Resume this rollout", disabled=not session.can(ROLE_APPROVER)):
                    response = admin_request(
                        "POST", f"/config/rollouts/{rollout_id}/resume",
                        required_role=ROLE_APPROVER, json={"reason": resume_reason or None},
                    )
                    if response is not None and response.status_code == 200:
                        st.success(
                            "Resumed. Nothing is re-delivered; the gate re-runs on the next "
                            "tick and pauses again if the conflict is still there."
                        )
                    elif response is not None:
                        st.error(f"Resume refused ({response.status_code}): {_detail(response)}")
        elif status == "completed":
            st.success("Every agent applied the config and stayed healthy through its gate.")
        elif status == "cancelled":
            st.warning(
                f"Cancelled ({rollout.get('paused_reason')}). "
                f"{PAUSE_HELP.get(rollout.get('paused_reason'), '')}"
            )
        elif status == "aborted":
            st.warning("Aborted. Configs already delivered were **not** recalled.")
        else:
            st.info(
                "Batch started "
                f"{format_local_time(batch_info.get('started_at'))} — the next batch is "
                f"released once every agent reports APPLIED with our hash, stays healthy, "
                f"and the {batch_info.get('health_gate_seconds')}s gate has elapsed."
            )

        st.dataframe(
            [
                {
                    "batch": batch["batch"] + 1,
                    "agents": batch["agents"],
                    "status": batch["status"],
                    "states": ", ".join(
                        f"{STATUS_ICON.get(state, '•')} {count}"
                        for state, count in sorted((batch.get("states") or {}).items())
                    ),
                }
                for batch in rollout.get("batches") or []
            ],
            use_container_width=True,
            hide_index=True,
        )

# --------------------------------------------------------------------- 7 stop


st.header("6. Stop", divider="gray")
st.caption(
    "**Abort** stops promotion and cannot recall a config that is already on an agent. "
    "**Roll back** creates a new revision per applied agent and runs it as its own "
    "gated rollout — a different, bigger decision."
)

if rollout_id:
    stop_cols = st.columns(2)
    with stop_cols[0]:
        abort_reason = st.text_input("Abort reason", key="rc_abort_reason")
        if st.button("Abort promotion", disabled=not session.can(ROLE_OPERATOR)):
            response = admin_request(
                "POST", f"/config/rollouts/{rollout_id}/abort", required_role=ROLE_OPERATOR,
                json={"reason": abort_reason or None},
            )
            if response is not None and response.status_code == 200:
                st.success("Aborted. Un-promoted agents stay held; applied agents are untouched.")
            elif response is not None:
                st.error(f"Abort refused ({response.status_code}): {_detail(response)}")
    with stop_cols[1]:
        confirm_rollback = st.checkbox(
            "I understand this rolls back every agent this rollout applied", key="rc_confirm_rb"
        )
        if st.button(
            "Roll back applied agents",
            disabled=not (session.can(ROLE_APPROVER) and confirm_rollback),
        ):
            response = admin_request(
                "POST", f"/config/rollouts/{rollout_id}/rollback",
                required_role=ROLE_APPROVER, json={"note": note or None},
            )
            if response is not None and response.status_code == 200:
                st.success(
                    "Rollback rollout started — canary first, gated like any other rollout."
                )
            elif response is not None:
                st.error(f"Rollback refused ({response.status_code}): {_detail(response)}")

# ------------------------------------------------------------------ 8 history


st.header("7. History", divider="gray")
tab_rollouts, tab_revisions = st.tabs(["Rollouts", "Revisions"])

with tab_rollouts:
    listing = _payload(
        admin_request("GET", "/config/rollouts", required_role=ROLE_VIEWER), {}
    ) or {}
    rows = listing.get("rollouts") or []
    if not rows:
        st.caption("No rollouts yet.")
    else:
        st.dataframe(
            [
                {
                    "rollout": row.get("rollout_id"),
                    "status": row.get("status"),
                    "batch": f"{(row.get('batch') or {}).get('current', 0) + 1}/"
                    f"{(row.get('batch') or {}).get('count', 0)}",
                    "agents": row.get("agents"),
                    "paused": row.get("paused_reason") or "",
                    "created": format_local_time(row.get("created_at")),
                }
                for row in rows
            ],
            use_container_width=True,
            hide_index=True,
        )

with tab_revisions:
    revisions = _payload(
        admin_request("GET", "/config/revisions", required_role=ROLE_VIEWER), {}
    ) or {}
    rows = revisions.get("revisions") or []
    if not rows:
        st.caption("No revisions yet.")
    else:
        st.dataframe(
            [
                {
                    "revision": row.get("revision_id"),
                    "status": row.get("status"),
                    "agent": row.get("agent_id"),
                    "hash": (row.get("config_hash") or "")[:12],
                    "author": row.get("author"),
                    "approved by": row.get("approved_by") or "",
                    "created": format_local_time(row.get("created_at")),
                }
                for row in rows
            ],
            use_container_width=True,
            hide_index=True,
        )
    st.caption(
        "Bodies are never listed here (D8). Read one agent's config on the Agents page, "
        "where the reveal is role-gated and audited."
    )


# ------------------------------------------------------------------- groups (#132)


st.header("8. Groups", divider="gray")
st.caption(
    "A group is a **saved SQL selection** now. Search and manage groups on the "
    "**Groups** page; this page only consumes them as a push target."
)
st.markdown("Open the **Groups** page from the sidebar to search, save or edit one.")
