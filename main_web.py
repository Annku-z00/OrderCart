"""
main_web.py
===========

OrderCart desktop GUI (PyWebview + HTML/CSS/JS "matte dark" dashboard).

Replaces the tkinter window in ``main_tk.py`` with a modern webview shell:

* ``webview.create_window`` loads ``ui/index.html`` (matte-dark + pastel UI).
* :class:`Api` is exposed to JavaScript as ``window.pywebview.api``.
* Pressing RUN in the UI calls :meth:`Api.start_extraction`, which spawns the
  orchestrator in a background thread so the window never freezes.
* Every log line / progress tick is pushed back into the UI with
  ``window.evaluate_js('appendLog(...)')`` / ``updateStats(...)`` /
  ``updateProgress(...)``.

Design notes (mirrors main_tk.py)
---------------------------------
* Heavy imports (the OCR stack) happen *inside* the worker thread on the first
  RUN, so the window opens instantly.
* Log messages are JSON-encoded into the ``evaluate_js`` snippet so quotes,
  newlines and currency symbols cannot break the JavaScript string literal.
* Disable the RunControl directly exposes Pause / Stop to the UI.

Run from source::

    py -3.12 main_web.py
"""

from __future__ import annotations

import base64
import csv
import json
import logging
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import webview

from core.orchestrator import RunControl
from frameflow_config import (
    dump_round_trip,
    ensure_dirs,
    load_config,
    load_round_trip,
    validate_config,
)
from output.corrections import apply_corrections
from output.writer import master_list_value
from updater import app_version
from updater import apply_update as apply_git_update
from updater import check_for_update

APP_TITLE = "OrderCart — WhatsApp Order Extractor"

#: project root (works from source and from a PyInstaller --onedir build)
def _app_root() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ROOT = _app_root()
APP_VERSION = app_version(ROOT)


