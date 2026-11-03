"""
tests/test_phase4.py
====================

Phase 4 tests — the production-readiness polish:

* **Dry run**        – processes ids but never writes output nor marks state.
* **Raw OCR log**    – TRUNC / FAILED ids dump their raw text to ``logs/<id>_raw.txt``.
* **TRUNC image**    – the source image for a TRUNC id is copied to ``review/<id>.jpg``.
* **Corrections**    – ``apply_corrections`` rewrites rows in the output file.
* **Review list**    – ``RunSummary.review_ids()`` surfaces TRUNC/FAILED ids.

Like the Phase 3 tests everything is mocked — no network, no OCR, no OpenCV.
"""

from __future__ import annotations

import os

import pytest

from core.orchestrator import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TRUNC,
    Orchestrator,
    ProcessResult,
    RunSummary,
)
from extractor.rules import Rules
from output.corrections import apply_corrections
from output.csv_writer import CSVWriter
from output.writer import NotepadWriter

from tests.test_phase3 import (  # reuse the Phase 3 doubles/fixtures
    CONFIG,
    FakeAPI,
    FakeOCR,
    RecordingWriter,
    make_message,
)
from state.state import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_orchestrator(
    tmp_path,
    api,
    ocr,
    writer=None,
    state=None,
    *,
    dry_run: bool = False,
    logs_dir=None,
    review_dir=None,
):
    return Orchestrator(
        api_client=api,
        ocr_engine=ocr,
        writer=writer if writer is not None else RecordingWriter(),
        state=state if state is not None else StateStore(str(tmp_path / "state.db")),
        rules=Rules.from_config(CONFIG),
        config=CONFIG,
        assets_dir=str(tmp_path / "assets"),
        logs_dir=str(logs_dir if logs_dir is not None else tmp_path / "logs"),
        review_dir=str(review_dir if review_dir is not None else tmp_path / "review"),
        dry_run=dry_run,
    )


def trunc_scenario():
    """One chat whose single image OCRs to a truncated order id."""
    chat = {"id": "chat-12989", "name": "12989"}
    api = FakeAPI(
        chats={"12989": chat},
        messages={"chat-12989": [make_message("m1", image=True)]},
        media={"m1": b"Order ID: 34187320244..."},
    )
    return api


def failed_scenario():
    """One chat whose image has no order id at all."""
    chat = {"id": "chat-12992", "name": "12992"}
    api = FakeAPI(
        chats={"12992": chat},
        messages={"chat-12992": [make_message("m1", image=True)]},
        media={"m1": b"Hello, thanks for your message!"},
    )
    return api


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_writes_nothing_and_marks_no_state(self, tmp_path):
        writer = RecordingWriter()
        state = StateStore(str(tmp_path / "state.db"))
        api = trunc_scenario()
        ocr = FakeOCR()

        orch = build_orchestrator(tmp_path, api, ocr, writer=writer, state=state, dry_run=True)
        summary = orch.run(12989, 12989)

        # Nothing written, nothing recorded in state.
        assert writer.rows == []
        assert state.is_processed(12989) is False

        # But the id *was* processed and the result recorded.
        assert summary.processed == 1
        assert summary.results[0].status == STATUS_TRUNC
        assert summary.results[0].dry_run is True
        assert "[dry-run]" in summary.results[0].message
        assert summary.dry_run is True

    def test_run_dry_run_parameter_overrides_instance(self, tmp_path):
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, trunc_scenario(), FakeOCR(), writer=writer)
        summary = orch.run(12989, 12989, dry_run=True)
        assert writer.rows == []
        assert summary.dry_run is True

    def test_non_dry_run_still_writes(self, tmp_path):
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, trunc_scenario(), FakeOCR(), writer=writer)
        orch.run(12989, 12989)
        assert len(writer.rows) == 1


# ---------------------------------------------------------------------------
# Raw OCR log
# ---------------------------------------------------------------------------
class TestRawOcrLog:
    def test_raw_log_written_for_trunc(self, tmp_path):
        logs = tmp_path / "logs"
        orch = build_orchestrator(tmp_path, trunc_scenario(), FakeOCR(), logs_dir=logs)
        orch.run(12989, 12989)

        raw = logs / "12989_raw.txt"
        assert raw.exists()
        text = raw.read_text(encoding="utf-8")
        assert "# internal_id: 12989" in text
        assert "# status: TRUNC" in text
        assert "34187320244" in text

    def test_raw_log_written_for_failed(self, tmp_path):
        logs = tmp_path / "logs"
        orch = build_orchestrator(tmp_path, failed_scenario(), FakeOCR(), logs_dir=logs)
        orch.run(12992, 12992)

        raw = logs / "12992_raw.txt"
        assert raw.exists()
        assert "# status: FAILED" in raw.read_text(encoding="utf-8")

    def test_no_raw_log_for_ok(self, tmp_path):
        logs = tmp_path / "logs"
        chat = {"id": "chat-12988", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"chat-12988": [make_message("m1", image=True)]},
            media={"m1": b"Order ID: 342022570640639296"},
        )
        orch = build_orchestrator(tmp_path, api, FakeOCR(), logs_dir=logs)
        orch.run(12988, 12988)

        assert not (logs / "12988_raw.txt").exists()


