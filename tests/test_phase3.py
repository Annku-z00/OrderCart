"""
tests/test_phase3.py
====================

Phase 3 tests — orchestrator loop, output wiring and MISSING resilience.

Everything is mocked: a fake WhatsApp API client serves chats/messages, a
fake OCR engine "reads" whatever bytes the fake media download wrote to disk,
and the writer/state layers are either the real ones (writing into ``tmp_path``)
or lightweight fakes that record what they were asked to do.

The goal is to prove the *loop logic* is correct without touching the network,
PaddleOCR, Tesseract or OpenCV.
"""

from __future__ import annotations

import os

import pytest

from core.orchestrator import (
    STATUS_FAILED,
    STATUS_FALLBACK_ADDRESS,
    STATUS_FALLBACK_NAME,
    STATUS_MISSING,
    STATUS_NO_IMAGE,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TRUNC,
    Orchestrator,
    ProcessResult,
    RunControl,
    RunSummary,
)
from extractor.rules import Rules
from output.csv_writer import CSVWriter
from output.writer import NotepadWriter
from state.state import StateStore
from whatsapp.api_client import extract_image_messages, extract_text_messages

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------
CONFIG = {
    "whatsapp_api": {"provider": "waha", "base_url": "http://localhost:3000"},
    "extraction": {
        "order_id_labels": ["Order ID", "Order Number", "Order No", "Order #"],
        "name_labels": ["Customer Name", "Name"],
        "address_labels": ["Address", "Shipping Address"],
        "text_message_limit": 5,
        "address_max_chars": 400,
        "truncated_min_digits": 5,
        "truncated_max_digits": 14,
    },
    "ocr": {"upscale_factor": 1},  # OCR is Google Lens only; tests inject FakeOCR
    "output": {"default_file": "orders.txt", "date_format": "%Y-%m-%d %H:%M:%S"},
    "processing": {"delay_between_chats": 0, "max_retries": 1, "retry_backoff": 0},
}

#: Opt-in to the older one-row-per-id behaviour.  multi-order behaviour is now
#: opt-in; the default is single-value per §3.1–§3.6.
CONFIG_MULTI = {
    **CONFIG,
    "extraction": {**CONFIG["extraction"], "multi_order_output": True},
}


class FakeOCRResult:
    """Duck-typed stand-in for :class:`ocr.engine.OCRResult`."""

    def __init__(self, text: str = "", confidence: float = 0.9, engine: str = "fake"):
        self.text = text
        self.confidence = confidence
        self.engine = engine
        self.lines = [ln for ln in text.split("\n") if ln.strip()]
        self.error = None


class FakeOCR:
    """OCR engine that returns the file's own bytes decoded as UTF-8.

    The fake API writes the "recognised text" into the image file, so this
    engine effectively reads back whatever the test scripted for that message.
    A ``texts`` mapping lets a test force a specific result per basename.
    """

    def __init__(self, texts: dict | None = None):
        self.texts = texts or {}
        self.calls: list[str] = []

    def run_on_file(self, path: str, upscale_factor: int = 1) -> FakeOCRResult:
        self.calls.append(path)
        name = os.path.basename(path)
        if name in self.texts:
            return FakeOCRResult(self.texts[name])
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return FakeOCRResult(fh.read())
        except (OSError, UnicodeDecodeError):
            return FakeOCRResult("")

    # A few orchestrators prefer run(image); provide it for completeness.
    def run(self, image) -> FakeOCRResult:  # pragma: no cover - not used by tests
        return FakeOCRResult("")


