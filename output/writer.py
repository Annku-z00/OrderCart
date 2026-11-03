"""
output/writer.py
================

Notepad-mode output writer.

Output format (one line per order value)::

    12988,342022570640639296     -> full order id
    12988,342022570640639297     -> a chat's *second* order (multi-order row)
    12989,TRUNC:34187320244      -> truncated order id
    12990,NAME:Priya Sharma      -> name fallback
    12991,ADDR:12 MG Road, Pune  -> address fallback
    12992,FAILED                 -> nothing extracted

With ``mode="fill"`` the writer instead updates a seller master list in place::

    12328                         -> 12328<TAB>3420026566347011
    12329                         -> 12329<TAB>341999308084544

A chat that yields no order id falls back to the customer **name**, then the
**address**, written plain so the column keeps one value.  Every master-list
line carries **exactly one** value - never several ids - and a chat with
several orders writes the name they share, or the most common name when they
differ, or the first order id when no name was sent (the app then flags the
row for review).  A **cropped** order id is the
order id from six digits on, so a screenshot the customer cut still fills the
line; only a chat with nothing readable at all stays blank.

Notes
-----
* One chat may carry several orders, so ``write_result`` emits one row per
  distinct order id (``result["order_ids"]``); fallbacks still emit one row.
* Writes are **idempotent**: an identical ``<id>,<value>`` line that already
  exists in the file is skipped, so a safe retry (after a partial media/OCR
  failure) never duplicates rows.
* Every row is flushed to disk immediately (append + flush + fsync) so a crash
  never loses already-processed ids.
* For the atomic-append guarantee we write into ``<file>.tmp`` for full
  rewrites, but the common path is a straight ``open(..., "a")`` append, which
  is already safe (no truncation of existing data).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

#: A master-list line: ``12328<TAB>3420026566347011`` / ``12328`` / ``12328,342...``.
#: Only the leading internal id and everything after it are needed.
_MASTER_LINE_RE = re.compile(r"^(\s*)(\d+)(.*)$")

#: Values that are *not* a real order id and must never be pasted into the
#: seller's master list next to an internal id.
_NON_ID_VALUES = {"FAILED", "MISSING", "NONE", "N/A", "NA", "-"}


def _normalize_ids(order_ids: Union[str, Sequence[str], None]) -> List[str]:
    """Coerce a single id or a list of ids into a clean list of strings."""
    if order_ids is None:
        return []
    if isinstance(order_ids, str):
        return [order_ids] if order_ids.strip() else []
    return [str(x).strip() for x in order_ids if str(x).strip()]


def _same_id(line_id: str, key: str) -> bool:
    """True when a master-list line belongs to the run's internal id.

    Compared **numerically**, so the seller's own spelling always wins: a line
    written ``012330`` still matches chat ``12330`` and keeps its leading zero
    (the writer never rewrites the id the seller typed).  Non-numeric text
    falls back to an exact string compare.
    """
    if line_id == key:
        return True
    try:
        return int(line_id) == int(key)
    except (TypeError, ValueError):
        return False


def format_notepad_line(internal_id: Union[int, str], result: Dict[str, Any]) -> str:
    """Build the comma-separated Notepad line for one internal id.

    ``result`` is the dict returned by :func:`extractor.parser.extract_fields`
    (or an equivalent mapping produced by the orchestrator).  Supported shapes:

    * ``{"type": "OK", "order_id": "...", "order_ids": ["..."]}``
    * ``{"type": "TRUNC", "order_id": "TRUNC:..."}`` (already prefixed)
    * ``{"type": "FALLBACK_NAME", "name": "..."}``
    * ``{"type": "FALLBACK_ADDRESS", "address": "..."}``
    * ``{"type": "FAILED"}``

    A row is produced for the *first* value only — use
    :func:`format_notepad_lines` when a chat may carry several order ids.
    """
    return format_notepad_lines(internal_id, result)[0]


def _notepad_values(result: Dict[str, Any]) -> List[str]:
    """Return every output value for ``result`` (one per order id or fallback)."""
    result = result or {}
    status = str(result.get("type") or result.get("status") or "").upper()

    # ``order_rows`` carries the full row list (complete ids *and* cut-off
    # ones) when the run produced several values for one chat.
    rows = result.get("order_rows") or []
    if rows:
        values = []
        for entry in rows:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "").strip()
            else:
                value = str(entry or "").strip()
            if value:
                values.append(value)
        if values:
            return values

    # --- full / truncated order id(s) ---
    # ``order_ids`` (multi-order support) wins; fall back to the single
    # ``order_id`` key, which may itself be a list or a "TRUNC:1234" string.
    ids = _normalize_ids(result.get("order_ids"))
    if not ids:
        ids = _normalize_ids(result.get("order_id"))
    if ids:
        return list(ids)

    if status == "TRUNC" and result.get("value"):
        return [f"TRUNC:{result['value']}"]
    if status in ("FALLBACK_NAME", "NAME") and result.get("name"):
        return [f"NAME:{result['name']}"]
    if status in ("FALLBACK_ADDRESS", "ADDR", "ADDRESS") and result.get("address"):
        # Collapse internal newlines so the row stays on a single line.
        return [f"ADDR:{' '.join(str(result['address']).split())}"]
    if status == "FAILED":
        return ["FAILED"]
    if result.get("name"):
        return [f"NAME:{result['name']}"]
    if result.get("address"):
        return [f"ADDR:{' '.join(str(result['address']).split())}"]
    return ["FAILED"]


def _is_printable_order_id(value: str) -> bool:
    """True when ``value`` is a real order id worth pasting into Notepad++.

    ``TRUNC:`` / ``NAME:`` / ``ADDR:`` / ``FAILED`` are *not* order numbers and
    would only clutter the seller's master list.  A six-digit crop, by
    contrast, is a valid key and is kept.
    """
    text = (value or "").strip()
    if not text:
        return False
    upper = text.upper()
    if upper.startswith("TRUNC:"):
        return False
    if upper.startswith("NAME:") or upper.startswith("ADDR:"):
        return False
    if upper in _NON_ID_VALUES:
        return False
    return True


def _single_line(value: Any) -> str:
    """Collapse newlines / tabs so a name or address stays on one master line."""
    return " ".join(str(value or "").split())


def _is_order_number(value: str) -> bool:
    """True when an **existing** line value is a bare order number.

    Used to decide what already sits next to an internal id.  A real order id
    is digits only, so a name (``Priya Sharma``), an address (``12 MG Road``)
    or a ``TRUNC:`` placeholder is recognised as weaker and may be replaced by
    a real id on a later run.
    """
    text = (value or "").strip()
    return bool(text) and text.isdigit()


def _order_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The per-order rows the orchestrator attaches to a result."""
    return [
        row for row in (result.get("order_rows") or [])
        if isinstance(row, dict) and str(row.get("value") or "").strip()
    ]


