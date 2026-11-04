"""The shipped `docker-compose.yml` may not put the database on a bind mount (#132).

SQLite needs POSIX locking and `mmap`. Docker Desktop for macOS shares host paths over
virtiofs, which provides neither reliably: the server then reports `disk I/O error` /
`database disk image is malformed` for a file a host-side `integrity_check` calls `ok`,
every write answers 503, and the only visible symptom is "a restart fixes it for a
while".

`./data:/app/data` is the natural thing to write in a compose file, and it is exactly
what this template shipped, so every macOS user met the corruption. These assertions
are cheap and they encode the decision rather than the mechanism: the volume may be
renamed, it may not become a host path again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ships with the server dependencies")

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
DATA_TARGET = "/app/data"
VOLUME = "opamp-data"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def mount_sources(service: dict) -> dict:
    """`{target: source}` for one service, accepting both mount spellings."""
    found = {}
    for entry in service.get("volumes") or []:
        if isinstance(entry, str):
            source, _, rest = entry.partition(":")
            found[rest.split(":")[0]] = source
        elif isinstance(entry, dict):
            found[entry.get("target")] = entry.get("source")
    return found


def services_mounting_the_data_directory() -> dict:
    """`{service: source}` — every service that touches /app/data, writer or reader."""
    found = {}
    for name, service in compose()["services"].items():
        source = mount_sources(service).get(DATA_TARGET)
        if source is not None:
            found[name] = source
    return found


def test_the_database_is_a_named_volume_and_not_a_host_path():
    mounting = services_mounting_the_data_directory()
    assert mounting, "no service mounts /app/data — the template is not self-explanatory"
    assert set(mounting.values()) == {VOLUME}, (
        f"/app/data is mounted from {mounting}; every one must be the named volume "
        f"`{VOLUME}`. A host path there corrupts the database on macOS (#132), and a "
        "read-only host path on the UI is the same filesystem by another name"
    )
    # `opamp-data:/app/data` is only a volume if it is declared; otherwise compose reads
    # it as a relative host directory and the bind mount is back, spelled differently.
    assert VOLUME in (compose().get("volumes") or {})


def test_the_volume_is_not_quietly_a_bind_mount_underneath():
    """`driver_opts: {type: none, o: bind, device: /Users/...}` is the same trap.

    It is how a host directory gets mounted while the compose file still *looks* like
    it uses a named volume, which is worse than the honest `./data:/app/data` because
    the next reader has no reason to look.
    """
    options = ((compose().get("volumes") or {}).get(VOLUME) or {}).get("driver_opts") or {}
    assert options.get("type") != "none" and "bind" not in str(options.get("o") or "")
    assert "device" not in options
