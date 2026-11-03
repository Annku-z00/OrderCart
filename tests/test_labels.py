"""
tests/test_labels.py
====================

Shipping labels (the dedicated Labels view):

* :mod:`labels.generator` — phone/PIN extraction, row normalisation,
  text wrapping, Code128 sizing and the PDF page count;
* :class:`main_web.Api` — the bridge saves the seller settings and writes a
  PDF to ``exports/`` (the real ``config.yaml`` / output file are never
  touched: ``ROOT`` is redirected into ``tmp_path``);
* the JavaScript/HTML wiring is asserted statically (there is no JS runner in
  this repo), matching the style of the other web-UI tests.
"""

from __future__ import annotations

import os
import re

import pytest

import main_web
from main_web import Api
from labels.generator import (
    LabelOptions,
    as_options,
    extract_phone_pin,
    generate_labels_pdf,
    make_barcode,
    rows_to_labels,
    wrap_text,
)

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(main_web.__file__)), "ui")


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
class TestContactExtraction:
    def test_finds_phone_and_last_six_digit_pin(self):
        phone, pin = extract_phone_pin(
            "12 MG Road|Bengaluru|Karnataka - 560001|9876543210"
        )
        assert phone == "9876543210"
        assert pin == "560001"

    def test_accepts_a_country_code_on_the_phone(self):
        phone, pin = extract_phone_pin("+91 98765 43210\nJaipur 302001")
        assert phone == "9876543210"
        assert pin == "302001"

    def test_missing_values_are_empty_strings(self):
        assert extract_phone_pin("") == ("", "")
        assert extract_phone_pin("no contact here") == ("", "")

    def test_picks_the_last_pin_not_a_house_number(self):
        _phone, pin = extract_phone_pin("House 123456, Sector 7, Pune 411001")
        assert pin == "411001"


class TestRowsToLabels:
    def test_skips_trunc_failed_and_merges_duplicates(self):
        rows = [
            {"internal_id": "1", "order_id": "342512705", "name": "Asha",
             "address": "A|B|560001", "timestamp": "2026-10-21 10:00:00"},
            {"internal_id": "1", "order_id": "342512705", "name": "Asha",
             "address": "A|B|560001", "timestamp": "2026-10-21 10:00:00"},
            {"internal_id": "2", "order_id": "TRUNC:347011", "name": "X",
             "address": "Y"},
            {"internal_id": "3", "order_id": "FAILED", "name": "X", "address": "Y"},
            {"internal_id": "4", "order_id": "", "name": "X", "address": "Y"},
            {"internal_id": "5", "order_id": "999888", "status": "FAILED",
             "name": "X", "address": "Y"},
        ]
        labels, skipped, incomplete = rows_to_labels(rows, LabelOptions())
        assert len(labels) == 1
        assert labels[0].order_id == "342512705"
        assert labels[0].phone == "9876543210" or labels[0].phone == ""
        assert skipped == 4
        assert incomplete == 0

    def test_a_duplicate_with_an_address_fills_the_id_only_row(self):
        rows = [
            {"internal_id": "1", "order_id": "123456", "name": "", "address": ""},
            {"internal_id": "1", "order_id": "123456", "name": "Asha",
             "address": "A|B|560001"},
        ]
        labels, skipped, incomplete = rows_to_labels(rows)
        assert len(labels) == 1
        assert labels[0].name == "Asha"
        assert labels[0].address == "A|B|560001"
        assert skipped == 0
        assert incomplete == 0

    def test_raw_address_is_accepted(self):
        labels, _, _ = rows_to_labels(
            [{"internal_id": "1", "order_id": "123456", "name": "A",
              "raw_address": "Line 1|Line 2|560001"}]
        )
        assert labels[0].address == "Line 1|Line 2|560001"
        assert labels[0].pin == "560001"

    def test_rows_without_an_address_are_skipped(self):
        labels, skipped, incomplete = rows_to_labels(
            [{"internal_id": "1", "order_id": "123456", "name": "A", "address": ""}]
        )
        assert labels == []
        assert skipped == 0
        assert incomplete == 1

    def test_options_are_clamped(self):
        opts = as_options({"copies": 999, "width_in": 0.5, "height_in": 99,
                           "show_barcode": "false"})
        assert opts.copies == 50
        assert opts.width_in == 2.0
        assert opts.height_in == 10.0
        assert opts.show_barcode is False

    def test_missing_options_keep_the_default_seller(self):
        opts = as_options({})
        assert opts.seller_name == "VEER HANUMAN CRAFTS"
        assert opts.footer == "Thank you for shopping with us!"
        assert opts.brand == "MEESHO"


class TestLayoutHelpers:
    def test_wrap_text_honours_pipe_separators(self):
        lines = wrap_text("12 MG Road|Bengaluru|560001", "Helvetica", 8, 200)
        assert lines == ["12 MG Road", "Bengaluru", "560001"]

    def test_barcode_fits_the_content_width(self):
        barcode = make_barcode("342343834138346752", 260, 50)
        assert barcode is not None
        assert barcode.width <= 260

    def test_short_barcode_is_not_stretched_beyond_the_limit(self):
        barcode = make_barcode("342512705", 260, 50)
        assert barcode is not None
        assert barcode.width <= 260