#: A **cropped** order id is still the order id from this many digits on
#: (``extraction.truncated_id_min_digits`` in ``config.yaml``).  The customers
#: crop the back of the Meesho screen, so the visible ``Order #342187`` is all
#: there is of that order - but a five-digit or shorter crop is not an id.
_MIN_CROPPED_ID_DIGITS = 6


def _order_id_value(row: Dict[str, Any]) -> str:
    """The order id a row contributes, or ``""`` when the row is not an id.

    A ``TRUNC`` row is a **cropped** screenshot: from six digits on its digits
    *are* the order id and are written as they stand.  A shorter crop is not
    an id, so it is ignored here and the name / address fallback applies
    instead of pasting a fragment into the seller's list.
    """
    value = str(row.get("value") or "").strip()
    if value.upper().startswith("TRUNC:"):
        value = value[len("TRUNC:"):].strip()
    if not _is_printable_order_id(value):
        return ""
    status = str(row.get("status") or "").strip().upper()
    if status == "TRUNC" and len(re.sub(r"\D", "", value)) < _MIN_CROPPED_ID_DIGITS:
        return ""
    return value


def _most_common(values: List[str]) -> str:
    """Return the most frequent value, compared case-insensitively.

    Ties keep the first value seen, so the result is always one of the values
    the seller actually received - it is never invented.
    """
    counts: Dict[str, int] = {}
    first_seen: Dict[str, str] = {}
    for value in values:
        key = value.casefold()
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, value)
    best_key = ""
    best_count = 0
    for key, count in counts.items():
        if count > best_count:
            best_key, best_count = key, count
    return first_seen.get(best_key, "")


