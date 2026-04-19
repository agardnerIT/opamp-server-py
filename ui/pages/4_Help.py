import os
import sys
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.shared import (
    SERVER_URL,
    get_server_url,
    render_sidebar,
)

st.set_page_config(
    page_title="Help - OpAMP Server Dashboard",
    page_icon="❓",
    layout="wide"
)

render_sidebar()

server_url = get_server_url()

st.markdown("## Collector Configuration")
st.markdown("Add this minimal config to your OpenTelemetry Collector to connect to this OpAMP server:")

default_yaml = f"""extensions:
  opamp:
    server:
      http:
        endpoint: {server_url}/v1/opamp
    agent_description:
      non_identifying_attributes:
        "deployment.environment": "development"
    capabilities:
      reports_health: true
      reports_effective_config: true
      reports_available_components: true
service:
  extensions: [opamp]
"""

st.code(default_yaml, language="yaml")

st.download_button(
    "Download config.yaml",
    default_yaml,
    file_name="config.yaml",
    mime="text/yaml",
    key="download_config"
)

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
