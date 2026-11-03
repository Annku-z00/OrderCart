"""
tests/test_multi_account.py
===========================

Collecting orders from **several WhatsApp accounts** in one run.

* every account (every WAHA session) is scanned independently — an internal
  id that was already done for one account is still scanned for the next;
* the resume state is namespaced per account, so the accounts never block
  each other;
* the old single-column state table is migrated on the fly;
* the account manager lists the configured accounts next to the live WAHA
  sessions and persists which ones are ticked.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main_web
from state.state import StateStore

# ---------------------------------------------------------------------------
# The scoped resume state
# ---------------------------------------------------------------------------
class TestScopedState:
    def test_an_id_is_scanned_again_for_another_account(self, tmp_path):
        """Account A already did 12988 — account B still scans it."""
        path = str(tmp_path / "state.db")
        account_a = StateStore(path, scope="default")
        account_b = StateStore(path, scope="sales1")

        assert account_a.is_processed(12988) is False
        account_a.mark_processed(12988, {"type": "OK"})
        assert account_a.is_processed(12988) is True
        # The other account has its own slice of the state.
        assert account_b.is_processed(12988) is False

        account_b.mark_processed(12988, {"type": "OK"})
        assert account_b.is_processed(12988) is True
        assert account_a.is_processed(12988) is True

    def test_the_scope_is_listed(self, tmp_path):
        path = str(tmp_path / "state.db")
        StateStore(path, scope="default").mark_processed(1)
        StateStore(path, scope="sales1").mark_processed(2)
        store = StateStore(path)
        assert store.scopes() == ["default", "sales1"]

    def test_the_legacy_table_is_migrated(self, tmp_path):
        """An old state.db (no scope column) keeps its rows."""
        path = str(tmp_path / "state.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE processed (id INTEGER PRIMARY KEY, "
            "result_json TEXT, timestamp TEXT)"
        )
        conn.execute(
            "INSERT INTO processed (id, result_json, timestamp) VALUES (?, ?, ?)",
            (12988, '{"type": "OK"}', "2026-10-21 10:00:00"),
        )
        conn.commit()
        conn.close()

        store = StateStore(path)
        assert store.is_processed(12988) is True
        assert store.get_result(12988) == {"type": "OK"}
        # The rows moved into the default scope.
        assert store.scopes() == [""]


# ---------------------------------------------------------------------------
# The account manager
# ---------------------------------------------------------------------------
class TestAccountManager:
    def _api(self, tmp_path, monkeypatch, accounts=None):
        monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
        api = main_web.Api()
        api.config.setdefault("whatsapp_api", {})
        if accounts is not None:
            api.config["whatsapp_api"]["accounts"] = accounts
        return api

    def test_a_config_without_accounts_still_works(self, tmp_path, monkeypatch):
        """The old single-account config keeps working."""
        api = self._api(tmp_path, monkeypatch)
        api.config["whatsapp_api"].pop("accounts", None)
        api.config["whatsapp_api"]["session_name"] = "default"

        assert api._configured_accounts() == [
            {"name": "default", "label": "default", "enabled": True}
        ]
        assert len(api._enabled_accounts()) == 1

    def test_the_ticked_accounts_are_used(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch, accounts=[
            {"name": "default", "label": "Personal", "enabled": True},
            {"name": "sales1", "label": "Shop A", "enabled": False},
            {"name": "sales2", "label": "Shop B", "enabled": True},
        ])
        enabled = api._enabled_accounts()
        assert [a["name"] for a in enabled] == ["default", "sales2"]

    def test_saving_the_accounts_persists_them(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.save_whatsapp_accounts([
            {"name": "default", "label": "Personal", "enabled": True},
            {"name": "sales1", "label": "Shop A", "enabled": False},
        ])
        assert result["ok"] is True
        assert api.config["whatsapp_api"]["accounts"][1]["label"] == "Shop A"
        # The first ticked account becomes the default session.
        assert api.config["whatsapp_api"]["session_name"] == "default"

    def test_saving_accepts_a_json_string(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.save_whatsapp_accounts(
            '[{"name": "sales1", "label": "Shop A", "enabled": true}]'
        )
        assert result["ok"] is True
        assert api.config["whatsapp_api"]["accounts"][0]["name"] == "sales1"

    def test_an_empty_account_list_is_refused(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.save_whatsapp_accounts([])
        assert result["ok"] is False

    def test_a_duplicate_account_is_dropped(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch)
        result = api.save_whatsapp_accounts([
            {"name": "default", "label": "A", "enabled": True},
            {"name": "default", "label": "B", "enabled": True},
        ])
        assert result["ok"] is True
        assert len(result["accounts"]) == 1

    def test_the_list_merges_the_config_and_the_server(self, tmp_path, monkeypatch):
        """A session the server knows but the config does not is offered."""
        api = self._api(tmp_path, monkeypatch, accounts=[
            {"name": "default", "label": "Personal", "enabled": True},
        ])

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            @classmethod
            def from_config(cls, config, session_name=None):
                return cls()

            def list_sessions(self):
                return [
                    {"name": "default", "status": "WORKING", "me": "Andro"},
                    {"name": "sales1", "status": "SCAN_QR_CODE", "me": ""},
                ]

            def close(self):
                pass

        import whatsapp.api_client as api_client

        monkeypatch.setattr(api_client, "WhatsAppAPIClient", FakeClient)

        info = api.get_whatsapp_accounts()
        names = [a["name"] for a in info["accounts"]]
        assert "default" in names
        assert "sales1" in names

        by_name = {a["name"]: a for a in info["accounts"]}
        assert by_name["default"]["status"] == "WORKING"
        assert by_name["default"]["enabled"] is True
        # A server session the config does not list is not ticked yet.
        assert by_name["sales1"]["enabled"] is False


# ---------------------------------------------------------------------------
# The run itself visits every account
# ---------------------------------------------------------------------------
class TestRunOverSeveralAccounts:
    def _api(self, tmp_path, monkeypatch, accounts):
        monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
        api = main_web.Api()
        api.config.setdefault("whatsapp_api", {})
        api.config["whatsapp_api"]["accounts"] = accounts
        return api

    def test_every_ticked_account_is_scanned(self, tmp_path, monkeypatch):
        """The worker walks the ticked accounts, one run each."""
        api = self._api(tmp_path, monkeypatch, [
            {"name": "default", "label": "Personal", "enabled": True},
            {"name": "sales1", "label": "Shop A", "enabled": True},
            {"name": "sales2", "label": "Shop B", "enabled": False},
        ])
        monkeypatch.setattr(
            api, "_session_statuses",
            lambda: {"default": "WORKING", "sales1": "WORKING"},
        )

        visited = []

        def fake_run_one(account, start, end, output, output_format, dry_run, reprocess):
            visited.append(account["name"])

        monkeypatch.setattr(api, "_run_one_account", fake_run_one)
        api._worker_main(12988, 12990, "orders.csv", "csv", True, False)

        assert visited == ["default", "sales1"]

    def test_an_account_that_is_not_linked_is_skipped(self, tmp_path, monkeypatch):
        """An account that still waits for its QR scan must not fail the run."""
        api = self._api(tmp_path, monkeypatch, [
            {"name": "default", "label": "Personal", "enabled": True},
            {"name": "sales1", "label": "Shop A", "enabled": True},
        ])
        monkeypatch.setattr(
            api, "_session_statuses",
            lambda: {"default": "WORKING", "sales1": "SCAN_QR_CODE"},
        )

        visited = []
        monkeypatch.setattr(
            api, "_run_one_account",
            lambda account, *a: visited.append(account["name"]),
        )
        api._worker_main(12988, 12990, "orders.csv", "csv", True, False)

        # Only the linked account was scanned.
        assert visited == ["default"]

    def test_nothing_is_scanned_when_no_account_is_ticked(self, tmp_path, monkeypatch):
        api = self._api(tmp_path, monkeypatch, [
            {"name": "default", "label": "Personal", "enabled": False},
        ])
        visited = []
        monkeypatch.setattr(
            api, "_run_one_account",
            lambda account, *a: visited.append(account["name"]),
        )
        api._worker_main(12988, 12990, "orders.csv", "csv", True, False)
        assert visited == []
