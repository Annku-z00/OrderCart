"""
tests/test_phase2.py
====================

Phase 2 tests: TRUNC detection, multi-line address preservation, Notepad & CSV
writers, and the SQLite state store.

Run with::

    pytest -v tests/test_phase2.py
"""

from __future__ import annotations

import os

import pytest

from extractor.parser import extract_fields, find_order_id
from extractor.rules import Rules
from output.csv_writer import CSV_HEADER, CSVWriter
from output.writer import NotepadWriter, format_notepad_line
from state.state import StateStore


@pytest.fixture()
def rules() -> Rules:
    return Rules()


# ===========================================================================
# TRUNC detection
# ===========================================================================
class TestTruncDetection:
    def test_ellipsis_with_order_label(self, rules):
        """Input: Order #34187320244... -> TRUNC:34187320244"""
        value, conf = find_order_id("Order #34187320244...", rules)
        assert value == "TRUNC:34187320244"
        assert conf == pytest.approx(0.70)

    def test_ellipsis_unicode(self, rules):
        value, _ = find_order_id("Order ID 34187320244\u2026", rules)
        assert value == "TRUNC:34187320244"

    def test_partial_id_near_label_without_ellipsis_is_full_id(self, rules):
        # 10 digits with no "..." is a *complete* order id — order ids may
        # start with any digit.  Only a trailing ellipsis or fewer than six
        # digits makes an id truncated.
        value, conf = find_order_id("Order No: 3418732024", rules)
        assert value == "3418732024"
        assert conf == pytest.approx(0.90)

    def test_partial_id_that_looks_like_phone_is_rejected(self, rules):
        # 10 digits starting 6-9 is treated as a phone number -> rejected.
        value, _ = find_order_id("Order No: 9988776655", rules)
        assert value is None

    def test_full_16_digit_still_wins(self, rules):
        value, _ = find_order_id("Order #3418579486740656", rules)
        assert value == "3418579486740656"

    def test_trunc_rejects_phone_number(self, rules):
        # 9876543210 is a 10-digit phone (starts 9) -> must be rejected.
        value, _ = find_order_id("Call 9876543210...", rules)
        assert value is None

    def test_short_number_with_ellipsis_is_truncated(self, rules):
        # Any digit run cut off with "..." is an incomplete id, however short.
        value, conf = find_order_id("Order #1234...", rules)
        assert value == "TRUNC:1234"
        assert conf == pytest.approx(0.70)

    def test_trivial_short_number_without_context_is_ignored(self, rules):
        # A bare 4-digit number with no label and no ellipsis is not an id.
        assert find_order_id("qty 1234", rules)[0] is None

    def test_trunc_via_extract_fields(self, rules):
        result = extract_fields("Order #34187320244...", "", rules)
        assert result["order_id"] == "TRUNC:34187320244"
        # A truncated id is reported with its own status so the CSV `status`
        # column and the run summary can distinguish it from a clean OK.
        assert result["type"] == "TRUNC"

    def test_trunc_config_driven(self):
        rules = Rules.from_config(
            {"extraction": {"truncated_min_digits": 8, "truncated_max_digits": 12}}
        )
        assert find_order_id("Order #1234567...", rules)[0] is None  # 7 < min
        assert find_order_id("Order #12345678...", rules)[0] == "TRUNC:12345678"


# ===========================================================================
# Multi-line address preservation
# ===========================================================================
class TestAddressPreservation:
    def test_newlines_preserved_in_finding(self, rules):
        text = "Address\nFlat 3B, Green Villa\nMG Road, Pune\nMaharashtra 411001"
        result = extract_fields(text, "", rules)
        assert result["type"] == "FALLBACK_ADDRESS"
        assert "\n" in result["address"]
        assert result["address"].count("\n") >= 2

    def test_notepad_collapses_address_newlines(self):
        """Notepad rows must stay on ONE line -> newlines become spaces."""
        line = format_notepad_line(
            12991, {"type": "FALLBACK_ADDRESS", "address": "12 MG Road\nPune 411001"}
        )
        assert line == "12991,ADDR:12 MG Road Pune 411001"
        assert "\n" not in line

    def test_csv_joins_address_with_pipe(self, tmp_path):
        path = os.path.join(tmp_path, "orders.csv")
        writer = CSVWriter(path)
        writer.write_result(
            12990,
            {"type": "FALLBACK_ADDRESS", "address": "12 MG Road\nPune\n411001",
             "confidence": 0.71},
        )
        rows = writer.read_rows()
        assert len(rows) == 1
        assert rows[0]["address"] == "12 MG Road|Pune|411001"


