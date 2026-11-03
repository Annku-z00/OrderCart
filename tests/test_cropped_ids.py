"""
tests/test_cropped_ids.py
=========================

Regression tests for **cropped / partial order ids** (the seller's screenshot
may cut the id off, e.g. only ``342343`` of ``342343834138346752`` is visible).

Since the seller confirmed every order id carries a label, the **label is the
anchor** and its length no longer disqualifies the value:

* a labelled run of **6–18 digits** is accepted;
* **6–15 digits** is a *partial* (cropped) id  -> ``TRUNC:<last6>`` (default) and
  the result is flagged ``partial`` so the screenshot is routed to ``review/``;
* **16–18 digits** is a *full* id              -> bare ``<last6>``;
* the text between the label and the number must not name another field
  (``Order ID # PIN: 517126`` -> the number belongs to the PIN); the gap is
  capped at two newlines;
* the **unlabelled** 16–18 digit fallback is off by default
  (``extraction.require_order_id_label: true``).

The OCR fixtures live in ``tests/fixtures/ocr/``.
"""

from __future__ import annotations

import os

import pytest

from extractor.parser import extract_fields, extract_fields_multi, truncate_order_id
from extractor.rules import (
    INTERVENING_LABEL_RE,
    MAX_LABEL_VALUE_NEWLINES,
    Rules,
)

from tests.test_phase3 import (
    FakeAPI,
    FakeOCR,
    RecordingWriter,
    build_orchestrator,
    make_message,
)

RULES = Rules()

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ocr")


def load_fixture(name: str) -> str:
    """Return the raw OCR text of ``tests/fixtures/ocr/<name>``."""
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as fh:
        return fh.read().strip("\n")


#: ``(fixture, expected type, expected order_id, expected partial flag)``
FIXTURE_CASES = [
    ("crop_6digit.txt", "TRUNC", "TRUNC:342343", True),
    ("crop_8digit.txt", "TRUNC", "TRUNC:234383", True),
    ("crop_12digit.txt", "TRUNC", "TRUNC:834138", True),
    ("label_then_pin_only.txt", "FAILED", "", False),
    ("label_pin_same_line.txt", "FAILED", "", False),
    ("full_16digit.txt", "OK", "346752", False),
]

#: ``(fixture, expected type, expected order_id)`` — the expanded guard traps
#: (Customer / Total / nested Order label) and the newline-cap fixtures.
TRAP_CASES = [
    ("trap_customer_after_label.txt", "FAILED", ""),
    ("trap_total_after_label.txt", "FAILED", ""),
    ("trap_nested_order_label.txt", "FAILED", ""),
    ("trap_three_newlines.txt", "FAILED", ""),
    ("label_two_newlines.txt", "OK", "346752"),
]


# ---------------------------------------------------------------------------
# The six required fixtures
# ---------------------------------------------------------------------------
class TestCroppedOrderIdFixtures:
    @pytest.mark.parametrize("name,expected_type,expected_id,expected_partial", FIXTURE_CASES)
    def test_fixture_output(self, name, expected_type, expected_id, expected_partial):
        result = extract_fields(load_fixture(name), "", RULES)
        assert result["type"] == expected_type
        assert result["order_id"] == expected_id
        assert result["partial"] is expected_partial

    def test_all_six_fixtures_exist(self):
        assert len(FIXTURE_CASES) == 6
        for name, *_ in FIXTURE_CASES:
            assert os.path.isfile(os.path.join(FIXTURE_DIR, name)), name


