"""Proof: the number in the master list IS the internal chat number.

Builds a list with the ids from the screenshot (1265-1290, one per line),
then runs the **real** Orchestrator over that range against fake WhatsApp
chats named exactly ``"1277"``, ``"1278"`` ... - the same lookup the live
WAHA client does (``get_chat_by_name(str(internal_id))``).

Produces:
  confirm_chats_BEFORE.txt   ids only, like the seller's file
  confirm_chats_AFTER.txt    what the run writes

Only the chat named 1278 etc. is touched; every other line stays as it was.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from core.orchestrator import Orchestrator
from frameflow_config import load_config
from extractor.rules import Rules
from output.writer import NotepadWriter
from state.state import StateStore
from tests.test_phase3 import FakeAPI, FakeOCR, RecordingWriter, make_message

ROOT = os.path.dirname(os.path.abspath(__file__))
BEFORE = os.path.join(ROOT, "confirm_chats_BEFORE.txt")
AFTER = os.path.join(ROOT, "confirm_chats_AFTER.txt")

#: Exactly the column from the Notepad++ screenshot.
IDS = list(range(1265, 1291))

#: The WhatsApp chat names (the internal number) and what each chat sends.
CHAT_TEXTS: dict[str, str] = {
    "1276": "Order ID: 342008003755738432",
    "1277": "Order ID: 342008010858497088",
    "1278": "Order ID: 3420026566347011",   # <- the line the arrow points at
    "1280": "Order ID: 341999308084544",
    "1285": "Order ID: 342008725999454816",
}
# 1279 and the rest have no chat -> their lines stay blank.


def main() -> None:
    with open(BEFORE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(str(i) for i in IDS) + "\n")
    shutil.copyfile(BEFORE, AFTER)

    chats = {name: {"id": f"c-{name}", "name": name} for name in CHAT_TEXTS}
    messages = {
        f"c-{name}": [make_message(f"m-{name}", text=text)]
        for name, text in CHAT_TEXTS.items()
    }
    api = FakeAPI(chats=chats, messages=messages)

    config = load_config()
    scratch = tempfile.mkdtemp(prefix="frameflow-confirm-")
    orch = Orchestrator(
        api_client=api,
        ocr_engine=FakeOCR(),
        writer=RecordingWriter(),
        notepad_writer=NotepadWriter(AFTER, mode="fill"),
        state=StateStore(os.path.join(scratch, "state.db")),
        rules=Rules.from_config(config),
        config=config,
        assets_dir=os.path.join(scratch, "assets"),
        logs_dir=os.path.join(scratch, "logs"),
        review_dir=os.path.join(scratch, "review"),
    )
    logs: list[str] = []
    orch.log_callback = logs.append

    summary = orch.run(IDS[0], IDS[-1])

    print("Run range :", IDS[0], "->", IDS[-1])
    print("Chats that exist:", ", ".join(sorted(CHAT_TEXTS)))
    print("Chats missing   :", ", ".join(
        str(i) for i in IDS if str(i) not in CHAT_TEXTS
    ))
    print()
    print("AFTER  (confirm_chats_AFTER.txt)")
    with open(AFTER, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            if "\t" in line:
                internal_id, value = line.split("\t", 1)
                print(f"  {internal_id:>5} <TAB> {value}")
            else:
                print(f"  {line:>5}          (blank)")
    print()
    print("Results:", summary.total, "processed,",
          summary.missing, "missing chats")
    print()
    print("Terminal:")
    for line in logs[:25]:
        if "missing" in line.lower() or "not in the Notepad" in line:
            continue
        print("  " + line)


if __name__ == "__main__":
    main()
