"""
frameflow_config.py
===================

Small helper for loading ``config.yaml`` with sane defaults and for resolving
project-relative folders (logs/, review/, failed/, assets/).

Kept as a standalone module so both the CLI and the GUI (and the tests) can use
it without importing the heavy OCR stack.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a hard dependency
    yaml = None  # type: ignore

try:  # optional: keeps the comments in config.yaml when the app saves it
    from ruamel.yaml import YAML as _RoundTripYAML
except Exception:  # pragma: no cover - ruamel is an optional nicety
    _RoundTripYAML = None  # type: ignore


DEFAULT_CONFIG_PATH = "config.yaml"

# Folder names used across the app (kept in one place).
LOGS_DIR = "logs"
REVIEW_DIR = "review"
FAILED_DIR = "failed"
ASSETS_DIR = "assets"
STATE_DIR = "state"

DEFAULTS: Dict[str, Any] = {
    "whatsapp_api": {
        "provider": "waha",
        "base_url": "http://localhost:3000",
        "api_key": "",
        "session_name": "default",
        # The WhatsApp accounts to collect from — each one is a WAHA session.
        # ``[{"name": "default", "label": "Personal", "enabled": true}]``
        "accounts": [],
    },
    "timeouts": {"api_call": 30, "media_download": 60},
    # OCR is Google Lens, implemented in-tree (ocr/proto.py + ocr/lens.py):
    # a hand-written protobuf codec and one POST — no OCR dependency at all.
    # ``lens_retries`` is the number of extra attempts after the first
    # failure; ``lens_retry_backoff`` is the pause (seconds) between attempts.
    "ocr": {"upscale_factor": 1, "lens_timeout": 15,
            "lens_retries": 2, "lens_retry_backoff": 0.5,
            "jpeg_quality": 90, "max_pixels": 1500000, "max_dimension": 1600,
            "focus_pass": True, "focus_ratio": 0.45,
            # Reuse an earlier read of the same pixels (see ocr/cache.py).
            "cache": True, "cache_dir": ""},
    "extraction": {
        "order_id_regex_primary": r"(?<!\d)(\d{15,18})(?:_\d+)?(?!\d)",
        "order_id_regex_generic": r"\b\d{6,20}\b",
        "pin_code_regex": r"\b\d{6}\b",
        "phone_regex": r"\b[6-9]\d{9}\b",
        "order_id_labels": [
            "Sub OrderID", "Sub Order ID", "Sub-Order No", "Sub Order No",
            "Order ID", "Order Id", "Order No", "Order Number", "OrderId",
            "Order #", "Order#", "Order I'd", "Order I’d", "Order:",
            "ऑर्डर आईडी", "ऑर्डर संख्या", "ऑर्डर नंबर", "ऑर्डर नं", "ऑर्डर #",
            "ऑर्डर:", "आर्डर आईडी", "आर्डर",
            "ஆர்டர் ஐடி", "ஆர்டர் எண்", "ஆர்டர் #", "ஆர்டர்:",
            "ఆర్డర్ ఐడి", "ఆర్డర్ ఐడీ", "ఆర్డర్ నంబర్", "ఆర్డర్ #", "ఆర్డర్:",
            "অর্ডার আইডি", "অর্ডার নম্বর", "অর্ডার নং", "অর্ডার #", "অর্ডার:",
        ],
        "name_labels": ["Customer Name", "Name", "Ship to", "Deliver to", "Receiver",
                        "Sold to", "Delivery Address", "Customer",
                        "ग्राहक", "பெயர்", "పేరు", "নাম"],
        "address_labels": ["Address", "Shipping Address", "Deliver to", "Delivery Address",
                           "पता", "முகவரி", "చిరునామా", "ঠিকানা",
                           # Full "Delivery Address" phrases first (first match wins).
                           "डिलीवरी का पता", "डिलीवरी पता", "शिपिंग पता",
                           "டெலிவரி முகவரி", "விநியோக முகவரி",
                           "డెలివరీ అడ్రసు", "డెలివరీ చిరునామా",
                           "ডেলিভারি ঠিকানা", "ডেলিভারি অ্যাড্রেস"],
        "ignore_words": ["Meesho", "Flipkart", "Amazon", "Invoice", "Order", "Address",
                         "Change", "Supplier"],
        "non_order_labels": ["SKU", "Model No", "Model", "Style",
                             "Item No", "Item Code", "Product Code", "Product ID",
                             "Qty", "Quantity", "Size", "MRP", "AWB", "Tracking",
                             "Invoice No", "GST", "HSN"],
        "eta_labels": ["ETA", "Expected", "Arriving", "Ship by", "Delivery"],
        # §3 output shape.  These defaults mirror the shipped ``config.yaml``:
        # the order id is written exactly as it appears (no short window),
        # every order of a chat becomes its own row, and a cropped id counts
        # as a complete id.  A missing/edited config.yaml must not silently
        # switch the app back to the old 6-digit workflow.
        "order_id_truncate_length": 0,
        "order_id_truncate_from": "first",
        "multi_order_output": True,
        # A cut-off order id with at least this many digits is a real order
        # key and beats the name / address fallback.
        "truncated_id_min_digits": 6,
        # A cropped order id ("Order #342581") counts as a complete id.
        "cropped_id_is_complete": True,
        # What a row collects: "single" (the order id, else the name, else
        # the address — one value per row) or "all" (the order id *and* the
        # name *and* the address in the same row).
        "collect": "single",
        # §3.2 unlabelled Meesho id band and the §3.6 list-page cap.
        "min_unlabelled_id_digits": 16,
        "max_unlabelled_id_digits": 18,
        "list_page_max_candidates": 3,
        # §3.1 label-first extraction: every order id carries a label, so the
        # unlabelled fallback is off; a 6–15 digit labelled value is a crop.
        "require_order_id_label": True,
        "full_id_min_digits": 16,
        "max_labelled_id_digits": 18,
        "partial_id_prefix": True,
        # §3.4 per-filter toggles (all on by default).
        "reject_patterns": {
            "phone": True, "pin": True, "price": True, "date": True,
            "product": True, "eta": True, "concatenation": True,
        },
        "text_message_limit": 50,
        "address_max_chars": 400,
        "truncated_min_digits": 1,
        "truncated_max_digits": 20,
    },
    "output": {
        "default_file": "orders.csv",
        "date_format": "%Y-%m-%d %H:%M:%S",
        # "append" writes id,value rows at the end.  "fill" updates an existing
        # Notepad++ master list in place (12328 -> 12328<TAB>342...).
        "notepad_mode": "append",
    },
    # Shipping labels (the Labels view).  A 4x6 inch thermal label per order,
    # with a Code128 barcode of the Order ID.  Seller details are printed in
    # the "Sold By" block; copies is the number of labels per order.
    "labels": {
        "brand": "MEESHO",
        "seller_name": "VEER HANUMAN CRAFTS",
        "seller_address": "",
        "seller_phone": "",
        "seller_gstin": "",
        "footer": "Thank you for shopping with us!",
        "show_barcode": True,
        "copies": 1,
        "width_in": 4,
        "height_in": 6,
    },
    "processing": {"delay_between_chats": 0.5, "max_retries": 3, "retry_backoff": 2.0,
                   "max_messages_per_chat": 50, "max_images_per_chat": 0},
    # Online update check (updater.py).  The app compares its own commit with
    # the newest commit on this branch of the public GitHub repo and shows a
    # banner when they differ.  "Update now" only runs ``git pull --ff-only``,
    # so it is offered for a clone (source run), not for a built copy.
    "update": {
        "enabled": True,
        "repo": "Annku-z00/OrderCart",
        "branch": "main",
        "check_on_start": True,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str = DEFAULT_CONFIG_PATH, create_if_missing: bool = False) -> Dict[str, Any]:
    """Load ``config.yaml`` merged over :data:`DEFAULTS`.

    A missing or unreadable file never raises — the caller always receives a
    usable configuration dict.
    """
    data: Dict[str, Any] = {}
    if yaml is not None and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if isinstance(loaded, dict):
                data = loaded
        except Exception:  # pragma: no cover - defensive
            data = {}
    elif create_if_missing and yaml is not None and not os.path.isfile(path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(DEFAULTS, fh, sort_keys=False, allow_unicode=True)
        except Exception:  # pragma: no cover
            pass

    return _deep_merge(DEFAULTS, data)


def validate_config(config: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable problems in ``config``.

    A wrong regex or an out-of-range number only used to fail in the middle of
    a long run (or, worse, silently change what is extracted).  The GUI calls
    this right after loading ``config.yaml`` and logs every problem, so a typo
    is visible on the first launch instead of after 40 chats.
    """
    problems: List[str] = []
    extraction = (config or {}).get("extraction") or {}
    ocr = (config or {}).get("ocr") or {}

    for key in (
        "order_id_regex_primary",
        "order_id_regex_generic",
        "pin_code_regex",
        "phone_regex",
    ):
        value = str(extraction.get(key) or "").strip()
        if not value:
            problems.append(f"extraction.{key}: the pattern is empty")
            continue
        try:
            re.compile(value)
        except re.error as exc:
            problems.append(f"extraction.{key}: invalid regex ({exc})")

    collect = str(extraction.get("collect") or "single").strip().lower()
    if collect not in ("single", "all"):
        problems.append(
            f"extraction.collect: {collect!r} is not 'single' or 'all'"
        )

    truncate = extraction.get("order_id_truncate_length", 0)
    try:
        if int(truncate) < 0:
            problems.append("extraction.order_id_truncate_length: must be >= 0")
    except (TypeError, ValueError):
        problems.append("extraction.order_id_truncate_length: not a number")

    for key, minimum in (("lens_timeout", 1), ("lens_retries", 0),
                         ("upscale_factor", 1)):
        try:
            if float(ocr.get(key, minimum)) < minimum:
                problems.append(f"ocr.{key}: must be >= {minimum}")
        except (TypeError, ValueError):
            problems.append(f"ocr.{key}: not a number")

    return problems


