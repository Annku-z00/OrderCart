"""
tests/test_order_details.py
===========================

The "collect everything that is available" feature set:

* a screenshot yields the order id **and** the customer name **and** the
  address — the three travel together into one output row;
* every order keeps **its own** customer (a chat with two orders does not
  give both of them the first customer's name);
* the seller's plain "internal_id,value" Notepad list is written next to the
  detailed output and is corrected together with it;
* the Orders view can export the whole list to Excel / Notepad;
* a phone label in the seller's own language ("संपर्क नंबर") is never read as
  a customer's name.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.orchestrator import Orchestrator
from extractor.parser import find_name
from extractor.rules import Rules
from output.csv_writer import CSVWriter
from output.writer import NotepadWriter
from state.state import StateStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
CONFIG = {
    "whatsapp_api": {"provider": "waha", "base_url": "http://localhost:3000"},
    "extraction": {
        "order_id_labels": ["Order ID", "Order Number", "Order No", "Order #"],
        "name_labels": ["Customer Name", "Name", "Delivery Address"],
        "address_labels": ["Address", "Shipping Address", "Delivery Address"],
        "multi_order_output": True,
        "cropped_id_is_complete": True,
        # The shipped setting: the id is written exactly as it appears.
        "order_id_truncate_length": 0,
        "order_id_truncate_from": "first",
        # These tests cover the "all" mode — the id *and* the customer
        # details travel together.  TestCollectModes covers "single".
        "collect": "all",
        "text_message_limit": 5,
        "address_max_chars": 400,
    },
    "ocr": {"upscale_factor": 1},
    "output": {"default_file": "orders.csv", "date_format": "%Y-%m-%d %H:%M:%S"},
    "processing": {"delay_between_chats": 0, "max_retries": 1, "retry_backoff": 0},
}


class FakeOCRResult:
    def __init__(self, text: str = "", confidence: float = 0.9):
        self.text = text
        self.confidence = confidence
        self.engine = "fake"
        self.lines = [ln for ln in text.split("\n") if ln.strip()]
        self.error = None


class FakeOCR:
    """Returns the "recognised text" the fake API wrote into the file."""

    def __init__(self):
        self.calls = []

    def run_on_file(self, path: str, upscale_factor: int = 1) -> FakeOCRResult:
        self.calls.append(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return FakeOCRResult(fh.read())
        except (OSError, UnicodeDecodeError):
            return FakeOCRResult("")


class FakeAPI:
    """Serves scripted image messages; each one carries its own text."""

    def __init__(self, chat_id: str, texts):
        self.chat_id = chat_id
        self.chats = {chat_id: {"id": chat_id, "name": chat_id}}
        self.messages = {
            chat_id: [
                {
                    "id": {"id": "img-%d" % index},
                    "fromMe": False,
                    "hasMedia": True,
                    "type": "image",
                    "_data": {"type": "image", "mimetype": "image/jpeg"},
                    "media": {"mimetype": "image/jpeg", "url": "http://f/i.jpg"},
                }
                for index in range(len(texts))
            ]
        }
        self.texts = list(texts)
        self._served = 0

    def get_chat_by_name(self, name: str):
        return self.chats.get(str(name))

    def get_messages(self, chat_id: str, limit: int = 50):
        return list(self.messages.get(chat_id, []))[:limit]

    def download_media(self, msg):
        text = self.texts[self._served]
        self._served += 1
        return text.encode("utf-8")

    def close(self):
        pass


def _run(tmp_path, texts, notepad_path=None):
    """Run one chat whose screenshots carry ``texts``; return the CSV rows."""
    # The chat is named after the internal order id, the way the seller
    # names them.
    chat_id = "12988"
    api = FakeAPI(chat_id, texts)
    csv_path = os.path.join(str(tmp_path), "orders.csv")
    writer = CSVWriter(csv_path, date_format=CONFIG["output"]["date_format"])
    notepad_writer = (
        NotepadWriter(notepad_path) if notepad_path else None
    )
    orch = Orchestrator(
        api_client=api,
        ocr_engine=FakeOCR(),
        writer=writer,
        state=StateStore(os.path.join(str(tmp_path), "state.db")),
        notepad_writer=notepad_writer,
        config=CONFIG,
        assets_dir=os.path.join(str(tmp_path), "assets"),
        logs_dir=os.path.join(str(tmp_path), "logs"),
        review_dir=os.path.join(str(tmp_path), "review"),
    )
    result = orch.process_one(12988)
    rows = writer.read_rows()
    orch.close()
    return result, rows


#: One Meesho order screen: the order id AND the delivery block.
SCREENSHOT = """ORDER DETAILS
Order #342169589629518912
Royal Photo Frame 5*7
Free Size Cash 168
Delivery Address
punitha
232/3 teachers colony, karur
Tamil Nadu - 639004
Contact Number - 9786782006
"""

#: A second customer in the same chat.
SCREENSHOT_TWO = """ORDER DETAILS
Order #342343834138346752
Royal Photo Frame 5*7
Delivery Address
sathya
45 cross street, madurai
Tamil Nadu - 639004
Contact Number - 9741122334
"""


# ---------------------------------------------------------------------------
# The order id, the name and the address travel together
# ---------------------------------------------------------------------------
class TestOrderDetailsTogether:
    def test_one_row_carries_the_id_the_name_and_the_address(self, tmp_path):
        result, rows = _run(tmp_path, [SCREENSHOT])

        assert result.status == "OK"
        assert result.order_ids == ["342169589629518912"]
        assert len(rows) == 1

        row = rows[0]
        assert row["order_id"] == "342169589629518912"
        assert row["name"] == "punitha"
        assert "teachers colony" in row["address"]
        assert "639004" in row["address"]

    def test_every_order_keeps_its_own_customer(self, tmp_path):
        """Two orders, two customers — never the first one for both."""
        result, rows = _run(tmp_path, [SCREENSHOT, SCREENSHOT_TWO])

        assert len(result.order_ids) == 2
        assert len(rows) == 2

        by_id = {row["order_id"]: row for row in rows}
        assert by_id["342169589629518912"]["name"] == "punitha"
        assert "karur" in by_id["342169589629518912"]["address"]
        assert by_id["342343834138346752"]["name"] == "sathya"
        assert "madurai" in by_id["342343834138346752"]["address"]

    def test_the_notepad_list_stays_id_only(self, tmp_path):
        """The seller's "id,value" file keeps its load-bearing format."""
        notepad = os.path.join(str(tmp_path), "notepad.txt")
        _run(tmp_path, [SCREENSHOT], notepad_path=notepad)

        with open(notepad, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        assert lines == ["12988,342169589629518912"]


# ---------------------------------------------------------------------------
# A phone label in the seller's language is never a customer name
# ---------------------------------------------------------------------------
class TestPhoneLabelIsNotAName:
    RULES = Rules.from_config(CONFIG)

    def test_a_hindi_contact_number_is_not_a_name(self):
        text = "संपर्क नंबर 9786782006"
        name, _conf = find_name(text, self.RULES)
        assert name is None

    def test_a_tamil_contact_number_is_not_a_name(self):
        text = "தொடர்பு எண் 9786782006"
        name, _conf = find_name(text, self.RULES)
        assert name is None

    def test_an_english_contact_number_is_not_a_name(self):
        text = "Contact Number - 9786782006"
        name, _conf = find_name(text, self.RULES)
        assert name is None

    def test_a_real_name_next_to_the_number_still_works(self):
        text = "punitha 7418622538"
        name, _conf = find_name(text, self.RULES)
        assert name == "punitha"


# ---------------------------------------------------------------------------
# What a row collects: "single" (the default) or "all"
# ---------------------------------------------------------------------------
class TestCollectModes:
    def _run(self, tmp_path, texts, mode):
        cfg = {
            **CONFIG,
            "extraction": {**CONFIG["extraction"], "collect": mode},
        }
        chat_id = "12988"
        api = FakeAPI(chat_id, texts)
        csv_path = os.path.join(str(tmp_path), "orders.csv")
        writer = CSVWriter(csv_path, date_format=cfg["output"]["date_format"])
        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=writer,
            state=StateStore(os.path.join(str(tmp_path), "state.db")),
            config=cfg,
            assets_dir=os.path.join(str(tmp_path), "assets"),
            logs_dir=os.path.join(str(tmp_path), "logs"),
            review_dir=os.path.join(str(tmp_path), "review"),
        )
        result = orch.process_one(12988)
        rows = writer.read_rows()
        orch.close()
        return result, rows

    def test_single_mode_keeps_only_the_order_id(self, tmp_path):
        """The default: one value per row — the id wins, nothing else."""
        _result, rows = self._run(tmp_path, [SCREENSHOT], "single")
        assert len(rows) == 1
        assert rows[0]["order_id"] == "342169589629518912"
        assert rows[0]["name"] == ""
        assert rows[0]["address"] == ""

    def test_all_mode_keeps_the_id_and_the_details(self, tmp_path):
        _result, rows = self._run(tmp_path, [SCREENSHOT], "all")
        assert len(rows) == 1
        assert rows[0]["order_id"] == "342169589629518912"
        assert rows[0]["name"] == "punitha"
        assert "teachers colony" in rows[0]["address"]

    def test_single_mode_falls_back_to_the_name(self, tmp_path):
        """No order id in the picture -> the name is the value."""
        no_id = (
            "ORDER DETAILS\nDelivery Address\npunitha\n"
            "232/3 teachers colony, karur\nTamil Nadu - 639004\n"
            "Contact Number - 9786782006\n"
        )
        result, rows = self._run(tmp_path, [no_id], "single")
        assert result.status == "FALLBACK_NAME"
        assert rows[0]["order_id"] == ""
        assert rows[0]["name"] == "punitha"

    def test_single_mode_falls_back_to_the_address(self, tmp_path):
        """Neither an id nor a name -> the address is the value."""
        no_id_no_name = (
            "ORDER DETAILS\nDelivery Address\n"
            "232/3 teachers colony, karur\nTamil Nadu - 639004\n"
            "Contact Number - 9786782006\n"
        )
        result, rows = self._run(tmp_path, [no_id_no_name], "single")
        assert result.status == "FALLBACK_ADDRESS"
        assert rows[0]["order_id"] == ""
        assert rows[0]["name"] == ""
        assert "teachers colony" in rows[0]["address"]


