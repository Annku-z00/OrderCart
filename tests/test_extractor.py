"""
tests/test_extractor.py
=======================

Unit tests for the field extractor.  These run **without** any API, OCR engine
or network access — they feed sample OCR / WhatsApp strings straight into
:func:`extractor.parser.extract_fields`.

Run with::

    pytest -v tests/test_extractor.py
"""

from __future__ import annotations

import pytest

from extractor.parser import (
    combine_text,
    extract_fields,
    find_address,
    find_name,
    find_order_id,
    is_valid_name,
)
from extractor.rules import Rules, normalize


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def rules() -> Rules:
    """Default rule set (mirrors config.yaml defaults)."""
    return Rules()


# ---------------------------------------------------------------------------
# Sample OCR payloads
# ---------------------------------------------------------------------------
MEESHO_SCREENSHOT = """
Order Confirmed
Order ID: 3418579486740656
Deliver to
Priya Sharma
12 MG Road, Near Big Bazaar
Pune, Maharashtra
411001
Total: ₹1,299.00
Thank you for shopping with Meesho
"""

ORDER_TEXT_ONLY = "hi bhai my order id is 3418579486740657 pls send fast"

NAME_ONLY_IMAGE = """
Ship to
Rahul Verma
Payment Method: Cash on Delivery
"""

ADDRESS_ONLY_IMAGE = """
Delivery Address
A-14 Sunrise Apartments, Sector 21
Kharghar, Navi Mumbai
Maharashtra 410210
"""

NOISE_ONLY = """
Meesho
Invoice
Order
Address
Total Rs. 500
"""

PRICE_AND_PHONE = """
Call me on 9876543210
Order Total: Rs. 1499
Price: ₹999.50
"""


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------
class TestNormalize:
    def test_collapses_horizontal_whitespace(self):
        assert normalize("Order   ID:\t 123") == "Order ID: 123"

    def test_preserves_newlines(self):
        out = normalize("line one\nline two")
        assert "\n" in out
        assert out == "line one\nline two"

    def test_handles_none_and_empty(self):
        assert normalize(None) == ""
        assert normalize("") == ""

    def test_unifies_crlf(self):
        assert normalize("a\r\nb\rc") == "a\nb\nc"


# ---------------------------------------------------------------------------
# Order ID
# ---------------------------------------------------------------------------
class TestFindOrderId:
    def test_16_digit_primary(self, rules):
        value, conf = find_order_id(MEESHO_SCREENSHOT, rules)
        assert value == "3418579486740656"
        assert conf == pytest.approx(0.95)

    def test_16_digit_from_text_message(self, rules):
        value, conf = find_order_id(ORDER_TEXT_ONLY, rules)
        assert value == "3418579486740657"
        assert conf == pytest.approx(0.95)

    def test_rejects_phone_number(self, rules):
        value, _ = find_order_id("Call me on 9876543210", rules)
        assert value is None

    def test_rejects_pin_code(self, rules):
        value, _ = find_order_id(NAME_ONLY_IMAGE, rules)
        assert value is None

    def test_rejects_price(self, rules):
        value, _ = find_order_id(PRICE_AND_PHONE, rules)
        assert value is None

    def test_generic_number_near_label(self, rules):
        # 15 digits is above the truncated range (5-14) -> treated as a full
        # generic id sitting next to a label (confidence 0.90).
        value, conf = find_order_id("Order No: 123456789012345", rules)
        assert value == "123456789012345"
        assert conf == pytest.approx(0.90)

    def test_short_number_near_label_is_a_full_id(self, rules):
        # 12 digits that do NOT start with "34" is a full id, not a crop.
        # (A 34-prefixed 10–15 digit run is the cropped case — see
        # test_partial_id_near_label_without_ellipsis.)
        value, conf = find_order_id("Order No: 123456789012", rules)
        assert value == "123456789012"
        assert conf == pytest.approx(0.90)

    def test_short_number_without_label_low_confidence(self, rules):
        # The same digits away from any order label still resolve, just at the
        # lower "found somewhere" confidence.
        value, conf = find_order_id("ref 123456789012", rules)
        assert value == "123456789012"
        assert conf == pytest.approx(0.80)

    def test_generic_number_without_label_gets_lower_confidence(self, rules):
        value, conf = find_order_id("ref 123456789012345", rules)
        assert value == "123456789012345"
        assert conf == pytest.approx(0.80)

    def test_empty_input(self, rules):
        assert find_order_id("", rules) == (None, 0.0)