class FakeAPI:
    """Minimal WhatsApp API client double.

    ``chats``      – mapping of internal id (str) -> chat dict (omitted = MISSING)
    ``messages``   – mapping of chat_id -> list of message dicts
    ``media``      – mapping of message id -> bytes returned by download_media
    """

    def __init__(self, chats=None, messages=None, media=None, raise_for=None):
        self.chats = chats or {}
        self.messages = messages or {}
        self.media = media or {}
        self.raise_for = raise_for or {}          # internal id -> Exception
        self.closed = False
        self.media_calls: list = []               # raw message ids downloaded

    def get_chat_by_name(self, name: str):
        key = str(name)
        if key in self.raise_for:
            raise self.raise_for[key]
        return self.chats.get(key)

    def get_messages(self, chat_id: str, limit: int = 10):
        return list(self.messages.get(chat_id, []))[:limit]

    def download_media(self, msg):
        """Mirror of ``WhatsAppAPIClient.download_media(msg)``.

        Accepts the message dict (WAHA shape with ``id.id`` or a plain id) and
        returns the scripted bytes without any network access.
        """
        mid = msg["id"]["id"] if isinstance(msg, dict) and isinstance(msg.get("id"), dict) \
            else (msg.get("id") if isinstance(msg, dict) else msg)
        self.media_calls.append(mid)
        return self.media.get(mid, b"")

    def close(self):
        self.closed = True


class RecordingWriter:
    """Writer double that records ``(internal_id, result)`` tuples."""

    def __init__(self, fail_on: int | None = None):
        self.rows: list[tuple[int, dict]] = []
        self.fail_on = fail_on

    def write_result(self, internal_id, result, **kwargs):
        if self.fail_on is not None and int(internal_id) == int(self.fail_on):
            raise PermissionError("simulated file lock")
        self.rows.append((int(internal_id), dict(result)))
        return f"{internal_id},?"


def make_message(msg_id: str, *, text: str = "", image: bool = False, from_me: bool = False):
    msg = {"id": msg_id, "fromMe": from_me}
    if image:
        msg["type"] = "image"
        msg["mimetype"] = "image/jpeg"
    else:
        msg["body"] = text
    return msg


def build_orchestrator(tmp_path, api, ocr, writer=None, state=None, control=None, config=None):
    config = config or CONFIG
    return Orchestrator(
        api_client=api,
        ocr_engine=ocr,
        writer=writer if writer is not None else RecordingWriter(),
        state=state if state is not None else StateStore(str(tmp_path / "state.db")),
        rules=Rules.from_config(config),
        config=config,
        assets_dir=str(tmp_path / "assets"),
        # Keep review/ inside the test's tmp dir instead of the repo checkout.
        review_dir=str(tmp_path / "review"),
        control=control,
    )


# ---------------------------------------------------------------------------
# Order-id happy paths
# ---------------------------------------------------------------------------
class TestOrderIdExtraction:
    def test_order_id_from_image(self, tmp_path):
        chat = {"id": "chat-12988", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "chat-12988": [make_message("m1", image=True)],
            },
            media={"m1": b"Order ID: 342022570640639296"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12988, 12988)

        assert summary.ok == 1
        assert len(writer.rows) == 1
        internal_id, result = writer.rows[0]
        assert internal_id == 12988
        # §3: the emitted value is the last 6 digits of the id.
        assert result["order_id"] == "639296"
        assert result["type"] == STATUS_OK
        assert result["source"] == "image"
        assert summary.duration >= 0

    def test_order_id_from_plain_text_message(self, tmp_path):
        """~1% of customers type the order id instead of screenshotting it."""
        chat = {"id": "chat-12989", "name": "12989"}
        api = FakeAPI(
            chats={"12989": chat},
            messages={
                "chat-12989": [
                    make_message("m1", text="Hi, my order number is 342022570640639296")
                ]
            },
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12989, 12989)

        assert summary.ok == 1
        _, result = writer.rows[0]
        # §3: last-6 of 342022570640639296.
        assert result["order_id"] == "639296"
        assert result["source"] == "text"

    def test_truncated_order_id_from_image(self, tmp_path):
        chat = {"id": "chat-12990", "name": "12990"}
        api = FakeAPI(
            chats={"12990": chat},
            messages={"chat-12990": [make_message("m1", image=True)]},
            media={"m1": "Order No: 34187320244...".encode("utf-8")},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12990, 12990)

        assert summary.trunc == 1
        _, result = writer.rows[0]
        assert result["order_id"] == "TRUNC:34187320244"
        assert result["type"] == STATUS_TRUNC

    def test_name_beats_truncated_id_in_image(self, tmp_path):
        """A cut-off id AND a Delivery Address name -> NAME:mayank, not TRUNC."""
        chat = {"id": "chat-12995", "name": "12995"}
        api = FakeAPI(
            chats={"12995": chat},
            messages={"chat-12995": [make_message("m1", image=True)]},
            media={"m1": b"Order #341972763328...\nDelivery Address\nmayank"},
        )
        writer = RecordingWriter()
        # Uses the default (production) rule set, whose name labels include the
        # "Delivery Address" layout — the minimal phase3 CONFIG above does not.
        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=writer,
            state=StateStore(str(tmp_path / "state.db")),
            rules=Rules(),
            config=CONFIG,
            assets_dir=str(tmp_path / "assets"),
            logs_dir=str(tmp_path / "logs"),
            review_dir=str(tmp_path / "review"),
        )

        summary = orch.run(12995, 12995)

        assert summary.fallback_name == 1
        assert summary.trunc == 0
        _, result = writer.rows[0]
        assert result["type"] == STATUS_FALLBACK_NAME
        assert result["name"] == "mayank"


