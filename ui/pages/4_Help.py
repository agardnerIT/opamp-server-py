import os
import sys
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ui.shared import (
    SERVER_URL,
    collector_config_snippet,
    get_server_url,
    render_sidebar,
)

st.set_page_config(
    page_title="Help - OpAMP Server Dashboard",
    page_icon="❓",
    layout="wide"
)

render_sidebar()

st.markdown("## Collector Configuration")
st.markdown("Add this minimal config to your OpenTelemetry Collector to connect to this OpAMP server:")

# #116: one snippet for the Help page and the empty fleet view, so a fix to the config
# can never land in only one of them.
default_yaml = collector_config_snippet(get_server_url())

st.code(default_yaml, language="yaml")

st.download_button(
    "Download config.yaml",
    default_yaml,
    file_name="config.yaml",
    mime="text/yaml",
    key="download_config"
)

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
The OpAMP extension supports both **identifying** and **non-identifying** attributes:

- **Identifying attributes** uniquely identify the agent (e.g., `agent.id`, `host.name`)
- **Non-identifying attributes** provide additional context (e.g., `environment`, `service.name`)

These are used for filtering and grouping in the Agents view.
""")

st.divider()

st.markdown("### Capabilities")
st.markdown("""
The OpAMP extension can report:

| Capability | Description |
|------------|-------------|
| `reports_health` | Send periodic health status |
| `reports_effective_config` | Report current collector configuration |
| `reports_available_components` | Report which components (receivers, processors, exporters) are in use |
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
6. **Stop** — *abort* stops promotion (it cannot recall a config already on an agent);
   *roll back* creates a new revision per applied agent and runs it as its own gated
   rollout.

Two prerequisites, both upstream realities:

- A pushed config is applied by the **OpAMP Supervisor** (`cmd/opampsupervisor`), not by
  `extension/opampextension` — a bare collector never advertises `AcceptsRemoteConfig`.
- An invalid config leaves the collector **down until it is manually restarted** (the
  extension is no longer running), which is why rollout staging and the health gate are
  not optional.

Need a restart after the config lands? Tick "Restart each agent once its batch passes
the gate" on the rollout plan — and enable `accepts_restart_command: true` in the
collector's `opamp` extension, or the server will refuse the restart with a 409 rather
than pretending to have sent one.
""")