# ---------------------------------------------------------------------------
# Expanded guard + newline cap traps
# ---------------------------------------------------------------------------
class TestGuardTraps:
    @pytest.mark.parametrize("name,expected_type,expected_id", TRAP_CASES)
    def test_trap_fixture_output(self, name, expected_type, expected_id):
        result = extract_fields(load_fixture(name), "", RULES)
        assert result["type"] == expected_type, (name, result)
        assert result["order_id"] == expected_id, (name, result)

    def test_all_trap_fixtures_exist(self):
        assert len(TRAP_CASES) == 5
        for name, *_ in TRAP_CASES:
            assert os.path.isfile(os.path.join(FIXTURE_DIR, name)), name

    def test_guard_covers_every_configured_term(self):
        # Each expanded guard term rejects when it sits in the gap.
        for term in [
            "PIN", "Pincode", "Pin code", "Mobile", "Phone", "Contact", "AWB",
            "Tracking", "SKU", "Price", "Amount", "Total", "Qty", "Quantity",
            "Discount", "Customer", "Name", "Address", "Date", "Delivery", "ETA",
            "Return", "Refund", "Status", "Order ID", "Order No", "Order:",
        ]:
            assert INTERVENING_LABEL_RE.search(term), term

    @pytest.mark.parametrize("extra_newlines", [0, 1, 2])
    def test_gap_within_newline_cap_is_accepted(self, extra_newlines):
        gap = "\n" * extra_newlines
        result = extract_fields(f"Order ID #{gap}342343834138346752", "", RULES)
        assert result["order_id"] == "346752"
        assert result["partial"] is False

    @pytest.mark.parametrize("extra_newlines", [3, 4, 5])
    def test_gap_beyond_newline_cap_is_rejected(self, extra_newlines):
        gap = "\n" * extra_newlines
        result = extract_fields(f"Order ID #{gap}342343834138346752", "", RULES)
        assert result["type"] == "FAILED"
        assert result["order_ids"] == []

    def test_newline_cap_constant(self):
        assert MAX_LABEL_VALUE_NEWLINES == 2


# ---------------------------------------------------------------------------
# Labelled length handling: 6–15 partial, 16–18 full
# ---------------------------------------------------------------------------
class TestLabelledLengthBand:
    @pytest.mark.parametrize("digits", list(range(6, 19)))
    def test_every_labelled_length(self, digits):
        value = "3" * digits
        result = extract_fields(f"Order ID # {value}", "", RULES)
        if digits >= RULES.full_id_min_digits:
            assert result["type"] == "OK"
            assert result["order_ids"] == [value[-6:]]
            assert result["partial"] is False
        else:
            assert result["type"] == "TRUNC"
            assert result["order_id"] == f"TRUNC:{value[-6:]}"
            assert result["order_ids"] == []
            assert result["partial"] is True

    @pytest.mark.parametrize("digits", [7, 8, 9, 10, 11, 12, 13, 14, 15, 17])
    def test_crops_of_a_real_id(self, digits):
        # A crop of the real id 342343834138346752 is always ``TRUNC:`` + partial.
        value = "342343834138346752"[:digits]
        result = extract_fields(f"Order ID # {value}", "", RULES)
        assert result["partial"] is (digits < 16)
        if digits < 16:
            assert result["order_id"] == f"TRUNC:{value[-6:]}"

    def test_over_18_digit_labelled_run_is_not_an_id(self):
        # 19+ digits is a gluing of two fields, never a single id.
        result = extract_fields("Order ID # 3418579486740656517", "", RULES)
        assert result["order_ids"] == []
        assert result["partial"] is False


# ---------------------------------------------------------------------------
# Intervening-label guard
# ---------------------------------------------------------------------------
class TestInterveningLabelGuard:
    @pytest.mark.parametrize("text", [
        "Order ID # PIN: 517126",
        "Order ID #\nPIN: 517126",
        "Order ID # Pincode 517126",
        "Order ID # Mobile 9988776655",
        "Order ID # Phone: 9988776655",
        "Order ID # AWB 3418579486740656",
        "Order ID # Tracking 3418579486740656",
        "Order ID # SKU 3418579486740656",
        "Order ID # Price 3418579486740656",
    ])
    def test_number_belonging_to_another_field_is_rejected(self, text):
        result = extract_fields(text, "", RULES)
        assert result["order_ids"] == []
        assert result["type"] != "OK"

    def test_clean_label_still_wins(self):
        result = extract_fields("Order ID # 342343834138346752", "", RULES)
        assert result["order_ids"] == ["346752"]


# ---------------------------------------------------------------------------
# partial_id_prefix toggle + unlabelled fallback toggle
# ---------------------------------------------------------------------------
class TestConfigToggles:
    def test_partial_prefix_false_emits_bare_and_flags_partial(self):
        rules = Rules.from_config({"extraction": {"partial_id_prefix": False}})
        result = extract_fields("Order ID # 342343", "", rules)
        assert result["type"] == "OK"
        assert result["order_id"] == "342343"
        assert result["order_ids"] == ["342343"]
        assert result["partial"] is True

    def test_unlabelled_path_disabled_by_default(self):
        assert RULES.require_order_id_label is True
        # A bare 18-digit run with no label is *not* accepted by default ...
        assert extract_fields("342343834138346752", "", RULES)["order_ids"] == []

    def test_unlabelled_path_opt_in(self):
        rules = Rules.from_config({"extraction": {"require_order_id_label": False}})
        result = extract_fields("342343834138346752", "", rules)
        assert result["order_ids"] == ["346752"]