def load_round_trip(path: str) -> Any:
    """Load ``config.yaml`` for a comment-preserving rewrite (or ``None``)."""
    if _RoundTripYAML is None or not os.path.isfile(path):
        return None
    try:
        parser = _RoundTripYAML()
        parser.preserve_quotes = True
        with open(path, "r", encoding="utf-8") as fh:
            return parser.load(fh)
    except Exception:  # pragma: no cover - defensive
        return None


def dump_round_trip(path: str, data: Any) -> bool:
    """Write a round-trip config back, comments included.  False on failure."""
    if _RoundTripYAML is None:
        return False
    try:
        parser = _RoundTripYAML()
        parser.preserve_quotes = True
        with open(path, "w", encoding="utf-8") as fh:
            parser.dump(data, fh)
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def ensure_dirs(root: str = ".") -> Dict[str, str]:
    """Create and return the standard working folders."""
    folders = {
        "logs": os.path.join(root, LOGS_DIR),
        "review": os.path.join(root, REVIEW_DIR),
        "failed": os.path.join(root, FAILED_DIR),
        "assets": os.path.join(root, ASSETS_DIR),
        "state": os.path.join(root, STATE_DIR),
    }
    for folder in folders.values():
        os.makedirs(folder, exist_ok=True)
    return folders
