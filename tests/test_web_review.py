"""
tests/test_web_review.py
========================

The web UI **review / corrections workflow** (Orders view):

* ``Api.apply_correction(internal_id, corrected_value)`` validates, delegates the
  file rewrite to :func:`output.corrections.apply_corrections` (frozen — always
  faked here, so the real output file is never touched), writes the required
  ``event=correction … source=ui`` audit line to ``logs/ordercart.log`` and
  returns the refreshed rows so the Orders table re-renders immediately;
* ``Api.get_orders()`` exposes the session-scoped ``ProcessResult.ambiguous`` /
  ``.partial`` flags behind the "Needs review" filter (plus the persisted
  ``TRUNC`` / ``FAILED`` rows, which survive a restart);
* ``Api._emit_progress`` pushes ``updateProgress(index, total)`` with
  json-encoded literals only (§4.4);
* the JavaScript half of the wiring (pencil editor, J/K/Enter/Esc, the
  ``btn-review-filter`` toggle) — asserted statically, because there is no JS
  test runner in this repo.

Everything runs offline against ``tmp_path`` output files.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager

import pytest

import main_web
from main_web import Api
from state.state import StateStore

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(main_web.__file__)), "ui")


def _read_len(path: str) -> int:
    """Character length of a text file (0 when it does not exist yet)."""
    if not os.path.isfile(path):
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return len(fh.read())


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------
@pytest.fixture
def output_file(tmp_path):
    """A Notepad-format output file (``internal_id,value``) in tmp_path."""
    path = tmp_path / "orders.txt"
    path.write_text(
        "12988,TRUNC:347011\n"
        "12990,FAILED\n"
        "12996,346752\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def api(output_file, tmp_path, monkeypatch):
    """An :class:`Api` pointed at the tmp output file.

    ``main_web.apply_corrections`` is always replaced by a fake, so
    :mod:`output.corrections` and the real file are never exercised.
    ``main_web.ROOT`` is redirected into ``tmp_path`` as well, so a *relative*
    output path (the seller's Notepad list, for instance) can never reach the
    project's own files.
    """
    monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
    instance = Api()
    instance.config.setdefault("output", {})
    instance.config["output"]["default_file"] = output_file
    # The seller's plain "internal_id,value" list — a tmp file, never the
    # project's own test_jeetu.txt.
    instance.config["output"]["notepad_file"] = str(tmp_path / "notepad.txt")
    return instance


@pytest.fixture(scope="module")
def app_js():
    """The contents of ``ui/app.js``."""
    with open(os.path.join(UI_DIR, "app.js"), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def index_html():
    """The contents of ``ui/index.html``."""
    with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as fh:
        return fh.read()


class FakeResult:
    """Minimal stand-in for ``core.orchestrator.ProcessResult``."""

    def __init__(self, internal_id, status="TRUNC", ambiguous=False,
                 partial=False, skipped=False):
        self.internal_id = internal_id
        self.status = status
        self.ambiguous = ambiguous
        self.partial = partial
        self.skipped = skipped


class FakeWindow:
    """Captures every ``evaluate_js`` snippet instead of rendering it."""

    def __init__(self):
        self.snippets = []

    def evaluate_js(self, snippet):  # pragma: no cover - trivial
        self.snippets.append(snippet)


@contextmanager
def real_logging(log_dir):
    """Run the block with the *app's* logging wiring (``configure_logging``).

    ``configure_logging`` only installs its ``logs/ordercart.log`` FileHandler
    when the root logger has none yet — and pytest's own stderr-capture
    handler *subclasses* ``logging.FileHandler`` (it writes to ``\\\\.\\nul``),
    so under pytest the app's file handler would never be installed.  Swap in
    the real handler set for the duration of the block; caplog keeps working
    because its LogCaptureHandlers stay attached.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    keep = [h for h in saved if type(h).__name__ == "LogCaptureHandler"]
    root.handlers = list(keep)
    try:
        main_web.configure_logging(log_dir)
        yield
    finally:
        for handler in root.handlers:
            if handler not in keep:
                handler.close()
        root.handlers = saved


def install_fake_corrections(monkeypatch, calls):
    """Replace ``main_web.apply_corrections`` with a recording fake.

    The fake rewrites the *tmp* file the way the real writer would (so the
    refresh can be asserted) without importing or calling the frozen module.
    """

    def fake_apply_corrections(path, corrections, output_format=None, config=None,
                               matches=None):
        calls.append({
            "path": path,
            "corrections": dict(corrections),
            "output_format": output_format,
            "config": config,
            "matches": dict(matches or {}),
        })
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        wanted = {str(k).strip(): str(v).strip() for k, v in (matches or {}).items()}
        changed = 0
        out = []
        for line in lines:
            iid, _, value = line.partition(",")
            key = iid.strip()
            if key in corrections:
                # A pinned correction only touches the row it points at.
                if key in wanted and wanted[key] != value.strip():
                    out.append(line)
                    continue
                out.append(f"{key},{corrections[key]}")
                changed += 1
            else:
                out.append(line)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
        return changed

    monkeypatch.setattr(main_web, "apply_corrections", fake_apply_corrections)


# ---------------------------------------------------------------------------
# Api.apply_correction
# ---------------------------------------------------------------------------
class TestApplyCorrection:
    def test_delegates_with_the_right_args(self, api, monkeypatch):
        calls = []
        install_fake_corrections(monkeypatch, calls)

        result = api.apply_correction("12988", "763328")

        assert result["ok"] is True
        assert result["changed"] == 1
        assert len(calls) == 1
        call = calls[0]
        # path + mapping the frozen helper expects, config for the CSV date fmt
        assert call["path"] == api._output_file()
        assert call["corrections"] == {"12988": "763328"}
        assert call["config"] is api.config

    def test_strips_an_accidental_trunc_prefix(self, api, monkeypatch):
        calls = []
        install_fake_corrections(monkeypatch, calls)

        result = api.apply_correction("12988", "  TRUNC:763328  ")

        assert result["ok"] is True
        assert result["order_id"] == "763328"
        assert calls[0]["corrections"] == {"12988": "763328"}

    @pytest.mark.parametrize("internal_id,value,fragment", [
        ("", "763328", "internal_id"),
        ("   ", "763328", "internal_id"),
        ("12988", "", "empty"),
        ("12988", "   ", "empty"),
        ("12988", "  TRUNC:  ", "empty"),
    ])
    def test_blank_inputs_are_rejected_without_touching_the_file(
        self, api, monkeypatch, internal_id, value, fragment
    ):
        calls = []
        install_fake_corrections(monkeypatch, calls)

        result = api.apply_correction(internal_id, value)

        assert result["ok"] is False
        assert fragment in result["error"].lower()
        assert calls == []            # the file was never handed to the writer

    def test_orders_table_refreshes_with_the_corrected_value(
        self, api, monkeypatch
    ):
        """The payload the UI re-renders must already carry the new value."""
        install_fake_corrections(monkeypatch, [])

        before = {r["internal_id"]: r["order_id"] for r in api.get_orders()}
        assert before["12988"] == "TRUNC:347011"

        result = api.apply_correction("12988", "763328")

        assert result["ok"] is True
        orders = result["orders"]
        row = next(r for r in orders if r["internal_id"] == "12988")
        assert row["order_id"] == "763328"          # refreshed, not stale
        # untouched rows survive the refresh
        assert any(r["internal_id"] == "12996" and r["order_id"] == "346752"
                   for r in orders)

    def test_audit_line_is_logged_at_info(self, api, monkeypatch, caplog):
        install_fake_corrections(monkeypatch, [])
        log_file = os.path.join(api.folders["logs"], "ordercart.log")
        marker = "event=correction internal_id=12988"

        with real_logging(api.folders["logs"]), caplog.at_level(logging.INFO):
            before = _read_len(log_file)
            api.apply_correction("12988", "763328")

        line = "event=correction internal_id=12988 old=TRUNC:347011 new=763328 source=ui"
        assert line in caplog.text, caplog.text

        # ...and it reaches logs/frameflow.log (the Api logger's file handler)
        assert os.path.isfile(log_file)
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            added = fh.read()[before:]
        assert marker in added

    def test_writer_failure_is_reported_not_raised(self, api, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(main_web, "apply_corrections", boom)

        result = api.apply_correction("12988", "763328")

        assert result["ok"] is False
        assert "disk full" in result["error"]
        assert isinstance(result["orders"], list)   # UI can still re-render


# ---------------------------------------------------------------------------
# "Needs review" data (ProcessResult.ambiguous / .partial)
# ---------------------------------------------------------------------------
class TestReviewFlags:
    def test_flags_reach_the_orders_rows(self, api):
        api._on_orchestrator_progress(FakeResult(12988, ambiguous=True), 1, 3)
        api._on_orchestrator_progress(FakeResult(12990, partial=True), 2, 3)
        api._on_orchestrator_progress(FakeResult(12996, skipped=True), 3, 3)

        rows = {r["internal_id"]: r for r in api.get_orders()}

        assert rows["12988"]["ambiguous"] is True
        assert rows["12988"]["partial"] is False
        assert rows["12990"]["partial"] is True
        # a skipped chat never carries review flags (progress returns early)
        assert rows["12996"]["ambiguous"] is False
        assert rows["12996"]["partial"] is False

    def test_trunc_and_failed_rows_are_identified_by_status_alone(self, api):
        """After a restart no flags survive — status still marks the rows."""
        rows = {r["internal_id"]: r for r in api.get_orders()}

        assert rows["12988"]["status"] == "TRUNC"      # TRUNC: prefix decoded
        assert rows["12990"]["status"] == "FAILED"
        assert rows["12996"]["status"] == "OK"
        # ...which is exactly what rowNeedsReview() keys off in app.js
        assert str(rows["12988"]["order_id"]).startswith("TRUNC:")

    def test_progress_bar_ticked_for_skipped_rows_too(self, api):
        window = FakeWindow()
        api.bind_window(window)

        api._on_orchestrator_progress(FakeResult(1, skipped=True), 2, 5)

        assert window.snippets == ["updateProgress(2, 5)"]


# ---------------------------------------------------------------------------
# "Last scanned" date basis (state DB -> Orders rows)
# ---------------------------------------------------------------------------
class TestLastScanned:
    def test_state_timestamp_reaches_every_row_of_the_chat(self, api):
        db_path = os.path.join(api.folders["state"], "state.db")
        store = StateStore(db_path, scope="gg")
        store.mark_processed(12988, {"type": "OK"}, "2026-10-23 09:46:36")
        store.close()

        rows = {r["internal_id"]: r for r in api.get_orders()}

        assert rows["12988"]["last_scanned"] == "2026-10-23 09:46:36"
        # A chat with no state record falls back to "" — the UI then uses the
        # row's own timestamp for the "last scanned" filter.
        assert rows["12990"]["last_scanned"] == ""
        assert rows["12996"]["last_scanned"] == ""

    def test_missing_state_db_is_not_fatal(self, api):
        # The fixture's state dir exists, but no run has created the DB yet.
        rows = api.get_orders()
        assert rows
        assert all("last_scanned" in row for row in rows)

    def test_latest_scope_wins(self, api):
        db_path = os.path.join(api.folders["state"], "state.db")
        first = StateStore(db_path, scope="gg")
        first.mark_processed(12988, {"type": "OK"}, "2026-10-21 10:00:00")
        first.close()
        second = StateStore(db_path, scope="personal")
        second.mark_processed(12988, {"type": "OK"}, "2026-10-23 09:46:36")
        second.close()

        rows = {r["internal_id"]: r for r in api.get_orders()}
        assert rows["12988"]["last_scanned"] == "2026-10-23 09:46:36"


# ---------------------------------------------------------------------------
# updateProgress sanitisation (§4.4)
# ---------------------------------------------------------------------------
class TestProgressPush:
    def test_only_json_literals_reach_evaluate_js(self, api):
        window = FakeWindow()
        api.bind_window(window)

        api._emit_progress(7, 120)

        assert window.snippets == ["updateProgress(7, 120)"]

    def test_progress_before_window_bind_is_a_noop(self, api):
        api._emit_progress(1, 2)      # _window is None -> must not raise


# ---------------------------------------------------------------------------
# JavaScript half of the workflow (no JS runner in this repo)
# ---------------------------------------------------------------------------
class TestAppJsWiring:
    def test_js_saves_through_apply_correction_and_rerenders(self, app_js):
        assert "a.apply_correction(" in app_js
        # the refreshed payload re-renders the table (no stale rows)
        assert "renderOrders(res.orders)" in app_js
        assert "a.get_orders()" in app_js

    def test_js_has_the_needs_review_filter(self, app_js, index_html):
        assert 'id="btn-review-filter"' in index_html
        assert 'id="review-count"' in index_html
        assert '$("btn-review-filter")' in app_js
        assert "rowNeedsReview" in app_js
        assert "toggleReviewFilter" in app_js

    def test_js_inline_editor_lifecycle(self, app_js):
        assert "startRowEdit" in app_js
        assert "saveRowEdit" in app_js
        assert "cancelRowEdit" in app_js
        assert "row-edit-input" in app_js

    def test_js_has_the_progress_bar(self, app_js, index_html):
        """Task 4: index/total from progress_callback -> updateProgress()."""
        assert 'id="progress-card"' in index_html
        assert 'id="progress-fill"' in index_html
        assert 'id="progress-text"' in index_html
        assert "function updateProgress(" in app_js
        # a fresh run resets the bar (hidden until the first real tick)
        assert "card.hidden = true" in app_js

    def test_js_keyboard_is_exactly_j_k_enter_escape(self, app_js):
        """J/K move rows, Enter saves, Esc cancels — and nothing else."""
        assert 'e.key === "j"' in app_js
        assert 'e.key === "k"' in app_js
        assert 'e.key === "Enter"' in app_js
        assert 'e.key === "Escape"' in app_js
        # The complete set of keys compared anywhere in app.js: the review
        # keyboard (Enter / Escape / j / J / k / K) plus the pre-existing
        # "Enter in the ID fields = RUN" handler. Anything new would show up
        # here as an extra key.
        compared = set(re.findall(r'e\.key === "([^"]+)"', app_js))
        assert compared == {"Enter", "Escape", "j", "J", "k", "K"}


class TestReprocessSwitch:
    """The "Reprocess" toggle: scan chats that were already done again."""

    def test_the_bridge_takes_the_flag(self, api, monkeypatch):
        seen = {}

        def fake_worker(*args, **kwargs):
            seen["args"] = args

        monkeypatch.setattr(api, "_worker_main", fake_worker)
        result = api.start_extraction("10", "10", False, True)

        assert result["ok"] is True
        assert seen["args"][-1] is True  # reprocess

    def test_it_defaults_to_off(self, api, monkeypatch):
        seen = {}

        def fake_worker(*args, **kwargs):
            seen["args"] = args

        monkeypatch.setattr(api, "_worker_main", fake_worker)
        api.start_extraction("10", "10", False)

        assert seen["args"][-1] is False

    def test_the_ui_has_the_toggle(self, app_js, index_html):
        assert 'id="reprocess"' in index_html
        assert 'id="reprocess-text"' in index_html
        assert '$("reprocess")' in app_js
        # the flag is handed to the bridge
        assert "a.start_extraction(start, end, dryRun, reprocess)" in app_js


class TestCorrectionPinsTheRow:
    def test_the_ui_sends_the_current_value(self, app_js):
        # a chat can carry several orders (one row each) - the correction is
        # pinned to the row the seller is editing.
        assert "a.apply_correction(internalId, value, currentValue)" in app_js
        assert "tr.dataset.orderId" in app_js

    def test_the_bridge_pins_the_correction(self, api, monkeypatch):
        calls = []
        install_fake_corrections(monkeypatch, calls)

        api.apply_correction("12988", "763328", "TRUNC:347011")

        assert calls[0]["matches"] == {"12988": "TRUNC:347011"}

    def test_without_a_current_value_every_row_is_rewritten(self, api, monkeypatch):
        calls = []
        install_fake_corrections(monkeypatch, calls)

        api.apply_correction("12988", "763328")

        assert calls[0]["matches"] == {}
