"""
tests/test_ocr_engine.py
========================

Regression tests for the **Google-Lens** OCR engine.

FrameFlow implements the Lens upload protocol itself (:mod:`ocr.proto` +
:mod:`ocr.lens`), so these tests fake the HTTP client instead of a third-party
package.  They also install trip-wires on ``paddleocr`` / ``pytesseract``:
any access to those modules during an OCR call raises immediately.  That
proves no local OCR engine is ever imported or invoked — including when the
upload fails or times out.
"""

from __future__ import annotations

import inspect
import sys
import types

import numpy as np
import pytest

from ocr import engine as engine_mod
from ocr.engine import OCREngine, build_engine


# ---------------------------------------------------------------------------
# Trip-wires: any local-OCR access explodes
# ---------------------------------------------------------------------------
class _ExplodingModule(types.ModuleType):
    """Stand-in module whose *any* attribute access is a hard failure."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.accessed = False

    def __getattr__(self, item):  # noqa: D105 - module attribute hook
        self.accessed = True
        raise AssertionError(
            f"forbidden OCR module {self.__name__!r} was accessed ({item})"
        )


@pytest.fixture
def forbid_local_ocr(monkeypatch):
    """Install trip-wires so touching paddleocr/pytesseract fails the test."""
    modules = {}
    for name in ("paddleocr", "pytesseract"):
        mod = _ExplodingModule(name)
        monkeypatch.setitem(sys.modules, name, mod)
        modules[name] = mod
    return modules


def _assert_untouched(modules) -> None:
    for name, mod in modules.items():
        assert not mod.accessed, f"local OCR module {name!r} was accessed"


# ---------------------------------------------------------------------------
# Fake Lens backend
# ---------------------------------------------------------------------------
def install_fake_lens(monkeypatch, handler):
    """Install a fake ``ocr.lens.LensClient`` and return the call log.

    ``handler(index)`` is called for every attempt (1-based) and may return a
    string or raise to simulate a failure.
    """
    calls = []

    class FakeLensClient:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})
            self.kwargs = kwargs

        def ocr(self, image_bytes, width=0, height=0, mime_type="image/jpeg"):
            calls.append({
                "bytes": image_bytes,
                "width": width,
                "height": height,
                "mime_type": mime_type,
            })
            return handler(len(calls))

        def close(self):
            calls.append({"closed": True})

    module = types.ModuleType("fake_lens_client")
    module.LensClient = FakeLensClient
    monkeypatch.setattr(engine_mod, "LensClient", FakeLensClient)
    return calls


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------
def test_lens_success_returns_text(monkeypatch, forbid_local_ocr):
    install_fake_lens(
        monkeypatch,
        lambda i: "Order ID 1234567890123456\nName: Asha",
    )
    engine = OCREngine()

    result = engine.run(np.zeros((40, 30, 3), dtype=np.uint8))

    assert result.engine == "lens"
    assert result.error is None
    assert "1234567890123456" in result.text
    assert result.confidence > 0
    assert result.lines[0].startswith("Order ID")
    _assert_untouched(forbid_local_ocr)


def test_lens_upload_carries_the_image(monkeypatch, forbid_local_ocr):
    calls = install_fake_lens(monkeypatch, lambda i: "Order #342581970718279808")
    engine = OCREngine()

    result = engine.run(np.zeros((120, 90, 3), dtype=np.uint8))

    assert result.error is None
    uploads = [c for c in calls if "bytes" in c]
    assert len(uploads) == 1
    assert uploads[0]["bytes"][:2] == b"\xff\xd8"  # JPEG magic
    assert uploads[0]["width"] == 90
    assert uploads[0]["height"] == 120
    _assert_untouched(forbid_local_ocr)


def test_run_returns_error_for_none_image():
    assert OCREngine().run(None).error == "no_image"  # type: ignore[arg-type]


def test_missing_file_is_an_ocr_failure():
    result = OCREngine().run_on_file("does-not-exist.png")
    assert result.text == ""
    assert result.error and "missing_file" in result.error


# ---------------------------------------------------------------------------
# Failure paths must be OCR failures, never a fallback
# ---------------------------------------------------------------------------
def test_lens_exception_is_ocr_failure(monkeypatch, forbid_local_ocr):
    def boom(_index):
        raise RuntimeError("network down")

    install_fake_lens(monkeypatch, boom)
    engine = OCREngine(lens_retries=2, lens_retry_backoff=0)

    result = engine.run(np.zeros((10, 10, 3), dtype=np.uint8))

    assert result.text == ""
    assert result.engine == "lens"
    assert result.error and "network down" in result.error
    _assert_untouched(forbid_local_ocr)


def test_lens_timeout_is_ocr_failure(monkeypatch, forbid_local_ocr):
    def timeout(_index):
        raise TimeoutError("lens timed out")

    install_fake_lens(monkeypatch, timeout)
    engine = OCREngine(lens_retries=1, lens_retry_backoff=0)

    result = engine.run(np.zeros((10, 10, 3), dtype=np.uint8))

    assert result.text == ""
    assert result.error and "timed out" in result.error
    _assert_untouched(forbid_local_ocr)


def test_retry_policy_is_handed_to_the_client(monkeypatch):
    calls = install_fake_lens(monkeypatch, lambda i: "ok")
    engine = OCREngine(lens_retries=3, lens_retry_backoff=0.25, lens_timeout=9)

    engine.run(np.zeros((10, 10, 3), dtype=np.uint8))

    init = calls[0]["init"]
    assert init["max_retries"] == 3
    assert init["backoff_base"] == 0.25
    assert init["timeout"] == 9


def test_empty_lens_response_is_not_an_error(monkeypatch, forbid_local_ocr):
    calls = install_fake_lens(monkeypatch, lambda i: "")
    engine = OCREngine(lens_retries=1, lens_retry_backoff=0)

    result = engine.run(np.zeros((10, 10, 3), dtype=np.uint8))

    assert result.text == ""
    assert result.error is None  # Lens answered; the image just had no text
    assert len([c for c in calls if "bytes" in c]) == 1
    _assert_untouched(forbid_local_ocr)


# ---------------------------------------------------------------------------
# Focus pass (the enlarged header re-read)
# ---------------------------------------------------------------------------
def test_focus_pass_reads_the_header_only(monkeypatch, forbid_local_ocr):
    calls = install_fake_lens(
        monkeypatch,
        lambda i: "Order #342581970718279808" if i > 1 else "",
    )
    engine = OCREngine(focus_ratio=0.5)

    image = np.zeros((400, 200, 3), dtype=np.uint8)
    image[:100, :] = 255  # a bright header band
    result = engine.run_focused(image)

    assert result.error is None
    assert "342581970718279808" in result.text
    upload = [c for c in calls if "bytes" in c][-1]
    assert upload["height"] < 400  # the crop, not the whole page
    _assert_untouched(forbid_local_ocr)


def test_focus_pass_is_optional(monkeypatch):
    install_fake_lens(monkeypatch, lambda i: "Order #342581970718279808")
    engine = OCREngine(focus_pass=False)

    image = np.zeros((400, 200, 3), dtype=np.uint8)
    result = engine.run_focused(image)

    assert result.error == "no_focus_region" or result.text


def test_merge_focus_keeps_a_correct_full_read(monkeypatch):
    install_fake_lens(monkeypatch, lambda i: "")
    engine = OCREngine()

    full = "Order #342581970718279808"
    same = engine.merge_focus(full, "Order #342581970718279808")

    assert same == full  # no duplicate candidate


def test_merge_focus_adds_new_digits(monkeypatch):
    install_fake_lens(monkeypatch, lambda i: "")
    engine = OCREngine()

    merged = engine.merge_focus("Order #342581970718279808", "Order #342581970718279809")

    assert "342581970718279809" in merged


# ---------------------------------------------------------------------------
# Config wiring + static guard
# ---------------------------------------------------------------------------
def test_build_engine_reads_lens_config():
    engine = build_engine(
        {"ocr": {"lens_timeout": 9, "lens_retries": 4, "lens_retry_backoff": 0.25}}
    )
    assert engine.lens_timeout == 9
    assert engine.lens_retries == 4
    assert engine.lens_retry_backoff == 0.25


def test_build_engine_defaults():
    engine = build_engine({})
    assert engine.lens_retries >= 0
    assert engine.lens_timeout > 0
    assert engine.jpeg_quality >= 30


def test_build_engine_reads_the_upload_knobs():
    engine = build_engine(
        {"ocr": {"jpeg_quality": 55, "max_dimension": 900, "focus_pass": False}}
    )
    assert engine.jpeg_quality == 55
    assert engine.max_dimension == 900
    assert engine.focus_pass is False


def test_engine_source_has_no_local_engine_imports():
    src = inspect.getsource(engine_mod)
    for needle in (
        "import paddleocr",
        "from paddleocr",
        "import pytesseract",
        "from pytesseract",
    ):
        assert needle not in src, f"engine.py still references {needle!r}"


def test_no_ocr_third_party_dependency_is_imported():
    """The OCR stack must not pull in a third-party OCR package."""
    for module in ("ocr.engine", "ocr.lens", "ocr.proto"):
        src = inspect.getsource(sys.modules[module])
        assert "chrome_lens_py" not in src
        assert "import paddleocr" not in src
        assert "import pytesseract" not in src
