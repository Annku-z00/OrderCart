"""
tests/test_parser.py
====================

Regression tests for the **§3 extraction pipeline** in
:mod:`extractor.parser`:

* §3.1 a labelled order id is truncated to its last ``order_id_truncate_length``
  digits (6 by default) — the seller's manual Notepad workflow;
* §3.2 an unlabelled Meesho id (contiguous 16–18 digit run in the top third) is
  a fallback for screenshots whose header was not OCR-readable;
* §3.4 hard rejection filters (PIN / phone / price / date / product / ETA and
  digit-run concatenation) — see also ``tests/test_rules.py``;
* §3.6 the list-page cap: more than a handful of candidates collapses to a
  single ``TRUNC:`` row and is logged as ``event=ambiguous_order_id``;
* default single-value output vs the opt-in ``multi_order_output`` mode.

Everything is offline: the parser is pure text, and the one orchestrator test
uses the shared fake API / OCR from :mod:`tests.test_phase3`.

NOTE: :data:`MEESHO_12988_OCR` is the **real** Google Lens OCR text of the
list screenshot attached to chat 12988
(``assets/12988/false_66645292789868@lid_ACF21F5F….jpg``, captured with
``chrome-lens-py``), stored verbatim.  The historical ``logs/12988_raw.txt``
capture for that chat is empty (``status: FAILED``) and the constant was
previously a *reconstruction* stitched from several screenshots; it is now a
single genuine capture, so the expectations below were re-derived from the real
text: the top-most id is ``3420026566347011`` (last-6 ``347011``) and the footer
junk is Notepad's status bar, not the stitched ``517126`` / ``754217`` values.
"""

from __future__ import annotations

import logging

import pytest

from extractor.parser import (
    _apply_concatenation_guard,
    extract_fields,
    extract_fields_multi,
    truncate_order_id,
)
from extractor.rules import Rules

from tests.test_phase3 import (
    CONFIG,
    FakeAPI,
    FakeOCR,
    RecordingWriter,
    build_orchestrator,
    make_message,
)

RULES = Rules()

