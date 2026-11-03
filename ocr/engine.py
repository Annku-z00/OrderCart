"""
ocr/engine.py
=============

The OCR engine — **Google Lens, implemented from scratch**.

The image is uploaded to Google's official ``v1/crupload`` endpoint (the same
one Chromium's "Search image with Google Lens" overlay talks to) and the
recognised text comes back in the protobuf answer.  The transport lives in
:mod:`ocr.lens`, the wire codec in :mod:`ocr.proto`; this module is the thin,
stable layer the rest of FrameFlow talks to.

There is intentionally **no local OCR fallback**.  When a request fails
(network error, timeout, non-2xx response, unparsable answer) it is retried
according to the configured policy (``ocr.lens_retries`` attempts with
``ocr.lens_retry_backoff`` seconds between them) and then reported as an
error: :class:`OCRResult.error` is set and ``text`` is empty, so the
orchestrator treats it as a retryable OCR failure (``PARTIAL`` / unprocessed)
and **never** silently accepts a failed read as success.  An image that Lens
reads successfully but that simply contains no text is *not* recorded as an
error.

Two passes
----------
A Meesho order screenshot is a tall phone screen: the ``Order #…`` line sits
near the top in small type, while the delivery block with the PIN code and
the phone number sits far below.  One full-page pass therefore has to read a
lot of pixels for a handful of useful ones.

So the engine can also run a **focus pass**: the top of the image is cropped
and uploaded on its own.  The full-page upload is shrunk to the upload
limits, which makes the small ``Order #…`` line tiny — the crop is small
enough to be sent at its *native* resolution, so the id line arrives at Lens
with its original number of pixels.  The focus pass is *not* automatic — the
caller decides when it is worth an extra request (the orchestrator runs it for
the images whose full-page text yielded no order id at all), so a run over a
folder of screenshots does not double its request count for nothing.

The public surface is unchanged (``OCRResult``, ``OCREngine.run`` /
``run_on_file``, ``build_engine``), so the orchestrator and the unit tests
need no changes.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ocr.lens import LensClient, LensError

logger = logging.getLogger(__name__)

#: A digit run of at least this length is worth keeping from the focus pass.
_FOCUS_DIGIT_RUN = re.compile(r"\d{6,}")


@dataclass
class OCRResult:
    """Structured OCR output."""

    text: str = ""
    confidence: float = 0.0
    lines: List[str] = field(default_factory=list)
    engine: str = ""
    error: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def _lines_from_text(text: str) -> List[str]:
    return [ln for ln in (text or "").split("\n") if ln.strip()]


class OCREngine:
    """Google Lens OCR engine — implemented locally, no OCR dependency."""

    def __init__(
        self,
        lens_language: Optional[str] = None,
        lens_timeout: float = 15.0,
        lens_retries: int = 2,
        lens_retry_backoff: float = 0.5,
        jpeg_quality: int = 90,
        max_pixels: int = 1_500_000,
        max_dimension: int = 1600,
        focus_pass: bool = True,
        focus_ratio: float = 0.45,
        focus_max_pixels: int = 4_000_000,
        focus_max_dimension: int = 2400,
        upscale_factor: int = 1,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.lens_language = lens_language or None
        self.lens_timeout = float(lens_timeout or 15.0)
        #: Number of *additional* attempts after the first failure.
        self.lens_retries = max(0, int(lens_retries))
        self.lens_retry_backoff = max(0.0, float(lens_retry_backoff))

        #: JPEG quality of the uploaded frame.  Chromium uploads at 40, but
        #: these are crisp *digital* screenshots, not photos — a higher
        #: quality keeps the digits intact at no meaningful cost.
        self.jpeg_quality = int(min(95, max(30, int(jpeg_quality or 90))))
        #: Upload limits (Chromium parity): only shrink, never enlarge.
        self.max_pixels = int(max_pixels or 1_500_000)
        self.max_dimension = int(max_dimension or 1600)
        #: Run the extra "zoom into the header" pass.
        self.focus_pass = bool(focus_pass)
        #: Which share of the image height the focus pass looks at.
        self.focus_ratio = float(min(1.0, max(0.1, focus_ratio or 0.45)))
        #: The focus crop is uploaded at (almost) its native resolution, so it
        #: gets its own, much larger, limits.
        self.focus_max_pixels = int(focus_max_pixels or 4_000_000)
        self.focus_max_dimension = int(focus_max_dimension or 2400)
        self.upscale_factor = max(1, int(upscale_factor or 1))

        self._client = LensClient(
            timeout=self.lens_timeout,
            max_retries=self.lens_retries,
            backoff_base=self.lens_retry_backoff or 0.5,
            language=self.lens_language or "",
        )

        #: Reuse an earlier read of the same pixels (see :mod:`ocr.cache`).
        #: ``None`` disables the cache, which is what the unit tests want.
        from ocr.cache import OCRCache

        self._cache = OCRCache(cache_dir) if cache_dir else None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------
    def _cache_settings(self, variant: str, upscale_factor: int) -> Dict[str, Any]:
        """The settings that make two reads of one file interchangeable."""
        if variant == "focus":
            return {
                "upscale": int(upscale_factor or 1),
                "jpeg_quality": self.jpeg_quality,
                "max_pixels": self.focus_max_pixels,
                "max_dimension": self.focus_max_dimension,
                "focus_ratio": self.focus_ratio,
            }
        return {
            "upscale": int(upscale_factor or 1),
            "jpeg_quality": self.jpeg_quality,
            "max_pixels": self.max_pixels,
            "max_dimension": self.max_dimension,
        }

    def _cached_read(
        self, path: str, variant: str, upscale_factor: int
    ) -> Optional[str]:
        """The stored text for ``path``, or ``None`` when there is none."""
        if self._cache is None or not os.path.isfile(path):
            return None
        try:
            digest = self._cache.file_digest(path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not hash %s for the OCR cache: %s", path, exc)
            return None
        return self._cache.get(
            digest, variant, self._cache_settings(variant, upscale_factor)
        )

    def _store_read(
        self, path: str, variant: str, upscale_factor: int, text: str
    ) -> None:
        if self._cache is None:
            return
        try:
            digest = self._cache.file_digest(path)
        except Exception:  # pragma: no cover - defensive
            return
        self._cache.put(
            digest, variant, self._cache_settings(variant, upscale_factor), text
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, image: np.ndarray) -> OCRResult:
        """Run Google Lens OCR on a BGR numpy image (full page)."""
        if image is None:
            return OCRResult(error="no_image")
        try:
            payload = self._encode(image)
        except Exception as exc:  # pragma: no cover - defensive
            return OCRResult(engine="lens", error=f"image_error: {exc}")
        return self._read(payload)

    def run_on_file(self, path: str, upscale_factor: int = 1) -> OCRResult:
        """Run Google Lens OCR on an image file (full page).

        The file is decoded locally (OpenCV), shrunk to the upload limits and
        encoded as JPEG — the upload always carries real pixels, so the
        engine knows the dimensions it sends.  ``upscale_factor`` enlarges
        the image before the upload, which helps very small screenshots.
        """
        try:
            image = self._load(path, upscale_factor=upscale_factor)
        except FileNotFoundError:
            return OCRResult(engine="lens", error=f"missing_file: {path}")
        except Exception as exc:
            return OCRResult(engine="lens", error=f"image_error: {exc}")

        cached = self._cached_read(path, "full", upscale_factor)
        if cached is not None:
            return self._result(cached, engine="lens-cache")
        result = self.run(image)
        if result.error is None:
            self._store_read(path, "full", upscale_factor, result.text)
        return result

    def run_focused(self, image: np.ndarray) -> OCRResult:
        """OCR only the enlarged header of an image.

        The order-id line of a Meesho screenshot sits in the top few percent
        of a very tall image, so the crop is what makes the digits readable.
        A failure here is reported like any other OCR failure — the caller
        decides whether it can afford to ignore it.
        """
        if image is None:
            return OCRResult(error="no_image")
        crop = self._focus_crop(image)
        if crop is None:
            return OCRResult(engine="lens", error="no_focus_region")
        try:
            payload = self._encode(
                crop,
                max_pixels=self.focus_max_pixels,
                max_dimension=self.focus_max_dimension,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return OCRResult(engine="lens", error=f"image_error: {exc}")
        return self._read(payload)

    def run_focused_on_file(self, path: str, upscale_factor: int = 1) -> OCRResult:
        """Read an image file and OCR only its enlarged header."""
        try:
            image = self._load(path, upscale_factor=upscale_factor)
        except FileNotFoundError:
            return OCRResult(engine="lens", error=f"missing_file: {path}")
        except Exception as exc:
            return OCRResult(engine="lens", error=f"image_error: {exc}")

        cached = self._cached_read(path, "focus", upscale_factor)
        if cached is not None:
            return self._result(cached, engine="lens-cache")
        result = self.run_focused(image)
        if result.error is None:
            self._store_read(path, "focus", upscale_factor, result.text)
        return result

    def close(self) -> None:
        """Release the HTTP session."""
        try:
            self._client.close()
        except Exception:  # pragma: no cover - best effort cleanup
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _load(path: str, upscale_factor: int = 1) -> np.ndarray:
        """Read an image file into a BGR array (unicode paths included)."""
        from ocr.preprocess import load_image

        return load_image(path, upscale_factor=upscale_factor)

    def _resize_to_limits(
        self,
        image: np.ndarray,
        max_pixels: Optional[int] = None,
        max_dimension: Optional[int] = None,
    ) -> np.ndarray:
        """Shrink to the upload limits (Chromium parity: only when too big)."""
        import cv2

        limit_pixels = int(
            max_pixels if max_pixels is not None else self.max_pixels
        )
        limit_side = int(
            max_dimension if max_dimension is not None else self.max_dimension
        )
        height, width = image.shape[:2]
        if height <= 0 or width <= 0:
            return image
        area = height * width
        if area <= limit_pixels and width <= limit_side and height <= limit_side:
            return image
        scale = min(
            limit_side / float(width),
            limit_side / float(height),
            (limit_pixels / float(area)) ** 0.5,
        )
        scale = max(0.1, min(1.0, scale))
        return cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )

    def _encode(
        self,
        image: np.ndarray,
        max_pixels: Optional[int] = None,
        max_dimension: Optional[int] = None,
    ) -> Tuple[bytes, int, int]:
        """Return ``(jpeg_bytes, width, height)`` for the upload."""
        import cv2

        prepared = self._resize_to_limits(
            image,
            max_pixels=max_pixels if max_pixels is not None else self.max_pixels,
            max_dimension=(
                max_dimension if max_dimension is not None else self.max_dimension
            ),
        )
        ok, buffer = cv2.imencode(
            ".jpg", prepared, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if not ok:  # pragma: no cover - OpenCV always succeeds on a valid array
            raise ValueError("could not encode the image as JPEG")
        height, width = prepared.shape[:2]
        return buffer.tobytes(), width, height

    def _focus_crop(self, image: np.ndarray) -> Optional[np.ndarray]:
        """The header crop the focus pass looks at.

        The crop keeps the **native** resolution of the source image (it is
        only shrunk when it is wider than the focus limit).  That is the
        whole point of the pass: the full-page upload is downscaled to fit the
        upload limits, which makes the small ``Order #…`` line tiny — the
        crop is small enough to be sent at its original size, so the id line
        arrives at Lens with its original number of pixels.
        """
        import cv2

        height, width = image.shape[:2]
        if height < 80 or width < 80:
            return None
        cut = max(40, int(height * self.focus_ratio))
        crop = image[:cut, :]
        if crop.shape[1] > self.focus_max_dimension:
            scale = self.focus_max_dimension / float(crop.shape[1])
            crop = cv2.resize(
                crop,
                (self.focus_max_dimension, max(1, int(crop.shape[0] * scale))),
                interpolation=cv2.INTER_AREA,
            )
        return crop

    @staticmethod
    def _focus_adds_information(full_text: str, focus_text: str) -> bool:
        """True when the focus pass saw a digit run the full page did not.

        Keeps a correct full-page read from turning into two conflicting
        candidates, while still letting a zoomed read repair a garbled one.
        """
        if not focus_text.strip():
            return False
        known = set(_FOCUS_DIGIT_RUN.findall(full_text or ""))
        for run in _FOCUS_DIGIT_RUN.findall(focus_text):
            if run not in known:
                return True
        return False

    def _read(self, payload: Tuple[bytes, int, int]) -> OCRResult:
        """Upload the frame and turn the answer into an :class:`OCRResult`."""
        image_bytes, width, height = payload
        try:
            text = self._client.ocr(image_bytes, width=width, height=height)
        except LensError as exc:
            logger.warning("Google Lens OCR failed: %s", exc)
            return OCRResult(engine="lens", error=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Google Lens OCR failed: %s", exc)
            return OCRResult(engine="lens", error=f"{type(exc).__name__}: {exc}")
        return self._result(text)

    def _result(self, text: str, engine: str = "lens") -> OCRResult:
        """Build the result for a successful read."""
        text = (text or "").strip()
        if not text:
            # Lens answered but there was no text — that is not a failure.
            return OCRResult(engine=engine, error=None)
        return OCRResult(
            text=text,
            confidence=0.99,
            lines=_lines_from_text(text),
            engine=engine,
        )

    def merge_focus(self, full_text: str, focus_text: str) -> str:
        """Combine the full-page text with the focus text.

        The focus text is only added when it contributes a digit run the full
        page did not contain — a correct full read stays a single, confident
        candidate instead of turning into two conflicting ones.
        """
        focus_text = (focus_text or "").strip()
        if not self._focus_adds_information(full_text, focus_text):
            return full_text or ""
        return f"{full_text or ''}\n{focus_text}".strip()
def build_engine(
    config: Optional[Dict[str, Any]], cache_dir: Optional[str] = None
) -> OCREngine:
    """Factory: build the Google-Lens :class:`OCREngine` from ``config``.

    ``cache_dir`` overrides ``ocr.cache_dir`` (the app passes an absolute
    folder under ``assets/``).  Setting ``ocr.cache: false`` disables the
    cache entirely.
    """
    cfg = (config or {}).get("ocr", {}) or {}
    if bool(cfg.get("cache", True)):
        cache_dir = (
            cache_dir
            or str(cfg.get("cache_dir") or "").strip()
            or os.path.join("assets", ".ocr_cache")
        )
    else:
        cache_dir = None
    return OCREngine(
        lens_language=cfg.get("lens_language"),
        lens_timeout=float(cfg.get("lens_timeout", 15.0)),
        lens_retries=int(cfg.get("lens_retries", 2)),
        lens_retry_backoff=float(cfg.get("lens_retry_backoff", 0.5)),
        jpeg_quality=int(cfg.get("jpeg_quality", 90)),
        max_pixels=int(cfg.get("max_pixels", 1_500_000)),
        max_dimension=int(cfg.get("max_dimension", 1600)),
        focus_pass=bool(cfg.get("focus_pass", True)),
        focus_ratio=float(cfg.get("focus_ratio", 0.45)),
        upscale_factor=int(cfg.get("upscale_factor", 1)),
        cache_dir=cache_dir,
    )
