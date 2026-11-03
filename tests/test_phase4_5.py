"""
tests/test_phase4_5.py
======================

Phase 4.5 tests — contacts-endpoint chat-name resolution.

WAHA CORE 2026.9 (WEBJS) returns the WhatsApp display name on ``/chats``
(a formatted phone number for ``@lid`` chats) and the saved address-book
name on ``/contacts/{id}``.  The resolution ladder added in this phase:

    Pass 1  GET /api/{session}/chats?search=<name>   (server-side search)
    Pass 2  GET /api/{session}/chats                  (full list, cached)
    Pass 3  GET /api/{session}/contacts/{id}          (saved names, cached)

Covers:
  A. get_contact_by_id — fail-safe fetch, %40 encoding, header, timeout,
     per-instance cache (hits AND 404 misses; transient failures not cached)
  B. get_chat_by_name — ladder preference, contacts resolution, group
     skipping, number/pushname matching, exact DEBUG/INFO log formats
  C. Chat-name-override regression guard (Phase 4.3 pass 0 preserved)
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch
from urllib.parse import unquote

import pytest
import requests

from core.orchestrator import (
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_TRANSIENT_ERROR,
    STATUS_TRUNC,
    Orchestrator,
    ProcessResult,
    RunSummary,
)
from extractor.rules import Rules
from state.state import StateStore
from whatsapp.api_client import (
    ChatNotFoundError,
    WhatsAppAPIClient,
    WhatsAppAPIError,
)

from tests.test_phase3 import CONFIG, FakeAPI, FakeOCR, RecordingWriter, make_message


# ---------------------------------------------------------------------------
# Constant fixtures (mirrors the live WAHA evidence in the Phase 4.5 brief)
# ---------------------------------------------------------------------------
TARGET = "12988"
LID_USER = "66645292789868"
LID_CHAT = f"{LID_USER}@lid"
DISPLAY_NAME = "+91 87400 23243"          # WhatsApp display name on /chats
CU_CHAT = "918740023243@c.us"

CONTACT_12988: Dict[str, Any] = {
    "id": CU_CHAT,
    "number": LID_USER,
    "name": "12988",
    "pushname": "Jittu.kumawat",
    "shortName": "",
    "type": "in",
    "isMe": False,
    "isUser": True,
    "isGroup": False,
    "isWAContact": True,
    "isMyContact": True,
    "isBlocked": False,
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def make_client(**overrides) -> WhatsAppAPIClient:
    """A WhatsAppAPIClient with a dummy key — never touches the network."""
    defaults = dict(
        base_url="http://localhost:3001",
        api_key="test-key",
        session_name="default",
    )
    defaults.update(overrides)
    return WhatsAppAPIClient(**defaults)


def _resp(json_data: Any, status_code: int = 200) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json = Mock(return_value=json_data)
    return resp


class FakeWaha:
    """URL-routing fake for ``requests.Session`` (get + request).

    * ``/contacts/{id}``       -> contact record (404 when unknown)
    * ``?search=...`` (get)    -> server-side search response
    * ``/chats`` (request)     -> full chat list (as returned by get_all_chats)
    every other route -> 404.
    """

    def __init__(self, chat_list=None, contacts=None, search=None,
                 all_contacts=None, all_contacts_status=200):
        self.chat_list: List[dict] = list(chat_list or [])
        self.contacts: Dict[str, dict] = dict(contacts or {})
        # Default the search response to the chat list itself so Pass 1 sees
        # the same chats a real WAHA search-endpoint would return.
        base = self.chat_list if search is None else (search or [])
        self.search: List[dict] = list(base)
        # Global contacts list (WAHA /api/contacts/all).  Empty by default so
        # the per-chat pass remains the path exercised by the older tests.
        self.all_contacts: List[dict] = list(all_contacts or [])
        self.all_contacts_status = all_contacts_status
        self.calls = {"contacts": 0, "search": 0, "chats": 0, "all_contacts": 0}
        self.contact_ids: List[str] = []

    # -- requests.Session.get (contacts + search) ------------------------
    def get(self, url, headers=None, params=None, timeout=None, **kw):
        # The global list shares the /contacts/ prefix; route it first.
        if url.rstrip("/").endswith("/api/contacts/all"):
            self.calls["all_contacts"] += 1
            if self.all_contacts_status != 200:
                return _resp({"message": "Error"}, self.all_contacts_status)
            return _resp(self.all_contacts)
        if "/contacts/" in url:
            self.calls["contacts"] += 1
            cid = unquote(url.rsplit("/contacts/", 1)[-1])
            self.contact_ids.append(cid)
            if cid in self.contacts:
                return _resp(self.contacts[cid])
            return _resp({"message": "Not Found"}, 404)
        if isinstance(params, dict) and "search" in params:
            self.calls["search"] += 1
            return _resp(self.search)
        return _resp({"message": "Not Found"}, 404)

    # -- requests.Session.request (get_all_chats -> _request) -------------
    def request(self, method, url, headers=None, params=None, **kw):
        if url.endswith("/chats"):
            self.calls["chats"] += 1
            return _resp(self.chat_list)
        return _resp({"message": "Not Found"}, 404)

    @contextmanager
    def installed(self, client: WhatsAppAPIClient):
        """Patch the client's session and yield the fake itself."""
        self._get_mock = None
        self._request_mock = None
        with patch.object(client.session, "get", side_effect=self.get) as gm, \
             patch.object(client.session, "request", side_effect=self.request) as rm:
            self._get_mock = gm
            self._request_mock = rm
            yield self


