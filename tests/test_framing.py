"""Framing tests for the OpAMP WebSocket transport (#89)."""

import pytest

from server.framing import (
    DEFAULT_MAX_MESSAGE_BYTES,
    MalformedFrame,
    MessageTooBig,
    decode_message,
    decode_varint,
    encode_message,
    encode_varint,
    message_limit,
)


class TestVarint:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80\x01"),
            (300, b"\xac\x02"),
            (2**32, b"\x80\x80\x80\x80\x10"),
            (2**64 - 1, b"\xff" * 9 + b"\x01"),
        ],
    )
    def test_roundtrip(self, value, expected):
        assert encode_varint(value) == expected
        assert decode_varint(expected) == (value, len(expected))

    def test_out_of_range(self):
        with pytest.raises(ValueError):
            encode_varint(-1)
        with pytest.raises(ValueError):
            encode_varint(2**64)

    def test_truncated(self):
        with pytest.raises(MalformedFrame):
            decode_varint(b"\x80")

    def test_respects_offset(self):
        assert decode_varint(b"\xff\x7f", offset=1) == (127, 1)

    def test_eleven_bytes(self):
        with pytest.raises(MalformedFrame):
            decode_varint(b"\x80" * 10 + b"\x00")

    def test_overflow_on_tenth_byte(self):
        with pytest.raises(MalformedFrame):
            decode_varint(b"\xff" * 9 + b"\x02")


class TestMessage:
    PAYLOAD = b"\x0a\x04test"

    def test_roundtrip(self):
        frame = encode_message(self.PAYLOAD)
        assert frame == b"\x00" + self.PAYLOAD
        assert decode_message(frame) == self.PAYLOAD

    def test_header_only_is_empty_payload(self):
        assert decode_message(b"\x00") == b""

    def test_empty_frame_is_malformed(self):
        with pytest.raises(MalformedFrame):
            decode_message(b"")

    def test_non_zero_header_is_malformed(self):
        with pytest.raises(MalformedFrame):
            decode_message(b"\x01" + self.PAYLOAD)

    def test_non_canonical_header_is_accepted(self):
        # Wire-compatible but not minimal; the spec only requires header == 0.
        assert decode_message(b"\x80\x00" + self.PAYLOAD) == self.PAYLOAD

    def test_oversize_receive(self):
        with pytest.raises(MessageTooBig) as exc:
            decode_message(b"\x00" + b"x" * 10, max_bytes=10)
        assert exc.value.limit == 10

    def test_limit_includes_header(self):
        # Exactly at the limit, counting the header byte.
        assert decode_message(b"\x00" + b"x" * 9, max_bytes=10) == b"x" * 9
        with pytest.raises(MessageTooBig):
            decode_message(b"\x00" + b"x" * 10, max_bytes=10)

    def test_oversize_send_is_refused(self):
        with pytest.raises(MessageTooBig):
            encode_message(b"x" * 10, max_bytes=10)


class TestLimitConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("OPAMP_MAX_MESSAGE_BYTES", raising=False)
        assert message_limit() == DEFAULT_MAX_MESSAGE_BYTES == 64 * 1024 * 1024

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", "1024")
        assert message_limit() == 1024

    @pytest.mark.parametrize("raw", ["", "nope", "0", "-5"])
    def test_invalid_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv("OPAMP_MAX_MESSAGE_BYTES", raw)
        assert message_limit() == DEFAULT_MAX_MESSAGE_BYTES