def _fill_choice(result: Dict[str, Any]) -> tuple[str, str, str]:
    """Decide the **single** value for one master-list line.

    A master-list line carries exactly one value - never a row of order ids -
    and it is **never left blank just because the chat was ambiguous**.  The
    seller would rather see the name they received than an empty cell.

    Returns ``(value, notice, kind)``.  ``kind`` is the short, greppable
    warning label shown in the app (``"MULTI: names differ"``) so the seller
    can tell at a glance **which** check is needed; it is empty when there is
    nothing to report.

    * ``(order_id, "", "")`` - the chat has exactly one order.  A **cropped**
      id counts as an order from six digits on (see :func:`_order_id_value`),
      so a screenshot cut at the back still fills the line.
    * ``(name, "", "")`` - several orders, every one carrying the same name.
    * ``(name, notice, kind)`` - several orders where the names are **mixed**
      or some are **missing**: the most common name is used (the first one on
      a tie) and the notice asks the app to flag the row for review.  Nothing
      is invented - the name is always one the customer actually sent.  One
      name among three orders is the normal outcome of a cropped screenshot,
      so it fills the line and only the app carries the flag.
    * ``(address, notice, kind)`` / ``("", notice, kind)`` - several orders and
      **no name at all**: an order id is **never** written for a multi-order
      chat (one id would read like a single-order chat), so the address is
      used; with no address either the line stays blank and the app carries
      the warning.
    * ``(address, "", "")`` - no usable order id at all and no name; the
      address is the last thing that identifies the chat.
    * ``(name_or_address, "", "")`` - no order id at all: the normal fallback.

    Names and addresses are compared case- and whitespace-insensitively, so
    OCR noise such as ``Priya  Sharma`` still matches ``Priya Sharma``.
    """
    rows = _order_rows(result)
    if rows:
        pairs = [
            (
                _order_id_value(row),
                _single_line(row.get("name")),
                _single_line(row.get("address")),
            )
            for row in rows
        ]
        usable = [pair for pair in pairs if pair[0]]
        if not usable:
            # Not one usable id: every screenshot was too short a crop, or the
            # chat carried no order at all.  The name / address of those same
            # rows is the fallback - a five-digit fragment is never written.
            names = [name for _, name, _ in pairs if name]
            if names:
                return _most_common(names), "", ""
            addresses = [address for _, _, address in pairs if address]
            if addresses:
                return _most_common(addresses), "", ""
            return _fill_fallback_value(result), "", ""
        pairs = usable
    else:
        # Older result shapes carry ids without per-order names - trust the
        # count, but never fall back to writing several ids on one line.
        values = [
            value for value in _notepad_values(result)
            if _is_printable_order_id(value)
        ]
        pairs = [(value, "", "") for value in values]

    if not pairs:
        return _fill_fallback_value(result), "", ""

    if len(pairs) == 1:
        return pairs[0][0], "", ""

    names = [name for _, name, _ in pairs]
    present = [name for name in names if name]
    if (
        present
        and len(present) == len(names)
        and len({name.casefold() for name in present}) == 1
    ):
        # Every order, one customer: the clean case, nothing to flag.
        return present[0], "", ""

    if present:
        chosen = _most_common(present)
        missing = len(names) - len(present)
        if missing:
            kind = "MULTI: name missing"
            reason = f"{missing} without a name"
        else:
            kind = "MULTI: names differ"
            reason = "the names differ"
        return chosen, (
            f"{kind} - {len(pairs)} orders in this chat ({reason}); "
            f"the list used the name '{chosen}'"
        ), kind

    # Two or more orders: never an order id on the line - one of several ids
    # would read like a single-order chat.  The name identifies the customer;
    # the address is the last resort, then the line stays blank and the app
    # carries the warning.
    kind = "MULTI: no name"
    addresses = [address for _, _, address in pairs if address]
    if not addresses:
        chat_address = _single_line(result.get("address"))
        addresses = [chat_address] if chat_address else []
    if addresses:
        chosen_address = _most_common(addresses)
        return chosen_address, (
            f"{kind} - {len(pairs)} orders in this chat, no customer name "
            f"detected; the list used the address"
        ), kind

    return "", (
        f"{kind} - {len(pairs)} orders in this chat, no customer name and no "
        "address detected"
    ), kind