# ===========================================================================
# Notepad writer
# ===========================================================================
class TestNotepadWriter:
    def test_full_id_line(self, tmp_path):
        writer = NotepadWriter(os.path.join(tmp_path, "orders.txt"))
        line = writer.write_result(12988, {"type": "OK", "order_id": "342022570640639296"})
        assert line == "12988,342022570640639296"

    def test_trunc_line(self, tmp_path):
        writer = NotepadWriter(os.path.join(tmp_path, "orders.txt"))
        line = writer.write_result(12989, {"type": "OK", "order_id": "TRUNC:34187320244"})
        assert line == "12989,TRUNC:34187320244"

    def test_name_fallback_line(self, tmp_path):
        writer = NotepadWriter(os.path.join(tmp_path, "orders.txt"))
        line = writer.write_result(12990, {"type": "FALLBACK_NAME", "name": "Priya Sharma"})
        assert line == "12990,NAME:Priya Sharma"

    def test_address_fallback_line(self, tmp_path):
        writer = NotepadWriter(os.path.join(tmp_path, "orders.txt"))
        line = writer.write_result(
            12991, {"type": "FALLBACK_ADDRESS", "address": "12 MG Road, Pune 411001"}
        )
        assert line == "12991,ADDR:12 MG Road, Pune 411001"

    def test_failed_line(self, tmp_path):
        writer = NotepadWriter(os.path.join(tmp_path, "orders.txt"))
        line = writer.write_result(12992, {"type": "FAILED"})
        assert line == "12992,FAILED"

    def test_immediate_flush_and_multi_write(self, tmp_path):
        path = os.path.join(tmp_path, "orders.txt")
        writer = NotepadWriter(path)
        writer.write_result(12988, {"type": "OK", "order_id": "342022570640639296"})
        # Read the file mid-run: the row must already be on disk (no close needed).
        with open(path, "r", encoding="utf-8") as fh:
            assert "12988,342022570640639296" in fh.read()
        writer.write_result(12989, {"type": "OK", "order_id": "TRUNC:34187320244"})
        assert writer.read_lines() == [
            "12988,342022570640639296",
            "12989,TRUNC:34187320244",
        ]

    def test_accepts_list_of_order_ids(self):
        """The writer API accepts a list (multi-order support later)."""
        line = format_notepad_line(13000, {"type": "OK", "order_id": ["3418579486740656"]})
        assert line == "13000,3418579486740656"


# ===========================================================================
# CSV writer
# ===========================================================================
class TestCSVWriter:
    def test_header_written_once(self, tmp_path):
        path = os.path.join(tmp_path, "orders.csv")
        writer = CSVWriter(path)
        writer.write_result(12988, {"type": "OK", "order_id": "342022570640639296",
                                    "source": "image", "confidence": 0.97})
        writer.write_result(12989, {"type": "OK", "order_id": "342022570640639297",
                                    "source": "image", "confidence": 0.97})
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        # Only ONE header row.
        assert content.count(",".join(CSV_HEADER)) == 1

    def test_row_values(self, tmp_path):
        path = os.path.join(tmp_path, "orders.csv")
        writer = CSVWriter(path)
        writer.write_result(
            12990,
            {"type": "FALLBACK_NAME", "name": "Priya Sharma", "source": "image",
             "confidence": 0.82},
            timestamp="2026-10-16 12:01:00",
        )
        row = writer.read_rows()[0]
        assert row["internal_id"] == "12990"
        assert row["name"] == "Priya Sharma"
        assert row["status"] == "FALLBACK_NAME"
        assert row["source"] == "image"
        assert row["confidence"] == "0.82"
        assert row["timestamp"] == "2026-10-16 12:01:00"

    def test_failed_row(self, tmp_path):
        path = os.path.join(tmp_path, "orders.csv")
        writer = CSVWriter(path)
        writer.write_result(12991, {"type": "FAILED", "confidence": 0.0})
        row = writer.read_rows()[0]
        assert row["status"] == "FAILED"
        assert row["order_id"] == ""
        assert row["confidence"] == "0.00"


# ===========================================================================
# State store
# ===========================================================================
class TestStateStore:
    def test_mark_and_check(self, tmp_path):
        store = StateStore(":memory:")
        assert store.is_processed(12988) is False
        store.mark_processed(12988, {"type": "OK", "order_id": "123"})
        assert store.is_processed(12988) is True
        # Idempotent (INSERT OR REPLACE).
        store.mark_processed(12988, {"type": "OK", "order_id": "456"})
        assert store.count() == 1
        assert store.get_result(12988)["order_id"] == "456"
        store.close()

    def test_persist_across_instances(self, tmp_path):
        db = os.path.join(tmp_path, "state.db")
        store = StateStore(db)
        store.mark_processed(12999, {"type": "FAILED"})
        store.close()

        store2 = StateStore(db)
        assert store2.is_processed(12999) is True
        store2.close()

    def test_clear(self, tmp_path):
        store = StateStore(":memory:")
        store.mark_processed(1, {"type": "OK"})
        store.mark_processed(2, {"type": "OK"})
        assert store.count() == 2
        store.clear()
        assert store.count() == 0
        store.close()

    def test_stats(self, tmp_path):
        store = StateStore(":memory:")
        store.mark_processed(1, {"type": "OK"})
        store.mark_processed(2, {"type": "FALLBACK_NAME"})
        store.mark_processed(3, {"type": "FAILED"})
        stats = store.stats()
        assert stats["total"] == 3
        assert stats["OK"] == 1
        assert stats["FALLBACK_NAME"] == 1
        assert stats["FAILED"] == 1
        store.close()

    def test_reset_ids(self, tmp_path):
        store = StateStore(":memory:")
        store.mark_processed(1, {"type": "OK"})
        store.mark_processed(2, {"type": "OK"})
        store.reset_ids([1])
        assert store.is_processed(1) is False
        assert store.is_processed(2) is True
        store.close()


# ===========================================================================
# End-to-end: extraction -> output -> state ordering
# ===========================================================================
class TestPipelineOrdering:
    def test_output_then_state(self, tmp_path):
        """State is written AFTER output; a crash between them only re-processes."""
        out = os.path.join(tmp_path, "orders.txt")
        writer = NotepadWriter(out)
        store = StateStore(":memory:")

        rules = Rules()
        result = extract_fields("Order ID: 3418579486740656", "", rules)
        line = writer.write_result(12988, result)  # 1) output first
        assert not store.is_processed(12988)       # not yet marked
        store.mark_processed(12988, result)        # 2) state second
        assert store.is_processed(12988)

        # §3 output shape: the last 6 digits (the seller's Notepad workflow).
        assert line == "12988,740656"
        store.close()
