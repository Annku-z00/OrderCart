"""
tests/test_order_id_detection.py
================================

Regression tests for the **order-id detection** fixes.

They cover the three defects that made FrameFlow miss real Meesho order ids
(found by running the pipeline over the seller's 69 genuine screenshots of
chat 12988):

1. a **cut-off** id (``Order #342578720599009...``) used to lose to the
   address fallback, so the id was thrown away;
2. every screenshot of a chat was merged into **one** OCR blob, so the label
   of one screenshot could swallow the digits of the next one — and the chat
   produced a single output row for dozens of orders;
3. the multi-order scan picked up **every** 6+ digit number of a screenshot
   (the product code in the thumbnail, a PIN code) as if it were an order id.
"""

from __future__ import annotations

import os

import pytest

from core.orchestrator import Orchestrator
from extractor.parser import extract_fields, extract_fields_multi, find_all_order_ids
from extractor.rules import Rules
from output.csv_writer import CSVWriter
from output.writer import NotepadWriter, format_notepad_lines
from output.corrections import apply_corrections

from tests.test_phase3 import (
    CONFIG,
    FakeAPI,
    FakeOCR,
    RecordingWriter,
    build_orchestrator,
    make_message,
)


#: The shared test config, with the id window taken from the **front** of the
#: order id so the expectations read like the seller's own Notepad keys.
CONFIG_FIRST = {
    **CONFIG,
    "extraction": {
        **CONFIG["extraction"],
        "order_id_truncate_length": 6,
        "order_id_truncate_from": "first",
    },
}


def _rules(**extraction) -> Rules:
    cfg = {
        "extraction": {
            "order_id_truncate_length": 6,
            "order_id_truncate_from": "first",
            **extraction,
        }
    }
    return Rules.from_config(cfg)


# ---------------------------------------------------------------------------
# 1. A cut-off id must not lose to the address block
# ---------------------------------------------------------------------------
class TestTruncatedIdBeatsAddress:
    def test_cut_off_id_wins_over_the_address(self):
        # The genuine OCR text of a Meesho screenshot whose order id is cut
        # off ("Order #342578720599009...") — the delivery block is noise, the
        # id is the key.
        text = (
            "ORDER DETAILS\n"
            "HELP\n"
            "WITH PHOTOS\n"
            "SMALL SIZE\n"
            "Order #342578720599009...\n"
            "kk Royal photo frame 5 x 7, photo ...\n"
            "7014965655\n"
            "BUY 1\n"
            "Free Size Prepaid\n"
            "All issue easy returns\n"
            "Order Placed\n"
            "Delivery by Mon, 26 Oct\n"
            "Delivery Address\n"
            "CHANGE\n"
            "Syed Sadiq Ali\n"
            "3/132, Raziv Nagar Colony, Police Station Road,\n"
            "Nekkonda, Telangana, 506122\n"
            "9666611202\n"
        )
        result = extract_fields(text, "", _rules())
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:342578720599009"

    def test_a_name_still_wins_over_a_cut_off_id(self):
        # The documented rule: a customer name is a complete, readable
        # identifier, so it keeps its place in the chain.
        text = "Order #341972763328...\nDelivery Address\nmayank"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "mayank"

    def test_a_short_fragment_still_loses_to_the_address(self):
        # Five digits could be anything — the address stays the better answer.
        text = "Order #48291\n\nAddress:\n12 MG Road, Pune\nMaharashtra 411001"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_ADDRESS"

    def test_the_threshold_is_configurable(self):
        text = "Order #48291\n\nAddress:\n12 MG Road, Pune\nMaharashtra 411001"
        result = extract_fields(text, "", _rules(truncated_id_min_digits=4))
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:48291"

    def test_the_threshold_can_be_switched_off(self):
        text = (
            "Order #342578720599009...\n"
            "Delivery Address\n12 MG Road, Pune\nMaharashtra 411001\n"
        )
        result = extract_fields(text, "", _rules(truncated_id_min_digits=0))
        # 0 means "no preference" -> the address fallback answers again.
        assert result["type"] == "FALLBACK_ADDRESS"


