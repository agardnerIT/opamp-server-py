"""Protocol-level validation shared by both OpAMP transports (#73).

A malformed body is a *protocol* error, not an authentication one: the HTTP
endpoint answers 400 and the socket closes 1002, after telling the agent what was
wrong in a `ServerErrorResponse{BAD_REQUEST}` (spec §Bad Request).

Keeping this in its own module is deliberate. `server.main` owns state and
`server.ws` owns connections; both need the same answer to "is this a message at
all, and does it have an identity?", and neither should import the other.
"""

from __future__ import annotations

import secrets
import time

from proto.opamp_pb2 import ServerErrorResponseType_BadRequest, ServerToAgent

BAD_REQUEST = ServerErrorResponseType_BadRequest

# Spec §AgentToServer.instance_uid says "MUST be 16 bytes long". We enforce only
# "non-empty" on purpose: every harness, benchmark and test client in this repo
# uses readable ASCII ids, and existing fleets are already registered under them.
# Rejecting those would be a breaking change with no safety benefit — the uid is
# not a security boundary (the credential is, #94). An *empty* uid is different:
# it is not an id at all, and it makes every such agent collide on one registry
# row. Recorded in memory.md so nobody "fixes" this back to 16-byte-only by
# accident; if that changes, every fixture in tests/ needs a UUIDv7 first.
EXPECTED_INSTANCE_UID_BYTES = 16
MIN_ACCEPTED_UID_BYTES = 1


class BadRequest(ValueError):
    """An AgentToServer we refuse to apply. The transport picks 400 vs close 1002."""

    def __init__(self, detail: str, instance_uid: bytes = b""):
        self.detail = detail
        # Echoed in the error_response when the body had an identity, so a
        # multiplexed stream can tell which agent was refused.
        self.instance_uid = instance_uid
        super().__init__(detail)


def error_response(detail: str, instance_uid: bytes = b"") -> ServerToAgent:
    """A spec-shaped BAD_REQUEST reply: `error_response` set and nothing else.

    The proto says "if error_response is set then all other fields below must be
    unset"; `instance_uid` is field 1, above it, and is what multiplexing needs.
    """
    response = ServerToAgent()
    if instance_uid:
        response.instance_uid = instance_uid
    response.error_response.type = BAD_REQUEST
    response.error_response.error_message = detail
    return response


def uuid7() -> bytes:
    """A 16-byte UUIDv7: 48-bit unix-ms timestamp, version/variant bits, random tail.

    `uuid.uuid7()` only exists in Python 3.14 and this project supports 3.10, so
    fifteen lines are cheaper than a dependency. The point of v7 over v4 is that
    ids sort by creation time, which the DB and the UI get for free.
    """
    millis = (time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    value = bytearray(millis.to_bytes(6, "big") + secrets.token_bytes(10))
    value[6] = (value[6] & 0x0F) | 0x70  # version 7
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant
    return bytes(value)
