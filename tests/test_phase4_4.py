"""
tests/test_phase4_4.py
======================

Phase 4.4 tests — the WAHA two-step media flow.

WAHA CORE 2026.9+ removed the ``/api/{session}/chats/{chatId}/messages/{id}/media``
endpoint (HTTP 404).  The supported flow is:

1. ``GET .../messages?downloadMedia=true`` — WAHA materialises media
   server-side and adds ``media.url`` (→ ``/api/files/<name>``) to each
   message.
2. ``download_media(msg)`` GETs exactly ``msg["media"]["url"]`` with the
   ``X-Api-Key`` header.

Covers:
  A. get_messages passes downloadMedia=true, encodes the chat id, uses a
     media-friendly timeout and still raises ChatNotFoundError on 404
  B. download_media reads msg["media"]["url"] and nothing else
  C. pick_extension / mimetype → file extension mapping
  D. Orchestrator saves the downloaded media with the derived extension
"""

from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest

from core.orchestrator import Orchestrator, pick_extension
from extractor.rules import Rules
from state.state import StateStore
import requests

from whatsapp.api_client import (
    ChatNotFoundError,
    WhatsAppAPIClient,
    WhatsAppAPIError,
)

from tests.test_phase3 import CONFIG, FakeOCR, RecordingWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_client(**kwargs) -> WhatsAppAPIClient:
    """Create a WhatsAppAPIClient with a dummy key (no network calls)."""
    defaults = dict(
        base_url="http://localhost:3001",
        api_key="test-key",
        session_name="default",
    )
    defaults.update(kwargs)
    return WhatsAppAPIClient(**defaults)


def make_media_message(
    raw_id: str = "AC2860C51EAF654DC31D14E37816A1FE",
    mimetype: str = "image/jpeg",
    url: str = "http://localhost:3001/api/files/true_66645292789868@lid_x.jpg",
) -> dict:
    """A WAHA image message as returned with downloadMedia=true."""
    return {
        "id": {
            "fromMe": False,
            "remote": "66645292789868@lid",
            "id": raw_id,
            "_serialized": f"false_66645292789868@lid_{raw_id}",
        },
        "type": "image",
        "hasMedia": True,
        "media": {
            "url": url,
            "mimetype": mimetype,
            "filename": "true_66645292789868@lid_x.jpg",
        },
        "timestamp": 1792117674,
    }


def make_response(status_code: int = 200, content: bytes = b"") -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if content else ""
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error"
        )
    return resp


# ---------------------------------------------------------------------------
# A. get_messages
# ---------------------------------------------------------------------------
class TestGetMessagesDownloadMedia:
    def test_passes_download_media_true(self):
        """The WAHA branch must ask WAHA to materialise media by default."""
        client = make_client()
        kwargs: dict = {}

        def capture_get(url, **kw):
            kwargs.update(kw)
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        with patch.object(client.session, "get", side_effect=capture_get):
            client.get_messages("66645292789868@lid", limit=10)

        assert kwargs["params"]["downloadMedia"] == "true"

    def test_messages_url_is_correct(self):
        client = make_client()
        urls: list[str] = []

        def capture_get(url, **kw):
            urls.append(url)
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        with patch.object(client.session, "get", side_effect=capture_get):
            client.get_messages("66645292789868@lid", limit=10)

        assert urls[0].endswith("/chats/66645292789868%40lid/messages")

    def test_timeout_is_60s_by_default(self):
        client = make_client()
        kwargs: dict = {}

        def capture_get(url, **kw):
            kwargs.update(kw)
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        with patch.object(client.session, "get", side_effect=capture_get):
            client.get_messages("66645292789868@lid")

        # Server-side media fetch is slow the first time — the media
        # timeout (default 60s) is used, not the fast API timeout (30s).
        assert client.media_timeout == 60
        assert kwargs["timeout"] == 60

    def test_404_raises_chat_not_found(self):
        """A missing chat must still surface as ChatNotFoundError."""
        client = make_client()

        with patch.object(client.session, "get", return_value=make_response(404, b"")):
            with pytest.raises(ChatNotFoundError):
                client.get_messages("66645292789868@lid")

    def test_other_http_errors_raise_waha_api_error(self):
        client = make_client()

        with patch.object(client.session, "get", return_value=make_response(500, b"boom")):
            with pytest.raises(WhatsAppAPIError):
                client.get_messages("66645292789868@lid")


