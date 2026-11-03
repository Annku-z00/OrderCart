"""
tests/test_multi_order.py
=========================

Regression tests for **multi-order chats**: a single WhatsApp chat / internal
customer id may hold several orders spread across several screenshots.

Covers:
  * every complete order id is collected across all images (and the text),
  * ids found on *later* images are kept,
  * duplicate ids collapse to a single output row,
  * a name / address on a later image is still found,
  * a partial media / OCR failure writes what was extracted, records a
    ``PARTIAL`` state and can be retried without duplicating output.

Everything is mocked (fake API + OCR) — no network, no OCR, no OpenCV.
"""

from __future__ import annotations

import os

import pytest

from core.orchestrator import (
    STATUS_FALLBACK_ADDRESS,
    STATUS_FALLBACK_NAME,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_TRANSIENT_ERROR,
    STATUS_TRUNC,
    Orchestrator,
)
from extractor.parser import extract_fields_multi, find_all_order_ids
from extractor.rules import Rules
from output.csv_writer import CSVWriter
from output.writer import NotepadWriter, format_notepad_lines
from state.state import StateStore

from tests.test_phase3 import (
    CONFIG,
    FakeAPI,
    FakeOCR,
    FakeOCRResult,
    RecordingWriter,
    make_message,
)

# multi-order behaviour is now opt-in; default is single-value per §3.1–§3.6.
# This whole module exercises the opt-in path, and each emitted id is truncated
# to its last 6 digits on the way out.
CONFIG_MULTI = {
    **CONFIG,
    "extraction": {**CONFIG["extraction"], "multi_order_output": True},
}

RULES = Rules.from_config(CONFIG_MULTI)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FlakyAPI(FakeAPI):
    """FakeAPI whose ``download_media`` fails (raises) for scripted message ids."""

    def __init__(self, *, fail_media=(), **kwargs):
        super().__init__(**kwargs)
        self.fail_media = set(fail_media)

    def download_media(self, msg):
        mid = (
            msg["id"]["id"]
            if isinstance(msg, dict) and isinstance(msg.get("id"), dict)
            else (msg.get("id") if isinstance(msg, dict) else msg)
        )
        self.media_calls.append(mid)
        if mid in self.fail_media:
            raise RuntimeError("simulated download failure")
        return self.media.get(mid, b"")


class SelectiveOCR(FakeOCR):
    """FakeOCR that reports an *error* (OCR failure) for scripted basenames."""

    def __init__(self, fail_names=(), texts=None):
        super().__init__(texts)
        self.fail_names = set(fail_names)

    def run_on_file(self, path, upscale_factor=1):
        self.calls.append(path)
        name = os.path.basename(path)
        if name in self.fail_names:
            result = FakeOCRResult("")
            result.error = "simulated OCR failure"
            return result
        return super().run_on_file(path, upscale_factor)


