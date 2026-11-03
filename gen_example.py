"""Generate a before/after example of the Notepad master-list filling rules.

Writes two files next to the project so they can be opened in Notepad++:
  example_master_list_BEFORE.txt  - the seller's list (ids only)
  example_master_list_AFTER.txt   - what FrameFlow writes

Every "after" line comes from output.writer.NotepadWriter, using the same
result shapes the orchestrator produces.
"""

from __future__ import annotations

import os
import shutil

from output.writer import NotepadWriter

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
BEFORE = os.path.join(OUT_DIR, "example_master_list_BEFORE.txt")
AFTER = os.path.join(OUT_DIR, "example_master_list_AFTER.txt")

# Ids exactly like the seller's 15-10-2026.txt (Column Editor, one per line).
IDS = [12328, 12329, 12330, 12331, 12332, 12333, 12334, 12335, 12336, 12337,
       12338, 12339, 12340, 12341]

#: Errors the app would print in the terminal during this run.
NOTICES: list[str] = []


def row(value: str, name: str = "", address: str = "", status: str = "OK") -> dict:
    return {"value": value, "status": status, "name": name, "address": address}


def ok(rows: list[dict]) -> dict:
    return {
        "type": "OK",
        "order_ids": [r["value"] for r in rows],
        "order_rows": rows,
    }


# One entry per chat. ``None`` = no such chat (the orchestrator never writes).
SCENARIOS: dict[int, dict | None] = {
    # 12328: a plain single order -> the order id
    12328: ok([row("3420026566347011", "punitha", "12 MG Road, Pune 411001")]),

    # 12329: the chat does not exist -> line stays blank (nothing is written)
    12329: None,

    # 12330: single order, short id -> written as it stands
    12330: ok([row("341999308084544", "ramesh", "5 Park St, Chennai 600001")]),

    # 12331: FOUR orders, all the same customer -> the name, no order ids
    12331: ok([
        row("342008003755738432", "Priya Sharma", "7 Lake Rd, Pune 411001"),
        row("342008010858497088", "Priya Sharma", "7 Lake Rd, Pune 411001"),
        row("342006564139918464", "Priya  Sharma", "7 Lake Rd, Pune 411001"),
        row("34200665825825863", "PRIYA SHARMA", "7 Lake Rd, Pune 411001"),
    ]),

    # 12332: FOUR orders, 3 x Priya + 1 x Ravi -> majority name + app flag
    12332: ok([
        row("342008003755738432", "Priya Sharma", "7 Lake Rd, Pune"),
        row("342008010858497088", "Priya Sharma", "7 Lake Rd, Pune"),
        row("342006564139918464", "Priya Sharma", "7 Lake Rd, Pune"),
        row("342005019294532928", "Ravi Kumar", "9 Hill Rd, Pune"),
    ]),

    # 12333: no order id, but a name was read -> the name
    12333: {"type": "FALLBACK_NAME", "name": "punitha", "order_rows": []},

    # 12334: no id and no name, only an address -> the address
    12334: {
        "type": "FALLBACK_ADDRESS",
        "address": "12 MG Road, Pune 411001",
        "order_rows": [],
    },

    # 12335: nothing at all was readable -> blank + ERROR in the terminal
    12335: {"type": "FAILED", "order_rows": []},

    # 12336: one order -> id (a single order is never replaced by a name)
    12336: ok([row("3420087259994548160", "Priya Sharma", "7 Lake Rd, Pune")]),

    # 12338: TWO orders, only one has a name -> that name + app flag
    12338: ok([
        row("3420013298145700", "Priya Sharma", "7 Lake Rd, Pune"),
        row("3420087259994548160", "", "7 Lake Rd, Pune"),
    ]),

    # 12339: TWO orders, no names at all -> the address, never one of the ids
    12339: ok([
        row("3420013298145700", "", "12 MG Road, Pune 411001"),
        row("3420087259994548160", "", "12 MG Road, Pune 411001"),
    ]),

    # 12340: THREE orders, only ONE name (the other two screenshots were
    # cropped and the name was cut off) -> that name + app flag
    12340: ok([
        row("3420026566347011", "Priya Sharma", "7 Lake Rd, Pune"),
        row("3420087259994548", "", "", status="TRUNC"),
        row("3420080037557384", "", "", status="TRUNC"),
    ]),

    # 12341: the screenshot was cropped, but 6 digits survived -> the id
    12341: {
        "type": "TRUNC",
        "truncated_ids": ["342187"],
        "order_rows": [row("342187", "", "", status="TRUNC")],
    },
}

# 12337 is in the file but was never processed (outside the run range).


def main() -> None:
    with open(BEFORE, "w", encoding="utf-8", newline="\n") as fh:
        for internal_id in IDS:
            fh.write(f"{internal_id}\n")

    shutil.copyfile(BEFORE, AFTER)
    writer = NotepadWriter(AFTER, mode="fill")

    for internal_id in IDS:
        result = SCENARIOS.get(internal_id)
        if result is None:
            continue  # missing chat: the writer is never called
        writer.write_result(internal_id, result)
        if writer.last_notice:
            NOTICES.append(f"{internal_id}: WARN {writer.last_notice}")

    def show(title: str, path: str) -> None:
        print(title)
        with open(path, "r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                rendered = line.rstrip("\n").replace("\t", "  <TAB>  ")
                print(f"   line {number:>2}: {rendered if rendered else '(blank)'}")
        print()

    show("BEFORE  (the seller's list, ids only)", BEFORE)
    show("AFTER   (what FrameFlow writes)", AFTER)

    print("TERMINAL  (lines the app prints while running)")
    if NOTICES:
        for notice in NOTICES:
            print(f"   {notice}")
    else:
        print("   (no errors)")
    print()


if __name__ == "__main__":
    main()
