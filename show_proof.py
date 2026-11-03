"""Live, self-contained proof that the WAHA media fix works.

This deliberately avoids the OCR stack (cv2) which crashes natively
on this machine under some conditions.  It exercises *exactly* the code path
that was broken and now fixed:

    whatsapp.api_client.WhatsAppAPIClient.get_messages(downloadMedia=true flag)
    whatsapp.api_client.WhatsAppAPIClient.download_media(msg)  -> msg["media"]["url"]

Run:  "C:\\Users\\rames\\AppData\\Local\\Python\\pythoncore-3.10-64\\python.exe" show_proof.py
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from frameflow_config import load_config  # noqa: E402
from whatsapp.api_client import (  # noqa: E402
    WhatsAppAPIClient,
    message_id,
    is_image_message,
)

CONFIG = load_config("config.yaml")
CHAT_NAME = "Jeetu Kumawat"
OUT_DIR = os.path.join("assets", "_proof")
JPEG = b"\xff\xd8\xff"
PNG = b"\x89PNG"


def line(title=""):
    print("\n" + "=" * 68)
    if title:
        print(title)
        print("=" * 68)


def magic(data: bytes) -> str:
    if data.startswith(JPEG):
        return "JPEG"
    if data.startswith(PNG):
        return "PNG"
    return "unknown"


def main() -> int:
    api = WhatsAppAPIClient.from_config(CONFIG)
    base = CONFIG.get("whatsapp_api", {}).get("base_url")
    print(f"WAHA base_url : {base}")

    # 1. connectivity
    line("1) Connectivity")
    print(f"ping() -> {api.ping()}")

    # 2. resolve the contact
    line(f"2) Resolve contact '{CHAT_NAME}'")
    chat = api.get_chat_by_name(CHAT_NAME)
    if not chat:
        print("!! chat not found")
        api.close()
        return 2
    chat_id = chat.get("id") or chat.get("_serialized") or chat.get("serialized")
    if isinstance(chat_id, dict):  # WAHA nests the jid: {"server":..,"_serialized":..}
        chat_id = chat_id.get("_serialized") or chat_id.get("id")
    if not chat_id:
        print(f"!! could not derive a chat id from {chat!r}")
        api.close()
        return 2
    print(f"chat id -> {chat_id}")

    # 3. fetch messages asking WAHA to materialise media
    line("3) get_messages(downloadMedia=true)  [the fix]")
    messages = api.get_messages(chat_id, limit=30, download_media=True)
    print(f"messages returned : {len(messages)}")

    images, others = [], []
    for msg in messages:
        (images if is_image_message(msg) else others).append(msg)

    print(f"  is_image_message() == True  : {len(images)}")
    print(f"  everything else (text/voice) : {len(others)}")
    types = {}
    for msg in messages:
        data = msg.get("_data") or {}
        types[str(data.get("type"))] = types.get(str(data.get("type")), 0) + 1
    print(f"  WAHA _data.type histogram   : {types}")

    # 4. download the first few real images
    line("4) download_media() -> real bytes on disk")
    os.makedirs(OUT_DIR, exist_ok=True)
    ok = 0
    for idx, msg in enumerate(images[:5], start=1):
        mid = message_id(msg)
        if not mid:
            print(f"  [{idx}] skipped: message has no id")
            continue
        try:
            started = time.time()
            blob = api.download_media(msg)
            out = os.path.join(OUT_DIR, f"{idx}_{mid}.jpg")
            with open(out, "wb") as fh:
                fh.write(blob)
            fmt = magic(blob)
            ok += 1
            print(
                f"  [{idx}] OK  {len(blob):>7} bytes  {fmt:<6}  "
                f"{time.time() - started:5.1f}s  {os.path.basename(out)}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [{idx}] FAIL {type(exc).__name__}: {exc}")
    print(f"\n  downloaded {ok}/{min(len(images), 5)} images -> {OUT_DIR}")

    # 5. show what the real trial already produced on disk
    line("5) Artifacts already produced by the full pipeline run")
    assets = os.path.join("assets", "99999")
    if os.path.isdir(assets):
        for name in sorted(os.listdir(assets)):
            path = os.path.join(assets, name)
            with open(path, "rb") as fh:
                blob = fh.read()
            print(f"  assets/99999/{name}  {len(blob)} bytes  {magic(blob)}")
    out_file = os.path.join("output", "orders_trial.txt")
    if os.path.exists(out_file):
        print(f"\n  {out_file}:")
        with open(out_file, "r", encoding="utf-8") as fh:
            for row in fh:
                print(f"    {row.rstrip()}")

    api.close()
    line()
    verdict = "FIX VERIFIED" if ok else "could not download now (media may have expired)"
    print(f"RESULT: {verdict}  ({ok} images downloaded live)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
