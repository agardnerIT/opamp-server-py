"""Deterministic, correctly-sized `instance_uid` values for tests (#80, #112).

The OpAMP spec treats `instance_uid` as an opaque 16-byte value. Two things follow:

* a fixture that makes one up as ASCII (`b"test_opamp_00123456"`, 19 bytes) is not
  what any real agent sends, so a suite full of them quietly stops representing the
  wire — the defect #80 exists to fix;
* what a conformance case needs is the *width*, not a literal UUIDv7: the server
  never parses the bytes, so a derived value is indistinguishable from a real one.

So: derive 16 bytes from the readable label. Failure output still names the agent,
`GET /agent/{id}` still has a stable id inside one test, and an accidentally short
uid is now obviously a mistake rather than a convention.
"""

from __future__ import annotations

import hashlib

__all__ = ["uid", "agent_id"]


def uid(label: str) -> bytes:
    """A stable 16-byte uid for `label` (the same label always gives the same uid)."""
    return hashlib.sha256(f"opamp-uid:{label}".encode()).digest()[:16]


def agent_id(label: str) -> str:
    """The id the server files that agent under: the hex of its uid."""
    return uid(label).hex()
