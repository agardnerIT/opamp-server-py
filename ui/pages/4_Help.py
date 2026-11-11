import os
import sys
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.shared import (
    agent_auth_mode,
    agent_auth_note,
    connect_snippet,
    get_server_url,
    render_sidebar,
)
from server import profiles


render_sidebar()

# #121: this page used to open with "Add this minimal config to your OpenTelemetry
# Collector", which is one product's onboarding sold as the server's. The server is
# type-blind; what an agent's config *is* belongs to the profile dictionary, so the
# page says the two things that are universal (endpoint, credential) and then hands
# the rest to whichever profile the operator is actually running.
st.markdown("## Connecting an agent")
st.markdown(
    "Every agent needs two things from this server: the **OpAMP endpoint** and, when "
    "the server requires one, an **agent token**. Everything else in its configuration "
    "belongs to the agent's product — this server stores that config as opaque content "
    "unless a profile in its dictionary says it can interpret it."
)

# #116: one snippet for the Help page and the empty fleet view, so a fix to the config
# can never land in only one of them. #124: the server's mode decides whether it carries
# an agent credential. #121: the *text* is the profile's (`bootstrap_default` answers
# which one, because an operator reading the Help page has no agent to ask yet).
auth_mode = agent_auth_mode()
bootstrap = profiles.bootstrap_default()
default_config = connect_snippet(
    get_server_url(), profile=bootstrap, agent_auth=auth_mode
)
bootstrap_filename = bootstrap.filename_for()
bootstrap_language = "yaml" if bootstrap.onboarding else "text"

st.code(default_config, language=bootstrap_language)

st.download_button(
    f"Download {bootstrap_filename}",
    default_config,
    file_name=bootstrap_filename,
    mime="text/yaml" if bootstrap_language == "yaml" else "text/plain",
    key="download_config",
)

if note := agent_auth_note(auth_mode):
    (st.warning if auth_mode == "token" else st.caption)(note)

st.divider()

st.markdown("### Known agent types")
st.markdown(
    "The server's **profile dictionary** decides what it is willing to claim about an "
    "agent: whether its config is interpreted at all, whether a component inventory or "
    "a telemetry view is meaningful, and which reports describe it. The same list is "
    "`GET /agent/types`, `opampctl agents types` and the **MCP** `list_agent_types` "
    "tool — adding a type is a YAML file and a restart, with no server release."
)

for profile in profiles.PROFILES:
    with st.expander(f"{profile.icon} {profile.display_name} — `{profile.name}`"):
        st.markdown(profile.summary)

        declares = [
            "**Configuration**: "
            + (
                ", ".join(
                    f"`{name}`" for name in sorted(
                        {ct for fmt in profile.config_formats for ct in fmt.content_types}
                    ) if name
                )
                or "any content type, opaque"
            ),
            "**Structural validation**: "
            + ("yes, this profile is judged by its own rules" if profile.validates_structure
               else "no, the body is not interpreted"),
            "**Component inventory**: " + ("yes" if profile.has_components else "no concept of one"),
            "**Telemetry view**: "
            + (profile.telemetry.title if profile.telemetry else "this agent reports none here"),
            "**Reports**: " + (", ".join(profile.reports) or "none"),
        ]
        st.markdown("  \n".join(f"- {line}" for line in declares))

        # The product's own caveats: what it does with a pushed config, what happens
        # when one is wrong. True of one product, so it lives in that product's file.
        if profile.guidance:
            st.markdown(profile.guidance)

        snippet = connect_snippet(
            get_server_url(), profile=profile, agent_auth=auth_mode
        )
        st.code(snippet, language="yaml" if profile.onboarding else "text")

st.divider()

st.markdown("### Agent tokens")
st.markdown("""
Agents authenticate with `Authorization: Bearer <token>`; the server refuses a
connection without one, and the token belongs in the agent's own config (where it
goes is the agent's business — the snippet for its type above shows the shape).

| Kind | Mint | What it is |
|---|---|---|
| **fleet** | `opampctl agent-tokens create --expires-in-days 30 --max-agents 50` | One token for many agents, capped at `max_agents`. Not tied to an agent id, so it works before any agent has connected — which is what an empty fleet needs |
| **bound** | `opampctl agents token create <agent-id>` | The credential **is** that one agent's identity: another `instance_uid` is refused |

Give a fleet token an expiry and a cap. They are what bounds the damage from a leak:
an expired token stops working on its own, and the agent that would exceed the cap is
refused (`agent_limit`) with a line in the server log. Revoking one (`Admin → Agent
tokens`, or `opampctl agent-tokens revoke <agtok_…>`) blocks connections immediately
and keeps every agent's record and config.

The token is shown **once** — the server stores only its SHA-256 — so paste it
somewhere safe when it is minted. A lost token costs one re-mint, not an outage.
""")

