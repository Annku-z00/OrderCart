"""
tests/test_phase4_3.py
======================

Phase 4.3 tests — chat lookup hardening and raw message-id extraction.
The media download assertions were migrated to WAHA's two-step flow
(``downloadMedia=true`` + ``msg["media"]["url"]``) in Phase 4.4; the
endpoint-fallback tests were removed together with the dead endpoint code.

Covers:
  A. download_media GETs msg["media"]["url"] verbatim (Phase 4.4 contract)
  B. message_id uses the raw id field (msg["id"]["id"]) and fails loudly
  C. Chat lookup with real WAHA shape (groups skipped, case-insensitive)
  D. URL encoding of chat_id on the messages endpoint
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from whatsapp.api_client import WhatsAppAPIClient, message_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_client() -> WhatsAppAPIClient:
    """Create a WhatsAppAPIClient with a dummy key (no network calls)."""
    return WhatsAppAPIClient(
        base_url="http://localhost:3001",
        api_key="test-key",
        session_name="default",
    )


def make_waha_message(raw_id: str = "AC2860C51EAF654DC31D14E37816A1FE") -> dict:
    """Return a WAHA-shaped image message dict (media already materialised)."""
    return {
        "id": {
            "fromMe": False,
            "remote": "66645292789868@lid",
            "id": raw_id,
            "$1": f"false_66645292789868@lid_{raw_id}",
            "_serialized": f"false_66645292789868@lid_{raw_id}",
        },
        "type": "image",
        "hasMedia": True,
        "media": {
            "url": (
                "http://localhost:3001/api/files/"
                f"true_66645292789868@lid_{raw_id}.jpg"
            ),
            "mimetype": "image/jpeg",
            "filename": f"true_66645292789868@lid_{raw_id}.jpg",
        },
        "size": 41107,
        "width": 576,
        "height": 1280,
        "timestamp": 1792117674,
        "from": "66645292789868@lid",
        "to": "917877981553@c.us",
    }


def make_mock_response(status_code: int = 200, content: bytes = b"") -> Mock:
    """Create a mock requests.Response."""
    resp = Mock()
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if content else ""
    return resp


# ---------------------------------------------------------------------------
# Test A — download_media GETs msg["media"]["url"] as-is (Phase 4.4 flow)
# ---------------------------------------------------------------------------
class TestDownloadMediaMediaUrl:
    def test_gets_media_url_as_given(self):
        client = make_client()
        msg = make_waha_message()
        expected_url = msg["media"]["url"]

        captured_urls = []

        def capture_get(url, **kwargs):
            captured_urls.append(url)
            return make_mock_response(200, b"\xff\xd8\xff\xe0")

        with patch.object(client.session, "get", side_effect=capture_get):
            result = client.download_media(msg)

        assert captured_urls == [expected_url]
        assert result == b"\xff\xd8\xff\xe0"

    def test_no_per_message_media_path_is_built(self):
        """No URL may be constructed from the message id any more."""
        client = make_client()
        msg = make_waha_message()

        captured_urls = []

        def capture_get(url, **kwargs):
            captured_urls.append(url)
            return make_mock_response(200, b"\xff\xd8\xff\xe0")

        with patch.object(client.session, "get", side_effect=capture_get):
            client.download_media(msg)

        url = captured_urls[0]
        assert "/api/files/" in url
        assert "/messages/" not in url
        # The serialized id (with fromMe prefix) never leaks into a URL.
        assert "false_" not in url


# ---------------------------------------------------------------------------
# Test B — message_id extraction (raw hash, fails loudly)
# ---------------------------------------------------------------------------
class TestMissingRawId:
    def test_raises_value_error(self):
        msg = {
            "id": {
                "_serialized": "false_xxx",
                "$1": "false_xxx",
            }
        }
        with pytest.raises(ValueError, match=r"raw 'id' field"):
            message_id(msg)

    def test_error_message_contains_repr(self):
        msg = {
            "id": {
                "_serialized": "false_xxx",
                "$1": "false_xxx",
            }
        }
        with pytest.raises(ValueError) as exc_info:
            message_id(msg)
        assert "_serialized" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test C — Chat lookup with real WAHA shape
# ---------------------------------------------------------------------------
class TestChatLookup:
    def test_finds_chat_by_name(self):
        client = make_client()

        chats = [
            {
                "id": {
                    "server": "lid",
                    "user": "11111111111111",
                    "_serialized": "11111111111111@lid",
                },
                "name": "Some Contact",
                "isGroup": False,
            },
            {
                "id": {
                    "server": "lid",
                    "user": "66645292789868",
                    "_serialized": "66645292789868@lid",
                },
                "name": "12988",
                "isGroup": False,
            },
            {
                "id": {
                    "server": "lid",
                    "user": "22222222222222",
                    "_serialized": "22222222222222@lid",
                },
                "name": "Another Contact",
                "isGroup": False,
            },
        ]

        with patch.object(client, "get_all_chats", return_value=chats), patch.object(
            client, "_search_chats", return_value=[]
        ):
            result = client.get_chat_by_name("12988")

        assert result is not None
        assert result["id"]["user"] == "66645292789868"
        assert result["name"] == "12988"

    def test_groups_are_skipped(self):
        client = make_client()

        chats = [
            {
                "id": {
                    "server": "lid",
                    "user": "22222222222222",
                    "_serialized": "22222222222222@lid",
                },
                "name": "12988",
                "isGroup": True,
            },
            {
                "id": {
                    "server": "lid",
                    "user": "66645292789868",
                    "_serialized": "66645292789868@lid",
                },
                "name": "12988",
                "isGroup": False,
            },
        ]

        with patch.object(client, "get_all_chats", return_value=chats), patch.object(
            client, "_search_chats", return_value=[]
        ):
            result = client.get_chat_by_name("12988")

        assert result is not None
        assert result["id"]["user"] == "66645292789868"

    def test_case_insensitive_match(self):
        client = make_client()

        chats = [
            {
                "id": {
                    "server": "lid",
                    "user": "66645292789868",
                    "_serialized": "66645292789868@lid",
                },
                "name": "12988",
                "isGroup": False,
            },
        ]

        with patch.object(client, "get_all_chats", return_value=chats), patch.object(
            client, "_search_chats", return_value=[]
        ):
            result = client.get_chat_by_name("12988")

        assert result is not None

    def test_fallback_to_user_id(self):
        client = make_client()

        chats = [
            {
                "id": {
                    "server": "lid",
                    "user": "66645292789868",
                    "_serialized": "66645292789868@lid",
                },
                "name": "Some Random Name",
                "isGroup": False,
            },
        ]

        with patch.object(client, "get_all_chats", return_value=chats), patch.object(
            client, "_search_chats", return_value=[]
        ):
            result = client.get_chat_by_name("66645292789868")

        assert result is not None
        assert result["id"]["user"] == "66645292789868"


# ---------------------------------------------------------------------------
# Test D — URL encoding of chat_id on the messages endpoint
# ---------------------------------------------------------------------------
class TestUrlEncoding:
    def test_chat_id_is_encoded_in_messages_url(self):
        client = make_client()
        chat_id = "66645292789868@lid"

        captured_urls = []

        def capture_get(url, **kwargs):
            captured_urls.append(url)
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        with patch.object(client.session, "get", side_effect=capture_get):
            client.get_messages(chat_id, limit=5)

        url = captured_urls[0]
        assert "66645292789868%40lid" in url
        assert "66645292789868@lid" not in url


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
