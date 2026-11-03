"""The application version.

This is the version the update check compares against GitHub releases. It is
deliberately a checked-in constant rather than `importlib.metadata`: the server
image installs dependencies but not the package, so package metadata is not
available at runtime. `tests/test_version.py` keeps it in sync with
`pyproject.toml`, which is what `release.py` reads.
"""

__version__ = "0.1.0"