def waha_chat(name: str, user: str = LID_USER, is_group: bool = False) -> dict:
    """Chat record exactly as WAHA's /chats endpoint returns it for @lid."""
    return {
        "id": {"server": "lid", "user": user, "_serialized": f"{user}@lid"},
        "name": name,
        "isGroup": is_group,
    }


# ========================================================================
# A. get_contact_by_id
# ========================================================================
class TestGetContactById:
    def test_returns_dict_on_200(self):
        client = make_client()
        fake = FakeWaha(contacts={LID_CHAT: CONTACT_12988})
        with fake.installed(client):
            assert client.get_contact_by_id(LID_CHAT) == CONTACT_12988

    def test_returns_none_on_404(self):
        client = make_client()
        fake = FakeWaha(contacts={})  # no contact anywhere
        with fake.installed(client):
            assert client.get_contact_by_id(LID_CHAT) is None

    def test_returns_none_on_network_error(self):
        client = make_client()
        with patch.object(
            client.session, "get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            assert client.get_contact_by_id(LID_CHAT) is None

    def test_encodes_at_sign(self):
        client = make_client()
        seen: Dict[str, str] = {}

        def capture(url, **kw):
            seen["url"] = url
            return _resp(CONTACT_12988)

        with patch.object(client.session, "get", side_effect=capture):
            client.get_contact_by_id(LID_CHAT)

        url = seen["url"]
        assert url.endswith("/contacts/66645292789868%40lid")
        assert "%40" in url and "@" not in url

    def test_sends_api_key_header(self):
        client = make_client()
        seen: Dict[str, Dict[str, str]] = {}

        def capture(url, **kw):
            seen["headers"] = kw.get("headers") or {}
            return _resp(CONTACT_12988)

        with patch.object(client.session, "get", side_effect=capture):
            client.get_contact_by_id(LID_CHAT)

        assert seen["headers"]["X-Api-Key"] == "test-key"

    def test_uses_15s_timeout(self):
        client = make_client()
        seen: Dict[str, Any] = {}

        def capture(url, **kw):
            seen["timeout"] = kw.get("timeout")
            return _resp(CONTACT_12988)

        with patch.object(client.session, "get", side_effect=capture):
            client.get_contact_by_id(LID_CHAT)

        assert seen["timeout"] == 15

    def test_accepts_cus_form(self):
        client = make_client()
        fake = FakeWaha(contacts={CU_CHAT: CONTACT_12988})
        with fake.installed(client):
            assert client.get_contact_by_id(CU_CHAT) == CONTACT_12988

    def test_invalid_json_returns_none(self):
        client = make_client()
        bad = Mock()
        bad.status_code = 200
        bad.json = Mock(side_effect=ValueError("no json"))
        with patch.object(client.session, "get", return_value=bad):
            assert client.get_contact_by_id(LID_CHAT) is None

    def test_non_dict_and_empty_payloads_return_none(self):
        client = make_client()
        for payload in (["not", "a", "dict"], {}, "ok"):
            with patch.object(client.session, "get", return_value=_resp(payload)):
                assert client.get_contact_by_id(LID_CHAT) is None

    def test_caches_hits(self):
        client = make_client()
        fake = FakeWaha(contacts={LID_CHAT: CONTACT_12988})
        with fake.installed(client):
            first = client.get_contact_by_id(LID_CHAT)
            second = client.get_contact_by_id(LID_CHAT)
        assert fake.calls["contacts"] == 1
        assert first == second == CONTACT_12988
        assert client._contact_cache[LID_CHAT] == CONTACT_12988

    def test_caches_404_misses(self):
        client = make_client()
        fake = FakeWaha(contacts={})  # unknown id -> authoritative miss
        with fake.installed(client):
            assert client.get_contact_by_id(LID_CHAT) is None
            assert client.get_contact_by_id(LID_CHAT) is None
        assert fake.calls["contacts"] == 1

    def test_network_errors_are_not_cached(self):
        client = make_client()
        with patch.object(
            client.session, "get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            assert client.get_contact_by_id(LID_CHAT) is None   # attempt 1
            assert client.get_contact_by_id(LID_CHAT) is None   # attempt 2: retries
        assert client._contact_cache.get(LID_CHAT, "absent") == "absent"


# ========================================================================
# B. get_chat_by_name — the three-pass ladder
# ========================================================================
class TestGetChatByNameLadder:
    def test_prefers_search_endpoint(self):
        """Search hit short-circuits: /contacts/ and the chat list never load."""
        client = make_client()
        chat = waha_chat(TARGET)
        fake = FakeWaha(chat_list=[chat], contacts={LID_CHAT: CONTACT_12988})
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)

        assert result == chat
        assert fake.calls["contacts"] == 0
        assert fake.calls["chats"] == 0
        assert fake.calls["search"] == 1

    def test_falls_back_to_chat_list(self):
        """Search empty -> full chat list matches an exact display name."""
        client = make_client()
        chat = waha_chat(TARGET)
        fake = FakeWaha(chat_list=[chat], search=[], contacts={LID_CHAT: CONTACT_12988})
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)

        assert result == chat
        assert fake.calls["search"] == 1
        assert fake.calls["chats"] == 1
        assert fake.calls["contacts"] == 0

    def test_resolves_via_contacts(self):
        """Display name is a formatted phone number; saved contact name wins."""
        client = make_client()
        chat = waha_chat(DISPLAY_NAME)                  # name: "+91 87400 23243"
        fake = FakeWaha(
            chat_list=[chat],
            search=[],
            contacts={LID_CHAT: CONTACT_12988},         # contact name: "12988"
        )
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)

        assert result == chat
        assert result["name"] == DISPLAY_NAME
        assert fake.contact_ids == [LID_CHAT]

    def test_contacts_cache(self):
        """Two full lookups -> /contacts/ hit only once (misses cached too)."""
        client = make_client()
        fake = FakeWaha(
            chat_list=[waha_chat(DISPLAY_NAME)],
            search=[],
            contacts={LID_CHAT: CONTACT_12988},
        )
        with fake.installed(client):
            client.get_chat_by_name(TARGET)
            client.get_chat_by_name(TARGET)

        # The other chats resolve from cache; /contacts/ endpoint only once.
        assert fake.calls["contacts"] == 1
        assert fake.calls["chats"] == 1   # chat list itself has its own cache
        assert fake.calls["search"] == 2  # search pass re-runs each lookup

    def test_empty_chat_list_returns_none_safely(self):
        """No chats at all -> None without touching the contacts endpoint."""
        client = make_client()
        fake = FakeWaha(chat_list=[], search=[], contacts={})
        with fake.installed(client):
            assert client.get_chat_by_name(TARGET) is None
        assert fake.calls["contacts"] == 0

    def test_skips_groups(self):
        """Group chats never reach the contacts endpoint."""
        client = make_client()
        group = waha_chat(TARGET, user="999999999999", is_group=True)
        chat = waha_chat(DISPLAY_NAME, user=LID_USER)
        fake = FakeWaha(
            chat_list=[group, chat],
            search=[],
            contacts={LID_CHAT: CONTACT_12988},
        )
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)

        assert result == chat
        assert fake.contact_ids == [LID_CHAT]           # group id never checked

    def test_non_chat_list_shapes_are_defensive(self):
        """{\"chats\": [...]} and bare lists both work; junk never matches."""
        client = make_client()

        # Shape 1 — {"chats": [...]}
        fake = FakeWaha(chat_list=[waha_chat(TARGET)], search=[], contacts={LID_CHAT: CONTACT_12988})
        with patch.object(client.session, "request",
                          return_value=_resp({"chats": fake.chat_list})), \
             patch.object(client.session, "get", side_effect=fake.get):
            assert client.get_chat_by_name(TARGET) is not None

        # Shape 2 — bare list
        fake2 = FakeWaha(chat_list=[waha_chat(TARGET)], search=[], contacts={})
        with patch.object(client.session, "request",
                          return_value=_resp(fake2.chat_list)), \
             patch.object(client.session, "get", side_effect=fake2.get):
            assert client.get_chat_by_name(TARGET) is not None