# ---------------------------------------------------------------------------
# Meesho Sub-Order IDs ("Sub OrderID … 342343834138346752_1")
# ---------------------------------------------------------------------------
class TestSubOrderId:
    """Meesho appends ``_N`` to the base id for split shipments.

    ``_`` is a word character, so ``\\b\\d{16}\\b`` (and even
    ``\\b\\d{10,20}\\b``) never matched ``342343834138346752_1`` — the
    right-hand boundary was swallowed.  The new pattern keeps the optional
    ``_N`` suffix *outside* capture Group 1, so only the base id is returned.
    """

    def test_sub_order_id_with_label(self, rules):
        value, conf = find_order_id(
            "Sub OrderID\nCOPY\n342343834138346752_1\nPayment Mode Cash on Delivery",
            rules,
        )
        assert value == "342343834138346752"
        assert conf > 0.9

    def test_sub_order_id_without_label(self, rules):
        value, conf = find_order_id("342343834138346752_1", rules)
        assert value == "342343834138346752"
        assert conf > 0.9

    def test_sub_order_id_spaced_label_strips_suffix(self, rules):
        value, _ = find_order_id("Sub Order ID: 342343834138346752_7", rules)
        assert value == "342343834138346752"
        assert "_" not in value

    def test_multiple_sub_orders_return_single_base(self, rules):
        text = (
            "Sub OrderID\n"
            "342343834138346752_1\n"
            "342343834138346752_2\n"
            "342343834138346752_3"
        )
        value, conf = find_order_id(text, rules)
        # Only ONE base id, suffixes stripped — never ``…_2`` / ``…_3``.
        assert value == "342343834138346752"
        assert "_" not in value
        assert conf > 0.9

    def test_sub_order_labels_in_defaults(self, rules):
        assert "Sub OrderID" in rules.order_id_labels
        assert "Sub Order ID" in rules.order_id_labels

    def test_plain_16_digit_still_confident(self, rules):
        value, conf = find_order_id("Order ID: 3418579486740656", rules)
        assert value == "3418579486740656"
        assert conf == pytest.approx(0.95)

    def test_legacy_config_migrates_to_sub_order_pattern(self):
        # config.yaml / frameflow_config pin the old regex + label list; an
        # *untouched* legacy config must be transparently upgraded so the live
        # pipeline can actually see sub-order ids.
        rules = Rules.from_config(
            {
                "extraction": {
                    "order_id_regex_primary": r"\b\d{16}\b",
                    "order_id_labels": [
                        "Order ID", "Order No", "Order Number", "OrderId", "Order #",
                    ],
                }
            }
        )
        value, _ = find_order_id("Sub OrderID\n342343834138346752_1", rules)
        assert value == "342343834138346752"
        assert "Sub OrderID" in rules.order_id_labels


# ---------------------------------------------------------------------------
# Customer name
# ---------------------------------------------------------------------------
class TestFindName:
    def test_name_on_next_line_after_label(self, rules):
        value, _ = find_name(NAME_ONLY_IMAGE, rules)
        assert value == "Rahul Verma"

    def test_inline_name(self, rules):
        value, _ = find_name("Customer Name: Anita Desai", rules)
        assert value == "Anita Desai"

    def test_ignores_boilerplate(self, rules):
        value, _ = find_name(NOISE_ONLY, rules)
        assert value is None

    def test_name_must_not_contain_digits(self, rules):
        assert not is_valid_name("Priya 123", rules)

    def test_name_max_four_words(self, rules):
        assert not is_valid_name("one two three four five", rules)

    def test_valid_simple_name(self, rules):
        assert is_valid_name("Priya Sharma", rules)

    def test_name_from_meesho_screenshot(self, rules):
        value, _ = find_name(MEESHO_SCREENSHOT, rules)
        # "Order ID: 3418579486740656" line is skipped (has digits);
        # the "Deliver to" label yields "Priya Sharma".
        assert value == "Priya Sharma"


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------
class TestFindAddress:
    def test_address_block_multi_line(self, rules):
        value, _ = find_address(ADDRESS_ONLY_IMAGE, rules)
        assert value is not None
        assert "Sunrise Apartments" in value
        assert "410210" in value
        # Newlines are preserved in the raw finding.
        assert "\n" in value

    def test_address_stops_at_pin_code(self, rules):
        text = "Address\nSome Street\nMumbai 400001\nExtra Info After Pin"
        value, _ = find_address(text, rules)
        assert "Extra Info" not in value

    def test_address_stops_at_blank_line(self, rules):
        text = "Address\nLine one\nLine two\n\nSome other section"
        value, _ = find_address(text, rules)
        assert "other section" not in value

    def test_no_address_returns_none(self, rules):
        value, _ = find_address(NOISE_ONLY, rules)
        assert value is None