class TestPdf:
    def test_one_page_per_copy(self, tmp_path):
        rows = [{
            "internal_id": "12988", "order_id": "342512705", "name": "Asha",
            "address": "12 MG Road|Bengaluru|Karnataka - 560001|9876543210",
            "timestamp": "2026-10-21 10:00:00",
        }]
        out = tmp_path / "labels.pdf"
        summary = generate_labels_pdf(
            rows, str(out),
            {"copies": 2, "seller_name": "Test Seller",
             "seller_address": "Shop 1|Jaipur|302001"},
        )
        assert out.is_file()
        assert out.read_bytes().startswith(b"%PDF")
        assert summary == {
            "path": str(out), "labels": 1, "pages": 2,
            "skipped": 0, "incomplete": 0,
        }
        data = out.read_bytes()
        assert len(re.findall(rb"/Type\s*/Page(?![s])", data)) == 2

    def test_nothing_printable_raises(self, tmp_path):
        with pytest.raises(ValueError):
            generate_labels_pdf(
                [{"internal_id": "1", "order_id": "TRUNC:123", "name": "A",
                  "address": "B"}],
                str(tmp_path / "empty.pdf"),
            )

    def test_barcode_can_be_disabled(self, tmp_path):
        rows = [{"internal_id": "1", "order_id": "123456", "name": "A",
                 "address": "B"}]
        summary = generate_labels_pdf(
            rows, str(tmp_path / "no-barcode.pdf"), {"show_barcode": False}
        )
        assert summary["pages"] == 1


# ---------------------------------------------------------------------------
# Api bridge
# ---------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    """An Api pointed at tmp_path, with ``os.startfile`` neutralised."""
    monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
    monkeypatch.setattr(main_web.os, "startfile", lambda *a, **k: None,
                        raising=False)
    instance = Api()
    instance.config.setdefault("output", {})
    instance.config["output"]["default_file"] = str(tmp_path / "orders.csv")
    return instance


class TestApiBridge:
    def test_settings_round_trip(self, api):
        saved = api.save_label_settings({
            "brand": "MY SHOP",
            "seller_name": "Test Seller",
            "seller_address": "Shop 1|Jaipur|302001",
            "seller_phone": "9999999999",
            "copies": 2,
            "show_barcode": False,
        })
        assert saved["ok"]
        cfg = api.get_label_settings()
        assert cfg["brand"] == "MY SHOP"
        assert cfg["seller_name"] == "Test Seller"
        assert cfg["copies"] == 2
        assert cfg["show_barcode"] is False

    def test_generate_labels_writes_to_exports(self, api, tmp_path):
        rows = [{
            "internal_id": "12988", "order_id": "342512705", "name": "Asha",
            "address": "12 MG Road|Bengaluru|560001|9876543210",
            "timestamp": "2026-10-21 10:00:00",
        }]
        result = api.generate_labels(rows, {"copies": 2, "seller_name": "Test Seller"})
        assert result["ok"], result
        assert result["labels"] == 1
        assert result["pages"] == 2
        assert result["skipped"] == 0
        assert os.path.isfile(result["path"])
        assert result["path"].startswith(str(tmp_path / "exports"))
        # The modal values are remembered for the next batch.
        assert api.get_label_settings()["seller_name"] == "Test Seller"

    def test_generate_labels_reports_no_usable_ids(self, api):
        result = api.generate_labels([
            {"internal_id": "1", "order_id": "TRUNC:123", "name": "A",
             "address": "B"},
        ])
        assert result["ok"] is False
        assert "order id" in result["error"].lower()

    def test_generate_labels_rejects_an_empty_list(self, api):
        result = api.generate_labels([])
        assert result["ok"] is False
        assert "visible orders" in result["error"]

    def test_generate_labels_explains_a_missing_address(self, api):
        result = api.generate_labels([
            {"internal_id": "1", "order_id": "123456", "name": "A",
             "address": ""},
        ])
        assert result["ok"] is False
        assert "address" in result["error"]
        assert "Collect per row" in result["error"]

    def test_collect_mode_can_be_switched_from_the_labels_modal(self, api):
        result = api.set_collect_mode("all")
        assert result == {"ok": True, "collect": "all"}
        assert api.config["extraction"]["collect"] == "all"
        assert api.set_collect_mode("nonsense")["ok"] is False


# ---------------------------------------------------------------------------
# Static UI wiring
# ---------------------------------------------------------------------------
class TestUiWiring:
    def test_the_labels_sidebar_view_and_toolbar_shortcut_exist(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        for token in (
            'data-view="labels"',
            'id="view-labels"',
            'id="btn-labels"',
            'id="labels-tbody"',
            'id="labels-search"',
            'id="labels-select-all"',
            'id="btn-labels-generate"',
            'id="btn-labels-collect-all"',
            'id="btn-labels-save"',
            'id="label-seller-name"',
            'id="label-seller-address"',
            'id="label-copies"',
            'id="label-barcode"',
        ):
            assert token in html

    def test_js_loads_the_view_and_sends_the_raw_address(self):
        with open(os.path.join(UI_DIR, "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        for token in (
            "function loadLabelsView",
            "function renderLabelTable",
            "function mergeLabelRows",
            "function onGenerateLabels",
            "a.get_label_settings",
            "a.generate_labels",
            "a.set_collect_mode",
            "raw_address",
        ):
            assert token in js
        assert '"labels"' in js

    def test_the_feature_is_gated_until_extraction_is_finished(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        assert 'id="btn-labels" disabled' in html
        assert "nav-link-dev" in html
        with open(os.path.join(UI_DIR, "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        assert "LABELS_UNDER_DEVELOPMENT = true" in js
        assert 'name === "labels" && LABELS_UNDER_DEVELOPMENT' in js

    def test_the_labels_view_styles_exist(self):
        with open(os.path.join(UI_DIR, "style.css"), encoding="utf-8") as fh:
            css = fh.read()
        for token in (".labels-layout", ".labels-orders-card",
                      ".labels-settings-card", ".labels-table",
                      ".labels-body", ".labels-grid", ".labels-actions",
                      ".label-btn"):
            assert token in css