def build_orch(tmp_path, api, ocr, writer, state=None, **kwargs):
    return Orchestrator(
        api_client=api,
        ocr_engine=ocr,
        writer=writer,
        state=state if state is not None else StateStore(str(tmp_path / "state.db")),
        rules=Rules.from_config(CONFIG_MULTI),
        config=CONFIG_MULTI,
        assets_dir=str(tmp_path / "assets"),
        logs_dir=str(tmp_path / "logs"),
        review_dir=str(tmp_path / "review"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Extractor: find_all_order_ids / extract_fields_multi
# ---------------------------------------------------------------------------
class TestFindAllOrderIds:
    def test_multiple_ids_returned_in_document_order(self):
        text = "Order ID: 1111222233334444\nOrder ID: 5555666677778888"
        assert find_all_order_ids(text, RULES) == [
            "1111222233334444",
            "5555666677778888",
        ]

    def test_duplicate_ids_collapse(self):
        text = "Order ID 342022570640639296 and again 342022570640639296"
        assert find_all_order_ids(text, RULES) == ["342022570640639296"]

    def test_truncated_ids_are_excluded(self):
        assert find_all_order_ids("Order #341972763328...", RULES) == []

    def test_pin_code_inside_address_block_is_excluded(self):
        text = "Delivery Address\n12 MG Road\nPune - 411001"
        assert find_all_order_ids(text, RULES) == []

    def test_six_digit_number_next_to_order_label_is_an_id(self):
        assert find_all_order_ids("Order #482910", RULES) == ["482910"]

    def test_phone_number_is_excluded(self):
        assert find_all_order_ids("Call me on 9988776655", RULES) == []


class TestExtractFieldsMulti:
    # Opt-in multi-order mode: one row per distinct id, each last-6 truncated.
    def test_collects_ids_from_image_then_text(self):
        result = extract_fields_multi(
            "Order ID: 1111222233334444",
            "Order ID 5555666677778888",
            RULES,
        )
        assert result["type"] == "OK"
        assert result["order_ids"] == ["334444", "778888"]
        assert result["order_id"] == "334444"
        assert result["source"] == "image"

    def test_duplicate_across_image_and_text_collapses(self):
        result = extract_fields_multi(
            "Order ID: 1111222233334444",
            "Order ID 1111222233334444",
            RULES,
        )
        assert result["order_ids"] == ["334444"]

    def test_falls_back_to_name_when_no_id(self):
        result = extract_fields_multi(
            "Customer Name: Priya Sharma\nTotal Rs. 5", "", RULES
        )
        assert result["type"] == "FALLBACK_NAME"
        assert result["order_ids"] == []

    def test_falls_back_to_trunc_when_nothing_better(self):
        result = extract_fields_multi("Order #34187320244...", "", RULES)
        assert result["type"] == "TRUNC"
        assert result["order_id"] == "TRUNC:34187320244"
        assert result["order_ids"] == []


# ---------------------------------------------------------------------------
# Writers: one row per id + idempotent retries
# ---------------------------------------------------------------------------
class TestMultiOrderWriters:
    def test_notepad_emits_one_line_per_id(self, tmp_path):
        lines = format_notepad_lines(
            12988, {"type": "OK", "order_ids": ["1111222233334444", "5555666677778888"]}
        )
        assert lines == ["12988,1111222233334444", "12988,5555666677778888"]

    def test_notepad_write_result_writes_every_id(self, tmp_path):
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        writer.write_result(
            12988, {"type": "OK", "order_ids": ["1111222233334444", "5555666677778888"]}
        )
        assert writer.read_lines() == [
            "12988,1111222233334444",
            "12988,5555666677778888",
        ]

    def test_notepad_duplicate_write_is_idempotent(self, tmp_path):
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        result = {"type": "OK", "order_ids": ["1111222233334444", "5555666677778888"]}
        writer.write_result(12988, result)
        # A retry (e.g. after a partial failure) must not duplicate rows.
        writer.write_result(12988, result)
        assert writer.read_lines() == [
            "12988,1111222233334444",
            "12988,5555666677778888",
        ]

    def test_csv_emits_one_row_per_id(self, tmp_path):
        writer = CSVWriter(str(tmp_path / "orders.csv"))
        writer.write_result(
            12988,
            {
                "type": "OK",
                "order_ids": ["1111222233334444", "5555666677778888"],
                "source": "image",
                "confidence": 0.95,
            },
        )
        rows = writer.read_rows()
        assert [r["order_id"] for r in rows] == [
            "1111222233334444",
            "5555666677778888",
        ]
        assert all(r["internal_id"] == "12988" for r in rows)
        assert all(r["status"] == "OK" for r in rows)

    def test_csv_duplicate_write_is_idempotent(self, tmp_path):
        writer = CSVWriter(str(tmp_path / "orders.csv"))
        result = {"type": "OK", "order_ids": ["1111222233334444"]}
        writer.write_result(12988, result)
        writer.write_result(12988, result)
        assert len(writer.read_rows()) == 1


# ---------------------------------------------------------------------------
# Orchestrator: multiple ids per chat
# ---------------------------------------------------------------------------
class TestMultipleIdsPerChat:
    def test_two_images_two_orders_two_rows(self, tmp_path):
        chat = {"id": "c1", "name": "20001"}
        api = FakeAPI(
            chats={"20001": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Order ID: 5555666677778888",
            },
        )
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        orch = build_orch(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(20001, 20001)

        assert summary.ok == 1
        assert writer.read_lines() == [
            "20001,334444",
            "20001,778888",
        ]

    def test_id_on_later_image_is_kept(self, tmp_path):
        """Image #1 is noise; image #3 holds the only order id."""
        chat = {"id": "c1", "name": "20002"}
        api = FakeAPI(
            chats={"20002": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                    make_message("p3", image=True),
                ]
            },
            media={
                "p1": b"Thanks for shopping!",
                "p2": b"Some random screenshot",
                "p3": b"Order ID: 342022570640639296",
            },
        )
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        orch = build_orch(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(20002, 20002)

        assert summary.ok == 1
        assert writer.read_lines() == ["20002,639296"]

    def test_duplicate_id_in_chat_writes_one_row(self, tmp_path):
        chat = {"id": "c1", "name": "20003"}
        api = FakeAPI(
            chats={"20003": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 342022570640639296",
                "p2": b"Order ID: 342022570640639296",
            },
        )
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        orch = build_orch(tmp_path, api, FakeOCR(), writer)

        orch.run(20003, 20003)

        assert writer.read_lines() == ["20003,639296"]

    def test_name_on_later_image_is_found(self, tmp_path):
        chat = {"id": "c1", "name": "20004"}
        api = FakeAPI(
            chats={"20004": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Just a photo",
                "p2": b"Customer Name: Priya Sharma\nTotal Rs. 500",
            },
        )
        writer = RecordingWriter()
        orch = build_orch(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(20004, 20004)

        assert summary.fallback_name == 1
        _, result = writer.rows[0]
        assert result["type"] == STATUS_FALLBACK_NAME
        assert result["name"] == "Priya Sharma"

    def test_address_on_later_image_is_found(self, tmp_path):
        chat = {"id": "c1", "name": "20005"}
        api = FakeAPI(
            chats={"20005": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Just a photo",
                "p2": b"Address:\n12 MG Road\nPune 411001",
            },
        )
        writer = RecordingWriter()
        orch = build_orch(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(20005, 20005)

        assert summary.fallback_address == 1
        _, result = writer.rows[0]
        assert result["type"] == STATUS_FALLBACK_ADDRESS
        assert "MG Road" in result["address"]

    def test_state_records_all_order_ids(self, tmp_path):
        chat = {"id": "c1", "name": "20006"}
        api = FakeAPI(
            chats={"20006": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Order ID: 5555666677778888",
            },
        )
        state = StateStore(str(tmp_path / "state.db"))
        orch = build_orch(tmp_path, api, FakeOCR(), RecordingWriter(), state=state)

        orch.run(20006, 20006)

        stored = state.get_result(20006)
        assert stored["order_ids"] == ["334444", "778888"]
        assert stored["order_id"] == "334444"


# ---------------------------------------------------------------------------
# Orchestrator: partial failures + safe retries
# ---------------------------------------------------------------------------
class TestPartialFailures:
    def test_partial_download_failure_writes_ids_and_stays_unprocessed(self, tmp_path):
        chat = {"id": "c1", "name": "20100"}
        api = FlakyAPI(
            chats={"20100": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Order ID: 5555666677778888",
            },
            fail_media={"p2"},
        )
        state = StateStore(str(tmp_path / "state.db"))
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        orch = build_orch(tmp_path, api, FakeOCR(), writer, state=state)

        summary = orch.run(20100, 20100)

        assert summary.partial == 1
        assert summary.results[0].status == STATUS_PARTIAL
        # The id we *did* read is written — never lost.
        assert writer.read_lines() == ["20100,334444"]
        # But the chat is not marked done, so the missing image is retried.
        assert state.is_processed(20100) is False

    def test_retry_after_partial_failure_adds_the_missing_id_without_duplicates(
        self, tmp_path
    ):
        chat = {"id": "c1", "name": "20101"}
        messages = {
            "c1": [
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

        # First run: p2 download fails -> PARTIAL with only p1's id.
        first = FlakyAPI(
            chats={"20101": chat}, messages=messages, media=media, fail_media={"p2"}
        )
        build_orch(tmp_path, first, FakeOCR(), writer, state=state).run(20101, 20101)
        assert writer.read_lines() == ["20101,334444"]
        assert state.is_processed(20101) is False

        # Second run: the API has recovered.  Both ids are found; the already
        # written row is not duplicated and the chat is now marked processed.
        second = FakeAPI(chats={"20101": chat}, messages=messages, media=media)
        summary = build_orch(tmp_path, second, FakeOCR(), writer, state=state).run(
            20101, 20101
        )

        assert summary.ok == 1
        assert writer.read_lines() == [
            "20101,334444",
            "20101,778888",
        ]
        assert state.is_processed(20101) is True

    def test_partial_ocr_failure_writes_ids_and_stays_unprocessed(self, tmp_path):
        chat = {"id": "c1", "name": "20102"}
        api = FakeAPI(
            chats={"20102": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Order ID: 5555666677778888",
            },
        )
        state = StateStore(str(tmp_path / "state.db"))
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        ocr = SelectiveOCR(fail_names={"p2.img"})
        orch = build_orch(tmp_path, api, ocr, writer, state=state)

        summary = orch.run(20102, 20102)

        assert summary.partial == 1
        assert writer.read_lines() == ["20102,334444"]
        assert state.is_processed(20102) is False

    def test_partial_failure_with_nothing_extracted_is_transient(self, tmp_path):
        """Some images unreadable *and* nothing found -> retry, no FAILED row."""
        chat = {"id": "c1", "name": "20103"}
        api = FlakyAPI(
            chats={"20103": chat},
            messages={"c1": [make_message("p1", image=True)]},
            media={"p1": b"Order ID: 1111222233334444"},
            fail_media={"p1"},
        )
        state = StateStore(str(tmp_path / "state.db"))
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        orch = build_orch(tmp_path, api, FakeOCR(), writer, state=state)

        summary = orch.run(20103, 20103)

        assert summary.results[0].status == STATUS_TRANSIENT_ERROR
        assert writer.read_lines() == []
        assert state.is_processed(20103) is False

    def test_partial_status_surfaces_in_review(self, tmp_path):
        chat = {"id": "c1", "name": "20104"}
        api = FlakyAPI(
            chats={"20104": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 1111222233334444",
                "p2": b"Order ID: 5555666677778888",
            },
            fail_media={"p2"},
        )
        writer = RecordingWriter()
        orch = build_orch(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(20104, 20104)

        assert [r.internal_id for r in summary.review_ids()] == [20104]

    def test_stop_mid_scan_writes_partial_and_stays_unprocessed(self, tmp_path):
        """A stop request before the images are scanned must not lose the chat."""
        from core.orchestrator import RunControl

        chat = {"id": "c1", "name": "20105"}
        api = FakeAPI(
            chats={"20105": chat},
            messages={
                "c1": [
                    make_message("m1", text="Order ID 1111222233334444"),
                    make_message("p1", image=True),
                ]
            },
            media={"p1": b"Order ID: 5555666677778888"},
        )
        control = RunControl()
        control.request_stop()
        state = StateStore(str(tmp_path / "state.db"))
        writer = NotepadWriter(str(tmp_path / "orders.txt"))
        orch = build_orch(tmp_path, api, FakeOCR(), writer, state=state, control=control)

        result = orch.process_one(20105)

        assert result.status == STATUS_PARTIAL
        assert writer.read_lines() == ["20105,334444"]
        assert state.is_processed(20105) is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