def master_list_value(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, str, str]:
    """Return the single master-list value for one chat's order rows.

    Public wrapper around :func:`_fill_choice` so the Orders **export** and the
    in-place notepad writer can never disagree.  ``rows`` use the Orders-table
    keys (``order_id`` / ``name`` / ``address``), one row per order.

    Returns ``(value, notice, kind)`` exactly like :func:`_fill_choice`; a
    ``FAILED`` / empty chat gives ``""``.
    """
    converted: List[Dict[str, Any]] = []
    for row in rows:
        value = str(row.get("order_id") or "").strip()
        status = "TRUNC" if value.upper().startswith("TRUNC:") else "OK"
        if status == "TRUNC":
            value = value[len("TRUNC:"):].strip()
        converted.append({
            "value": value,
            "status": status,
            "name": str(row.get("name") or "").strip(),
            "address": str(row.get("address") or "").strip(),
        })

    # The chat-level fallbacks mirror the orchestrator's result shape, so a
    # name-only / address-only row still resolves.
    return _fill_choice({
        "order_rows": converted,
        "name": next((row["name"] for row in converted if row["name"]), ""),
        "address": next(
            (row["address"] for row in converted if row["address"]), ""
        ),
    })


def _fill_fallback_value(result: Dict[str, Any]) -> str:
    """Return the plain name/address to paste when a chat has no order id.

    The fallback chain is the same one the rest of the app uses —
    **order id -> name -> address** — but written *without* the ``NAME:`` /
    ``ADDR:`` marker so the master list keeps a single plain column, exactly
    like an order-id line.

    Returns ``""`` when there is nothing to paste: a ``FAILED`` chat, or one
    whose only value is a cut-off ``TRUNC:`` id (a partial order number is
    still an order number — a name must never displace it).
    """
    for value in _notepad_values(result):
        text = str(value or "").strip()
        if not text:
            continue
        upper = text.upper()
        if upper.startswith("NAME:"):
            return _single_line(text[len("NAME:"):])
        if upper.startswith("ADDR:"):
            return _single_line(text[len("ADDR:"):])
        # A real id, a TRUNC: crop or FAILED — the fallback chain stops here.
        break
    return ""


def format_notepad_lines(
    internal_id: Union[int, str], result: Dict[str, Any]
) -> List[str]:
    """Build **every** Notepad line for one internal id.

    Multi-order chats yield one line per distinct order id, so a chat with three
    orders produces three ``<internal_id>,<order_id>`` rows.  Fallbacks
    (``TRUNC:`` / ``NAME:`` / ``ADDR:`` / ``FAILED``) still produce exactly one
    row.
    """
    return [f"{internal_id},{value}" for value in _notepad_values(result)]


