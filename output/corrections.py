"""
output/corrections.py
=====================

Format-agnostic entry point for the TRUNC / FAILED review workflow.

After a run the GUI shows every ``TRUNC`` and ``FAILED`` id with an editable
field.  When the seller pastes the correct Order ID and hits **Save
corrections**, :func:`apply_corrections` rewrites just those rows in whichever
output file the run produced.

Keeping the dispatch here means the GUI never has to know which writer class is
behind the file — it passes a path + format and gets back the number of rows
that changed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

from output.csv_writer import CSVWriter
from output.writer import NotepadWriter


def apply_corrections(
    path: str,
    corrections: Mapping[Any, str],
    output_format: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    matches: Optional[Mapping[Any, str]] = None,
    notepad_mode: Optional[str] = None,
) -> int:
    """Rewrite ``path`` so the corrected rows carry the pasted Order IDs.

    Parameters
    ----------
    path:
        The output file produced by the run (``orders.txt`` / ``orders.csv``).
    corrections:
        Mapping of ``internal_id -> order_id``.  Entries with a blank value are
        ignored.
    output_format:
        ``"notepad"`` or ``"csv"``.  When omitted it is inferred from the file
        extension (``.csv`` -> CSV, anything else -> Notepad).
    config:
        The parsed ``config.yaml`` dict (used for the CSV date format).
    matches:
        Optional mapping of ``internal_id -> current value`` that pins the
        correction to one specific row.  A chat can carry several orders (one
        output row each), so without it a correction would rewrite every row
        of that chat.
    notepad_mode:
        ``"append"`` (default) or ``"fill"``.  ``fill`` updates the matching
        line of the seller's Notepad++ master list in place.

    Returns
    -------
    int
        The number of rows that were changed (0 when nothing matched).
    """
    if not path:
        return 0

    fmt = (output_format or "").strip().lower()
    if not fmt:
        fmt = "csv" if str(path).lower().endswith(".csv") else "notepad"

    if fmt == "csv":
        date_format = (
            (config or {}).get("output", {}) or {}
        ).get("date_format", "%Y-%m-%d %H:%M:%S")
        writer = CSVWriter(path, date_format=date_format)
    else:
        writer = NotepadWriter(path, mode=notepad_mode or "append")

    return writer.apply_corrections(corrections, matches=matches)