class TestGlobalContactsFallback:
    """Phase 4.6 — a partial ``/chats`` list must not hide a saved contact.

    WAHA omits chats whose ``conversationTimestamp`` is null from ``/chats``.
    A saved contact (``name="12988"``) can therefore be absent from that
    list even though the chat and its messages still exist.  The global
    ``/api/contacts/all`` pass resolves it in one cached call.
    """

    def test_resolves_when_chat_list_is_partial(self):
        client = make_client()
        # The chat list contains another chat, but not 12988.
        fake = FakeWaha(
            chat_list=[waha_chat("99999", user="111111111111")],
            search=[],
            contacts={},  # per-chat pass would find nothing
            all_contacts=[{
                "id": CU_CHAT,
                "name": "12988",
                "lid": LID_CHAT,
                "phoneNumber": "918740023243@s.whatsapp.net",
            }],
        )
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)

        assert result is not None
        assert result["id"] == "918740023243@s.whatsapp.net"
        assert result["name"] == TARGET
        assert fake.calls["all_contacts"] == 1
        # The one-call global pass short-circuits the per-chat pass.
        assert fake.calls["contacts"] == 0

    def test_global_contacts_cached_across_lookups(self):
        client = make_client()
        fake = FakeWaha(
            chat_list=[],
            search=[],
            contacts={},
            all_contacts=[{
                "id": CU_CHAT,
                "name": "12988",
                "phoneNumber": "918740023243@s.whatsapp.net",
            }],
        )
        with fake.installed(client):
            assert client.get_chat_by_name(TARGET) is not None
            assert client.get_chat_by_name(TARGET) is not None

        assert fake.calls["all_contacts"] == 1

    def test_global_contacts_failure_falls_back_to_per_chat(self):
        client = make_client()
        fake = FakeWaha(
            chat_list=[waha_chat(DISPLAY_NAME)],
            search=[],
            contacts={LID_CHAT: CONTACT_12988},
            all_contacts_status=500,
        )
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)

        assert result is not None
        assert fake.calls["all_contacts"] == 1
        assert fake.calls["contacts"] == 1