# ---------------------------------------------------------------------------
# B. download_media
# ---------------------------------------------------------------------------
class TestDownloadMediaViaMediaUrl:
    def test_uses_media_url_exactly(self):
        client = make_client()
        msg = make_media_message()
        expected = (
            "http://localhost:3001/api/files/true_66645292789868@lid_x.jpg"
        )
        calls: list[tuple] = []

        def capture_get(url, **kwargs):
            calls.append((url, kwargs))
            return make_response(200, b"\xff\xd8\xff\xe0jpeg")

        with patch.object(client.session, "get", side_effect=capture_get):
            result = client.download_media(msg)

        # Called with that exact URL — nothing constructed, nothing else.
        assert calls == [(expected, {"headers": client.headers, "timeout": 60})]
        assert result == b"\xff\xd8\xff\xe0jpeg"

    def test_sends_x_api_key_header(self):
        client = make_client()
        msg = make_media_message()
        kwargs: dict = {}

        def capture_get(url, **kw):
            kwargs.update(kw)
            return make_response(200, b"jpeg")

        with patch.object(client.session, "get", side_effect=capture_get):
            client.download_media(msg)

        assert kwargs["headers"]["X-Api-Key"] == "test-key"

    def test_raises_when_media_url_missing(self):
        client = make_client()
        msg = {
            "id": {"fromMe": False, "remote": "66645292789868@lid",
                   "id": "AC2860C51EAF654DC31D14E37816A1FE"},
            "type": "image",
            "hasMedia": True,
            # no "media" key -> no media.url (fetched without downloadMedia)
        }

        with pytest.raises(WhatsAppAPIError, match="downloadMedia=true"):
            client.download_media(msg)

    def test_raises_when_url_empty(self):
        client = make_client()
        msg = make_media_message(url="")

        with pytest.raises(WhatsAppAPIError, match="downloadMedia=true"):
            client.download_media(msg)

    def test_raises_on_non_200_response(self):
        client = make_client()
        msg = make_media_message()

        with patch.object(client.session, "get", return_value=make_response(500, b"Server Error")):
            with pytest.raises(WhatsAppAPIError, match="Media download failed"):
                client.download_media(msg)

    def test_raises_on_empty_body(self):
        client = make_client()
        msg = make_media_message()

        with patch.object(client.session, "get", return_value=make_response(200, b"")):
            with pytest.raises(WhatsAppAPIError, match="empty body"):
                client.download_media(msg)

    def test_rebases_url_from_waha_internal_port(self):
        """WAHA builds media.url from its internal base — we re-host it."""
        client = make_client()  # base_url http://localhost:3001
        msg = make_media_message()
        msg["media"]["url"] = "http://localhost:3000/api/files/x.jpg"

        captured_urls: list[str] = []

        def capture_get(url, **kwargs):
            captured_urls.append(url)
            return make_response(200, b"jpeg")

        with patch.object(client.session, "get", side_effect=capture_get):
            client.download_media(msg)

        assert captured_urls == ["http://localhost:3001/api/files/x.jpg"]

    def test_evolution_message_without_media_url_uses_base64_flow(self):
        """Evolution keeps its base64 endpoint (regression guard)."""
        client = make_client(provider="evolution", api_key="evo-key")
        msg = {
            "key": {"id": "ABC", "remoteJid": "15551234567@s.whatsapp.net"},
            "message": {"imageMessage": {}},
        }
        with patch.object(client, "_request") as req:
            req.return_value.json.return_value = {"base64": "aGVsbG8="}
            assert client.download_media(msg) == b"hello"
            args, kwargs = req.call_args
            assert args[0] == "POST"
            assert "getBase64FromMediaMessage" in args[1]


