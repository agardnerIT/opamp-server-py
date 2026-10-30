"""OpAMP WebSocket message framing.

Spec §WebSocket Message Format (v0.20.0, `specification.md:403`): each WebSocket
message is a base-128 varint `header` followed by an encoded `AgentToServer` or
`ServerToAgent` protobuf. The header is 0 in this version of the spec; any other
value is reserved and decoders SHOULD treat it as malformed.

The size limit covers the complete message — header and data — per
§WebSocket Message Size Limits (`specification.md:443`). The helper here is
transport-neutral so the plain-HTTP limit (#72) can share it.
"""

from __future__ import annotations

import os

DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_VARINT_BYTES = 10
MAX_UINT64 = (1 << 64) - 1


class MalformedFrame(ValueError):
    """The frame cannot be decoded: empty, truncated varint, or non-zero header."""


class MessageTooBig(ValueError):
    """The frame exceeds the configured message size limit."""

    def __init__(self, size: int, limit: int):
        super().__init__(f"OpAMP message is {size} bytes; limit is {limit} bytes")
        self.size = size
        self.limit = limit


def message_limit() -> int:
    """Configured size limit in bytes, header included.

    `OPAMP_MAX_MESSAGE_BYTES` overrides the 64 MiB default recommended by the
    spec. Invalid or non-positive values fall back to the default rather than
    disabling the limit.
    """
    raw = os.environ.get("OPAMP_MAX_MESSAGE_BYTES")
    if not raw:
        return DEFAULT_MAX_MESSAGE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_MESSAGE_BYTES
    return value if value > 0 else DEFAULT_MAX_MESSAGE_BYTES


def encode_varint(value: int) -> bytes:
    """Encode an unsigned 64-bit integer as a base-128 varint."""
    if not 0 <= value <= MAX_UINT64:
        raise ValueError(f"varint value out of range: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a varint at `offset`.

    Returns `(value, bytes_consumed)`. Rejects truncated headers and values that
    overflow 64 bits (a 10th byte greater than 1).
    """
    value = 0
    for index in range(MAX_VARINT_BYTES):
        position = offset + index
        if position >= len(buf):
            raise MalformedFrame("truncated varint header")
        byte = buf[position]
        if index == MAX_VARINT_BYTES - 1 and byte > 1:
            raise MalformedFrame("varint header overflows 64 bits")
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value, index + 1
    raise MalformedFrame("varint header longer than 10 bytes")


def encode_message(payload: bytes, *, max_bytes: int | None = None) -> bytes:
    """Frame a protobuf payload for the wire.

    Raises `MessageTooBig` instead of returning an oversized frame — the spec
    says a server MUST NOT send one.
    """
    limit = message_limit() if max_bytes is None else max_bytes
    frame = encode_varint(0) + payload
    if len(frame) > limit:
        raise MessageTooBig(len(frame), limit)
    return frame


def decode_message(frame: bytes, *, max_bytes: int | None = None) -> bytes:
    """Strip the header and return the protobuf payload.

    Raises `MessageTooBig` for oversized frames and `MalformedFrame` for an
    empty frame, a truncated varint, or a non-zero header. An empty payload is
    valid: it represents a message with every field unset.
    """
    limit = message_limit() if max_bytes is None else max_bytes
    if len(frame) > limit:
        raise MessageTooBig(len(frame), limit)
    if not frame:
        raise MalformedFrame("empty frame")

    header, consumed = decode_varint(frame)
    if header != 0:
        raise MalformedFrame(f"unsupported header value {header}")
    return frame[consumed:]