class TestGetChatByNameMatching:
    def test_case_insensitive(self):
        client = make_client()
        spaced = dict(CONTACT_12988, name=" 12988 ")
        fake = FakeWaha(
            chat_list=[waha_chat(DISPLAY_NAME)],
            search=[],
            contacts={LID_CHAT: spaced},
        )
        with fake.installed(client):
            assert client.get_chat_by_name("12988") is not None
            assert client.get_chat_by_name(" 12988 ") is not None
            assert client.get_chat_by_name("12") is None

    def test_matches_on_number(self):
        client = make_client()
        chat = waha_chat(DISPLAY_NAME, user="999999999999")   # unrelated lid user
        contact = dict(CONTACT_12988, name="Jittu", pushname="Jittu")
        fake = FakeWaha(
            chat_list=[chat],
            search=[],
            contacts={"999999999999@lid": contact},
        )
        with fake.installed(client):
            result = client.get_chat_by_name(LID_USER)

        assert result == chat
        assert fake.calls["contacts"] == 1

    def test_matches_on_pushname(self):
        client = make_client()
        chat = waha_chat(DISPLAY_NAME, user="999999999999")
        contact = dict(CONTACT_12988, name="Other", number="000000000000",
                       pushname="Jittu.kumawat")
        fake = FakeWaha(
            chat_list=[chat],
            search=[],
            contacts={"999999999999@lid": contact},
        )
        with fake.installed(client):
            result = client.get_chat_by_name("JITTU.KUMAWAT")   # case-insensitive

        assert result == chat

    def test_returns_none_when_no_match(self):
        client = make_client()
        other = {
            "id": CU_CHAT,
            "number": LID_USER,
            "name": "Someone Else",
            "pushname": "unknown",
        }
        fake = FakeWaha(
            chat_list=[waha_chat(DISPLAY_NAME)],
            search=[],
            contacts={LID_CHAT: other},
        )
        with fake.installed(client):
            assert client.get_chat_by_name(TARGET) is None

    def test_contacts_http_failure_never_crashes_lookup(self):
        client = make_client()
        fake = FakeWaha(chat_list=[waha_chat(DISPLAY_NAME)], search=[], contacts={})
        with patch.object(client.session, "request",
                          return_value=_resp(fake.chat_list)), \
             patch.object(client.session, "get",
                          side_effect=requests.exceptions.ConnectionError("refused")):
            assert client.get_chat_by_name(TARGET) is None


# ========================================================================
# C. Log contract (Task 4 — exact formats the debug script depends on)
# ========================================================================
class TestGetChatByNameLogs:
    def _recorded(self, client, fake, target):
        with fake.installed(client), self._caplog.at_level(
            logging.DEBUG, logger="whatsapp.api_client"
        ):
            return client.get_chat_by_name(target), list(self._caplog.records)

    @pytest.fixture(autouse=True)
    def _grab_caplog(self, caplog):
        self._caplog = caplog  # noqa: PLW0203 — shared through the class

    def test_resolved_via_contacts_log_lines(self):
        client = make_client()
        fake = FakeWaha(
            chat_list=[waha_chat(DISPLAY_NAME)],
            search=[],
            contacts={LID_CHAT: CONTACT_12988},
        )
        result, records = self._recorded(client, fake, TARGET)
        assert result is not None
        messages = [rec.getMessage() for rec in records]

        assert "[get_chat_by_name] target='12988'" in messages
        assert "[get_chat_by_name] pass 1 (search): hits=0" in messages
        assert any(
            m.startswith("[get_chat_by_name] pass 2 (chat list): non_group_count=")
            for m in messages
        )
        assert any(
            re.fullmatch(
                r"\[get_chat_by_name\] pass 3 \(contacts\): "
                r"checked=\d+, matched=1, elapsed_ms=\d+", m
            )
            for m in messages
        )
        assert (
            "[get_chat_by_name] RESOLVED via contacts: "
            "name='12988' chat_id='66645292789868@lid'"
        ) in messages
        # RESOLVED must be INFO; all other lookup-ladder lines stay DEBUG.
        for rec in records:
            msg = rec.getMessage()
            if not msg.startswith("[get_chat_by_name]"):
                continue  # unrelated client logs (e.g. "Fetched N chats") — pre-existing
            if "RESOLVED via contacts" in msg:
                assert rec.levelno == logging.INFO, msg
            else:
                assert rec.levelno == logging.DEBUG, msg

    def test_not_found_log_line(self):
        client = make_client()
        fake = FakeWaha(
            chat_list=[waha_chat(DISPLAY_NAME)],
            search=[],
            contacts={LID_CHAT: {"id": CU_CHAT, "name": "Someone Else",
                                 "pushname": "unknown"}},     # no match
        )
        result, messages_with = None, []
        with fake.installed(client), self._caplog.at_level(
            logging.DEBUG, logger="whatsapp.api_client"
        ):
            result = client.get_chat_by_name(TARGET)
        assert result is None
        assert "[get_chat_by_name] target='12988' NOT FOUND after all 3 passes" in [
            rec.getMessage() for rec in self._caplog.records
        ]
        assert not any(
            "[get_chat_by_name] RESOLVED via contacts" in rec.getMessage()
            for rec in self._caplog.records
        )