#: The unlabelled §3.2 fallback is off by default (``require_order_id_label``);
#: these tests exercise that fallback explicitly.
UNLABELLED_RULES = Rules.from_config(
    {"extraction": {"require_order_id_label": False}}
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#: REAL chat-12988 list-screenshot OCR (Google Lens capture, stored verbatim;
#: see the module doc).  The top third holds the genuine 34… ids; the Notepad
#: status bar / line numbers in the footer hold digit noise that must never
#: become an order id.  The same text is mirrored in
#: ``tests/fixtures/ocr/real_12988_list.txt``.
MEESHO_12988_OCR = "\n".join([
    'D:\\excl\\Oct\\15-10-2026.txt- Notepad++',
    'File Edit Search View',
    'Encoding',
    'Language',
    'Settings Tools',
    'Macro Run',
    'Plugins',
    'Window ?',
    '15-10-2026.txt',
    '1-10-2026.txt',
    'UN',
    '2-10-2026.txt',
    '3-10-2026.txt',
    '12328',
    '3420026566347011',
    '12329',
    '12330',
    '341999308084544',
    '12331',
    '342008003755738432',
    '12332',
    '342008010858497088',
    '12333',
    '342006564139918464',
    '12334',
    '342006665825863',
    '12335',
    '342005019294532928',
    '12336',
    '342012665492522880',
    '12337',
    '3419758877126504',
    '12338',
    '342027385628820736',
    '12339',
    '3419609570055146',
    '12340',
    '342087259994548160',
    '12341',
    '3420013298145700',
    '12342',
    '3419573353002691',
    '12343',
    '341964662086657984',
    '12344',
    '342127142131705856',
    '12345',
    '341981675817731136',
    '12346',
    '341297754877494528',
    '12347',
    '342000784904906688',
    '12348',
    '341987186874869696',
    '12349',
    '341875090643601280',
    '12350',
    '342022570640639296',
    '12351',
    '12352',
    '12353',
    '12354',
    '12355',
    'Normal text file',
    'length: 211,419 lines: 12,362',
    'Ln: 12,351 Col: 1 Pos: 211,398',
    'Windows (CR LF) UTF-8',
    '28°C Sunny',
    'ENG 9:44 AM',
    'US 10/15/2026',
    'X',
    '▼X',
    'INS',
    'Type here to search',
    '9',
    'Ps',
])

#: Status-bar / menu junk in the real list capture that must never become the
#: emitted value.  (The earlier reconstruction stitched several screenshots, so
#: its junk list — 517126 / 754217 / 112220 / 3418579486740656517 — does not
#: occur in this single genuine Notepad capture; Notepad's own status bar is the
#: real noise.)
GARBAGE_12988 = [
    "211,419", "12,362", "12,351", "211,398", "9:44 AM", "10/15/2026",
]

#: The top-most genuine id in the fixture (``3420026566347011``) and its
#: expected last-6 value.
EXPECTED_12988 = "347011"


def _value(result: dict) -> str:
    """Return the emitted value with any ``TRUNC:`` prefix stripped."""
    raw = str(result.get("order_id") or "")
    return raw.split(":", 1)[1] if raw.upper().startswith("TRUNC:") else raw


# ---------------------------------------------------------------------------
# §6 test_12988_real_ocr
# ---------------------------------------------------------------------------
class TestReal12988Screenshot:
    def test_12988_real_ocr(self):
        # The list page has no order label, so it is the unlabelled §3.2 path.
        result = extract_fields(MEESHO_12988_OCR, "", UNLABELLED_RULES)

        # Exactly one value is emitted — never 32 comma-joined numbers.
        assert result["order_ids"] in ([], [_value(result)])
        # It is the last-6 of the top-most 16–18 digit run.
        assert _value(result) == EXPECTED_12988

        # A >3-candidate list page collapses to a single TRUNC row (§3.6).
        assert result["type"] == "TRUNC"
        assert result["order_id"] == f"TRUNC:{EXPECTED_12988}"
        assert result["order_ids"] == []
        assert result["ambiguous"] is True

        # None of the garbage values survive in the emitted value.
        for bad in GARBAGE_12988:
            assert bad not in result["order_id"]
        assert "," not in _value(result)

    def test_12988_requires_unlabelled_path(self):
        # With the seller's default (label required) the same OCR yields nothing.
        result = extract_fields(MEESHO_12988_OCR, "", RULES)
        assert result["type"] == "FAILED"
        assert result["order_ids"] == []

    def test_12988_image_matches_real_log_garbage(self):
        # A regression guard: the raw fixture really does contain the junk.
        for bad in GARBAGE_12988:
            assert bad in MEESHO_12988_OCR


# ---------------------------------------------------------------------------
# §6 test_labelled_order_id_truncates  (+ every regional label)
# ---------------------------------------------------------------------------
REGIONAL_LABELS = [
    "Order ID", "Order Id", "Order #", "Order No", "Order Number",
    "ऑर्डर आईडी", "ऑर्डर नंबर",
    "ஆர்டர் ஐடி", "ஆர்டர் எண்",
    "ఆర్డర్ ఐడి", "ఆర్డర్ నంబర్",
    "অর্ডার আইডি", "অর্ডার নম্বর",
]


class TestLabelledTruncation:
    @pytest.mark.parametrize("label", REGIONAL_LABELS)
    def test_labelled_order_id_truncates(self, label):
        result = extract_fields(f"{label}: 3418579486740656", "", RULES)
        assert result["type"] == "OK"
        assert result["order_ids"] == ["740656"]
        assert result["order_id"] == "740656"

    def test_next_line_label_still_truncates(self):
        result = extract_fields("Order ID\n3418579486740656", "", RULES)
        assert result["order_id"] == "740656"

    def test_six_digit_labelled_id_is_partial(self):
        # §3.1: a labelled 6-digit value is a *crop*, not a PIN.
        result = extract_fields("Order #482910", "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:482910"
        assert result["partial"] is True

    def test_truncate_length_is_config_driven(self):
        rules = Rules.from_config({"extraction": {"order_id_truncate_length": 4}})
        assert truncate_order_id("3418579486740656", rules) == "0656"
        result = extract_fields("Order ID: 3418579486740656", "", rules)
        assert result["order_id"] == "0656"


# ---------------------------------------------------------------------------
# §6 test_concatenation_guard
# ---------------------------------------------------------------------------
class TestConcatenationGuard:
    def test_concatenation_guard(self):
        # "id SPACE pin" must never be fused into one super-id (unlabelled path).
        result = extract_fields("3418579486740656 517126", "", UNLABELLED_RULES)
        assert _value(result) != "3418579486740656517"
        assert _value(result) == "740656"

    def test_longer_containing_run_is_dropped(self):
        candidates = [
            {"value": "3418579486740656", "conf": 0.9, "source": "image"},
            {"value": "34185794867406561", "conf": 0.9, "source": "image"},
        ]
        kept = [c["value"] for c in _apply_concatenation_guard(candidates, RULES)]
        assert kept == ["3418579486740656"]

    def test_nineteen_digit_concatenation_is_not_an_id(self):
        result = extract_fields("3418579486740656517", "", RULES)
        assert result["order_ids"] == []
        assert "3418579486740656517" not in result["order_id"]


# ---------------------------------------------------------------------------
# §6 test_pin_rejected / test_price_date_phone_rejected
# ---------------------------------------------------------------------------
class TestRejections:
    def test_pin_rejected(self):
        text = "Meesho Order\nDeliver to\n12 MG Road\nPune - 411001\nPIN 517126"
        result = extract_fields(text, "", UNLABELLED_RULES)
        assert result["order_ids"] == []
        assert "517126" not in result["order_id"]

    @pytest.mark.parametrize("text", [
        "Price: ₹3418579486740656",
        "Total Rs. 3418579486740656",
        "Amount INR 3418579486740656",
        "Order placed on 15/10/2026",
        "Invoice date 15102026",
        "Batch 20261015",
        "Call me on 9988776655",
        "Contact +91 9988776655",
        "SKU 3418579486740656",
        "Product Code: 3418579486740656",
        "Product ID: 3418579486740656",
        "Delivery ETA 3418579486740656",
    ])
    def test_price_date_phone_product_eta_rejected(self, text):
        # §3.4 filters run on the unlabelled path (the seller's default has no
        # unlabelled path at all, so nothing is ever emitted here either).
        assert extract_fields(text, "", UNLABELLED_RULES)["order_ids"] == []
        assert extract_fields(text, "", RULES)["order_ids"] == []

    def test_labelled_id_still_wins_over_product_heuristic(self):
        text = "Product Code: 3418579486740656\nOrder ID: 1111222233334444"
        result = extract_fields(text, "", RULES)
        assert result["order_ids"] == ["334444"]


# ---------------------------------------------------------------------------
# §6 fallback chain
# ---------------------------------------------------------------------------
class TestFallbackChain:
    def test_name_fallback(self):
        result = extract_fields("Customer Name: प्रिया शर्मा\nTotal Rs. 500", "", RULES)
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "प्रिया शर्मा"          # Unicode preserved
        assert result["order_id"] == ""
        assert result["order_ids"] == []

    def test_regional_name_label_preserves_unicode(self):
        result = extract_fields("ग्राहक: राजेश कुमार", "", RULES)
        assert result["type"] == "FALLBACK_NAME"
        assert result["name"] == "राजेश कुमार"

    def test_address_fallback(self):
        text = "पता:\n12 MG Road, Pune\nMaharashtra 411001"
        result = extract_fields(text, "", RULES)
        assert result["type"] == "FALLBACK_ADDRESS"
        assert "MG Road" in result["address"]
        assert result["order_id"] == ""

    def test_trunc_fallback(self):
        result = extract_fields("Order #1234...", "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:1234"

    def test_failed_fallback(self):
        result = extract_fields("", "", RULES)
        assert result["type"] == "FAILED"
        assert result["order_id"] == ""
        assert result["order_ids"] == []


# ---------------------------------------------------------------------------
# §6 test_list_page_caps_at_one (+ orchestrator WARNING)
# ---------------------------------------------------------------------------
class TestListPageCap:
    def test_list_page_caps_at_one(self, tmp_path, caplog):
        # Parser level: more than list_page_max_candidates -> one TRUNC value.
        # A list page carries no order label, so the unlabelled path is on.
        result = extract_fields(MEESHO_12988_OCR, "", UNLABELLED_RULES)
        assert result["type"] == "TRUNC"
        assert result["order_ids"] == []
        assert result["ambiguous"] is True
        assert result["ambiguous_candidates"] > UNLABELLED_RULES.list_page_max_candidates

        # Orchestrator level: the event is logged and the review image is kept.
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"c1": [make_message("p1", image=True)]},
            media={"p1": MEESHO_12988_OCR.encode("utf-8")},
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        config = {
            **CONFIG,
            "extraction": {**CONFIG["extraction"], "require_order_id_label": False},
        }
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=config)

        with caplog.at_level(logging.WARNING, logger="core.orchestrator"):
            summary = orch.run(12988, 12988)

        assert summary.trunc == 1
        assert len(writer.rows) == 1
        assert writer.rows[0][1]["order_id"] == f"TRUNC:{EXPECTED_12988}"
        assert "event=ambiguous_order_id" in caplog.text
        assert "internal_id=12988" in caplog.text


# ---------------------------------------------------------------------------
# §6 default single-value vs opt-in multi-order
# ---------------------------------------------------------------------------
class TestOutputMode:
    def test_default_is_single_value(self):
        # Two labelled real ids with the default config -> exactly one value
        # (the top-most); the label is the anchor since the unlabelled path is off.
        result = extract_fields(
            "Order ID: 1111222233334444\nOrder ID: 5555666677778888", "", RULES
        )
        assert result["order_ids"] == ["334444"]
        assert result["ambiguous"] is True

    def test_default_config_is_single_value_via_multi_entry(self):
        # extract_fields_multi honours the same default.
        result = extract_fields_multi(
            "Order ID: 1111222233334444\nOrder ID: 5555666677778888", "", RULES
        )
        assert result["order_ids"] == ["334444"]

    def test_multi_order_opt_in(self):
        rules = Rules.from_config({"extraction": {"multi_order_output": True}})
        result = extract_fields_multi(
            "1111222233334444 and 5555666677778888", "", rules
        )
        assert result["order_ids"] == ["334444", "778888"]
        assert result["order_id"] == "334444"

    def test_default_flag_is_false(self):
        assert RULES.multi_order_output is False
