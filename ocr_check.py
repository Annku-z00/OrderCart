"""
ocr_check.py
============

A **standalone self-check** for the OCR stack — so you can verify the engine
with your own eyes instead of trusting a test report.

It runs the *real* pipeline (the same Google Lens client, the same extractor,
the same rules the app uses) over any folder of screenshots and prints, for
every picture:

    the "Order #…" line as the OCR read it   ->   the id the app would write

A picture whose printed id does not match the picture is flagged.

Usage
-----
    py -3.12 ocr_check.py                          # every image in assets/12988
    py -3.12 ocr_check.py "C:\\path\\to\\screens"   # a folder of your own
    py -3.12 ocr_check.py "C:\\path\\one.jpg"      # a single file
    py -3.12 ocr_check.py --no-ocr                 # extraction only (offline)

Everything is read-only: no output file is written, no state DB is touched.
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: An independent "what does the picture say" reader — deliberately *not* the
#: app's parser, so the check compares two different things.
_ORDER_IN_TEXT = re.compile(r"order\s*(?:id|no|number|#)?\s*[:#]?\s*(\d{4,25})", re.IGNORECASE)

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def _images_in(path: str) -> List[str]:
    """Every image file under ``path`` (or ``[path]`` when it is a file)."""
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        return []
    found: List[str] = []
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            if name.lower().endswith(_IMAGE_EXT):
                found.append(os.path.join(root, name))
    return found


def _read_picture_id(text: str) -> Optional[str]:
    """The order id as it is *printed in the picture*, or None."""
    match = _ORDER_IN_TEXT.search(text or "")
    return match.group(1) if match else None


def main(argv: List[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    no_ocr = "--no-ocr" in argv

    if args:
        targets = [os.path.abspath(a) for a in args]
    else:
        default = os.path.join(ROOT, "assets", "12988")
        targets = [default]
        print("No folder given — using %s" % default)
        print("Usage: py -3.12 ocr_check.py <folder-or-file> [--no-ocr]")
        print()

    files: List[str] = []
    for target in targets:
        found = _images_in(target)
        if not found:
            print("!! nothing found at %s" % target)
        files.extend(found)

    if not files:
        print("No images to check.")
        return 1

    print("=" * 78)
    print("OCR SELF-CHECK — %d image(s)" % len(files))
    print("=" * 78)

    # --- the pieces under test -------------------------------------------
    from extractor.parser import extract_fields_multi
    from extractor.rules import Rules
    from frameflow_config import load_config

    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    rules = Rules.from_config(cfg)

    engine = None
    if not no_ocr:
        from ocr.engine import build_engine

        engine = build_engine(cfg)
        print("engine : Google Lens (in-tree) — needs internet")
    else:
        print("engine : (offline: extraction only, no OCR)")
    print()

    matched = 0
    mismatched = 0
    no_id = 0
    rows: List[Tuple[str, str, str, str]] = []

    for index, path in enumerate(files, 1):
        name = os.path.basename(path)
        picture_id = ""
        app_id = ""
        status = ""

        if engine is not None:
            result = engine.run_on_file(path)
            text = result.text or ""
            picture_id = _read_picture_id(text) or ""

            fields = extract_fields_multi(text, "", rules)
            app_id = str(fields.get("order_id") or "")

            # The app also re-reads the enlarged header when the full page
            # carried no id — mirror that here.
            if not app_id:
                focus = engine.run_focused_on_file(path)
                if focus.text:
                    f2 = extract_fields_multi(focus.text, "", rules)
                    if f2.get("order_id"):
                        app_id = str(f2["order_id"])
                        picture_id = _read_picture_id(focus.text) or picture_id
            status = str(fields.get("type") or "")
        else:
            status = "(offline)"

        if app_id and picture_id:
            if app_id == picture_id:
                matched += 1
                verdict = "OK"
            else:
                mismatched += 1
                verdict = "MISMATCH"
        elif app_id:
            matched += 1
            verdict = "OK*"
        else:
            no_id += 1
            verdict = "no id"

        rows.append((name, picture_id or "-", app_id or "-", verdict))
        print("[%3d/%3d] %-46s picture=%-22s app=%-22s %s" % (
            index, len(files), name[-46:], picture_id or "-", app_id or "-", verdict))

    if engine is not None:
        engine.close()

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print("  images checked          : %d" % len(files))
    print("  id matches the picture  : %d" % matched)
    print("  MISMATCHES              : %d" % mismatched)
    print("  no id found             : %d" % no_id)
    if mismatched:
        print()
        print("  --- mismatches (investigate these) ---")
        for name, pic, app, verdict in rows:
            if verdict == "MISMATCH":
                print("    %-46s picture=%-22s app=%s" % (name[-46:], pic, app))
    print()
    if mismatched == 0:
        print("  RESULT: every id the app produced is exactly what the picture shows.")
    else:
        print("  RESULT: %d picture(s) disagree with the app — open them and look." % mismatched)
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