# ---------------------------------------------------------------------------
# Fallbacks and failures
# ---------------------------------------------------------------------------
class TestFallbacks:
    def test_name_fallback_when_no_order_id(self, tmp_path):
        chat = {"id": "chat-1", "name": "12991"}
        api = FakeAPI(
            chats={"12991": chat},
            messages={"chat-1": [make_message("m1", image=True)]},
            media={"m1": b"Customer Name: Priya Sharma\nTotal Rs. 500"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12991, 12991)

        assert summary.fallback_name == 1
        _, result = writer.rows[0]
        assert result["name"] == "Priya Sharma"
        assert result["type"] == STATUS_FALLBACK_NAME

    def test_address_fallback_when_no_id_or_name(self, tmp_path):
        chat = {"id": "chat-2", "name": "12992"}
        api = FakeAPI(
            chats={"12992": chat},
            messages={"chat-2": [make_message("m1", image=True)]},
            media={"m1": b"Address:\n12 MG Road\nPune 411001"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12992, 12992)

        assert summary.fallback_address == 1
        _, result = writer.rows[0]
        assert result["type"] == STATUS_FALLBACK_ADDRESS
        assert "MG Road" in result["address"]

    def test_nothing_found_writes_failed_row(self, tmp_path):
        chat = {"id": "chat-3", "name": "12993"}
        api = FakeAPI(
            chats={"12993": chat},
            messages={"chat-3": [make_message("m1", image=True)]},
            media={"m1": b"Thanks for shopping with us!"},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12993, 12993)

        assert summary.failed == 1
        _, result = writer.rows[0]
        assert result["type"] == STATUS_FAILED

    def test_no_messages_at_all_reports_no_image(self, tmp_path):
        chat = {"id": "chat-4", "name": "12994"}
        api = FakeAPI(chats={"12994": chat}, messages={"chat-4": []})
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12994, 12994)

        assert summary.no_image == 1
        assert writer.rows == []


# ---------------------------------------------------------------------------
# MISSING resilience — the headline requirement
# ---------------------------------------------------------------------------
class TestMissingResilience:
    def test_missing_chat_does_not_stop_the_loop(self, tmp_path):
        """12998 is missing; the ids around it must still be processed."""
        chats = {
            "12997": {"id": "c97", "name": "12997"},
            "12999": {"id": "c99", "name": "12999"},
        }
        messages = {
            "c97": [make_message("a", text="Order ID 1111222233334444")],
            "c99": [make_message("b", text="Order ID 5555666677778888")],
        }
        api = FakeAPI(chats=chats, messages=messages)
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(12997, 12999)

        assert summary.total == 3
        assert summary.missing == 1
        assert summary.ok == 2
        statuses = {r.internal_id: r.status for r in summary.results}
        assert statuses[12997] == STATUS_OK
        assert statuses[12998] == STATUS_MISSING
        assert statuses[12999] == STATUS_OK
        assert [row[0] for row in writer.rows] == [12997, 12999]

    def test_api_error_on_one_id_marks_failed_and_continues(self, tmp_path):
        from whatsapp.api_client import WhatsAppAPIError

        chats = {"13001": {"id": "c1", "name": "13001"}}
        api = FakeAPI(
            chats=chats,
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
            raise_for={"13000": WhatsAppAPIError("boom")},
        )
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        summary = orch.run(13000, 13001)

        statuses = {r.internal_id: r.status for r in summary.results}
        assert statuses[13000] == STATUS_FAILED
        assert statuses[13001] == STATUS_OK
        assert summary.errors == 1

    def test_many_consecutive_errors_aborts_run(self, tmp_path):
        from whatsapp.api_client import WhatsAppAPIError

        api = FakeAPI(raise_for={str(i): WhatsAppAPIError("down") for i in range(13100, 13120)})
        orch = build_orchestrator(tmp_path, api, FakeOCR())
        orch.max_consecutive_errors = 3

        summary = orch.run(13100, 13119)

        assert summary.stopped is True
        assert summary.errors == 3
        assert summary.processed == 3


# ---------------------------------------------------------------------------
# State DB integration
# ---------------------------------------------------------------------------
class TestStateIntegration:
    def test_already_processed_id_is_skipped(self, tmp_path):
        chat = {"id": "c1", "name": "14000"}
        api = FakeAPI(
            chats={"14000": chat},
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
        )
        state = StateStore(str(tmp_path / "state.db"))
        state.mark_processed(14000, {"type": "OK"})
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, state=state)

        summary = orch.run(14000, 14000)

        assert summary.skipped == 1
        assert writer.rows == []
        assert summary.results[0].status == STATUS_SKIPPED

    def test_reprocess_flag_ignores_state(self, tmp_path):
        chat = {"id": "c1", "name": "14001"}
        api = FakeAPI(
            chats={"14001": chat},
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
        )
        state = StateStore(str(tmp_path / "state.db"))
        state.mark_processed(14001, {"type": "OK"})
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, state=state)

        summary = orch.run(14001, 14001, reprocess=True)

        assert summary.ok == 1
        assert len(writer.rows) == 1

    def test_state_written_only_after_output(self, tmp_path):
        chat = {"id": "c1", "name": "14002"}
        api = FakeAPI(
            chats={"14002": chat},
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
        )
        state = StateStore(str(tmp_path / "state.db"))
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, state=state)

        orch.run(14002, 14002)

        assert state.is_processed(14002) is True
        # §3: last-6 of 1111222233334444.
        assert state.get_result(14002)["order_id"] == "334444"

    def test_writer_failure_does_not_mark_state(self, tmp_path):
        chat = {"id": "c1", "name": "14003"}
        api = FakeAPI(
            chats={"14003": chat},
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
        )
        state = StateStore(str(tmp_path / "state.db"))
        writer = RecordingWriter(fail_on=14003)
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer, state=state)

        summary = orch.run(14003, 14003)

        assert summary.results[0].status == STATUS_FAILED
        assert summary.results[0].error and "write failed" in summary.results[0].error
        # Crucially: the id can be retried on the next run.
        assert state.is_processed(14003) is False