# ---------------------------------------------------------------------------
# C. pick_extension
# ---------------------------------------------------------------------------
class TestPickExtension:
    @pytest.mark.parametrize(
        "mimetype,expected",
        [
            ("image/jpeg", ".jpg"),
            ("image/png", ".png"),
            ("image/webp", ".webp"),
            ("image/gif", ".gif"),
            ("image/bmp", ".bmp"),
        ],
    )
    def test_known_mimetypes(self, mimetype, expected):
        msg = {"media": {"mimetype": mimetype}}
        assert pick_extension(msg) == expected

    def test_unknown_mimetype_defaults_to_img(self):
        assert pick_extension({"media": {"mimetype": "video/mp4"}}) == ".img"

    def test_missing_media_defaults_to_img(self):
        assert pick_extension({}) == ".img"
        assert pick_extension(None) == ".img"

    def test_mimetype_case_insensitive(self):
        assert pick_extension({"media": {"mimetype": "Image/JPEG"}}) == ".jpg"


# ---------------------------------------------------------------------------
# D. Orchestrator integration — saved asset uses the derived extension
# ---------------------------------------------------------------------------
class MediaAPI:
    """API double whose image message carries a WAHA media block."""

    def __init__(self, msg, blob: bytes):
        self.msg = msg
        self.blob = blob
        self.downloaded: list = []

    def get_chat_by_name(self, name):
        return {"id": f"chat-{name}", "name": str(name)}

    def get_messages(self, chat_id, limit=10):
        return [self.msg]

    def download_media(self, msg):
        self.downloaded.append(msg)
        return self.blob

    def close(self):
        pass


class TestOrchestratorSavesWithExtension:
    def _run(self, tmp_path, msg, mimetype, blob=b"Order ID: 342022570640639296"):
        msg["media"]["mimetype"] = mimetype
        api = MediaAPI(msg, blob)
        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=RecordingWriter(),
            state=StateStore(str(tmp_path / "state.db")),
            rules=Rules.from_config(CONFIG),
            config=CONFIG,
            assets_dir=str(tmp_path / "assets"),
        )
        summary = orch.run(12988, 12988)
        asset_dir = tmp_path / "assets" / "12988"
        files = sorted(os.listdir(asset_dir)) if asset_dir.is_dir() else []
        return summary, files, api

    def test_jpeg_saved_as_jpg(self, tmp_path):
        msg = {
            "id": {"id": "m1"},
            "type": "image",
            "hasMedia": True,
            "fromMe": False,
            "media": {"mimetype": "image/jpeg", "url": "http://x/api/files/m1.jpg"},
        }
        summary, files, _ = self._run(tmp_path, msg, "image/jpeg")
        assert summary.ok == 1
        assert files == ["m1.jpg"]

    def test_png_saved_as_png(self, tmp_path):
        msg = {
            "id": {"id": "m2"},
            "type": "image",
            "hasMedia": True,
            "fromMe": False,
            "media": {"mimetype": "image/png", "url": "http://x/api/files/m2.png"},
        }
        summary, files, _ = self._run(tmp_path, msg, "image/png")
        assert summary.ok == 1
        assert files == ["m2.png"]

    def test_unknown_mimetype_saved_as_img(self, tmp_path):
        msg = {
            "id": {"id": "m3"},
            "type": "image",
            "hasMedia": True,
            "fromMe": False,
            "media": {"mimetype": "image/avif", "url": "http://x/api/files/m3"},
        }
        summary, files, _ = self._run(tmp_path, msg, "image/avif")
        assert summary.ok == 1
        assert files == ["m3.img"]

    def test_bytes_written_unchanged(self, tmp_path):
        blob = b"\x89PNG\r\n\x1a\nfake-png-bytes"
        msg = {
            "id": {"id": "m4"},
            "type": "image",
            "hasMedia": True,
            "fromMe": False,
            "media": {"mimetype": "image/png", "url": "http://x/api/files/m4.png"},
        }
        summary, files, _ = self._run(tmp_path, msg, "image/png", blob=blob)
        assert files == ["m4.png"]
        path = tmp_path / "assets" / "12988" / "m4.png"
        assert path.read_bytes() == blob


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