# ========================================================================
# D. Regression guard — Phase 4.3 override pass still works
# ========================================================================
class TestOverridesPass0:
    def test_override_resolves_before_any_http_ladder(self):
        client = make_client(chat_name_overrides={TARGET: "Misc Name"})
        fake = FakeWaha(chat_list=[waha_chat("Misc Name")], search=[], contacts={})
        with fake.installed(client):
            result = client.get_chat_by_name(TARGET)
        assert result == {"id": {"server": "lid", "user": LID_USER,
                                 "_serialized": LID_CHAT},
                          "name": "Misc Name", "isGroup": False}
        assert fake.calls["contacts"] == 0


# ========================================================================
# Shared GUI test helpers
# ========================================================================
class _Var:
    """Minimal stand-in for a tkinter ``Variable`` (only ``.get()`` is used)."""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def _load_main():
    """Import the tkinter GUI module even on a headless machine without tkinter.

    The GUI tests below verify pure, tk-free logic (config sync, review-row
    typing, corrections state gating).  Rather than skipping them when tkinter
    is unavailable, install a *minimal* stub so the module imports.  When the
    real tkinter is present it is used untouched.

    Note: the old ``main.py`` was renamed to ``main_tk.py`` when the PyWebview
    UI (``main_web.py``) was introduced — the tkinter version lives there now.
    """
    import importlib
    import sys
    import types

    try:
        import tkinter  # noqa: F401
    except ImportError:
        tk = types.ModuleType("tkinter")

        class _Tk:  # tiny base class for OrderCartApp
            pass

        tk.Tk = _Tk
        for _name in ("StringVar", "DoubleVar", "BooleanVar"):
            setattr(tk, _name, _Var)
        tk.filedialog = types.ModuleType("tkinter.filedialog")
        tk.messagebox = types.ModuleType("tkinter.messagebox")
        tk.ttk = types.ModuleType("tkinter.ttk")

        def _noop(*_args, **_kwargs):
            return None

        tk.filedialog.asksaveasfilename = lambda *a, **k: ""
        tk.messagebox.showinfo = _noop
        tk.messagebox.showerror = _noop
        tk.messagebox.askyesno = lambda *a, **k: False

        sys.modules["tkinter"] = tk
        sys.modules["tkinter.filedialog"] = tk.filedialog
        sys.modules["tkinter.messagebox"] = tk.messagebox
        sys.modules["tkinter.ttk"] = tk.ttk

    return importlib.import_module("main_tk")


# ========================================================================
# E. Fix 3 — review rows must be plain tuples (TRUNC/FAILED crash guard)
# ========================================================================
class TestReviewRows:
    def test_summary_review_rows_returns_tuples(self):
        summary = RunSummary()
        summary.record(ProcessResult(12988, STATUS_OK, order_id="342022570640639296"))
        summary.record(ProcessResult(12989, STATUS_TRUNC, order_id="TRUNC:34187320244"))
        summary.record(ProcessResult(12992, STATUS_FAILED))
        summary.record(ProcessResult(12993, STATUS_MISSING))

        rows = summary.review_rows()
        assert rows == [
            (12989, STATUS_TRUNC, "34187320244"),
            (12992, STATUS_FAILED, ""),
        ]
        assert all(isinstance(row, tuple) and len(row) == 3 for row in rows)

    def test_populate_review_builds_tuples(self):
        """``_populate_review`` must hand ``_build_review_window`` tuples.

        Regression: it used to forward the raw ``ProcessResult`` objects, which
        the window then tried to unpack -> ``TypeError: cannot unpack
        non-iterable ProcessResult object`` on every TRUNC/FAILED batch.
        """
        main_module = _load_main()

        summary = RunSummary()
        summary.record(ProcessResult(12988, STATUS_OK, order_id="342022570640639296"))
        summary.record(ProcessResult(12989, STATUS_TRUNC, order_id="TRUNC:34187320244"))
        summary.record(ProcessResult(12992, STATUS_FAILED))

        app = main_module.OrderCartApp.__new__(main_module.OrderCartApp)
        app.append_log = lambda *a, **k: None
        captured: Dict[str, Any] = {}
        app._build_review_window = lambda items: captured.setdefault("items", list(items))

        app._populate_review(summary)

        assert captured["items"] == [
            (12989, STATUS_TRUNC, "34187320244"),
            (12992, STATUS_FAILED, ""),
        ]
        assert all(isinstance(x, tuple) and len(x) == 3 for x in captured["items"])


# ========================================================================
# F. Fix 4 — transient failures are retried, not persisted
# ========================================================================
def _mock_resp(status_code: int = 200, json_data=None, content: bytes = b"") -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if content else ""
    resp.json = Mock(return_value=[] if json_data is None else json_data)
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(str(status_code))
    return resp


class _MessagesErrorAPI:
    """Chat resolves, but fetching its messages raises."""

    def __init__(self, exc):
        self.exc = exc

    def get_chat_by_name(self, name):
        return {"id": f"chat-{name}", "name": str(name)}

    def get_messages(self, chat_id, limit=10):
        raise self.exc

    def download_media(self, msg):  # pragma: no cover - never reached
        raise RuntimeError("unexpected media call")

    def close(self):
        pass


