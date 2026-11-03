"""
ocr/preprocess.py
=================

Minimal image preprocessing.

Design decision (Phase 2 update)
--------------------------------
Earlier drafts applied grayscale + denoise + adaptive threshold.  In practice
Meesho/Flipkart/Amazon order screenshots are **already crisp digital images**
(not photos of a screen), and the binarisation step *hurt* accuracy.

So we now do the simplest thing that works: pass the clear screenshot straight
through (to the OCR engine).  We only:

* read the file with OpenCV,
* optionally upscale by an integer factor (kept as a no-op by default),
* return a BGR numpy array ready for the OCR engine.

No grayscale, no denoising, no thresholding.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def preprocess_image(
    image: np.ndarray,
    upscale_factor: int = 1,
) -> np.ndarray:
    """Return the image ready for OCR.

    Parameters
    ----------
    image:
        BGR image loaded by OpenCV (or a numpy array of the same shape).
    upscale_factor:
        Integer zoom factor. ``1`` (default) leaves the image untouched.
        Higher values help tiny screenshots; blurring is intentionally omitted.

    Returns
    -------
    numpy.ndarray
        The (optionally upscaled) BGR image.
    """
    if image is None:
        raise ValueError("preprocess_image received a None image")

    if upscale_factor and upscale_factor > 1:
        image = cv2.resize(
            image,
            None,
            fx=float(upscale_factor),
            fy=float(upscale_factor),
            interpolation=cv2.INTER_CUBIC,
        )
        logger.debug("Upscaled image by %dx -> %s", upscale_factor, image.shape)

    return image


def load_image(path: str, upscale_factor: int = 1) -> np.ndarray:
    """Read an image from disk and return a preprocessed BGR array.

    Raises
    ------
    FileNotFoundError
        When the path does not exist.
    ValueError
        When OpenCV cannot decode the file (corrupt / unsupported format).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Image not found: {path}")

    # np.fromfile handles unicode paths on Windows that cv2.imread chokes on.
    try:
        buffer = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"Failed to read image {path}: {exc}") from exc

    if image is None:
        raise ValueError(f"OpenCV could not decode image: {path}")

    return preprocess_image(image, upscale_factor=upscale_factor)


def to_ocr_input(image: np.ndarray) -> np.ndarray:
    """Return the array exactly as the OCR engine expects it (unchanged BGR)."""
    return image
