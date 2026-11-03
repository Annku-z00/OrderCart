"""
tests/test_rules.py
===================

Unit tests for the §3.4 **hard rejection filters** on
:class:`extractor.rules.Rules` — one positive and one negative fixture per
filter, plus the config toggles.

The filters are consulted by the §3.2 unlabelled scan (and, for phone / price,
by the §3.1 labelled path as well).  Every filter has a ``reject_patterns``
toggle in ``config.yaml -> extraction.reject_patterns``.
"""

from __future__ import annotations

import re

import pytest

from extractor.rules import (
    DEFAULT_REJECT_PATTERNS,
    REJECT_PATTERNS,
    Rules,
)

RULES = Rules()


def _runs(text: str):
    """Yield ``(start, end, digits)`` for every digit run in ``text``."""
    for match in re.finditer(r"\d+", text):
        yield match.start(), match.end(), match.group()


def _rejected_numbers(text: str, rules: Rules = RULES):
    """Return the digit runs (6+ digits) that the §3.4 filters reject."""
    return [
        value
        for start, end, value in _runs(text)
        if len(value) >= 6 and rules.is_rejected_candidate(text, start, end, value)
    ]


def _accepted_numbers(text: str, rules: Rules = RULES):
    """Return the digit runs (6+ digits) that survive the §3.4 filters."""
    return [
        value
        for start, end, value in _runs(text)
        if len(value) >= 6 and not rules.is_rejected_candidate(text, start, end, value)
    ]


# ---------------------------------------------------------------------------
# The compiled pattern table exists and covers every §3.4 family
# ---------------------------------------------------------------------------
class TestRejectPatternTable:
    def test_all_families_present(self):
        for name in ("phone", "date", "product", "price"):
            assert name in REJECT_PATTERNS
            assert isinstance(REJECT_PATTERNS[name], re.Pattern)

    def test_default_toggles_all_on(self):
        for name in ("phone", "pin", "price", "date", "product", "eta", "concatenation"):
            assert DEFAULT_REJECT_PATTERNS[name] is True


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------
class TestPhoneFilter:
    @pytest.mark.parametrize("text", [
        "Call me on 9988776655",
        "Contact +91 9988776655",
        "WhatsApp 919988776655",
    ])
    def test_phone_rejected(self, text):
        assert _rejected_numbers(text)

    def test_order_id_not_phone(self):
        # 3418579486740656 is not a 10-digit Indian mobile -> not rejected.
        assert _accepted_numbers("Order ID: 3418579486740656")


# ---------------------------------------------------------------------------
# PIN (context sensitive)
# ---------------------------------------------------------------------------
class TestPinFilter:
    @pytest.mark.parametrize("text", [
        "PIN 673311",
        "Pincode: 411001",
        "Kerala - 673311",
    ])
    def test_pin_rejected(self, text):
        assert _rejected_numbers(text)

    def test_labelled_six_digit_is_not_a_pin(self):
        # A labelled 6-digit id is trusted, even though it looks like a PIN.
        assert _accepted_numbers("Order #482910")


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------
class TestPriceFilter:
    @pytest.mark.parametrize("text", [
        "Price: ₹3418579486740656",
        "Total Rs. 3418579486740656",
        "Amount INR 3418579486740656",
    ])
    def test_price_rejected(self, text):
        assert _rejected_numbers(text)

    def test_bare_number_is_not_a_price(self):
        assert _accepted_numbers("Order ref 3418579486740656")


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------
class TestDateFilter:
    @pytest.mark.parametrize("text", [
        "Invoice date 15102026",
        "Batch 20261015",
        "Order 26102026",
    ])
    def test_date_rejected(self, text):
        assert _rejected_numbers(text)

    def test_compact_date_forms_detected(self):
        # The §3.4 date filter recognises both compact orderings.
        assert RULES.looks_like_date("15102026") is True
        assert RULES.looks_like_date("20261015") is True

    def test_seventeen_digit_id_is_not_a_date(self):
        assert _accepted_numbers("Order ID: 34221613635856083")


# ---------------------------------------------------------------------------
# Product / SKU
# ---------------------------------------------------------------------------
class TestProductFilter:
    @pytest.mark.parametrize("text", [
        "SKU 3418579486740656",
        "Product Code: 3418579486740656",
        "Item Code 3418579486740656",
        "HSN 3418579486740656",
    ])
    def test_product_rejected(self, text):
        assert _rejected_numbers(text)

    def test_plain_number_is_not_a_product(self):
        assert _accepted_numbers("ref 3418579486740656")


# ---------------------------------------------------------------------------
# ETA / delivery
# ---------------------------------------------------------------------------
class TestEtaFilter:
    @pytest.mark.parametrize("text", [
        "ETA 3418579486740656",
        "Expected 3418579486740656",
        "Arriving 3418579486740656",
        "Delivery 3418579486740656",
    ])
    def test_eta_rejected(self, text):
        assert _rejected_numbers(text)

    def test_delivery_address_is_not_an_eta_label(self):
        # "Delivery Address" is a customer label, not an ETA label.
        assert _accepted_numbers("Delivery Address 3418579486740656")


# ---------------------------------------------------------------------------
# Concatenation
# ---------------------------------------------------------------------------
class TestConcatenationFilter:
    def test_containing_run_is_dropped(self):
        from extractor.parser import _apply_concatenation_guard

        candidates = [
            {"value": "3418579486740656", "conf": 0.9, "source": "image"},
            {"value": "34185794867406561", "conf": 0.9, "source": "image"},
        ]
        kept = [c["value"] for c in _apply_concatenation_guard(candidates, RULES)]
        assert kept == ["3418579486740656"]

    def test_independent_runs_survive(self):
        from extractor.parser import _apply_concatenation_guard

        candidates = [
            {"value": "1111222233334444", "conf": 0.9, "source": "image"},
            {"value": "5555666677778888", "conf": 0.9, "source": "image"},
        ]
        kept = [c["value"] for c in _apply_concatenation_guard(candidates, RULES)]
        assert kept == ["1111222233334444", "5555666677778888"]


# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------
class TestRejectToggles:
    def test_date_filter_can_be_disabled(self):
        rules = Rules.from_config(
            {"extraction": {"reject_patterns": {"date": False}}}
        )
        assert rules.reject_patterns["date"] is False
        # "Batch 20261015" is no longer rejected as a date.
        assert not rules.is_rejected_candidate("Batch 20261015", 6, 14, "20261015")

    def test_eta_filter_can_be_disabled(self):
        rules = Rules.from_config(
            {"extraction": {"reject_patterns": {"eta": False}}}
        )
        assert rules.reject_patterns["eta"] is False
        assert not rules.is_rejected_candidate(
            "ETA 3418579486740656", 4, 20, "3418579486740656"
        )


# ---------------------------------------------------------------------------
# Labelled trust
# ---------------------------------------------------------------------------
class TestLabelledTrust:
    def test_labelled_flag_suppresses_pin_and_date(self):
        text = "Order ID: 482910"
        assert RULES.is_rejected_candidate(text, 10, 16, "482910", labelled=True) is False

    def test_labelled_flag_still_rejects_phone(self):
        text = "Order ID: 9988776655"
        assert RULES.is_rejected_candidate(text, 10, 20, "9988776655", labelled=True) is True
