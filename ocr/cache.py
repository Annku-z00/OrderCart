"""
ocr/cache.py
============

An on-disk cache of Google Lens reads.

A run uploads every screenshot twice (the full page and the focus crop).
A **Reprocess** run then uploads the very same pictures again, although the
recognised text cannot change — the second upload is pure cost and pure
latency.  This module keeps the text next to the downloaded images, keyed by
a hash of the file bytes *and* the settings that affect the read (the pass,
the upscale factor and the upload limits).  Editing the image or changing a
setting therefore invalidates the entry automatically.

The cache is best effort by contract: an unreadable, unwritable or corrupt
cache never fails an OCR call - the engine simply uploads the image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: Bump when the way the text is produced changes, so old entries are ignored.
CACHE_VERSION = 1


class OCRCache:
    """Tiny JSON-per-image cache (one file per ``<digest, pass, settings>``)."""

    def __init__(self, directory: str) -> None:
        self.directory = str(directory or "").strip()
        self.enabled = bool(self.directory)
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    @staticmethod
    def file_digest(path: str) -> str:
        """SHA-1 of the image bytes (streamed - screenshots are a few MB)."""
        digest = hashlib.sha1()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def key(self, digest: str, variant: str, settings: dict) -> str:
        payload = json.dumps(
            [CACHE_VERSION, digest, variant, settings],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, key + ".json")

    # ------------------------------------------------------------------
    def get(self, digest: str, variant: str, settings: dict) -> Optional[str]:
        """The cached text, or ``None`` when there is no usable entry."""
        if not self.enabled:
            return None
        try:
            with open(self._path(self.key(digest, variant, settings)),
                      "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.misses += 1
            return None
        except Exception as exc:  # corrupt / unreadable entry
            logger.debug("OCR cache read failed: %s", exc)
            self.misses += 1
            return None

        text = data.get("text")
        if not isinstance(text, str):
            self.misses += 1
            return None
        self.hits += 1
        return text

    def put(self, digest: str, variant: str, settings: dict, text: str) -> None:
        """Store ``text`` (best effort - never raises)."""
        if not self.enabled:
            return
        path = self._path(self.key(digest, variant, settings))
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {"text": text or "", "variant": variant, "settings": settings},
                    handle,
                    ensure_ascii=False,
                )
            os.replace(tmp, path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("OCR cache write failed: %s", exc)
