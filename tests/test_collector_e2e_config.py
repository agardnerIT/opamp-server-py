"""The e2e collector configs must be *authenticatable*, not merely valid YAML (#113).

`collector/e2e.yaml` is what the README calls the known-good config, and CI mounts it
into a real `otelcol-contrib`. Agent auth became mandatory (#125) after these files
were written, so for a while the "known good" config was a guaranteed 401: the
workflow went red and `continue-on-error: true` hid it. Nothing in the suite noticed,
because every other test starts from `tests/fake_agent.py`, which sends whatever the
test tells it to.

These tests pin the seam that was missing — a bearer header in each config, read from
the environment rather than committed, the workflow that supplies that variable, and
the README blocks an operator copies. They are deliberately about *files*, not the
server, so a config that no Python touches can still fail the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ships with the server dependencies")

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_HTTP = REPO_ROOT / "collector" / "e2e.yaml"
E2E_WS = REPO_ROOT / "collector" / "e2e-ws.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "collector-e2e.yml"
README = REPO_ROOT / "README.md"

#: `${env:NAME}` (collector env expansion). A default value would defeat the point:
#: an unset token should be a loud failure, not a silent empty credential.
ENV_VAR = re.compile(r"^\$\{env:([A-Z0-9_]+)\}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def server_block(path: Path, transport: str) -> dict:
    return load(path)["extensions"]["opamp"]["server"][transport]


def token_env_var(path: Path, transport: str) -> str:
    """The env var a config's `Authorization` header reads, asserting the shape.

    `Bearer <token>` is what `server/agent_auth.py` parses, and `${env:…}` is how the
    collector reads a secret without it living in the YAML.
    """
    headers = server_block(path, transport).get("headers") or {}
    authorization = headers.get("Authorization")
    assert authorization, f"{path.name}: no Authorization header — a tokenless agent gets 401"
    scheme, _, value = authorization.partition(" ")
    assert scheme.lower() == "bearer", f"{path.name}: scheme must be Bearer, got {scheme!r}"
    match = ENV_VAR.match(value)
    assert match, (
        f"{path.name}: Authorization must be `Bearer ${{env:NAME}}`, got {value!r}. "
        "A literal token would be a committed secret; a bare ${NAME} is not expanded."
    )
    return match.group(1)


def docker_runs(workflow: str) -> list[str]:
    """Each `docker run …` invocation in a workflow, comment lines removed."""
    lines = workflow.splitlines()
    runs = []
    index = 0
    while index < len(lines):
        if "docker run" not in lines[index]:
            index += 1
            continue
        block = []
        while index < len(lines):
            line = lines[index]
            if not line.strip().startswith("#"):
                block.append(line)
            index += 1
            if not line.rstrip().endswith("\\"):
                break
        runs.append("\n".join(block))
    return runs


# --- the configs -------------------------------------------------------------


def test_http_e2e_config_carries_a_bearer_token():
    assert token_env_var(E2E_HTTP, "http")


def test_ws_e2e_config_carries_a_bearer_token():
    assert token_env_var(E2E_WS, "ws")


def test_both_transports_use_the_same_env_var():
    """One token, both collectors: CI mints a fleet token once and injects one name."""
    assert token_env_var(E2E_HTTP, "http") == token_env_var(E2E_WS, "ws")


def test_no_token_is_committed_in_the_configs():
    for path in (E2E_HTTP, E2E_WS):
        text = path.read_text(encoding="utf-8")
        assert "opamp_agent_" not in text, f"{path.name} contains what looks like a real token"


def test_ws_config_omits_polling_interval():
    """`server.ws` rejects the key (`0.140.0`), so a doc-rot edit must fail here too."""
    assert "polling_interval" not in server_block(E2E_WS, "ws")


# --- the workflow that makes them runnable -----------------------------------


def test_workflow_injects_the_token_into_every_collector():
    """Each collector mounted from `collector/` must get the variable the config names."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runs = [run for run in docker_runs(workflow) if "collector/e2e" in run]
    assert runs, "workflow no longer runs a collector from collector/"
    for name in (token_env_var(E2E_HTTP, "http"), token_env_var(E2E_WS, "ws")):
        for run in runs:
            assert f"-e {name}=" in run, (
                f"docker run starts a collector without {name}; its header would expand "
                f"to nothing and the server would answer 401:\n{run}"
            )


