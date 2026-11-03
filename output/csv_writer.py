"""
output/csv_writer.py
====================

CSV-mode output writer.

Columns::

    internal_id,order_id,name,address,status,source,confidence,partial,timestamp

* One chat may carry several orders, so ``write_result`` emits one row per
  distinct order id; fallbacks still emit one row.
* ``partial`` records whether the emitted id is a *crop* of a longer id
  (``true``) or a full id (``false``).  It is what makes a bare partial value
  (``extraction.partial_id_prefix: false``) distinguishable from a full id.
* Writes are **idempotent**: a row identical (in every column except the
  timestamp) to one already in the file is skipped, so a safe retry after a
  partial media/OCR failure never duplicates rows.
* Address newlines are joined with ``|`` (as required by the spec).
* Writes are flushed after every row so a crash cannot lose data.
* The header is written once when the file is first created.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

CSV_HEADER = [
    "internal_id",
    "order_id",
    "name",
    "address",
    "status",
    "source",
    "confidence",
    "partial",
    "timestamp",
]


def _as_ids(order_ids: Union[str, Sequence[str], None]) -> List[str]:
    """Coerce a single id or a list of ids into a clean list of strings."""
    if order_ids is None:
        return []
    if isinstance(order_ids, str):
        return [order_ids.strip()] if order_ids.strip() else []
    return [str(x).strip() for x in order_ids if str(x).strip()]


class CSVWriter:
    """Append-only CSV writer with a fixed schema and per-row flushing."""

    def __init__(
        self,
        path: str,
        encoding: str = "utf-8",
        date_format: str = "%Y-%m-%d %H:%M:%S",
    ) -> None:
        self.path = path
        self.encoding = encoding
        self.date_format = date_format
        self._seen_cache: Optional[set] = None
        self._ensure_parent()

    # ------------------------------------------------------------------
    def _ensure_parent(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _needs_header(self) -> bool:
        return not os.path.exists(self.path) or os.path.getsize(self.path) == 0

    # ------------------------------------------------------------------
    def write_result(
        self,
        internal_id: Union[int, str],
        result: Dict[str, Any],
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append one CSV row per distinct order id in ``result``.

        A multi-order chat yields several rows (same ``internal_id``, different
        ``order_id``).  When the result carries an ``order_rows`` list, every
        row keeps its own status and ``partial`` flag — a chat can mix a
        complete order id with a cut-off one (``TRUNC:…``).  Rows identical to
        ones already in the file (ignoring the timestamp) are skipped so a
        safe retry never duplicates output.  Returns the first row that was
        built.
        """
        result = result or {}
        status = str(result.get("type") or result.get("status") or "FAILED").upper()

        rows: List[Dict[str, Any]] = []
        order_rows = result.get("order_rows") or []
        if order_rows:
            for entry in order_rows:
                row_name = None
                row_address = None
                if isinstance(entry, dict):
                    value = str(entry.get("value") or "").strip()
                    row_status = str(entry.get("status") or status).upper()
                    row_partial = bool(entry.get("partial"))
                    # A chat can carry many orders, each with its own
                    # customer.  The per-row details win over the chat's —
                    # and an *empty* per-row detail is a deliberate "nothing
                    # was found for this order", so it must not fall back to
                    # the chat-level name.
                    if "name" in entry:
                        row_name = str(entry.get("name") or "")
                    if "address" in entry:
                        row_address = str(entry.get("address") or "")
                else:
                    value = str(entry or "").strip()
                    row_status = status
                    row_partial = False
                if not value:
                    continue
                row = self._build_row(
                    internal_id, result, value, row_status, timestamp,
                    name_override=row_name, address_override=row_address,
                )
                row["partial"] = "true" if row_partial else row["partial"]
                rows.append(row)
        else:
            order_ids = _as_ids(result.get("order_ids")) or _as_ids(result.get("order_id"))
            if not order_ids:
                order_ids = [""]
            rows = [
                self._build_row(internal_id, result, order_id, status, timestamp)
                for order_id in order_ids
            ]

        for row in rows:
            key = self._row_key(row)
            if key in self._seen_rows():
                continue
            self.append_row(row)
            self._seen_rows().add(key)
        return rows[0] if rows else {}

    def _build_row(
        self,
        internal_id: Union[int, str],
        result: Dict[str, Any],
        order_id: str,
        status: str,
        timestamp: Optional[str],
        name_override: Optional[str] = None,
        address_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = (name_override if name_override is not None
                else (result.get("name") or "")).strip()
        address_raw = (address_override if address_override is not None
                       else result.get("address")) or ""
        # CSV keeps everything on one line -> newlines become '|'.
        address = "|".join(
            part.strip() for part in str(address_raw).splitlines() if part.strip()
        )
        return {
            "internal_id": str(internal_id),
            "order_id": order_id,
            "name": name,
            "address": address,
            "status": status,
            "source": result.get("source", "") or "",
            "confidence": f"{float(result.get('confidence', 0.0)):.2f}",
            "partial": "true" if result.get("partial") else "false",
            "timestamp": timestamp or datetime.now().strftime(self.date_format),
        }

    @staticmethod
    def _row_key(row: Dict[str, Any]) -> tuple:
        """Identity of a row for idempotent writing (timestamp excluded)."""
        return (
            str(row.get("internal_id", "")).strip(),
            str(row.get("order_id", "")).strip(),
            str(row.get("name", "")).strip(),
            str(row.get("address", "")).strip(),
            str(row.get("status", "")).strip(),
        )

    def _seen_rows(self) -> set:
        """Lazily load row identities already on disk (for idempotent writes)."""
        if self._seen_cache is None:
            self._seen_cache = {self._row_key(row) for row in self.read_rows()}
        return self._seen_cache

    def append_row(self, row: Dict[str, Any]) -> None:
        """Append a pre-built row dict, writing the header when needed."""
        write_header = self._needs_header()
        with open(self.path, "a", encoding=self.encoding, newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADER, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in CSV_HEADER})
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover
                pass

    def read_rows(self) -> list:
        """Return all rows as list-of-dicts (used by tests / UI)."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding=self.encoding, newline="") as fh:
            return list(csv.DictReader(fh))

    # ------------------------------------------------------------------
    # Manual corrections (TRUNC / FAILED review workflow)
    # ------------------------------------------------------------------
    def apply_corrections(
        self,
        corrections: Dict[Any, str],
        status: str = "OK",
        source: str = "manual",
        matches: Optional[Dict[Any, str]] = None,
    ) -> int:
        """Fill in the real Order ID for ``TRUNC`` / ``FAILED`` rows.

        Matching is done on the ``internal_id`` column.  The corrected row gets
        the pasted ``order_id``, ``status`` (default ``OK``) and ``source``
        (default ``manual``).

        ``matches`` is optional: it maps an internal id to the **current**
        ``order_id`` of the row that should be corrected.  A chat can carry
        several orders (one row each), so without it a correction would
        overwrite every row of that chat.  Returns the number of rows changed.
        """
        normalized: Dict[str, str] = {}
        for key, value in (corrections or {}).items():
            text = str(value).strip()
            if not text:
                continue
            try:
                normalized[str(int(key))] = text
            except (TypeError, ValueError):
                normalized[str(key).strip()] = text
        wanted: Dict[str, str] = {}
        for key, value in (matches or {}).items():
            try:
                wanted[str(int(key))] = str(value).strip()
            except (TypeError, ValueError):
                wanted[str(key).strip()] = str(value).strip()
        if not normalized or not os.path.exists(self.path):
            return 0

        rows = self.read_rows()
        changed = 0
        now = datetime.now().strftime(self.date_format)
        for row in rows:
            internal_id = str(row.get("internal_id", "")).strip()
            if internal_id in normalized:
                if internal_id in wanted and wanted[internal_id] != str(
                    row.get("order_id", "")
                ).strip():
                    # Another row of the same chat - leave it alone.
                    continue
                row["order_id"] = normalized[internal_id]
                row["status"] = status
                row["source"] = source
                row["timestamp"] = now
                changed += 1

        if changed:
            self._rewrite(rows)
        return changed

    def _rewrite(self, rows: list) -> None:
        """Atomically rewrite the file (header + every row)."""
        self._seen_cache = None  # file content changed; reload on next write
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding=self.encoding, newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADER, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in CSV_HEADER})
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover
                pass
        try:
            os.replace(tmp_path, self.path)
        except PermissionError:  # pragma: no cover - target locked
            with open(self.path, "w", encoding=self.encoding, newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_HEADER, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in CSV_HEADER})
            try:
                os.remove(tmp_path)
            except OSError:
                pass