# ---------------------------------------------------------------------------
# 2. One chat, many orders: one row per order
# ---------------------------------------------------------------------------
class TestPerImageExtraction:
    def test_every_screenshot_contributes_its_own_id(self, tmp_path):
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order #342581970718279808",
                "p2": b"Order ID 342566565257791360",
            },
        )
        writer = RecordingWriter()
        orch = build_orchestrator(
            tmp_path,
            api,
            FakeOCR(),
            writer,
            config={
                **CONFIG_FIRST,
                "extraction": {
                    **CONFIG_FIRST["extraction"],
                    "multi_order_output": True,
                },
            },
        )

        summary = orch.run(12988, 12988)

        assert summary.ok == 1
        _iid, result = writer.rows[0]
        assert result["order_ids"] == ["342581", "342566"]
        assert result["order_id"] == "342581"

    def test_one_row_per_order_in_the_output_file(self, tmp_path):
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                    make_message("p3", image=True),
                ]
            },
            media={
                "p1": b"Order #342581970718279808",
                "p2": b"Order ID 342566565257791360",
                "p3": b"Order ID 342687779165546496",
            },
        )
        path = os.path.join(str(tmp_path), "orders.txt")
        orch = build_orchestrator(
            tmp_path,
            api,
            FakeOCR(),
            NotepadWriter(path),
            config={
                **CONFIG_FIRST,
                "extraction": {
                    **CONFIG_FIRST["extraction"],
                    "multi_order_output": True,
                },
            },
        )

        orch.run(12988, 12988)

        assert NotepadWriter(path).read_lines() == [
            "12988,342581",
            "12988,342566",
            "12988,342687",
        ]

    def test_single_value_mode_keeps_the_first_id(self, tmp_path):
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order #342581970718279808",
                "p2": b"Order ID 342566565257791360",
            },
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, config=CONFIG_FIRST)

        orch.run(12988, 12988)

        _iid, result = writer.rows[0]
        assert result["order_ids"] == ["342581"]
        assert result["ambiguous"] is True

    def test_a_label_cannot_swallow_the_next_screenshots_digits(self, tmp_path):
        """The label of screenshot #1 must not match the digits of #2."""
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order #\n",          # a label with no value of its own
                "p2": b"342566565257791360",  # the id of the second screenshot
            },
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, config=CONFIG_FIRST)

        orch.run(12988, 12988)

        _iid, result = writer.rows[0]
        assert result["order_id"] in ("", "342566")

    def test_the_chat_text_is_never_ocr_ed(self, tmp_path):
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "c1": [
                    make_message("t1", text="Order ID 342566565257791360"),
                    make_message("p1", image=True),
                ]
            },
            media={"p1": b"Order #342581970718279808"},
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=CONFIG_FIRST)

        orch.run(12988, 12988)

        # Only the image was sent to the OCR engine.
        assert len(ocr.calls) == 1
        _iid, result = writer.rows[0]
        assert result["order_ids"] == ["342581"]


# ---------------------------------------------------------------------------
# 3. The multi-order scan must not swallow every number of a screenshot
# ---------------------------------------------------------------------------
class TestMultiOrderScanIsAnchored:
    def test_a_product_code_next_to_an_order_label_is_not_an_id(self):
        text = (
            "WITH PHOTOS\nSMALL SIZE\n"
            "7014965655\nBUY 1\n"
            "Order #342691200486696067\n"
        )
        assert find_all_order_ids(text, _rules()) == ["342691200486696067"]

    def test_a_bare_number_alone_is_still_found(self):
        # Nothing is labelled, so the unlabelled scan answers.
        assert find_all_order_ids("1111222233334444", _rules()) == ["1111222233334444"]

    def test_the_scan_is_anchored_on_the_label(self):
        text = "Order #342581970718279808\n7014965655"
        assert find_all_order_ids(text, _rules()) == ["342581970718279808"]

    def test_an_unlabelled_pin_code_is_never_an_order_id(self):
        # The unlabelled fallback is restricted to the Meesho id band
        # (15-18 digits) - a bare 6-digit number could be a PIN code.
        assert find_all_order_ids("Delivery Address\nPune - 411001", _rules()) == []
        assert find_all_order_ids("Qty 4\n710070228", _rules()) == []

    def test_an_unlabelled_product_code_is_never_an_order_id(self):
        assert find_all_order_ids("SMALL SIZE\n7014965655\nBUY 1", _rules()) == []

    def test_a_mobile_number_next_to_the_label_is_never_an_order_id(self):
        # The phone filter also applies to a labelled value.
        assert find_all_order_ids("Order ID\n9876543210", _rules()) == []
        result = extract_fields("Order ID\n9876543210\nDelivery Address", "", _rules())
        assert result["order_id"] != "9876543210"

    def test_a_pin_code_next_to_the_label_is_never_an_order_id(self):
        assert find_all_order_ids("Order ID\nPIN: 517126", _rules()) == []