# ---------------------------------------------------------------------------
# Real writers wired through the orchestrator
# ---------------------------------------------------------------------------
class TestRealWriters:
    def test_notepad_output_file_contents(self, tmp_path):
        chat = {"id": "c1", "name": "15000"}
        api = FakeAPI(
            chats={"15000": chat, "15001": {"id": "c2", "name": "15001"}},
            messages={
                "c1": [make_message("a", text="Order ID 1111222233334444")],
                "c2": [make_message("b", image=True)],
            },
            media={"b": "Order #34187320244...".encode("utf-8")},
        )
        out = tmp_path / "orders.txt"
        writer = NotepadWriter(str(out))
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        orch.run(15000, 15001)

        lines = writer.read_lines()
        # §3: id 15000's value is the last-6 of 1111222233334444.
        assert lines[0] == "15000,334444"
        assert lines[1] == "15001,TRUNC:34187320244"

    def test_csv_output_file_contents(self, tmp_path):
        chat = {"id": "c1", "name": "16000"}
        api = FakeAPI(
            chats={"16000": chat},
            messages={"c1": [make_message("a", image=True)]},
            media={"a": b"Customer Name: Anil Kumar\nAddress:\n9 Park St\nKolkata 700016"},
        )
        out = tmp_path / "orders.csv"
        writer = CSVWriter(str(out))
        orch = build_orchestrator(tmp_path, api, FakeOCR(), writer)

        orch.run(16000, 16000)

        rows = writer.read_rows()
        assert len(rows) == 1
        assert rows[0]["internal_id"] == "16000"
        assert rows[0]["status"] == STATUS_FALLBACK_NAME
        assert rows[0]["name"] == "Anil Kumar"
        # Header is present exactly once.
        with open(out, "r", encoding="utf-8") as fh:
            assert fh.read().count("internal_id") == 1


