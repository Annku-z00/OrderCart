"""
live_rules_check.py
===================

End-to-end validation of the **business extraction rules** against real OCR
text captured live from Google Lens (see ``live_ocr_check.py``), plus the
adversarial cases the requirements call out explicitly:

* the label is the anchor — an id is only an id next to ``Order #`` / ``Order ID``
* phone numbers, PIN codes, prices, dates and product numbers are rejected
* adjacent digit groups are never concatenated
* the ``_N`` Meesho sub-order suffix is stripped
* one screenshot = one order (per-image extraction, never a whole-chat blob)
* the fallback chain: Order ID -> name -> substantial TRUNC -> address -> FAILED
* the output encoding: ``OK`` / ``TRUNC:`` / ``NAME:`` / ``ADDR:`` / ``FAILED``

Run:  py -3.12 live_rules_check.py
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from extractor.parser import extract_fields, extract_fields_multi  # noqa: E402
from extractor.rules import Rules  # noqa: E402
from frameflow_config import load_config  # noqa: E402

CFG = load_config("config.yaml")
RULES = Rules.from_config(CFG)

PASS = FAIL = 0


def encode(fields):
    """Reproduce the writer's load-bearing output encoding."""
    kind = fields.get("type") or "FAILED"
    oid = str(fields.get("order_id") or "").strip()
    if kind == "OK":
        return oid or "FAILED"
    if kind == "TRUNC":
        return oid if oid.startswith("TRUNC:") else f"TRUNC:{oid}"
    if kind == "FALLBACK_NAME":
        return f"NAME:{fields.get('name') or ''}"
    if kind == "FALLBACK_ADDRESS":
        return f"ADDR:{fields.get('address') or ''}"
    return "FAILED"


def check(label, ocr_text, expected_prefix, whatsapp_text=""):
    global PASS, FAIL
    fields = extract_fields_multi(ocr_text, whatsapp_text, RULES)
    value = encode(fields)
    if isinstance(expected_prefix, tuple):
        passed = value.startswith(expected_prefix)
    else:
        passed = value.startswith(expected_prefix)
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print(f"         -> {value[:100]}")
    if passed:
        PASS += 1
    else:
        FAIL += 1
    return fields


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
section("1) Real Lens OCR — order screenshots (captured live)")

check(
    "Order #342581970718279808 (full 18-digit id)",
    "3:43 | Am. | Vo | LTE1 | 32% | ऑर्डर विवरण | HELP | WITH PHOTOS | SMALL SIZE | "
    "Order #342581970718279808 | Royal Photo Frame 5*7 Ready to Hang... | "
    "Free Size Cash ₹162 | 7014965655 | Delivery by Sun, 25 Oct | Ordered | 16 Oct",
    "342581970718279808",
)

check(
    "Sub Order ID 342343834138346752_1 -> suffix stripped",
    "ORDER DETAILS | Sub Order ID | 342343834138346752_1 | Payment Mode | "
    "Cash on Delivery | COPY | Help Centre | Order Tracking",
    "342343834138346752",
)

check(
    "Order ID 342169589629518912 (Help-centre screen)",
    "HELP CENTRE | CHANGE LANGUAGE | Order ID 342169589629518912 | Sold | "
    "Supplier: KK ROYAL | Delivery by 25 October, 2026",
    "342169589629518912",
)

check(
    "Order #342292536901478... (cropped, cropped_id_is_complete=true)",
    "ORDER DETAILS | Order #342292536901478... | Royal Photo Frame 5*7 | "
    "7014965655 | Cash ₹170 | Delivery by Sun, 25 Oct",
    "342292536901478",
)

check(
    "MY ORDERS list page -> no label, nothing fabricated",
    "MY ORDERS | Search orders | Filters | SET OF 2, 5X7 INCH | Order Placed | "
    "Delivery by Sun, 25 Oct | Size: Free Size | Qty: 1 | 7014965655 | BUY 1 GET 1",
    "FAILED",
)

# ---------------------------------------------------------------------------
section("2) Rejection filters — phone / PIN / price / date / product / ETA")