def test_workflow_mints_the_token_as_an_admin():
    """Minting is an admin write (#126): needs `/agent-tokens` and a credential.

    Without `ADMIN_PASSWORD` the server runs read-only and that POST answers 403, so
    the failure mode is "no token at all" rather than "weak token".
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "/agent-tokens" in workflow, "workflow never mints a fleet token"
    assert re.search(r"^\s+ADMIN_PASSWORD:", workflow, re.MULTILINE), (
        "workflow mints a token but starts no server with an admin credential"
    )
    assert "::add-mask::" in workflow, "a minted token must not be echoed into the log"


def test_workflow_still_asserts_the_named_agents():
    """The config's `agent.name` and the assert script's name must not drift apart."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for path, transport in ((E2E_HTTP, "http"), (E2E_WS, "ws")):
        config = load(path)
        name = config["extensions"]["opamp"]["agent_description"][
            "non_identifying_attributes"
        ]["agent.name"]
        assert f"--agent-name {name}" in workflow, f"{path.name} declares {name!r}"


# --- the README blocks an operator copies ------------------------------------


def connect_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("### Connect a collector")
    end = text.index("### Transports")
    return text[start:end]


def quickstart_section() -> str:
    """The `docker compose up` + one collector command a new reader runs first."""
    text = README.read_text(encoding="utf-8")
    start = text.index("#### See your first agent")
    end = text.index("\n### ", start)  # next top section; "####x" also contains "### "
    return text[start:end]


def bash_blocks(section: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", section, re.DOTALL)


def test_readme_connect_blocks_show_the_token_header():
    """Every opamp config in the README's connect section must be paste-and-connect.

    The section used to show an HTTP block without `headers:` and explain the token
    three paragraphs later, which is how you get a 401 from the documented config.
    """
    section = connect_section()
    blocks = re.findall(r"```yaml\n(.*?)```", section, re.DOTALL)
    opamp_blocks = [block for block in blocks if "opamp:" in block and "server:" in block]
    assert opamp_blocks, "the connect section no longer shows a collector config"
    for block in opamp_blocks:
        assert "Authorization:" in block, (
            "an opamp config in the README has no Authorization header; "
            f"agent auth is mandatory:\n{block}"
        )


def test_readme_says_the_e2e_config_includes_the_token():
    section = connect_section()
    assert "collector/e2e.yaml" in section
    assert "headers:" in section.split("collector/e2e.yaml", 1)[1].split("\n\n", 1)[0]


def test_quickstart_snippets_can_actually_connect():
    """The get-an-agent-fastest path must be complete, not illustrative.

    A `docker run` for `collector/e2e.yaml` without `-e <token>` is a 401, and the
    README is the only place a reader sees that pair together before they have a
    token of their own. So: every collector command in the quickstart mounts the e2e
    config and injects the variable that config reads, and the section mints one.
    """
    section = quickstart_section()
    token_vars = {token_env_var(E2E_HTTP, "http"), token_env_var(E2E_WS, "ws")}

    runs = [run for block in bash_blocks(section) for run in docker_runs(block)]
    assert runs, "the quickstart no longer shows a docker run command"
    assert any("collector/e2e.yaml" in run for run in runs), (
        "the quickstart must mount collector/e2e.yaml, the config verified against a "
        "real collector"
    )
    for run in runs:
        assert "collector/e2e.yaml" in run, f"stray collector command in the quickstart:\n{run}"
        assert any(f"-e {name}=" in run for name in token_vars), (
            f"quickstart collector command has no token and would 401:\n{run}"
        )
        assert "--rm" in run, "a copy-paste quickstart should not leave containers behind"

    assert "/agent-tokens" in section, "quickstart shows a collector but never mints a token"


def test_quickstart_linux_variant_covers_the_loopback_port():
    """Linux needs both halves: host networking *and* the alias, since compose binds
    127.0.0.1 (`host-gateway` is the bridge IP there and would refuse)."""
    section = quickstart_section()
    linux = [run for block in bash_blocks(section) for run in docker_runs(block) if "--network host" in run]
    assert linux, "the quickstart no longer documents the Linux variant"
    for run in linux:
        assert "--add-host=host.docker.internal:127.0.0.1" in run, (
            "`--network host` alone leaves host.docker.internal unresolved on Linux"
        )
