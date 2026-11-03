"""Label generation for FrameFlow (shipping labels).

``labels.generator`` turns the order rows FrameFlow already extracted into a
printable PDF: **one 4x6 inch label per page**, each carrying the order id
(with a Code128 barcode), the customer name/address/phone and the seller's
return address.

The module is intentionally free of GUI and config imports so it can be tested
on its own.  ``main_web.Api.generate_labels`` is the bridge method the
Labels view calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib import colors
from reportlab.lib.pagesizes import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Tunables / defaults
# ---------------------------------------------------------------------------

#: A Meesho-style thermal label.
DEFAULT_WIDTH_IN = 4.0
DEFAULT_HEIGHT_IN = 6.0

#: Phone: an Indian mobile (10 digits, 6-9 first).  Kept permissive enough to
#: find it inside a free-form address block.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s-]?)?([6-9](?:[\s-]?\d){9})(?!\d)"
)
#: A standalone six-digit PIN.  The last match wins (addresses usually end with
#: the PIN, while an earlier six-digit number is often a house/plot number).
_PIN_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


@dataclass
class LabelOptions:
    """Everything the label template needs besides the order itself."""

    brand: str = "MEESHO"
    seller_name: str = "VEER HANUMAN CRAFTS"
    seller_address: str = ""
    seller_phone: str = ""
    seller_gstin: str = ""
    footer: str = "Thank you for shopping with us!"
    show_barcode: bool = True
    copies: int = 1
    width_in: float = DEFAULT_WIDTH_IN
    height_in: float = DEFAULT_HEIGHT_IN
    margin: float = 14.0


@dataclass
class LabelRow:
    """One order reduced to the fields printed on its label."""

    internal_id: str
    order_id: str
    name: str
    address: str
    phone: str = ""
    pin: str = ""
    date: str = ""


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def as_options(options: Any = None) -> LabelOptions:
    """Coerce a dict (or an existing :class:`LabelOptions`) into options."""
    if isinstance(options, LabelOptions):
        return options
    data: Dict[str, Any] = options if isinstance(options, dict) else {}
    defaults = LabelOptions()
    return LabelOptions(
        brand=_text(data.get("brand")) or defaults.brand,
        seller_name=(
            _text(data.get("seller_name"))
            if "seller_name" in data else defaults.seller_name
        ),
        seller_address=_text(data.get("seller_address")),
        seller_phone=_text(data.get("seller_phone")),
        seller_gstin=_text(data.get("seller_gstin")),
        footer=(
            _text(data.get("footer"))
            if "footer" in data else defaults.footer
        ),
        show_barcode=_as_bool(data.get("show_barcode"), True),
        copies=_as_int(data.get("copies"), 1, 1, 50),
        width_in=_as_float(data.get("width_in"), DEFAULT_WIDTH_IN, 2.0, 10.0),
        height_in=_as_float(data.get("height_in"), DEFAULT_HEIGHT_IN, 2.0, 10.0),
    )


def _usable_order_id(value: Any) -> str:
    """The id to print/barcode, or ``""`` when there is nothing usable.

    ``TRUNC:…`` is a crop of a longer id, so it is *not* a printable order id.
    ``FAILED`` / empty rows are skipped for the same reason.
    """
    text = _text(value)
    if not text:
        return ""
    upper = text.upper()
    if upper.startswith("TRUNC:"):
        return ""
    if upper in {"FAILED", "MISSING", "NONE", "N/A", "NA", "-", "—"}:
        return ""
    return text


def extract_phone_pin(address: Any) -> Tuple[str, str]:
    """Pull the customer phone and PIN out of a free-form address block.

    Returns ``(phone, pin)`` — either may be ``""``.  The address itself is not
    modified: the label still prints every line the OCR found.
    """
    text = _text(address)
    phone_match = _PHONE_RE.search(text)
    pin_matches = _PIN_RE.findall(text)
    phone = re.sub(r"\D", "", phone_match.group(1)) if phone_match else ""
    pin = pin_matches[-1] if pin_matches else ""
    return phone, pin


def rows_to_labels(
    rows: Iterable[Dict[str, Any]],
    options: Any = None,
) -> Tuple[List[LabelRow], int, int]:
    """Normalise order rows into label rows.

    Returns ``(labels, skipped, incomplete)``:

    * ``skipped`` — rows with no usable order id (``TRUNC:`` / ``FAILED`` /
      empty), or a non-dict entry;
    * ``incomplete`` — unique order ids that have **no customer address** in
      any of their rows.  A shipping label without an address is not usable,
      so those rows are not printed; set ``extraction.collect: all`` and
      reprocess the chats to fill the name/address columns.

    Duplicate rows for the same ``(internal_id, order_id)`` are merged: a
    later row that carries an address/name fills in an earlier one that does
    not.  That is exactly what a "single → all" reprocess produces (the old
    id-only row plus the new complete row).
    """
    candidates: Dict[Tuple[str, str], Dict[str, str]] = {}
    skipped = 0

    for row in rows or []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        order_id = _usable_order_id(row.get("order_id"))
        if not order_id:
            skipped += 1
            continue
        if _text(row.get("status")).upper() in ("FAILED", "TRUNC", "MISSING"):
            skipped += 1
            continue

        internal_id = _text(row.get("internal_id"))
        key = (internal_id, order_id)
        candidate = {
            "internal_id": internal_id,
            "order_id": order_id,
            "name": _text(row.get("name")),
            "address": _text(row.get("address") or row.get("raw_address")),
            "timestamp": _text(row.get("timestamp")),
        }
        existing = candidates.get(key)
        if existing is None:
            candidates[key] = candidate
        else:
            # Prefer the row that actually carries the delivery details.
            for field in ("address", "name", "timestamp"):
                if not existing.get(field) and candidate.get(field):
                    existing[field] = candidate[field]

    labels: List[LabelRow] = []
    incomplete = 0
    for candidate in candidates.values():
        address = candidate["address"]
        if not address:
            incomplete += 1
            continue
        phone, pin = extract_phone_pin(address)
        timestamp = candidate["timestamp"]
        date = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
        labels.append(
            LabelRow(
                internal_id=candidate["internal_id"],
                order_id=candidate["order_id"],
                name=candidate["name"] or "Customer",
                address=address,
                phone=phone,
                pin=pin,
                date=date,
            )
        )

    return labels, skipped, incomplete


# ---------------------------------------------------------------------------
# Text layout helpers
# ---------------------------------------------------------------------------

def _string_width(text: str, font: str, size: float) -> float:
    try:
        return pdfmetrics.stringWidth(text, font, size)
    except Exception:  # pragma: no cover - a missing font falls back gracefully
        return len(text) * size * 0.5


def _split_long_word(word: str, font: str, size: float, max_width: float) -> List[str]:
    """Split a single over-long token (a long address line, a URL, ...)."""
    chunks: List[str] = []
    current = ""
    for char in word:
        trial = current + char
        if _string_width(trial, font, size) <= max_width:
            current = trial
        else:
            if current:
                chunks.append(current)
            current = char
    if current:
        chunks.append(current)
    return chunks or [word]


def wrap_text(text: Any, font: str, size: float, max_width: float) -> List[str]:
    """Wrap ``text`` to ``max_width`` points, honouring ``|`` line separators."""
    source = _text(text).replace("\r\n", "\n").replace("\r", "\n")
    if not source:
        return []
    # CSV keeps an address on one line with "|" between original newlines.
    paragraphs = [part.strip() for part in source.replace("|", "\n").split("\n")]
    lines: List[str] = []

    for paragraph in paragraphs:
        if not paragraph:
            continue
        words = paragraph.split()
        line = ""
        for word in words:
            trial = (line + " " + word).strip()
            if _string_width(trial, font, size) <= max_width:
                line = trial
                continue
            if line:
                lines.append(line)
            if _string_width(word, font, size) > max_width:
                pieces = _split_long_word(word, font, size, max_width)
                lines.extend(pieces[:-1])
                line = pieces[-1]
            else:
                line = word
        if line:
            lines.append(line)
    return lines


def _is_phone_only_line(line: str, phone: str) -> bool:
    """True when an address line is just the phone number we print separately."""
    if not phone:
        return False
    digits = re.sub(r"\D", "", line)
    return digits == phone or (
        len(digits) == 12 and digits.startswith("91") and digits.endswith(phone)
    )


def _short(text: Any, limit: int) -> str:
    value = _text(text)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Barcode
# ---------------------------------------------------------------------------

def make_barcode(order_id: str, max_width: float, height: float = 52.0) -> Optional[Code128]:
    """Build a Code128 barcode that fits ``max_width`` points, or ``None``."""
    value = "".join(ch for ch in _text(order_id) if 32 <= ord(ch) <= 126)
    if not value:
        return None
    try:
        barcode = Code128(value, barHeight=height, barWidth=1.6,
                          humanReadable=False)
        if barcode.width > max_width and barcode.width > 0:
            scaled = max(0.35, 1.6 * max_width / barcode.width)
            barcode = Code128(value, barHeight=height, barWidth=scaled,
                              humanReadable=False)
        return barcode
    except Exception:  # pragma: no cover - malformed value / barcode limits
        return None


# ---------------------------------------------------------------------------
# Label drawing
# ---------------------------------------------------------------------------

def _seller_lines(options: LabelOptions, max_width: float) -> List[Tuple[str, str, float]]:
    """The seller block as ``(text, font, size)`` lines."""
    lines: List[Tuple[str, str, float]] = []
    if options.seller_name:
        for line in wrap_text(options.seller_name, "Helvetica-Bold", 8.5, max_width)[:2]:
            lines.append((line, "Helvetica-Bold", 8.5))
    if options.seller_address:
        for line in wrap_text(options.seller_address, "Helvetica", 7.2, max_width)[:3]:
            lines.append((line, "Helvetica", 7.2))
    contact: List[str] = []
    if options.seller_phone:
        contact.append("Ph: " + options.seller_phone)
    if options.seller_gstin:
        contact.append("GSTIN: " + options.seller_gstin)
    if contact:
        for line in wrap_text("  ·  ".join(contact), "Helvetica", 7.2, max_width)[:1]:
            lines.append((line, "Helvetica", 7.2))
    return lines


def _draw_label(c: canvas.Canvas, row: LabelRow, options: LabelOptions) -> None:
    """Draw one label at the current page origin, without ``showPage()``."""
    width = options.width_in * inch
    height = options.height_in * inch
    margin = options.margin
    content_width = width - 2 * margin

    c.saveState()
    c.setTitle("FrameFlow shipping label")
    c.setAuthor("FrameFlow")

    # Cut border — a real 4x6 label ends at the paper edge; the hairline makes
    # the safe area visible on a sheet of labels.
    c.setStrokeColor(colors.HexColor("#C9C9C9"))
    c.setLineWidth(0.7)
    c.rect(0.6, 0.6, width - 1.2, height - 1.2, stroke=1, fill=0)

    # ---- header: brand + order id -------------------------------------
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(margin, height - margin - 16, _short(options.brand or "MEESHO", 22))

    c.setFillColor(colors.HexColor("#555555"))
    c.setFont("Helvetica", 6.5)
    c.drawRightString(width - margin, height - margin - 4, "ORDER ID")
    c.setFillColor(colors.black)
    c.setFont("Courier-Bold", 10.5)
    c.drawRightString(width - margin, height - margin - 17, _short(row.order_id, 28))

    y = height - margin - 26
    c.setStrokeColor(colors.HexColor("#C9C9C9"))
    c.setLineWidth(0.6)
    c.line(margin, y, width - margin, y)
    y -= 8

    # ---- barcode ------------------------------------------------------
    if options.show_barcode:
        barcode = make_barcode(row.order_id, content_width, 50)
        if barcode is not None:
            barcode.drawOn(c, (width - barcode.width) / 2.0, y - 50)
            y -= 62
            c.setFont("Courier", 8)
            c.setFillColor(colors.black)
            c.drawCentredString(width / 2.0, y, _short(row.order_id, 34))
            y -= 12

    # ---- seller block (bottom) ---------------------------------------
    seller_lines = _seller_lines(options, content_width)
    footer_height = 18.0
    seller_height = 10.0 + sum(size + 2.0 for _, _, size in seller_lines)
    seller_top = margin + footer_height + seller_height

    c.setStrokeColor(colors.HexColor("#C9C9C9"))
    c.setLineWidth(0.6)
    c.line(margin, seller_top + 5, width - margin, seller_top + 5)

    sy = seller_top
    c.setFillColor(colors.HexColor("#555555"))
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(margin, sy, "SOLD BY")
    sy -= 10
    for text_line, font, size in seller_lines:
        c.setFillColor(colors.black)
        c.setFont(font, size)
        c.drawString(margin, sy, text_line)
        sy -= size + 2.0

    # ---- ship-to block (between barcode and seller) ------------------
    ship_bottom = seller_top + 11
    c.setFillColor(colors.HexColor("#555555"))
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(margin, y, "SHIP TO")
    y -= 14

    c.setFillColor(colors.black)
    name_lines = wrap_text(row.name, "Helvetica-Bold", 10.5, content_width)[:2]
    for line in name_lines:
        if y < ship_bottom:
            break
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(margin, y, line)
        y -= 12.5

    address_lines = [
        line
        for line in wrap_text(row.address, "Helvetica", 8.3, content_width)
        if not _is_phone_only_line(line, row.phone)
    ]
    for line in address_lines:
        if y < ship_bottom:
            c.setFont("Helvetica-Bold", 8.3)
            c.drawString(margin, y, "…")
            break
        c.setFont("Helvetica", 8.3)
        c.drawString(margin, y, line)
        y -= 10.2

    if row.phone and y >= ship_bottom:
        c.setFont("Helvetica-Bold", 8.3)
        c.drawString(margin, y, "Phone: " + row.phone)
        y -= 10.2

    # A small reference block uses some of the free space below the address;
    # it helps the seller match a printed label back to its chat.
    if y >= ship_bottom + 34:
        y -= 8
        c.setFillColor(colors.HexColor("#555555"))
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(margin, y, "ORDER REFERENCE")
        y -= 12
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 7.5)
        if row.internal_id:
            c.drawString(margin, y, "Internal ID: " + _short(row.internal_id, 24))
            y -= 9
        c.drawString(margin, y, "Order date: " + _short(row.date, 12))

    # ---- footer -------------------------------------------------------
    c.setFillColor(colors.HexColor("#555555"))
    c.setFont("Helvetica", 6)
    if row.internal_id:
        c.drawString(margin, margin, "Ref: " + _short(row.internal_id, 24))
    c.drawRightString(width - margin, margin, _short(row.date, 12))

    if options.footer:
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.HexColor("#777777"))
        c.drawCentredString(width / 2.0, margin + 7.5, _short(options.footer, 64))

    c.restoreState()


def generate_labels_pdf(
    rows: Iterable[Dict[str, Any]],
    path: str,
    options: Any = None,
) -> Dict[str, Any]:
    """Render ``rows`` into ``path`` and return a small summary.

    Raises :class:`ValueError` when there is nothing printable (no usable
    order id at all), so the bridge can show a clear message instead of writing
    an empty PDF.
    """
    opts = as_options(options)
    labels, skipped, incomplete = rows_to_labels(rows, opts)
    if not labels:
        if incomplete and skipped:
            raise ValueError(
                "no rows with both a usable Order ID and a customer address — "
                "set 'Collect per row' to 'All' and reprocess the chats"
            )
        if incomplete:
            raise ValueError(
                "no rows with a customer address — set 'Collect per row' to "
                "'All' in Settings, tick Reprocess, and run the chats again"
            )
        raise ValueError("no rows with a usable Order ID — nothing to print")

    page_size = (opts.width_in * inch, opts.height_in * inch)
    document = canvas.Canvas(path, pagesize=page_size)
    document.setTitle("FrameFlow shipping labels")
    pages = 0
    for label in labels:
        for _ in range(opts.copies):
            _draw_label(document, label, opts)
            document.showPage()
            pages += 1
    document.save()

    return {
        "path": path,
        "labels": len(labels),
        "pages": pages,
        "skipped": skipped,
        "incomplete": incomplete,
    }