# ---------------------------------------------------------------------------
# Progress + logging callbacks
# ---------------------------------------------------------------------------
class TestCallbacks:
    def test_progress_and_log_callbacks_are_invoked(self, tmp_path):
        chat = {"id": "c1", "name": "17000"}
        api = FakeAPI(
            chats={"17000": chat, "17001": {"id": "c2", "name": "17001"}},
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
        )
        progress = []
        logs = []
        orch = build_orchestrator(tmp_path, api, FakeOCR())
        orch.progress_callback = lambda result, index, total: progress.append(
            (result.internal_id, index, total)
        )
        orch.log_callback = logs.append

        orch.run(17000, 17001)

        assert progress[0] == (17000, 1, 2)
        assert progress[-1][1] == 2 and progress[-1][2] == 2
        assert any("Starting run" in line for line in logs)
        assert any("Run finished" in line for line in logs)

    def test_callback_exception_is_swallowed(self, tmp_path):
        chat = {"id": "c1", "name": "17100"}
        api = FakeAPI(
            chats={"17100": chat},
            messages={"c1": [make_message("a", text="Order ID 1111222233334444")]},
        )
        orch = build_orchestrator(tmp_path, api, FakeOCR())

        def explode(*_args):
            raise RuntimeError("UI blew up")

        orch.log_callback = explode
        orch.progress_callback = explode

        summary = orch.run(17100, 17100)
        assert summary.ok == 1


# ---------------------------------------------------------------------------
# Run control (stop / pause)
# ---------------------------------------------------------------------------
class TestRunControl:
    def test_stop_after_first_id(self, tmp_path):
        chats = {str(i): {"id": f"c{i}", "name": str(i)} for i in range(18000, 18005)}
        api = FakeAPI(
            chats=chats,
            messages={f"c{i}": [make_message("a", text="Order ID 1111222233334444")] for i in range(18000, 18005)},
        )
        control = RunControl()
        orch = build_orchestrator(tmp_path, api, FakeOCR(), control=control)
        orch.progress_callback = lambda r, i, t: control.request_stop()

        summary = orch.run(18000, 18004)

        assert summary.stopped is True
        assert summary.processed == 1

    def test_pause_blocks_until_resumed(self, tmp_path):
        control = RunControl()
        control.pause()
        assert control.paused is True
        control.resume()
        assert control.paused is False
        # wait_if_paused returns immediately once resumed.
        assert control.wait_if_paused() is False

    def test_reset_clears_flags(self, tmp_path):
        control = RunControl()
        control.request_stop()
        control.reset()
        assert control.stopped is False
        assert control.paused is False


