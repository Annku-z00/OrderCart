"""
extractor/rules.py
==================

Central home for the text-normalisation helpers, compiled regex patterns and
the label / ignore-word constants used by the field extractor.

Everything is driven by ``config.yaml`` through the :class:`Rules` dataclass,
so the seller can tune behaviour without editing code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Defaults (used when a value is missing from config.yaml)
# ---------------------------------------------------------------------------
#: Order-id pattern used by the extractor.
#:
#: * ``(\d{15,18})`` — **Group 1** is the base Meesho order id.
#: * ``(?:_\d+)?`` — an optional "sub-order" suffix (``_1``, ``_2`` …) that
#:   Meesho appends when an order is split into multiple shipments.  It sits
#:   *outside* Group 1 so callers always receive the bare base id.
#: * ``(?<!\d)`` / ``(?!\d)`` — keep the match from latching onto a fragment
#:   of a longer digit run.  ``\b`` cannot be used on the right-hand edge
#:   because ``_`` is a word character, so a trailing ``_1`` swallowed the
#:   boundary and the id was missed entirely (the original Sub-Order-ID bug).
ORDER_ID_PATTERN = r"(?<!\d)(\d{15,18})(?:_\d+)?(?!\d)"

DEFAULT_ORDER_ID_PRIMARY = ORDER_ID_PATTERN
DEFAULT_ORDER_ID_GENERIC = r"\b\d{6,20}\b"
DEFAULT_PIN_CODE = r"\b\d{6}\b"
DEFAULT_PHONE = r"\b[6-9]\d{9}\b"

#: Full-vs-truncated order-id classification.
#:
#: A digit run is a **full** order id when it has at least
#: :data:`DEFAULT_MIN_FULL_ORDER_ID_DIGITS` digits.  Order ids may start with
#: **any** digit — the old "only ``34``-prefixed ids are real" assumption was
#: wrong for the cropped 6–15 digit screenshots customers actually send, so it
#: is gone.  A run is only *truncated* when it is immediately followed by an
#: ellipsis (``Order #4829103829…``) or has fewer than six digits
#: (``Order #12345``).
DEFAULT_MIN_FULL_ORDER_ID_DIGITS = 6

#: Postal-context detection for 6-digit PIN codes.  A bare 6-digit message (or
#: one next to an "Order #" label) is an order id — only a ``pin`` /
#: ``pincode`` / ``postal`` / ``zip`` label (English or regional), the
#: ``City - 673311`` dash / ``Jaipur, 302001`` comma / ``(673311)`` bracket of
#: an address line, or an address / ``Deliver to`` block makes it a PIN code.
_PIN_LABEL_RE = re.compile(
    r"(?i)"
    r"(?:\bpin(?:\s*code)?\b|\bpostal(?:\s*code)?\b|\bzip(?:\s*code)?\b"
    r"|पिनकोड|पिन|பின்|പിൻ|పిన్|পিন)"
    r"\s*[:\-\u2013]?\s*$"
)
_PIN_DASH_RE = re.compile(r"(?:[A-Za-z\u0900-\u097F]\s*)?[\-\u2013]\s*$")
#: ``Jaipur, 302001`` / ``(673311)`` — an address punctuation separator sitting
#: directly before the 6-digit run.
_PIN_SEP_BEFORE_RE = re.compile(r"[,\(]\s*$")

#: Indian states / union territories and the destination cities Meesho ships
#: to, plus the generic address words.  A 6-digit run within
#: :data:`_ADDRESS_CONTEXT_WINDOW` characters of any of these — or sitting in an
#: address block at all — is a postal PIN, never an order id
#: (see :meth:`Rules.is_pin_context`).
_INDIA_STATES_AND_CITIES = [
    "andhra pradesh", "arunachal", "assam", "bihar", "chhattisgarh", "chattisgarh",
    "goa", "gujarat", "haryana", "himachal", "jharkhand", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
    "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu", "tamilnadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "delhi", "new delhi", "jammu", "kashmir", "ladakh", "puducherry", "pondicherry",
    "chandigarh",
    "surat", "mumbai", "bengaluru", "bangalore", "chennai", "hyderabad", "kolkata",
    "pune", "ahmedabad", "jaipur", "lucknow", "patna", "bhopal", "indore",
]
_ADDRESS_KEYWORDS = [
    "deliver to", "delivery address", "shipping address", "customer address",
    "house", "flat", "plot", "road", "street", "nagar", "colony", "near", "opp",
    "behind", "ward", "village", "post", "tehsil", "taluk", "dist", "district",
    "mandal", "thana", "panchayat", "sector", "phase", "block",
]


def _phrase_alternation(phrases: List[str]) -> str:
    """Join ``phrases`` into a first-match-wins alternation (longest first).

    Multi-word phrases allow any whitespace run between the words, mirroring
    :func:`_label_to_regex` (kept inline here because this block runs before
    that helper is defined).
    """
    parts = sorted({p for p in phrases if p}, key=len, reverse=True)
    return "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in parts)


_ADDRESS_CONTEXT_RE = re.compile(
    r"(?i)\b(?:" + _phrase_alternation(_INDIA_STATES_AND_CITIES + _ADDRESS_KEYWORDS) + r")\b"
)

#: How far (in characters) either side of a 6-digit run an address / state /
#: city keyword still marks it as a PIN (roughly two address lines).
_ADDRESS_CONTEXT_WINDOW = 80

#: Order-id "labels" (case-insensitive) used to decide whether a number sits
#: next to an order reference: Meesho's sub-order header, both apostrophe
#: variants phones autocorrect for ``Order ID`` (``Order I'd`` / ``Order I’d``)
#: and the regional labels seen on Hindi / Tamil / Telugu / Bengali screenshots.
#:
#: Every genuine order id carries a label (task spec rule C), so the "short"
#: label forms — a bare ``Order:`` / ``Order #`` / ``ऑर्डर #``… — are listed too.
#: **Order matters**: the longer, more specific form must precede the shorter
#: one (``Order ID`` before ``Order:``, ``ऑर्डर आईडी`` before ``ऑर्डर``) because
#: :func:`_label_to_regex` joins them into a first-match-wins alternation.
DEFAULT_ORDER_ID_LABELS = [
    # English — sub-order header, the classic forms and a bare "Order:".
    "Sub OrderID", "Sub Order ID", "Sub-Order No", "Sub Order No",
    "Order ID", "Order Id", "Order No", "Order Number", "OrderId",
    "Order #", "Order#",
    "Order I'd", "Order I’d",
    "Order:",
    # Hindi / Devanagari — "ID" and "number" forms, "#" / ":" forms and the
    # alternate spelling with a leading आ.
    "ऑर्डर आईडी", "ऑर्डर संख्या", "ऑर्डर नंबर", "ऑर्डर नं",
    "ऑर्डर #", "ऑर्डर:",
    "आर्डर आईडी", "आर्डर",
    # Tamil.
    "ஆர்டர் ஐடி", "ஆர்டர் எண்", "ஆர்டர் #", "ஆர்டர்:",
    # Telugu.
    "ఆర్డర్ ఐడి", "ఆర్డర్ ఐడీ", "ఆర్డర్ నంబర్", "ఆర్డర్ #", "ఆర్డర్:",
    # Bangla.
    "অর্ডার আইডি", "অর্ডার নম্বর", "অর্ডার নং", "অর্ডার #", "অর্ডার:",
]

#: Pre-sub-order defaults.  ``config.yaml`` (and ``frameflow_config.DEFAULTS``)
#: still pin these exact values, so :meth:`Rules.from_config` transparently
#: upgrades an *untouched* config onto the new defaults above — otherwise the
#: live pipeline keeps the old, Sub-Order-ID-blind behaviour even though the
#: code has been fixed.
LEGACY_ORDER_ID_PRIMARY = r"\b\d{16}\b"
LEGACY_ORDER_ID_LABELS = ["Order ID", "Order No", "Order Number", "OrderId", "Order #"]

#: The label list shipped *before* the multilingual / short-label expansion.
#: ``config.yaml`` is written with the resolved list, so a user who never
#: touched the labels keeps this exact list on disk; :meth:`Rules.from_config`
#: upgrades it onto :data:`DEFAULT_ORDER_ID_LABELS` the same way it upgrades
#: :data:`LEGACY_ORDER_ID_LABELS`, so the live pipeline gains ``ऑर्डर #`` /
#: ``Order:`` recognition without the seller editing YAML.
PRE_EXPANSION_ORDER_ID_LABELS = [
    "Sub OrderID", "Sub Order ID",
    "Order ID", "Order Id", "Order No", "Order Number", "OrderId",
    "Order #", "Order#",
    "Order I'd", "Order I’d",
    "ऑर्डर आईडी", "ऑर्डर संख्या", "ऑर्डर नंबर",
    "ஆர்டர் ஐடி", "ஆர்டர் எண்",
    "ఆర్డర్ ఐడి", "ఆర్డర్ నంబర్",
    "অর্ডার আইডি", "অর্ডার নম্বর",
]
DEFAULT_NAME_LABELS = ["Customer Name", "Name", "Ship to", "Deliver to", "Receiver",
                       "Sold to", "Delivery Address", "Customer",
                       # Hindi / Tamil / Telugu / Bangla "name" labels (§3.3).
                       "ग्राहक", "பெயர்", "పేరు", "নাম"]
DEFAULT_ADDRESS_LABELS = ["Address", "Shipping Address", "Deliver to", "Delivery Address",
                          # Hindi / Tamil / Telugu / Bangla "address" labels (§3.3).
                          "पता", "முகவரி", "చిరునామా", "ঠিকানা",
                          # The full "Delivery Address" phrase, so the captured
                          # block starts *after* it instead of grabbing the
                          # label's tail ("डिलीवरी का पता <name>" -> the name
                          # alone would be the whole address).  Longer labels
                          # must come first: the first match wins.
                          "डिलीवरी का पता", "डिलीवरी पता", "शिपिंग पता",
                          "டெலிவரி முகவரி", "விநியோக முகவரி",
                          "డెలివరీ అడ్రసు", "డెలివరీ చిరునామా",
                          "ডেলিভারি ঠিকানা", "ডেলিভারি অ্যাড্রেস"]
DEFAULT_IGNORE_WORDS = ["Meesho", "Flipkart", "Amazon", "Invoice", "Order", "Address",
                        "Change", "Supplier"]

# Currency markers that tell us a number is a *price*, not an order id.
#
# Symbols are unambiguous and matched literally; currency *words* must match
# as whole words, otherwise "rs" matches inside "Orders", "Sellers", "Buyers"
# or "Hours" and rejects perfectly valid order ids.  This mirrors the
# ``_CURRENCY_RE`` used by :mod:`extractor.parser` for address filtering.
CURRENCY_SYMBOLS = ("₹", "$", "€")
CURRENCY_WORDS = ("rs", "rs.", "inr", "usd", "eur")

#: Kept for backwards compatibility (union of both groups).
CURRENCY_MARKERS = CURRENCY_SYMBOLS + CURRENCY_WORDS

#: Whole-word matcher for :data:`CURRENCY_WORDS` (``rs`` / ``rs.`` / ``inr`` …).
_CURRENCY_WORD_RE = re.compile(r"\b(?:rs\.?|inr|usd|eur)\b", re.IGNORECASE)

#: Labels that mark a nearby number as **product metadata** (SKU / size /
#: quantity / a tracking or invoice number …) rather than an order id.  They
#: are only consulted when the number is *not* sitting next to a real order
#: label, so an explicit ``Order ID`` line always wins.
DEFAULT_NON_ORDER_LABELS = [
    "SKU", "Model No", "Model", "Style",
    "Item No", "Item Code", "Product Code", "Product ID",
    "Qty", "Quantity", "Size", "MRP", "AWB", "Tracking",
    "Invoice No", "GST", "HSN",
]

#: ETA / delivery labels.  A number sitting next to one of these is a delivery
#: *estimate*, never an order id.  ``Delivery`` is excluded when it forms
#: ``Delivery Address`` (a real customer label) so the name/address rules are
#: not disturbed.
DEFAULT_ETA_LABELS = ["ETA", "Expected", "Arriving", "Ship by", "Delivery"]
_ETA_RE = re.compile(
    r"(?i)\b(?:ETA|Expected|Arriving|Ship\s*by|Delivery)\b(?!\s*(?:Address|Date))"
)

#: Truncation length for the seller's short-key Notepad workflow.
#:
#: **Assumption** (see AGENTS.md / the task spec §3.5): within this seller's
#: order volume no two distinct Meesho orders share the same short window, so
#: the window is a stable key.  Exposed as config
#: ``extraction.order_id_truncate_length``; never hardcode 6 in the parser.
DEFAULT_ORDER_ID_TRUNCATE_LENGTH = 6

#: Which end of the order id the ``order_id_truncate_length`` window is taken
#: from: ``"last"`` (the original trailing-window workflow) or ``"first"``.
#:
#: The seller crops the order id from the **back** side of the Meesho screen,
#: so the leading digits survive every crop while the tail depends on how much
#: of the line the customer's crop happened to keep — ``"first"`` is therefore
#: the stable side.  Exposed as config ``extraction.order_id_truncate_from``;
#: never hardcode the direction in the parser.
DEFAULT_ORDER_ID_TRUNCATE_FROM = "last"

#: Default output mode.  ``False`` = **one last-6 value per chat** (§3.1–§3.6);
#: ``True`` = the earlier multi-order behaviour (one row per distinct id).
DEFAULT_MULTI_ORDER_OUTPUT = False

#: §3.2 unlabelled Meesho id length band and the §3.6 list-page cap.
DEFAULT_MIN_UNLABELLED_ID_DIGITS = 16
DEFAULT_MAX_UNLABELLED_ID_DIGITS = 18
DEFAULT_LIST_PAGE_MAX_CANDIDATES = 3

#: §3.1 label-first extraction.  The seller confirmed **every** order id carries
#: a label, so the unlabelled 16–18 digit fallback is disabled by default and
#: PIN / phone heuristics only matter when it is re-enabled.
DEFAULT_REQUIRE_ORDER_ID_LABEL = True

#: A labelled value with fewer than this many digits is a *crop* of a longer id
#: (the screenshot cut it off) -> partial.  16–18 digits is treated as a full id.
DEFAULT_FULL_ID_MIN_DIGITS = 16

#: Longest digit run that can still be a single labelled order id; longer runs
#: are the label's value glued onto the next field (a concatenation).
DEFAULT_MAX_LABELLED_ID_DIGITS = 18

#: When true a **cropped** order id counts as a complete id.
#:
#: The customers crop the *back* of the Meesho screen, so the leading digits
#: of the id survive every crop — the visible "Order #342581" of a cropped
#: screenshot is exactly the short key the seller works with.  With this on,
#: such a crop is written as a plain id (no ``TRUNC:`` prefix, no review
#: flag) instead of being sent to the review queue.
DEFAULT_CROPPED_ID_IS_COMPLETE = False

#: How many digits a *cut-off* order id must show before it is treated as a
#: real order key (and therefore preferred over the address fallback).
#:
#: Meesho order ids are 15–18 digits long and the customers crop the **tail**
#: of the screen, so a visible fragment of six digits or more is still the
#: seller's key.  A shorter fragment could be a PIN, a quantity or a price and
#: is not emitted as an order id.
DEFAULT_TRUNCATED_ID_MIN_DIGITS = 6

#: When true a partial (6–15 digit) labelled value is emitted as ``TRUNC:<…>``;
#: when false it is emitted bare but still flagged ``partial`` for review.
DEFAULT_PARTIAL_ID_PREFIX = True

#: What a row collects.
#:
#: ``"single"`` — **one** value per row: the order id, else the customer
#: name, else the address.  This is the seller's Notepad workflow and the
#: default.
#: ``"all"`` — the order id **and** the customer name **and** the address in
#: the same row.
DEFAULT_COLLECT = "single"

#: The accepted values of the ``extraction.collect`` setting.
COLLECT_MODES = ("single", "all")


def _collect_mode(value: Any) -> str:
    """Normalise the ``extraction.collect`` setting to a known mode."""
    mode = str(value or DEFAULT_COLLECT).strip().lower()
    return mode if mode in COLLECT_MODES else DEFAULT_COLLECT

#: Field labels that must never sit *between* an order label and the number that
#: follows it (``Order ID # PIN: 517126`` — 517126 is the PIN, not the id).
#: The bare ``order`` entry also catches a *nested* order label, e.g.
#: ``Order ID #\nOrder: 342343``.  The regional ``pin`` words are listed so a
#: Hindi screenshot (``Order ID: पिन 517126``) is caught too.
INTERVENING_LABEL_RE = re.compile(
    r"(?i)\b(?:pin|pin\s*code|postal|zip|mobile|phone|contact|awb|tracking|sku|"
    r"price|amount|total|qty|quantity|discount|customer|name|address|date|"
    r"delivery|eta|return|refund|status|order)\b"
    r"|पिनकोड|पिन|பின்|പിൻ|పిన్|পিন"
)

#: How many newlines may sit between an order label and its value.  A real id is
#: never more than one blank line below its label, so anything deeper means the
#: number belongs to a *different* field further down the screenshot.
MAX_LABEL_VALUE_NEWLINES = 2

#: Per-filter toggles for the §3.4 rejection rules.  Every entry can be turned
#: off from ``config.yaml -> extraction.reject_patterns``.
DEFAULT_REJECT_PATTERNS: Dict[str, bool] = {
    "phone": True,
    "pin": True,
    "price": True,
    "date": True,
    "product": True,
    "eta": True,
    "concatenation": True,
}

#: Compiled §3.4 reject patterns (the numeric-shape half of them).  Context
#: filters (price adjacency, date separators, product/ETA labels) live on
#: :class:`Rules` because they need the surrounding text.
REJECT_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    # Phone: 10-digit mobile, with optional +91 / 0 country prefix.
    "phone": re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{9}"),
    # Date: dd/mm/yyyy, mm-dd-yy, yyyy-mm-dd.
    "date": re.compile(
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
        r"|\b(?:19|20)\d{2}[/-]\d{1,2}[/-]\d{1,2}\b"
    ),
    # Product / SKU codes.
    "product": re.compile(
        r"(?i)\b(?:sku|pid|product(?:\s*(?:code|id))?|item\s*code|hsn)\b"
    ),
    # Price markers (symbols + words).
    "price": re.compile(r"₹|/-|\b(?:rs\.?|inr)\b", re.IGNORECASE),
}

#: Month names / abbreviations used to spot a date fragment.  The match is
#: anchored tightly (the month must be the token immediately before/after the
#: number) so the common English word "may" in ordinary chat text never
#: rejects a genuine id.
_MONTH_PATTERN = (
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_MONTH_TAIL_RE = re.compile(rf"(?i)\b(?:{_MONTH_PATTERN})\b[\s,./\-]*$")
_MONTH_HEAD_RE = re.compile(rf"(?i)^[\s,./\-]*(?:{_MONTH_PATTERN})\b")
#: A number flanked by date separators (``15/10/2026``) is a date fragment.
_DATE_SEP_AFTER_RE = re.compile(r"\s*[/.\-]\s*\d")
_DATE_SEP_BEFORE_RE = re.compile(r"\d\s*[/.\-]\s*$")


def _is_yyyymmdd(value: str) -> bool:
    year, month, day = int(value[:4]), int(value[4:6]), int(value[6:8])
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def _is_ddmmyyyy(value: str) -> bool:
    day, month, year = int(value[:2]), int(value[2:4]), int(value[4:8])
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def normalize(text: Optional[str]) -> str:
    """Normalise raw OCR / WhatsApp text while **preserving newlines**.

    * unifies line endings and unicode space characters
    * strips trailing whitespace on every line
    * keeps blank-line depth intact (the label→value gap-depth guard, see
      :data:`MAX_LABEL_VALUE_NEWLINES`, needs the real newline count)
    """
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    # A few unicode "space" variants that OCR loves to emit.
    for ch in ("\u00a0", "\u2007", "\u202f", "\u2009", "\u200a", "\u2002", "\u2003"):
        text = text.replace(ch, " ")
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"[ \t]{2,}", " ", text)   # collapse horizontal runs
    return text.strip()


def _label_to_regex(label: str) -> str:
    """Turn a human label such as ``"Order #"`` into a flexible regex frag."""
    escaped = re.escape(label)
    # allow flexible whitespace inside multi-word labels
    escaped = escaped.replace(r"\ ", r"\s+")
    return escaped


#: Unicode ranges of the Indic scripts FrameFlow supports (Devanagari through
#: Malayalam).  Python's ``\w`` classifies *combining marks* (Mn/Mc) — the
#: vowel signs and viramas that every Hindi/Tamil/Telugu/Bangla word carries —
#: as non-word characters, so a plain ``\b`` fails next to them (e.g.
#: ``पता:`` never matched ``\bपता\b``).  This explicit class restores a usable
#: label boundary for those scripts.
_INDIC_CHARS = "\u0900-\u0D7F"


def _label_boundary(label_regex: str) -> str:
    """Wrap a label regex in a Unicode-aware word boundary."""
    cls = rf"[^\W{_INDIC_CHARS}]"
    return rf"(?<!{cls})(?:{label_regex})(?!{cls})"


@dataclass
class Rules:
    """Container for every tunable extraction rule."""

    order_id_regex_primary: str = DEFAULT_ORDER_ID_PRIMARY
    order_id_regex_generic: str = DEFAULT_ORDER_ID_GENERIC
    pin_code_regex: str = DEFAULT_PIN_CODE
    phone_regex: str = DEFAULT_PHONE

    order_id_labels: List[str] = field(default_factory=lambda: list(DEFAULT_ORDER_ID_LABELS))
    name_labels: List[str] = field(default_factory=lambda: list(DEFAULT_NAME_LABELS))
    address_labels: List[str] = field(default_factory=lambda: list(DEFAULT_ADDRESS_LABELS))
    ignore_words: List[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_WORDS))
    non_order_labels: List[str] = field(default_factory=lambda: list(DEFAULT_NON_ORDER_LABELS))

    text_message_limit: int = 50
    address_max_chars: int = 400

    # Full-vs-truncated order-id classification (see the DEFAULT_* constants).
    min_full_order_id_digits: int = DEFAULT_MIN_FULL_ORDER_ID_DIGITS

    # §3 short-window output + single-vs-multi output mode.
    order_id_truncate_length: int = DEFAULT_ORDER_ID_TRUNCATE_LENGTH
    order_id_truncate_from: str = DEFAULT_ORDER_ID_TRUNCATE_FROM
    multi_order_output: bool = DEFAULT_MULTI_ORDER_OUTPUT

    # §3.2 unlabelled Meesho id band and the §3.6 list-page cap.
    min_unlabelled_id_digits: int = DEFAULT_MIN_UNLABELLED_ID_DIGITS
    max_unlabelled_id_digits: int = DEFAULT_MAX_UNLABELLED_ID_DIGITS
    list_page_max_candidates: int = DEFAULT_LIST_PAGE_MAX_CANDIDATES

    # §3.1 label-first extraction (see the DEFAULT_* constants above).
    require_order_id_label: bool = DEFAULT_REQUIRE_ORDER_ID_LABEL
    full_id_min_digits: int = DEFAULT_FULL_ID_MIN_DIGITS
    max_labelled_id_digits: int = DEFAULT_MAX_LABELLED_ID_DIGITS
    partial_id_prefix: bool = DEFAULT_PARTIAL_ID_PREFIX

    eta_labels: List[str] = field(default_factory=lambda: list(DEFAULT_ETA_LABELS))
    reject_patterns: Dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_REJECT_PATTERNS)
    )

    # Truncated order-id detection (``Order #34187320244...``).
    truncated_min_digits: int = 1
    truncated_max_digits: int = 20
    #: A truncated id with at least this many digits is a *substantial* crop
    #: of a real order id, so it outranks the address fallback (a name still
    #: wins — see :func:`extractor.parser.extract_fields`).  Set to ``0`` to
    #: turn the preference off.
    truncated_id_min_digits: int = DEFAULT_TRUNCATED_ID_MIN_DIGITS
    #: A cropped order id counts as a complete id (see
    #: :data:`DEFAULT_CROPPED_ID_IS_COMPLETE`).
    cropped_id_is_complete: bool = DEFAULT_CROPPED_ID_IS_COMPLETE

    #: What a row collects.
    #:
    #: ``"single"`` (the default) — **one** value per row, taken in this
    #: order: the order id, else the customer name, else the address.
    #: ``"all"`` — the order id **and** the customer name **and** the address
    #: in the same row.
    collect: str = DEFAULT_COLLECT

    # Compiled patterns (populated in __post_init__).
    _re_primary: re.Pattern = field(init=False, repr=False, compare=False)
    _re_generic: re.Pattern = field(init=False, repr=False, compare=False)
    _re_pin: re.Pattern = field(init=False, repr=False, compare=False)
    _re_phone: re.Pattern = field(init=False, repr=False, compare=False)
    _re_phone_full: re.Pattern = field(init=False, repr=False, compare=False)
    _re_address_hint: re.Pattern = field(init=False, repr=False, compare=False)
    _re_order_label_value: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._re_primary = re.compile(self.order_id_regex_primary)
        self._re_generic = re.compile(self.order_id_regex_generic)
        self._re_pin = re.compile(self.pin_code_regex)
        self._re_phone = re.compile(self.phone_regex)
        self._re_phone_full = REJECT_PATTERNS["phone"]
        # §3.1: a label, the filler between it and the value, then a digit run.
        # The filler may cross up to MAX_LABEL_VALUE_NEWLINES newlines so
        # ``Order ID`` + newline + value (a common OCR layout) still matches —
        # the parser rejects a deeper gap and any intervening field label (see
        # INTERVENING_LABEL_RE).  Lazy filler means "Order ID 12345 67890"
        # captures only "12345" — adjacent groups are never joined.  The label
        # and filler are named groups so the guard can scan the filler *alone*.
        joined = "|".join(_label_to_regex(lbl) for lbl in self.order_id_labels)
        self._re_order_label_value = re.compile(
            rf"(?i)(?P<label>{joined})(?P<filler>[^\d]{{0,15}}?)(?P<value>\d+)"
        )
        hints = [lbl for lbl in self.address_labels if lbl]
        if hints:
            joined = "|".join(_label_to_regex(h) for h in hints)
            self._re_address_hint = re.compile(rf"(?i)(?:{joined})")
        else:  # pragma: no cover - only when the caller emptied address_labels
            self._re_address_hint = re.compile(r"(?!x)x")

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def looks_like_phone(self, value: str) -> bool:
        """True when ``value`` is exactly a 10-digit Indian mobile number."""
        return bool(self._re_phone.fullmatch(value.strip()))

    def looks_like_pin(self, value: str) -> bool:
        """True when ``value`` is exactly a 6-digit PIN code."""
        return bool(self._re_pin.fullmatch(value.strip()))

    def is_full_order_id(self, value: str) -> bool:
        """True when ``value`` has enough digits to be a complete order id.

        Any digit run with at least :attr:`min_full_order_id_digits` digits is
        a full id — order ids may start with any digit, so a length-only check
        is correct.  The trailing-ellipsis test (which needs the surrounding
        text) lives in :mod:`extractor.parser`.
        """
        value = (value or "").strip()
        return len(value) >= self.min_full_order_id_digits

    def _in_address_block(self, text: str, pos: int) -> bool:
        """True when ``pos`` falls inside an address / "Deliver to" block.

        The scan walks back to the start of the current block (a blank line
        ends it).  A line carrying an order-id label means we are *not* in an
        address block, so an "Order #482910" line is never mis-read as a PIN.
        """
        text = text or ""
        block_start = text.rfind("\n", 0, pos) + 1
        while block_start > 0:
            prev_end = block_start - 1
            prev_start = text.rfind("\n", 0, prev_end) + 1
            prev_line = text[prev_start:prev_end]
            if not prev_line.strip():
                break
            block_start = prev_start
        block = text[block_start:pos]
        if any(lbl.lower() in block.lower() for lbl in self.order_id_labels):
            return False
        return bool(self._re_address_hint.search(block))

    def is_pin_context(self, text: str, start: int, end: int) -> bool:
        """True when the 6-digit run at ``text[start:end]`` is a postal PIN.

        A 6-digit number is a valid order id unless it is clearly postal.  The
        postal signals, in order, are:

        * a ``pin`` / ``pincode`` / ``postal`` / ``zip`` label (English or the
          Hindi / Tamil / Malayalam / Telugu / Bangla equivalent) sitting
          directly before it;
        * an address separator before it — the ``City - 673311`` dash, the
          ``Jaipur, 302001`` comma or the ``(673311)`` bracket;
        * an Indian state / union territory / destination city or a generic
          address word within :data:`_ADDRESS_CONTEXT_WINDOW` characters;
        * a 10-digit mobile number immediately after it (Meesho prints the PIN
          line directly above the contact number);
        * an address / ``Deliver to`` block at all.

        **Same-line order-label override**: an ``Order #482910`` /
        ``Order No - 482910`` label on the same line before the number always
        wins, unless a ``pin`` / ``पिन`` label sits between the order label and
        the digits (``Order ID: PIN 517126``).
        """
        if not self.looks_like_pin((text or "")[start:end]):
            return False
        text = text or ""
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]

        # Order label on the same line *before* the number => trust it, unless a
        # PIN label sits between the two ("Order ID: PIN 517126").
        label_pos = -1
        for lbl in self.order_id_labels:
            pos = line.lower().rfind(lbl.lower(), 0, start - line_start)
            if pos > label_pos:
                label_pos = pos
        if label_pos >= 0:
            between = line[label_pos:start - line_start]
            return bool(_PIN_LABEL_RE.search(between))

        before = text[max(0, start - 24):start]
        if _PIN_LABEL_RE.search(before):
            return True
        if _PIN_DASH_RE.search(before) or _PIN_SEP_BEFORE_RE.search(before):
            return True
        if self.has_address_context(text, start, end):
            return True
        if self.followed_by_mobile_number(text, end):
            return True
        return self._in_address_block(text, start)

    def has_address_context(
        self, text: str, start: int, end: int, window: int = _ADDRESS_CONTEXT_WINDOW
    ) -> bool:
        """True when a state / city / address word sits within ``window`` chars.

        Used as a *postal* signal: ``Rajasthan 302001`` and ``302001
        Rajasthan`` both mark the 6-digit run as a PIN code.
        """
        text = text or ""
        before = text[max(0, start - window):start]
        after = text[end:end + window]
        return bool(
            _ADDRESS_CONTEXT_RE.search(before) or _ADDRESS_CONTEXT_RE.search(after)
        )

    def line_has_address_context(self, text: str, start: int, end: int) -> bool:
        """True when the *line* holding a 6-digit run carries a postal signal.

        The Rule C guard for short labelled values: an ``Order ID #`` header
        detached from the delivery block often sits one line above
        ``Kerala - 673311`` — the number on that line is the PIN, not the id.
        """
        text = text or ""
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        if _ADDRESS_CONTEXT_RE.search(line):
            return True
        before = text[line_start:start]
        if _PIN_LABEL_RE.search(before):
            return True
        return bool(
            _PIN_DASH_RE.search(before) or _PIN_SEP_BEFORE_RE.search(before)
        )

    def followed_by_mobile_number(self, text: str, end: int, window: int = 16) -> bool:
        """True when a 10-digit Indian mobile number follows within ``window``.

        A Meesho delivery block prints the PIN line immediately above the
        contact number, so ``517126`` next to ``8309847092`` is the PIN.
        """
        after = (text or "")[end:end + window]
        return bool(re.match(r"\s*(?:\+?91[\s-]?)?[6-9]\d{9}\b", after))

    def has_currency_nearby(self, text: str, start: int, end: int, window: int = 6) -> bool:
        """True when a currency marker sits just before/after ``text[start:end]``.

        Currency *symbols* (``₹ $ €``) are matched literally.  Currency *words*
        (``rs``, ``inr``, ``usd``, ``eur``) must match as whole words so that a
        label such as ``"Orders:"`` (which contains the letters ``rs``) is not
        mistaken for a price.
        """
        before = text[max(0, start - window):start]
        after = text[end:end + window]
        if any(sym in before or sym in after for sym in CURRENCY_SYMBOLS):
            return True
        return bool(_CURRENCY_WORD_RE.search(before) or _CURRENCY_WORD_RE.search(after))

    def near_label(self, text: str, start: int, end: int, labels: List[str]) -> bool:
        """True when one of ``labels`` appears on the same or the previous line."""
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        current_line = text[line_start:line_end].lower()
        if any(lbl.lower() in current_line for lbl in labels):
            return True

        # Previous line (covers the "Label\n<value>" layout).
        if line_start > 0:
            prev_end = line_start - 1
            prev_start = text.rfind("\n", 0, prev_end) + 1
            prev_line = text[prev_start:prev_end].lower()
            if any(lbl.lower() in prev_line for lbl in labels):
                return True
        return False

    def is_valid_order_id_candidate(self, text: str, match: re.Match) -> bool:
        """Apply the rejection rules from the spec to a matched number."""
        return not self.is_non_order_number(text, match.start(), match.end())

    def looks_like_date(
        self, value: str, text: str = "", start: int = 0, end: int = 0
    ) -> bool:
        """True when a numeric candidate is really a date / date fragment.

        Catches compact ``YYYYMMDD`` / ``DDMMYYYY`` runs (``20261015``), a
        number flanked by date separators (``15/10/2026``) and a number sitting
        directly next to a month name (``15 October``).
        """
        value = (value or "").strip()
        if len(value) == 8 and value.isdigit():
            if _is_yyyymmdd(value) or _is_ddmmyyyy(value):
                return True
        if not text:
            return False
        before = text[max(0, start - 12):start]
        after = text[end:end + 12]
        if _MONTH_TAIL_RE.search(before) or _MONTH_HEAD_RE.match(after):
            return True
        if _DATE_SEP_AFTER_RE.match(after) or _DATE_SEP_BEFORE_RE.search(before):
            return True
        return False

    def near_non_order_label(self, text: str, start: int, end: int) -> bool:
        """True when a product-metadata label sits on this or the previous line."""
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end].lower()
        if any(lbl.lower() in line for lbl in self.non_order_labels):
            return True
        if line_start > 0:
            prev_end = line_start - 1
            prev_start = text.rfind("\n", 0, prev_end) + 1
            prev_line = text[prev_start:prev_end].lower()
            if any(lbl.lower() in prev_line for lbl in self.non_order_labels):
                return True
        return False

    def is_non_order_number(
        self, text: str, start: int, end: int, value: Optional[str] = None
    ) -> bool:
        """Reject a number that is a phone, PIN, price, date or product number.

        A number explicitly sitting next to an order label is trusted: only the
        unambiguous phone / currency checks still apply to it, so an
        ``Order ID: 482910`` line is never mistaken for a PIN or a date.
        """
        if value is None:
            value = (text or "")[start:end]
        if self.looks_like_phone(value):
            return True
        if self.looks_like_pin(value) and self.is_pin_context(text, start, end):
            return True
        if self.has_currency_nearby(text, start, end):
            return True
        if not self.near_label(text, start, end, self.order_id_labels):
            if self.looks_like_date(value, text, start, end):
                return True
            if self.near_non_order_label(text, start, end):
                return True
        return False

    # ------------------------------------------------------------------
    # §3.4 rejection filters (the new, toggleable pipeline)
    # ------------------------------------------------------------------
    def near_eta_label(self, text: str, start: int, end: int) -> bool:
        """True when a delivery / ETA label sits on this or the previous line.

        ``Delivery Address`` is deliberately *not* an ETA label (the negative
        look-ahead in :data:`_ETA_RE`), otherwise the customer's address block
        would reject legitimate ids.
        """
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        if _ETA_RE.search(text[line_start:line_end]):
            return True
        if line_start > 0:
            prev_end = line_start - 1
            prev_start = text.rfind("\n", 0, prev_end) + 1
            if _ETA_RE.search(text[prev_start:prev_end]):
                return True
        return False

    def looks_like_phone_extended(self, value: str) -> bool:
        """True for a 10-digit mobile with an optional ``+91`` / ``91`` prefix."""
        return bool(self._re_phone_full.fullmatch((value or "").strip()))

    def is_rejected_candidate(
        self,
        text: str,
        start: int,
        end: int,
        value: Optional[str] = None,
        labelled: bool = False,
    ) -> bool:
        """Apply the §3.4 hard rejection filters to one numeric candidate.

        Every filter can be switched off from
        ``config.yaml -> extraction.reject_patterns``.  ``labelled=True`` marks
        a candidate that sits right after an order label: only the unambiguous
        phone / price filters still apply to it, so ``Order ID: 482910`` is
        never mistaken for a PIN or a date.
        """
        flags = self.reject_patterns or {}
        if value is None:
            value = (text or "")[start:end]

        if flags.get("phone", True) and self.looks_like_phone_extended(value):
            return True
        if flags.get("price", True) and self.has_currency_nearby(text, start, end):
            return True
        if labelled:
            return False
        if flags.get("pin", True) and self.looks_like_pin(value) and self.is_pin_context(
            text, start, end
        ):
            return True
        if flags.get("date", True) and self.looks_like_date(value, text, start, end):
            return True
        if flags.get("product", True) and self.near_non_order_label(text, start, end):
            return True
        if flags.get("eta", True) and self.near_eta_label(text, start, end):
            return True
        return False

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "Rules":
        """Build a :class:`Rules` instance from a parsed ``config.yaml``."""
        cfg = (config or {}).get("extraction", {}) or {}

        # Transparently migrate an *untouched* legacy config onto the new
        # sub-order-aware defaults.  A config that still carries the exact old
        # ``\b\d{16}\b`` pattern (or the exact old label list) cannot match
        # Meesho ``<15-18 digits>_1`` ids, so upgrading it is always safe.  Any
        # value the user explicitly customised is left exactly as supplied.
        primary = cfg.get("order_id_regex_primary", DEFAULT_ORDER_ID_PRIMARY)
        if primary == LEGACY_ORDER_ID_PRIMARY:
            primary = DEFAULT_ORDER_ID_PRIMARY

        labels = list(cfg.get("order_id_labels", DEFAULT_ORDER_ID_LABELS))
        if labels == LEGACY_ORDER_ID_LABELS or labels == PRE_EXPANSION_ORDER_ID_LABELS:
            labels = list(DEFAULT_ORDER_ID_LABELS)

        partial_id_prefix = bool(
            cfg.get("partial_id_prefix", DEFAULT_PARTIAL_ID_PREFIX)
        )
        if not partial_id_prefix:
            # Without the ``TRUNC:`` prefix a partial id is only distinguishable
            # in a CSV row through the dedicated column, so refuse a config the
            # writer cannot honour instead of silently losing the distinction.
            from output.csv_writer import CSV_HEADER

            if "partial" not in CSV_HEADER:
                raise ValueError(
                    "partial_id_prefix: false requires 'partial' in the CSV schema"
                )

        return cls(
            order_id_regex_primary=primary,
            order_id_regex_generic=cfg.get("order_id_regex_generic", DEFAULT_ORDER_ID_GENERIC),
            pin_code_regex=cfg.get("pin_code_regex", DEFAULT_PIN_CODE),
            phone_regex=cfg.get("phone_regex", DEFAULT_PHONE),
            order_id_labels=labels,
            name_labels=list(cfg.get("name_labels", DEFAULT_NAME_LABELS)),
            address_labels=list(cfg.get("address_labels", DEFAULT_ADDRESS_LABELS)),
            ignore_words=list(cfg.get("ignore_words", DEFAULT_IGNORE_WORDS)),
            non_order_labels=list(cfg.get("non_order_labels", DEFAULT_NON_ORDER_LABELS)),
            text_message_limit=int(cfg.get("text_message_limit", 50)),
            address_max_chars=int(cfg.get("address_max_chars", 400)),
            truncated_min_digits=int(cfg.get("truncated_min_digits", 1)),
            truncated_max_digits=int(cfg.get("truncated_max_digits", 20)),
            truncated_id_min_digits=int(
                cfg.get("truncated_id_min_digits", DEFAULT_TRUNCATED_ID_MIN_DIGITS)
            ),
            cropped_id_is_complete=bool(
                cfg.get("cropped_id_is_complete", DEFAULT_CROPPED_ID_IS_COMPLETE)
            ),
            collect=_collect_mode(cfg.get("collect", DEFAULT_COLLECT)),
            order_id_truncate_length=int(
                cfg.get("order_id_truncate_length", DEFAULT_ORDER_ID_TRUNCATE_LENGTH)
            ),
            order_id_truncate_from=str(
                cfg.get("order_id_truncate_from", DEFAULT_ORDER_ID_TRUNCATE_FROM) or ""
            ).strip().lower(),
            multi_order_output=bool(
                cfg.get("multi_order_output", DEFAULT_MULTI_ORDER_OUTPUT)
            ),
            min_unlabelled_id_digits=int(
                cfg.get("min_unlabelled_id_digits", DEFAULT_MIN_UNLABELLED_ID_DIGITS)
            ),
            max_unlabelled_id_digits=int(
                cfg.get("max_unlabelled_id_digits", DEFAULT_MAX_UNLABELLED_ID_DIGITS)
            ),
            list_page_max_candidates=int(
                cfg.get("list_page_max_candidates", DEFAULT_LIST_PAGE_MAX_CANDIDATES)
            ),
            require_order_id_label=bool(
                cfg.get("require_order_id_label", DEFAULT_REQUIRE_ORDER_ID_LABEL)
            ),
            full_id_min_digits=int(
                cfg.get("full_id_min_digits", DEFAULT_FULL_ID_MIN_DIGITS)
            ),
            max_labelled_id_digits=int(
                cfg.get("max_labelled_id_digits", DEFAULT_MAX_LABELLED_ID_DIGITS)
            ),
            partial_id_prefix=partial_id_prefix,
            eta_labels=list(cfg.get("eta_labels", DEFAULT_ETA_LABELS)),
            reject_patterns=dict(
                cfg.get("reject_patterns", DEFAULT_REJECT_PATTERNS) or {}
            ),
            min_full_order_id_digits=int(
                cfg.get("min_full_order_id_digits", DEFAULT_MIN_FULL_ORDER_ID_DIGITS)
            ),
        )

    # Convenience: pre-compiled label regexes -------------------------------
    def name_label_pattern(self, label: str) -> re.Pattern:
        return re.compile(
            r"(?i)"
            + _label_boundary(_label_to_regex(label))
            + r"\s*[:\-–]?\s*(.*)$"
        )

    def address_label_pattern(self, label: str) -> re.Pattern:
        return re.compile(
            r"(?i)"
            + _label_boundary(_label_to_regex(label))
            + r"\s*[:\-–]?\s*(.*)$"
        )
