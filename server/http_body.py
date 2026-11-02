"""Bounded, decompressing reads of the OpAMP HTTP request body (#72, shared with #71).

Spec §Plain HTTP Message Size Limits: a server MUST enforce a limit on the request
body **including after decompression**, and MUST answer `413 Content Too Large`
rather than buffer whatever arrived. Uvicorn has no body limit of its own, so
`await request.body()` was one POST away from OOMing the process.

Two caps, because either one alone is bypassable:

* `Content-Length` — rejected before reading a byte when it already exceeds the
  limit. Cheap, but a lying (or absent) header must not help.
* the bytes actually read — a chunked body has no `Content-Length` at all, and a
  client may simply keep writing past the one it declared.
* the decompressed bytes — a 1 MiB gzip body can expand past any limit, so gzip is
  unrolled *inside* the bounded read instead of before it. That ordering is the
  whole point: decompress-then-cap means the bomb has already allocated.
"""

from __future__ import annotations

import zlib
from typing import Optional

from fastapi import Request

from server.framing import message_limit
from server.protocol import BadRequest

# `identity` is the no-op spelling; the empty header is the common case.
IDENTITY_ENCODINGS = ("", "identity")
GZIP_ENCODINGS = ("gzip", "x-gzip")


class BodyTooLarge(Exception):
    """The request body exceeds `OPAMP_MAX_MESSAGE_BYTES`; the transport answers 413."""

    def __init__(self, size: int, limit: int):
        super().__init__(f"request body is {size} bytes; limit is {limit} bytes")
        self.size = size
        self.limit = limit


class UnsupportedEncoding(BadRequest):
    """A `Content-Encoding` we cannot decode.

    Still a `BadRequest` (the agent gets a spec-shaped `BAD_REQUEST` error response
    either way), but the HTTP transport answers **415 Unsupported Media Type**: the
    message is not malformed, the encoding is simply not one we implement, and
    "bad request" would send an operator looking for a bug in the agent's protobuf.
    """


def _declared_length(request: Request) -> Optional[int]:
    """`Content-Length` as an int, or None when absent/unparseable."""
    raw = request.headers.get("content-length")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _decompressor(encoding: str):
    """A zlib decompressor for the declared encoding, or None for identity.

    An unknown encoding is a BAD_REQUEST, not a silent pass-through (#71): parsing
    `br` bytes as protobuf would produce a half-empty message and lose the report.
    """
    value = (encoding or "").strip().lower()
    if value in IDENTITY_ENCODINGS:
        return None
    if value in GZIP_ENCODINGS:
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    raise UnsupportedEncoding(f"unsupported Content-Encoding: {encoding!r}")


async def read_body(request: Request, *, limit: Optional[int] = None) -> bytes:
    """Read the request body, decompressing gzip, never exceeding `limit`.

    Returns the decoded bytes. Raises `BodyTooLarge` (→ 413) or `BadRequest`
    (→ BAD_REQUEST) before the caller can touch agent state, so a refused body
    cannot register an agent, bump a heartbeat or record metrics.
    """
    maximum = message_limit() if limit is None else limit

    declared = _declared_length(request)
    if declared is not None and declared > maximum:
        raise BodyTooLarge(declared, maximum)

    decompressor = _decompressor(request.headers.get("content-encoding", ""))
    body = bytearray()
    compressed = 0

    async for chunk in request.stream():
        compressed += len(chunk)
        # A gzip bomb is small on the wire; this catches a plain oversized body
        # whose Content-Length was absent or lied about.
        if compressed > maximum:
            raise BodyTooLarge(compressed, maximum)

        if decompressor is None:
            body.extend(chunk)
            if len(body) > maximum:
                raise BodyTooLarge(len(body), maximum)
            continue

        # `max_length` is the remaining budget + 1, so overshoot is detectable
        # without letting zlib allocate the bomb's full output first.
        budget = maximum - len(body) + 1
        try:
            produced = decompressor.decompress(chunk, budget)
        except zlib.error as exc:
            raise BadRequest(f"invalid gzip request body: {exc}") from exc
        body.extend(produced)
        if len(body) > maximum:
            raise BodyTooLarge(len(body), maximum)
        if decompressor.unconsumed_tail:
            # zlib stopped at our ceiling: there is more decompressed data waiting.
            raise BodyTooLarge(maximum + 1, maximum)
        if decompressor.unused_data:
            raise BadRequest("gzip request body has trailing data after one stream")

    if decompressor is not None:
        try:
            body.extend(decompressor.flush())
        except zlib.error as exc:  # pragma: no cover - decompress() normally raises first
            raise BadRequest(f"invalid gzip request body: {exc}") from exc
        if len(body) > maximum:
            raise BodyTooLarge(len(body), maximum)
        if not decompressor.eof:
            raise BadRequest("truncated gzip request body")

    return bytes(body)