# ---------------------------------------------------------------------------
# Small unit checks
# ---------------------------------------------------------------------------
class TestRunSummary:
    def test_counts_and_report(self):
        summary = RunSummary(total=4)
        summary._range = "1-4"
        summary.record(ProcessResult(internal_id=1, status=STATUS_OK, order_id="1"))
        summary.record(ProcessResult(internal_id=2, status=STATUS_TRUNC, order_id="TRUNC:1"))
        summary.record(ProcessResult(internal_id=3, status=STATUS_MISSING))
        summary.record(ProcessResult(internal_id=4, status=STATUS_SKIPPED, skipped=True))

        assert summary.processed == 3
        assert summary.skipped == 1
        assert summary.ok == 1
        assert summary.trunc == 1
        assert summary.missing == 1
        assert summary.successful == 2
        assert "OrderCart run summary" in summary.report()

    def test_process_result_written_flag(self):
        assert ProcessResult(1, STATUS_OK).written is True
        assert ProcessResult(1, STATUS_FAILED).written is True
        assert ProcessResult(1, STATUS_MISSING).written is False
        assert ProcessResult(1, STATUS_NO_IMAGE).written is False
        assert ProcessResult(1, STATUS_SKIPPED).written is False

    def test_summary_line_is_human_readable(self):
        r = ProcessResult(12988, STATUS_OK, order_id="342022570640639296")
        assert r.summary_line() == "12988: OK 342022570640639296"


class TestRangeHandling:
    def test_reversed_range_is_normalised(self, tmp_path):
        api = FakeAPI(chats={"500": {"id": "c", "name": "500"}})
        orch = build_orchestrator(tmp_path, api, FakeOCR())
        summary = orch.run(502, 500)
        assert summary.total == 3

    def test_assets_folder_created_for_images(self, tmp_path):
        chat = {"id": "c1", "name": "600"}
        api = FakeAPI(
            chats={"600": chat},
            messages={"c1": [make_message("img-1", image=True)]},
            media={"img-1": b"Order ID: 9999888877776666"},
        )
        orch = build_orchestrator(tmp_path, api, FakeOCR())
        orch.run(600, 600)

        assert os.path.isdir(os.path.join(str(tmp_path / "assets"), "600"))


class TestOrchestratorLifecycle:
    def test_close_closes_api(self, tmp_path):
        api = FakeAPI()
        orch = build_orchestrator(tmp_path, api, FakeOCR())
        orch.close()
        assert api.closed is True

    def test_build_writer_chooses_csv_for_csv_path(self, tmp_path):
        writer = Orchestrator.build_writer(str(tmp_path / "a.csv"), "csv", CONFIG)
        assert isinstance(writer, CSVWriter)
        writer2 = Orchestrator.build_writer(str(tmp_path / "a.txt"), "notepad", CONFIG)
        assert isinstance(writer2, NotepadWriter)

    def test_process_one_never_raises_on_api_explosion(self, tmp_path):
        class ExplodingAPI:
            def get_chat_by_name(self, name):
                raise RuntimeError("kaboom")

        orch = build_orchestrator(tmp_path, ExplodingAPI(), FakeOCR())
        result = orch.process_one(1)
        assert result.status == STATUS_FAILED
        assert result.error == "kaboom"


# ---------------------------------------------------------------------------
# Incoming-only messages (the seller's own outgoing prompts never count)
# ---------------------------------------------------------------------------
class TestIncomingOnlyMessages:
    def test_outgoing_text_is_ignored(self):
        msgs = [
            make_message(
                "m1",
                text="1 फोटो भेजें 2 ऑर्डर आईडी का स्क्रीनशॉट साझा करें",
                from_me=True,
            ),
            make_message("m2", text="Order ID 3418579486740656"),
        ]
        assert extract_text_messages(msgs) == ["Order ID 3418579486740656"]

    def test_outgoing_image_is_ignored(self):
        msgs = [
            make_message("m1", image=True, from_me=True),
            make_message("m2", image=True),
        ]
        assert [m["id"] for m in extract_image_messages(msgs)] == ["m2"]

    def test_outgoing_image_is_never_downloaded_or_ocrd(self, tmp_path):
        """A picture the seller sent is skipped before download / OCR."""
        chat = {"id": "chat-12988", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={
                "chat-12988": [
                    make_message("out", image=True, from_me=True),
                    make_message("in", image=True),
                ]
            },
            media={
                "out": b"Order ID: 111111111111111111",
                "in": b"Order ID: 342022570640639296",
            },
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer)

        summary = orch.run(12988, 12988)

        assert summary.ok == 1
        assert orch.outgoing_images_skipped == 1
        assert "out" not in api.media_calls
        assert len(ocr.calls) == 1
        # This suite's config truncates to the last 6 digits (see the
        # happy-path test above) - the point here is *which* picture was read.
        assert writer.rows[0][1]["order_id"] == "639296"


