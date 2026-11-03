"""
test_improvements.py
====================

Regression tests for the hardening pass:

* a wrapped delivery date is never a customer name (``Oct``),
* the real ``<name> <mobile>`` line beats the date line above it,
* OCR noise in an unsupported script is rejected,
* the regional "Delivery Address" labels capture the whole block,
* a product / checkout page is not written out as an order,
* the OCR cache returns the stored text and honours the settings,
* ``config.yaml`` problems are reported before a run starts.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from core.orchestrator import Orchestrator, ProcessResult, RunSummary
from extractor.parser import (
    extract_fields_multi,
    find_address,
    find_name,
    is_valid_name,
)
from extractor.rules import Rules
from frameflow_config import DEFAULTS, load_config, validate_config
from ocr.cache import OCRCache
from ocr.engine import OCREngine
from whatsapp.api_client import WhatsAppAPIClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def rules() -> Rules:
    """The rules exactly as the shipped ``config.yaml`` loads them."""
    return Rules.from_config(load_config(os.path.join(ROOT, "config.yaml")))


# ---------------------------------------------------------------------------
# Customer-name hardening
# ---------------------------------------------------------------------------
class TestNameHardening:
    def test_a_wrapped_month_is_not_a_name(self, rules):
        assert is_valid_name("Oct", rules) is False
        assert is_valid_name("October", rules) is False
        assert is_valid_name("punitha", rules) is True

    def test_the_inline_name_beats_the_date_line_above_it(self, rules):
        # The OCR wraps the delivery date onto its own line; the customer is
        # printed as "<name> <mobile>" on the next one.
        text = (
            "மதிப்பிடப்பட்ட டெலிவரி தேதி Sunday, 25th\n"
            "Oct\n"
            "punitha 7418622538\n"
            "130/11f indhra nagar, virudhunagar,\n"
            "Tamil Nadu, 626001"
        )
        result = extract_fields_multi(text, "", rules)
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "punitha"

    def test_a_separator_before_the_phone_is_not_part_of_the_name(self, rules):
        result = extract_fields_multi(
            "Order details\npunitha • 7418622538", "", rules
        )
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "punitha"

    def test_ocr_noise_in_an_unsupported_script_is_rejected(self, rules):
        # A Telugu phone label once came back as Lao characters.
        assert is_valid_name("ລ້ລ້ ລ້", rules) is False

    def test_indic_names_stay_valid(self, rules):
        for name in ("प्रिया शर्मा", "ரவி", "సీత", "সুমন"):
            assert is_valid_name(name, rules) is True, name


# ---------------------------------------------------------------------------
# Regional delivery-address labels
# ---------------------------------------------------------------------------
class TestRegionalAddressLabels:
    def test_telegu_inline_delivery_name_and_full_address(self, rules):
        # "డెలివరీ అడ్రసు Zadda Seeyon" — the label and the customer share a
        # line, so the name must win over the address fallback (the documented
        # chain is Order ID -> name -> TRUNC -> address), while the address
        # itself is still captured in full.
        text = (
            "డెలివరీ అడ్రసు Zadda Seeyon\n"
            "8-132, NH 765, Maddalakatta, Peda Araveedu, Prakasam District,\n"
            "Prakasam District, Andhra Pradesh - 523320\n"
            "సంప్రదించు నంబరు - 9010808197"
        )
        result = extract_fields_multi(text, "", rules)
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "Zadda Seeyon"
        address, _ = find_address(text, rules)
        assert "523320" in address
        assert "Maddalakatta" in address

    def test_hindi_inline_delivery_name_is_detected(self, rules):
        text = (
            "डिलीवरी का पता Shyamu singh Tomar\n"
            "Durga colony, badokhar Mata ka\n"
            "Madhya Pradesh - 476001\n"
            "संपर्क नंबर - 9754114062"
        )
        result = extract_fields_multi(text, "", rules)
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "Shyamu singh Tomar"
        address, _ = find_address(text, rules)
        assert "Durga colony" in address
        assert "476001" in address

    def test_inline_delivery_name_without_a_pin_block(self, rules):
        # A cropped screenshot may lose the PIN line; the label+name line is
        # still a reliable name.
        text = "డెలివరీ అడ్రసు Zadda Seeyon\n8-132, NH 765, Maddalakatta"
        name, _ = find_name(text, rules)
        assert name == "Zadda Seeyon"


# ---------------------------------------------------------------------------
# WAHA chat-list pagination
# ---------------------------------------------------------------------------
class TestChatListPagination:
    @staticmethod
    def _client() -> WhatsAppAPIClient:
        return WhatsAppAPIClient(
            base_url="http://localhost:3001",
            api_key="test-key",
            session_name="gg",
            chat_cache_ttl=0,
        )

    @staticmethod
    def _response(payload):
        class _Response:
            def json(self):
                return payload

        return _Response()

    def test_waha_chats_are_paged_beyond_the_default_20(self):
        client = self._client()
        pages = [
            {"chats": [{"id": f"u{i}@c.us"} for i in range(client._WAHA_CHATS_PAGE)]},
            {"chats": [{"id": "918740023243@s.whatsapp.net"}]},
        ]
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append(kwargs.get("params"))
            return self._response(pages[len(calls) - 1])

        with patch.object(client, "_request", side_effect=fake_request):
            chats = client.get_all_chats(force_refresh=True)

        assert len(chats) == client._WAHA_CHATS_PAGE + 1
        assert calls == [
            {"limit": client._WAHA_CHATS_PAGE, "offset": 0},
            {"limit": client._WAHA_CHATS_PAGE, "offset": client._WAHA_CHATS_PAGE},
        ]

    def test_a_short_first_page_stops_after_one_request(self):
        client = self._client()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append(kwargs.get("params"))
            return self._response([{"id": "919999999999@c.us"}])

        with patch.object(client, "_request", side_effect=fake_request):
            chats = client.get_all_chats(force_refresh=True)

        assert len(chats) == 1
        assert len(calls) == 1

    def test_the_cached_list_is_reused(self):
        client = self._client()
        client.chat_cache_ttl = 60
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append(kwargs.get("params"))
            return self._response([{"id": "919999999999@c.us"}])

        with patch.object(client, "_request", side_effect=fake_request):
            client.get_all_chats()
            client.get_all_chats()

        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Run summary: order-id count vs chat count
# ---------------------------------------------------------------------------
class TestOrderIdCount:
    def test_a_multi_order_chat_counts_every_id(self):
        summary = RunSummary(total=2)
        summary.record(
            ProcessResult(
                internal_id=12988,
                status="OK",
                order_ids=[
                    "342343834138346752",
                    "342512705",
                    "342525200682166272",
                ],
            )
        )
        summary.record(
            ProcessResult(internal_id=12989, status="FALLBACK_NAME", name="punitha")
        )

        # ``ok`` stays the number of chats; the KPI counts the ids.
        assert summary.ok == 1
        assert summary.order_id_count == 4
        assert "order ids      : 4" in summary.report()

    def test_missing_and_failed_chats_count_no_ids(self):
        summary = RunSummary(total=2)
        summary.record(ProcessResult(internal_id=1, status="MISSING"))
        summary.record(ProcessResult(internal_id=2, status="FAILED"))

        assert summary.order_id_count == 0


# ---------------------------------------------------------------------------
# Product pages are not orders
# ---------------------------------------------------------------------------
class TestProductPageGuard:
    def test_a_product_page_is_recognised(self):
        product = (
            "1 Product\nTRACK ORDER\nDelivery by Thursday, 29th Oct\n"
            "BUY 1\nGot a great deal on this product!\n"
            "Meesho Loans\nCheck Offer\nSwitch to UPI\nPay Now\n"
            "Continue Shopping"
        )
        assert Orchestrator._looks_like_product_page(product) is True

    def test_a_real_order_screen_is_not_a_product_page(self):
        order = (
            "ORDER DETAILS\nOrder #342581970718279808\n"
            "All issue easy returns\nOrder Placed\nDelivery by Sun, 25 Oct"
        )
        assert Orchestrator._looks_like_product_page(order) is False

    def test_empty_text_is_not_a_product_page(self):
        assert Orchestrator._looks_like_product_page("") is False


# ---------------------------------------------------------------------------
# OCR cache
# ---------------------------------------------------------------------------
class TestOCRCache:
    def test_round_trip(self, tmp_path):
        cache = OCRCache(str(tmp_path))
        settings = {"upscale": 1, "jpeg_quality": 90}
        assert cache.get("abc", "full", settings) is None
        cache.put("abc", "full", settings, "Order #123")
        assert cache.get("abc", "full", settings) == "Order #123"
        assert cache.hits == 1

    def test_settings_partition_the_entries(self, tmp_path):
        cache = OCRCache(str(tmp_path))
        cache.put("abc", "full", {"upscale": 1}, "one")
        assert cache.get("abc", "full", {"upscale": 2}) is None
        assert cache.get("abc", "focus", {"upscale": 1}) is None

    def test_an_empty_read_is_cached_too(self, tmp_path):
        cache = OCRCache(str(tmp_path))
        cache.put("abc", "full", {}, "")
        assert cache.get("abc", "full", {}) == ""

    def test_disabled_cache_does_nothing(self, tmp_path):
        cache = OCRCache("")
        cache.put("abc", "full", {}, "text")
        assert cache.get("abc", "full", {}) is None

    def test_a_corrupt_entry_is_a_miss(self, tmp_path):
        cache = OCRCache(str(tmp_path))
        settings = {"upscale": 1}
        key = cache.key("abc", "full", settings)
        (tmp_path / (key + ".json")).write_text("{not json", encoding="utf-8")
        assert cache.get("abc", "full", settings) is None


class TestEngineCache:
    def test_the_second_read_comes_from_the_cache(self, tmp_path):
        image = os.path.join(
            ROOT, "assets", "12988",
            "false_66645292789868@lid_AC010D775FF360B92D2A182122776426.jpg",
        )
        if not os.path.isfile(image):
            pytest.skip("sample screenshot is not available")

        engine = OCREngine(cache_dir=str(tmp_path))
        calls = {"n": 0}

        def fake_ocr(image_bytes, width, height):
            calls["n"] += 1
            return "Order #342581970718279808"

        engine._client.ocr = fake_ocr  # type: ignore[assignment]

        first = engine.run_on_file(image)
        second = engine.run_on_file(image)

        assert first.text == second.text == "Order #342581970718279808"
        assert calls["n"] == 1
        assert second.engine == "lens-cache"


class TestConnectionCheck:
    def test_a_dead_server_is_reported_not_swallowed(self, monkeypatch):
        from whatsapp.api_client import WhatsAppAPIClient

        client = WhatsAppAPIClient(base_url="http://127.0.0.1:1", api_key="")

        def boom(*args, **kwargs):
            raise RuntimeError("server down")

        monkeypatch.setattr(client, "_request", boom)
        assert client.check_connection() == "server down"

    def test_a_live_server_returns_none(self, monkeypatch):
        from whatsapp.api_client import WhatsAppAPIClient

        client = WhatsAppAPIClient(base_url="http://127.0.0.1:1", api_key="")
        monkeypatch.setattr(client, "_request", lambda *a, **k: object())
        assert client.check_connection() is None


# ---------------------------------------------------------------------------
# config.yaml validation
# ---------------------------------------------------------------------------
class TestConfigValidation:
    def test_the_shipped_config_is_clean(self):
        assert validate_config(load_config("config.yaml")) == []

    def test_a_broken_regex_is_reported(self):
        problems = validate_config(
            {"extraction": {"order_id_regex_primary": "([unclosed"}}
        )
        assert any("order_id_regex_primary" in p for p in problems)

    def test_an_unknown_collect_mode_is_reported(self):
        problems = validate_config({"extraction": {"collect": "everything"}})
        assert any("collect" in p for p in problems)

    def test_defaults_match_the_shipped_workflow(self):
        # A missing config.yaml must not silently go back to the old
        # 6-digit / single-row workflow.
        assert DEFAULTS["extraction"]["order_id_truncate_length"] == 0
        assert DEFAULTS["extraction"]["order_id_truncate_from"] == "first"
        assert DEFAULTS["extraction"]["multi_order_output"] is True
        assert DEFAULTS["extraction"]["cropped_id_is_complete"] is True
        assert DEFAULTS["output"]["default_file"] == "orders.csv"