# ---------------------------------------------------------------------------
# Invariant: a bare emitted value always comes from a full labelled match
# ---------------------------------------------------------------------------
class TestBareValueInvariant:
    @pytest.mark.parametrize("name", [name for name, *_ in FIXTURE_CASES + TRAP_CASES])
    def test_bare_value_implies_full_labelled_match(self, name):
        text = load_fixture(name)
        result = extract_fields(text, "", RULES)
        if result["type"] == "OK" and result["order_ids"]:
            # Bare value => not partial...
            assert result["partial"] is False
            # ... and the underlying labelled match really is 16–18 digits.
            match = RULES._re_order_label_value.search(text)
            assert match is not None
            assert len(match.group("value")) >= RULES.full_id_min_digits

    @pytest.mark.parametrize("name", [name for name, *_ in FIXTURE_CASES + TRAP_CASES])
    def test_partial_always_carries_trunc_or_flag(self, name):
        text = load_fixture(name)
        result = extract_fields(text, "", RULES)
        if result["type"] == "TRUNC" and result["partial"]:
            assert result["order_id"].startswith("TRUNC:")
        elif result["partial"]:
            # partial with the prefix disabled -> still flagged
            assert result["type"] == "OK"


# ---------------------------------------------------------------------------
# Review routing for partial ids
# ---------------------------------------------------------------------------
class TestPartialReviewRouting:
    def test_partial_id_is_copied_to_review(self, tmp_path):
        api = FakeAPI(
            chats={"12988": {"id": "c1", "name": "12988"}},
            messages={"c1": [make_message("p1", image=True)]},
            media={"p1": b"Order ID # 342343"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12988, 12988)

        assert summary.trunc == 1
        result = summary.results[0]
        assert result.partial is True
        assert result.review_path is not None
        assert result.review_path.endswith("12988.jpg")
        assert result.to_dict()["partial"] is True

    def test_partial_prefix_false_still_routes_to_review(self, tmp_path):
        config = {
            "whatsapp_api": {"provider": "waha", "base_url": "http://localhost:3000"},
            "extraction": {"partial_id_prefix": False},
            "ocr": {"upscale_factor": 1},
            "output": {"default_file": "orders.txt"},
            "processing": {"delay_between_chats": 0, "max_retries": 1, "retry_backoff": 0},
        }
        api = FakeAPI(
            chats={"12989": {"id": "c1", "name": "12989"}},
            messages={"c1": [make_message("p1", image=True)]},
            media={"p1": b"Order ID # 342343"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, config=config)

        summary = orch.run(12989, 12989)

        result = summary.results[0]
        assert result.status == "OK"
        assert result.order_id == "342343"      # bare, no TRUNC prefix
        assert result.partial is True
        assert result.review_path is not None

    def test_full_id_is_not_routed_to_review(self, tmp_path):
        api = FakeAPI(
            chats={"12990": {"id": "c1", "name": "12990"}},
            messages={"c1": [make_message("p1", image=True)]},
            media={"p1": b"Order ID # 342343834138346752"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12990, 12990)

        result = summary.results[0]
        assert result.order_id == "346752"
        assert result.partial is False
        assert result.review_path is None


# ---------------------------------------------------------------------------
# CSV schema: `partial` must be a column so a bare partial stays identifiable
# ---------------------------------------------------------------------------
class TestCsvSchemaCarriesPartial:
    def test_csv_header_has_partial(self):
        from output.csv_writer import CSV_HEADER

        assert "partial" in CSV_HEADER

    def test_csv_row_records_partial_flag(self, tmp_path):
        from output.csv_writer import CSVWriter

        writer = CSVWriter(str(tmp_path / "orders.csv"))
        writer.write_result(12988, {"type": "OK", "order_id": "342343",
                                    "order_ids": ["342343"], "partial": True})
        writer.write_result(12989, {"type": "OK", "order_id": "346752",
                                    "order_ids": ["346752"], "partial": False})
        rows = {r["internal_id"]: r for r in writer.read_rows()}
        assert rows["12988"]["partial"] == "true"
        assert rows["12989"]["partial"] == "false"

    def test_partial_id_prefix_false_loads_with_partial_column(self):
        # The guard must *not* fire: the writer carries the column.
        rules = Rules.from_config({"extraction": {"partial_id_prefix": False}})
        assert rules.partial_id_prefix is False

    def test_guard_would_fire_without_the_column(self, monkeypatch):
        # Simulate a schema without `partial` -> a clear load-time error.
        import output.csv_writer as csv_writer

        monkeypatch.setattr(
            csv_writer, "CSV_HEADER",
            [c for c in csv_writer.CSV_HEADER if c != "partial"],
        )
        with pytest.raises(ValueError, match="requires 'partial' in the CSV schema"):
            Rules.from_config({"extraction": {"partial_id_prefix": False}})


# ---------------------------------------------------------------------------
# The real 12988 capture against the production config
# ---------------------------------------------------------------------------
#: The real capture (pasted in by the seller): the seller's **Notepad file-list**
#: page for chat 12988 — a column of order ids with *no* order label, plus
#: Notepad's status-bar digit noise.  It is not an order screen, so it cannot
#: exercise the §3.1 labelled path; the order-screen id ``342343834138346752``
#: (last-6 ``346752``) does not occur in it at all.  See the note in
#: ``tests/test_parser.py``.
REAL_12988_FIXTURE = os.path.join(FIXTURE_DIR, "12988_real.txt")


def _real_12988_text() -> str:
    if not os.path.isfile(REAL_12988_FIXTURE):
        pytest.skip("tests/fixtures/ocr/12988_real.txt has not been pasted yet")
    with open(REAL_12988_FIXTURE, encoding="utf-8") as fh:
        text = fh.read().strip("\n")
    if not text.strip():
        pytest.skip("tests/fixtures/ocr/12988_real.txt is empty")
    return text


def load_real_fixture(name: str) -> str:
    """Return the OCR **body** of a ``real_12988_*.txt`` capture.

    Those fixtures open with a ``#`` metadata header that ends at a ``# ---``
    line; only the body is OCR text (the header would otherwise inject the media
    id / internal id into the parse).
    """
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    idx = next((i for i, ln in enumerate(lines) if ln.strip() == "# ---"), -1)
    body = lines[idx + 1:] if idx >= 0 else lines
    return "\n".join(body).strip("\n")


#: The §3.2 unlabelled fallback is off by default; these tests turn it on.
UNLABELLED_RULES = Rules.from_config(
    {"extraction": {"require_order_id_label": False}}
)


def _diagnose_12988(text: str, rules: Rules = RULES) -> str:
    """Explain what the labelled pipeline saw (used in assertion messages)."""
    lines = [f"labels tried: {rules.order_id_labels}"]
    for match in rules._re_order_label_value.finditer(text):
        label = match.group("label")
        filler = match.group("filler")
        value = match.group("value")
        if not (rules.min_full_order_id_digits <= len(value) <= rules.max_labelled_id_digits):
            reason = f"length {len(value)} outside 6..{rules.max_labelled_id_digits}"
        elif filler.count("\n") > MAX_LABEL_VALUE_NEWLINES:
            reason = f"gap has {filler.count(chr(10))} newlines (> {MAX_LABEL_VALUE_NEWLINES})"
        elif INTERVENING_LABEL_RE.search(filler):
            reason = f"intervening label inside {filler!r}"
        else:
            reason = "accepted"
        lines.append(f"  label={label!r} filler={filler!r} value={value!r} -> {reason}")
    if len(lines) == 1:
        lines.append("  no labelled match at all")
    lines.append(
        "  unlabelled path: "
        + ("ON" if not rules.require_order_id_label else "OFF (production default)")
    )
    return "\n".join(lines)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTION_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")


def _production_config() -> dict:
    """The shipped ``config.yaml`` as a plain dict."""
    from frameflow_config import load_config

    return load_config(PRODUCTION_CONFIG_PATH)


def _production_rules() -> Rules:
    """Rules exactly as the shipped ``config.yaml`` loads them."""
    return Rules.from_config(_production_config())


class TestReal12988Screenshot:
    """The genuine chat-12988 capture against the shipped ``config.yaml``.

    The capture is the seller's Notepad **file-list** page: a column of ids with
    no order label, plus status-bar digit noise.  Production rules require a
    label, so the labelled path cannot fire and the chat correctly resolves to
    ``FAILED``; the ids only surface when the unlabelled §3.2 fallback is turned
    on explicitly.  (The earlier ``346752`` expectation came from a stitched
    reconstruction that contained the order-screen id ``342343834138346752``,
    which is absent from this single genuine capture.)
    """

    def test_12988_real_default_config(self):
        text = _real_12988_text()
        rules = _production_rules()
        # Sanity-check we really are on the production settings.
        assert rules.require_order_id_label is True
        assert rules.partial_id_prefix is True
        result = extract_fields(text, "", rules)
        diag = _diagnose_12988(text, rules)
        assert result["type"] == "FAILED", diag
        assert result["order_id"] == "", diag
        assert result["order_ids"] == [], diag
        # A list page carries no order label at all — that is *why* it FAILs ...
        assert "no labelled match at all" in diag
        # ... and the order-screen id is genuinely absent from the capture.
        assert "342343834138346752" not in text

    def test_12988_real_partial_prefix_false(self):
        text = _real_12988_text()
        cfg = _production_config()
        cfg["extraction"]["partial_id_prefix"] = False
        rules = Rules.from_config(cfg)
        assert rules.partial_id_prefix is False
        result = extract_fields(text, "", rules)
        # No labelled value exists, so the prefix toggle cannot change anything.
        assert result["type"] == "FAILED", _diagnose_12988(text, rules)
        assert result["order_id"] == ""

    def test_12988_real_list_page_with_unlabelled_opt_in(self):
        text = _real_12988_text()
        cfg = _production_config()
        cfg["extraction"]["require_order_id_label"] = False
        rules = Rules.from_config(cfg)
        result = extract_fields(text, "", rules)
        diag = _diagnose_12988(text, rules)
        # With the fallback explicitly on, the §3.6 >cap list-page rule collapses
        # the page to a single TRUNC:<window> row (top-most 3420026566347011).
        # The window follows the shipped config (which end it is taken from).
        assert result["type"] == "TRUNC", diag
        assert result["order_id"] == f"TRUNC:{truncate_order_id('3420026566347011', rules)}", diag
        assert result["ambiguous"] is True, diag


# ---------------------------------------------------------------------------
# The other genuine 12988 captures (labelled order screens)
# ---------------------------------------------------------------------------
class TestRealLabelledCaptures:
    """Real Google Lens captures of chat 12988 (see each fixture's header)."""

    def test_hindi_chat_ascii_apostrophe_label_full_id(self):
        # "Order I'd :" (phones autocorrect "Order ID") with the 18-digit id on
        # the next line -> a full id, not a crop.
        text = load_real_fixture("real_12988_hindi_chat.txt")
        result = extract_fields(text, "", RULES)
        assert result["type"] == "OK"
        assert result["order_id"] == "560832"
        assert result["partial"] is False

    def test_crop_6digit_ellipsis_is_partial_trunc(self):
        # "Order #342187..." — the id is cut off, so it is a partial TRUNC.
        text = load_real_fixture("real_12988_crop_6digit.txt")
        result = extract_fields(text, "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:342187"
        assert result["partial"] is True

    def test_crop_12digit_ellipsis_is_partial_trunc(self):
        text = load_real_fixture("real_12988_crop_12digit.txt")
        result = extract_fields(text, "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:341972763328"
        assert result["partial"] is True

    def test_devanagari_hash_label_matches(self):
        # The real Hindi screen labels the id "ऑर्डर #" — a form the pre-
        # expansion label list missed (rule B).  The label must now anchor ...
        text = load_real_fixture("real_12988_hindi_order.txt")
        match = RULES._re_order_label_value.search(text)
        assert match is not None
        assert match.group("value") == "34187320244"

    def test_devanagari_hash_label_keeps_the_cropped_id(self):
        # ... and, because the id now sits next to a real order label, the
        # "SMALL SIZE" line above it no longer vetoes the crop: the ellipsis
        # fallback returns the genuine 11-digit id (previously the first
        # ellipsis in the page won and produced a bogus ``TRUNC:7``).
        text = load_real_fixture("real_12988_hindi_order.txt")
        result = extract_fields(text, "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:34187320244"
        assert result["partial"] is True

    def test_pin_leak_address_pincode_is_not_an_id(self):
        # The delivery block's 517126 (PIN) and 8309847092 (mobile) are never ids.
        text = load_real_fixture("real_12988_pin_leak.txt")
        for rules in (RULES, UNLABELLED_RULES):
            result = extract_fields(text, "", rules)
            assert result["order_ids"] == []
            assert "517126" not in result["order_id"]
            assert "8309847092" not in result["order_id"]

    def test_contact_number_leak_is_not_an_id(self):
        text = load_real_fixture("real_12988_phone_leak.txt")
        for rules in (RULES, UNLABELLED_RULES):
            result = extract_fields(text, "", rules)
            assert result["order_ids"] == []
            assert "9696062564" not in result["order_id"]
            assert "230134" not in result["order_id"]   # Uttar Pradesh PIN
