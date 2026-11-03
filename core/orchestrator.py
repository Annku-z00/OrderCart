"""
core/orchestrator.py
====================

The batch-processing brain of OrderCart.

For every internal id from ``start_id`` to ``end_id`` (inclusive) the
orchestrator runs the ten steps from the spec:

1.  Check the SQLite state DB — skip the id when it is already done
    (unless the caller asked to reprocess).
2.  Ask the WhatsApp API client for the chat named after the internal id.
3.  Chat not found -> log ``MISSING`` and continue (never crash the loop).
4.  Pull the recent messages for the chat.
5.  Scan the plain-text messages for order ids / name / address.
6.  Download **every** image message in the chat (in original message order)
    and run OCR on each, collecting ids / name / address from the whole batch.
    A single chat may hold several orders, so the scan never stops at the first
    complete order id.
7.  Write the result to the output file — one ``internal_id,order_id`` row per
    distinct order id (Notepad), or one CSV row per id.
8.  Mark the id as processed in the state DB (only after step 7 succeeded, and
    never for a PARTIAL result where some images could not be read).
9.  Report progress + a log line through the callbacks the GUI supplies.
10. Sleep for the configured delay so we stay gentle on the WhatsApp API.

The loop is deliberately resilient: any exception for a single id is caught,
recorded on that id's :class:`ProcessResult` and the loop moves on.

Nothing in this module talks to the network directly — the API client and the
OCR engine are injected — which is exactly what lets the Phase 3 tests swap in
fakes and run in milliseconds.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from extractor.parser import extract_fields_multi, find_customer_details
from extractor.rules import Rules, normalize

try:  # Message helpers live in the WhatsApp client module.
    from whatsapp.api_client import (
        ChatNotFoundError,
        WhatsAppAPIError,
        extract_image_messages,
        extract_text_messages,
        is_image_message,
        message_id,
    )
except ImportError:  # pragma: no cover - allows standalone import in tests
    from whatsapp.api_client import (  # type: ignore
        ChatNotFoundError,
        WhatsAppAPIError,
        extract_image_messages,
        extract_text_messages,
        is_image_message,
        message_id,
    )

logger = logging.getLogger(__name__)


#: Markers that identify a screenshot as an **order screen** — the order
#: headings, the delivery block and the order timeline.  A picture of the
#: product itself carries none of them, so it is never mistaken for a failed
#: order.
_ORDER_SCREEN_MARKERS = (
    "order details",
    "order id",
    "order no",
    "order number",
    "order #",
    "order placed",
    "order confirmed",
    "order packed",
    "order shipped",
    "order cancelled",
    "your order",
    "review your order",
    "delivery address",
    "shipping address",
    "cancel order",
    "track order",
    "out for delivery",
    "sold by",
    "sold to",
    "delivered on",
    "order summary",
    "order history",
    # The same word in the languages the seller receives.
    "ऑर्डर",
    "आर्डर",
    "ஆர்டர்",
    "ఆర్డర్",
    "অর্ডার",
)


#: Markers that prove a screenshot really is an *order* screen.
#: ``_ORDER_SCREEN_MARKERS`` is deliberately looser (it also accepts
#: "track order"), which is right when deciding whether an empty picture is a
#: failed order — but a product page shows "TRACK ORDER" too, so the product
#: guard below needs the stricter set.
_STRONG_ORDER_MARKERS = (
    "order details",
    "order id",
    "order no",
    "order number",
    "order #",
    "order placed",
    "order confirmed",
    "order packed",
    "order shipped",
    "order cancelled",
    "your order",
    "review your order",
    "delivery address",
    "shipping address",
    "cancel order",
    "out for delivery",
    "delivered on",
    "order summary",
    "order history",
    "ऑर्डर",
    "आर्डर",
    "ஆர்டர்",
    "ఆర్డర్",
    "অর্ডার",
)

#: Screen furniture that only appears on a product / checkout page — never on
#: an order that was actually placed.  A product page also prints the saved
#: delivery address, so without this guard the run writes an address-only row
#: for a picture that has no order at all.
_PRODUCT_PAGE_MARKERS = (
    "switch to upi",
    "pay now",
    "add to cart",
    "check offer",
    "meesho maha saver",
    "got a great deal",
    "continue shopping",
    "share this product",
)


# ---------------------------------------------------------------------------
# Media file extensions (mimetype -> extension for downloaded images)
# ---------------------------------------------------------------------------
MIMETYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def pick_extension(msg: Dict[str, Any]) -> str:
    """Derive the file extension for a downloaded message's media.

    WAHA populates ``msg["media"]["mimetype"]`` for image messages once the
    media has been downloaded (``downloadMedia=true``).  Saving the file with
    a self-documenting extension keeps ``temp/`` and ``assets/`` readable,
    while OpenCV happily opens any of these formats regardless of extension.
    Unknown or missing mimetypes fall back to ``.img``.
    """
    mime = str(((msg or {}).get("media") or {}).get("mimetype") or "").lower()
    return MIMETYPE_EXT.get(mime, ".img")


# ---------------------------------------------------------------------------
# Status constants (kept as plain strings so they serialise cleanly)
# ---------------------------------------------------------------------------
STATUS_OK = "OK"
STATUS_TRUNC = "TRUNC"
STATUS_FALLBACK_NAME = "FALLBACK_NAME"
STATUS_FALLBACK_ADDRESS = "FALLBACK_ADDRESS"
STATUS_FAILED = "FAILED"
STATUS_MISSING = "MISSING"
STATUS_NO_IMAGE = "NO_IMAGE"
STATUS_SKIPPED = "SKIPPED"
STATUS_REVIEW = "REVIEW"
#: Some (but not all) images could not be downloaded / OCR'd.  The rows we did
#: extract are written, but the chat is *not* marked processed so a later run
#: retries the missing images — the idempotent writers make that duplicate-free.
STATUS_PARTIAL = "PARTIAL"
#: A fetch/transport failure that should be retried on the next run.  It is
#: deliberately NOT written to the state DB so the id is attempted again.
STATUS_TRANSIENT_ERROR = "TRANSIENT_ERROR"

#: Statuses that mean "we successfully wrote an output row for this id".
#: ``TRANSIENT_ERROR`` is intentionally absent: transient failures must not be
#: persisted, otherwise a single API blip would skip the chat forever.
_WRITTEN_STATUSES = {
    STATUS_OK,
    STATUS_TRUNC,
    STATUS_FALLBACK_NAME,
    STATUS_FALLBACK_ADDRESS,
    STATUS_FAILED,
    STATUS_PARTIAL,
}

#: Human-friendly log suffixes.
_STATUS_ICON = {
    STATUS_OK: "OK",
    STATUS_TRUNC: "TRUNC",
    STATUS_FALLBACK_NAME: "NAME",
    STATUS_FALLBACK_ADDRESS: "ADDR",
    STATUS_FAILED: "FAILED",
    STATUS_PARTIAL: "PARTIAL",
    STATUS_MISSING: "MISSING",
    STATUS_NO_IMAGE: "NO-IMAGE",
    STATUS_SKIPPED: "SKIP",
    STATUS_REVIEW: "REVIEW",
    STATUS_TRANSIENT_ERROR: "RETRY",
}


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------
@dataclass
class ProcessResult:
    """Outcome of processing one internal id."""

    internal_id: int
    status: str
    order_id: str = ""
    name: str = ""
    address: str = ""
    source: str = ""
    confidence: float = 0.0
    chat_id: Optional[str] = None
    images: int = 0
    message: str = ""
    error: Optional[str] = None
    skipped: bool = False
    timestamp: str = ""
    dry_run: bool = False
    raw_text: str = ""
    review_path: Optional[str] = None
    transient: bool = False
    #: Every distinct order id found for this chat (one output row each).
    order_ids: List[str] = field(default_factory=list)
    #: True when some images were skipped due to a download / OCR failure.
    incomplete: bool = False
    #: True when more than one candidate order id survived (the top-most was
    #: emitted) or a list page collapsed to one ``TRUNC:`` row.
    ambiguous: bool = False
    #: True when the emitted id is a *crop* of a longer id (a 6-15 digit
    #: labelled value) and therefore needs a human to verify it.
    partial: bool = False
    #: Short label for the review flag (``MULTI: names differ`` /
    #: ``MULTI: name missing`` / ``MULTI: no name``), shown in the Orders
    #: table so the seller knows which check the row needs.
    review_reason: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def written(self) -> bool:
        """True when an output row was written for this id."""
        return self.status in _WRITTEN_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "internal_id": self.internal_id,
            "status": self.status,
            "order_id": self.order_id,
            "order_ids": list(self.order_ids),
            "name": self.name,
            "address": self.address,
            "source": self.source,
            "confidence": round(float(self.confidence), 4),
            "chat_id": self.chat_id,
            "images": self.images,
            "message": self.message,
            "error": self.error,
            "skipped": self.skipped,
            "incomplete": self.incomplete,
            "ambiguous": self.ambiguous,
            "partial": self.partial,
            "review_reason": self.review_reason,
            "transient": self.transient,
            "timestamp": self.timestamp,
            "dry_run": self.dry_run,
            "review_path": self.review_path,
        }

    def summary_line(self) -> str:
        """One-line description used in the GUI log."""
        tag = _STATUS_ICON.get(self.status, self.status)
        if self.order_ids:
            detail = ", ".join(self.order_ids)
        else:
            detail = self.order_id or self.name or self.address or ""
        if self.status in (STATUS_FAILED, STATUS_TRANSIENT_ERROR):
            detail = self.message or "nothing extracted"
        return f"{self.internal_id}: {tag} {detail}".rstrip()


@dataclass
class RunSummary:
    """Aggregate statistics for a whole run."""

    total: int = 0
    processed: int = 0
    skipped: int = 0
    ok: int = 0
    trunc: int = 0
    fallback_name: int = 0
    fallback_address: int = 0
    failed: int = 0
    partial: int = 0
    missing: int = 0
    no_image: int = 0
    errors: int = 0
    transient: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0
    stopped: bool = False
    dry_run: bool = False
    results: List[ProcessResult] = field(default_factory=list)

    def record(self, result: ProcessResult) -> None:
        self.results.append(result)
        if result.skipped:
            self.skipped += 1
        elif result.transient:
            # A fetch/transport failure: attempted, but not counted as
            # processed (nothing was written and it will be retried).
            self.transient += 1
        else:
            self.processed += 1
        if result.status == STATUS_OK:
            self.ok += 1
        elif result.status == STATUS_TRUNC:
            self.trunc += 1
        elif result.status == STATUS_FALLBACK_NAME:
            self.fallback_name += 1
        elif result.status == STATUS_FALLBACK_ADDRESS:
            self.fallback_address += 1
        elif result.status == STATUS_FAILED:
            self.failed += 1
        elif result.status == STATUS_PARTIAL:
            self.partial += 1
        elif result.status == STATUS_MISSING:
            self.missing += 1
        elif result.status == STATUS_NO_IMAGE:
            self.no_image += 1
        if result.error:
            self.errors += 1

    @property
    def successful(self) -> int:
        """Ids that ended with a usable order id or a documented fallback."""
        return (
            self.ok
            + self.trunc
            + self.fallback_name
            + self.fallback_address
            + self.partial
        )

    @property
    def order_id_count(self) -> int:
        """How many output rows this run produced (every id of every chat).

        ``ok`` / ``successful`` count **chats**; a chat can carry several
        orders, so the Dashboard KPI and the summary must count the ids
        themselves — one chat with five screenshots is five extracted ids,
        not one.
        """
        total = 0
        for result in self.results:
            if result.order_ids:
                total += len(result.order_ids)
            elif result.status in (
                STATUS_TRUNC,
                STATUS_FALLBACK_NAME,
                STATUS_FALLBACK_ADDRESS,
                STATUS_PARTIAL,
            ):
                total += 1
        return total

    def review_ids(self) -> List[ProcessResult]:
        """Results that need a human to confirm/paste the order id.

        Anything truncated (``TRUNC``), that produced no usable order id
        (``FAILED``) or that only completed *partially* (some images could not
        be read) is surfaced in the GUI review panel.  A chat that also
        carries a **cut-off** order id needs a human too, even when the chat
        as a whole produced complete ids (``partial``).  Skipped ids are
        excluded because nothing was attempted for them this run.
        """
        return [
            r
            for r in self.results
            if not r.skipped
            and (
                r.status in (STATUS_TRUNC, STATUS_FAILED, STATUS_PARTIAL)
                or r.partial
            )
        ]

    def review_rows(self) -> List[tuple]:
        """``(internal_id, status, current_value)`` rows for the review panel.

        The GUI's review window expects these plain tuples (it unpacks them
        when building the form).  Providing the conversion here — once — means
        both :meth:`run`'s automatic review popup and the manual
        ``Review fixes…`` button share identical, correctly-typed rows.
        """
        rows: List[tuple] = []
        for result in self.review_ids():
            current = str(getattr(result, "order_id", "") or "")
            status = str(getattr(result, "status", "") or "")
            if status.upper() == STATUS_TRUNC and current.upper().startswith("TRUNC:"):
                current = current.split(":", 1)[1]
            rows.append((int(result.internal_id), status, current))
        return rows

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "processed": self.processed,
            "skipped": self.skipped,
            "ok": self.ok,
            "order_ids": self.order_id_count,
            "trunc": self.trunc,
            "fallback_name": self.fallback_name,
            "fallback_address": self.fallback_address,
            "failed": self.failed,
            "partial": self.partial,
            "missing": self.missing,
            "no_image": self.no_image,
            "errors": self.errors,
            "transient": self.transient,
            "successful": self.successful,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 3),
            "stopped": self.stopped,
            "dry_run": self.dry_run,
        }

    def report(self) -> str:
        """Multi-line text report (printed by the CLI and shown in the GUI)."""
        d = self.to_dict()
        lines = [
            "OrderCart run summary",
            "---------------------",
            f"range          : {getattr(self, '_range', '')}",
            f"processed      : {d['processed']}  (skipped {d['skipped']})",
            f"order ids      : {d['order_ids']}",
            f"truncated ids  : {d['trunc']}",
            f"name fallback  : {d['fallback_name']}",
            f"address fallbk : {d['fallback_address']}",
            f"partial        : {d['partial']}",
            f"failed         : {d['failed']}",
            f"missing chats  : {d['missing']}",
            f"no image       : {d['no_image']}",
            f"errors         : {d['errors']}",
            f"retryable      : {d['transient']}",
            f"duration       : {d['duration']}s",
            f"stopped early  : {d['stopped']}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run control (pause / resume / stop) — shared between the GUI and the worker
# ---------------------------------------------------------------------------
class RunControl:
    """Thread-safe pause / resume / stop flags for the worker thread."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._pause = threading.Event()

    # -- stop ----------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()
        # A paused loop must wake up to notice the stop request.
        self._pause.clear()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    # -- pause ---------------------------------------------------------
    def pause(self) -> None:
        if not self._stop.is_set():
            self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def toggle_pause(self) -> bool:
        """Flip the paused state and return the *new* paused value."""
        if self._pause.is_set():
            self.resume()
            return False
        self.pause()
        return True

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    # -- blocking helper ----------------------------------------------
    def wait_if_paused(self, poll: float = 0.1) -> bool:
        """Block while paused. Returns ``True`` if a stop was requested."""
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(poll)
        return self._stop.is_set()

    def reset(self) -> None:
        self._stop.clear()
        self._pause.clear()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class Orchestrator:
    """Runs the per-id pipeline over a range of internal ids."""

    def __init__(
        self,
        api_client: Any,
        ocr_engine: Any,
        writer: Any,
        state: Any,
        notepad_writer: Any = None,
        rules: Optional[Rules] = None,
        config: Optional[Dict[str, Any]] = None,
        assets_dir: str = "assets",
        logs_dir: str = "logs",
        review_dir: str = "review",
        failed_dir: str = "failed",
        dry_run: bool = False,
        control: Optional[RunControl] = None,
        progress_callback: Optional[Callable[[ProcessResult, int, int], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        max_consecutive_errors: int = 5,
        images_per_chat: int = 0,
    ) -> None:
        self.api = api_client
        self.ocr = ocr_engine
        self.writer = writer
        #: Optional second writer for the seller's plain "internal_id,value"
        #: Notepad list.  The primary writer keeps the full detail (order id,
        #: name and address per order); this one keeps the id-only format the
        #: Notepad workflow expects.
        self.notepad_writer = notepad_writer
        self.state = state
        self.config = config or {}
        self.rules = rules or Rules.from_config(self.config)

        extraction = self.config.get("extraction", {}) or {}
        processing = self.config.get("processing", {}) or {}
        ocr_cfg = self.config.get("ocr", {}) or {}

        self.text_message_limit = int(extraction.get("text_message_limit", 5))
        # A chat may hold many screenshots; fetch enough messages that none of
        # the image messages fall outside the window.
        self.max_messages_per_chat = max(
            self.text_message_limit, int(processing.get("max_messages_per_chat", 50) or 50)
        )
        self.delay = float(processing.get("delay_between_chats", 0.5))
        self.upscale_factor = int(ocr_cfg.get("upscale_factor", 1))
        #: Re-read the enlarged header of an image whose full-page text held
        #: no order id (the id line is tiny in a very tall screenshot).
        self.focus_pass = bool(ocr_cfg.get("focus_pass", True))
        #: One output row per order id found in a chat (a chat can carry many
        #: orders).  Off -> one row per chat, the first id wins.
        self.multi_order_output = bool(
            extraction.get("multi_order_output", False)
        )
        self.assets_dir = assets_dir
        self.logs_dir = logs_dir
        self.review_dir = review_dir
        #: Screenshots that yielded no id, no name and no address but do
        #: look like an order screen — the seller's "failed" list.
        self.failed_dir = failed_dir
        #: How many pictures went to the failed list / how many were plain
        #: product pictures that were skipped on purpose / how many gave a
        #: name or an address but no order id.
        self.failed_images = 0
        self.skipped_pictures = 0
        self.no_id_images = 0
        #: Pictures *the seller* sent to the customer.  They are never order
        #: screenshots, so they are dropped before download / OCR.
        self.outgoing_images_skipped = 0
        self.dry_run = bool(dry_run)
        self.max_consecutive_errors = max(1, int(max_consecutive_errors))
        # 0 (the default) means "download and scan every image message"; a
        # positive value caps how many images are processed per chat.
        images_cap = int(images_per_chat) if images_per_chat else int(
            processing.get("max_images_per_chat", 0) or 0
        )
        self.images_per_chat = max(0, images_cap)

        self.control = control or RunControl()
        self.progress_callback = progress_callback
        self.log_callback = log_callback

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        logger.info(message)
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:  # pragma: no cover - UI callback must not break us
                pass

    def _emit_progress(self, result: ProcessResult, index: int, total: int) -> None:
        if self.progress_callback:
            try:
                self.progress_callback(result, index, total)
            except Exception:  # pragma: no cover
                pass

    @staticmethod
    def _chat_id(chat: Dict[str, Any]) -> Optional[str]:
        """Extract the provider-specific chat id from a chat dict."""
        if not isinstance(chat, dict):
            return None
        for key in ("id", "chatId", "remoteJid"):
            value = chat.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and value.get("_serialized"):
                return str(value["_serialized"])
        return None

    def _map_status(self, result_type: str) -> str:
        mapping = {
            "OK": STATUS_OK,
            "TRUNC": STATUS_TRUNC,
            "FALLBACK_NAME": STATUS_FALLBACK_NAME,
            "FALLBACK_ADDRESS": STATUS_FALLBACK_ADDRESS,
            "FAILED": STATUS_FAILED,
            "PARTIAL": STATUS_PARTIAL,
        }
        return mapping.get(str(result_type or "").upper(), STATUS_FAILED)

    def _ocr_image(self, image_path: str) -> Tuple[str, bool]:
        """Run the injected OCR engine on ``image_path``.

        Returns ``(text, failed)``.  ``failed`` is ``True`` only when OCR
        could not run at all (engine raised, or it returned an error and no
        text) — an image that simply contains no text is *not* a failure.  This
        lets the orchestrator distinguish "nothing there" from "could not read
        it, retry later".
        """
        result: Any = None
        try:
            if hasattr(self.ocr, "run_on_file"):
                result = self.ocr.run_on_file(
                    image_path, upscale_factor=self.upscale_factor
                )
            else:  # pragma: no cover - only hit with a bare engine
                from ocr.preprocess import load_image

                image = load_image(image_path, upscale_factor=self.upscale_factor)
                result = self.ocr.run(image)
        except TypeError:
            # A fake engine that only accepts the path.
            try:
                result = self.ocr.run_on_file(image_path)  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover - OCR failure must not crash
                self.log(f"  OCR failed for {os.path.basename(image_path)}: {exc}")
                return "", True
        except Exception as exc:  # pragma: no cover - OCR failure must not crash
            self.log(f"  OCR failed for {os.path.basename(image_path)}: {exc}")
            return "", True

        if result is None:
            return "", True
        if isinstance(result, str):
            return result, False
        text = getattr(result, "text", "") or ""
        error = getattr(result, "error", None)
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        if text:
            self.log(f"  OCR ({getattr(result, 'engine', 'ocr')}) conf={confidence:.2f}")
        if error and not (text or "").strip():
            return "", True
        return text, False

    def _focus_image(self, image_path: str) -> str:
        """Re-read only the header of an image (the enlarged focus pass).

        The order id of a Meesho screenshot sits in the top few percent of a
        very tall image, so the enlarged crop often reads it better than the
        full page.  Only used for the images whose full-page text carried no
        order id at all — an OCR failure here is never fatal, the full-page
        result simply stands.
        """
        runner = getattr(self.ocr, "run_focused_on_file", None)
        if not callable(runner):
            return ""
        try:
            result = runner(image_path, upscale_factor=self.upscale_factor)
        except TypeError:  # pragma: no cover - a fake engine without the kwarg
            try:
                result = runner(image_path)
            except Exception:
                return ""
        except Exception as exc:  # pragma: no cover - never fatal
            self.log(f"  focus OCR failed for {os.path.basename(image_path)}: {exc}")
            return ""
        if result is None:
            return ""
        text = getattr(result, "text", "") or ""
        return text if not getattr(result, "error", None) else ""

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------
    def _extract(self, text: str, is_text: bool = False) -> Dict[str, Any]:
        """Extract the fields of **one** source.

        A source is a single screenshot (the OCR text of one image) or the
        WhatsApp chat text.  Extracting per source is what keeps the label of
        one screenshot from swallowing the digits of the next one.

        What a row collects is the ``extraction.collect`` setting:

        * ``"single"`` (the default) — the extractor's own fallback chain
          decides: the order id, else the customer name, else the address.
          One value per row, nothing more.
        * ``"all"`` — the order id **and** the customer name **and** the
          address of the same screenshot travel together into the row, so
          the seller sees who the order belongs to.
        """
        if not text:
            return {
                "order_id": "", "order_ids": [], "name": "", "address": "",
                "type": "FAILED", "confidence": 0.0, "source": "",
                "ambiguous": False, "ambiguous_candidates": 0, "partial": False,
            }
        if is_text:
            fields = extract_fields_multi("", text, self.rules)
        else:
            fields = extract_fields_multi(text, "", self.rules)

        if str(getattr(self.rules, "collect", "single")).lower() != "all":
            return fields

        # "all" mode — add the details the fallback chain left out.  Only the
        # *reliable* ones are used (a label or the delivery-block shape), so
        # a stray line never turns into a customer's name.
        if not fields.get("name") or not fields.get("address"):
            name, address = find_customer_details(text, self.rules)
            if name and not fields.get("name"):
                fields["name"] = name
            if address and not fields.get("address"):
                fields["address"] = address
        return fields

    @staticmethod
    def _has_order_id(fields: Dict[str, Any]) -> bool:
        """True when an extraction carries an order id (complete or cut off)."""
        if not fields:
            return False
        if fields.get("order_ids"):
            return True
        return bool(str(fields.get("order_id") or "").strip())

    @staticmethod
    def _image_yielded_nothing(fields: Dict[str, Any]) -> bool:
        """True when a screenshot produced no id, no name and no address."""
        if not fields:
            return True
        if fields.get("order_ids"):
            return False
        if str(fields.get("order_id") or "").strip():
            return False
        if str(fields.get("name") or "").strip():
            return False
        if str(fields.get("address") or "").strip():
            return False
        return True

    @staticmethod
    def _looks_like_order(text: str) -> bool:
        """True when a screenshot really is an **order** screen.

        The customers also send plain product pictures (a photo of the frame
        they ordered, a saree, the packaging).  Those are not failed orders,
        so they must never end up in the failed list.  Only a picture that
        carries an order marker — the order screen's own headings, the
        delivery block, the order timeline — counts as an order that went
        wrong.

        A product page (a price, a "BUY 1" button, the product name) is
        deliberately *not* a marker: it appears on both kinds of picture.
        """
        blob = (text or "").lower()
        if not blob.strip():
            return False
        return any(marker in blob for marker in _ORDER_SCREEN_MARKERS)

    @staticmethod
    def _looks_like_product_page(text: str) -> bool:
        """True when the picture is a product / checkout page, not an order.

        Such a page prints the saved delivery address just like an order does,
        so it would otherwise be written out as a legitimate ``ADDR:`` row —
        even though the chat contains no order at all.  A real order screen
        wins: any of the strong order markers cancels the product verdict.
        """
        blob = (text or "").lower()
        if not blob.strip():
            return False
        if any(marker in blob for marker in _STRONG_ORDER_MARKERS):
            return False
        return any(marker in blob for marker in _PRODUCT_PAGE_MARKERS)

    def _copy_failed_image(self, internal_id: int, image_path: str) -> Optional[str]:
        """Copy a screenshot that yielded nothing into ``failed/``.

        The file is grouped per chat (``failed/<internal id>/<name>.jpg``)
        so the seller can see which conversation the picture came from.
        Returns the destination path, or ``None`` when it could not be
        copied.
        """
        if not image_path or not self.failed_dir:
            return None
        try:
            folder = os.path.join(self.failed_dir, str(internal_id))
            os.makedirs(folder, exist_ok=True)
            name = os.path.basename(image_path)
            dest = os.path.join(folder, name)
            if os.path.abspath(image_path) != os.path.abspath(dest):
                shutil.copyfile(image_path, dest)
            self.failed_images += 1
            self.log(
                f"  no order id, name or address — saved to failed/: {name}"
            )
            return dest
        except Exception as exc:  # pragma: no cover - diagnostics must not crash
            self.log(f"  could not save the failed image: {exc}")
            return None

    def _aggregate(
        self,
        per_image: List[Tuple[str, Dict[str, Any]]],
        chat_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine the per-image extractions into the chat's result.

        A chat can carry many orders, so the result holds **every** order id
        that was found — one output row each.  The order ids keep the order of
        the screenshots; a cut-off id (``TRUNC:…``) is kept as it is, and the
        customer name / address only matter when no order id was found at all.
        """
        sources: List[Tuple[str, Dict[str, Any]]] = list(per_image)
        sources.append(("", chat_fields))

        order_ids: List[str] = []
        truncated: List[Tuple[str, str]] = []
        rows: List[Dict[str, Any]] = []
        name = ""
        address = ""
        source = ""
        confidence = 0.0
        ambiguous = False
        ambiguous_candidates = 0
        partial = False
        review_image = ""

        for path, fields in sources:
            if not fields:
                continue
            ftype = str(fields.get("type") or "").upper()
            if not source:
                source = str(fields.get("source") or "")
            confidence = max(confidence, float(fields.get("confidence") or 0.0))
            if fields.get("ambiguous"):
                ambiguous = True
            ambiguous_candidates += int(fields.get("ambiguous_candidates") or 0)
            if fields.get("partial"):
                partial = True

            # The customer details that belong to *this* source (screenshot).
            src_name = str(fields.get("name") or "").strip()
            src_address = str(fields.get("address") or "").strip()

            if ftype == "OK":
                values = [str(v).strip() for v in (fields.get("order_ids") or [])]
                if not values and fields.get("order_id"):
                    values = [str(fields["order_id"]).strip()]
                for value in values:
                    if value and value not in order_ids:
                        order_ids.append(value)
                        rows.append({
                            "value": value,
                            "status": "OK",
                            "partial": bool(fields.get("partial")),
                            # The name / address of the customer this very
                            # order belongs to — not the chat's average.
                            # Empty in "single" mode: that mode keeps one
                            # value per row.
                            "name": src_name,
                            "address": src_address,
                        })
            elif ftype == "TRUNC":
                value = str(fields.get("order_id") or "").strip()
                if value and value not in [v for v, _p in truncated]:
                    truncated.append((value, path))
                    partial = True
                    if path and not review_image:
                        review_image = path
                    rows.append({
                        "value": value,
                        "status": "TRUNC",
                        "partial": True,
                        "name": src_name,
                        "address": src_address,
                    })
            elif ftype == "FALLBACK_NAME" and not name:
                name = src_name
            elif ftype == "FALLBACK_ADDRESS" and not address:
                address = src_address

            # A chat whose screenshots carried a name / address *next to* an
            # order id still has those details — keep them as the chat-level
            # fallback so a writer without per-row support does not lose them.
            if src_name and not name:
                name = src_name
            if src_address and not address:
                address = src_address

        result: Dict[str, Any] = {
            "order_id": "",
            "order_ids": [],
            "truncated_ids": [],
            "name": name,
            "address": address,
            "type": "FAILED",
            "confidence": confidence,
            "source": source,
            "ambiguous": ambiguous,
            "ambiguous_candidates": ambiguous_candidates,
            "partial": partial,
            "review_image": review_image,
            "order_rows": [],
        }

        if order_ids:
            result["type"] = "OK"
        elif truncated:
            result["type"] = "TRUNC"
        elif name:
            result["type"] = "FALLBACK_NAME"
        elif address:
            result["type"] = "FALLBACK_ADDRESS"

        if not self.multi_order_output and len(rows) > 1:
            # One row per chat (the classic single-value mode): the first
            # *complete* id wins, a cut-off one only when there is nothing
            # else.  The rest stay in the chat for a multi-order run.
            complete = [row for row in rows if row["status"] == "OK"]
            rows = complete[:1] if complete else rows[:1]
            result["ambiguous"] = True
            ambiguous_candidates = max(
                ambiguous_candidates, len(order_ids) + len(truncated)
            )

        result["order_ids"] = [row["value"] for row in rows if row["status"] == "OK"]
        result["truncated_ids"] = [row["value"] for row in rows if row["status"] == "TRUNC"]
        result["order_id"] = (
            result["order_ids"][0]
            if result["order_ids"]
            else (result["truncated_ids"][0] if result["truncated_ids"] else "")
        )
        result["ambiguous_candidates"] = ambiguous_candidates
        result["order_rows"] = rows
        return result

    def _download_image(self, chat_id: str, msg: Dict[str, Any], internal_id: int) -> Optional[str]:
        """Download one image message into ``assets/<id>/`` and return its path.

        The file is stored as ``assets/<id>/<message_id><ext>`` where ``ext``
        comes from ``msg["media"]["mimetype"]`` (see :func:`pick_extension`).
        Downloading itself is delegated to the API client's
        :meth:`download_media(msg)`, which fetches the WAHA ``media.url``.
        """
        msg_id = message_id(msg) or f"msg{int(time.time() * 1000)}"
        folder = os.path.join(self.assets_dir, str(internal_id))
        os.makedirs(folder, exist_ok=True)
        safe_name = str(msg_id).replace("/", "_").replace("\\", "_")
        path = os.path.join(folder, safe_name + pick_extension(msg))
        try:
            data = self.api.download_media(msg)
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            self.log(f"  media download failed for message {msg_id}: {exc}")
            return None
        if not os.path.exists(path):
            self.log(f"  media download produced no file for message {msg_id}")
            return None
        logger.debug(
            "Saved media for message %s -> %s", msg_id, path,
        )
        return path

    def _download_images(
        self,
        chat_id: str,
        messages: List[Dict[str, Any]],
        internal_id: int,
    ) -> List[tuple]:
        """Download several image messages, preserving order.

        A chat with multiple photos (family pictures next to the one order
        screenshot) is fetched concurrently with a small thread pool so the
        batch is not dominated by serial round-trips.  The returned list pairs
        every input message with its path (or ``None`` on failure), in the same
        order as ``messages``, so callers stay deterministic.
        """
        if not messages:
            return []
        if len(messages) == 1:
            return [(messages[0], self._download_image(chat_id, messages[0], internal_id))]

        max_workers = min(4, len(messages))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._download_image, chat_id, msg, internal_id)
                for msg in messages
            ]
            results: List[tuple] = []
            for msg, future in zip(messages, futures):
                try:
                    results.append((msg, future.result()))
                except Exception as exc:  # pragma: no cover - defensive
                    self.log(f"  media download failed: {exc}")
                    results.append((msg, None))
        return results

    # ------------------------------------------------------------------
    # Single id
    # ------------------------------------------------------------------
    def process_one(self, internal_id: int, reprocess: bool = False) -> ProcessResult:
        """Run the pipeline for one internal id (never raises)."""
        try:
            return self._process_one(internal_id, reprocess)
        except Exception as exc:  # pragma: no cover - defensive catch-all
            logger.exception("Unexpected error processing id %s", internal_id)
            return ProcessResult(
                internal_id=internal_id,
                status=STATUS_FAILED,
                message="unexpected error",
                error=str(exc),
            )

    def _process_one(self, internal_id: int, reprocess: bool) -> ProcessResult:
        # --- 1. state DB --------------------------------------------------
        if not reprocess and self.state is not None and self.state.is_processed(internal_id):
            return ProcessResult(
                internal_id=internal_id,
                status=STATUS_SKIPPED,
                message="already processed",
                skipped=True,
            )

        # --- 2. locate the chat ------------------------------------------
        ok, chat, err = self._fetch_chat(internal_id)
        if not ok:
            return ProcessResult(
                internal_id=internal_id,
                status=STATUS_FAILED,
                message="api error",
                error=err,
            )
        if chat is None:
            return ProcessResult(
                internal_id=internal_id,
                status=STATUS_MISSING,
                message="chat not found",
            )

        chat_id = self._chat_id(chat)
        result = ProcessResult(internal_id=internal_id, status=STATUS_FAILED)

        # --- 3. messages ---------------------------------------------------
        messages: List[Dict[str, Any]] = []
        if chat_id:
            try:
                messages = self.api.get_messages(
                    chat_id, limit=self.max_messages_per_chat
                ) or []
            except ChatNotFoundError:
                # The chat exists but its messages endpoint 404s.  Nothing was
                # attempted, so do not count it as processed — surface it under
                # MISSING so the operator can see it.
                return ProcessResult(
                    internal_id=internal_id,
                    status=STATUS_MISSING,
                    chat_id=chat_id,
                    message="messages unavailable",
                    skipped=True,
                )
            except WhatsAppAPIError as exc:
                # A fetch/transport failure — transient.  Do NOT persist to the
                # state DB; the id is retried automatically on the next run.
                return ProcessResult(
                    internal_id=internal_id,
                    status=STATUS_TRANSIENT_ERROR,
                    chat_id=chat_id,
                    message="api error (transient)",
                    error=str(exc),
                    transient=True,
                )

        # --- 4. text pass --------------------------------------------------
        # Inspect every incoming text message in the fetched window: a customer
        # may type a labelled Order ID (e.g. "Order Id: 3422…") and send no
        # screenshot at all, and the id can sit anywhere in the window.
        text_messages = extract_text_messages(messages, limit=self.max_messages_per_chat)
        whatsapp_text = normalize("\n".join(text_messages))
        result.chat_id = chat_id

        # --- 5. image + OCR pass ------------------------------------------
        # One chat may hold several orders across several screenshots, so every
        # available image is downloaded *first* (in original message order) and
        # then scanned.  We never stop at the first complete order id — a later
        # image may hold another order, the customer's name or the address.
        #
        # Every image is extracted **on its own**: one screenshot carries one
        # order, and merging all the OCR text into one blob lets the label of
        # one screenshot swallow the digits of the next one.
        images = extract_image_messages(messages)
        # Only the *customer's* pictures are order screenshots.  A picture the
        # seller sent is counted (so the run summary can say it was skipped)
        # and never downloaded or OCR'd.
        self.outgoing_images_skipped += sum(
            1 for m in messages if is_image_message(m) and m.get("fromMe")
        )
        ocr_chunks: List[str] = []
        image_paths: List[str] = []
        per_image: List[Tuple[str, Dict[str, Any]]] = []
        media_failures = 0
        ocr_failures = 0
        stopped_early = False

        if images:
            if not chat_id:
                result.message = "no chat id for media download"
            else:
                selected = (
                    images if self.images_per_chat <= 0
                    else images[: self.images_per_chat]
                )
                downloads = self._download_images(chat_id, selected, internal_id)
                for _msg, path in downloads:
                    if self.control.should_stop():
                        stopped_early = True
                        break
                    if not path:
                        media_failures += 1
                        continue
                    result.images += 1
                    image_paths.append(path)
                    text, failed = self._ocr_image(path)
                    if failed:
                        ocr_failures += 1
                    if not text:
                        continue
                    ocr_chunks.append(text)
                    fields = self._extract(text)
                    if not self._has_order_id(fields) and self.focus_pass:
                        # No order id in the full page — look at the header
                        # once more, enlarged.  Never fatal.
                        focus_text = self._focus_image(path)
                        if focus_text:
                            focus_fields = self._extract(focus_text)
                            if self._has_order_id(focus_fields):
                                ocr_chunks.append(focus_text)
                                fields = focus_fields
                    if (not self._has_order_id(fields)
                            and self._looks_like_product_page(text)):
                        # A product / checkout page.  It prints the saved
                        # delivery address too, so it would otherwise become
                        # an ADDR: row — but the chat has no order in it.
                        self.skipped_pictures += 1
                        continue
                    if self._image_yielded_nothing(fields):
                        # Neither an order id, nor a name, nor an address.
                        # The image still goes to the failed list — but only
                        # when it really looks like an *order* screen: a
                        # customer often sends a picture of the product (a
                        # photo frame, a saree) and that is not a failed
                        # order, it is just a picture.
                        if self._looks_like_order(text):
                            self._copy_failed_image(internal_id, path)
                        else:
                            self.skipped_pictures += 1
                    elif not self._has_order_id(fields):
                        # A name or an address, but no order id.  That is a
                        # perfectly good result (the order id is what the
                        # seller asked for, so it is counted separately).
                        self.no_id_images += 1
                    per_image.append((path, fields))

        ocr_text = normalize("\n".join(ocr_chunks))
        # The WhatsApp chat text is never OCR'd — it is its own source.
        chat_fields = self._extract(whatsapp_text, is_text=True)
        final_result = self._aggregate(per_image, chat_fields)

        if not final_result.get("order_ids") and not images:
            # Nothing to read at all — leave it for the seller but still write a row.
            if not whatsapp_text and not ocr_text:
                return ProcessResult(
                    internal_id=internal_id,
                    status=STATUS_NO_IMAGE,
                    chat_id=chat_id,
                    message="no text and no image messages",
                    timestamp=result.timestamp,
                )

        # A stop request or a media / OCR failure leaves some images unscanned:
        # the result is written as PARTIAL but *not* marked processed, so a
        # later run re-scans it (idempotent writers keep it duplicate-free).
        incomplete = (media_failures + ocr_failures) > 0 or stopped_early

        # Every image download failed and nothing usable was extracted: this is
        # a transient transport failure, not a real FAILED.  Don't write the row
        # and don't mark the id processed — it is retried on the next run.
        if (
            media_failures
            and result.images == 0
            and self._map_status(final_result.get("type")) == STATUS_FAILED
        ):
            return ProcessResult(
                internal_id=internal_id,
                status=STATUS_TRANSIENT_ERROR,
                chat_id=chat_id,
                images=0,
                message="media download failed",
                error="media download failed",
                transient=True,
                timestamp=result.timestamp,
            )

        if incomplete:
            if self._map_status(final_result.get("type")) == STATUS_FAILED:
                # Some images could not be read and nothing at all was found —
                # let the whole chat retry rather than persisting a FAILED row.
                return ProcessResult(
                    internal_id=internal_id,
                    status=STATUS_TRANSIENT_ERROR,
                    chat_id=chat_id,
                    images=result.images,
                    message="some images could not be read",
                    error="partial media / OCR failure",
                    transient=True,
                    timestamp=result.timestamp,
                )
            # Some images failed but others yielded results: write what we have
            # under PARTIAL.  It is *not* marked processed, so a later run
            # retries the missing images; the idempotent writers keep the retry
            # duplicate-free and never lose already-extracted ids.
            final_result = dict(final_result)
            final_result["type"] = STATUS_PARTIAL
            result.incomplete = True
            if stopped_early and not (media_failures + ocr_failures):
                result.message = "stopped: some images were not scanned"
            else:
                result.message = "partial: some images could not be read"
                self.log(
                    f"  {media_failures} media / {ocr_failures} OCR failure(s) — "
                    "writing partial result, will retry"
                )

        return self._finalise(
            result,
            final_result,
            images=result.images,
            chat_id=chat_id,
            ocr_text=ocr_text,
            whatsapp_text=whatsapp_text,
            image_paths=image_paths,
        )

    def _fetch_chat(self, internal_id: int):
        """Return ``(ok, chat_or_None, error_or_None)`` for one id."""
        try:
            chat = self.api.get_chat_by_name(str(internal_id))
            return True, chat, None
        except ChatNotFoundError:
            return True, None, None
        except WhatsAppAPIError as exc:
            self.log(f"{internal_id}: API error while locating chat: {exc}")
            return False, None, str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            self.log(f"{internal_id}: unexpected error while locating chat: {exc}")
            return False, None, str(exc)

    def _save_raw_ocr(self, internal_id: int, raw_text: str, status: str) -> Optional[str]:
        """Persist the raw OCR text for TRUNC / FAILED ids to ``logs/<id>_raw.txt``."""
        if not self.logs_dir:
            return None
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            path = os.path.join(self.logs_dir, f"{internal_id}_raw.txt")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"# internal_id: {internal_id}\n")
                fh.write(f"# status: {status}\n")
                fh.write(f"# saved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                fh.write("# ---\n")
                fh.write(raw_text or "")
                if raw_text and not raw_text.endswith("\n"):
                    fh.write("\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            return path
        except Exception as exc:  # pragma: no cover - diagnostics must not crash
            self.log(f"{internal_id}: could not save raw OCR log: {exc}")
            return None

    def _copy_review_image(self, internal_id: int, image_path: Optional[str]) -> Optional[str]:
        """Copy the source image for a TRUNC id to ``review/<id>.jpg``."""
        if not image_path or not self.review_dir:
            return None
        if not os.path.exists(image_path):
            return None
        try:
            os.makedirs(self.review_dir, exist_ok=True)
            dest = os.path.join(self.review_dir, f"{internal_id}.jpg")
            if os.path.abspath(image_path) != os.path.abspath(dest):
                shutil.copyfile(image_path, dest)
            return dest
        except Exception as exc:  # pragma: no cover - diagnostics must not crash
            self.log(f"{internal_id}: could not copy review image: {exc}")
            return None

    def _finalise(
        self,
        result: ProcessResult,
        extraction: Dict[str, Any],
        images: int,
        chat_id: Optional[str],
        ocr_text: str = "",
        whatsapp_text: str = "",
        image_paths: Optional[List[str]] = None,
    ) -> ProcessResult:
        """Write the row(s), then mark state (only when the write succeeded)."""
        status = self._map_status(extraction.get("type"))

        # Collect every distinct order id for this chat (multi-order support).
        order_ids = [
            str(x).strip()
            for x in (extraction.get("order_ids") or [])
            if str(x).strip()
        ]
        single = str(extraction.get("order_id") or "")
        if not order_ids and single and not single.upper().startswith("TRUNC:"):
            order_ids = [single]
        deduped: List[str] = []
        seen: set = set()
        for order_id in order_ids:
            if order_id not in seen:
                seen.add(order_id)
                deduped.append(order_id)
        order_ids = deduped

        result.status = status
        result.order_ids = order_ids
        result.order_id = order_ids[0] if order_ids else single
        result.name = str(extraction.get("name") or "")
        result.address = str(extraction.get("address") or "")
        result.source = str(extraction.get("source") or "")
        result.confidence = float(extraction.get("confidence") or 0.0)
        result.images = images
        result.chat_id = chat_id
        result.ambiguous = bool(extraction.get("ambiguous"))
        result.partial = bool(extraction.get("partial"))
        result.raw_text = "\n".join(part for part in (ocr_text, whatsapp_text) if part)
        if status == STATUS_FAILED:
            result.message = result.message or "nothing extracted"
            # Nothing at all could be read from the chat: no order id, no name
            # and no address.  Say so loudly - the terminal colours any line
            # containing "ERROR" red, and the log keeps the ERROR record.
            logger.error(
                "event=nothing_extracted internal_id=%s images=%s",
                result.internal_id,
                images,
            )
            self.log(
                f"{result.internal_id}: ERROR nothing extracted - "
                "no order id, no name and no address"
            )

        # --- side artefacts (TRUNC / FAILED / PARTIAL diagnostics) -------
        if status in (STATUS_TRUNC, STATUS_FAILED, STATUS_PARTIAL) or result.partial:
            self._save_raw_ocr(result.internal_id, result.raw_text, status)
        if status == STATUS_TRUNC or result.ambiguous or result.partial:
            # A cut-off id, an ambiguous id and a cropped (partial) id all need a
            # human to confirm the order — copy the source screenshot into
            # review/ for the seller.  The image that actually produced the
            # cut-off id is preferred over the first one of the chat.
            first_image = (
                str(extraction.get("review_image") or "")
                or (image_paths or [None])[0]
            )
            result.review_path = self._copy_review_image(result.internal_id, first_image)
        if result.ambiguous:
            logger.warning(
                "event=ambiguous_order_id internal_id=%s candidates=%s",
                result.internal_id,
                extraction.get("ambiguous_candidates", 0),
            )

        # --- dry run: do not write output, do not touch state ------------
        if self.dry_run:
            result.dry_run = True
            result.message = (result.message + " [dry-run]").strip()
            return result

        # --- 7. write output ---------------------------------------------
        write_ok = True
        try:
            self.writer.write_result(result.internal_id, extraction)
        except Exception as exc:
            write_ok = False
            result.status = STATUS_FAILED
            result.error = f"write failed: {exc}"
            self.log(f"{result.internal_id}: could not write output row: {exc}")

        # The seller's id-only Notepad list — the same rows in the plain
        # "internal_id,value" format, written next to the detailed output.
        # A failure here never invalidates the main write.
        if self.notepad_writer is not None:
            fill_mode = str(getattr(self.notepad_writer, "mode", "") or "") == "fill"
            has_line = getattr(self.notepad_writer, "has_line", None)
            if (
                fill_mode
                and callable(has_line)
                and not has_line(result.internal_id)
            ):
                # The master list is authoritative: the seller builds it with
                # the day's chat ids already in it.  A missing id means the
                # list and the run range are out of sync - do not invent a new
                # "extra" line, say so instead.
                self.log(
                    f"{result.internal_id}: WARN not in the Notepad list - "
                    "not filled (check the file has this id)"
                )
            try:
                self.notepad_writer.write_result(result.internal_id, extraction)
            except Exception as exc:
                self.log(f"{result.internal_id}: could not write the Notepad row: {exc}")
            notice = str(getattr(self.notepad_writer, "last_notice", "") or "")
            if notice:
                # Several orders in one chat: the list still got a value (the
                # name that was sent, or the first id), but the row is flagged
                # in the app so the seller can check it.  This is a warning,
                # not a failure - the notepad line is filled.  The kind says
                # *which* check is needed ("MULTI: names differ" / "name
                # missing" / "no name") in the terminal, the log and the
                # Orders table.
                kind = str(
                    getattr(self.notepad_writer, "last_notice_kind", "") or ""
                )
                result.ambiguous = True
                if kind:
                    result.review_reason = kind
                logger.warning(
                    "event=multi_order_flagged internal_id=%s kind=%s detail=%s",
                    result.internal_id,
                    kind,
                    notice,
                )
                self.log(f"{result.internal_id}: WARN {notice}")

        # --- 8. mark state (after a successful write only) ---------------
        # A PARTIAL result (some images could not be read) is written but not
        # persisted, so the chat is retried and the missing images re-fetched.
        if write_ok and result.written and not result.incomplete and self.state is not None:
            try:
                self.state.mark_processed(
                    result.internal_id,
                    {
                        "type": result.status,
                        "order_id": result.order_id,
                        "order_ids": result.order_ids,
                        "name": result.name,
                        "address": result.address,
                        "source": result.source,
                        "confidence": result.confidence,
                    },
                    timestamp=result.timestamp,
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.log(f"{result.internal_id}: could not update state DB: {exc}")

        return result

    # ------------------------------------------------------------------
    # Whole run
    # ------------------------------------------------------------------
    def run(
        self,
        start_id: int,
        end_id: int,
        reprocess: bool = False,
        dry_run: Optional[bool] = None,
    ) -> RunSummary:
        """Process ids ``start_id … end_id`` inclusive and return a summary."""
        start_id, end_id = int(start_id), int(end_id)
        if end_id < start_id:
            start_id, end_id = end_id, start_id

        if dry_run is not None:
            self.dry_run = bool(dry_run)

        ids = list(range(start_id, end_id + 1))
        summary = RunSummary(total=len(ids), dry_run=self.dry_run)
        summary._range = f"{start_id}-{end_id}"  # type: ignore[attr-defined]
        summary.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        started = time.time()

        self.log(
            f"Starting run {start_id}-{end_id} ({len(ids)} chats, "
            f"reprocess={reprocess}, dry_run={self.dry_run})"
        )
        self.control.reset()

        consecutive_errors = 0
        try:
            for index, internal_id in enumerate(ids, start=1):
                if self.control.should_stop():
                    summary.stopped = True
                    self.log("Stop requested — ending run")
                    break
                if self.control.wait_if_paused():
                    summary.stopped = True
                    self.log("Stopped while paused — ending run")
                    break

                result = self.process_one(internal_id, reprocess=reprocess)
                summary.record(result)

                if result.error and not result.skipped:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

                self.log(result.summary_line())
                self._emit_progress(result, index, len(ids))

                if consecutive_errors >= self.max_consecutive_errors:
                    summary.stopped = True
                    self.log(
                        f"Aborting: {consecutive_errors} consecutive errors — "
                        "check the WhatsApp API connection."
                    )
                    break

                # --- 10. polite delay -------------------------------------
                if index < len(ids) and self.delay > 0:
                    if self.control.wait_if_paused():
                        summary.stopped = True
                        break
                    time.sleep(self.delay)
        finally:
            summary.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            summary.duration = time.time() - started
            self.log(
                f"Run finished: {summary.processed} processed, "
                f"{summary.successful} extracted, {summary.failed} failed, "
                f"{summary.missing} missing ({summary.duration:.1f}s)"
            )

        return summary

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Release the HTTP session, the state DB and any other resources."""
        try:
            if self.api is not None and hasattr(self.api, "close"):
                self.api.close()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            state = getattr(self, "state", None)
            if state is not None and hasattr(state, "close"):
                state.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "Orchestrator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Dict[str, Any],
        output_path: Optional[str] = None,
        output_format: Optional[str] = None,
        state_path: str = os.path.join("state", "state.db"),
        assets_dir: str = "assets",
        logs_dir: str = "logs",
        review_dir: str = "review",
        failed_dir: str = "failed",
        session_name: Optional[str] = None,
        state_scope: str = "",
        **kwargs: Any,
    ) -> "Orchestrator":
        """Build a fully-wired orchestrator from ``config.yaml``.

        The heavy imports (OCR engine, writers, state) happen here so the
        module itself stays light and easy to import in tests.

        ``session_name`` picks the WhatsApp account (the WAHA session) to
        collect from and ``state_scope`` namespaces the resume state, so
        every account is scanned independently even when the same internal
        id shows up in two of them.
        """
        from ocr.engine import build_engine
        from state.state import StateStore
        from whatsapp.api_client import WhatsAppAPIClient

        api = WhatsAppAPIClient.from_config(config, session_name=session_name)
        ocr = build_engine(
            config,
            cache_dir=(
                os.path.join(assets_dir, ".ocr_cache") if assets_dir else None
            ),
        )
        state = StateStore(state_path, scope=state_scope)

        out_cfg = config.get("output", {}) or {}
        path = output_path or out_cfg.get("default_file") or "orders.csv"
        fmt = (output_format or ("csv" if str(path).lower().endswith(".csv") else "notepad")).lower()
        writer = cls.build_writer(path, fmt, config)

        # The seller's plain "internal_id,value" Notepad list, written next
        # to the detailed output when the config names one.
        notepad_writer = None
        notepad_path = str(out_cfg.get("notepad_file") or "").strip()
        if notepad_path:
            if not os.path.isabs(notepad_path):
                notepad_path = os.path.join(os.getcwd(), notepad_path)
            from output.writer import NotepadWriter

            notepad_mode = str(
                out_cfg.get("notepad_mode") or "append"
            ).strip().lower()
            notepad_writer = NotepadWriter(notepad_path, mode=notepad_mode)

        return cls(
            api_client=api,
            ocr_engine=ocr,
            writer=writer,
            state=state,
            notepad_writer=notepad_writer,
            rules=Rules.from_config(config),
            config=config,
            assets_dir=assets_dir,
            logs_dir=logs_dir,
            review_dir=review_dir,
            failed_dir=failed_dir,
            **kwargs,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def build_writer(path: str, output_format: str, config: Optional[Dict[str, Any]] = None):
        """Return a :class:`NotepadWriter` or :class:`CSVWriter` for ``path``."""
        from output.csv_writer import CSVWriter
        from output.writer import NotepadWriter

        fmt = (output_format or "").lower()
        if fmt == "csv" or (not fmt and str(path).lower().endswith(".csv")):
            date_format = (config or {}).get("output", {}).get("date_format", "%Y-%m-%d %H:%M:%S")
            return CSVWriter(path, date_format=date_format)
        return NotepadWriter(path)
