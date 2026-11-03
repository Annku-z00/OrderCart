"""
ocr/lens.py
===========

The Google Lens OCR transport — implemented from scratch.

FrameFlow used to depend on ``chrome-lens-py`` for this.  That package ships
~40 generated protobuf modules, a betterproto layer, an async HTTP stack and a
CLI; all FrameFlow needs is "upload one image, read the text back".  This
module does exactly that with :mod:`requests` and the hand-rolled codec in
:mod:`ocr.proto`, so the OCR stack has **no third-party OCR dependency** any
more.

The protocol
------------
Chromium's "Search image with Google Lens" overlay talks to a single endpoint::

    POST https://lensfrontend-pa.googleapis.com/v1/crupload
    Content-Type: application/x-protobuf
    X-Goog-Api-Key: <public Chromium key>

The body is a ``LensOverlayServerRequest`` protobuf holding the JPEG bytes and
a Chromium-shaped client context (platform / surface / locale / filters).  The
answer is a ``LensOverlayServerResponse`` protobuf whose ``objects_response →
text → text_layout → paragraphs → lines → words`` chain carries the recognised
text: every word has a ``plain_text`` and a ``text_separator``, and a line is
the words joined with their separators.

Everything above is implemented here with :mod:`ocr.proto`; the field numbers
live next to the code that uses them, with the proto name in a comment.

Failure handling
----------------
A Lens failure is **always** an OCR failure — never a silently empty result.
:class:`LensError` (and its subclasses) is raised for transport problems,
HTTP error statuses, server-side errors and unparsable answers, so the caller
can retry or mark the image as unreadable.  An answer that simply contains no
text is *not* an error: it returns an empty string.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Dict, List, Optional, Tuple

import requests

from ocr.proto import (
    WireError,
    encode_bytes,
    encode_int,
    encode_message,
    encode_string,
    first_int,
    first_message,
    iter_messages,
    parse_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint / client constants (Chromium parity)
# ---------------------------------------------------------------------------
#: The only Lens endpoint in use — the same one Chromium's overlay talks to.
LENS_ENDPOINT = "https://lensfrontend-pa.googleapis.com/v1/crupload"

#: The public API key Chromium ships with the Lens overlay.  It is not a
#: secret: every Chrome install uses the same one.  It can be overridden with
#: the ``LENS_API_KEY`` environment variable.
LENS_API_KEY = "AIzaSyDr2UxVnv_U85AbhhY8XSHSIavUW0DC-sY"

LENS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LENS_HEADERS = {
    "Content-Type": "application/x-protobuf",
    "User-Agent": LENS_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Accept": "*/*",
}

#: Locale context sent with every request (Chromium parity).
LENS_CLIENT_REGION = "US"
LENS_CLIENT_TIME_ZONE = "America/New_York"

#: HTTP statuses worth another attempt.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: Statuses whose ``Retry-After`` header is honoured.
RETRY_AFTER_STATUS_CODES = frozenset({429, 503})


# ---------------------------------------------------------------------------
# Proto field numbers (Lens overlay messages)
# ---------------------------------------------------------------------------
# LensOverlayServerRequest
F_SERVER_REQUEST_OBJECTS_REQUEST = 1
# LensOverlayObjectsRequest
F_OBJECTS_REQUEST_REQUEST_CONTEXT = 1
F_OBJECTS_REQUEST_IMAGE_DATA = 3
# LensOverlayRequestContext
F_REQUEST_CONTEXT_REQUEST_ID = 3
F_REQUEST_CONTEXT_CLIENT_CONTEXT = 4
# LensOverlayRequestId
F_REQUEST_ID_UUID = 1
F_REQUEST_ID_SEQUENCE_ID = 2
F_REQUEST_ID_IMAGE_SEQUENCE_ID = 3
F_REQUEST_ID_TIME_USEC = 7
F_REQUEST_ID_MIME_TYPE = 13
# LensOverlayClientContext
F_CLIENT_CONTEXT_PLATFORM = 1
F_CLIENT_CONTEXT_SURFACE = 2
F_CLIENT_CONTEXT_LOCALE_CONTEXT = 4
F_CLIENT_CONTEXT_CLIENT_FILTERS = 17
F_CLIENT_CONTEXT_RENDERING_CONTEXT = 20
# LocaleContext
F_LOCALE_LANGUAGE = 1
F_LOCALE_REGION = 2
F_LOCALE_TIME_ZONE = 3
# AppliedFilters / AppliedFilter
F_APPLIED_FILTERS_FILTER = 1
F_APPLIED_FILTER_FILTER_TYPE = 1
# RenderingContext
F_RENDERING_ENVIRONMENT = 2
# ImageData / ImagePayload / ImageMetadata
F_IMAGE_DATA_PAYLOAD = 1
F_IMAGE_DATA_IMAGE_METADATA = 3
F_IMAGE_PAYLOAD_IMAGE_BYTES = 1
F_IMAGE_METADATA_WIDTH = 1
F_IMAGE_METADATA_HEIGHT = 2

# LensOverlayServerResponse
F_SERVER_RESPONSE_ERROR = 1
F_SERVER_RESPONSE_OBJECTS_RESPONSE = 2
# LensOverlayObjectsResponse
F_OBJECTS_RESPONSE_TEXT = 3
# Text / TextLayout
F_TEXT_TEXT_LAYOUT = 1
F_TEXT_LAYOUT_PARAGRAPHS = 1
# TextLayout.Paragraph
F_PARAGRAPH_LINES = 2
# TextLayout.Line
F_LINE_WORDS = 1
# TextLayout.Word
F_WORD_PLAIN_TEXT = 2
F_WORD_TEXT_SEPARATOR = 3

# Enum values
PLATFORM_WEB = 3
SURFACE_CHROMIUM = 4
FILTER_TYPE_AUTO_FILTER = 7
RENDERING_ENVIRONMENT_LENS_OVERLAY = 14


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LensError(RuntimeError):
    """Base class for every Lens failure (always an OCR failure)."""


class LensTransportError(LensError):
    """The request never produced an HTTP response (network / timeout)."""


class LensHTTPError(LensError):
    """The server answered with an error status."""

    def __init__(self, message: str, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class LensServerError(LensError):
    """The server answered 200 but reported an error in the payload."""


class LensProtocolError(LensError):
    """The answer could not be decoded (unexpected protobuf shape)."""


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------
def _build_request_id(
    uuid: int,
    sequence_id: int,
    image_sequence_id: int,
    time_usec: int,
    mime_type: str,
) -> bytes:
    payload = b""
    payload += encode_int(F_REQUEST_ID_UUID, uuid)
    payload += encode_int(F_REQUEST_ID_SEQUENCE_ID, sequence_id)
    payload += encode_int(F_REQUEST_ID_IMAGE_SEQUENCE_ID, image_sequence_id)
    payload += encode_int(F_REQUEST_ID_TIME_USEC, time_usec)
    payload += encode_string(F_REQUEST_ID_MIME_TYPE, mime_type)
    return payload


def _build_client_context(language: str, region: str, time_zone: str) -> bytes:
    locale = (
        encode_string(F_LOCALE_LANGUAGE, language)
        + encode_string(F_LOCALE_REGION, region)
        + encode_string(F_LOCALE_TIME_ZONE, time_zone)
    )
    filters = encode_message(
        F_APPLIED_FILTERS_FILTER,
        encode_int(F_APPLIED_FILTER_FILTER_TYPE, FILTER_TYPE_AUTO_FILTER),
    )
    rendering = encode_int(
        F_RENDERING_ENVIRONMENT, RENDERING_ENVIRONMENT_LENS_OVERLAY
    )
    return (
        encode_int(F_CLIENT_CONTEXT_PLATFORM, PLATFORM_WEB)
        + encode_int(F_CLIENT_CONTEXT_SURFACE, SURFACE_CHROMIUM)
        + encode_message(F_CLIENT_CONTEXT_LOCALE_CONTEXT, locale)
        + encode_message(F_CLIENT_CONTEXT_CLIENT_FILTERS, filters)
        + encode_message(F_CLIENT_CONTEXT_RENDERING_CONTEXT, rendering)
    )


def build_ocr_request(
    image_bytes: bytes,
    width: int,
    height: int,
    uuid: int,
    sequence_id: int = 1,
    image_sequence_id: int = 1,
    time_usec: Optional[int] = None,
    mime_type: str = "image/jpeg",
    language: str = "",
    region: str = LENS_CLIENT_REGION,
    time_zone: str = LENS_CLIENT_TIME_ZONE,
) -> bytes:
    """Build the ``LensOverlayServerRequest`` payload for one image upload."""
    if not image_bytes:
        raise LensProtocolError("cannot upload an empty image")
    if time_usec is None:
        time_usec = int(time.time() * 1_000_000)

    request_id = _build_request_id(
        uuid, sequence_id, image_sequence_id, time_usec, mime_type
    )
    client_context = _build_client_context(language, region, time_zone)
    request_context = (
        encode_message(F_REQUEST_CONTEXT_REQUEST_ID, request_id)
        + encode_message(F_REQUEST_CONTEXT_CLIENT_CONTEXT, client_context)
    )

    image_data = encode_message(
        F_IMAGE_DATA_PAYLOAD,
        encode_bytes(F_IMAGE_PAYLOAD_IMAGE_BYTES, image_bytes),
    ) + encode_message(
        F_IMAGE_DATA_IMAGE_METADATA,
        encode_int(F_IMAGE_METADATA_WIDTH, int(width))
        + encode_int(F_IMAGE_METADATA_HEIGHT, int(height)),
    )

    objects_request = (
        encode_message(F_OBJECTS_REQUEST_REQUEST_CONTEXT, request_context)
        + encode_message(F_OBJECTS_REQUEST_IMAGE_DATA, image_data)
    )
    return encode_message(
        F_SERVER_REQUEST_OBJECTS_REQUEST, objects_request
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def _decode_word(word_bytes: bytes) -> str:
    """One recognised word: its text plus the separator that follows it."""
    fields = parse_message(word_bytes)
    text = ""
    for value in fields.get(F_WORD_PLAIN_TEXT, []):
        if isinstance(value, (bytes, bytearray)):
            text = bytes(value).decode("utf-8", errors="replace")
            break
    separator = ""
    for value in fields.get(F_WORD_TEXT_SEPARATOR, []):
        if isinstance(value, (bytes, bytearray)):
            separator = bytes(value).decode("utf-8", errors="replace")
            break
    return text + separator


def _decode_line(line_bytes: bytes) -> str:
    fields = parse_message(line_bytes)
    parts: List[str] = []
    for word_bytes in iter_messages(fields, F_LINE_WORDS):
        parts.append(_decode_word(word_bytes))
    return "".join(parts).strip()


def _decode_paragraph(paragraph_bytes: bytes) -> str:
    fields = parse_message(paragraph_bytes)
    lines: List[str] = []
    for line_bytes in iter_messages(fields, F_PARAGRAPH_LINES):
        lines.append(_decode_line(line_bytes))
    return "\n".join(lines)


def extract_text(response_bytes: bytes) -> str:
    """Pull the recognised text out of a ``LensOverlayServerResponse``.

    Returns the empty string when the response carries no text at all (a
    picture without any writing on it) — that is *not* an error.
    """
    try:
        top = parse_message(response_bytes)
    except WireError as exc:
        raise LensProtocolError(f"could not decode the Lens response: {exc}") from exc

    error_payload = first_message(top, F_SERVER_RESPONSE_ERROR)
    if error_payload:
        error_fields = parse_message(error_payload)
        error_type = first_int(error_fields, 1)
        if error_type:
            raise LensServerError(f"Lens reported error type {error_type}")

    objects_response = first_message(top, F_SERVER_RESPONSE_OBJECTS_RESPONSE)
    if not objects_response:
        return ""
    objects_fields = parse_message(objects_response)
    text_payload = first_message(objects_fields, F_OBJECTS_RESPONSE_TEXT)
    if not text_payload:
        return ""
    text_fields = parse_message(text_payload)
    layout_payload = first_message(text_fields, F_TEXT_TEXT_LAYOUT)
    if not layout_payload:
        return ""
    layout_fields = parse_message(layout_payload)

    paragraphs: List[str] = []
    for paragraph_bytes in iter_messages(layout_fields, F_TEXT_LAYOUT_PARAGRAPHS):
        text = _decode_paragraph(paragraph_bytes)
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------
class LensClient:
    """Uploads an image to Google Lens and returns the recognised text.

    The client owns one :class:`requests.Session` (connection reuse across the
    dozens of screenshots a run contains) and applies the retry policy:
    transient failures are retried with an exponential backoff, and a
    ``Retry-After`` header on a 429/503 replaces the computed delay.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        language: str = "",
        region: str = LENS_CLIENT_REGION,
        time_zone: str = LENS_CLIENT_TIME_ZONE,
    ) -> None:
        import os

        self.api_key = api_key or os.environ.get("LENS_API_KEY") or LENS_API_KEY
        self.endpoint = endpoint or LENS_ENDPOINT
        self.timeout = float(timeout or 15.0)
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(0.0, float(backoff_max))
        self.language = language or ""
        self.region = region or LENS_CLIENT_REGION
        self.time_zone = time_zone or LENS_CLIENT_TIME_ZONE
        self._session: Optional[requests.Session] = None
        self._sequence = 0

    # ------------------------------------------------------------------
    # Session handling
    # ------------------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        """The pooled HTTP session (created on first use)."""
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            self._session = None

    def __enter__(self) -> "LensClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # The call
    # ------------------------------------------------------------------
    def _next_ids(self) -> Tuple[int, int]:
        """Mint a request uuid and bump the per-client sequence counter."""
        self._sequence += 1
        uuid = random.randint(0, (1 << 63) - 1)
        return uuid, self._sequence

    def _headers(self) -> Dict[str, str]:
        headers = dict(LENS_HEADERS)
        headers["X-Goog-Api-Key"] = self.api_key
        return headers

    def _post(self, payload: bytes) -> bytes:
        """POST the payload, applying the retry policy. Returns the raw body."""
        last_error: Optional[Exception] = None
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.post(
                    self.endpoint,
                    data=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = LensTransportError(f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "Lens request failed (%s), attempt %d/%d",
                    type(exc).__name__,
                    attempt,
                    attempts,
                )
            else:
                status = response.status_code
                if status < 400:
                    return response.content
                body = response.text
                if status not in RETRYABLE_STATUS_CODES or attempt >= attempts:
                    raise LensHTTPError(
                        f"HTTP error: {status}", status_code=status, body=body[:500]
                    )
                last_error = LensHTTPError(
                    f"HTTP error: {status}", status_code=status, body=body[:500]
                )
                logger.warning("Lens request failed (HTTP %d), attempt %d/%d",
                               status, attempt, attempts)
                retry_after = self._retry_after(response)
                if retry_after is not None:
                    time.sleep(retry_after)
                    continue

            if attempt < attempts:
                time.sleep(self._backoff(attempt))
        raise last_error or LensTransportError("Lens request failed")

    @staticmethod
    def _retry_after(response: "requests.Response") -> Optional[float]:
        """``Retry-After`` in seconds, when the server sent a usable one."""
        if response.status_code not in RETRY_AFTER_STATUS_CODES:
            return None
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(seconds, 60.0))

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        return delay * (0.5 + 0.5 * random.random())

    def ocr(
        self,
        image_bytes: bytes,
        width: int,
        height: int,
        mime_type: str = "image/jpeg",
    ) -> str:
        """Upload one image and return the recognised text.

        Raises a :class:`LensError` subclass on every failure; an image
        without readable text returns an empty string.
        """
        uuid, sequence = self._next_ids()
        payload = build_ocr_request(
            image_bytes,
            width=width,
            height=height,
            uuid=uuid,
            sequence_id=sequence,
            image_sequence_id=sequence,
            mime_type=mime_type,
            language=self.language,
            region=self.region,
            time_zone=self.time_zone,
        )
        body = self._post(payload)
        return extract_text(body)
