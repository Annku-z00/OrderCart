"""
tests/test_smart_pincode_disambiguation.py
==========================================

The **context-scored smart order-id engine** (task rules A–D):

* **Rule A — prefix-agnostic lengths.**  No ``startswith("3")`` / ``34`` check
  anywhere: an order id may start with any digit ``0–9``.  The labelled band is
  **6–18 digits** (6–15 = partial crop, 16–18 = full) and the minimum length is
  six digits.
* **Rule B — multilingual labels.**  English, Hindi, Tamil, Telugu and Bangla
  ``Order`` labels all anchor a short (6–9 digit) id, including the short
  ``Order:`` / ``ऑर्डर #`` / ``ஆர்டர்:`` forms and the alternate Hindi spelling
  ``आर्डर``.
* **Rule C — same-line / newline guard.**  A 6–9 digit value under a detached
  ``Order ID #`` header is rejected when the value's line carries a postal
  signal, when it is more than one newline below the label, or when a 10-digit
  mobile follows it.
* **Rule D — enhanced postal context.**  A 6-digit run is a PIN (never an id)
  when it is preceded by a PIN label (any of the five scripts), an address dash
  / comma / bracket, an Indian state / city / address keyword within 80 chars,
  or is followed by a mobile number — unless a real ``Order`` label sits on the
  exact same line before it.

Everything is pure text: no API, no OCR, no network.
"""

from __future__ import annotations

import pytest

from extractor.parser import extract_fields, find_order_id
from extractor.rules import (
    PRE_EXPANSION_ORDER_ID_LABELS,
    Rules,
)

RULES = Rules()

#: The §3.2 unlabelled fallback off (production default) / on.
UNLABELLED_RULES = Rules.from_config({"extraction": {"require_order_id_label": False}})