# ---------------------------------------------------------------------------
# The failed list — but only for real order screens
# ---------------------------------------------------------------------------
#: An order screen whose id was cropped away and that carries no delivery
#: block either: it still has the order headings, so nothing was read from
#: it and it belongs in the failed list.
ORDER_SCREEN_WITHOUT_ID = """ORDER DETAILS
Royal Photo Frame 5*7
Order Placed
Shipping Soon!
Ordered   Shipped   Out for Delivery
16 Oct   18 Oct   26 Oct
"""

#: A plain picture of the product the customer ordered — not an order.
PRODUCT_PICTURE = """Royal Photo Frame 5*7
Free Size
Cash 168
BUY 1
All issue easy returns
"""


class TestFailedList:
    def _run(self, tmp_path, texts):
        cfg = {**CONFIG, "extraction": {**CONFIG["extraction"], "collect": "single"}}
        chat_id = "12988"
        api = FakeAPI(chat_id, texts)
        csv_path = os.path.join(str(tmp_path), "orders.csv")
        failed_dir = os.path.join(str(tmp_path), "failed")
        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=CSVWriter(csv_path, date_format=cfg["output"]["date_format"]),
            state=StateStore(os.path.join(str(tmp_path), "state.db")),
            config=cfg,
            assets_dir=os.path.join(str(tmp_path), "assets"),
            logs_dir=os.path.join(str(tmp_path), "logs"),
            review_dir=os.path.join(str(tmp_path), "review"),
            failed_dir=failed_dir,
        )
        result = orch.process_one(12988)
        orch.close()
        return result, failed_dir

    def test_an_order_screen_with_nothing_in_it_goes_to_the_failed_list(self, tmp_path):
        _result, failed_dir = self._run(tmp_path, [ORDER_SCREEN_WITHOUT_ID])

        saved = []
        for root, _dirs, files in os.walk(failed_dir):
            saved.extend(os.path.join(root, f) for f in files)
        assert len(saved) == 1, "the order screen should be in the failed list"

    def test_a_product_picture_is_not_a_failed_order(self, tmp_path):
        """The customer sent a photo of the frame — that is not a failure."""
        _result, failed_dir = self._run(tmp_path, [PRODUCT_PICTURE])

        saved = []
        for root, _dirs, files in os.walk(failed_dir):
            saved.extend(os.path.join(root, f) for f in files)
        assert saved == [], "a product picture must not land in the failed list"

    def test_the_marker_check_itself(self):
        assert Orchestrator._looks_like_order("ORDER DETAILS\nOrder #123")
        assert Orchestrator._looks_like_order("Delivery Address\nsomewhere")
        assert Orchestrator._looks_like_order("Order confirmed\nSaved 39")
        assert not Orchestrator._looks_like_order(PRODUCT_PICTURE)
        assert not Orchestrator._looks_like_order("")
        assert not Orchestrator._looks_like_order("just a family photo")


