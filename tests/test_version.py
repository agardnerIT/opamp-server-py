"""The runtime version must match the release metadata (#46)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from server.version import __version__


def test_runtime_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["project"]["version"] == __version__, (
        "bump server/version.py with pyproject.toml; the update check compares "
        "releases against it"
    )