class _MediaErrorAPI:
    """Messages arrive with an image, but every media download fails."""

    def get_chat_by_name(self, name):
        return {"id": f"chat-{name}", "name": str(name)}

    def get_messages(self, chat_id, limit=10):
        return [make_message("m1", image=True)]

    def download_media(self, msg):
        raise RuntimeError("media boom")

    def close(self):
        pass


class _Messages404API:
    def get_chat_by_name(self, name):
        return {"id": f"chat-{name}", "name": str(name)}

    def get_messages(self, chat_id, limit=10):
        raise ChatNotFoundError("404")

    def download_media(self, msg):  # pragma: no cover - never reached
        return b""

    def close(self):
        pass


def _build_orch(tmp_path, api, writer=None, state=None):
    return Orchestrator(
        api_client=api,
        ocr_engine=FakeOCR(),
        writer=writer if writer is not None else RecordingWriter(),
        state=state if state is not None else StateStore(str(tmp_path / "state.db")),
        rules=Rules.from_config(CONFIG),
        config=CONFIG,
        assets_dir=str(tmp_path / "assets"),
        logs_dir=str(tmp_path / "logs"),
        review_dir=str(tmp_path / "review"),
    )


class TestApiClientTransientRetries:
    def test_get_messages_retries_on_500(self):
        client = make_client(retry_backoff=0)
        seq = [_mock_resp(500), _mock_resp(200, json_data=[])]
        with patch.object(client.session, "get", side_effect=seq) as get:
            assert client.get_messages("66645292789868@lid", limit=5) == []
        assert get.call_count == 2

    def test_get_messages_retries_on_429(self):
        client = make_client(retry_backoff=0)
        seq = [_mock_resp(429), _mock_resp(200, json_data=[])]
        with patch.object(client.session, "get", side_effect=seq) as get:
            client.get_messages("66645292789868@lid")
        assert get.call_count == 2

    def test_download_media_retries_on_500(self):
        client = make_client(retry_backoff=0)
        msg = {
            "id": {"id": "m1"},
            "type": "image",
            "media": {"url": "http://localhost:3001/api/files/m1.jpg"},
        }
        seq = [_mock_resp(500), _mock_resp(200, content=b"jpeg")]
        with patch.object(client.session, "get", side_effect=seq) as get:
            data = client.download_media(msg)
        assert get.call_count == 2
        assert data == b"jpeg"

    def test_get_messages_gives_up_after_retries(self):
        client = make_client(retry_backoff=0)
        with patch.object(client.session, "get", return_value=_mock_resp(500)):
            with pytest.raises(WhatsAppAPIError):
                client.get_messages("66645292789868@lid")

    def test_404_is_not_retried(self):
        client = make_client(retry_backoff=0)
        with patch.object(client.session, "get", return_value=_mock_resp(404)) as get:
            with pytest.raises(ChatNotFoundError):
                client.get_messages("66645292789868@lid")
        assert get.call_count == 1