# ---------------------------------------------------------------------------
# Full priority chain
# ---------------------------------------------------------------------------
class TestExtractFields:
    def test_priority_order_id_wins(self, rules):
        result = extract_fields(MEESHO_SCREENSHOT, "", rules)
        assert result["type"] == "OK"
        # §3 behaviour change: the output value is the last 6 digits of the
        # order id (the seller's manual Notepad workflow), not the full id.
        assert result["order_id"] == "740656"
        assert result["order_ids"] == ["740656"]
        assert result["name"] == ""
        assert result["address"] == ""
        assert result["source"] == "image"

    def test_text_order_id_from_whatsapp(self, rules):
        result = extract_fields("", ORDER_TEXT_ONLY, rules)
        assert result["type"] == "OK"
        # §3: last-6 of 3418579486740657.
        assert result["order_id"] == "740657"
        assert result["source"] == "text"

    def test_fallback_to_name(self, rules):
        result = extract_fields(NAME_ONLY_IMAGE, "", rules)
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "Rahul Verma"
        assert result["order_id"] == ""

    def test_fallback_to_address(self, rules):
        result = extract_fields(ADDRESS_ONLY_IMAGE, "", rules)
        assert result["type"] == "FALLBACK_ADDRESS"
        assert "Sunrise Apartments" in result["address"]

    def test_failed_when_nothing_found(self, rules):
        result = extract_fields(NOISE_ONLY, "", rules)
        assert result["type"] == "FAILED"
        assert result["confidence"] == pytest.approx(0.0)

    def test_image_text_preferred_over_whatsapp_text(self, rules):
        # Image has an order id, WhatsApp text has another -> image wins.
        result = extract_fields("Order ID: 3418579486740001", "3418579486740002", rules)
        # §3: last-6 of 3418579486740001, still sourced from the image.
        assert result["order_id"] == "740001"
        assert result["source"] == "image"

    def test_confidence_threshold_flagging(self, rules):
        # An address-only fallback returns 0.72 -> below the 0.75 review line.
        result = extract_fields(ADDRESS_ONLY_IMAGE, "", rules)
        assert result["confidence"] < 0.75

    def test_order_id_confidence_above_review_line(self, rules):
        result = extract_fields(MEESHO_SCREENSHOT, "", rules)
        assert result["confidence"] >= 0.75


# ---------------------------------------------------------------------------
# combine_text()
# ---------------------------------------------------------------------------
class TestCombineText:
    def test_joins_ocr_and_whatsapp(self):
        out = combine_text("img text", "wa text")
        assert "img text" in out
        assert "wa text" in out

    def test_handles_empty_parts(self):
        assert combine_text("", "only") == "only"
        assert combine_text("only", "") == "only"
        assert combine_text("", "") == ""


# ---------------------------------------------------------------------------
# Config-driven Rules
# ---------------------------------------------------------------------------
class TestRulesFromConfig:
    def test_from_config_overrides(self):
        cfg = {
            "extraction": {
                "order_id_regex_primary": r"\b\d{12}\b",
                "ignore_words": ["TestWord"],
                "address_max_chars": 100,
            }
        }
        rules = Rules.from_config(cfg)
        assert rules.address_max_chars == 100
        assert "TestWord" in rules.ignore_words
        # The overridden 12-digit pattern now matches.
        value, _ = find_order_id("Order ID: 341857948674", rules)
        assert value == "341857948674"


