"""Build a real master list you can open in Notepad to confirm the rules.

Creates two files in this folder:

  confirm_master_list.txt         the seller's list - internal ids only
  confirm_master_list_filled.txt  the same list after a FrameFlow run

The "filled" file is produced by the real ``output.writer.NotepadWriter``
(mode="fill"), using the same result shapes ``core.orchestrator`` builds, over
the ids of the current run range (12988-13010).  Every rule is represented at
least once, so the file is a checklist, not a mock-up.
"""

from __future__ import annotations

import logging
import os
import shutil

from output.writer import NotepadWriter

ROOT = os.path.dirname(os.path.abspath(__file__))
BEFORE = os.path.join(ROOT, "confirm_master_list.txt")
AFTER = os.path.join(ROOT, "confirm_master_list_filled.txt")

#: The run range from config.yaml (12988-13010), one id per line - exactly the
#: shape of the seller's own D:/excel/<date>.txt.
IDS = list(range(12988, 13011))

#: One id the seller forgot to type into the list (the chat exists anyway).
#: The app must warn and leave the list alone, never append a stray line.
MISSING_FROM_LIST = 13000

NOTICES: list[str] = []


def row(value: str, name: str = "", address: str = "", status: str = "OK") -> dict:
    return {"value": value, "status": status, "name": name, "address": address}


def ok(rows: list[dict]) -> dict:
    return {"type": "OK", "order_ids": [r["value"] for r in rows], "order_rows": rows}


#: One entry per chat.  ``None`` = the chat does not exist.
SCENARIOS: dict[int, dict | None] = {
    # 12988: ONE order -> the order id (as the screenshot shows it)
    12988: ok([row("3420026566347011", "punitha", "12 MG Road, Pune 411001")]),

    # 12989: the chat does not exist -> the line stays blank, nothing shifts
    12989: None,

    # 12990: one order, a short 15-digit id -> written as it stands
    12990: ok([row("341999308084544", "ramesh kumar", "5 Park St, Chennai 600001")]),

    # 12991: FOUR orders, one customer -> the name, no ids
    12991: ok([
        row("342008003755738432", "Priya Sharma", "7 Lake Rd, Pune 411001"),
        row("342008010858497088", "Priya Sharma", "7 Lake Rd, Pune 411001"),
        row("342006564139918464", "Priya  Sharma", "7 Lake Rd, Pune 411001"),
        row("342006658258258630", "PRIYA SHARMA", "7 Lake Rd, Pune 411001"),
    ]),

    # 12992: 3 x Priya + 1 x Ravi -> the most common name + WARN names differ
    12992: ok([
        row("342008003755738432", "Priya Sharma", "7 Lake Rd, Pune"),
        row("342008010858497088", "Priya Sharma", "7 Lake Rd, Pune"),
        row("342006564139918464", "Priya Sharma", "7 Lake Rd, Pune"),
        row("342005019294532928", "Ravi Kumar", "9 Hill Rd, Pune"),
    ]),

    # 12993: THREE orders, only ONE name (two screenshots were cropped)
    #        -> that name + WARN name missing
    12993: ok([
        row("3420026566347011", "Priya Sharma", "7 Lake Rd, Pune"),
        row("3420087259994548", "", "", status="TRUNC"),
        row("3420080037557384", "", "", status="TRUNC"),
    ]),

    # 12994: TWO orders, no name at all -> the address, never one of the ids
    12994: ok([
        row("3420013298145700", "", "12 MG Road, Pune 411001"),
        row("3420087259994548160", "", "12 MG Road, Pune 411001"),
    ]),

    # 12995: a cropped screenshot, but 6 digits survived -> the id itself
    12995: {
        "type": "TRUNC",
        "truncated_ids": ["342187"],
        "order_rows": [row("342187", "", "", status="TRUNC")],
    },

    # 12996: cropped at 5 digits - not an id -> the name fills the line
    12996: {
        "type": "TRUNC",
        "truncated_ids": ["34218"],
        "order_rows": [row("34218", "punitha", "12 MG Road, Pune 411001",
                           status="TRUNC")],
    },

    # 12997: no order id, but a name was read -> the name
    12997: {"type": "FALLBACK_NAME", "name": "punitha", "order_rows": []},

    # 12998: no id, no name, only an address -> the address
    12998: {
        "type": "FALLBACK_ADDRESS",
        "address": "12 MG Road, Pune 411001",
        "order_rows": [],
    },

    # 12999: nothing readable -> the line stays blank + ERROR in the terminal
    12999: {"type": "FAILED", "order_rows": []},

    # 13000: a chat that is not in the list at all -> WARN, never appended
    13000: ok([row("342008819999999999", "someone", "somewhere")]),

    # 13001: one order with a name -> the id (a single order keeps its id)
    13001: ok([row("3420087259994548160", "Priya Sharma", "7 Lake Rd, Pune")]),

    # 13002: an 18-digit id -> written in full
    13002: ok([row("342008003755738432", "Ravi Kumar", "9 Hill Rd, Pune")]),

    # 13003: an address with a comma -> one value, not split
    13003: {
        "type": "FALLBACK_ADDRESS",
        "address": "Flat 4, Lake View, Pune 411001",
        "order_rows": [],
    },

    # 13004: two orders, same cropped id twice, no names -> first id + WARN
    13004: ok([
        row("34200132981457", "", "", status="TRUNC"),
        row("34200872599945", "", "", status="TRUNC"),
    ]),

    # 13010: run already stops before the last ids - these stay untouched
}

# 13005-13009 are in the list but outside the processed range (no entry).
# 12989, 13005-13009 keep their blank line; 13000 is missing from the list.


def main() -> None:
    # Show what the app writes to logs/frameflow.log for this run.
    logging.basicConfig(
        level=logging.WARNING, format="  log: %(message)s", force=True
    )

    with open(BEFORE, "w", encoding="utf-8", newline="\n") as fh:
        for internal_id in IDS:
            if internal_id == MISSING_FROM_LIST:
                continue          # the seller forgot this one
            fh.write(f"{internal_id}\n")

    shutil.copyfile(BEFORE, AFTER)
    writer = NotepadWriter(AFTER, mode="fill")

    for internal_id in IDS:
        result = SCENARIOS.get(internal_id)
        if result is None:
            continue
        writer.write_result(internal_id, result)
        if writer.last_notice:
            NOTICES.append(
                f"{internal_id}: WARN {writer.last_notice}"
            )

    print(f"\nBefore : {BEFORE}")
    print(f"After  : {AFTER}\n")

    with open(AFTER, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if "\t" in line:
                internal_id, value = line.split("\t", 1)
                print(f"  line {number:>2}: {internal_id:>6} <TAB> {value}")
            else:
                print(f"  line {number:>2}: {line:>6}          (blank)")

    print("\nTERMINAL:")
    for notice in NOTICES:
        print("  " + notice)


if __name__ == "__main__":
    main()