# ---------------------------------------------------------------------------
# TRUNC image copy
# ---------------------------------------------------------------------------
class TestTruncImageCopy:
    def test_trunc_image_copied_to_review(self, tmp_path):
        review = tmp_path / "review"
        orch = build_orchestrator(tmp_path, trunc_scenario(), FakeOCR(), review_dir=review)
        result = orch.process_one(12989, reprocess=True)

        dest = review / "12989.jpg"
        assert dest.exists()
        assert dest.read_bytes() == b"Order ID: 34187320244..."
        assert result.review_path == str(dest)

    def test_ok_image_not_copied(self, tmp_path):
        review = tmp_path / "review"
        chat = {"id": "chat-12988", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"chat-12988": [make_message("m1", image=True)]},
            media={"m1": b"Order ID: 342022570640639296"},
        )
        orch = build_orchestrator(tmp_path, api, FakeOCR(), review_dir=review)
        orch.process_one(12988, reprocess=True)

        assert not (review / "12988.jpg").exists()


# ---------------------------------------------------------------------------
# RunSummary.review_ids
# ---------------------------------------------------------------------------
class TestReviewIds:
    def test_review_ids_collects_trunc_and_failed(self):
        summary = RunSummary()
        summary.record(ProcessResult(12988, STATUS_OK))
        summary.record(ProcessResult(12989, STATUS_TRUNC))
        summary.record(ProcessResult(12992, STATUS_FAILED))
        summary.record(ProcessResult(12993, "MISSING"))
        summary.record(ProcessResult(12994, "SKIPPED", skipped=True))

        ids = [r.internal_id for r in summary.review_ids()]
        assert ids == [12989, 12992]


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------
class TestNotepadCorrections:
    def test_correction_rewrites_matching_row(self, tmp_path):
        path = tmp_path / "orders.txt"
        path.write_text("12988,342022570640639296\n12989,TRUNC:34187320244\n", encoding="utf-8")
        writer = NotepadWriter(str(path))

        changed = writer.apply_corrections({12989: "341873202440000001"})

        assert changed == 1
        assert path.read_text(encoding="utf-8") == (
            "12988,342022570640639296\n12989,341873202440000001\n"
        )

    def test_unknown_id_is_ignored(self, tmp_path):
        path = tmp_path / "orders.txt"
        path.write_text("12988,342022570640639296\n", encoding="utf-8")
        writer = NotepadWriter(str(path))

        assert writer.apply_corrections({99999: "1"}) == 0
        assert path.read_text(encoding="utf-8") == "12988,342022570640639296\n"

    def test_missing_file_returns_zero(self, tmp_path):
        writer = NotepadWriter(str(tmp_path / "nope.txt"))
        assert writer.apply_corrections({12989: "123"}) == 0


class TestCsvCorrections:
    def test_correction_rewrites_matching_row(self, tmp_path):
        path = tmp_path / "orders.csv"
        writer = CSVWriter(str(path))
        writer.write_result(12989, {"type": "TRUNC", "order_id": "TRUNC:34187320244"})
        writer.write_result(12990, {"type": "OK", "order_id": "342022570640639296"})

        changed = writer.apply_corrections({12989: "341873202440000001"})

        assert changed == 1
        rows = writer.read_rows()
        corrected = next(r for r in rows if r["internal_id"] == "12989")
        assert corrected["order_id"] == "341873202440000001"
        assert corrected["status"] == "OK"
        assert corrected["source"] == "manual"
        # The untouched row keeps its original value.
        other = next(r for r in rows if r["internal_id"] == "12990")
        assert other["order_id"] == "342022570640639296"


class TestCorrectionsDispatcher:
    def test_dispatches_notepad_by_extension(self, tmp_path):
        path = tmp_path / "orders.txt"
        path.write_text("12989,TRUNC:34187320244\n", encoding="utf-8")
        changed = apply_corrections(str(path), {12989: "341873202440000001"})
        assert changed == 1
        assert path.read_text(encoding="utf-8") == "12989,341873202440000001\n"

    def test_dispatches_csv_by_extension(self, tmp_path):
        path = tmp_path / "orders.csv"
        writer = CSVWriter(str(path))
        writer.write_result(12989, {"type": "TRUNC", "order_id": "TRUNC:34187320244"})

        changed = apply_corrections(str(path), {12989: "341873202440000001"})

        assert changed == 1
        rows = CSVWriter(str(path)).read_rows()
        assert rows[0]["order_id"] == "341873202440000001"

    def test_explicit_format_overrides_extension(self, tmp_path):
        path = tmp_path / "orders.dat"
        path.write_text("12989,TRUNC:34187320244\n", encoding="utf-8")
        changed = apply_corrections(str(path), {12989: "341873202440000001"}, output_format="notepad")
        assert changed == 1
        assert path.read_text(encoding="utf-8") == "12989,341873202440000001\n"

    def test_empty_corrections_returns_zero(self, tmp_path):
        path = tmp_path / "orders.txt"
        path.write_text("12989,TRUNC:34187320244\n", encoding="utf-8")
        assert apply_corrections(str(path), {}) == 0