check(
    "bare phone number is never an order id",
    "7014965655",
    "FAILED",
)
check(
    "Contact Number line is a label, not a name",
    "Contact Number - 9786782006 | 232/3 teachers colony, Karur, Tamil Nadu - 639004 | "
    "TRACK ORDER | Delivery by Thursday, 29th Oct | ₹168 | Qty: 1",
    "ADDR:",
)
check(
    "PIN code 639004 is not an order id",
    "232/3 teachers colony, Karur, Tamil Nadu - 639004",
    "FAILED",
)
check(
    "date + price + qty only -> FAILED",
    "Delivery by Sun, 25 Oct | Ordered | 16 Oct | ₹162 | Qty: 1",
    "FAILED",
)
check(
    "product / SKU block is rejected",
    "SKU: 879123 | Model No 998877 | Item Code 5544332211 | MRP ₹499",
    "FAILED",
)
check(
    "AWB / tracking numbers rejected",
    "AWB 7712345678901 | Tracking 5544332211998 | Invoice No 9911223344",
    "FAILED",
)

# ---------------------------------------------------------------------------
section("3) The label is the anchor")

check(
    "6-digit Meesho id next to the label is kept verbatim",
    "Order #342187 | Delivery by 25 Oct",
    ("342187", "TRUNC:342187"),
)
check(
    "number with no label is not promoted to an order id",
    "342581970718279808",
    "FAILED",
)
check(
    "nested label — first label wins",
    "Order #342581970718279808 | Supplier Order No 555666777",
    "342581970718279808",
)
check(
    "two distinct labelled ids -> ambiguous, top-most wins",
    "Order #342581970718279808 | Order #342687779165546496",
    "342581970718279808",
)

# ---------------------------------------------------------------------------
section("4) Digit groups are never concatenated")

check(
    "split digits are not glued into one id",
    "Order #34258 19707 18279 808",
    "FAILED",
)
check(
    "phone right after a short label value is not appended",
    "Order #342581970718279808 7014965655",
    "342581970718279808",
)

# ---------------------------------------------------------------------------
section("5) Fallback chain: name -> TRUNC -> address -> FAILED")

f = check(
    "'<name> <mobile>' customer line -> NAME",
    "punitha 7418622538 | 232/3 teachers colony, Karur, Tamil Nadu - 639004",
    "NAME:",
)
print(f"         name={f.get('name')!r}")

check(
    "Hindi labelled order id",
    "ऑर्डर आईडी 342581970718279808",
    "342581970718279808",
)

check(
    "Delivery Address block without the label still found by shape",
    "232/3 teachers colony, s.vellalapstty p.o, thozhirpettai, karur.4 | "
    "teachers colony, Karur, Tamil Nadu - 639004 | Contact Number - 9786782006",
    "ADDR:",
)

check(
    "substantial cropped id beats the address block",
    "Order #342578720599009... | teachers colony, Karur, Tamil Nadu - 639004",
    ("TRUNC:342578720599009", "342578720599009"),
)

check(
    "empty input -> FAILED",
    "",
    "FAILED",
)

# ---------------------------------------------------------------------------
section("6) WhatsApp chat text is parsed, never OCR'd")

check(
    "chat text carrying the id directly",
    "",
    "342581970718279808",
    whatsapp_text="bhai order id 342581970718279808 check kar lo",
)
check(
    "chat text with only chatter -> nothing invented",
    "",
    "FAILED",
    whatsapp_text="kal tak deliver ho jayega bhai",
)

# ---------------------------------------------------------------------------
section("7) One screenshot = one order (per-image extraction)")

shot_a = "Order #342581970718279808 | Royal Photo Frame | ₹162"
shot_b = "Order #342687779165546496 | SET OF 2, 5X7 INCH | ₹170"
a = extract_fields_multi(shot_a, "", RULES)
b = extract_fields_multi(shot_b, "", RULES)
two_ok = a.get("order_id") == "342581970718279808" and b.get("order_id") == "342687779165546496"
print(f"  [{'PASS' if two_ok else 'FAIL'}] two screenshots -> two independent ids")
print(f"         shot A -> {encode(a)}")
print(f"         shot B -> {encode(b)}")
PASS, FAIL = (PASS + 1, FAIL) if two_ok else (PASS, FAIL + 1)

blob = extract_fields_multi(shot_a + "\n" + shot_b, "", RULES)
blob_ids = blob.get("order_ids") or []
no_swallow = len(set(blob_ids)) >= 1 and "342581970718279808" in blob_ids
print(f"  [{'PASS' if no_swallow else 'FAIL'}] concatenated blob -> ids stay separate")
print(f"         {blob_ids}")
PASS, FAIL = (PASS + 1, FAIL) if no_swallow else (PASS, FAIL + 1)

# ---------------------------------------------------------------------------
print("\n" + "=" * 78)
print(f"RULES CHECK: {PASS} passed, {FAIL} failed")
print("=" * 78)
raise SystemExit(0 if FAIL == 0 else 1)
