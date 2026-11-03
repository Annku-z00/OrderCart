"""
ocr/proto.py
============

A tiny, dependency-free **proto3 wire-format codec** plus the field numbers of
the Google Lens overlay messages that FrameFlow talks to.

Why this module exists
----------------------
FrameFlow used to talk to Google Lens through ``chrome-lens-py``, which ships
~40 generated ``*_pb2.py`` modules (plus a betterproto layer, an async HTTP
stack and a CLI).  All FrameFlow needs is:

* build **one** request message (an image upload),
* read **one** response message (the recognised text).

So the protocol is implemented here from scratch instead: a couple of hundred
lines of pure Python, no protobuf runtime, no generated code, no async event
loop.  Only :mod:`requests` (already a FrameFlow dependency) is used for the
HTTP call itself.

Wire format refresher
---------------------
Every field is ``tag = (field_number << 3) | wire_type`` followed by a value:

===== ========== ====================================================
type  name       payload
===== ========== ====================================================
0     varint    the value itself, base-128, 7 bits per byte
1     64-bit    8 raw bytes, little endian
2     length    a varint byte count, then that many raw bytes
5     32-bit    4 raw bytes, little endian
===== ========== ====================================================

``string`` / ``bytes`` / embedded messages all use the length-delimited form;
``int32`` / ``int64`` / ``uint32`` / ``uint64`` / ``bool`` / ``enum`` use the
varint form.  In proto3 a field whose value equals its default is simply left
off the wire, so the encoder never writes zeros.
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Tuple

__all__ = [
    "WireError",
    "encode_bool",
    "encode_bytes",
    "encode_int",
    "encode_message",
    "encode_string",
    "parse_message",
    "varint_bytes",
]


class WireError(ValueError):
    """Raised when a buffer is not valid protobuf wire format."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def varint_bytes(value: int) -> bytes:
    """Encode ``value`` as a base-128 varint (7 payload bits per byte)."""
    if value < 0:
        # Negative int32/int64 values are sign-extended to 64 bits, which is
        # exactly ten bytes of 0xFF-ish payload.  Lens never sends one, but
        # the codec stays correct for completeness.
        value += 1 << 64
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            break
    return bytes(out)


def _tag(field_number: int, wire_type: int) -> bytes:
    if field_number <= 0:
        raise WireError(f"field number must be positive, got {field_number}")
    return varint_bytes((field_number << 3) | wire_type)


def encode_int(field_number: int, value: int) -> bytes:
    """Encode an ``int32`` / ``int64`` / ``uint32`` / ``uint64`` / ``enum``."""
    if value == 0:
        return b""  # proto3 default -> omitted
    return _tag(field_number, 0) + varint_bytes(int(value))


def encode_bool(field_number: int, value: bool) -> bytes:
    """Encode a ``bool``."""
    if not value:
        return b""
    return _tag(field_number, 0) + varint_bytes(1)


def encode_bytes(field_number: int, value: bytes) -> bytes:
    """Encode a ``bytes`` field."""
    value = bytes(value or b"")
    if not value:
        return b""
    return _tag(field_number, 2) + varint_bytes(len(value)) + value


def encode_string(field_number: int, value: str) -> bytes:
    """Encode a ``string`` field (UTF-8)."""
    if not value:
        return b""
    return encode_bytes(field_number, str(value).encode("utf-8"))


def encode_message(field_number: int, payload: bytes) -> bytes:
    """Encode an embedded message as a length-delimited field."""
    payload = bytes(payload or b"")
    return _tag(field_number, 2) + varint_bytes(len(payload)) + payload


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read one varint starting at ``pos`` -> ``(value, new_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WireError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise WireError("varint is too long")


def parse_message(buf: bytes) -> Dict[int, List[object]]:
    """Parse a protobuf message into ``{field_number: [values]}``.

    Every value is either an ``int`` (varint fields), ``bytes`` (length
    delimited fields) or a nested :func:`parse_message` result kept as raw
    ``bytes`` so the caller decides how deep to descend.
    """
    out: Dict[int, List[object]] = {}
    pos = 0
    size = len(buf or b"")
    while pos < size:
        key, pos = _read_varint(buf, pos)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
        elif wire_type == 1:
            if pos + 8 > size:
                raise WireError("truncated 64-bit field")
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > size:
                raise WireError("truncated length-delimited field")
            value = buf[pos:pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > size:
                raise WireError("truncated 32-bit field")
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise WireError(f"unsupported wire type {wire_type}")
        out.setdefault(field_number, []).append(value)
    return out


def iter_messages(fields: Dict[int, List[object]], field_number: int) -> Iterator[bytes]:
    """Yield the raw payloads of every occurrence of ``field_number``."""
    for value in fields.get(field_number, []):
        if isinstance(value, (bytes, bytearray)):
            yield bytes(value)


def first_message(
    fields: Dict[int, List[object]], field_number: int
) -> Optional[bytes]:
    """Return the first payload of ``field_number`` (``None`` when absent)."""
    for value in fields.get(field_number, []):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    return None


def first_int(fields: Dict[int, List[object]], field_number: int) -> int:
    """Return the first varint value of ``field_number`` (0 when absent)."""
    for value in fields.get(field_number, []):
        if isinstance(value, int):
            return int(value)
    return 0


def first_string(fields: Dict[int, List[object]], field_number: int) -> str:
    """Return the first string value of ``field_number`` (``""`` when absent)."""
    for value in fields.get(field_number, []):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace")
    return ""
