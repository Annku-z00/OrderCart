"""
tests/test_business_rules.py
============================

Regression tests for the extraction business rules:

* **6+ digit** order ids of every length (no fixed 16/18-digit minimum),
* explicitly **labelled** ids are preferred (``Order ID`` / ``Order Id`` /
  ``Order #`` / ``Order No`` …),
* a sub-six-digit number is never a complete id — the name then address
  fallbacks are used first,
* WhatsApp **text** messages are inspected directly (no OCR) even when there is
  no screenshot, and ids are merged from text **and** images (first-seen order,
  de-duplicated),
* unlabelled PIN codes, phone numbers, prices, dates and product numbers are
  rejected,
* adjacent digit groups are never concatenated,
* partial failures / retries never duplicate output.

Everything is mocked (fake API + fake OCR) — no network, no OCR, no OpenCV.
"""

from __future__ import annotations

import pytest

from extractor.parser import (
    extract_fields,
    find_all_order_ids,
    find_order_id,
)
from extractor.rules import Rules
from output.writer import NotepadWriter
from state.state import StateStore

from tests.test_phase3 import (
    CONFIG,
    CONFIG_MULTI,
    FakeAPI,
    FakeOCR,
    RecordingWriter,
    build_orchestrator,
    make_message,
)
from tests.test_multi_order import FlakyAPI

RULES = Rules()


# ---------------------------------------------------------------------------
# 1. Order-id lengths (6+ digits, no fixed minimum)
# ---------------------------------------------------------------------------
class TestOrderIdLengths:
    def test_six_digit_labelled_in_message(self):
        result = extract_fields("", "Order Id: 482910", RULES)
        # §3.1: a labelled value shorter than 16 digits is a *crop* -> TRUNC.
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:482910"
        assert result["order_ids"] == []
        assert result["partial"] is True
        assert result["source"] == "text"

    @pytest.mark.parametrize("digits", [6, 7, 8, 9, 10, 12, 14, 15])
    def test_ids_six_to_fifteen_digits(self, digits):
        value = "1" * digits
        result = extract_fields(f"Order ID: {value}", "", RULES)
        # §3.1: 6–15 digits is a partial (cropped) id -> TRUNC:<last6>.
        assert result["type"] == "TRUNC"
        assert result["order_id"] == f"TRUNC:{value[-6:]}"
        assert result["partial"] is True

    @pytest.mark.parametrize("digits", [16, 17, 18])
    def test_ids_sixteen_to_eighteen_digits(self, digits):
        value = "3" * digits
        result = extract_fields(f"Order ID: {value}", "", RULES)
        # §3.1: 16–18 digits is a full id -> bare last-6.
        assert result["type"] == "OK"
        assert result["order_ids"] == [value[-6:]]
        assert result["partial"] is False

    def test_over_eighteen_digit_labelled_run_is_not_an_id(self):
        # 19+ digits is two fields glued together — never a single order id.
        result = extract_fields("Order ID: " + "3" * 20, "", RULES)
        assert result["order_ids"] == []
        assert result["type"] == "FAILED"

    def test_no_fixed_sixteen_minimum(self):
        # A 6-digit id must not be rejected just for being short.
        assert find_order_id("Order No: 482910", RULES)[0] == "482910"

    @pytest.mark.parametrize("text,expected", [
        ("Order ID: 482910", "482910"),
        ("Order Id: 5839201", "5839201"),
        ("Order #: 541873202491", "541873202491"),
        ("Order No - 123456789012345", "123456789012345"),
        ("Order ID 3418579486740656", "3418579486740656"),
    ])
    def test_labelled_variants(self, text, expected):
        assert find_order_id(text, RULES)[0] == expected


