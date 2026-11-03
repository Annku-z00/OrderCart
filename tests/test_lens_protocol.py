"""
tests/test_lens_protocol.py
===========================

Tests for the **hand-written** Google Lens protocol: the protobuf codec in
:mod:`ocr.proto` and the client in :mod:`ocr.lens`.

Nothing here touches the network — the HTTP session is faked — so the suite
stays fast and offline.
"""

from __future__ import annotations

import pytest

from ocr import lens as lens_mod
from ocr import proto
from ocr.lens import (
    LensClient,
    LensHTTPError,
    LensServerError,
    build_ocr_request,
    extract_text,
)
from ocr.proto import (
    WireError,
    encode_int,
    encode_message,
    encode_string,
    first_int,
    first_message,
    iter_messages,
    parse_message,
    varint_bytes,
)


# ---------------------------------------------------------------------------
# The codec
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 65535, 2 ** 31, 2 ** 63 - 1])
def test_varint_round_trip(value):
    encoded = varint_bytes(value)
    fields = parse_message(encode_int(1, value))
    assert first_int(fields, 1) == value
    assert len(encoded) >= 1


def test_zero_is_not_written():
    """proto3 omits default values."""
    assert encode_int(5, 0) == b""
    assert encode_string(5, "") == b""


def test_string_and_message_round_trip():
    payload = encode_string(1, "Order ID")
    fields = parse_message(payload)
    assert fields[1][0] == b"Order ID"

    inner = encode_int(2, 7)
    fields = parse_message(encode_message(3, inner))
    assert parse_message(first_message(fields, 3))[2][0] == 7


def test_parse_rejects_garbage():
    with pytest.raises(WireError):
        parse_message(b"\x0a\x05ab")  # length says 5, only 2 bytes follow


def test_nested_messages_are_iterable():
    inner = encode_int(1, 42)
    fields = parse_message(encode_message(9, inner))
    found = [parse_message(payload) for payload in iter_messages(fields, 9)]
    assert found and first_int(found[0], 1) == 42


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
def test_request_carries_the_image_and_the_client_context():
    payload = build_ocr_request(
        b"\xff\xd8\xff\xdbjpegbytes",
        width=720,
        height=1280,
        uuid=1234567890,
        mime_type="image/jpeg",
    )
    top = parse_message(payload)
    objects = first_message(top, lens_mod.F_SERVER_REQUEST_OBJECTS_REQUEST)
    assert objects is not None
    objects_fields = parse_message(objects)

    context = first_message(objects_fields, lens_mod.F_OBJECTS_REQUEST_REQUEST_CONTEXT)
    context_fields = parse_message(context)
    request_id = parse_message(
        first_message(context_fields, lens_mod.F_REQUEST_CONTEXT_REQUEST_ID)
    )
    assert first_int(request_id, lens_mod.F_REQUEST_ID_UUID) == 1234567890
    assert first_int(request_id, lens_mod.F_REQUEST_ID_SEQUENCE_ID) == 1

    client = parse_message(
        first_message(context_fields, lens_mod.F_REQUEST_CONTEXT_CLIENT_CONTEXT)
    )
    assert first_int(client, lens_mod.F_CLIENT_CONTEXT_PLATFORM) == lens_mod.PLATFORM_WEB
    assert first_int(client, lens_mod.F_CLIENT_CONTEXT_SURFACE) == lens_mod.SURFACE_CHROMIUM

    image_data = first_message(objects_fields, lens_mod.F_OBJECTS_REQUEST_IMAGE_DATA)
    image_fields = parse_message(image_data)
    payload_bytes = first_message(image_fields, lens_mod.F_IMAGE_DATA_PAYLOAD)
    assert parse_message(payload_bytes)[lens_mod.F_IMAGE_PAYLOAD_IMAGE_BYTES][0] == (
        b"\xff\xd8\xff\xdbjpegbytes"
    )
    metadata = parse_message(
        first_message(image_fields, lens_mod.F_IMAGE_DATA_IMAGE_METADATA)
    )
    assert first_int(metadata, lens_mod.F_IMAGE_METADATA_WIDTH) == 720
    assert first_int(metadata, lens_mod.F_IMAGE_METADATA_HEIGHT) == 1280