# ---------------------------------------------------------------------------
# 4. A cropped screenshot ("Order #342581") is a complete id
# ---------------------------------------------------------------------------
class TestCroppedIdIsComplete:
    """The customer cropped the back of the screen: the leading digits are
    all the seller needs, so the crop is written out as a plain id."""

    def test_a_cropped_id_is_a_plain_id(self):
        rules = _rules(cropped_id_is_complete=True)
        result = extract_fields("Order #342581\nRoyal Photo Frame", "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342581"
        assert result["partial"] is False

    def test_a_cut_off_id_is_a_plain_id_too(self):
        # "Order #342578720599009..." - the app's own ellipsis.
        rules = _rules(cropped_id_is_complete=True)
        result = extract_fields("Order #342578720599009...\nRoyal Photo", "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342578"

    def test_without_the_switch_a_crop_stays_a_review_item(self):
        rules = _rules()  # cropped_id_is_complete defaults to off
        result = extract_fields("Order #342581\nRoyal Photo Frame", "", rules)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:342581"
        assert result["partial"] is True

    def test_a_crop_still_beats_the_address(self):
        rules = _rules(cropped_id_is_complete=True)
        text = (
            "Order #342578720599009...\n"
            "Delivery Address\nCHANGE\n"
            "Syed Sadiq Ali\n3/132, Telangana, 506122\n"
        )
        result = extract_fields(text, "", rules)
        assert result["order_id"] == "342578"
        assert result["type"] == "OK"

    def test_a_labelled_crop_beats_the_name_too(self):
        # Regression: the same real 12988 screenshot also carries a customer
        # name.  The label-anchored crop must still win, otherwise the order
        # id disappears and the row is written as FALLBACK_NAME.
        rules = _rules(cropped_id_is_complete=True)
        text = (
            "Order #342578720599009...\n"
            "Delivery Address\nCHANGE\n"
            "Syed Sadiq Ali\n"
            "3/132, Raziv Nagar Colony, Police Station Road,\n"
            "Nekkonda, Telangana, 506122\n"
            "9666611202\n"
        )
        result = extract_fields(text, "", rules)
        assert result["order_id"] == "342578"
        assert result["type"] == "OK"
        assert result["partial"] is False

    def test_a_six_digit_crop_wins_over_the_name(self):
        # The seller's rule: order id first, even when only six digits are
        # visible.  Name/address are only fallbacks when there is no usable id.
        rules = _rules(cropped_id_is_complete=True)
        text = (
            "342578720599009...\n"
            "Delivery Address\nCHANGE\n"
            "Syed Sadiq Ali\n3/132, Telangana, 506122\n"
        )
        result = extract_fields(text, "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342578"
        assert result["partial"] is False

    def test_a_five_digit_crop_is_not_an_id(self):
        # Six visible digits is the minimum; five is too short and falls to
        # the address instead of being written as an order id.
        rules = _rules(cropped_id_is_complete=True)
        text = (
            "Order #48291\n"
            "Address:\n12 MG Road, Pune\nMaharashtra 411001\n"
        )
        result = extract_fields(text, "", rules)
        assert result["type"] == "FALLBACK_ADDRESS"
        assert result["order_id"] == ""

    def test_a_full_id_is_untouched(self):
        rules = _rules(cropped_id_is_complete=True)
        result = extract_fields("Order #342581970718279808", "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342581"
        assert result["partial"] is False


# ---------------------------------------------------------------------------
# 4b. A mangled "Order" label still leaves "#<id>" behind
# ---------------------------------------------------------------------------
class TestHashPrefixedOrderIds:
    def test_a_garbled_label_on_an_order_screen_is_recovered(self):
        rules = _rules()
        text = (
            "ఆర్డర్ వివరాలు\n"
            "HELP\n"
            "໙໕ #342572222316923008\n"
            "Royal Photo Frame 5*7\n"
            "Delivery Address\n"
            "kadali lakshmi\n"
            "pullapu street, Andhra Pradesh, 533235\n"
            "9515950840\n"
        )
        result = extract_fields(text, "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342572"

    def test_an_id_hash_receipt_is_recovered(self):
        rules = _rules()
        text = (
            "Thank you for shopping with us!\n"
            "ID: #342562154670311552\n"
            "Estimated Delivery by Monday, 26th...\n"
            "Delivery Address\n"
            "m, Ajjayya\n"
            "Ballari District, Karnataka, 583101\n"
            "8123991219\n"
        )
        result = extract_fields(text, "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342562"

    def test_a_hash_number_outside_an_order_screen_is_not_an_id(self):
        rules = _rules()
        result = extract_fields("promo code #342562154670311552", "", rules)
        assert result["order_id"] == ""
        assert result["type"] == "FAILED"


# ---------------------------------------------------------------------------
# 5. The fallback chain: id -> name -> address
# ---------------------------------------------------------------------------
class TestFallbackChain:
    """No order id -> the customer name -> the delivery address."""

    def test_the_name_is_used_when_there_is_no_id(self):
        text = "Delivery Address\nSyed Sadiq Ali\n3/132, Telangana, 506122"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "Syed Sadiq Ali"

    def test_the_address_is_used_when_there_is_no_id_and_no_name(self):
        text = "Delivery Address\n3/132, Raziv Nagar Colony, Telangana, 506122"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_ADDRESS"
        assert "Raziv Nagar Colony" in result["address"]

    def test_a_delivery_block_without_its_label_still_gives_the_address(self):
        # The customer screenshotted the BOTTOM of the order page: the
        # "Delivery Address" label is cropped off, but the shape survives -
        # a 6-digit PIN line with the mobile number right below it.
        text = (
            "232/3 teachers colony, s.vellalapstty p.o, thozhirpettai, karur.4,\n"
            "teachers colony, Karur, Tamil Nadu - 639004\n"
            "Contact Number - 9786782006\n"
            "1 Product\nTRACK ORDER\n"
        )
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_ADDRESS"
        assert "teachers colony" in result["address"]
        assert "639004" in result["address"]

    def test_the_shape_needs_both_the_pin_and_the_number(self):
        # a PIN on its own is not a delivery block
        text = "Karur, Tamil Nadu - 639004\n1 Product\nTRACK ORDER"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FAILED"

    def test_the_labelled_address_still_wins(self):
        text = (
            "Delivery Address\n3/132, Raziv Nagar Colony, Telangana, 506122\n"
            "Contact Number - 9786782006\n"
        )
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_ADDRESS"
        assert result["confidence"] == 0.72   # the labelled variant

    def test_a_name_line_with_the_mobile_number_is_found(self):
        # Meesho prints "punitha 7418622538" - the name and the number on one
        # line, with no label anywhere.
        text = "punitha 7418622538\nKarur, Tamil Nadu - 639004"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "punitha"

    def test_a_name_above_a_standalone_mobile_number_is_found(self):
        # The OCR split the name and the number onto two lines.
        text = "Nagma Bano\n6239226952\nNear Aastik Dev, Raebareli District"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "Nagma Bano"

    def test_a_product_tagline_is_never_a_name(self):
        # The Meesho product code is phone-shaped (10 digits) and sits right
        # under the tagline - neither is a customer.
        text = (
            "All issue easy returns\n7014965655\nBUY 1\n"
            "Cash on Delivery\n₹168\n"
        )
        result = extract_fields(text, "", _rules())
        assert result.get("name") != "All issue easy returns"

    def test_a_supplier_line_is_never_a_name(self):
        text = "Sold by Royal Photo Frame\n7014965655\nBUY 1"
        result = extract_fields(text, "", _rules())
        assert result.get("name") != "Sold by Royal Photo Frame"

    def test_a_phone_label_line_is_never_a_name(self):
        text = "Contact Number - 9786782006\nKarur, Tamil Nadu - 639004"
        result = extract_fields(text, "", _rules())
        assert result.get("name") != "Contact Number"

    def test_a_name_label_line_is_never_a_name(self):
        # "Customer: 9876543210" - the trap fixture of the test suite.
        text = "Order ID #\nCustomer: 9876543210"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FAILED"

    def test_an_orphaned_order_header_is_not_an_address(self):
        text = "Order ID #\nKerala - 673311"
        result = extract_fields(text, "", _rules())
        assert result["type"] == "FAILED"


# ---------------------------------------------------------------------------
# 6. The order id is written exactly as it appears
# ---------------------------------------------------------------------------
class TestOrderIdIsWrittenInFull:
    """Whatever the screenshot shows is what lands in the output file:

    6, 7, 8, 10 or 18 digits - never shortened to a fixed window."""

    def _rules(self):
        # the shipped config: no window at all, and a cropped id counts as a
        # complete id (so it is written plain, not as "TRUNC:…")
        return _rules(order_id_truncate_length=0, cropped_id_is_complete=True)

    @pytest.mark.parametrize("value", [
        "342187",        # 6  - the customer cropped the screen
        "3421695",       # 7
        "342512705",     # 9
        "342615945",     # 9
        "3426159459655617",   # 16
        "342581970718279808",  # 18 - the full id
    ])
    def test_the_id_is_written_as_shown(self, value):
        result = extract_fields(f"Order # {value}", "", self._rules())
        assert result["type"] == "OK"
        assert result["order_id"] == value

    def test_an_eight_digit_id_stays_eight_digits(self):
        result = extract_fields("Order # 34258197", "", self._rules())
        assert result["order_id"] == "34258197"

    def test_a_window_is_still_available(self):
        # the switch back to the short key
        rules = _rules(order_id_truncate_length=6, order_id_truncate_from="first")
        result = extract_fields("Order # 342581970718279808", "", rules)
        assert result["order_id"] == "342581"


# ---------------------------------------------------------------------------
# 6. The writers keep the per-row status
# ---------------------------------------------------------------------------
class TestPerRowStatus:
    def test_the_csv_keeps_a_trunc_status_on_its_own_row(self, tmp_path):
        path = os.path.join(str(tmp_path), "orders.csv")
        writer = CSVWriter(path)
        writer.write_result(
            12988,
            {
                "type": "OK",
                "order_ids": ["342581", "TRUNC:342578720599009"],
                "order_rows": [
                    {"value": "342581", "status": "OK", "partial": False},
                    {"value": "TRUNC:342578720599009", "status": "TRUNC",
                     "partial": True},
                ],
            },
        )
        rows = writer.read_rows()
        assert [r["order_id"] for r in rows] == [
            "342581",
            "TRUNC:342578720599009",
        ]
        assert rows[0]["status"] == "OK"
        assert rows[1]["status"] == "TRUNC"
        assert rows[1]["partial"] == "true"

    def test_the_notepad_format_writes_every_value(self):
        lines = format_notepad_lines(
            12988,
            {
                "type": "OK",
                "order_ids": ["342581"],
                "order_rows": [
                    {"value": "342581", "status": "OK"},
                    {"value": "TRUNC:342578720599009", "status": "TRUNC"},
                ],
            },
        )
        assert lines == ["12988,342581", "12988,TRUNC:342578720599009"]


# ---------------------------------------------------------------------------
# The review workflow targets one row of a chat
# ---------------------------------------------------------------------------
class TestRowTargetedCorrection:
    def test_a_correction_only_touches_the_pinned_row(self, tmp_path):
        path = os.path.join(str(tmp_path), "orders.txt")
        writer = NotepadWriter(path)
        writer.write_result(
            12988,
            {
                "type": "OK",
                "order_ids": ["342581"],
                "order_rows": [
                    {"value": "342581", "status": "OK"},
                    {"value": "TRUNC:342578720599009", "status": "TRUNC"},
                ],
            },
        )
        assert writer.read_lines() == [
            "12988,342581",
            "12988,TRUNC:342578720599009",
        ]

        changed = apply_corrections(
            path,
            {12988: "342578720599009"},
            matches={12988: "TRUNC:342578720599009"},
        )

        assert changed == 1
        assert writer.read_lines() == [
            "12988,342581",
            "12988,342578720599009",
        ]

    def test_without_a_pin_every_row_of_the_chat_is_rewritten(self, tmp_path):
        path = os.path.join(str(tmp_path), "orders.txt")
        writer = NotepadWriter(path)
        writer.write_result(
            12988,
            {
                "type": "OK",
                "order_ids": ["342581"],
                "order_rows": [
                    {"value": "342581", "status": "OK"},
                    {"value": "TRUNC:342578720599009", "status": "TRUNC"},
                ],
            },
        )

        changed = apply_corrections(path, {12988: "1234567890"})

        assert changed == 2
        assert writer.read_lines() == [
            "12988,1234567890",
            "12988,1234567890",
        ]

    def test_the_csv_correction_is_pinned_too(self, tmp_path):
        path = os.path.join(str(tmp_path), "orders.csv")
        writer = CSVWriter(path)
        writer.write_result(
            12988,
            {
                "type": "OK",
                "order_ids": ["342581", "TRUNC:342578720599009"],
                "order_rows": [
                    {"value": "342581", "status": "OK"},
                    {"value": "TRUNC:342578720599009", "status": "TRUNC"},
                ],
            },
        )

        changed = apply_corrections(
            path,
            {12988: "342578720599009"},
            output_format="csv",
            matches={12988: "TRUNC:342578720599009"},
        )

        assert changed == 1
        rows = writer.read_rows()
        assert [r["order_id"] for r in rows] == ["342581", "342578720599009"]


# ---------------------------------------------------------------------------
# The focus pass
# ---------------------------------------------------------------------------
class TestFocusPass:
    def test_a_header_without_an_id_is_read_again(self, tmp_path):
        """An image whose full page holds no order id gets a second look."""
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"c1": [make_message("p1", image=True)]},
            media={"p1": b"Order #342581970718279808"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, config=CONFIG_FIRST)

        orch.run(12988, 12988)

        _iid, result = writer.rows[0]
        assert result["order_ids"] == ["342581"]