# ---------------------------------------------------------------------------
# 2. Sub-six-digit numbers -> name / address fallback (never a complete id)
# ---------------------------------------------------------------------------
class TestShortNumberFallbacks:
    def test_short_number_falls_back_to_name(self):
        result = extract_fields("Order #12345\nCustomer Name: Priya Sharma", "", RULES)
        assert result["type"] == "FALLBACK_NAME"
        assert result["order_id"] == ""
        assert result["name"] == "Priya Sharma"

    def test_short_number_falls_back_to_address(self):
        text = "Order #48291\n\nAddress:\n12 MG Road, Pune\nMaharashtra 411001"
        result = extract_fields(text, "", RULES)
        assert result["type"] == "FALLBACK_ADDRESS"
        assert result["order_id"] == ""
        assert "MG Road" in result["address"]

    def test_short_number_alone_is_truncated(self):
        result = extract_fields("Order #12345", "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:12345"

    def test_trunc_not_treated_as_complete_id(self):
        text = "Order ID: 48291"
        assert find_all_order_ids(text, RULES) == []


# ---------------------------------------------------------------------------
# 3. Labelled id preferred / no joining
# ---------------------------------------------------------------------------
class TestLabelPreference:
    def test_labelled_id_beats_product_number(self):
        text = "Product Code: 3418579486740656\nOrder ID: 482910"
        assert find_order_id(text, RULES)[0] == "482910"
        assert find_all_order_ids(text, RULES) == ["482910"]

    def test_adjacent_digit_groups_are_not_joined(self):
        # "12345 67890" must never be fused into a bogus 10-digit id.
        assert find_all_order_ids("Order ID: 12345 67890", RULES) == []
        # Each fragment is sub-six-digit, so only the truncation pass reacts.
        assert find_order_id("Order ID: 12345 67890", RULES)[0] == "TRUNC:12345"


# ---------------------------------------------------------------------------
# 4. Unlabelled unrelated numbers are rejected
# ---------------------------------------------------------------------------
class TestUnrelatedNumbersRejected:
    @pytest.mark.parametrize("text", [
        "PIN 673311",
        "Pincode: 411001",
        "Deliver to\nKINJU NIVAS, P.O NEWMAHE\nKerala - 673311",
        "Address\n12 MG Road, Pune\n411001",
        "Call me on 9876543210",
        "Contact 9988776655",
    ])
    def test_pin_and_phone_rejected(self, text):
        assert find_all_order_ids(text, RULES) == []

    @pytest.mark.parametrize("text", [
        "Price: ₹123456",
        "Total Rs. 120000",
        "Amount INR 750000",
    ])
    def test_prices_rejected(self, text):
        assert find_all_order_ids(text, RULES) == []

    @pytest.mark.parametrize("text", [
        "Invoice date 15102026",
        "Batch 20261015",
        "Order placed on 15/10/2026",
    ])
    def test_dates_rejected(self, text):
        assert find_all_order_ids(text, RULES) == []

    @pytest.mark.parametrize("text", [
        "Product Code: 123456",
        "Product ID: 1234567890",
        "SKU 9876543",
        "Model No: 12345678",
        "Qty: 123456",
        "Tracking 123456789012",
    ])
    def test_product_numbers_rejected(self, text):
        assert find_all_order_ids(text, RULES) == []

    def test_labelled_six_digit_still_accepted(self):
        # The rejections above must not swallow a genuinely labelled id.
        assert find_all_order_ids("Order #482910", RULES) == ["482910"]


# ---------------------------------------------------------------------------
# 5. WhatsApp text is inspected directly (no OCR) + merged with images
# ---------------------------------------------------------------------------
class TestTextAndImageMerge:
    def test_labelled_id_in_text_only_chat(self, tmp_path):
        chat = {"id": "c1", "name": "21001"}
        api = FakeAPI(
            chats={"21001": chat},
            messages={"c1": [make_message("m1", text="Order Id: 342216136358560832")]},
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer)

        summary = orch.run(21001, 21001)

        assert summary.ok == 1
        _, result = writer.rows[0]
        # §3: last-6 of the typed id.
        assert result["order_ids"] == ["560832"]
        assert result["source"] == "text"
        # Text is parsed directly — never sent through OCR, nothing downloaded.
        assert ocr.calls == []
        assert api.media_calls == []

    def test_labelled_id_beyond_the_old_five_message_window(self, tmp_path):
        chat = {"id": "c1", "name": "21002"}
        chatter = [make_message(f"x{i}", text=f"chatter number {i}") for i in range(8)]
        messages = {"c1": chatter + [make_message("id1", text="Order Id: 342216136358560832")]}
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, FakeAPI(chats={"21002": chat}, messages=messages),
                                  FakeOCR(), writer)

        summary = orch.run(21002, 21002)

        assert summary.ok == 1
        # §3: last-6 of 342216136358560832.
        assert writer.rows[0][1]["order_ids"] == ["560832"]

    def test_multiple_ids_split_between_text_and_images(self, tmp_path):
        # multi-order behaviour is now opt-in; default is single-value per
        # §3.1–§3.6.  This test exercises the opt-in path.
        chat = {"id": "c1", "name": "21003"}
        api = FakeAPI(
            chats={"21003": chat},
            messages={
                "c1": [
                    make_message("t1", text="Order Id: 342216136358560832"),
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Order ID: 5555666677778888",
            },
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=CONFIG_MULTI)

        summary = orch.run(21003, 21003)

        assert summary.ok == 1
        # Image ids first (image-before-text priority), then the typed id — each
        # last-6 truncated.
        assert writer.rows[0][1]["order_ids"] == [
            "334444",
            "778888",
            "560832",
        ]
        assert len(ocr.calls) == 2  # both images scanned, no early stop

    def test_duplicate_id_across_text_and_image_collapses(self, tmp_path):
        chat = {"id": "c1", "name": "21004"}
        api = FakeAPI(
            chats={"21004": chat},
            messages={
                "c1": [
                    make_message("t1", text="Order ID 342022570640639296"),
                    make_message("p1", image=True),
                ]
            },
            media={"p1": b"Order ID: 342022570640639296"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, config=CONFIG_MULTI)

        orch.run(21004, 21004)

        assert writer.rows[0][1]["order_ids"] == ["639296"]

    def test_id_on_first_image_and_another_on_later_image(self, tmp_path):
        chat = {"id": "c1", "name": "21005"}
        api = FakeAPI(
            chats={"21005": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                    make_message("p3", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Just a photo",
                "p3": b"Order ID: 5555666677778888",
            },
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, config=CONFIG_MULTI)

        orch.run(21005, 21005)

        assert writer.rows[0][1]["order_ids"] == [
            "334444",
            "778888",
        ]


# ---------------------------------------------------------------------------
# 6. Partial failure + retry without duplicate output
# ---------------------------------------------------------------------------
class TestPartialFailureRetry:
    def test_partial_then_retry_merges_text_and_image_without_duplicates(self, tmp_path):
        chat = {"id": "c1", "name": "21010"}
        messages = {
            "c1": [
                make_message("t1", text="Order Id: 342216136358560832"),
                make_message("p1", image=True),
                make_message("p2", image=True),
            ]
        }
        media = {
            "p1": b"Order ID: 1111222233334444",
            "p2": b"Order ID: 5555666677778888",
        }
        state = StateStore(str(tmp_path / "state.db"))
        writer = NotepadWriter(str(tmp_path / "orders.txt"))

        # First run: p2 fails to download -> PARTIAL with p1's id + the text id.
        first = FlakyAPI(
            chats={"21010": chat}, messages=messages, media=media, fail_media={"p2"}
        )
        build_orchestrator(
            tmp_path, first, FakeOCR(), writer, state=state, config=CONFIG_MULTI
        ).run(21010, 21010)
        assert writer.read_lines() == [
            "21010,334444",
            "21010,560832",
        ]
        assert state.is_processed(21010) is False

        # Second run: the API recovered -> missing id added, no duplicates.
        second = FakeAPI(chats={"21010": chat}, messages=messages, media=media)
        summary = build_orchestrator(
            tmp_path, second, FakeOCR(), writer, state=state, config=CONFIG_MULTI
        ).run(21010, 21010)

        assert summary.ok == 1
        assert writer.read_lines() == [
            "21010,334444",
            "21010,560832",
            "21010,778888",
        ]
        assert state.is_processed(21010) is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
