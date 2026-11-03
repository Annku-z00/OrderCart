"""
main.py
=======

OrderCart desktop GUI (tkinter).

One window, one button.  The seller types the internal id range, picks the
output file + format, checks the WhatsApp API URL / key (pre-filled from
``config.yaml``) and hits **RUN**.

Design notes
------------
* The processing loop runs in a background :class:`threading.Thread`; the GUI
  thread never blocks.  All communication back to the UI goes through a
  :class:`queue.Queue` which is polled with ``after()`` — the standard tkinter
  pattern.
* Heavy imports (OpenCV) happen **inside** the worker thread the
  first time RUN is pressed, so the window opens instantly.
* Pause / Resume and Stop are delivered to the worker through the shared
  :class:`core.orchestrator.RunControl` object.
* Everything the user types is written back into ``config.yaml`` on RUN so the
  saved settings survive a restart.

Run from source with::

    python main.py
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

from frameflow_config import ensure_dirs, load_config
from core.orchestrator import RunControl

APP_TITLE = "OrderCart - WhatsApp Order Extractor"
APP_VERSION = "1.0"
POLL_INTERVAL_MS = 120

#: project root (works both from source and from a PyInstaller one-file build)
def _app_root() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ROOT = _app_root()


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def configure_logging(log_dir: str) -> logging.Logger:
    """Log to ``logs/ordercart.log`` and to the console (when not frozen)."""
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
# The GUI
# ---------------------------------------------------------------------------
class OrderCartApp(tk.Tk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()

        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("900x680")
        self.minsize(820, 600)

        self.folders = ensure_dirs(ROOT)
        self.config: Dict[str, Any] = load_config(os.path.join(ROOT, "config.yaml"))
        self.logger = configure_logging(self.folders["logs"])

        # --- worker plumbing ---
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._control = RunControl()
        self._summary = None
        self._running = False
        self._paused = False

        # --- tkinter variables ---
        self.start_var = tk.StringVar(value="12988")
        self.end_var = tk.StringVar(value="13010")
        out_cfg = self.config.get("output", {}) or {}
        default_out = out_cfg.get("default_file", "orders.txt")
        if not os.path.isabs(default_out):
            default_out = os.path.join(ROOT, default_out)
        self.output_var = tk.StringVar(value=default_out)
        self.format_var = tk.StringVar(
            value="csv" if default_out.lower().endswith(".csv") else "notepad"
        )
        api_cfg = self.config.get("whatsapp_api", {}) or {}
        self.provider_var = tk.StringVar(value=api_cfg.get("provider", "waha"))
        self.url_var = tk.StringVar(value=api_cfg.get("base_url", "http://localhost:3000"))
        self.key_var = tk.StringVar(value=api_cfg.get("api_key", ""))
        self.session_var = tk.StringVar(value=api_cfg.get("session_name", "default"))
        self.reprocess_var = tk.BooleanVar(value=False)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_var = tk.DoubleVar(value=0.0)

        # --- review panel state ---
        self._review_window: Optional[tk.Toplevel] = None
        self._review_entries: Dict[int, tk.Entry] = {}

        self._build_widgets()
        self._poll_queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.logger.info("OrderCart started (root=%s)", ROOT)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        pad = {"padx": 6, "pady": 4}
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # --- range + output -------------------------------------------
        box = ttk.LabelFrame(outer, text="Order range & output")
        box.pack(fill="x", pady=(0, 8))

        ttk.Label(box, text="Start ID:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.start_var, width=12).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(box, text="End ID:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.end_var, width=12).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(box, text="Format:").grid(row=0, column=4, sticky="w", **pad)
        ttk.Radiobutton(box, text="Notepad", value="notepad", variable=self.format_var).grid(row=0, column=5, **pad)
        ttk.Radiobutton(box, text="CSV", value="csv", variable=self.format_var).grid(row=0, column=6, **pad)

        ttk.Label(box, text="Output file:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.output_var).grid(row=1, column=1, columnspan=5, sticky="ew", **pad)
        ttk.Button(box, text="Browse…", command=self._browse_output).grid(row=1, column=6, sticky="e", **pad)

        ttk.Checkbutton(
            box,
            text="Reprocess IDs already marked done",
            variable=self.reprocess_var,
        ).grid(row=2, column=0, columnspan=4, sticky="w", **pad)

        ttk.Checkbutton(
            box,
            text="Dry run (no output, no state change)",
            variable=self.dry_run_var,
        ).grid(row=2, column=4, columnspan=3, sticky="w", **pad)

        box.columnconfigure(5, weight=1)

        # --- API settings ---------------------------------------------
        api_box = ttk.LabelFrame(outer, text="WhatsApp API")
        api_box.pack(fill="x", pady=(0, 8))

        ttk.Label(api_box, text="Provider:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(
            api_box, textvariable=self.provider_var, width=12, state="readonly",
            values=("waha", "evolution"),
        ).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(api_box, text="API URL:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(api_box, textvariable=self.url_var, width=32).grid(row=0, column=3, sticky="ew", **pad)

        ttk.Label(api_box, text="API key:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(api_box, textvariable=self.key_var, width=28, show="*").grid(row=1, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(api_box, text="Session:").grid(row=1, column=3, sticky="w", **pad)
        ttk.Entry(api_box, textvariable=self.session_var, width=16).grid(row=1, column=4, sticky="w", **pad)
        api_box.columnconfigure(3, weight=1)

        # --- buttons ---------------------------------------------------
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(0, 8))

        self.run_btn = ttk.Button(btns, text="▶ RUN", command=self._on_run)
        self.run_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(btns, text="⏸ Pause", command=self._on_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="↺ Reset state", command=self._on_reset).pack(side="left", padx=4)
        self.review_btn = ttk.Button(btns, text="🔎 Review fixes…", command=self._show_review_window)
        self.review_btn.pack(side="left", padx=4)

        ttk.Button(btns, text="📁 Open output folder", command=lambda: self._open_folder(os.path.dirname(self.output_var.get()) or ROOT)).pack(side="right", padx=4)
        ttk.Button(btns, text="📁 Open review folder", command=lambda: self._open_folder(self.folders["review"])).pack(side="right", padx=4)

        # --- progress + status ----------------------------------------
        prog_frame = ttk.Frame(outer)
        prog_frame.pack(fill="x", pady=(0, 4))
        self.progress = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100.0)
        self.progress.pack(fill="x")
        ttk.Label(outer, textvariable=self.status_var, anchor="w").pack(fill="x")

        # --- live log --------------------------------------------------
        log_box = ttk.LabelFrame(outer, text="Live log")
        log_box.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(log_box, height=16, wrap="none", state="disabled", background="#111", foreground="#ddd")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def append_log(self, message: str) -> None:
        """Append a timestamped-ish line to the on-screen log."""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        # Keep the widget from growing without bound.
        if int(self.log_text.index("end-1c").split(".")[0]) > 4000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_running_ui(self, running: bool) -> None:
        self._running = running
        self.run_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(state="normal" if running else "disabled")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if not running:
            self.pause_btn.configure(text="⏸ Pause")
            self._paused = False

    def _browse_output(self) -> None:
        is_csv = self.format_var.get() == "csv"
        path = filedialog.asksaveasfilename(
            title="Choose output file",
            defaultextension=".csv" if is_csv else ".txt",
            filetypes=[("CSV", "*.csv"), ("Text", "*.txt"), ("All files", "*.*")],
            initialdir=os.path.dirname(self.output_var.get()) or ROOT,
        )
        if path:
            self.output_var.set(path)
            self.format_var.set("csv" if path.lower().endswith(".csv") else "notepad")

    @staticmethod
    def _open_folder(path: str) -> None:
        path = path or "."
        try:
            os.makedirs(path, exist_ok=True)
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # pragma: no cover - platform dependent
            messagebox.showerror("Open folder", f"Could not open {path}:\n{exc}")

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------
    def _persist_config(self) -> None:
        """Write the GUI values back into ``config.yaml`` (best-effort).

        The in-memory :attr:`config` is updated **first** so the run that is
        about to start uses exactly what the user just typed — persisting to
        disk is a separate, best-effort step.
        """
        api_values = {
            "provider": self.provider_var.get().strip() or "waha",
            "base_url": self.url_var.get().strip(),
            "api_key": self.key_var.get().strip(),
            "session_name": self.session_var.get().strip() or "default",
        }
        default_file = os.path.basename(self.output_var.get().strip() or "orders.txt")

        # --- keep the live config in sync (fixes the "old values" bug) -----
        self.config.setdefault("whatsapp_api", {})
        self.config["whatsapp_api"].update(api_values)
        self.config.setdefault("output", {})
        self.config["output"]["default_file"] = default_file

        # --- persist to config.yaml (optional) ----------------------------
        try:
            import yaml
        except ImportError:  # pragma: no cover
            return
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        cfg.setdefault("whatsapp_api", {})
        cfg["whatsapp_api"].update(api_values)
        cfg.setdefault("output", {})
        cfg["output"]["default_file"] = default_file
        try:
            with open(os.path.join(ROOT, "config.yaml"), "w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("Could not save config.yaml: %s", exc)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        if self._running:
            return
        try:
            start = int(str(self.start_var.get()).strip())
            end = int(str(self.end_var.get()).strip())
        except ValueError:
            messagebox.showerror("Invalid range", "Start ID and End ID must be whole numbers.")
            return
        if end < start:
            start, end = end, start
        if (end - start) > 5000:
            if not messagebox.askyesno(
                "Large range",
                f"That is {end - start + 1} chats. This can take a long time.\n\nContinue?",
            ):
                return

        output = self.output_var.get().strip()
        if not output:
            messagebox.showerror("No output file", "Please choose an output file.")
            return
        if not self.url_var.get().strip():
            messagebox.showerror("No API URL", "Please enter the WhatsApp API base URL.")
            return

        self._persist_config()

        output_format = self.format_var.get()
        if output_format == "notepad" and output.lower().endswith(".csv"):
            output_format = "csv"
        elif output_format == "csv" and not output.lower().endswith(".csv"):
            output += ".csv"
            self.output_var.set(output)

        # Fresh control object so a previous Stop doesn't bleed through.
        self._control = RunControl()
        self.progress_var.set(0.0)
        self.set_status("Starting…")
        self._set_running_ui(True)

        self._worker = threading.Thread(
            target=self._worker_main,
            args=(
                start,
                end,
                output,
                output_format,
                self.reprocess_var.get(),
                self.dry_run_var.get(),
                self.config,
            ),
            name="OrderCartWorker",
            daemon=True,
        )
        self._worker.start()

    def _on_pause(self) -> None:
        if not self._running:
            return
        paused = self._control.toggle_pause()
        self._paused = paused
        self.pause_btn.configure(text="▶ Resume" if paused else "⏸ Pause")
        self.set_status("Paused — press Resume to continue." if paused else "Running…")

    def _on_stop(self) -> None:
        if not self._running:
            return
        self._control.request_stop()
        self.set_status("Stopping after the current chat…")

    def _on_reset(self) -> None:
        if self._running:
            messagebox.showinfo("Please wait", "Stop the current run before resetting the state.")
            return
        if not messagebox.askyesno(
            "Reset state",
            "Clear the list of processed IDs?\n\n"
            "Every ID in the range will be processed again on the next run.",
        ):
            return
        try:
            from state.state import StateStore

            store = StateStore(os.path.join(self.folders["state"], "state.db"))
            count = store.count()
            store.clear()
            store.close()
            self.append_log(f"— State cleared ({count} records removed) —")
            self.set_status("State cleared.")
        except Exception as exc:
            messagebox.showerror("Reset failed", str(exc))

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------
    def _worker_main(
        self,
        start: int,
        end: int,
        output: str,
        output_format: str,
        reprocess: bool,
        dry_run: bool,
        config: Dict[str, Any],
    ) -> None:
        """Runs in the background thread — never touches tkinter directly."""
        put = self._queue.put
        try:
            put(("log", "Loading OCR engine (first run may take a moment)…"))
            if dry_run:
                put(("log", "DRY RUN — nothing will be written and no state recorded."))
            # Heavy imports live here so window startup stays instant.
            from core.orchestrator import Orchestrator

            orch = Orchestrator.from_config(
                config,
                output_path=output,
                output_format=output_format,
                state_path=os.path.join(self.folders["state"], "state.db"),
                assets_dir=self.folders["assets"],
                logs_dir=self.folders["logs"],
                review_dir=self.folders["review"],
                dry_run=dry_run,
                control=self._control,
            )
            orch.log_callback = lambda msg: put(("log", msg))
            orch.progress_callback = lambda result, index, total: put(
                ("progress", result, index, total)
            )

            summary = orch.run(start, end, reprocess=reprocess, dry_run=dry_run)
            put(("done", summary))
            orch.close()
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            self.logger.exception("Worker crashed")
            put(("error", str(exc)))

    # ------------------------------------------------------------------
    # Queue polling (runs on the GUI thread)
    # ------------------------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1])
                elif kind == "progress":
                    result, index, total = item[1], item[2], item[3]
                    self._render_progress(result, index, total)
                elif kind == "done":
                    self._on_worker_done(item[1])
                elif kind == "error":
                    self._on_worker_error(item[1])
        except queue.Empty:
            pass
        finally:
            self.after(POLL_INTERVAL_MS, self._poll_queue)

    def _render_progress(self, result, index: int, total: int) -> None:
        pct = (index / total * 100.0) if total else 100.0
        self.progress_var.set(pct)
        self.set_status(f"Processing {index}/{total} — last: {result.summary_line()}")

    def _on_worker_done(self, summary) -> None:
        self._set_running_ui(False)
        self._summary = summary
        self.progress_var.set(100.0)
        self.append_log("")
        self.append_log(summary.report())

        dry_note = " (dry run — nothing written)" if getattr(summary, "dry_run", False) else ""
        self.set_status(
            f"Done{dry_note} — {summary.successful} extracted, {summary.failed} failed, "
            f"{summary.missing} missing in {summary.duration:.1f}s."
        )

        # Surface TRUNC / FAILED ids for manual correction.
        self._populate_review(summary)

        # Offer to open the output file when something was written.
        if summary.processed and not getattr(summary, "dry_run", False):
            if messagebox.askyesno(
                "Run complete",
                f"Processed {summary.processed} chats.\n\n"
                f"Order IDs     : {summary.ok}\n"
                f"Truncated IDs : {summary.trunc}\n"
                f"Name fallback : {summary.fallback_name}\n"
                f"Addr fallback : {summary.fallback_address}\n"
                f"Failed        : {summary.failed}\n"
                f"Missing chats : {summary.missing}\n\n"
                "Open the output folder now?",
            ):
                self._open_folder(os.path.dirname(self.output_var.get()) or ROOT)

    def _on_worker_error(self, message: str) -> None:
        self._set_running_ui(False)
        self.set_status("Run failed.")
        self.append_log(f"ERROR: {message}")
        messagebox.showerror(
            "Run failed",
            f"{message}\n\n"
            "Check:\n"
            "  • the WhatsApp API is running (WAHA / Evolution)\n"
            "  • the API URL, key and session name are correct\n"
            "  • see logs/ordercart.log for details",
        )

    # ------------------------------------------------------------------
    # Review / corrections workflow
    # ------------------------------------------------------------------
    def _populate_review(self, summary) -> None:
        """Show the TRUNC / FAILED review window for the finished run."""
        try:
            # ``review_rows()`` returns plain ``(id, status, value)`` tuples —
            # ``_build_review_window`` unpacks them positionally.  Passing the
            # raw ProcessResult objects here used to crash the window.
            rows = list(summary.review_rows())
        except Exception:
            rows = []
        if not rows:
            return
        self.append_log(
            f"— {len(rows)} id(s) need review (TRUNC/FAILED). "
            "Use “Review fixes…” to paste the correct Order IDs. —"
        )
        self._build_review_window(rows)

    def _review_items(self) -> List[tuple]:
        """Return ``(internal_id, status, current_value)`` for the review list.

        Prefers the latest run summary; falls back to the review folder so the
        window still works after a restart.
        """
        items: List[tuple] = []
        summary = self._summary
        if summary is not None:
            try:
                items = list(summary.review_rows())
            except Exception:
                items = []
        if items:
            return items

        review_dir = self.folders.get("review")
        if review_dir and os.path.isdir(review_dir):
            for name in sorted(os.listdir(review_dir)):
                if name.lower().endswith(".jpg"):
                    stem = os.path.splitext(name)[0]
                    try:
                        items.append((int(stem), "TRUNC", ""))
                    except ValueError:
                        continue
        return items

    def _show_review_window(self) -> None:
        """Open (or focus) the review window using the known review items."""
        items = self._review_items()
        if not items:
            messagebox.showinfo(
                "Nothing to review",
                "No TRUNC or FAILED ids to fix yet.\n\n"
                "Run a batch first — ids that need a human check will appear here.",
            )
            return
        self._build_review_window(items)

    def _build_review_window(self, items: List[tuple]) -> None:
        # Close a previous window so we don't stack stale entries.
        if self._review_window is not None and self._review_window.winfo_exists():
            self._review_window.destroy()
        self._review_entries = {}

        win = tk.Toplevel(self)
        self._review_window = win
        win.title("Review TRUNC / FAILED ids")
        win.geometry("560x420")
        win.transient(self)

        ttk.Label(
            win,
            text=(
                "Paste the correct Order ID for each row and press "
                "“Save corrections”. Blank rows are ignored."
            ),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 4))

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        ttk.Label(inner, text="Internal ID", font=("", 9, "bold")).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(inner, text="Status", font=("", 9, "bold")).grid(row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(inner, text="Order ID (edit)", font=("", 9, "bold")).grid(row=0, column=2, sticky="w", padx=4, pady=2)

        for row, (internal_id, status, current) in enumerate(items, start=1):
            ttk.Label(inner, text=str(internal_id)).grid(row=row, column=0, sticky="w", padx=4, pady=2)
            ttk.Label(inner, text=str(status)).grid(row=row, column=1, sticky="w", padx=4, pady=2)
            entry = ttk.Entry(inner, width=28)
            entry.insert(0, str(current or ""))
            entry.grid(row=row, column=2, sticky="ew", padx=4, pady=2)
            self._review_entries[int(internal_id)] = entry

        inner.columnconfigure(2, weight=1)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(btns, text="💾 Save corrections", command=self._save_corrections).pack(side="left")
        ttk.Button(btns, text="📁 Open review folder", command=lambda: self._open_folder(self.folders["review"])).pack(side="left", padx=6)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")

    def _save_corrections(self) -> None:
        """Rewrite the output file with the pasted Order IDs."""
        corrections: Dict[int, str] = {}
        for internal_id, entry in self._review_entries.items():
            value = entry.get().strip()
            if value:
                corrections[int(internal_id)] = value
        if not corrections:
            messagebox.showinfo("Save corrections", "No Order IDs were entered.")
            return

        output = self.output_var.get().strip()
        if not output:
            messagebox.showerror("Save corrections", "No output file is set.")
            return

        try:
            from output.corrections import apply_corrections

            changed = apply_corrections(
                output,
                corrections,
                output_format=self.format_var.get(),
                config=self.config,
            )
        except Exception as exc:
            messagebox.showerror("Save corrections", f"Could not update the output file:\n{exc}")
            return

        # Mark the corrected ids as done ONLY when the output file actually
        # changed.  A 0-change result means nothing matched (e.g. the wrong
        # file or blank values); marking ids processed then would silently drop
        # them from every future run.
        if changed > 0:
            try:
                from state.state import StateStore

                store = StateStore(os.path.join(self.folders["state"], "state.db"))
                for internal_id, value in corrections.items():
                    store.mark_processed(
                        int(internal_id),
                        {"type": "OK", "order_id": value, "source": "manual"},
                    )
                store.close()
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.warning("Could not update state after corrections: %s", exc)
        else:
            self.logger.warning(
                "Save corrections: 0 rows matched in %s — state left unchanged.",
                output,
            )

        self.append_log(f"— Saved {changed} correction(s) to {os.path.basename(output)} —")
        self.set_status(f"Saved {changed} correction(s).")
        messagebox.showinfo(
            "Save corrections",
            f"Updated {changed} row(s) in:\n{output}",
        )

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno("Quit", "A run is in progress. Stop and quit?"):
                return
            self._control.request_stop()
            if self._worker is not None:
                self._worker.join(timeout=3.0)
        self.destroy()


# ---------------------------------------------------------------------------
def main() -> None:
    app = OrderCartApp()
    app.mainloop()


if __name__ == "__main__":
    main()