class TestTransientStatus:
    def test_message_fetch_error_is_transient_and_not_persisted(self, tmp_path):
        api = _MessagesErrorAPI(WhatsAppAPIError("500 boom"))
        writer = RecordingWriter()
        state = StateStore(str(tmp_path / "state.db"))
        orch = _build_orch(tmp_path, api, writer, state)

        summary = orch.run(12988, 12988)

        assert writer.rows == []
        assert state.is_processed(12988) is False
        assert summary.results[0].status == STATUS_TRANSIENT_ERROR
        assert summary.results[0].transient is True
        assert summary.transient == 1
        assert summary.processed == 0

    def test_media_download_error_is_transient_and_not_persisted(self, tmp_path):
        api = _MediaErrorAPI()
        writer = RecordingWriter()
        state = StateStore(str(tmp_path / "state.db"))
        orch = _build_orch(tmp_path, api, writer, state)

        summary = orch.run(12988, 12988)

        assert writer.rows == []
        assert state.is_processed(12988) is False
        assert summary.results[0].status == STATUS_TRANSIENT_ERROR

    def test_ocr_failure_is_persisted_as_failed(self, tmp_path):
        """A real read failure (valid image, nothing extracted) IS persisted."""
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"c1": [make_message("m1", image=True)]},
            media={"m1": b"nothing useful in this image"},
        )
        writer = RecordingWriter()
        state = StateStore(str(tmp_path / "state.db"))
        orch = _build_orch(tmp_path, api, writer, state)

        summary = orch.run(12988, 12988)

        assert len(writer.rows) == 1
        assert summary.failed == 1
        assert summary.results[0].status == STATUS_FAILED
        assert state.is_processed(12988) is True

    def test_nothing_extracted_raises_a_visible_error_line(self, tmp_path, caplog):
        """No id, no name and no address must be reported as an ERROR."""
        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"c1": [make_message("m1", image=True)]},
            media={"m1": b"nothing useful in this image"},
        )
        orch = _build_orch(tmp_path, api, RecordingWriter())
        logs: list[str] = []
        orch.log_callback = logs.append

        with caplog.at_level(logging.ERROR, logger="core.orchestrator"):
            orch.run(12988, 12988)

        assert any("ERROR nothing extracted" in line for line in logs)
        assert "event=nothing_extracted" in caplog.text
        assert "internal_id=12988" in caplog.text

    def test_master_list_missing_the_id_is_reported_not_appended(self, tmp_path):
        """The Notepad master list is authoritative: no "extra" id lines."""
        from output.writer import NotepadWriter  # local: only this test needs it

        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"c1": [make_message("m1", image=True)]},
            media={"m1": b"Order ID: 3420026566347011"},
        )
        notepad = tmp_path / "15-10-2026.txt"
        notepad.write_text("12328\t3420026566347011\n", encoding="utf-8")

        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=RecordingWriter(),
            notepad_writer=NotepadWriter(str(notepad), mode="fill"),
            state=StateStore(str(tmp_path / "state.db")),
            rules=Rules.from_config(CONFIG),
            config=CONFIG,
            assets_dir=str(tmp_path / "assets"),
            logs_dir=str(tmp_path / "logs"),
            review_dir=str(tmp_path / "review"),
        )
        logs: list[str] = []
        orch.log_callback = logs.append

        orch.run(12988, 12988)

        assert notepad.read_text(encoding="utf-8") == "12328\t3420026566347011\n"
        assert any("WARN not in the Notepad list" in line for line in logs)

    def test_multiple_orders_flag_the_row_in_the_app(self, tmp_path, caplog):
        """Several orders: the app flags the row, but never blocks the list."""

        class NoticeWriter:
            """Stands in for NotepadWriter's multi-order signal."""

            mode = "fill"
            last_notice = ""
            last_notice_kind = ""

            def has_line(self, _internal_id):
                return True

            def write_result(self, _internal_id, _result):
                self.last_notice_kind = "MULTI: names differ"
                self.last_notice = (
                    "MULTI: names differ - 4 orders in this chat "
                    "(the names differ); the list used the name 'Priya Sharma'"
                )
                return ""

        chat = {"id": "c1", "name": "12988"}
        api = FakeAPI(
            chats={"12988": chat},
            messages={"c1": [make_message("m1", image=True)]},
            media={"m1": b"Order ID: 3420026566347011"},
        )
        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=RecordingWriter(),
            notepad_writer=NoticeWriter(),
            state=StateStore(str(tmp_path / "state.db")),
            rules=Rules.from_config(CONFIG),
            config=CONFIG,
            assets_dir=str(tmp_path / "assets"),
            logs_dir=str(tmp_path / "logs"),
            review_dir=str(tmp_path / "review"),
        )
        logs: list[str] = []
        orch.log_callback = logs.append

        with caplog.at_level(logging.WARNING, logger="core.orchestrator"):
            summary = orch.run(12988, 12988)

        # Not an error: a warning, and the row is flagged for review.  The
        # warning is *typed*, so the seller knows which check is needed.
        assert any(
            "WARN" in line and "MULTI: names differ" in line for line in logs
        )
        assert not any("ERROR" in line for line in logs)
        assert "event=multi_order_flagged" in caplog.text
        assert "kind=MULTI: names differ" in caplog.text
        assert summary.results[0].ambiguous is True
        assert summary.results[0].review_reason == "MULTI: names differ"

    def test_missing_chat_leaves_its_row_blank_and_later_rows_stay_aligned(
        self, tmp_path
    ):
        """12989 has no chat -> its line stays blank; 12990 fills its own line."""
        import copy

        from output.writer import NotepadWriter  # local: only this test needs it

        # The shipped config.yaml keeps full ids (truncate_length 0); the shared
        # test CONFIG still truncates, which would hide the alignment check.
        config = copy.deepcopy(CONFIG)
        config["extraction"]["order_id_truncate_length"] = 0

        chats = {
            "12988": {"id": "c1", "name": "12988"},
            "12990": {"id": "c2", "name": "12990"},
        }
        api = FakeAPI(
            chats=chats,
            messages={
                "c1": [make_message("m1", image=True)],
                "c2": [make_message("m2", image=True)],
            },
            media={
                "m1": b"Order ID: 3420026566347011",
                "m2": b"Order ID: 3419993080845445",
            },
        )
        notepad = tmp_path / "15-10-2026.txt"
        notepad.write_text("12988\n12989\n12990\n", encoding="utf-8")

        orch = Orchestrator(
            api_client=api,
            ocr_engine=FakeOCR(),
            writer=RecordingWriter(),
            notepad_writer=NotepadWriter(str(notepad), mode="fill"),
            state=StateStore(str(tmp_path / "state.db")),
            rules=Rules.from_config(config),
            config=config,
            assets_dir=str(tmp_path / "assets"),
            logs_dir=str(tmp_path / "logs"),
            review_dir=str(tmp_path / "review"),
        )

        orch.run(12988, 12990)

        assert notepad.read_text(encoding="utf-8").splitlines() == [
            "12988\t3420026566347011",
            "12989",                        # missing chat: stays blank
            "12990\t3419993080845445",      # next chat fills its own line
        ]


