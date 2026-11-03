"""
extractor/parser.py
===================

Field extraction logic.

The extraction pipeline (task spec §3) is:

1. **§3.1 labelled order id** — an ``Order ID`` / ``Order Id`` / ``Order #`` /
   ``Order No`` / ``Order Number`` label (or a Hindi / Tamil / Telugu / Bangla
   equivalent) immediately followed by a digit run of **6+ digits**.  A short
   ``extraction.order_id_truncate_length``-digit window (6 by default) is
   emitted — from the **front** or the **back** of the id depending on
   ``extraction.order_id_truncate_from``; that is the seller's manual Notepad
   workflow.
2. **§3.2 unlabelled Meesho id** — only when §3.1 yields nothing: a contiguous
   ``\\d{16,18}`` run in the **top third** of the text, after every §3.4
   rejection filter.  0 survivors fall through to the fallback chain; 1 survivor
   is emitted; 2–3 survivors emit the top-most candidate and flag the chat as
   ambiguous; more than
   :attr:`~extractor.rules.Rules.list_page_max_candidates` survivors is treated
   as a *list page* and emits ``TRUNC:<last-6>``.
3. **§3.3 fallback chain** — customer name -> address -> ``TRUNC:<fragment>`` ->
   ``FAILED``.  Unicode name/address values are preserved verbatim.

The same logic runs over the OCR text and the WhatsApp chat text (chat text is
never OCR'd).  A full Meesho id is only *truncated* when it is immediately
followed by an ellipsis; a sub-six-digit number is never accepted as a complete
id.  A 6-digit number is only a postal PIN when it sits in a postal context, and
adjacent digit groups are never concatenated into one id.

The public entry-point is :func:`extract_fields` (single value per chat) and
:func:`extract_fields_multi`, which switches to the older one-row-per-id
behaviour when ``extraction.multi_order_output`` is true.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

try:  # Allow both `from extractor.rules import ...` and direct module import.
    from .rules import (
        INTERVENING_LABEL_RE,
        MAX_LABEL_VALUE_NEWLINES,
        Rules,
        normalize,
    )
except ImportError:  # pragma: no cover
    from extractor.rules import (  # type: ignore
        INTERVENING_LABEL_RE,
        MAX_LABEL_VALUE_NEWLINES,
        Rules,
        normalize,
    )

# ``(value, confidence)`` — ``value`` is ``None`` when nothing was found.
Finding = Tuple[Optional[str], float]


# ---------------------------------------------------------------------------
# Order ID
# ---------------------------------------------------------------------------
# An ellipsis marker means the screenshot cut the id off ("34187320244...").
_ELLIPSIS_RE = re.compile(r"\.{2,}|\u2026")

# A hash-prefixed id whose label OCR mangled ("໙໕ #342572222316923008") or
# shortened ("ID: #342562154670311552").  Only trusted on an order screen.
_HASH_ORDER_RE = re.compile(r"#\s*(\d{15,18})(?!\d)")
_ORDER_SCREEN_RE = re.compile(
    r"(?i)(?:order\s*details?|thank you for shopping with us|delivery address|"
    r"cancel order|out for delivery|sold by|track order|"
    r"ऑर्डर|ஆர்டர்|ఆర్డర్|অর্ডার)"
)


def _trunc_range(rules: Rules) -> Tuple[int, int]:
    """Return the ``(min, max)`` digit counts considered 'truncated'."""
    return (
        int(getattr(rules, "truncated_min_digits", 1)),
        int(getattr(rules, "truncated_max_digits", 20)),
    )


def _followed_by_ellipsis(text: str, end: int) -> bool:
    """True when the digit run ending at ``end`` is cut off with ``...``/``…``.

    ``Order #341972763328...`` is an *incomplete* id, so the number can never
    be treated as a full order id even though it has plenty of digits.
    """
    return bool(re.match(r"\s*(?:\.{2,}|\u2026)", text[end:]))


def _id_is_rejected(value: str, text: str, start: int, end: int, rules: Rules) -> bool:
    """Shared rejection test: phones, PIN codes, prices, dates, product numbers.

    Delegates to :meth:`extractor.rules.Rules.is_non_order_number`.  A number
    explicitly labelled ``Order ID`` / ``Order #`` (or an equivalent regional
    label) is trusted over the postal / date / product heuristics, so a bare
    ``482910`` or ``Order #482910`` stays a valid order id.
    """
    return rules.is_non_order_number(text, start, end, value=value)


def _primary_base(match: "re.Match[str]") -> Tuple[str, int, int]:
    """Return ``(base_id, start, end)`` for a primary-regex match.

    The default :data:`~extractor.rules.ORDER_ID_PATTERN` captures the base
    order id in group 1 and keeps an optional Meesho ``_N`` sub-order suffix
    *outside* the group, so the suffix is never part of the returned id.  A
    user-supplied pattern without capture groups falls back to the whole match.
    """
    if match.re.groups >= 1 and match.group(1) is not None:
        return match.group(1), match.start(1), match.end(1)
    return match.group(), match.start(), match.end()


def _has_suborder_suffix(match: "re.Match[str]") -> bool:
    """True when the primary match carried a ``_N`` sub-order suffix."""
    if match.re.groups >= 1 and match.group(1) is not None:
        return len(match.group()) > len(match.group(1))
    return False


def _trailing_digits(prefix: str) -> Optional[Tuple[str, Tuple[int, int]]]:
    """Return ``(digits, (start, end))`` for the digit run touching the end."""
    match = re.search(r"(\d+)\s*$", prefix)
    if not match:
        return None
    return match.group(1), (match.start(1), match.end(1))


def find_ellipsis_truncated(text: str, rules: Rules) -> Finding:
    """Detect ``Order #34187320244...`` style ids -> ``"TRUNC:34187320244"``.

    A digit run of the configured truncated length sitting directly before an
    ellipsis (``...`` or ``…``) is captured and returned with the ``TRUNC:``
    prefix at confidence ``0.70``.  The id is *always* incomplete in this case
    — the trailing ``…`` is exactly what the seller needs to spot and fix.
    """
    min_d, max_d = _trunc_range(rules)
    for match in _ELLIPSIS_RE.finditer(text):
        prefix = text[: match.start()]
        tail = _trailing_digits(prefix)
        if not tail:
            continue
        value, (start, end) = tail
        if not (min_d <= len(value) <= max_d):
            continue
        if _id_is_rejected(value, text, start, end, rules):
            continue
        return f"TRUNC:{value}", 0.70
    return None, 0.0


def find_label_truncated(text: str, rules: Rules) -> Finding:
    """Detect a *cut-off* id sitting next to an order label.

    Only runs **shorter than a full order id** (fewer than six digits by
    default) can be truncated here: any 6–20 digit run without a trailing
    ellipsis is a complete id and is returned by :func:`find_order_id`'s
    generic pass instead.  The digit run must sit next to one of the
    configured order labels (``Order #12345`` -> ``TRUNC:12345``).
    """
    min_d, _ = _trunc_range(rules)
    # 6+ digits is a full id; cap the label pass just below that threshold.
    max_d = min(
        int(getattr(rules, "truncated_max_digits", 20)),
        int(getattr(rules, "min_full_order_id_digits", 6)) - 1,
    )
    if min_d > max_d:
        return None, 0.0

    partial_re = re.compile(rf"\b(\d{{{min_d},{max_d}}})\b")
    for match in partial_re.finditer(text):
        value = match.group(1)
        if _id_is_rejected(value, text, match.start(), match.end(), rules):
            continue
        # A trailing ellipsis is handled by :func:`find_ellipsis_truncated`.
        if _followed_by_ellipsis(text, match.end()):
            continue
        if rules.near_label(text, match.start(), match.end(), rules.order_id_labels):
            return f"TRUNC:{value}", 0.65
    return None, 0.0


def find_truncated_order_id(text: str, rules: Rules) -> Finding:
    """Try the ellipsis pass first, then the order-label pass."""
    found = find_ellipsis_truncated(text, rules)
    if found[0]:
        return found
    return find_label_truncated(text, rules)


def _truncated_digits(value: str) -> str:
    """The digits of a ``TRUNC:<digits>`` value (without the prefix)."""
    text = str(value or "").strip()
    if text.upper().startswith("TRUNC:"):
        return text.split(":", 1)[1].strip()
    return text


def _is_substantial_truncated(value: str, rules: Rules) -> bool:
    """True when a truncated order id carries enough digits to be a real id.

    A Meesho order id is 15–18 digits long and the customers crop the *tail*
    of the screen, so a visible fragment of nine digits or more is
    unmistakably a cropped order id — the seller only has to complete it.  A
    short fragment (five digits, say) could be anything, so it stays a
    fallback.

    ``extraction.truncated_id_min_digits`` set to ``0`` switches the
    preference off entirely (every truncated id stays behind the address).
    """
    minimum = int(getattr(rules, "truncated_id_min_digits", 9) or 0)
    if minimum <= 0:
        return False
    return len(_truncated_digits(value)) >= minimum


def _truncated_result(
    sources: Tuple[Tuple[str, str], ...], rules: Rules
) -> Optional[Dict[str, Any]]:
    """The ``TRUNC:`` fallback for the given sources (``None`` when absent).

    With ``extraction.cropped_id_is_complete`` the cut-off id is not a review
    item: the seller crops the **back** of the Meesho screen, so the leading
    digits of the id survive every crop and the visible fragment already *is*
    the short key they work with.  The result is then the plain windowed id
    (``"342578"``) instead of ``"TRUNC:342578720599009"``.
    """
    for source, blob in sources:
        if not blob:
            continue
        value, conf = find_truncated_order_id(blob, rules)
        if not value:
            continue
        digits = _truncated_digits(value)
        if bool(getattr(rules, "cropped_id_is_complete", False)):
            key = truncate_order_id(digits, rules)
            if not key:
                continue
            result = _empty_result()
            result.update(
                order_id=key,
                order_ids=[key],
                type="OK",
                confidence=conf,
                source=source,
                partial=False,
                truncated_digits=digits,
            )
            return result
        result = _empty_result()
        # A ``TRUNC:`` fallback is always a *cropped* id (an ellipsis or a
        # sub-six-digit run next to a label), so flag it ``partial`` — the
        # orchestrator then keeps the screenshot in ``review/``.
        result.update(
            order_id=value,
            type="TRUNC",
            confidence=conf,
            source=source,
            partial=True,
            truncated_digits=digits,
        )
        return result
    return None


def find_order_id(text: str, rules: Rules) -> Finding:
    """Find the most likely order id.

    Priority order
    --------------
    1. A 16-digit id, or a 15–18 digit id carrying a Meesho ``_N`` sub-order
       suffix (``342343834138346752_1``).  The suffix is stripped and only the
       base id is returned                                -> 0.95
    2. A **full** generic id (6–20 digits, any leading digit) near an order
       label -> 0.90, else the first full generic number anywhere -> 0.80
    3. A 1–20 digit run followed by an ellipsis (``TRUNC:…``)   -> 0.70
    4. A run of fewer than six digits next to an "Order …" label
       (``Order #12345``) (``TRUNC:…``)                        -> 0.65

    The generic pass deliberately runs *before* truncation so a genuine 6+
    digit id is never downgraded to ``TRUNC:``; it skips anything ending in
    ``…``, which the ellipsis pass below then picks up.  Order ids may start
    with **any** digit — a 12-digit run beginning with ``5`` is a complete id,
    not a crop.

    Phone numbers, PIN codes, prices, dates and product numbers (SKU / size /
    quantity / tracking …) are rejected at every step.  A 6-digit run is only a
    PIN when it sits in a postal context (see
    :meth:`extractor.rules.Rules.is_pin_context`), and an unlabelled date or
    product number is never accepted just because it is long.  Adjacent digit
    groups are never concatenated into one id.
    """
    text = normalize(text)
    if not text:
        return None, 0.0

    # 1) Primary: classic 16-digit ids and Meesho sub-order ids
    #    (15–18 digits, optionally suffixed ``_1`` / ``_2`` …).  Only Group 1
    #    — the base id — is ever returned, so duplicated sub-orders such as
    #    ``…_1`` and ``…_2`` collapse to a single base id.
    for match in rules._re_primary.finditer(text):
        base, start, end = _primary_base(match)
        if not base or _id_is_rejected(base, text, start, end, rules):
            continue

        grouped = match.re.groups >= 1 and match.group(1) is not None
        if not grouped:
            # Custom / legacy pattern without a capture group: keep the
            # original "first valid primary match wins" behaviour.
            return base, 0.95

        # A sub-order suffix or a classic 16-digit id is a confident hit;
        # other lengths fall through to the generic pass so a bare 15/17/18
        # digit number keeps its original 0.90 / 0.80 confidence.
        if len(base) == 16 or _has_suborder_suffix(match):
            return base, 0.95

    # 2) Generic FULL ids (6–20 digits, any leading digit, no trailing ``…``).
    fallback: Optional[str] = None
    for match in rules._re_generic.finditer(text):
        value = match.group()
        if not rules.is_valid_order_id_candidate(text, match):
            continue
        if _followed_by_ellipsis(text, match.end()):
            continue
        if not rules.is_full_order_id(value):
            continue
        if rules.near_label(text, match.start(), match.end(), rules.order_id_labels):
            return value, 0.90
        if fallback is None:
            fallback = value

    if fallback:
        return fallback, 0.80

    # 3) Truncated id followed by an ellipsis.
    found = find_ellipsis_truncated(text, rules)
    if found[0]:
        return found

    # 4) A sub-six-digit run sitting next to an order label.
    found = find_label_truncated(text, rules)
    if found[0]:
        return found

    return None, 0.0


def find_all_order_ids(text: str, rules: Rules) -> List[str]:
    """Return **every** unique complete order id in ``text`` (first-seen order).

    Where :func:`find_order_id` stops at the single best candidate, this walks
    the whole buffer so one screenshot — or one chat with several order
    screenshots — can yield more than one id.  Only genuine, *complete* ids are
    returned: duplicates collapse to their first occurrence in the text and
    truncated ids (``TRUNC:``) / name / address fallbacks are never included.

    The **label is the anchor**: when the text carries an order label, only
    the values next to such a label are returned.  A screenshot is full of
    numbers that are not order ids (the product code in the thumbnail, the
    PIN code of the delivery block, a price) and a bare six-digit number
    cannot be told apart from one — so an unlabelled number is only used when
    the text carries no order label at all.

    The same rejection rules as :func:`find_order_id` apply (phone numbers,
    context-aware PIN codes, prices, dates and product numbers are skipped), so
    a chat's family photos, a delivery PIN or a product code never masquerade
    as an order.  Digit groups separated by whitespace are **not** joined —
    each contiguous run is judged on its own.
    """
    text = normalize(text)
    if not text:
        return []

    # ``(position, value)`` pairs so the final list follows the document order
    # even though the primary and generic passes are separate scans.
    spans: List[Tuple[int, str]] = []

    # Labelled ids first — the label is the anchor.
    for candidate in _find_labelled_candidates((("image", text),), rules):
        spans.append((int(candidate.get("pos") or 0), candidate["value"]))

    # Only when nothing was labelled: the unlabelled scan.  It is restricted
    # to the **Meesho id band** (15–18 digits, the primary pattern) — a bare
    # 6-digit number could be a PIN code, a product code or a quantity, and
    # those must never be written out as an order id.
    if not spans:
        for match in rules._re_primary.finditer(text):
            base, start, end = _primary_base(match)
            if not base or _id_is_rejected(base, text, start, end, rules):
                continue
            if _followed_by_ellipsis(text, end):
                continue
            spans.append((start, base))

    spans.sort(key=lambda item: item[0])
    found: List[str] = []
    seen: set = set()
    for _pos, value in spans:
        if value and value not in seen:
            seen.add(value)
            found.append(value)
    return found


# ---------------------------------------------------------------------------
# §3 pipeline — labelled / unlabelled candidates + last-N truncation
# ---------------------------------------------------------------------------
#: A candidate is a small dict: ``{"value", "conf", "source"}``.
Candidate = Dict[str, Any]

#: Text shorter than this carries no meaningful "top third" / footer split, so
#: the whole short message is scanned (a 3-line chat message must not lose its
#: id to a top-third cut).
_TOP_THIRD_MIN_CHARS = 240

#: A labelled value of at most this many digits is "short" (6–9): it needs its
#: label on the same line, or a postal-signal-free next line, because a 6–9 digit
#: number is otherwise indistinguishable from a postal PIN (task rule C).
_SHORT_LABELLED_MAX_DIGITS = 9


def truncate_order_id(value: str, rules: Rules) -> str:
    """Return the configured window of ``value`` — its **first** or last N digits.

    This is the seller's manual workflow (paste a short stable key of the
    Meesho id next to the contact number).  Which end the window is taken from
    lives in config (``extraction.order_id_truncate_from``):

    * ``"first"`` — the seller crops the order id from the **back** side of the
      screen, so the leading digits survive every crop while the tail varies
      with how much of the line the customer's crop kept.  ``342216136358560832``
      yields ``342216``.
    * ``"last"`` — the original trailing window.  ``342216136358560832`` yields
      ``560832``.

    The length lives in config (``extraction.order_id_truncate_length``); a
    length of ``0`` disables the window and returns the id unchanged.
    """
    value = (value or "").strip()
    length = int(getattr(rules, "order_id_truncate_length", 6) or 0)
    if length > 0 and len(value) > length:
        direction = str(
            getattr(rules, "order_id_truncate_from", "last") or "last"
        ).strip().lower()
        if direction == "first":
            return value[:length]
        return value[-length:]
    return value


def _top_third(text: str) -> str:
    """Return the header slice of ``text`` where order ids actually sit.

    Footer numbers (product codes, delivery ETA, PIN) are the main source of
    false positives, so §3.2 only scans the top third of a long screenshot.
    Short messages (chat text) are scanned in full.
    """
    if len(text) <= _TOP_THIRD_MIN_CHARS:
        return text
    return text[: max(1, len(text) // 3)]


def _find_labelled_candidates(
    sources: Tuple[Tuple[str, str], ...], rules: Rules
) -> List[Candidate]:
    """§3.1 — collect ``label + digits`` hits (image text first, then chat).

    The **label is the anchor**: any contiguous run of 6–18 digits right after an
    order label is accepted, whatever its length — a 6-digit value is a
    legitimate *cropped* order id, not a PIN.  Four guards run before a value is
    accepted:

    * an *intervening-label* guard — when the filler between the label and the
      number (or any of the ≤2 newlines in it) names another field
      (``Order ID # PIN: 517126``, ``Order ID #\nTotal: 150000``) the candidate
      is rejected, because the number belongs to that other field;
    * a *gap-depth* guard — more than
      :data:`extractor.rules.MAX_LABEL_VALUE_NEWLINES` newlines between label and
      value rejects the candidate (the number is too far from its label);
    * a *short-value* guard (rule C) — a **6–9 digit** value whose label is on a
      *different* line is rejected when the value's own line carries a postal
      signal (``Order ID #\nKerala - 673311``), when it sits more than one
      newline below the label (``Order ID #\n\n517126``) or when a mobile number
      follows it (``Order ID #\n517126\n8309847092``) — Google Lens routinely
      detaches the header from the delivery block, and the first short number
      under it is the PIN;
    * the usual phone / price rejection (PIN, date and product heuristics are
      deliberately **not** applied to a labelled value).
    """
    out: List[Candidate] = []
    min_digits = int(getattr(rules, "min_full_order_id_digits", 6))
    max_digits = int(getattr(rules, "max_labelled_id_digits", 18))
    seen: set = set()
    for source, blob in sources:
        if not blob:
            continue
        before_blob = len(out)
        for match in rules._re_order_label_value.finditer(blob):
            value = match.group("value")
            if not (min_digits <= len(value) <= max_digits):
                continue
            start, end = match.start("value"), match.end("value")
            # A cut-off id ("Order #123...") normally falls through to the
            # TRUNC fallback.  When ``cropped_id_is_complete`` is on, however,
            # the visible digits *are* the seller's key, so the label is enough
            # to trust them — otherwise a delivery block's name would win and
            # the order id would be lost on that screenshot entirely.
            if _followed_by_ellipsis(blob, end) and not bool(
                getattr(rules, "cropped_id_is_complete", False)
            ):
                continue
            filler = match.group("filler")
            # Cap the gap depth: a real id is never more than
            # MAX_LABEL_VALUE_NEWLINES below its label, so a deeper gap means the
            # number belongs to another field further down the screenshot.
            if filler.count("\n") > MAX_LABEL_VALUE_NEWLINES:
                continue
            # Intervening-label guard: scan the *filler only* (never the label
            # itself) so a PIN / phone / SKU / price / nested order label sitting
            # between the label and the number rejects the candidate.
            # Known false-negative: a repeated label (e.g. "Order ID #\nOrder ID:
            # 342…") puts the word "Order" in the filler and trips the guard.
            # Fix only if a real screenshot hits this.
            if INTERVENING_LABEL_RE.search(filler):
                continue
            # Rule C — short-value guard.  A 6–9 digit value is only trusted when
            # its label is on the same line, or the line below carries no postal
            # signal.  Google Lens often detaches the "Order ID #" header from
            # the delivery block, and the first number under it is the PIN
            # ("Order ID #\nKerala - 673311", "Order ID #\n\n517126"), which is
            # then followed by the contact number.
            if len(value) <= _SHORT_LABELLED_MAX_DIGITS:
                newlines = filler.count("\n")
                if newlines > 1:
                    continue
                if newlines == 1 and rules.line_has_address_context(blob, start, end):
                    continue
                if newlines >= 1 and rules.followed_by_mobile_number(blob, end):
                    continue
            if rules.is_rejected_candidate(blob, start, end, value, labelled=True):
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append({
                "value": value,
                "conf": 0.92,
                "source": source,
                "pos": start,
            })

        # §3.1b — a mangled label can still leave the "#" intact.  On an
        # order screen, accept a hash-prefixed Meesho-band number even when
        # ``Order`` itself was OCR'd as garbage ("໙໕ #342…") or shortened
        # ("ID: #342…").  The 15–18 digit band and the order-screen marker keep
        # product codes, PINs and phone numbers out.
        if len(out) == before_blob and _ORDER_SCREEN_RE.search(blob):
            for match in _HASH_ORDER_RE.finditer(blob):
                value = match.group(1)
                if not (min_digits <= len(value) <= max_digits):
                    continue
                start, end = match.start(1), match.end(1)
                if _followed_by_ellipsis(blob, end) and not bool(
                    getattr(rules, "cropped_id_is_complete", False)
                ):
                    continue
                if rules.is_rejected_candidate(
                    blob, start, end, value, labelled=True
                ):
                    continue
                if value in seen:
                    continue
                seen.add(value)
                out.append({
                    "value": value,
                    "conf": 0.87,
                    "source": source,
                    "pos": start,
                })
    return out


def _find_unlabelled_candidates(
    sources: Tuple[Tuple[str, str], ...], rules: Rules
) -> List[Candidate]:
    """§3.2 — contiguous ``\\d{16,18}`` runs in the top third, §3.4 filtered."""
    low = int(getattr(rules, "min_unlabelled_id_digits", 16))
    high = int(getattr(rules, "max_unlabelled_id_digits", 18))
    if high < low:
        low, high = high, low
    pattern = re.compile(rf"(?<!\d)\d{{{low},{high}}}(?!\d)")
    out: List[Candidate] = []
    seen: set = set()
    for source, blob in sources:
        if not blob:
            continue
        header = _top_third(blob)
        for match in pattern.finditer(header):
            value = match.group()
            if rules.is_rejected_candidate(
                header, match.start(), match.end(), value
            ):
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append({"value": value, "conf": 0.85, "source": source})
    return out


def _apply_concatenation_guard(
    candidates: List[Candidate], rules: Rules
) -> List[Candidate]:
    """§3.4 — drop a candidate that *contains* another as a contiguous run.

    ``3418579486740656517`` is ``3418579486740656`` with an extra ``517`` glued
    on; keeping both would emit a bogus super-id.  The longer one is dropped.
    """
    if not (rules.reject_patterns or {}).get("concatenation", True):
        return candidates
    values = [c["value"] for c in candidates]
    kept: List[Candidate] = []
    for candidate in candidates:
        value = candidate["value"]
        if any(other != value and other in value for other in values):
            continue
        kept.append(candidate)
    return kept


# ---------------------------------------------------------------------------
# Customer Name
# ---------------------------------------------------------------------------
def _clean_name(value: str) -> str:
    """Strip separators / collapse whitespace on a candidate name."""
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    # Meesho prints "punitha • 7418622538" / "Name | Mobile" — the separator
    # between the two values is not part of the name.
    return value.strip(" :-\u2013\u2014\t\u2022\u00b7|,").strip()


# NOTE: name validation can no longer use a single regex — Python's ``\w``
# class excludes Indic combining marks, so ``^[^\W\d_ …]$`` rejected Hindi/
# Tamil/Telugu/Bangla names.  See :func:`_valid_name_text` below.


#: Script blocks a customer name may be written in.  The seller's customers
#: write Latin or one of the Indian scripts below.  A name built from any
#: *other* script is OCR noise — a Telugu phone label once came back as the
#: Lao "ລ້ລ້ ລ້", which was accepted as a customer name.
_NAME_SCRIPT_RANGES = (
    (0x0041, 0x024F),   # Latin
    (0x1E00, 0x1EFF),   # Latin Extended Additional
    (0x0600, 0x06FF),   # Arabic (Urdu names)
    (0x0900, 0x097F),   # Devanagari
    (0x0980, 0x09FF),   # Bengali
    (0x0A00, 0x0A7F),   # Gurmukhi
    (0x0A80, 0x0AFF),   # Gujarati
    (0x0B00, 0x0B7F),   # Odia
    (0x0B80, 0x0BFF),   # Tamil
    (0x0C00, 0x0C7F),   # Telugu
    (0x0C80, 0x0CFF),   # Kannada
    (0x0D00, 0x0D7F),   # Malayalam
)


def _is_supported_name_script(ch: str) -> bool:
    """True when ``ch`` belongs to a script a customer name is written in."""
    code = ord(ch)
    for low, high in _NAME_SCRIPT_RANGES:
        if low <= code <= high:
            return True
    return False


#: Month names / abbreviations.  A screenshot wraps its delivery date
#: ("Sunday, 25th Oct"), and the wrapped fragment used to be accepted as a
#: one-word customer name.
_MONTH_WORDS = frozenset({
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
})


def _valid_name_text(name: str) -> bool:
    """True when ``name`` is letters (any script) + spaces/``.'-`` only.

    ``str.isalpha`` / ``\\w`` treat Indic *combining marks* (Mn/Mc) as
    non-letters, so a Unicode-category check is used instead: the first
    character must be a letter and the rest letters or marks.  This keeps Hindi
    / Tamil / Telugu / Bangla names valid without ASCII-folding them.
    """
    if not name:
        return False
    if unicodedata.category(name[0])[0] != "L":
        return False
    for ch in name:
        if ch in " .'-":
            continue
        if unicodedata.category(ch)[0] in ("L", "M"):
            if _is_supported_name_script(ch):
                continue
            return False
        return False
    return True


def is_valid_name(name: str, rules: Rules) -> bool:
    """Validate a name candidate against the spec's rules."""
    name = _clean_name(name)
    if not name or len(name) < 2 or len(name) > 60:
        return False
    if any(ch.isdigit() for ch in name):
        return False

    words = name.split()
    if not (1 <= len(words) <= 4):
        return False
    if not _valid_name_text(name):
        return False

    lowered_words = [w.lower().strip(".:'-") for w in words]
    ignore = {w.lower() for w in rules.ignore_words}
    if any(w in ignore for w in lowered_words):
        return False

    # A wrapped delivery date is not a customer ("Sunday, 25th Oct" -> "Oct").
    if len(lowered_words) == 1 and lowered_words[0] in _MONTH_WORDS:
        return False

    # Reject if the *whole* value is one of the boilerplate words.
    if name.lower() in ignore:
        return False

    return True


def _inline_address_label_name(line: str, rules: Rules) -> Optional[str]:
    """Return the customer name printed *after* an address label on one line.

    Meesho prints the delivery header and the customer together in several
    languages — ``डिलीवरी का पता Shyamu singh Tomar``, ``డెలివరీ అడ్రసు Zadda
    Seeyon`` — so the label line itself carries the name.  Only the longest
    matching label is stripped and the remainder must pass
    :func:`is_valid_name`: an address fragment (or a UI word such as
    ``CHANGE``) can never become a customer.
    """
    stripped = (line or "").strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for label in sorted(rules.address_labels, key=len, reverse=True):
        low = label.strip().lower()
        if not low or not lowered.startswith(low):
            continue
        remainder = _clean_name(stripped[len(label):])
        if remainder and is_valid_name(remainder, rules):
            return remainder
        return None
    return None


def _find_labelled_name(text: str, rules: Rules) -> Finding:
    """The **labelled** branch of :func:`find_name`.

    A name that sits behind one of the configured name labels (``Customer
    Name``, ``Delivery Address``, ``Sold to`` …).  This branch is strict —
    the candidate must pass :func:`is_valid_name` — so it is safe to use on
    its own when only a reliable name is wanted.
    """
    text = normalize(text)
    if not text:
        return None, 0.0
    lines = text.split("\n")
    for label in rules.name_labels:
        pattern = rules.name_label_pattern(label)
        for idx, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue

            # "Supplier : KK ROYAL" is the seller's own supplier, never the
            # customer — never let a name label inside it match.
            if re.search(r"(?i)supplier\s*:?\s*$", line[: match.start()]):
                continue

            inline = _clean_name(match.group(1))
            if is_valid_name(inline, rules):
                return inline, 0.82

            # Look at the next couple of non-empty lines.
            for j in range(idx + 1, min(idx + 3, len(lines))):
                candidate = _clean_name(lines[j])
                if not candidate:
                    continue
                if is_valid_name(candidate, rules):
                    return candidate, 0.80
                break  # first non-empty line wasn't a name -> stop
    return None, 0.0


def find_name(text: str, rules: Rules) -> Finding:
    """Find a customer name using the configured name labels."""
    text = normalize(text)
    if not text:
        return None, 0.0

    name, conf = _find_labelled_name(text, rules)
    if name:
        return name, conf

    # No name label anywhere - a delivery block still has its shape (a name
    # line with the mobile number glued to it).
    name, _address = find_delivery_block(text, rules)
    if name:
        return name, 0.74
    return find_name_without_label(text, rules)


def find_customer_details(
    text: str, rules: Rules
) -> Tuple[Optional[str], Optional[str]]:
    """The customer's name and address — the **reliable** ones only.

    Used when the run collects *everything* about an order (the
    ``extraction.collect: all`` mode).  Unlike the fallback chain, this
    never falls back to the loose "a line next to a mobile number" guess —
    that rule happily returns "Oct" from a delivery date — so the details
    that end up in the row are the ones a label or the delivery-block shape
    vouches for.

    Returns ``(name, address)``; either may be ``None``.
    """
    text = normalize(text)
    if not text:
        return None, None

    name, _conf = _find_labelled_name(text, rules)
    if not name:
        block_name, block_address = find_delivery_block(text, rules)
        name = block_name

    address, _conf = find_address(text, rules)
    if not address:
        _block_name, block_address = find_delivery_block(text, rules)
        address = block_address
    return name, address


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------
# Lines that are clearly *not* address content (prices / boilerplate).
_ADDRESS_REJECT_RE = re.compile(
    r"(?i)(total|subtotal|grand total|price|amount|discount|tax|invoice|"
    r"cash on delivery|payment method|thank you|order id|order no|"
    r"premium bestsellers|continue shopping|track order|buy \d|"
    r"add to cart|favour|favourite|share with|estimated delivery|"
    r"all issue easy returns|got a great deal)"
)
_CURRENCY_RE = re.compile(r"(₹|\brs\.?\b|\binr\b|\$|\busd\b|€)")


def _looks_like_address_text(value: str, rules: Rules) -> bool:
    """Heuristic validation for a captured address candidate."""
    if not value:
        return False
    stripped = value.strip()
    if len(stripped) < 8:
        return False

    # A line that asks a question is app chrome ("Change your mobile
    # number?"), never an address.
    if "?" in stripped:
        return False

    # Reject obvious price / boilerplate lines.
    if _CURRENCY_RE.search(stripped) or _ADDRESS_REJECT_RE.search(stripped):
        return False

    # Reject a candidate made up only of the configured ignore words.
    words = [w.strip(".,;:").lower() for w in stripped.split()]
    words = [w for w in words if w]
    if not words:
        return False
    if all(w in {ig.lower() for ig in rules.ignore_words} for w in words):
        return False

    # A believable address has either a digit (house no. / PIN) or ≥ 2 words.
    has_digit = any(ch.isdigit() for ch in stripped)
    return has_digit or len(words) >= 2


def find_address(text: str, rules: Rules) -> Finding:
    """Find a shipping address block after an address label.

    The block is captured line-by-line until a 6-digit PIN code, a blank line
    or the ``address_max_chars`` cap is hit.  Newlines are preserved.

    When the screenshot starts *inside* the delivery block the
    "Delivery Address" label is cropped away.  The block still has its
    unmistakable shape — a 6-digit PIN line with a 10-digit mobile number
    right below it — so that shape is used as a second chance (see
    :func:`find_address_without_label`).
    """
    text = normalize(text)
    if not text:
        return None, 0.0

    lines = text.split("\n")
    for label in rules.address_labels:
        pattern = rules.address_label_pattern(label)
        for idx, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue

            collected: List[str] = []
            # "Delivery Address Shyamu singh Tomar" carries the customer on
            # the label line itself — that text is the name, never address.
            inline_name = _inline_address_label_name(line, rules)
            remainder = "" if inline_name else _clean_name(match.group(1))
            if remainder and remainder.lower() not in {w.lower() for w in rules.ignore_words}:
                # Only keep an inline remainder if it actually looks like an address.
                if _looks_like_address_text(remainder, rules):
                    collected.append(remainder)

            j = idx + 1
            while j < len(lines):
                if len("\n".join(collected)) >= rules.address_max_chars:
                    break
                candidate = lines[j].strip()
                if candidate == "":
                    break
                # Stop before pulling in price / boilerplate lines.
                if not _looks_like_address_text(candidate, rules):
                    if collected:
                        break
                    # Otherwise skip the noise line and keep scanning.
                    j += 1
                    continue
                collected.append(candidate)
                if rules._re_pin.search(candidate):
                    break
                j += 1

            address = "\n".join(collected).strip()
            if _looks_like_address_text(address, rules):
                return address, 0.72

    # No label anywhere - the block may still be recognisable by its shape.
    return find_address_without_label(text, rules)


#: Words that mark a line as a *phone label* rather than a customer name
#: ("Contact Number - 9786782006" is not a customer called "Contact Number").
_PHONE_LABEL_WORDS = re.compile(
    r"(?i)\b(contact|phone|mobile|call|tel|telephone|whatsapp)\b"
)

#: The very same labels in the languages the seller actually receives.  A
#: Meesho screenshot in Hindi carries "संपर्क नंबर 9786782006" — without this
#: the line-splitting rule below would read "संपर्क नंबर" as the customer's
#: name.  These scripts have no reliable ``\b`` word boundary, so they are
#: matched as plain substrings.
_PHONE_LABEL_WORDS_NON_LATIN = re.compile(
    r"(संपर्क|सम्पर्क|नंबर|नम्बर|नम्बरु|मोबाइल|फोन|फ़ोन|कॉल|दूरभाष|"
    r"தொடர்பு|எண்|அலைபேசி|"
    r"సంఖ్య|ఫోన్|నంబర్|నంబరు|సంప్రదించు|యోగాయోగ|"
    r"নম্বর|ফোন|যোগাযোগ)"
)


def _split_name_and_phone(line: str, rules: Rules) -> Tuple[str, str]:
    """Split ``"punitha 7418622538"`` into ``("punitha", "7418622538")``.

    Returns ``("", "")`` when the line is not a name-with-phone line.
    """
    match = re.search(r"(?<!\d)([6-9]\d{9})(?!\d)", line)
    if not match:
        return "", ""
    if _PHONE_LABEL_WORDS.search(line) or _PHONE_LABEL_WORDS_NON_LATIN.search(line):
        return "", match.group(1)
    name = _clean_name(line[: match.start()])
    if not is_valid_name(name, rules):
        return "", match.group(1)
    # "Customer: 9876543210" is a *label* line, not a customer called
    # "Customer" - the name labels never introduce a value themselves.
    lowered = name.lower()
    if any(lowered.startswith(label.lower()) for label in rules.name_labels):
        return "", match.group(1)
    return name, match.group(1)


#: Words that mark a line as a *label* rather than a customer name
#: ("Sold by Royal Photo Frame" is a supplier line, not a customer).
_LABEL_STOP_WORDS = {"by", "to", "from", "at", "on", "is", "was", "no"}

#: A customer name is short.  A four-word line ("All issue easy returns") is
#: a product tagline, not a name.
_LABEL_FREE_NAME_MAX_WORDS = 3

#: The Meesho product code sits right above the "BUY 1" / "GET 1" button and
#: is 10 digits long — phone-*shaped*, but not a phone number.
_PRODUCT_CODE_MARKERS = ("buy 1", "get 1", "add to cart")


def _looks_like_a_label_line(value: str, rules: Rules) -> bool:
    """True when a candidate name is really a label line."""
    if not value:
        return True
    if ":" in value:
        return True
    if _PHONE_LABEL_WORDS_NON_LATIN.search(value):
        # "संपर्क नंबर" / "தொடர்பு எண்" — a label in the seller's language.
        return True
    words = {w.strip(".,:;").lower() for w in value.split()}
    if words & _LABEL_STOP_WORDS:
        return True
    lowered = value.lower()
    return any(lowered.startswith(label.lower()) for label in rules.name_labels)


def _is_product_code_line(lines: List[str], index: int) -> bool:
    """True when the number on ``lines[index]`` is a Meesho product code."""
    for follow in lines[index + 1:index + 3]:
        lowered = follow.strip().lower()
        if not lowered:
            continue
        return any(marker in lowered for marker in _PRODUCT_CODE_MARKERS)
    return False


def find_name_without_label(text: str, rules: Rules) -> Finding:
    """Find a customer name on a line next to the mobile number.

    Meesho prints the customer's name and the contact number together, and
    the OCR may split them onto two lines::

        punitha 7418622538          <- same line
        Nagma Bano
        6239226952                  <- or the number on the next line

    A line that says "Contact Number - 9786782006" or "Customer: 9876543210"
    is a *label* line, never a customer — and the product code under the
    thumbnail ("7014965655", right above "BUY 1") is not a phone number
    either.

    The confidence is lower than the labelled variant (0.74 vs 0.82) because
    the shape is a hint rather than a label.
    """
    text = normalize(text)
    if not text:
        return None, 0.0
    lines = text.split("\n")
    for index, line in enumerate(lines):
        # shape 0: "<address label> <name>" on one line — Meesho does this in
        # the regional languages ("डिलीवरी का पता Shyamu singh Tomar").
        inline_label_name = _inline_address_label_name(line, rules)
        if (
            inline_label_name
            and len(inline_label_name.split()) <= _LABEL_FREE_NAME_MAX_WORDS
        ):
            return inline_label_name, 0.78
        # shape 1: "<name> <mobile>" on one line
        name, _phone = _split_name_and_phone(line, rules)
        if name and len(name.split()) <= _LABEL_FREE_NAME_MAX_WORDS:
            return name, 0.74
        # shape 2: "<name>" with the mobile number on the next line
        if index + 1 >= len(lines):
            continue
        if not rules._re_phone.search(lines[index + 1]):
            continue
        if _is_product_code_line(lines, index + 1):
            continue
        # The line below may itself carry the customer next to the number
        # ("punitha 7418622538").  That name is the real one - the line above
        # is often a wrapped delivery date ("Sunday, 25th Oct") whose last
        # word used to be accepted as a one-word name.
        inline_name, _ = _split_name_and_phone(lines[index + 1], rules)
        if inline_name and len(inline_name.split()) <= _LABEL_FREE_NAME_MAX_WORDS:
            return inline_name, 0.74
        candidate = _clean_name(line)
        if not candidate or ":" in line:
            continue
        if len(candidate.split()) > _LABEL_FREE_NAME_MAX_WORDS:
            continue
        if not is_valid_name(candidate, rules):
            continue
        if _looks_like_a_label_line(candidate, rules):
            continue
        return candidate, 0.70
    return None, 0.0


def find_delivery_block(
    text: str, rules: Rules
) -> Tuple[Optional[str], Optional[str]]:
    """Find the ``(name, address)`` of a delivery block whose labels were cut off.

    A Meesho delivery block always looks like this::

        Delivery Address
        <customer name>            (sometimes "<name> <mobile>")
        <address line>
        <city>, <state> - <PIN>
        Contact Number - <mobile>

    A customer who screenshots the *bottom* of the order page cuts the
    "Delivery Address" label off — and in a regional-language screenshot even
    the label is in that language.  The shape survives, though: a 6-digit PIN
    in a postal context, with the mobile number next to it.

    Returns ``(name, address)``; either may be ``None``.
    """
    text = normalize(text)
    if not text:
        return None, None

    lines = text.split("\n")
    offsets: List[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1

    for index, line in enumerate(lines):
        pin = rules._re_pin.search(line)
        if not pin:
            continue
        absolute = offsets[index] + pin.start()
        # The PIN must be postal: either the line carries a state / city /
        # address word, or the mobile number sits right below it.
        postal = rules.has_address_context(text, absolute, absolute + 6)
        if not postal and index + 1 < len(lines):
            postal = bool(rules._re_phone.search(lines[index + 1]))
        if not postal:
            continue

        collected: List[str] = []
        j = index
        while j >= 0 and len("\n".join(collected)) < rules.address_max_chars:
            candidate = lines[j].strip()
            if not candidate:
                break
            if not _looks_like_address_text(candidate, rules):
                break
            collected.insert(0, candidate)
            j -= 1

        # A lone PIN line is not a delivery block - the block needs the line
        # above it as well.
        if len(collected) < 2:
            continue
        # An *orphaned* order header ("Order ID #" followed by a PIN) is not a
        # delivery block: the PIN belongs to whatever the header asked for.
        if any(_has_order_label(line, rules) for line in collected):
            continue

        name = ""
        if len(collected) > 1:
            # The first line of the block may be "<name> <mobile>" - the
            # name is the part before the number.
            first, _phone = _split_name_and_phone(collected[0], rules)
            if first:
                name = first
                collected = collected[1:]
            else:
                # ... or "<address label> <name>" in one line (regional labels
                # included) — the name, not the address, follows the label.
                inline_name = _inline_address_label_name(collected[0], rules)
                if inline_name:
                    name = inline_name
                    collected = collected[1:]

        address = "\n".join(collected).strip()
        if not _looks_like_address_text(address, rules):
            address = ""
        if not name and not address:
            continue
        return name, (address or None)

    return None, None


def _has_order_label(line: str, rules: Rules) -> bool:
    """True when a line carries one of the order-id labels."""
    lowered = line.lower()
    return any(label.lower() in lowered for label in rules.order_id_labels)


def find_address_without_label(text: str, rules: Rules) -> Finding:
    """Find the address of a delivery block whose label was cropped away.

    A Meesho delivery block always looks like this::

        Delivery Address
        <customer name>
        <address line>
        <city>, <state> - <PIN>
        Contact Number - <mobile>

    A customer who screenshots the *bottom* of the order page cuts the
    "Delivery Address" label off, but the shape survives: a 6-digit PIN line
    with a 10-digit mobile number directly below it.  The address is then
    everything above the PIN line.

    The confidence is lower than the labelled variant (0.68 vs 0.72) because
    the shape is a hint rather than a label.
    """
    _name, address = find_delivery_block(text, rules)
    if not address:
        return None, 0.0
    return address, 0.68


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------
def _empty_result() -> Dict[str, Any]:
    return {
        "order_id": "",
        "order_ids": [],
        "name": "",
        "address": "",
        "type": "FAILED",
        "confidence": 0.0,
        "source": "",
        "ambiguous": False,
        "ambiguous_candidates": 0,
        "partial": False,
    }


def _single_id_result(
    candidate: Candidate,
    rules: Rules,
    *,
    ambiguous: bool = False,
    candidates: int = 1,
    trunc: bool = False,
    partial: bool = False,
) -> Dict[str, Any]:
    """Build the ``type="OK"``/``"TRUNC"`` result for one chosen candidate.

    ``partial`` marks a *cropped* labelled value (6–15 digits): by default it is
    emitted as ``TRUNC:<last-N>`` so the seller can spot it; with
    ``extraction.partial_id_prefix: false`` it is emitted bare but the result is
    still flagged ``partial`` (and therefore routed to ``review/``).
    """
    value = truncate_order_id(candidate["value"], rules)
    result = _empty_result()
    result["source"] = candidate.get("source", "")
    result["confidence"] = float(candidate.get("conf", 0.85) or 0.0)
    result["ambiguous"] = bool(ambiguous)
    result["ambiguous_candidates"] = int(candidates)
    result["partial"] = bool(partial)
    use_prefix = trunc or (partial and bool(getattr(rules, "partial_id_prefix", True)))
    if use_prefix:
        result["type"] = "TRUNC"
        result["order_id"] = f"TRUNC:{value}"
        result["order_ids"] = []
    else:
        result["type"] = "OK"
        result["order_id"] = value
        result["order_ids"] = [value]
    return result


def extract_fields(
    ocr_text: str,
    whatsapp_text: str,
    rules: Rules,
) -> Dict[str, Any]:
    """Run the §3 pipeline and return **one** value per chat.

    Resolution order (highest priority first):

    1. §3.1 a **labelled** order id           -> ``type="OK"`` (full id) or
       ``type="TRUNC"`` (a crop, unless ``cropped_id_is_complete`` is on)
    2. §3.2 an **unlabelled** Meesho id       -> only when
       ``extraction.require_order_id_label`` is **false**
    3. §3.3 a **cut-off id**                  -> a visible fragment of
       ``extraction.truncated_id_min_digits`` digits or more (6 by default)
       is still the order key
    4. §3.3 the customer name                 -> ``type="FALLBACK_NAME"``
    5. §3.3 the shipping address              -> ``type="FALLBACK_ADDRESS"``
    6. nothing                                -> ``type="FAILED"``

    With ``extraction.cropped_id_is_complete`` the order id stays first (a
    labelled or ellipsis-cut crop of six or more digits is written as a plain
    id), then the chain is exactly **name -> address -> FAILED**.  Without the
    flag the old review-friendly chain is kept: name -> substantial ``TRUNC``
    -> address -> short ``TRUNC`` -> ``FAILED``.

    Returns a dict with the keys: ``order_id``, ``order_ids``, ``name``,
    ``address``, ``type``, ``confidence``, ``source`` (``image`` or ``text``),
    ``ambiguous``, ``ambiguous_candidates`` and ``partial``.  A truncated or
    partial id is written back into ``order_id`` as the load-bearing
    ``"TRUNC:<digits>"`` string so the writers, CSV status column and run
    summary stay unchanged.
    """
    image_text = normalize(ocr_text)
    text_text = normalize(whatsapp_text)
    sources = (("image", image_text), ("text", text_text))

    # §3.1 — a labelled id wins outright; >1 distinct labels are ambiguous.
    labelled = _find_labelled_candidates(sources, rules)
    if labelled:
        best = labelled[0]
        full_min = int(getattr(rules, "full_id_min_digits", 16))
        # A cropped id ("Order #342581") is a *complete* id for the seller
        # when the leading digits are all they need - see
        # ``extraction.cropped_id_is_complete``.
        cropped_ok = bool(getattr(rules, "cropped_id_is_complete", False))
        partial = len(best["value"]) < full_min and not cropped_ok
        return _single_id_result(
            best,
            rules,
            ambiguous=len(labelled) > 1,
            candidates=len(labelled),
            partial=partial,
        )

    # §3.2 — unlabelled 16–18 digit Meesho ids in the top third.  Disabled
    # whenever an explicit order label is required (the seller's default).
    unlabelled: List[Candidate] = []
    if not bool(getattr(rules, "require_order_id_label", True)):
        unlabelled = _apply_concatenation_guard(
            _find_unlabelled_candidates(sources, rules), rules
        )
    if unlabelled:
        top = unlabelled[0]
        count = len(unlabelled)
        max_candidates = int(getattr(rules, "list_page_max_candidates", 3) or 3)
        if count > max_candidates:
            # §3.6 — more than the cap: a list page, not a single order.
            return _single_id_result(
                top, rules, ambiguous=True, candidates=count, trunc=True
            )
        # 1 survivor -> confident; 2–3 -> top-most, flagged ambiguous.
        return _single_id_result(top, rules, ambiguous=count > 1, candidates=count)

    # §3.3 — fallback chain.
    #
    # The seller's fields are: order id -> name -> address, nothing else.
    # When ``cropped_id_is_complete`` is on, a visible crop of at least
    # ``truncated_id_min_digits`` digits (6 by default) is still the order id
    # and therefore wins over the name.  A shorter fragment is not an id at all
    # — it falls through to the name / address and is never emitted as
    # ``TRUNC:``.
    #
    # Without the flag the old review-oriented chain is kept:
    #   name -> substantial TRUNC -> address -> short TRUNC -> FAILED
    result = _empty_result()
    truncated = _truncated_result(sources, rules)
    # The "substantial" test measures the *visible* digits of the crop, not
    # the short key it is written out as.
    substantial = bool(
        truncated
        and _is_substantial_truncated(
            str(truncated.get("truncated_digits") or truncated["order_id"]),
            rules,
        )
    )
    cropped_complete = bool(getattr(rules, "cropped_id_is_complete", False))

    if cropped_complete:
        # Order id first: a 6+ digit crop is the key the seller needs.
        if substantial and truncated:
            return truncated
        for source, blob in sources:
            if not blob:
                continue
            value, conf = find_name(blob, rules)
            if value:
                result.update(
                    name=value, type="FALLBACK_NAME", confidence=conf,
                    source=source,
                )
                return result
        for source, blob in sources:
            if not blob:
                continue
            value, conf = find_address(blob, rules)
            if value:
                result.update(
                    address=value, type="FALLBACK_ADDRESS", confidence=conf,
                    source=source,
                )
                return result
        return result

    for source, blob in sources:
        if not blob:
            continue
        value, conf = find_name(blob, rules)
        if value:
            result.update(name=value, type="FALLBACK_NAME", confidence=conf, source=source)
            return result

    if substantial and truncated:
        return truncated

    for source, blob in sources:
        if not blob:
            continue
        value, conf = find_address(blob, rules)
        if value:
            result.update(address=value, type="FALLBACK_ADDRESS", confidence=conf, source=source)
            return result

    if truncated:
        return truncated

    return result


def extract_fields_multi(
    ocr_text: str,
    whatsapp_text: str,
    rules: Rules,
) -> Dict[str, Any]:
    """Orchestrator entry point — single-value by default.

    With ``extraction.multi_order_output`` **false** (the default) this is
    exactly :func:`extract_fields`: one value per chat, per §3.1–§3.6.

    With the flag **true** it restores the earlier multi-order behaviour: every
    unique complete id found across the image OCR text and the chat text is
    emitted as its own row (first-seen order, image before text), each
    truncated to its last ``order_id_truncate_length`` digits.  When no complete
    id is found the call falls back to the single-result priority chain
    (name -> address -> ``TRUNC`` -> ``FAILED``) unchanged.
    """
    if not getattr(rules, "multi_order_output", False):
        return extract_fields(ocr_text, whatsapp_text, rules)

    image_text = normalize(ocr_text)
    text_text = normalize(whatsapp_text)

    order_ids: List[str] = []
    seen: set = set()
    confidence = 0.0
    source = ""
    for candidate_source, blob in (("image", image_text), ("text", text_text)):
        if not blob:
            continue
        ids = find_all_order_ids(blob, rules)
        if not ids:
            continue
        if not source:
            source = candidate_source
            _, confidence = find_order_id(blob, rules)
        for value in ids:
            short = truncate_order_id(value, rules)
            if short and short not in seen:
                seen.add(short)
                order_ids.append(short)

    if order_ids:
        result = _empty_result()
        result.update(
            order_id=order_ids[0],
            order_ids=order_ids,
            type="OK",
            confidence=confidence or 0.90,
            source=source or "image",
            ambiguous=False,
            ambiguous_candidates=len(order_ids),
        )
        return result

    return extract_fields(ocr_text, whatsapp_text, rules)


def combine_text(ocr_text: str, whatsapp_text: str) -> str:
    """Concatenate OCR + WhatsApp text into one normalised buffer."""
    parts = [normalize(ocr_text), normalize(whatsapp_text)]
    return "\n".join(p for p in parts if p)
