"""`docker-compose.yml` must actually read `.env`, not just interpolate a subset of it.

Compose loads `.env` automatically, but only for the `${VAR:-default}` references
written out in `environment:`. A variable that is documented in `.env.sample` and
set in `.env` but never spelled out in the compose file is *silently* dropped: no
warning, no error, the container simply runs with the built-in default. The
template shipped that way, and 45 of the 79 documented settings were affected —
including `CONFIG_AT_REST_KEY`, which the README tells you to put in `.env` and
which is a prerequisite for config push.

`env_file:` closes that hole: it passes the whole file to the container, so
`.env` stays the single place an operator edits. `environment:` is kept because
it is where the documented, secure defaults live and it wins over `env_file:`
(they read the same file, so they cannot disagree).

These assertions encode the decision, not the mechanism: the file may be
reorganised, but a documented variable may not become unreachable again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ships with the server dependencies")

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_SAMPLE = REPO_ROOT / ".env.sample"
ENV_FILE = ".env"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def env_sample_keys() -> list[str]:
    """Every `KEY=` documented in `.env.sample`, in file order."""
    keys = []
    for line in ENV_SAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0])
    return keys


def declared_env_files(service: dict) -> list[dict]:
    """`env_file:` entries normalised to `{path, required}` (both YAML spellings)."""
    found = []
    for entry in service.get("env_file") or []:
        if isinstance(entry, str):
            found.append({"path": entry, "required": True})
        else:
            found.append(
                {"path": entry.get("path"), "required": entry.get("required", True)}
            )
    return found


def environment_keys(service: dict) -> set[str]:
    """Keys named in `environment:`, accepting list and mapping spellings."""
    keys = set()
    for entry in service.get("environment") or []:
        if isinstance(entry, str):
            keys.add(entry.split("=", 1)[0])
        else:
            keys.add(str(entry))
    return keys


def env_file_services() -> list[str]:
    """Services that load the whole `.env`."""
    return [
        name
        for name, service in compose()["services"].items()
        if any(e["path"] == ENV_FILE for e in declared_env_files(service))
    ]


def test_both_services_read_the_whole_dot_env():
    reading = env_file_services()
    assert {"server", "ui"} <= set(reading), (
        f"only {reading} load `{ENV_FILE}`. Compose interpolation in `environment:` "
        "covers a subset of the documented settings; the rest reach a container only "
        "through `env_file:`"
    )


def test_a_missing_dot_env_stays_a_supported_posture():
    """No `.env` must keep working: it is the read-only, random-password default.

    `required: true` would turn "I did not configure anything yet" into a startup
    failure, which is exactly the wrong first experience for a secure-by-default
    template.
    """
    for name in env_file_services():
        for entry in declared_env_files(compose()["services"][name]):
            if entry["path"] == ENV_FILE:
                assert entry["required"] is False, (
                    f"service `{name}` makes `{ENV_FILE}` mandatory; a missing .env is "
                    "the secure default posture, not an error"
                )


def test_every_documented_variable_can_reach_a_container():
    """The regression: documented in `.env.sample`, unreachable by the container.

    A key is reachable if it is named in some service's `environment:` (interpolated
    from `.env`) or if that service loads the file wholesale via `env_file:`.
    """
    loading = set(env_file_services())
    listed: set[str] = set()
    for service in compose()["services"].values():
        listed |= environment_keys(service)

    unreachable = [k for k in env_sample_keys() if k not in listed and not loading]
    assert not unreachable, (
        f"{len(unreachable)} variables are documented in .env.sample but no service "
        f"can receive them: {', '.join(unreachable)}. Setting one in `{ENV_FILE}` "
        "would be ignored without a warning"
    )


def test_dot_env_is_a_local_secret_and_is_never_baked_into_an_image():
    """It holds the admin password and the config encryption key."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ENV_FILE in [line.strip() for line in ignored], (
        f"`{ENV_FILE}` is not gitignored; it holds credentials"
    )

    dockerignore = (
        (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    assert ENV_FILE in [line.strip() for line in dockerignore], (
        f"`{ENV_FILE}` is not in .dockerignore, so a local build could copy it into "
        "the image"
    )


def test_the_sample_file_is_the_committed_template():
    """`.env.sample` is what makes the pass-through above meaningful."""
    tracked = (REPO_ROOT / ".env.sample").is_file()
    assert tracked, ".env.sample is the committed template for .env"