# ---------------------------------------------------------------------------
# Currency word-boundary regression (Bug 4 — "rs" inside "Orders")
# ---------------------------------------------------------------------------
class TestOrderIdCurrencyBoundary:
    """``has_currency_nearby`` must only match currency *words* as whole words.

    ``"Orders:"``/``"Sellers:"``/``"Buyers:"`` all contain the letters ``rs``;
    a naive substring check rejected their order ids.  Currency symbols stay
    literal and currency words must be bounded.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Orders: 3418579486740656", "3418579486740656"),
            ("Sellers: 342022570640639296", "342022570640639296"),
            ("Buyers: 1234567890123456", "1234567890123456"),
        ],
    )
    def test_words_containing_rs_do_not_reject_ids(self, rules, text, expected):
        value, _ = find_order_id(text, rules)
        assert value == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Total Rs. 500",
            "Cash ₹191",
            "USD 100",
            "Total INR 750",
            "Amount EUR 20",
        ],
    )
    def test_price_lines_do_not_yield_order_ids(self, rules, text):
        assert find_order_id(text, rules)[0] is None

    @pytest.mark.parametrize(
        "text",
        [
            "Total Rs. 3418579486740656",
            "Cash ₹3418579486740656",
            "USD 3418579486740656",
            "Please pay INR 3418579486740656",
            "Total EUR 3418579486740656",
        ],
    )
    def test_currency_words_reject_nearby_16_digit_numbers(self, rules, text):
        assert find_order_id(text, rules)[0] is None

    def test_symbols_and_words_are_split(self):
        from extractor.rules import CURRENCY_SYMBOLS, CURRENCY_WORDS

        assert "₹" in CURRENCY_SYMBOLS
        assert "rs" in CURRENCY_WORDS
        assert "₹" not in CURRENCY_WORDS

    def test_has_currency_nearby_whole_word(self, rules):
        # A real currency word is detected …
        assert rules.has_currency_nearby("Rs 1234", 3, 7) is True
        # … but "rs" hidden inside "Orders" is not.
        assert rules.has_currency_nearby("Orders 1234", 7, 11) is False


# ---------------------------------------------------------------------------
# Meesho production screenshot patterns (Phase 5)
# ---------------------------------------------------------------------------
class TestMeeshoProductionPatterns:
    """The real customer screenshots that motivated the Phase 5 rules."""

    def test_order_hash_label_sixteen_digits(self, rules):
        value, conf = find_order_id("Order #3418579486740656", rules)
        assert value == "3418579486740656"
        assert conf == pytest.approx(0.95)

    def test_order_hash_without_space_is_a_full_id(self, rules):
        # The "Order#" label is recognised; a 15-digit run with no ellipsis is
        # a complete id (order ids may start with any digit).
        value, conf = find_order_id("Order#341895582462480", rules)
        assert value == "341895582462480"
        assert conf == pytest.approx(0.90)

    def test_order_apostrophe_label_next_line(self, rules):
        value, conf = find_order_id("Order I'd :\n342216136358560832", rules)
        assert value == "342216136358560832"
        assert conf == pytest.approx(0.90)

    def test_order_apostrophe_curly_variant(self, rules):
        value, _ = find_order_id("Order I’d :\n342216136358560832", rules)
        assert value == "342216136358560832"

    def test_ellipsis_crop_with_thumbnail_and_phone(self, rules):
        # The seller's product-thumbnail phone number (7014965655) must be
        # ignored; the id is truncated, not complete.
        text = "Order #341972763328...\nRoyal Photo Frame 5*7\n7014965655"
        value, conf = find_order_id(text, rules)
        assert value == "TRUNC:341972763328"
        assert conf == pytest.approx(0.70)

    def test_layout_a_delivery_address_name_and_address(self, rules):
        text = (
            "Delivery Address CHANGE\n"
            "ADHEENA ANEESH\n"
            "KINJU NIVAS, P.O NEWMAHE, THALASSERY\n"
            "Kerala - 673311\n"
            "7994736612"
        )
        name, _ = find_name(text, rules)
        assert name == "ADHEENA ANEESH"
        address, _ = find_address(text, rules)
        assert "KINJU NIVAS" in address

    def test_layout_a_lowercase_name(self, rules):
        text = (
            "Delivery Address\n"
            "mayank\n"
            "Aya Nagar Pahari Ambedkar Colony Gali number 4\n"
            "9971639425\n"
            "मीशो महा सेवर"
        )
        name, _ = find_name(text, rules)
        assert name == "mayank"
        address, _ = find_address(text, rules)
        assert "Aya Nagar" in address

    def test_layout_b_deliver_to_inline_name(self, rules):
        text = (
            "Order confirmed\n"
            "Deliver to Akesh Vasant Valvi\n"
            "Mahadev kirana, Bas stend, Navapur, Maharashtra - 425418\n"
            "Contact Number - 9309983868"
        )
        name, _ = find_name(text, rules)
        assert name == "Akesh Vasant Valvi"
        address, _ = find_address(text, rules)
        assert "Mahadev kirana" in address

    def test_layout_c_sold_to_never_supplier(self, rules):
        text = "Order ID 341875090643601280   Sold to Manashi\nSupplier : KK ROYAL"
        name, _ = find_name(text, rules)
        assert name == "Manashi"
        assert name != "KK ROYAL"

    def test_name_beats_truncated_id(self, rules):
        # A cut-off id must not shadow a perfectly good customer name.
        result = extract_fields(
            "Order #341972763328...\nDelivery Address\nmayank", "", rules
        )
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "mayank"
        assert result["order_id"] == ""

    def test_truncated_id_used_only_when_nothing_else(self, rules):
        result = extract_fields("Order #341972763328...", "", rules)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:341972763328"


# ---------------------------------------------------------------------------
# Context-aware 6-digit PINs and the universal full-order-id rule
# ---------------------------------------------------------------------------
class TestSixDigitAndPrefixRules:
    """A 6-digit number is an order id unless it is clearly a postal PIN.

    Order ids may start with **any** digit: only a trailing ellipsis or fewer
    than six digits makes an id truncated.  The old ``34``-prefix crop rule is
    gone.
    """

    def test_six_digit_after_order_hash_is_order_id(self, rules):
        value, conf = find_order_id("Order #482910", rules)
        assert value == "482910"
        assert conf == pytest.approx(0.90)

    def test_six_digit_after_order_label_is_order_id(self, rules):
        value, conf = find_order_id("Order ID: 583920", rules)
        assert value == "583920"
        assert conf == pytest.approx(0.90)

    def test_six_digit_after_order_label_with_dash_is_order_id(self, rules):
        # The label wins over the postal dash heuristic.
        value, _ = find_order_id("Order No - 482910", rules)
        assert value == "482910"

    def test_standalone_six_digit_message_is_order_id(self, rules):
        # A customer who simply types the id with no label.
        value, conf = find_order_id("482910", rules)
        assert value == "482910"
        assert conf == pytest.approx(0.80)

    def test_six_digit_in_delivery_address_block_is_pin(self, rules):
        text = (
            "Delivery Address\n"
            "KINJU NIVAS, P.O NEWMAHE, THALASSERY\n"
            "Kerala - 673311"
        )
        assert find_order_id(text, rules)[0] is None
        result = extract_fields(text, "", rules)
        assert result["order_id"] == ""
        assert result["type"] != "OK"

    def test_six_digit_after_pin_label_is_blocked(self, rules):
        assert find_order_id("PIN 673311", rules)[0] is None
        assert find_order_id("Pincode: 673311", rules)[0] is None
        assert find_order_id("पिन 673311", rules)[0] is None

    def test_six_digit_after_city_dash_is_blocked(self, rules):
        assert find_order_id("Maharashtra - 425418", rules)[0] is None
        assert find_order_id("- 673311", rules)[0] is None

    def test_six_digit_inside_plain_address_block_is_blocked(self, rules):
        text = "Address\n12 MG Road, Pune\n411001"
        assert find_order_id(text, rules)[0] is None

    def test_twelve_digit_starting_with_five_is_full_id(self, rules):
        value, conf = find_order_id("Order #541873202491", rules)
        assert value == "541873202491"
        assert conf == pytest.approx(0.90)

    def test_twelve_digit_without_label_is_full_id(self, rules):
        value, conf = find_order_id("my order is 541873202491", rules)
        assert value == "541873202491"
        assert conf == pytest.approx(0.80)

    def test_six_digit_with_ellipsis_is_truncated(self, rules):
        # An order id cut off by an ellipsis is TRUNC even at 6 digits.
        value, conf = find_order_id("Order #482910...", rules)
        assert value == "TRUNC:482910"
        assert conf == pytest.approx(0.70)

    def test_five_digit_near_label_is_truncated(self, rules):
        value, conf = find_order_id("Order #12345", rules)
        assert value == "TRUNC:12345"
        assert conf == pytest.approx(0.65)