# ---------------------------------------------------------------------------
# The export buttons
# ---------------------------------------------------------------------------
class TestExport:
    def _api(self, tmp_path, monkeypatch):
        import main_web

        monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
        api = main_web.Api()
        out = os.path.join(str(tmp_path), "orders.csv")
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write(
                "internal_id,order_id,name,address,status,source,confidence,"
                "partial,timestamp\n"
            )
            fh.write("12988,342169589629518912,punitha,karur 639004,OK,image,"
                     "0.95,false,2026-10-21 12:00:00\n")
        api.config.setdefault("output", {})
        api.config["output"]["default_file"] = out
        return api

    def test_export_to_excel_writes_a_workbook(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.export_orders_as("xlsx")

        assert result["ok"] is True
        assert result["rows"] == 1
        assert result["path"].endswith(".xlsx")
        assert os.path.isfile(result["path"])
        # A real workbook — readable by openpyxl.
        from openpyxl import load_workbook

        sheet = load_workbook(result["path"]).active
        assert sheet.cell(row=1, column=1).value == "internal_id"
        assert sheet.cell(row=2, column=2).value == "342169589629518912"

    def test_export_to_notepad_writes_a_tab_separated_list(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.export_orders_as("txt")

        assert result["ok"] is True
        with open(result["path"], encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        assert lines[0].split("\t")[:4] == [
            "Internal ID", "Order ID", "Name", "Address",
        ]
        assert lines[1].split("\t")[:4] == [
            "12988", "342169589629518912", "punitha", "karur 639004",
        ]

    def test_export_to_master_list_writes_id_tab_value(self, tmp_path, monkeypatch):
        """The "Export Notepad" button writes the notepad master-list format."""
        api = self._api(tmp_path, monkeypatch)
        result = api.export_orders_as("notepad")

        assert result["ok"] is True
        assert result["path"].endswith(".txt")
        with open(result["path"], encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        # One order -> its id, no header line, exactly id <TAB> value.
        assert lines == ["12988\t342169589629518912"]

    def test_master_list_export_collapses_several_orders_to_the_name(
        self, tmp_path, monkeypatch
    ):
        api = self._api(tmp_path, monkeypatch)
        out = api.config["output"]["default_file"]
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write(
                "internal_id,order_id,name,address,status,source,confidence,"
                "partial,timestamp\n"
            )
            fh.write("12988,342169589629518912,Priya Sharma,Pune,OK,image,"
                     "0.95,false,2026-10-21 12:00:00\n")
            fh.write("12988,342169589629518913,Priya Sharma,Pune,OK,image,"
                     "0.95,false,2026-10-21 12:00:00\n")
            fh.write("12989,,,Pune,OK,image,"
                     "0.95,false,2026-10-21 12:00:00\n")

        result = api.export_orders_as("notepad")

        with open(result["path"], encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        assert lines == ["12988\tPriya Sharma", "12989\tPune"]

    def test_export_to_csv_writes_every_column(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.export_orders_as("csv")

        assert result["ok"] is True
        with open(result["path"], encoding="utf-8-sig") as fh:
            content = fh.read()
        assert "order_id" in content
        assert "342169589629518912" in content

    def test_an_unknown_format_is_refused(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.export_orders_as("pdf")
        assert result["ok"] is False
        assert "pdf" in result["error"]

    def test_exporting_nothing_is_refused(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        api.config["output"]["default_file"] = os.path.join(
            str(tmp_path), "does-not-exist.csv"
        )
        result = api.export_orders_as("xlsx")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# The clipboard helper
# ---------------------------------------------------------------------------
class TestClipboard:
    def test_the_text_reaches_the_clipboard(self, tmp_path, monkeypatch):
        import main_web

        monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
        api = main_web.Api()
        result = api.copy_to_clipboard("12988\t342169589629518912")

        assert result["ok"] is True
        assert result["length"] == len("12988\t342169589629518912")

    def test_copying_nothing_is_refused(self, tmp_path, monkeypatch):
        import main_web

        monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
        api = main_web.Api()
        result = api.copy_to_clipboard("")
        assert result["ok"] is False
