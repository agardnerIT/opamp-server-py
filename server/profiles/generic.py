"""Any OpAMP agent: opaque, and honest about it (#118).

The spec's baseline. An agent that reports this profile is stored, displayed and
relayed exactly as the protocol describes — no structural judgement, no component
inventory, no heavy/outdated report, no telemetry view. There is nothing to
suppress here because nothing is assumed; `config_validation` still enforces
everything that is format-agnostic and dangerous (size, UTF-8, duplicate keys, the
`<redacted>` refusal, approval-required), because those are not profile hooks.

This profile has no `description_matchers` and no `sniff`, so it can never be
*inferred* — it is reached only as the fallback (nothing matched) or by an operator
pinning it deliberately ("stop interpreting this agent").
"""

from __future__ import annotations

from server.profiles.base import AgentProfile

#: No content types are claimed: an unrecognised agent's body is opaque bytes, and
#: claiming a format we cannot judge would be the exact overreach this profile exists
#: to avoid.
NO_FORMATS = ()


def bootstrap_snippet(endpoint: str, token: str) -> str:
    """Plain text, because there is no such thing as a generic agent config file.

    Inventing a YAML shape here would be worse than saying nothing: an operator would
    paste it into a product that does not read it and conclude the server is broken.
    """
    return (
        f"OpAMP endpoint: {endpoint}\n"
        f'Authorization: Bearer {token}\n'
        "\n"
        "The server does not know this agent's configuration format, so the exact\n"
        "way to set those two values is defined by the agent. Nothing else about\n"
        "its configuration is interpreted by this server.\n"
    )


PROFILE = AgentProfile(
    name="generic",
    display_name="Generic agent",
    summary=(
        "An agent whose configuration format this server does not interpret. Its "
        "config is stored, relayed and shown as opaque content, and the collector-"
        "specific reports and validation do not describe it."
    ),
    config_formats=NO_FORMATS,
    validates_structure=False,
    snippet=bootstrap_snippet,
)