# ---------------------------------------------------------------------------
# Multi-order scanning (every image is read; nothing stops at the first id)
# ---------------------------------------------------------------------------
class TestMultiOrderScanning:
    # multi-order behaviour is now opt-in; default is single-value per §3.1–§3.6.
    # These tests exercise the opt-in path (config=CONFIG_MULTI) and each id is
    # truncated to its last 6 digits on the way out.
    def test_text_and_image_ids_are_both_collected(self, tmp_path):
        """A typed id no longer short-circuits the images — all ids are kept."""
        chat = {"id": "c1", "name": "12996"}
        api = FakeAPI(
            chats={"12996": chat},
            messages={
                "c1": [
                    make_message("m1", text="Order ID 342022570640639296"),
                    make_message("m2", image=True),
                ]
            },
            media={"m2": b"Order ID: 9999888877776666"},
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=CONFIG_MULTI)

        summary = orch.run(12996, 12996)

        assert summary.ok == 1
        _, result = writer.rows[0]
        # Image ids come first (matching the extractor's image-before-text
        # priority), then the ids typed in the chat text — each last-6.
        assert result["order_ids"] == [
            "776666",
            "639296",
        ]
        # The image was still downloaded and read.
        assert api.media_calls == ["m2"]
        assert len(ocr.calls) == 1

    def test_every_image_is_scanned_and_all_ids_collected(self, tmp_path):
        """Three screenshots, three orders — all three ids survive."""
        chat = {"id": "c1", "name": "12997"}
        api = FakeAPI(
            chats={"12997": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                    make_message("p3", image=True),
                ]
            },
            media={
                "p1": b"Order ID: 342022570640639296",
                "p2": b"Order ID: 1111222233334444",
                "p3": b"Order ID: 5555666677778888",
            },
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=CONFIG_MULTI)

        summary = orch.run(12997, 12997)

        assert summary.ok == 1
        # Every image was read — no early stop at the first complete id.
        assert len(ocr.calls) == 3
        _, result = writer.rows[0]
        assert result["order_ids"] == [
            "639296",
            "334444",
            "778888",
        ]

    def test_duplicate_id_across_images_is_deduped(self, tmp_path):
        chat = {"id": "c1", "name": "12999"}
        api = FakeAPI(
            chats={"12999": chat},
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
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=CONFIG_MULTI)

        orch.run(12999, 12999)

        _, result = writer.rows[0]
        assert result["order_ids"] == ["639296"]

    def test_truncated_image_does_not_stop_the_scan(self, tmp_path):
        """A cut-off id in image #1 must not prevent finding a full id in #2."""
        chat = {"id": "c1", "name": "12998"}
        api = FakeAPI(
            chats={"12998": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Order #341972763328...",
                "p2": b"Order ID: 342022570640639296",
            },
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer, config=CONFIG_MULTI)

        summary = orch.run(12998, 12998)

        assert summary.ok == 1
        assert len(ocr.calls) == 2
        _, result = writer.rows[0]
        assert result["order_ids"] == ["639296"]

    def test_name_on_a_later_image_is_found(self, tmp_path):
        """A later screenshot may hold the name even when the first is noise."""
        chat = {"id": "c1", "name": "13002"}
        api = FakeAPI(
            chats={"13002": chat},
            messages={
                "c1": [
                    make_message("p1", image=True),
                    make_message("p2", image=True),
                ]
            },
            media={
                "p1": b"Thanks for shopping with us!",
                "p2": b"Customer Name: Priya Sharma\nTotal Rs. 500",
            },
        )
        ocr = FakeOCR()
        writer = RecordingWriter()
        orch = build_orchestrator(tmp_path, api, ocr, writer)

        summary = orch.run(13002, 13002)

        assert summary.fallback_name == 1
        _, result = writer.rows[0]
        assert result["name"] == "Priya Sharma"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
