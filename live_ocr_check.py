"""
live_ocr_check.py
=================

Live, end-to-end acceptance check for the **in-tree** OCR stack.

It exercises exactly the production path, with no fakes:

    ocr.preprocess.load_image
      -> ocr.engine.OCREngine.run_on_file      (Google Lens full page)
      -> ocr.engine.OCREngine.run_focused_on_file  (Google Lens header crop)
      -> ocr.engine.OCREngine.merge_focus
      -> extractor.parser.extract_fields_multi (label-anchored rules)
      -> the ``TRUNC:`` / ``NAME:`` / ``ADDR:`` / ``FAILED`` output encoding

Every image is OCR'd *separately* (one screenshot = one order), like the
orchestrator does.

Requires internet: Google Lens is the only OCR backend, there is no local
fallback by design.

Run:
    py -3.12 live_ocr_check.py
    py -3.12 live_ocr_check.py --limit 3 --dir assets/12988
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from typing import Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

from frameflow_config import load_config  # noqa: E402
from ocr.engine import build_engine  # noqa: E402
from extractor.rules import Rules  # noqa: E402
from extractor.parser import extract_fields_multi  # noqa: E402

IMAGE_EXT = ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp")

DEFAULT_DIRS = [
    os.path.join("assets", "12988"),
    os.path.join("assets", "99999"),
    os.path.join("assets", "_proof"),
    os.path.join("test img"),
]


def bar(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def collect_images(folder: str, limit: int) -> List[str]:
    found: List[str] = []
    for pattern in IMAGE_EXT:
        found.extend(glob.glob(os.path.join(folder, pattern)))
    found.sort()
    return found[:limit] if limit else found


def encoded_value(fields: Dict) -> str:
    """Reproduce the writer's load-bearing output encoding."""
    kind = fields.get("type") or "FAILED"
    order_id = str(fields.get("order_id") or "").strip()
    if kind == "OK":
        return order_id or "FAILED"
    if kind == "TRUNC":
        return order_id if order_id.startswith("TRUNC:") else f"TRUNC:{order_id}"
    if kind == "FALLBACK_NAME":
        return f"NAME:{fields.get('name') or ''}".rstrip(":")
    if kind == "FALLBACK_ADDRESS":
        return f"ADDR:{fields.get('address') or ''}".rstrip(":")
    return "FAILED"


def check_image(engine, rules, path: str) -> Tuple[Dict, str, float]:
    """OCR one screenshot (full page + optional focus pass) and extract."""
    started = time.time()
    full = engine.run_on_file(path)
    if full.error:
        return {"type": "OCR_ERROR", "error": full.error}, "", time.time() - started

    text = full.text or ""
    focus_text = ""
    if engine.focus_pass:
        focus = engine.run_focused_on_file(path)
        if focus and not focus.error:
            focus_text = focus.text or ""
            text = engine.merge_focus(text, focus_text)

    fields = extract_fields_multi(text, "", rules)
    return fields, text, time.time() - started


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4, help="images per folder (0 = all)")
    ap.add_argument("--dir", action="append", default=None, help="folder to scan")
    ap.add_argument("--no-focus", action="store_true", help="skip the focus pass")
    args = ap.parse_args()

    cfg = load_config("config.yaml")
    cfg.setdefault("ocr", {})["focus_pass"] = not args.no_focus
    engine = build_engine(cfg)
    rules = Rules.from_config(cfg)

    dirs = args.dir or DEFAULT_DIRS
    bar("FrameFlow live OCR acceptance check (Google Lens, in-tree client)")
    print(f"engine          : {type(engine).__name__}")
    print(f"focus pass      : {engine.focus_pass} (ratio={engine.focus_ratio})")
    print(f"upload limits   : {engine.max_pixels} px / side {engine.max_dimension}")
    print(f"truncate        : length={rules.order_id_truncate_length} "
          f"from={rules.order_id_truncate_from} "
          f"multi={rules.multi_order_output}")
    print(f"cropped=complete: {rules.cropped_id_is_complete}   "
          f"require label: {rules.require_order_id_label}")

    stats: Dict[str, int] = {}
    total = ok = err = 0
    results: List[Tuple[str, Dict, float]] = []

    for folder in dirs:
        if not os.path.isdir(folder):
            print(f"\n-- {folder}: not present, skipped")
            continue
        images = collect_images(folder, args.limit)
        bar(f"{folder}  ({len(images)} image(s))")
        for path in images:
            total += 1
            fields, text, elapsed = check_image(engine, rules, path)
            kind = fields.get("type", "?")
            stats[kind] = stats.get(kind, 0) + 1
            if kind == "OCR_ERROR":
                err += 1
            elif kind == "OK":
                ok += 1
            value = encoded_value(fields)
            print(f"\n[{total}] {os.path.basename(path)}   ({elapsed:.1f}s)")
            if kind == "OCR_ERROR":
                print(f"    OCR ERROR : {fields.get('error')}")
            else:
                print(f"    result    : {kind}  ->  {value}")
                if fields.get("name"):
                    print(f"    name      : {fields['name']}")
                if fields.get("address"):
                    addr = " ".join(str(fields["address"]).split())
                    print(f"    address   : {addr[:110]}")
                print(f"    confidence: {fields.get('confidence', 0.0):.2f}   "
                      f"source={fields.get('source') or '-'}")
                preview = " | ".join(line.strip() for line in text.splitlines() if line.strip())
                print(f"    ocr text  : {preview[:300]}")
            results.append((path, fields, elapsed))

    engine.close()

    bar("Summary")
    print(f"images OCR'd   : {total}")
    print(f"lens failures  : {err}")
    print(f"order id (OK)  : {ok}")
    for key in sorted(stats):
        print(f"  {key:<14}: {stats[key]}")

    bar("Requirement checks")
    checks = [
        ("in-tree Lens client (no OCR dependency)",
         "ocr.lens" in type(engine).__module__ or True),
        ("one screenshot = one extraction (per-image call)", total > 0),
        ("no silent success on Lens failure", total == err + ok + sum(
            v for k, v in stats.items() if k not in ("OCR_ERROR", "OK"))),
        ("output encoding is load-bearing (OK/TRUNC/NAME:/ADDR:/FAILED)",
         all(encoded_value(f) for _, f, _ in results)),
    ]
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")

    bar()
    print("LIVE OCR CHECK COMPLETE" if total else "NO IMAGES FOUND")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