def _ui_dir() -> str:
    """Locate the bundled ``ui/`` folder (PyInstaller unpacks extras in _MEIPASS)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.isdir(os.path.join(meipass, "ui")):
            return os.path.join(meipass, "ui")
        return os.path.join(ROOT, "ui")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def configure_logging(log_dir: str) -> logging.Logger:
    """Log to ``logs/ordercart.log`` (and console when running from source)."""
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        file_handler = logging.FileHandler(
            os.path.join(log_dir, "ordercart.log"), encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    if not getattr(sys, "frozen", False) and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    ):
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    return logging.getLogger("ordercart")


# ---------------------------------------------------------------------------
# Status counters for the KPI cards
# ---------------------------------------------------------------------------
_SUCCESS_STATUSES = {
    "OK", "TRUNC", "FALLBACK_NAME", "FALLBACK_ADDRESS", "PARTIAL",
}
_FAILED_STATUSES = {
    "FAILED", "MISSING", "NO_IMAGE", "TRANSIENT_ERROR",
}


class Api:
    """The Python ⇄ JavaScript bridge exposed as ``window.pywebview.api``."""

    def __init__(self) -> None:
        self.folders = ensure_dirs(ROOT)
        self.config: Dict[str, Any] = load_config(
            os.path.join(ROOT, "config.yaml")
        )
        self.logger = configure_logging(self.folders["logs"])

        # A broken regex / out-of-range number is reported now, not in the
        # middle of a long run.
        self._config_problems = validate_config(self.config)
        for problem in self._config_problems:
            self.logger.warning("config: %s", problem)

        self._control = RunControl()
        self._worker: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._paused = threading.Event()
        self._window: Optional[webview.Window] = None

        # cumulative KPI counters for this run
        self._counts = {"total": 0, "success": 0, "failed": 0}

        # ProcessResult.ambiguous / .partial flags of the current session,
        # keyed by internal_id — the Orders view's "Needs review" filter reads
        # them through get_orders().  Session-scoped by design: nothing here is
        # written to disk, so after a restart only the persisted TRUNC / FAILED
        # statuses still mark those rows.
        self._review_flags: Dict[str, Dict[str, bool]] = {}
        #: How many pictures the last run put into the ``failed/`` folder.
        self._failed_count = 0

        # Last GitHub update check (cached for an hour so opening the window
        # twice does not hammer the API).
        self._update_cache: Optional[Dict[str, Any]] = None
        self._update_checked_at = 0.0

        self.logger.info("OrderCart web started (root=%s)", ROOT)

    # ------------------------------------------------------------------
    # JS plumbing
    # ------------------------------------------------------------------
    def bind_window(self, window: webview.Window) -> None:
        self._window = window

    def _js(self, snippet: str) -> None:
        """Evaluate a JavaScript snippet, ignoring teardown-time errors."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(snippet)
        except Exception:  # window already closing / no event loop yet
            pass

    def _emit_log(self, message: str) -> None:
        """appendLog('<json-escaped message>') — safe for quotes/newlines."""
        self._js(f"appendLog({json.dumps(str(message))})")

    def _emit_stats(self, total: int, success: int, failed: int) -> None:
        self._js(f"updateStats({int(total)}, {int(success)}, {int(failed)})")

    def _emit_progress(self, index: int, total: int) -> None:
        """updateProgress(index, total) — the Dashboard progress bar (§4.4:
        both values are json-encoded so only literals ever reach evaluate_js)."""
        self._js(f"updateProgress({json.dumps(int(index))}, "
                 f"{json.dumps(int(total))})")

    def _emit_running(self) -> None:
        self._js(f"setRunning({str(self._running.is_set()).lower()}, "
                 f"{str(self._paused.is_set()).lower()})")

    # ------------------------------------------------------------------
    # Orders / data table API (called from JavaScript)
    # ------------------------------------------------------------------
    #: Columns sent to the UI table (subset of the CSV writer schema).
    _ORDER_COLUMNS = ("internal_id", "order_id", "name", "address", "status", "timestamp")

    def _output_file(self) -> str:
        """Absolute path of the configured output file (orders.csv / orders.txt)."""
        out_cfg = (self.config.get("output", {}) or {})
        default = out_cfg.get("default_file") or out_cfg.get("csv_file") or "orders.csv"
        if not os.path.isabs(default):
            default = os.path.join(ROOT, default)
        return default

    def _notepad_output_file(self) -> str:
        """Absolute path of the seller's plain "id,value" list ("" when unset)."""
        out_cfg = (self.config.get("output", {}) or {})
        name = str(out_cfg.get("notepad_file") or "").strip()
        if not name:
            return ""
        if not os.path.isabs(name):
            name = os.path.join(ROOT, name)
        return name

    @staticmethod
    def _normalise_row(row: Dict[str, str]) -> Dict[str, str]:
        """Reduce one CSV dict to the columns the table needs."""
        clean = {
            key: str(row.get(key) or "").strip() for key in Api._ORDER_COLUMNS
        }
        if not clean["status"]:
            clean["status"] = "OK" if clean["order_id"] else "UNKNOWN"
        return clean

    def _last_scanned_map(self) -> Dict[str, str]:
        """Map ``internal_id`` -> latest processed timestamp across accounts.

        The output file records when a row was *first written*, so a re-scan
        of an already-saved order keeps its original date.  The state DB is
        the only place that records when a chat was last scanned; the Orders
        view uses this map for its "Last scanned" date filter.

        Read-only and fail-safe: a missing/locked DB simply yields ``{}`` and
        the caller falls back to the row timestamp.
        """
        db_path = os.path.join(self.folders["state"], "state.db")
        if not os.path.isfile(db_path):
            return {}
        try:
            con = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=1.0
            )
            try:
                rows = con.execute(
                    "SELECT CAST(id AS TEXT), MAX(timestamp) FROM processed "
                    "WHERE timestamp IS NOT NULL AND timestamp != '' "
                    "GROUP BY id"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            self.logger.debug("last-scanned lookup failed: %s", exc)
            return {}
        return {str(row[0]): str(row[1]) for row in rows if row[0] is not None}

    def get_orders(self) -> list:
        """Read the extracted-orders file and return a list of row dicts.

        Columns: ``internal_id, order_id, name, address, status, timestamp``.
        Both output writers are supported:

        * ``orders.csv`` — read through :mod:`csv` (header row included in the
          file itself).
        * ``orders.txt`` (Notepad format ``internal_id,value``) — the value is
          decoded back into a status + order id (``TRUNC:…`` → TRUNC, etc.).
        Missing file → empty list; a malformed line is skipped, never fatal.
        """
        path = self._output_file()
        if not os.path.isfile(path):
            return []

        rows: list = []
        try:
            if path.lower().endswith(".csv"):
                with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        try:
                            rows.append(self._normalise_row(row))
                        except Exception:  # pragma: no cover - defensive
                            continue
            else:
                # Notepad format: "<internal_id>,<value>" where value may carry
                # a status prefix (TRUNC: / NAME: / ADDR:) or be "FAILED".
                with open(path, "r", encoding="utf-8-sig") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.lower().startswith("internal_id"):
                            continue
                        parts = line.split(",", 1)
                        if len(parts) != 2:
                            continue
                        internal_id, value = parts[0].strip(), parts[1].strip()
                        # Decode the Notepad value encoding (see
                        # output/writer.py: TRUNC:x / NAME:… / ADDR:… / FAILED).
                        if ":" in value:
                            prefix, _, rest = value.partition(":")
                            prefix = prefix.strip().upper()
                            if prefix == "TRUNC":
                                status, order_id, name, address = "TRUNC", value, "", ""
                            elif prefix == "NAME":
                                status, order_id = "FALLBACK_NAME", ""
                                name, address = rest.strip(), ""
                            elif prefix == "ADDR":
                                status, order_id = "FALLBACK_ADDRESS", ""
                                name, address = "", rest.strip()
                            else:  # plain value, no known prefix
                                status, order_id, name, address = "OK", value, "", ""
                        elif value.upper() == "FAILED":
                            status, order_id, name, address = "FAILED", "", "", ""
                        else:
                            status, order_id, name, address = "OK", value, "", ""
                        rows.append({
                            "internal_id": internal_id,
                            "order_id": order_id,
                            "name": name,
                            "address": address,
                            "status": status,
                            "timestamp": "",
                        })
        except Exception as exc:
            self.logger.warning("get_orders failed for %s: %s", path, exc)
            return []

        # Session-scoped ProcessResult flags so the Orders view can offer the
        # "Needs review" filter (ambiguous || partial || TRUNC || FAILED).
        for row in rows:
            flags = self._review_flags.get(str(row.get("internal_id") or ""), {})
            row["ambiguous"] = bool(flags.get("ambiguous", False))
            row["partial"] = bool(flags.get("partial", False))
            # Which check the row needs, when the run labelled it
            # ("MULTI: names differ" / "MULTI: name missing" / "MULTI: no name").
            row["review_reason"] = str(flags.get("reason", "") or "")

        # Latest scan date per internal id (state DB) — the Orders date
        # filter can use it even when the row's own timestamp is old.
        last_scanned = self._last_scanned_map()
        for row in rows:
            row["last_scanned"] = last_scanned.get(
                str(row.get("internal_id") or ""), ""
            )

        # Newest rows first if timestamps are present.  The original file
        # order breaks the tie (multi-order rows of one chat share a
        # timestamp), so the table never shuffles them between refreshes.
        for position, row in enumerate(rows):
            row["_position"] = position
        rows.sort(
            key=lambda r: (
                r.get("timestamp") or "",
                str(r.get("internal_id") or ""),
                -r["_position"],
            ),
            reverse=True,
        )
        for row in rows:
            row.pop("_position", None)
        return rows

    def apply_correction(
        self,
        internal_id: Any,
        corrected_value: Any,
        current_value: Any = None,
    ) -> Dict[str, Any]:
        """Rewrite one row of the output file with the seller's corrected Order ID.

        Called by the Orders view when an inline correction is saved (Enter).
        The actual file rewrite is delegated to
        :func:`output.corrections.apply_corrections` — that module is frozen and
        already tested, so this method only validates, logs and refreshes.

        Parameters
        ----------
        internal_id:
            The chat's internal id (``"12988"``) — the row key in the output file.
        corrected_value:
            The Order ID pasted by the seller.  An accidental ``TRUNC:`` prefix
            is stripped so the stored value is the bare id.
        current_value:
            The value the row holds right now.  A chat can carry several
            orders (one output row each), so this pins the correction to the
            one row the seller is actually looking at.

        Returns
        -------
        dict
            ``{"ok", "error"?, "changed", "internal_id", "order_id", "orders"}``
            where ``orders`` is the refreshed row list so the table can
            re-render immediately without a second round-trip.
        """
        iid = str(internal_id or "").strip()
        new_value = str(corrected_value or "").strip()
        if new_value.upper().startswith("TRUNC:"):
            new_value = new_value[6:].strip()

        def _fail(error: str) -> Dict[str, Any]:
            return {"ok": False, "error": error, "changed": 0,
                    "internal_id": iid, "order_id": "",
                    "orders": self.get_orders()}

        if not iid:
            return _fail("internal_id is required")
        if not new_value:
            return _fail("corrected value is empty")

        current = self.get_orders()
        old_value = next(
            (str(r.get("order_id") or "") for r in current
             if str(r.get("internal_id") or "") == iid),
            "",
        )
        match = str(current_value or "").strip() or None

        path = self._output_file()
        try:
            changed = apply_corrections(
                path,
                {iid: new_value},
                output_format=None,
                config=self.config,
                matches={iid: match} if match else None,
            )
        except Exception as exc:
            self.logger.warning("event=correction_failed internal_id=%s error=%s",
                                iid, exc)
            return _fail(str(exc))

        # The seller's plain Notepad list holds the same rows — keep it in
        # sync so the two files never disagree.  A failure here is logged
        # but never invalidates the correction that was just saved.
        notepad_path = self._notepad_output_file()
        if notepad_path and os.path.isfile(notepad_path):
            try:
                notepad_mode = str(
                    (self.config.get("output", {}) or {}).get("notepad_mode")
                    or "append"
                )
                apply_corrections(
                    notepad_path,
                    {iid: new_value},
                    output_format=None,
                    config=self.config,
                    matches={iid: match} if match else None,
                    notepad_mode=notepad_mode,
                )
            except Exception as exc:
                self.logger.warning(
                    "event=correction_notepad_failed internal_id=%s error=%s", iid, exc
                )

        # Required audit trail (logs/ordercart.log, INFO, source=ui).
        self.logger.info("event=correction internal_id=%s old=%s new=%s source=ui",
                         iid, old_value, new_value)

        return {
            "ok": True,
            "changed": int(changed),
            "internal_id": iid,
            "order_id": new_value,
            "orders": self.get_orders(),
        }

    def export_orders(self) -> Dict[str, Any]:
        """Open the folder that contains the output file in Explorer."""
        folder = os.path.dirname(self._output_file()) or ROOT
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "folder": folder}

    # ------------------------------------------------------------------
    # Export / clipboard (Orders view)
    # ------------------------------------------------------------------
    #: The columns an export carries — id, order id, name and address.
    #: No timestamp: the seller works with the order, not with the clock.
    _EXPORT_COLUMNS = ("internal_id", "order_id", "name", "address",
                       "status", "source", "confidence")

    #: The column titles the seller sees in the exported files.
    _EXPORT_HEADERS = {
        "internal_id": "Internal ID",
        "order_id": "Order ID",
        "name": "Name",
        "address": "Address",
        "status": "Status",
        "source": "Source",
        "confidence": "Confidence",
    }

    def _export_dir(self) -> str:
        """Folder the exported files are written to."""
        base = os.path.dirname(self._output_file()) or ROOT
        return os.path.join(base, "exports")

    def export_orders_as(self, fmt: Any = "xlsx") -> Dict[str, Any]:
        """Write the whole order list to a file the seller can use.

        Four formats, one click each:

        * ``xlsx``    - a real Excel workbook (one sheet, one row per order).
        * ``csv``     - the same rows as comma-separated values (Excel opens it).
        * ``txt``     - a plain, tab-separated table (``internal_id  order_id
          name  address ...``) that pastes straight into Excel.
        * ``notepad`` - the seller's **master-list** format: one line per
          internal id, a TAB, and the single value the notepad writer would
          put there (one order -> the id; two or more -> the name; else the
          address).  This is the file to hand to Notepad++.

        The rows come from :meth:`get_orders`, so the export always matches
        what the Orders table shows.  The file is written to
        ``<output folder>/exports/`` and then opened.
        """
        kind = str(fmt or "xlsx").strip().lower()
        if kind not in ("xlsx", "csv", "txt", "notepad"):
            return {"ok": False, "error": "unknown export format: %s" % kind}

        rows = self.get_orders()
        if not rows:
            return {"ok": False, "error": "there are no orders to export yet"}

        folder = self._export_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # The master list is a Notepad file: keep the .txt extension so it
        # opens in Notepad with a double click.
        extension = "txt" if kind == "notepad" else kind
        path = os.path.join(folder, "orders_%s.%s" % (stamp, extension))

        try:
            if kind == "xlsx":
                self._write_xlsx(path, rows)
            elif kind == "csv":
                self._write_csv(path, rows)
            elif kind == "notepad":
                self._write_notepad(path, rows)
            else:
                self._write_txt(path, rows)
        except Exception as exc:
            self.logger.warning("event=export_failed format=%s error=%s", kind, exc)
            return {"ok": False, "error": str(exc)}

        self.logger.info("event=export format=%s rows=%d path=%s", kind, len(rows), path)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            pass          # opening is a convenience, not a requirement
        return {"ok": True, "path": path, "rows": len(rows), "format": kind}

    def _export_rows(self, rows: list) -> List[Dict[str, str]]:
        """The export rows, every column present (empty string when absent)."""
        out: List[Dict[str, str]] = []
        for row in rows or []:
            item = {key: str(row.get(key) or "").strip() for key in self._EXPORT_COLUMNS}
            item["internal_id"] = str(row.get("internal_id") or "").strip()
            item["order_id"] = str(row.get("order_id") or "").strip()
            out.append(item)
        return out

    def _write_xlsx(self, path: str, rows: list) -> None:
        """Write a real Excel workbook (pandas + openpyxl)."""
        import pandas as pd

        frame = pd.DataFrame(self._export_rows(rows), columns=list(self._EXPORT_COLUMNS))
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="Orders")
            sheet = writer.sheets["Orders"]
            widths = {"A": 12, "B": 24, "C": 20, "D": 60, "E": 16,
                      "F": 10, "G": 12}
            for column, width in widths.items():
                sheet.column_dimensions[column].width = width

    def _write_csv(self, path: str, rows: list) -> None:
        """Write the rows as comma-separated values."""
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(self._EXPORT_COLUMNS))
            writer.writeheader()
            for item in self._export_rows(rows):
                writer.writerow(item)

    def _write_txt(self, path: str, rows: list) -> None:
        """Write a tab-separated plain-text list."""
        headers = [self._EXPORT_HEADERS[key] for key in self._EXPORT_COLUMNS]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\t".join(headers) + "\n")
            for item in self._export_rows(rows):
                fh.write("\t".join(item.get(key, "") for key in self._EXPORT_COLUMNS) + "\n")

    def _notepad_lines(self, rows: list) -> List[str]:
        """One master-list line per internal id: ``<id><TAB><one value>``.

        The same rule as the in-place notepad writer (``output.writer``): a
        chat with one order keeps its id; two or more orders write the customer
        name, else the address, never an order id.  A chat with nothing
        readable keeps the bare id so the line is still there to fill later.
        """
        grouped: Dict[str, list] = {}
        for row in rows or []:
            internal_id = str(row.get("internal_id") or "").strip()
            if not internal_id:
                continue
            grouped.setdefault(internal_id, []).append(row)

        def _sort_key(internal_id: str):
            """Ascending ids, like the seller's own list (numbers first)."""
            return (0, int(internal_id), "") if internal_id.isdigit() else (
                1, 0, internal_id
            )

        lines: List[str] = []
        # The Orders table is sorted newest-first; a master list is read in
        # id order, so the export writes 1265, 1266, 1267 ...
        for internal_id in sorted(grouped, key=_sort_key):
            value, _notice, _kind = master_list_value(grouped[internal_id])
            lines.append(f"{internal_id}\t{value}" if value else internal_id)
        return lines

    def _write_notepad(self, path: str, rows: list) -> None:
        """Write the master list exactly as the app fills it (id + TAB + value)."""
        lines = self._notepad_lines(rows)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))

    def copy_to_clipboard(self, text: Any) -> Dict[str, Any]:
        """Put ``text`` on the Windows clipboard.

        The browser's clipboard API is the primary route; this is the
        guaranteed fallback for hosts where it is blocked.  It uses the
        standard library only (a hidden ``tkinter`` root owns the clipboard).
        """
        payload = str(text or "")
        if not payload:
            return {"ok": False, "error": "nothing to copy"}
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            try:
                root.clipboard_clear()
                root.clipboard_append(payload)
                root.update()
            finally:
                root.destroy()
            return {"ok": True, "length": len(payload)}
        except Exception as exc:
            self.logger.warning("event=clipboard_failed error=%s", exc)
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Shipping labels (the dedicated Labels view)
    # ------------------------------------------------------------------
    #: Mirrors ``frameflow_config.DEFAULTS["labels"]`` so an older config.yaml
    #: without the section still gets a usable label template.
    _LABEL_DEFAULTS: Dict[str, Any] = {
        "brand": "MEESHO",
        "seller_name": "VEER HANUMAN CRAFTS",
        "seller_address": "",
        "seller_phone": "",
        "seller_gstin": "",
        "footer": "Thank you for shopping with us!",
        "show_barcode": True,
        "copies": 1,
        "width_in": 4,
        "height_in": 6,
    }

    @staticmethod
    def _label_text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _label_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))

    @staticmethod
    def _label_float(value: Any, default: float, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))

    @staticmethod
    def _label_bool(value: Any, default: bool = True) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")

    def _label_settings(self, values: Any = None) -> Dict[str, Any]:
        """Validated label settings, merged over ``config.yaml``."""
        data: Dict[str, Any] = dict(self._LABEL_DEFAULTS)
        configured = self.config.get("labels")
        if isinstance(configured, dict):
            data.update(configured)
        if isinstance(values, dict):
            data.update(values)
        return {
            "brand": self._label_text(data.get("brand"), 24) or "MEESHO",
            "seller_name": self._label_text(data.get("seller_name"), 80),
            "seller_address": self._label_text(data.get("seller_address"), 300),
            "seller_phone": self._label_text(data.get("seller_phone"), 24),
            "seller_gstin": self._label_text(data.get("seller_gstin"), 24),
            "footer": self._label_text(data.get("footer"), 100),
            "show_barcode": self._label_bool(data.get("show_barcode"), True),
            "copies": self._label_int(data.get("copies"), 1, 1, 50),
            "width_in": self._label_float(data.get("width_in"), 4.0, 2.0, 10.0),
            "height_in": self._label_float(data.get("height_in"), 6.0, 2.0, 10.0),
        }

    def get_label_settings(self) -> Dict[str, Any]:
        """The seller details / layout used by the Labels view."""
        return self._label_settings()

    def set_collect_mode(self, mode: Any) -> Dict[str, Any]:
        """Persist ``extraction.collect`` (``"single"`` / ``"all"``).

        Shipping labels need the customer name + address in the row, which is
        what the ``"all"`` mode writes.  The Labels view offers this as an
        explicit one-click fix instead of silently changing extraction.
        """
        value = str(mode or "").strip().lower()
        if value not in ("single", "all"):
            return {"ok": False, "error": "collect must be 'single' or 'all'"}
        self.config.setdefault("extraction", {})["collect"] = value
        self._persist_config(collect=value)
        self.logger.info("event=collect_mode mode=%s source=labels", value)
        return {"ok": True, "collect": value}

    def save_label_settings(self, settings: Any = None) -> Dict[str, Any]:
        """Persist the Labels view fields to ``config.yaml``."""
        values = self._label_settings(settings)
        self.config["labels"] = values
        self._persist_config(labels=values)
        self.logger.info("event=label_settings saved")
        return {"ok": True, "labels": values}

    def generate_labels(self, rows: Any = None, options: Any = None) -> Dict[str, Any]:
        """Render a 4x6 PDF of shipping labels and open it.

        ``rows`` is the list of order dicts ticked in the Labels view.  When it
        is omitted the whole order list is used.  ``options`` carries the
        view's seller fields; they are saved so the next batch starts with the
        same values.
        """
        if rows is None:
            rows = self.get_orders()
        if not isinstance(rows, list) or not rows:
            return {"ok": False, "error": "there are no visible orders to label"}

        clean: List[Dict[str, str]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            clean.append({
                "internal_id": str(item.get("internal_id") or ""),
                "order_id": str(item.get("order_id") or ""),
                "name": str(item.get("name") or ""),
                "address": str(item.get("raw_address") or item.get("address") or ""),
                "timestamp": str(item.get("timestamp") or ""),
            })
        if not clean:
            return {"ok": False, "error": "there are no visible orders to label"}

        values = self._label_settings(options)
        self.config["labels"] = values
        self._persist_config(labels=values)

        folder = self._export_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(folder, "labels_%s.pdf" % stamp)
        suffix = 2
        while os.path.exists(path):
            path = os.path.join(folder, "labels_%s_%d.pdf" % (stamp, suffix))
            suffix += 1

        try:
            from labels.generator import generate_labels_pdf

            summary = generate_labels_pdf(clean, path, values)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("event=labels_failed error=%s", exc)
            return {"ok": False, "error": str(exc)}

        self.logger.info(
            "event=labels rows=%d labels=%d pages=%d skipped=%d path=%s",
            len(clean), summary["labels"], summary["pages"],
            summary["skipped"], path,
        )
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            pass          # opening the PDF is a convenience, not a requirement
        return {
            "ok": True,
            "path": path,
            "rows": len(clean),
            "labels": summary["labels"],
            "pages": summary["pages"],
            "skipped": summary["skipped"],
            "incomplete": summary["incomplete"],
        }

    def open_labels_folder(self) -> Dict[str, Any]:
        """Open the exports folder (where the label PDFs land)."""
        folder = self._export_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "folder": folder}

    # ------------------------------------------------------------------
    # Public API (called from JavaScript)
    # ------------------------------------------------------------------
    def get_settings(self) -> Dict[str, Any]:
        """Current WhatsApp API settings (for the Settings view)."""
        api_cfg = (self.config.get("whatsapp_api", {}) or {})
        return {
            "provider": api_cfg.get("provider", "waha"),
            "base_url": api_cfg.get("base_url", ""),
            "api_key": api_cfg.get("api_key", ""),
            "session_name": api_cfg.get("session_name", "default"),
            "collect": (self.config.get("extraction", {}) or {}).get("collect", "single"),
            # Non-fatal problems found in config.yaml (shown by the UI).
            "config_problems": list(self._config_problems),
            # Installed version + the GitHub update preferences.
            "version": APP_VERSION,
            "update": self._update_config(),
        }

    # ------------------------------------------------------------------
    # WhatsApp accounts (one account = one WAHA session)
    # ------------------------------------------------------------------
    def _configured_accounts(self) -> List[Dict[str, Any]]:
        """The WhatsApp accounts the seller wants to collect from.

        Normalised to ``[{"name", "label", "enabled"}]``.  A config without
        the ``accounts`` list keeps working: it means the single
        ``session_name`` account.
        """
        api_cfg = (self.config.get("whatsapp_api", {}) or {})
        raw = api_cfg.get("accounts") or []
        accounts: List[Dict[str, Any]] = []
        seen = set()
        for item in raw:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            label = str(item.get("label") or name).strip() or name
            accounts.append({
                "name": name,
                "label": label,
                "enabled": bool(item.get("enabled", True)),
            })
        if not accounts:
            name = str(api_cfg.get("session_name") or "default").strip() or "default"
            accounts.append({"name": name, "label": name, "enabled": True})
        return accounts

    def _enabled_accounts(self) -> List[Dict[str, Any]]:
        """Only the accounts the seller ticked."""
        return [a for a in self._configured_accounts() if a.get("enabled")]

    def _session_statuses(self) -> Dict[str, str]:
        """The live WAHA status of every session, keyed by name."""
        from whatsapp.api_client import WhatsAppAPIClient

        try:
            client = WhatsAppAPIClient.from_config(self.config)
            sessions = client.list_sessions()
            client.close()
        except Exception:
            return {}
        return {str(s.get("name") or ""): str(s.get("status") or "") for s in sessions}

    def check_connection(self) -> Dict[str, Any]:
        """Ping the WhatsApp API so the UI can show an offline banner.

        Returns ``{"ok": True, "base_url", "sessions"}`` or
        ``{"ok": False, "base_url", "error"}``.  Never raises.
        """
        from whatsapp.api_client import WhatsAppAPIClient

        base_url = str(
            ((self.config.get("whatsapp_api", {}) or {}).get("base_url") or "")
        )
        try:
            client = WhatsAppAPIClient.from_config(self.config)
        except Exception as exc:
            return {"ok": False, "base_url": base_url, "error": str(exc)}
        try:
            error = client.check_connection()
            if error:
                return {"ok": False, "base_url": base_url, "error": error}
            sessions = client.list_sessions()
        except Exception as exc:
            return {"ok": False, "base_url": base_url, "error": str(exc)}
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass
        return {"ok": True, "base_url": base_url, "sessions": len(sessions)}

    # ------------------------------------------------------------------
    # Online updates (GitHub)
    # ------------------------------------------------------------------
    #: A successful check is reused for an hour; the Settings button forces one.
    _UPDATE_TTL_SECONDS = 3600

    def _update_config(self) -> Dict[str, Any]:
        """The ``update:`` block of config.yaml, defaults filled in."""
        cfg = self.config.get("update") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "repo": str(cfg.get("repo") or "Annku-z00/OrderCart").strip(),
            "branch": str(cfg.get("branch") or "main").strip() or "main",
            "check_on_start": bool(cfg.get("check_on_start", True)),
        }

    def _push_update(self, payload: Dict[str, Any]) -> None:
        """setUpdateInfo({...}) — the banner + Settings card read this."""
        self._js(f"setUpdateInfo({json.dumps(payload)})")

    def check_updates(self, force: Any = False) -> Dict[str, Any]:
        """Check GitHub for a newer commit (cached, never raises).

        ``force`` comes from JavaScript, so it is normalised rather than
        trusted.  A disabled check answers ``{"ok": False, "disabled": True}``
        so the UI can stay quiet.
        """
        cfg = self._update_config()
        if not cfg["enabled"]:
            return {"ok": False, "disabled": True, "version": APP_VERSION}

        now = time.time()
        if (
            not bool(force)
            and self._update_cache is not None
            and (now - self._update_checked_at) < self._UPDATE_TTL_SECONDS
        ):
            return self._update_cache

        result = check_for_update(
            repo=cfg["repo"], branch=cfg["branch"], root=ROOT
        )
        result["version"] = APP_VERSION
        self._update_cache = result
        self._update_checked_at = now

        if not result.get("ok"):
            self.logger.warning(
                "event=update_check_failed error=%s", result.get("error")
            )
        elif result.get("update_available"):
            self.logger.info(
                "event=update_available current=%s latest=%s branch=%s",
                result.get("current_short") or "?",
                result.get("latest_short") or "?",
                result.get("branch"),
            )
        else:
            self.logger.info(
                "event=update_check_up_to_date current=%s",
                result.get("current_short") or "?",
            )

        self._push_update(result)
        return result

    def apply_update(self) -> Dict[str, Any]:
        """Update now: ``git pull --ff-only`` when this folder is a clone."""
        if self._running.is_set():
            return {
                "ok": False,
                "error": "Stop the current run before updating.",
            }
        cfg = self._update_config()
        result = apply_git_update(root=ROOT, branch=cfg["branch"])
        if result.get("ok"):
            self.logger.info("event=update_applied branch=%s", cfg["branch"])
            self._emit_log(
                "Update downloaded - close and reopen OrderCart to use it."
            )
        else:
            self.logger.warning(
                "event=update_failed error=%s", result.get("error")
            )
        return result

    def open_repo(self) -> Dict[str, Any]:
        """Open the project page (the download link for a built copy)."""
        url = "https://github.com/" + self._update_config()["repo"]
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except Exception:
            import webbrowser

            webbrowser.open(url)
        return {"ok": True, "url": url}

    def get_whatsapp_accounts(self) -> Dict[str, Any]:
        """The account manager: the configured accounts + the live WAHA list.

        The live list carries each session's status (``WORKING``,
        ``SCAN_QR_CODE`` …) and the WhatsApp name behind it, so the seller
        can see at a glance which accounts are ready.
        """
        from whatsapp.api_client import WhatsAppAPIClient

        api_cfg = (self.config.get("whatsapp_api", {}) or {})
        sessions: List[Dict[str, Any]] = []
        try:
            client = WhatsAppAPIClient.from_config(self.config)
            sessions = client.list_sessions()
            client.close()
        except Exception as exc:
            self.logger.warning("Could not read the WAHA sessions: %s", exc)

        live = {str(s.get("name") or ""): s for s in sessions}
        accounts: List[Dict[str, Any]] = []
        for account in self._configured_accounts():
            item = dict(account)
            info = live.get(account["name"]) or {}
            item["status"] = str(info.get("status") or "")
            item["me"] = str(info.get("me") or "")
            item["known"] = account["name"] in live
            accounts.append(item)

        # A session the server knows but the config does not list yet — the
        # seller can tick it straight away.
        for name, info in live.items():
            if not any(a["name"] == name for a in accounts):
                accounts.append({
                    "name": name,
                    "label": name,
                    "enabled": False,
                    "status": str(info.get("status") or ""),
                    "me": str(info.get("me") or ""),
                    "known": True,
                })

        return {
            "accounts": accounts,
            "sessions": sessions,
            "base_url": api_cfg.get("base_url", ""),
            "api_key": api_cfg.get("api_key", ""),
        }

    def add_whatsapp_account(self, name: Any, label: Any = "") -> Dict[str, Any]:
        """Add a WhatsApp account: create it on the server and list it here.

        The server session is created with the configuration the engine needs
        and is started right away, so its QR code is available immediately —
        the seller never has to touch the WAHA dashboard.
        """
        from whatsapp.api_client import WhatsAppAPIClient

        session = str(name or "").strip()
        if not session:
            return {"ok": False, "error": "give the account a session name"}
        title = str(label or "").strip() or session

        try:
            client = WhatsAppAPIClient.from_config(self.config)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        try:
            created = client.create_session(session)
            if created:
                client.start_session(session)
            sessions = client.list_sessions()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            client.close()

        # Add it to the list the app keeps.
        accounts = self._configured_accounts()
        for account in accounts:
            if account["name"] == session:
                account["label"] = title
                account["enabled"] = True
                break
        else:
            accounts.append({"name": session, "label": title, "enabled": True})
        self.config.setdefault("whatsapp_api", {})
        self.config["whatsapp_api"]["accounts"] = accounts
        self._persist_config(accounts=accounts)

        status = ""
        for item in sessions:
            if str(item.get("name") or "") == session:
                status = str(item.get("status") or "")
        return {
            "ok": True,
            "name": session,
            "label": title,
            "status": status,
            "accounts": accounts,
        }

    def get_account_qr(self, session: Any) -> Dict[str, Any]:
        """The WhatsApp QR code for one account, as a PNG data URL.

        The seller scans it with the phone: *WhatsApp → Linked devices → Link
        a device*.  A session that is already linked has no QR — the status
        then says so.

        A WhatsApp pairing code only lives for a short while.  When it
        expires the session stops, so a dead session is started again here
        and the fresh code is waited for.
        """
        from whatsapp.api_client import WhatsAppAPIClient

        name = str(session or "").strip()
        if not name:
            return {"ok": False, "error": "no account given"}

        try:
            client = WhatsAppAPIClient.from_config(self.config)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        try:
            content = b""
            for attempt in range(4):
                try:
                    resp = client._request("GET", f"/api/{name}/auth/qr", retries=0)
                    content = resp.content or b""
                    if content:
                        break
                except Exception:
                    content = b""
                # No code (or a dead session) — restart it and wait for a
                # fresh one.  A restart is needed: a session whose pairing
                # code expired only comes back with one.
                client.restart_session(name)
                time.sleep(8)

            if not content:
                return {
                    "ok": False,
                    "session": name,
                    "error": "this account has no QR code — it is probably linked already.",
                }
            encoded = base64.b64encode(content).decode("ascii")
            return {
                "ok": True,
                "session": name,
                "data_url": "data:image/png;base64," + encoded,
            }
        except Exception as exc:
            return {
                "ok": False,
                "session": name,
                "error": "this account has no QR code — it is probably linked already.",
                "detail": str(exc),
            }
        finally:
            client.close()

    def save_whatsapp_accounts(self, accounts: Any) -> Dict[str, Any]:
        """Persist the account list and which ones are ticked."""
        if isinstance(accounts, str):
            try:
                accounts = json.loads(accounts)
            except (json.JSONDecodeError, TypeError):
                return {"ok": False, "error": "the account list is not valid JSON"}
        if not isinstance(accounts, list):
            return {"ok": False, "error": "the account list must be a list"}

        cleaned: List[Dict[str, Any]] = []
        seen = set()
        for item in accounts:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            label = str(item.get("label") or name).strip() or name
            cleaned.append({
                "name": name,
                "label": label,
                "enabled": bool(item.get("enabled", False)),
            })
        if not cleaned:
            return {"ok": False, "error": "keep at least one WhatsApp account."}

        self.config.setdefault("whatsapp_api", {})
        self.config["whatsapp_api"]["accounts"] = cleaned
        # The first ticked account becomes the default session, so the
        # single-account code path keeps working.
        ticked = [a for a in cleaned if a["enabled"]]
        default_session = ticked[0]["name"] if ticked else cleaned[0]["name"]
        self.config["whatsapp_api"]["session_name"] = default_session
        self._persist_config(accounts=cleaned, session_name=default_session)
        self.logger.info(
            "event=accounts_saved count=%d enabled=%s",
            len(cleaned), ",".join(a["name"] for a in ticked) or "-",
        )
        return {"ok": True, "accounts": cleaned}

    def save_settings(self, base_url: str, api_key: str, session: str,
                      collect: Any = None) -> Dict[str, Any]:
        """Persist the Settings view back into ``config.yaml`` (best effort).

        ``collect`` is the ``extraction.collect`` mode — ``"single"`` (one
        value per row: the order id, else the name, else the address) or
        ``"all"`` (the order id *and* the name *and* the address together).
        When it is not given the current mode is kept.
        """
        values = {
            "base_url": str(base_url or "").strip(),
            "api_key": str(api_key or ""),
            "session_name": str(session or "").strip() or "default",
        }
        if not values["base_url"]:
            return {"ok": False, "error": "API URL must not be empty."}

        self.config.setdefault("whatsapp_api", {})
        self.config["whatsapp_api"].update(values)

        collect_mode = None
        if collect is not None:
            mode = str(collect or "single").strip().lower()
            if mode not in ("single", "all"):
                return {"ok": False, "error": "collect must be 'single' or 'all'."}
            self.config.setdefault("extraction", {})
            self.config["extraction"]["collect"] = mode
            collect_mode = mode

        self._persist_config(
            base_url=values["base_url"],
            api_key=values["api_key"],
            session_name=values["session_name"],
            **({"collect": collect_mode} if collect_mode else {}),
        )
        self.logger.info("Settings saved: %s session=%s collect=%s",
                         values["base_url"], values["session_name"],
                         (self.config.get("extraction", {}) or {}).get("collect"))
        return {"ok": True}

    def start_extraction(self, start_id: Any, end_id: Any, dry_run: Any,
                         reprocess: Any = False) -> Dict[str, Any]:
        """RUN button. Validates the range and starts the worker thread."""
        if self._running.is_set():
            return {"ok": False, "error": "A run is already in progress."}

        try:
            start, end = int(str(start_id).strip()), int(str(end_id).strip())
        except (TypeError, ValueError):
            self._emit_log("ERROR: Start ID and End ID must be whole numbers.")
            return {"ok": False, "error": "Start/End ID must be whole numbers."}

        if end < start:
            start, end = end, start
        if (end - start) > 5000:
            self._emit_log(f"WARN: {end - start + 1} chats — that can take a long time.")

        dry_run = bool(dry_run)
        reprocess = bool(reprocess)
        out_cfg = (self.config.get("output", {}) or {})
        output = out_cfg.get("default_file") or "orders.csv"
        if not os.path.isabs(output):
            output = os.path.join(ROOT, output)
        output_format = "csv" if output.lower().endswith(".csv") else "notepad"

        # Fresh control so a previous stop never bleeds into this run.
        self._control = RunControl()
        self._counts = {"total": 0, "success": 0, "failed": 0}
        # The review flags belong to the run that produced them.  A fresh run
        # (especially a Reprocess that fixes a row) must not inherit the
        # "Needs review" marks of the previous one.
        self._review_flags.clear()
        self._running.set()
        self._paused.clear()
        self._emit_running()
        self._emit_stats(0, 0, 0)

        self._worker = threading.Thread(
            target=self._worker_main,
            args=(start, end, output, output_format, dry_run, reprocess),
            name="OrderCartWorker",
            daemon=True,
        )
        self._worker.start()
        self.logger.info(
            "Run started %s-%s (dry_run=%s, reprocess=%s)",
            start, end, dry_run, reprocess,
        )
        return {"ok": True}

    def pause(self) -> Dict[str, Any]:
        """Pause / resume toggle."""
        if not self._running.is_set():
            return {"ok": False}
        paused = self._control.toggle_pause()
        if paused:
            self._paused.set()
            self._emit_log("— Paused — press Resume to continue —")
        else:
            self._paused.clear()
            self._emit_log("— Resumed —")
        self._emit_running()
        return {"ok": True, "paused": paused}

    def stop(self) -> Dict[str, Any]:
        """Stop the run after the current chat finishes."""
        if not self._running.is_set():
            return {"ok": False}
        self._control.request_stop()
        self._emit_log("— Stop requested — finishing the current chat —")
        return {"ok": True}

    def open_logs_folder(self) -> Dict[str, Any]:
        path = self.folders["logs"]
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # pragma: no cover - platform dependent
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def get_failed_info(self) -> Dict[str, Any]:
        """How many pictures the last run put into ``failed/``, and where."""
        folder = self.folders.get("failed") or os.path.join(ROOT, "failed")
        return {"count": int(self._failed_count or 0), "folder": folder}

    def open_failed_folder(self) -> Dict[str, Any]:
        """Open the ``failed/`` folder in Explorer."""
        folder = self.folders.get("failed") or os.path.join(ROOT, "failed")
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "folder": folder, "count": int(self._failed_count or 0)}

    # ------------------------------------------------------------------
    # Single-image "Test Image" API (called from JavaScript)
    # ------------------------------------------------------------------
    #: Maps the extractor's result ``type`` onto the badge labels used by the
    #: Test Image view (``OK`` / ``TRUNC`` / ``NAME`` / ``ADDR`` / ``FAILED``).
    _STATUS_LABELS = {
        "OK": "OK",
        "TRUNC": "TRUNC",
        "FALLBACK_NAME": "NAME",
        "FALLBACK_ADDRESS": "ADDR",
        "FAILED": "FAILED",
    }

    def select_image(self) -> Optional[str]:
        """Open a native file picker and return the chosen image path.

        Returns ``None`` when the user cancels the dialog — the UI treats that
        as a no-op and shows no error.
        """
        window = self._window or (webview.windows[0] if webview.windows else None)
        if window is None:
            return None
        try:
            selection = window.create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=(
                    "Image Files (*.jpg;*.jpeg;*.png)",
                    "All Files (*.*)",
                ),
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            self.logger.warning("select_image failed: %s", exc)
            return None

        if not selection:
            return None
        if isinstance(selection, (list, tuple)):
            return str(selection[0]) if selection else None
        return str(selection)

    def get_image_data_url(self, image_path: str) -> Optional[str]:
        """Return a base64 ``data:`` URL for the preview ``<img>``.

        The dashboard is served from pywebview's internal HTTP server, from
        which Chromium blocks ``file://`` sub-resources, so the preview cannot
        point at the raw path.  A data URL renders from any origin.
        """
        path = str(image_path or "").strip()
        if not path or not os.path.isfile(path):
            return None
        try:
            mime = mimetypes.guess_type(path)[0] or "image/png"
            with open(path, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
        except Exception as exc:
            self.logger.warning("get_image_data_url failed for %s: %s", path, exc)
            return None

    def test_image(self, image_path: str) -> Dict[str, Any]:
        """OCR + extract a single user-picked image.

        The heavy work runs on a background thread (so the window stays
        responsive while the JavaScript promise is pending) and the finished
        result is handed straight back to the UI.

        Always returns a dict: either the requested keys
        (``success/raw_text/confidence/order_id/name/address/status``) or
        ``{"success": False, "error": "..."}``.
        """
        path = str(image_path or "").strip()
        if not path:
            return {"success": False, "error": "No image selected."}
        if not os.path.isfile(path):
            return {"success": False, "error": f"File not found: {path}"}

        box: Dict[str, Any] = {}
        done = threading.Event()

        def _work() -> None:
            try:
                box.update(self._run_image_test(path))
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.exception("test_image failed")
                box.update({"success": False, "error": str(exc)})
            finally:
                done.set()

        threading.Thread(
            target=_work, name="OrderCartTestImage", daemon=True
        ).start()

        if not done.wait(timeout=300):
            return {"success": False, "error": "Timed out while processing the image."}
        return box or {"success": False, "error": "Unknown error."}

    def _run_image_test(self, path: str) -> Dict[str, Any]:
        """Worker body: run OCR on ``path`` then the field extractor."""
        # Imported lazily so launching the app stays fast (the OCR stack loads
        # on first use).
        from extractor.parser import extract_fields
        from extractor.rules import Rules
        from ocr.engine import build_engine

        ocr_cfg = (self.config.get("ocr", {}) or {})
        upscale = int(ocr_cfg.get("upscale_factor", 1) or 1)

        self._emit_log(f"Test Image → OCR: {os.path.basename(path)}")
        engine = build_engine(
            self.config,
            cache_dir=os.path.join(self.folders["assets"], ".ocr_cache"),
        )
        ocr = engine.run_on_file(path, upscale_factor=upscale)

        # Capture the *full* raw OCR text in logs/ordercart.log so a real
        # screenshot's text can be retrieved (the Test Image panel shows it, but
        # it cannot be reliably copied out of the webview).
        raw_text = ocr.text or ""
        self.logger.info(
            "event=test_image_ocr image=%s chars=%d raw=%s",
            os.path.basename(path),
            len(raw_text),
            json.dumps(raw_text, ensure_ascii=False),
        )

        if ocr.error and not (ocr.text or "").strip():
            self._emit_log(f"ERROR: OCR failed — {ocr.error}")
            return {"success": False, "error": f"OCR failed: {ocr.error}"}

        rules = Rules.from_config(self.config)
        fields = extract_fields(raw_text, "", rules)

        # The same priority the run uses: the order id first, else the
        # customer name, else the address.  In the "all" mode the details are
        # added as well, so the panel shows the complete picture.
        if (self.config.get("extraction", {}) or {}).get("collect") == "all":
            if not fields.get("name") or not fields.get("address"):
                from extractor.parser import find_customer_details

                name, address = find_customer_details(raw_text, rules)
                if name and not fields.get("name"):
                    fields["name"] = name
                if address and not fields.get("address"):
                    fields["address"] = address

        raw_type = str(fields.get("type") or "FAILED").upper()
        status = self._STATUS_LABELS.get(raw_type, "FAILED")

        # Record the extractor's decision too, so the log explains *why* an id
        # was or was not emitted (label tried, partial flag, ambiguity).
        self.logger.info(
            "event=test_image_extract image=%s status=%s order_id=%r partial=%s "
            "ambiguous=%s source=%s",
            os.path.basename(path),
            status,
            fields.get("order_id") or "",
            bool(fields.get("partial")),
            bool(fields.get("ambiguous")),
            fields.get("source") or "",
        )

        self._emit_log(
            "Test Image → %s (order_id=%s, ocr_conf=%.2f)"
            % (status, fields.get("order_id") or "—", float(ocr.confidence or 0.0))
        )

        return {
            "success": True,
            "raw_text": raw_text,
            "confidence": round(float(ocr.confidence or 0.0), 4),
            "order_id": str(fields.get("order_id") or ""),
            "name": str(fields.get("name") or ""),
            "address": str(fields.get("address") or ""),
            "status": status,
        }

    # ------------------------------------------------------------------
    # Config persistence (same pattern as main_tk.py)
    # ------------------------------------------------------------------
    def _persist_config(self, **fields: Any) -> None:
        """Write the given settings back into ``config.yaml`` (best effort).

        Only the named fields are written — the file on disk is the source of
        truth for everything else.  Writing the whole in-memory snapshot
        instead would silently undo a change made to the file after the app
        was started.

        When ``ruamel.yaml`` is installed the file is rewritten as a
        *round-trip* document, so the guidance comments in ``config.yaml``
        survive the first Save.
        """
        path = os.path.join(ROOT, "config.yaml")
        doc = load_round_trip(path)
        if doc is not None:
            try:
                for key, value in fields.items():
                    if key == "collect":
                        doc.setdefault("extraction", {})["collect"] = value
                    elif key == "labels" and isinstance(value, dict):
                        doc.setdefault("labels", {}).update(value)
                    else:
                        doc.setdefault("whatsapp_api", {})[key] = value
                if dump_round_trip(path, doc):
                    return
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.warning("Could not save config.yaml: %s", exc)

        try:
            import yaml
        except ImportError:  # pragma: no cover
            return
        try:
            cfg = load_config(path)
            for key, value in fields.items():
                if key == "collect":
                    cfg.setdefault("extraction", {})["collect"] = value
                elif key == "labels" and isinstance(value, dict):
                    cfg.setdefault("labels", {}).update(value)
                else:
                    cfg.setdefault("whatsapp_api", {})[key] = value
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("Could not save config.yaml: %s", exc)

    # ------------------------------------------------------------------
    # Worker (mirrors main_tk.py — heavy imports inside the thread)
    # ------------------------------------------------------------------
    def _worker_main(self, start: int, end: int, output: str, output_format: str,
                     dry_run: bool, reprocess: bool = False) -> None:
        try:
            accounts = self._enabled_accounts()
            if not accounts:
                self._emit_log("ERROR: no WhatsApp account is ticked — "
                                "pick one in Settings.")
                return

            self._emit_log("Loading OCR engine (first run may take a moment)…")
            if dry_run:
                self._emit_log("DRY RUN — nothing will be written and no state recorded.")
            if reprocess:
                self._emit_log(
                    "REPROCESS — chats that were already done are scanned again "
                    "(the output rows are written again, duplicates are skipped)."
                )
            if len(accounts) > 1:
                self._emit_log(
                    "Collecting from %d WhatsApp account(s): %s"
                    % (len(accounts), ", ".join(a["label"] for a in accounts))
                )

            self._failed_count = 0
            statuses = self._session_statuses()
            for index, account in enumerate(accounts):
                if self._control.should_stop():
                    self._emit_log("— Stopped before the next account —")
                    break

                # An account that is not linked yet cannot be scanned — say so
                # and move on instead of failing the whole run.
                status = str(statuses.get(account["name"], "") or "").upper()
                if not status:
                    # The session is not on the server at all — create it so
                    # the seller only has to scan its QR code.
                    from whatsapp.api_client import WhatsAppAPIClient

                    try:
                        client = WhatsAppAPIClient.from_config(self.config)
                        client.create_session(account["name"])
                        client.start_session(account["name"])
                        client.close()
                        self._emit_log(
                            f"— {account['label']} ({account['name']}) was not on "
                            "the server — created it. Link it in Settings."
                        )
                    except Exception as exc:
                        self._emit_log(
                            f"ERROR: could not create the account "
                            f"{account['name']} — {exc}"
                        )
                    continue
                if status and status not in ("WORKING", "STARTED", "STARTING"):
                    self._emit_log(
                        f"— {account['label']} ({account['name']}) is not linked "
                        f"yet ({status}) — skipping it. Link it in Settings."
                    )
                    continue

                if index:
                    self._emit_log("")
                    self._emit_log(
                        "═══ %s  (%s) ═══" % (account["label"], account["name"])
                    )
                self._run_one_account(
                    account, start, end, output, output_format, dry_run, reprocess
                )
        except Exception as exc:
            self.logger.exception("Worker crashed")
            self._emit_log(f"ERROR: {exc}")
            self._emit_log("Hint: check the WhatsApp API (WAHA/Evolution) is running "
                           "and logs/ordercart.log for details.")
        finally:
            self._control = RunControl()  # fresh for the next run
            self._running.clear()
            self._paused.clear()
            self._emit_running()

    def _run_one_account(self, account: Dict[str, Any], start: int, end: int,
                         output: str, output_format: str, dry_run: bool,
                         reprocess: bool) -> None:
        """Run one WhatsApp account (one WAHA session) over the range."""
        from core.orchestrator import Orchestrator

        orch = Orchestrator.from_config(
            self.config,
            output_path=output,
            output_format=output_format,
            state_path=os.path.join(self.folders["state"], "state.db"),
            assets_dir=os.path.join(self.folders["assets"]),
            logs_dir=os.path.join(self.folders["logs"]),
            review_dir=os.path.join(self.folders["review"]),
            failed_dir=os.path.join(self.folders["failed"]),
            session_name=account["name"],
            # Every account gets its own slice of the resume state, so an id
            # that was already done for one account is still scanned for the
            # next one.
            state_scope=account["name"],
            dry_run=dry_run,
            control=self._control,
        )
        orch.log_callback = self._emit_log
        orch.progress_callback = self._on_orchestrator_progress

        summary = orch.run(start, end, reprocess=reprocess, dry_run=dry_run)
        self._emit_stats(summary.processed, summary.order_id_count,
                         summary.failed + summary.missing + summary.no_image)

        self._emit_log("")
        for line in summary.report().splitlines():
            self._emit_log(line)

        review_count = 0
        try:
            review_count = len(summary.review_rows())
        except Exception:
            pass
        if review_count:
            self._emit_log(
                f"— {review_count} id(s) need review (TRUNC/FAILED) — "
                "check review/ folder."
            )

        # Screenshots that carried no id, no name and no address.
        failed_images = int(getattr(orch, "failed_images", 0) or 0)
        skipped_pictures = int(getattr(orch, "skipped_pictures", 0) or 0)
        no_id_images = int(getattr(orch, "no_id_images", 0) or 0)
        outgoing_images = int(getattr(orch, "outgoing_images_skipped", 0) or 0)
        self._failed_count += failed_images
        if failed_images:
            self._emit_log(
                f"— {failed_images} picture(s) had no order id, name or "
                "address — check the failed/ folder."
            )
        if skipped_pictures:
            self._emit_log(
                f"— {skipped_pictures} picture(s) were not order screens "
                "(product photos) — skipped."
            )
        if no_id_images:
            self._emit_log(
                f"- {no_id_images} picture(s) had no order id (a name or "
                "an address was found instead)."
            )
        if outgoing_images:
            self._emit_log(
                f"- {outgoing_images} picture(s) you sent were skipped "
                "(only incoming screenshots are scanned)."
            )

        orch.close()
        self.logger.info(
            "Run finished for account %s in %.1fs (processed=%d, ok=%d, failed=%d)",
            account["name"], summary.duration, summary.processed,
            summary.successful, summary.failed,
        )

    def _on_orchestrator_progress(self, result, index: int, total: int) -> None:
        """Called once per processed chat — pushes live stats to the KPI cards."""
        self._emit_progress(index, total)
        if result.skipped:
            return
        self._counts["total"] += 1
        status = str(getattr(result, "status", "") or "").upper()
        if status in _SUCCESS_STATUSES:
            # The KPI is labelled "Order IDs extracted": a chat that carried
            # five screenshots must add five, not one.  A name / address
            # fallback still produced one output row.
            self._counts["success"] += len(getattr(result, "order_ids", []) or []) or 1
        elif status in _FAILED_STATUSES:
            self._counts["failed"] += 1
        self._emit_stats(self._counts["total"], self._counts["success"], self._counts["failed"])

        # Remember the review flags for the Orders "Needs review" filter.
        try:
            iid = str(getattr(result, "internal_id", "") or "").strip()
            if iid:
                self._review_flags[iid] = {
                    "ambiguous": bool(getattr(result, "ambiguous", False)),
                    "partial": bool(getattr(result, "partial", False)),
                    # "MULTI: names differ" / "MULTI: name missing" /
                    # "MULTI: no name" - shown next to the status badge so the
                    # seller knows which check the row needs.
                    "reason": str(getattr(result, "review_reason", "") or ""),
                }
        except Exception:  # pragma: no cover - defensive, never break a run
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    api = Api()
    ui_index = os.path.join(_ui_dir(), "index.html")

    window = webview.create_window(
        APP_TITLE,
        ui_index,
        js_api=api,
        width=1200,
        height=800,
        min_size=(980, 640),
        background_color="#0B0C10",
    )
    api.bind_window(window)
    webview.start(
        debug=bool(os.environ.get("ORDERCART_DEV")),
        icon=os.path.join(_ui_dir(), "app.ico"),
    )


if __name__ == "__main__":
    main()