# ========================================================================
# G. Fix 7 — a 404 on /messages is MISSING, not "processed"
# ========================================================================
class TestMessages404:
    def test_404_messages_counted_as_missing_not_processed(self, tmp_path):
        api = _Messages404API()
        writer = RecordingWriter()
        state = StateStore(str(tmp_path / "state.db"))
        orch = _build_orch(tmp_path, api, writer, state)

        summary = orch.run(12988, 12988)

        assert summary.missing == 1
        assert summary.processed == 0
        assert summary.results[0].skipped is True
        assert writer.rows == []
        assert state.is_processed(12988) is False


# ========================================================================
# H. Fix 8 — relaxed lookup is word-bounded, id.user substrings don't match
# ========================================================================
class TestRelaxedMatchWordBoundary:
    def test_relaxed_match_requires_word_boundary(self):
        client = make_client()
        chat = waha_chat("12988765 (Meesho)", user="999999999999")
        fake = FakeWaha(chat_list=[chat], search=[], contacts={})
        with fake.installed(client):
            assert client.get_chat_by_name("12988") is None

    def test_user_id_substring_does_not_match(self):
        client = make_client()
        chat = {
            "id": {"server": "lid", "user": "12988765",
                   "_serialized": "12988765@lid"},
            "name": "Different Name",
            "isGroup": False,
        }
        fake = FakeWaha(chat_list=[chat], search=[], contacts={})
        with fake.installed(client):
            assert client.get_chat_by_name("12988") is None

    def test_relaxed_match_still_matches_exact_token(self):
        client = make_client()
        chat = waha_chat("12988 (Meesho)", user="999999999999")
        fake = FakeWaha(chat_list=[chat], search=[], contacts={})
        with fake.installed(client):
            assert client.get_chat_by_name("12988") is chat


# ========================================================================
# I. Fix 9 — Orchestrator.close() releases the state DB too
# ========================================================================
class TestOrchestratorClose:
    def test_close_closes_state(self, tmp_path):
        class _State:
            def __init__(self):
                self.closed = False

            def is_processed(self, internal_id):
                return False

            def mark_processed(self, *a, **k):  # pragma: no cover
                pass

            def close(self):
                self.closed = True

        state = _State()
        orch = _build_orch(tmp_path, FakeAPI(), state=state)
        orch.close()
        assert state.closed is True


# ========================================================================
# J. Fix 5 / Fix 6 — GUI config + corrections state gating
# ========================================================================
class TestGuiConfigPersist:
    def test_gui_config_updates_persist(self, tmp_path, monkeypatch):
        main_module = _load_main()

        monkeypatch.setattr(main_module, "ROOT", str(tmp_path))
        app = main_module.OrderCartApp.__new__(main_module.OrderCartApp)
        app.config = {
            "whatsapp_api": {
                "provider": "waha",
                "base_url": "http://old",
                "api_key": "old",
                "session_name": "old",
            },
            "output": {"default_file": "orders.txt"},
        }
        app.provider_var = _Var("waha")
        app.url_var = _Var("http://localhost:3001")
        app.key_var = _Var("new-key")
        app.session_var = _Var("newsession")
        app.output_var = _Var(str(tmp_path / "new_orders.txt"))
        app.logger = logging.getLogger("frameflow.test")

        app._persist_config()

        assert app.config["whatsapp_api"]["base_url"] == "http://localhost:3001"
        assert app.config["whatsapp_api"]["api_key"] == "new-key"
        assert app.config["whatsapp_api"]["session_name"] == "newsession"
        assert app.config["output"]["default_file"] == "new_orders.txt"


class TestSaveCorrectionsGating:
    def _make_app(self, main_module):
        app = main_module.OrderCartApp.__new__(main_module.OrderCartApp)
        app._review_entries = {12989: _Var("341873202440000001")}
        app.output_var = _Var("orders.txt")
        app.format_var = _Var("notepad")
        app.config = {}
        app.folders = {"state": "state"}
        app.logger = logging.getLogger("frameflow.test")
        app.append_log = lambda *a, **k: None
        app.set_status = lambda *a, **k: None
        return app

    def test_zero_changes_does_not_touch_state(self, monkeypatch):
        main_module = _load_main()
        from output import corrections as corrections_module
        import state.state as state_module

        app = self._make_app(main_module)
        monkeypatch.setattr(corrections_module, "apply_corrections", lambda *a, **k: 0)
        store = Mock()
        store_cls = Mock(return_value=store)
        monkeypatch.setattr(state_module, "StateStore", store_cls)
        monkeypatch.setattr(main_module.messagebox, "showinfo", lambda *a, **k: None)

        app._save_corrections()

        store_cls.assert_not_called()
        store.mark_processed.assert_not_called()

    def test_changes_do_mark_processed(self, monkeypatch):
        main_module = _load_main()
        from output import corrections as corrections_module
        import state.state as state_module

        app = self._make_app(main_module)
        monkeypatch.setattr(corrections_module, "apply_corrections", lambda *a, **k: 1)
        store = Mock()
        store_cls = Mock(return_value=store)
        monkeypatch.setattr(state_module, "StateStore", store_cls)
        monkeypatch.setattr(main_module.messagebox, "showinfo", lambda *a, **k: None)

        app._save_corrections()

        store.mark_processed.assert_called_once()
        store.close.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