# ---------------------------------------------------------------------------
# Rule A — prefix-agnostic lengths (no "34" hardcoding)
# ---------------------------------------------------------------------------
class TestPrefixAgnosticLengths:
    @pytest.mark.parametrize("prefix", list("0123456789"))
    def test_six_digit_id_any_prefix(self, prefix):
        # A labelled 6-digit id is a *crop* whatever its leading digit.
        result = extract_fields(f"Order ID # {prefix}82910", "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == f"TRUNC:{prefix}82910"
        assert result["partial"] is True

    @pytest.mark.parametrize("prefix", ["1", "2", "3", "4", "5", "6", "7", "8", "9"])
    def test_sixteen_digit_id_any_prefix(self, prefix):
        # A full 16-digit id is bare last-6 whatever its leading digit.
        value = prefix + "418750906436012"  # 1 + 15 = exactly 16 digits
        result = extract_fields(f"Order ID # {value}", "", RULES)
        assert result["type"] == "OK"
        assert result["order_ids"] == [value[-6:]]
        assert result["partial"] is False

    def test_future_prefix_four_and_five_and_nine(self):
        assert extract_fields("Order ID # 482910", "", RULES)["order_id"] == "TRUNC:482910"
        assert extract_fields("Order ID # 582910", "", RULES)["order_id"] == "TRUNC:582910"
        assert extract_fields("Order ID # 982910", "", RULES)["order_id"] == "TRUNC:982910"
        assert extract_fields(
            "Order ID # 4418750906436012", "", RULES
        )["order_ids"] == ["436012"]
        assert extract_fields(
            "Order ID # 9418750906436012", "", RULES
        )["order_ids"] == ["436012"]

    def test_no_prefix_filter_in_find_order_id(self):
        for prefix in "0459":
            assert find_order_id(f"Order ID: {prefix}829101234567", RULES)[0] == (
                f"{prefix}829101234567"
            )

    def test_minimum_length_is_six_digits(self):
        # 6 digits is the floor; 5 digits is only ever a labelled ellipsis crop.
        assert extract_fields("Order ID # 482910", "", RULES)["type"] == "TRUNC"
        # A bare 5-digit number with no label is not an id at all.
        assert find_order_id("48291", RULES)[0] is None
        # Five digits next to a label is a cut-off id -> TRUNC.
        assert extract_fields("Order #12345", "", RULES)["order_id"] == "TRUNC:12345"


class TestNoPrefixHardcoding:
    """A source-level guard: the extractor must never filter on leading digits."""

    def test_extractor_source_has_no_leading_digit_filter(self):
        import inspect
        import re as _re

        from extractor import parser, rules as rules_mod

        blob = inspect.getsource(parser) + inspect.getsource(rules_mod)
        # e.g. ``startswith("3")`` / ``startswith("34")`` — the bug this engine
        # removes.  Any ``startswith`` with a digit (or "3"/"34") is forbidden.
        assert not _re.search(r"startswith\(\s*[\"']\d", blob)
        assert not _re.search(r"startswith\(\s*[\"']3[0-9]*[\"']", blob)
        for bad in ('== "34"', "== '34'", 'before != "3"'):
            assert bad not in blob, bad


# ---------------------------------------------------------------------------
# Rule B — multilingual labels (English / Hindi / Tamil / Telugu / Bangla)
# ---------------------------------------------------------------------------
class TestMultilingualLabels:
    #: ``(label, expected 6-digit value)``.
    LABELS = [
        ("Order ID #", "482910"),
        ("Order No", "482910"),
        ("Order Number", "482910"),
        ("Order:", "482910"),
        ("ऑर्डर आईडी #", "482910"),
        ("ऑर्डर #", "482910"),
        ("ऑर्डर:", "482910"),
        ("ऑर्डर संख्या", "482910"),
        ("आर्डर आईडी #", "482910"),
        ("ஆர்டர் ஐடி:", "482910"),
        ("ஆர்டர் #", "482910"),
        ("ஆர்டர்:", "482910"),
        ("ఆర్డర్ ఐడి", "482910"),
        ("ఆర్డర్ ఐడీ", "482910"),
        ("ఆర్డర్:", "482910"),
        ("ఆర్డర్ నంబర్", "482910"),
        ("অর্ডার আইডি #", "482910"),
        ("অর্ডার নং", "482910"),
        ("অর্ডার #", "482910"),
        ("অর্ডার:", "482910"),
        ("Sub-Order No", "482910"),
    ]

    @pytest.mark.parametrize("label,value", LABELS)
    def test_six_digit_multilingual(self, label, value):
        result = extract_fields(f"{label} {value}", "", RULES)
        assert result["type"] == "TRUNC", label
        assert result["order_id"] == f"TRUNC:{value}", label
        assert result["partial"] is True, label

    @pytest.mark.parametrize("label", [
        "Order ID #", "Order:", "ऑर्डर आईडी", "ऑर्डर #", "ஆர்டர் ஐடி",
        "ஆர்டர் #", "ఆర్డర్ ఐడి", "ఆర్డర్ #", "অর্ডার আইডি", "অর্ডার #",
    ])
    def test_sixteen_digit_multilingual(self, label):
        result = extract_fields(f"{label} 342343834138346752", "", RULES)
        assert result["type"] == "OK", label
        assert result["order_ids"] == ["346752"], label

    def test_new_label_forms_are_in_defaults(self):
        for lbl in [
            "Order:", "Sub-Order No", "Sub Order No",
            "ऑर्डर #", "ऑर्डर:", "ऑर्डर नं", "आर्डर आईडी", "आर्डर",
            "ஆர்டர் #", "ஆர்டர்:",
            "ఆర్డర్ ఐడీ", "ఆర్డర్ #", "ఆర్డర్:",
            "অর্ডার নং", "অর্ডার #", "অর্ডার:",
        ]:
            assert lbl in RULES.order_id_labels, lbl

    def test_production_config_carries_expanded_labels(self):
        from frameflow_config import load_config

        rules = Rules.from_config(load_config("config.yaml"))
        for lbl in ("ऑर्डर #", "Order:", "ஆர்டர் #", "ఆర్డర్ #", "অর্ডার #"):
            assert lbl in rules.order_id_labels, lbl

    def test_pre_expansion_config_migrates_to_expanded_labels(self):
        # A config that still carries the exact pre-expansion list is upgraded,
        # so a seller who never edited the labels gains the new forms.
        rules = Rules.from_config(
            {"extraction": {"order_id_labels": list(PRE_EXPANSION_ORDER_ID_LABELS)}}
        )
        assert "ऑर्डर #" in rules.order_id_labels
        assert "Order:" in rules.order_id_labels


# ---------------------------------------------------------------------------
# Google Lens pincode traps (rules C + D)
# ---------------------------------------------------------------------------
class TestPincodeTraps:
    def test_city_state_pincode_without_dash_is_rejected(self):
        # "Jaipur, 302001" / "Rajasthan 302001" — no dash, no "Pin:" word.
        for text in ["Jaipur, 302001", "Rajasthan 302001", "302001 Rajasthan"]:
            assert find_order_id(text, RULES)[0] is None, text
            result = extract_fields(text, "", RULES)
            assert result["type"] != "OK", text
            assert result["order_ids"] == [], text

    def test_bracket_pincode_is_rejected(self):
        assert find_order_id("Delivery Address (673311)", RULES)[0] is None
        assert find_order_id("(302001)", RULES)[0] is None

    @pytest.mark.parametrize("label", [
        "PIN", "Pincode:", "Pin code", "Postal", "Zip", "पिन", "பின்", "పిన్", "পিন",
    ])
    def test_multilingual_pin_labels_are_rejected(self, label):
        assert find_order_id(f"{label} 673311", RULES)[0] is None, label

    def test_pincode_followed_by_mobile_is_rejected(self):
        # Meesho prints the PIN line directly above the contact number.
        assert find_order_id("517126\n8309847092", RULES)[0] is None
        assert extract_fields(
            "Order ID #\n517126\n8309847092", "", RULES
        )["order_ids"] == []

    def test_orphan_header_before_state_pincode_is_rejected(self):
        result = extract_fields("Order ID #\nRajasthan 302001", "", RULES)
        assert result["type"] == "FAILED"
        assert result["order_ids"] == []

    def test_orphan_header_before_bare_pincode_two_lines_down_is_rejected(self):
        result = extract_fields("Order ID #\n\n517126", "", RULES)
        assert result["type"] == "FAILED"
        assert result["order_ids"] == []

    def test_orphan_header_before_kerala_dash_pincode_is_rejected(self):
        result = extract_fields("Order ID #\nKerala - 673311", "", RULES)
        assert result["type"] == "FAILED"
        assert result["order_ids"] == []

    def test_pincode_inside_address_block_is_rejected(self):
        text = "Delivery Address\nADHEENA ANEESH\nKERALA - 673311"
        assert find_order_id(text, RULES)[0] is None
        assert extract_fields(text, "", RULES)["order_ids"] == []

    def test_short_labelled_value_on_the_same_line_still_wins(self):
        # Rule C/D override: a real Order label on the exact same line before the
        # number is trusted, even inside a document that also has an address.
        text = (
            "Order No - 482910\n"
            "Delivery Address\n"
            "KINJU NIVAS, P.O NEWMAHE\n"
            "Kerala - 673311"
        )
        result = extract_fields(text, "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:482910"
        assert result["partial"] is True

    def test_short_labelled_value_same_line_amid_address_words(self):
        result = extract_fields("Order ID: 482910 Jaipur", "", RULES)
        assert result["order_id"] == "TRUNC:482910"

    def test_pin_label_between_order_label_and_value_is_rejected(self):
        # "Order ID: PIN 517126" — the number belongs to the PIN field.
        for text in ["Order ID # PIN: 517126", "Order ID: पिन 517126"]:
            assert extract_fields(text, "", RULES)["order_ids"] == [], text

    def test_genuine_short_id_in_a_chat_message_survives(self):
        # A customer typing the id with an order word but no postal signal.
        assert find_order_id("my order is 482910", RULES)[0] == "482910"
        assert extract_fields("482910", "", RULES)["order_ids"] == []

    def test_multi_order_keeps_labelled_short_id_and_drops_pincode(self):
        from extractor.parser import find_all_order_ids

        text = "Order No - 482910\nDelivery Address\nKerala - 673311"
        assert find_all_order_ids(text, RULES) == ["482910"]


# ---------------------------------------------------------------------------
# The named §3.6 list-page cap still applies (regression guard)
# ---------------------------------------------------------------------------
class TestListPageStillCapped:
    def test_many_unlabelled_ids_collapse_to_one(self):
        ids = "\n".join(f"3420026566347{i:03d}" for i in range(10))
        result = extract_fields(ids, "", UNLABELLED_RULES)
        assert result["type"] == "TRUNC"
        assert result["ambiguous"] is True
        assert result["order_ids"] == []