class NotepadWriter:
    """Append-only writer producing the ``id,value`` Notepad format.

    Two layouts are supported:

    * ``mode="append"`` (default) — append ``<internal_id>,<order_id>`` rows at
      the end of the file.  This is the original format used by the tests and
      by ``default_file`` when it points at a ``.txt``.
    * ``mode="fill"`` — update the seller's existing master list **in place**:
      a line ``12328`` becomes ``12328<TAB>3420026566347011`` and an existing
      value is extended rather than duplicated.  A chat with no order id gets
      its name, else its address.  A row is only added for an id that has no
      line yet when the list is still new/empty (one list per day/week is the
      seller's workflow), otherwise the mismatch is logged.
    """

    def __init__(
        self, path: str, encoding: str = "utf-8", mode: str = "append"
    ) -> None:
        self.path = path
        self.encoding = encoding
        self.mode = "fill" if str(mode or "").strip().lower() == "fill" else "append"
        self._seen_cache: Optional[set] = None
        #: Set by the last :meth:`write_result` call when the chat carried
        #: several orders and the line had to be summarised.  The orchestrator
        #: turns it into a visible WARN line and a "Needs review" flag.
        self.last_notice: str = ""
        #: Short label for the same event (``"MULTI: names differ"`` /
        #: ``"MULTI: name missing"`` / ``"MULTI: no name"``), shown in the app
        #: so the seller knows which check to run.  Empty when nothing happened.
        self.last_notice_kind: str = ""
        self._ensure_parent()

    # ------------------------------------------------------------------
    def _ensure_parent(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def has_line(self, internal_id: Union[int, str]) -> bool:
        """True when the master list already has a line for ``internal_id``.

        The seller builds the list with the day's chat ids already in it, so a
        missing line means the list and the run range are out of sync.  The
        orchestrator uses this to say so out loud instead of silently filling
        nothing.
        """
        key = str(internal_id).strip()
        if not key:
            return False
        for line in self._read_raw_lines():
            match = _MASTER_LINE_RE.match(line)
            if match and _same_id(match.group(2), key):
                return True
        return False

    # ------------------------------------------------------------------
    def write_result(
        self,
        internal_id: Union[int, str],
        result: Dict[str, Any],
        order_ids: Optional[Union[str, Sequence[str]]] = None,
    ) -> str:
        """Append one row **per distinct order id** for ``internal_id``.

        A multi-order chat produces several ``<internal_id>,<order_id>`` rows;
        a fallback (``TRUNC:`` / ``NAME:`` / ``ADDR:`` / ``FAILED``) produces a
        single row.  Rows identical to ones already in the file are skipped, so
        re-running a partially-failed chat never duplicates its output.  Returns
        the newline-joined lines that the result *would* produce.
        """
        if order_ids is not None:
            result = dict(result or {})
            result["order_ids"] = _normalize_ids(order_ids)
        self.last_notice = ""
        self.last_notice_kind = ""
        if self.mode == "fill":
            return self._fill_result(internal_id, result)

        lines = format_notepad_lines(internal_id, result)
        for line in lines:
            if line in self._seen_lines():
                continue
            self.append_line(line)
            self._seen_lines().add(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Master-list ("fill") mode
    # ------------------------------------------------------------------
    def _read_raw_lines(self) -> List[str]:
        """Every line, including the blank placeholder lines in a master list."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding=self.encoding) as fh:
            return [line.rstrip("\n") for line in fh]

    @staticmethod
    def _split_values(rest: str) -> tuple[str, List[str]]:
        """Return ``(separator, existing_values)`` for the text after an id.

        Only the separator actually in use is split on, so an address that
        contains a comma (``12 MG Road, Pune``) stays one value on a
        tab-separated line instead of being shredded into several.
        """
        if not rest.strip():
            return "\t", []
        if rest.startswith("\t"):
            parts = rest[1:].split("\t")
            sep = "\t"
        elif rest.startswith(","):
            parts = rest[1:].split(",")
            sep = ","
        else:
            # A legacy "12328 342..." line is normalised to a tab.
            return "\t", [rest.strip()]
        values = [part.strip() for part in parts if part.strip()]
        return sep, values

    def _fill_result(
        self, internal_id: Union[int, str], result: Dict[str, Any]
    ) -> str:
        """Place the single value for ``internal_id`` on its master-list line.

        The line always ends up with **one** value, never a row of ids::

            12328<TAB>3420026566347011     one order -> its order id
            12329<TAB>Priya Sharma         4 orders, all the same customer -> name
            12330<TAB>12 MG Road, Pune     no id, no name -> the address
            12331                          nothing readable -> blank

        When a chat carries several orders the line is still filled: the name
        they share, the most common name when they differ, or the first order
        id when no name was sent.  :attr:`last_notice` explains what happened
        and :attr:`last_notice_kind` labels it (``MULTI: ...``) so the app can
        flag the row - the notepad file itself stays clean.

        A plain value only ever fills a **blank** line: once a real id (or a
        ``TRUNC:`` crop) is there, a name never overwrites it.
        """
        key = str(internal_id).strip()
        if not key:
            return ""

        decision, notice, kind = _fill_choice(result)
        self.last_notice = notice
        self.last_notice_kind = kind
        if not decision:
            # Nothing could be written (no name, no address): the line stays
            # blank and the app carries the warning.
            return ""

        if _is_order_number(decision):
            return self._fill_id_value(key, decision)
        return self._fill_plain_value(key, decision)

    def _fill_id_value(self, key: str, value: str) -> str:
        """Paste one real order id next to ``key`` in the master list."""
        values = [value]
        lines = self._read_raw_lines()
        changed = False
        matched = False
        for index, line in enumerate(lines):
            match = _MASTER_LINE_RE.match(line)
            if not match or not _same_id(match.group(2), key):
                continue
            matched = True
            prefix = f"{match.group(1)}{match.group(2)}"
            sep, existing = self._split_values(match.group(3))
            # A previous run may have written a placeholder (TRUNC:) or a
            # name/address fallback; a real id replaces it instead of sitting
            # next to it.
            existing = [
                existing_value for existing_value in existing
                if _is_order_number(existing_value)
            ]
            new_values = [value for value in values if value not in existing]
            if not new_values:
                return "\n".join(existing)
            updated = prefix + sep + sep.join(existing + new_values)
            if updated != line:
                lines[index] = updated
                changed = True
            break

        if not matched:
            # The seller's master list always carries the internal id already
            # (a fresh list is made per day / week).  A missing one is a real
            # mismatch — report it instead of adding a stray "12329" line at
            # the bottom of the list.
            if not self._accepts_new_rows(key, lines):
                return ""
            lines.append(key + "\t" + "\t".join(values))
            changed = True

        if changed:
            self._rewrite(lines)
            self._seen_cache = None
        return "\n".join(values)

    def _fill_plain_value(self, key: str, value: str) -> str:
        """Write a plain (non-order-id) value onto a still-blank master line.

        Used for the name/address fallback and for the "several orders, one
        customer" rule.  A line that already carries something stronger - a
        real order id or a ``TRUNC:`` crop - keeps it: text never overwrites a
        number.
        """
        if not value:
            return ""

        lines = self._read_raw_lines()
        changed = False
        matched = False
        for index, line in enumerate(lines):
            match = _MASTER_LINE_RE.match(line)
            if not match or not _same_id(match.group(2), key):
                continue
            matched = True
            prefix = f"{match.group(1)}{match.group(2)}"
            sep, existing = self._split_values(match.group(3))
            # Anything stronger than a name - a real id or a TRUNC: crop -
            # owns the line; plain text only fills a blank one.
            if any(
                _is_order_number(existing_value)
                or existing_value.upper().startswith("TRUNC:")
                for existing_value in existing
            ):
                return ""
            if value in existing:
                return "\n".join(existing)
            updated = prefix + sep + value
            if updated != line:
                lines[index] = updated
                changed = True
            break

        if not matched:
            if not self._accepts_new_rows(key, lines):
                return ""
            lines.append(key + "\t" + value)
            changed = True

        if changed:
            self._rewrite(lines)
            self._seen_cache = None
        return value

    def _accepts_new_rows(self, key: str, lines: List[str]) -> bool:
        """True only when the master list is brand new / still empty.

        The seller keeps one master list per day (or week) and it always
        carries the internal ids already, so a chat whose id has no line is a
        real mismatch — adding ``12329<TAB>...`` at the bottom would create the
        stray "separate number" lines the seller does not want.  A missing or
        empty file, by contrast, is the first run of a new list: rows are
        added then, otherwise that list would stay empty forever.
        """
        if any(_MASTER_LINE_RE.match(line) for line in lines):
            logger.warning(
                "event=notepad_line_missing internal_id=%s path=%s",
                key,
                self.path,
            )
            return False
        return True

    def _seen_lines(self) -> set:
        """Lazily load the set of lines already on disk (for idempotent writes)."""
        if self._seen_cache is None:
            self._seen_cache = set(self.read_lines())
        return self._seen_cache

    def append_line(self, line: str) -> None:
        """Append a raw line, using ``.tmp`` + atomic rename for safety.

        We open in append mode and flush + ``os.fsync`` so the bytes are on
        disk before we return.  If the target file is locked we fall back to
        writing a ``.tmp`` copy and renaming over the original.
        """
        try:
            with open(self.path, "a", encoding=self.encoding, newline="\n") as fh:
                fh.write(line.rstrip("\n") + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:  # pragma: no cover - fsync not always supported
                    pass
            return
        except PermissionError:
            logger.warning("Output file locked, writing via .tmp: %s", self.path)

        self._atomic_append(line)

    def _atomic_append(self, line: str) -> None:
        """Fallback: copy existing content + new line into a ``.tmp`` file."""
        tmp_path = f"{self.path}.tmp"
        existing = ""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding=self.encoding) as fh:
                    existing = fh.read()
            except OSError:
                existing = ""
        with open(tmp_path, "w", encoding=self.encoding, newline="\n") as fh:
            fh.write(existing)
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(line.rstrip("\n") + "\n")
        os.replace(tmp_path, self.path)

    def read_lines(self) -> List[str]:
        """Return every line currently in the file (used by tests / UI)."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding=self.encoding) as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]

    # ------------------------------------------------------------------
    # Manual corrections (TRUNC / FAILED review workflow)
    # ------------------------------------------------------------------
    def apply_corrections(
        self,
        corrections: Mapping[int, str],
        matches: Optional[Mapping[int, str]] = None,
    ) -> int:
        """Replace the value of rows whose internal id is in ``corrections``.

        Used by the GUI review panel: after a run the seller pastes the real
        Order ID for every ``TRUNC:`` / ``FAILED`` row.  Only the value part
        after the first comma is touched — ids and unrelated lines are left
        exactly as they were.

        ``matches`` is optional: it maps an internal id to the **current**
        value of the row that should be corrected.  A chat can carry several
        orders (one row each), so without it a correction would overwrite
        every row of that chat.

        Returns the number of rows that actually changed.
        """
        normalized: Dict[int, str] = {}
        for key, value in (corrections or {}).items():
            text = str(value).strip()
            if not text:
                continue
            try:
                normalized[int(key)] = text
            except (TypeError, ValueError):
                continue
        wanted: Dict[int, str] = {}
        for key, value in (matches or {}).items():
            try:
                wanted[int(key)] = str(value).strip()
            except (TypeError, ValueError):
                continue
        if not normalized or not os.path.exists(self.path):
            return 0

        if self.mode == "fill":
            return self._apply_fill_corrections(normalized, wanted)

        with open(self.path, "r", encoding=self.encoding) as fh:
            lines = [ln.rstrip("\n") for ln in fh]

        changed = 0
        out_lines: List[str] = []
        for line in lines:
            if not line.strip():
                continue
            head, sep, rest = line.partition(",")
            if sep:
                try:
                    key = int(head.strip())
                except ValueError:
                    key = None
                if key is not None and key in normalized:
                    if key in wanted and wanted[key] != rest.strip():
                        # Another row of the same chat - leave it alone.
                        out_lines.append(line)
                        continue
                    out_lines.append(f"{head.strip()},{normalized[key]}")
                    changed += 1
                    continue
            out_lines.append(line)

        if changed:
            self._rewrite(out_lines)
        return changed

    def _apply_fill_corrections(
        self, normalized: Dict[int, str], wanted: Dict[int, str]
    ) -> int:
        """Rewrite a corrected value inside the master-list layout."""
        lines = self._read_raw_lines()
        changed = 0
        for index, line in enumerate(lines):
            match = _MASTER_LINE_RE.match(line)
            if not match:
                continue
            try:
                key = int(match.group(2))
            except ValueError:
                continue
            if key not in normalized:
                continue
            prefix = f"{match.group(1)}{match.group(2)}"
            sep, values = self._split_values(match.group(3))
            current = wanted.get(key)
            if current and current not in values:
                # This is another order of the same chat; leave it alone.
                continue
            new_value = normalized[key]
            if current:
                values = [new_value if value == current else value for value in values]
            else:
                values = [new_value]
            updated = prefix + (sep if values else "") + sep.join(values)
            if updated != line:
                lines[index] = updated
                changed += 1
        if changed:
            self._rewrite(lines)
        return changed

    def _rewrite(self, lines: List[str]) -> None:
        """Atomically rewrite the whole file with ``lines``."""
        self._seen_cache = None  # file content changed; reload on next write
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding=self.encoding, newline="\n") as fh:
            for line in lines:
                fh.write(line.rstrip("\n") + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover
                pass
        try:
            os.replace(tmp_path, self.path)
        except PermissionError:  # pragma: no cover - target locked
            # Fall back to an in-place rewrite (best effort).
            with open(self.path, "w", encoding=self.encoding, newline="\n") as fh:
                for line in lines:
                    fh.write(line.rstrip("\n") + "\n")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