def test_request_refuses_an_empty_image():
    with pytest.raises(lens_mod.LensProtocolError):
        build_ocr_request(b"", width=1, height=1, uuid=1)


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------
def _fake_response(words, error_type=0):
    """Build a LensOverlayServerResponse carrying ``words``.

    ``words`` is a list of ``(text, separator)`` pairs — one word each.
    """
    payload = b""
    for text, separator in words:
        word = encode_string(lens_mod.F_WORD_PLAIN_TEXT, text)
        word += encode_string(lens_mod.F_WORD_TEXT_SEPARATOR, separator)
        payload += encode_message(lens_mod.F_LINE_WORDS, word)
    line = payload
    paragraph = encode_message(lens_mod.F_PARAGRAPH_LINES, line)
    layout = encode_message(lens_mod.F_TEXT_LAYOUT_PARAGRAPHS, paragraph)
    text = encode_message(lens_mod.F_TEXT_TEXT_LAYOUT, layout)
    objects = encode_message(lens_mod.F_OBJECTS_RESPONSE_TEXT, text)
    payload = encode_message(lens_mod.F_SERVER_RESPONSE_OBJECTS_RESPONSE, objects)
    if error_type:
        payload += encode_message(
            lens_mod.F_SERVER_RESPONSE_ERROR, encode_int(1, error_type)
        )
    return payload


def test_response_text_is_extracted():
    body = _fake_response([("Order #342581970718279808", "")])
    assert extract_text(body) == "Order #342581970718279808"


def test_response_keeps_the_word_separators():
    body = _fake_response([("Order", " "), ("#3425", "")])
    assert extract_text(body) == "Order #3425"


def test_response_without_text_is_empty_not_an_error():
    assert extract_text(b"") == ""


def test_server_error_is_raised():
    body = _fake_response([("x", "")], error_type=2)
    with pytest.raises(LensServerError):
        extract_text(body)


def test_unparsable_response_raises():
    with pytest.raises(lens_mod.LensProtocolError):
        extract_text(b"\x0a\x05ab")


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text or content.decode("utf-8", errors="replace")


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        item = self.responses.pop(0) if self.responses else _FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def test_client_posts_the_payload_and_returns_the_text(monkeypatch):
    session = _FakeSession([_FakeResponse(content=_fake_response([("Order #1234", "")]))])
    client = LensClient()
    monkeypatch.setattr(client, "_session", session)

    text = client.ocr(b"\xff\xd8jpeg", width=100, height=200)

    assert text == "Order #1234"
    call = session.calls[0]
    assert call["url"].endswith("/v1/crupload")
    assert call["headers"]["Content-Type"] == "application/x-protobuf"
    assert call["headers"]["X-Goog-Api-Key"]
    assert call["data"]


def test_client_retries_a_transient_status(monkeypatch):
    session = _FakeSession([
        _FakeResponse(status_code=503, text="busy"),
        _FakeResponse(content=_fake_response([("Order #1234", "")])),
    ])
    client = LensClient(max_retries=2, backoff_base=0)
    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr(lens_mod.time, "sleep", lambda *_: None)

    text = client.ocr(b"\xff\xd8jpeg", width=100, height=200)

    assert text == "Order #1234"
    assert len(session.calls) == 2


def test_client_raises_on_a_hard_error(monkeypatch):
    session = _FakeSession([_FakeResponse(status_code=400, text="bad request")])
    client = LensClient(max_retries=0)
    monkeypatch.setattr(client, "_session", session)

    with pytest.raises(LensHTTPError) as exc:
        client.ocr(b"\xff\xd8jpeg", width=100, height=200)
    assert exc.value.status_code == 400


def test_client_raises_after_the_retries_are_used_up(monkeypatch):
    session = _FakeSession([
        _FakeResponse(status_code=500, text="boom"),
        _FakeResponse(status_code=500, text="boom"),
    ])
    client = LensClient(max_retries=1, backoff_base=0)
    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr(lens_mod.time, "sleep", lambda *_: None)

    with pytest.raises(LensHTTPError):
        client.ocr(b"\xff\xd8jpeg", width=100, height=200)
    assert len(session.calls) == 2


def test_retry_after_header_is_honoured(monkeypatch):
    slept = []
    session = _FakeSession([
        _FakeResponse(status_code=429, headers={"retry-after": "3"}, text="slow down"),
        _FakeResponse(content=_fake_response([("Order #1234", "")])),
    ])
    client = LensClient(max_retries=1)
    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr(lens_mod.time, "sleep", lambda s: slept.append(s))

    client.ocr(b"\xff\xd8jpeg", width=100, height=200)

    assert slept == [3.0]


def test_client_uses_a_fresh_uuid_per_call(monkeypatch):
    session = _FakeSession([
        _FakeResponse(content=_fake_response([("a", "")])),
        _FakeResponse(content=_fake_response([("b", "")])),
    ])
    client = LensClient()
    monkeypatch.setattr(client, "_session", session)

    client.ocr(b"\xff\xd8jpeg", width=10, height=10)
    client.ocr(b"\xff\xd8jpeg", width=10, height=10)

    assert len(session.calls) == 2
    assert session.calls[0]["data"] != session.calls[1]["data"]
