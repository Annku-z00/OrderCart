"""
debug_chat.py — Phase 4.5 chat-name resolution probe.

Run with:  py -3.12 debug_chat.py   (or `python debug_chat.py`)

Prints:
  1. What ``/chats`` returns (WAHA shows the WhatsApp *display* name for
     ``@lid`` chats — a formatted phone number — not the saved name).
  2. What ``get_chat_by_name('12988')`` resolves to now, in the exact
     Phase 4.5 acceptance format:
         === get_chat_by_name('12988') ===
         FOUND: 12988 / 66645292789868@lid     (or NOT FOUND)
  3. The raw ``/contacts/{id}`` record for the target chat (saved contact
     name / pushname side by side with the display name).

Console `print()` is deliberate here: this is a debug probe, not app code —
all application logging still goes through the `logging` module.
"""
import sys

from frameflow_config import load_config
from whatsapp.api_client import WhatsAppAPIClient

TARGET = "12988"


def _serialized(chat) -> str:
    """Serialized chat id (``66645292789868@lid``) from a chat record."""
    cid = (chat or {}).get("id")
    if isinstance(cid, dict):
        return str(cid.get("_serialized") or "?")
    return str(cid or "?")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 — console may not support reconfigure
        pass

    cfg = load_config()
    client = WhatsAppAPIClient.from_config(cfg)

    print("=== Chats returned by get_all_chats() ===")
    chats = client.get_all_chats()
    print(f"Total chats: {len(chats)}")
    for c in chats[:40]:
        name = c.get("name", "")
        is_group = c.get("isGroup")
        id_obj = c.get("id") or {}
        if isinstance(id_obj, dict):
            serialized = id_obj.get("_serialized", "?")
            server = id_obj.get("server", "?")
        else:
            serialized = str(id_obj)
            server = "?"
        print(
            f"  name={name!r:30s}  id={serialized!r:45s}  "
            f"server={server!r}  isGroup={is_group}"
        )

    print()
    print(f"=== get_chat_by_name('{TARGET}') ===")
    result = client.get_chat_by_name(TARGET)
    if result:
        serialized = _serialized(result)
        print(f"FOUND: {TARGET} / {serialized}")
        # Task 6: also surface the contacts-endpoint record for the target.
        contact = client.get_contact_by_id(serialized) or {}
        print(
            f"CONTACT: name={(contact.get('name') or '?')!r}  "
            f"pushname={(contact.get('pushname') or '?')!r}  "
            f"contact_id={(contact.get('id') or '?')!r}"
        )
    else:
        print("NOT FOUND")
        contact = {}

    print()
    print("=== /contacts/{id} records for @lid chats (saved vs display name) ===")
    shown = 0
    for chat in chats:
        if chat.get("isGroup"):
            continue
        serialized = _serialized(chat)
        if not serialized or "@lid" not in serialized:
            continue
        if shown >= 25:
            print("  ... (listing capped at 25 chats)")
            break
        shown += 1
        contact = client.get_contact_by_id(serialized)
        if contact is None:
            print(f"  chat={serialized!r}  display={chat.get('name')!r}  contact=<none>")
            continue
        print(
            f"  chat={serialized!r}  display={chat.get('name')!r}  "
            f"contact_name={(contact.get('name') or '')!r}  "
            f"pushname={(contact.get('pushname') or '')!r}"
        )

    client.close()
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