st.divider()

st.markdown("### Admin console security")
st.markdown("""
This UI is an **admin console**, not a public site:

- Signing in posts your name and password to `POST /auth/session` **once**; the
  password is discarded and only the short-lived bearer **token** is kept. Sessions
  expire on a server TTL and after a period of inactivity, and can be revoked.
  *Extend* on the Admin page is an audited re-auth: it issues a new token and
  revokes the old one.
- **Roles** (`viewer` < `operator` < `approver` < `admin`) decide which actions are
  offered. Hiding a button is cosmetic — the server re-checks your role on every
  request and records the action in the audit log.
- Streamlit keeps the session **server-side**, but the browser↔UI hop is plain HTTP
  unless you put it behind TLS. Never publish port 8501 directly: bind it to
  loopback (as `docker-compose.yml` does) or terminate TLS in front of it.
- With no admin credential configured the server is **read-only**: reads work,
  every privileged action is refused.
- No secret is ever placed in a URL. Only non-secret identifiers (agent ids,
  grouping) appear in query parameters, because they land in proxy logs.
""")

st.divider()

st.markdown("### Agent Description")
st.markdown("""
The OpAMP protocol supports both **identifying** and **non-identifying** attributes:

- **Identifying attributes** uniquely identify the agent (e.g., `agent.id`, `host.name`)
- **Non-identifying attributes** provide additional context (e.g., `environment`, `service.name`)

They are used for filtering and grouping in the Agents view, and — because the spec
says `service.name` SHOULD be a reverse FQDN naming the agent type — they are also
what the server matches profiles on when an agent does not say what it is.
""")

st.divider()

st.markdown("### Capabilities")
st.markdown("""
An agent can report:

| Capability | Description |
|------------|-------------|
| `reports_health` | Send periodic health status |
| `reports_effective_config` | Report its current configuration |
| `reports_available_components` | Report which components it has in use (the vocabulary is the agent's own) |
""")

st.divider()

st.markdown("### Advanced: Custom Capabilities")
st.markdown("""
You can enable additional capabilities:

```yaml
capabilities:
  reports_health: true
  reports_effective_config: true
  reports_available_components: true
  accepts_remote_config: true
  accepts_packages: true
```
""")

st.divider()

st.markdown("### Remote config: propose → approve → canary → promote")
st.markdown("""
The **Remote Config** page can change what a whole fleet runs, so it is built around
review rather than a single "push" button. The flow:

1. **Target** — pick agents with the same filters as the Agents page. The page shows
   the count and the blast-radius breakdown before anything else.
2. **Edit** — paste the config (or prefill from an agent's current effective config).
   A **redacted** document is refused: pushing one back would wipe the fleet's
   credentials.
3. **Validate** — structural checks, protected invariants and the policy gate. Errors
   block the next step.
4. **Propose** — stores one immutable revision per target and delivers *nothing*.
5. **Approve & roll out** (approver) — the canary batch is released first; the rest of
   the fleet is **held** until every agent in the canary reports `APPLIED` with our
   hash, stays healthy, and the health-gate window has elapsed.

   The **Health gate** field on the plan is where that window is set. It starts at the
   server's configured default (`CONFIG_PUSH_HEALTH_GATE_SECONDS`) and then it *is* the
   number: the rollout is created with whatever is in the box, so a rollout reviewed at
   30s runs at 30s. `0` means promote as soon as every agent reports `APPLIED` with our
   hash, with no settling time — useful in a lab, not on a fleet.
6. **Stop** — *abort* stops promotion (it cannot recall a config already on an agent);
   *roll back* creates a new revision per applied agent and runs it as its own gated
   rollout.

Need a restart after the config lands? Tick "Restart each agent once its batch passes
  the gate" on the rollout plan, and the agent must advertise `accepts_restart_command`
— otherwise the server refuses the restart with a 409 rather than pretending to have
sent one. What a *pushed config* does to a particular product (who applies it, and
what happens if it is invalid) is described under **Known agent types** above.
""")
