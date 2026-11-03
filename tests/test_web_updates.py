"""
tests/test_web_updates.py
=========================

The bridge side of the online update check: ``Api.check_updates`` /
``Api.apply_update`` / ``Api.open_repo`` and the ``setUpdateInfo(...)`` push.

``updater`` itself is covered offline in ``tests/test_updater.py``; here the
network call is replaced so the tests only assert the wiring (cache, config,
running-run guard, push channel).
"""

from __future__ import annotations

import main_web


class FakeWindow:
    def __init__(self) -> None:
        self.snippets = []

    def evaluate_js(self, snippet):  # pragma: no cover - trivial
        self.snippets.append(snippet)


def make_api(tmp_path, monkeypatch):
    monkeypatch.setattr(main_web, "ROOT", str(tmp_path))
    return main_web.Api()


def fake_result(**overrides):
    result = {
        "ok": True,
        "update_available": False,
        "current": "a" * 40,
        "current_short": "aaaaaaa",
        "latest": "a" * 40,
        "latest_short": "aaaaaaa",
        "message": "Up to date",
        "branch": "main",
        "checked_at": "2026-11-03 10:00:00",
    }
    result.update(overrides)
    return result


class TestCheckUpdates:
    def test_settings_carry_the_version_and_update_preferences(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)

        cfg = api.get_settings()

        assert cfg["version"] == main_web.APP_VERSION
        assert cfg["update"]["repo"] == "Annku-z00/OrderCart"
        assert cfg["update"]["branch"] == "main"
        assert cfg["update"]["enabled"] is True

    def test_a_disabled_check_answers_quietly(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        api.config["update"] = {"enabled": False}

        result = api.check_updates()

        assert result["disabled"] is True
        assert result["version"] == main_web.APP_VERSION

    def test_the_result_is_pushed_to_the_ui(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        window = FakeWindow()
        api.bind_window(window)
        monkeypatch.setattr(
            main_web, "check_for_update", lambda **kwargs: fake_result()
        )

        api.check_updates(True)

        assert any("setUpdateInfo(" in snippet for snippet in window.snippets)
        assert any("aaaaaaa" in snippet for snippet in window.snippets)

    def test_a_second_call_reuses_the_cached_result(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        calls = []

        def fake(**kwargs):
            calls.append(kwargs)
            return fake_result()

        monkeypatch.setattr(main_web, "check_for_update", fake)

        api.check_updates()
        api.check_updates()

        assert len(calls) == 1

    def test_force_asks_github_again(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        calls = []

        def fake(**kwargs):
            calls.append(kwargs)
            return fake_result()

        monkeypatch.setattr(main_web, "check_for_update", fake)

        api.check_updates()
        api.check_updates(True)

        assert len(calls) == 2

    def test_two_simultaneous_calls_share_one_request(self, tmp_path, monkeypatch):
        """The startup check is fired twice by pywebview - ask GitHub once."""
        import threading

        api = make_api(tmp_path, monkeypatch)
        calls = []
        started = threading.Event()
        release = threading.Event()

        def fake(**kwargs):
            calls.append(kwargs)
            started.set()
            release.wait(2)
            return fake_result()

        monkeypatch.setattr(main_web, "check_for_update", fake)

        worker = threading.Thread(target=api.check_updates, daemon=True)
        worker.start()
        assert started.wait(2)
        release.set()
        api.check_updates()
        worker.join(2)

        assert len(calls) == 1

    def test_the_config_repo_and_branch_reach_the_check(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        api.config["update"] = {"repo": "someone/fork", "branch": "dev"}
        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return fake_result()

        monkeypatch.setattr(main_web, "check_for_update", fake)
        api.check_updates(True)

        assert seen["repo"] == "someone/fork"
        assert seen["branch"] == "dev"
        assert seen["root"] == str(tmp_path)


class TestApplyUpdate:
    def test_it_is_refused_while_a_run_is_active(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        api._running.set()

        result = api.apply_update()

        assert result["ok"] is False
        assert "Stop the current run" in result["error"]

    def test_it_pulls_the_configured_branch(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        api.config["update"] = {"branch": "main"}
        seen = {}

        def fake_apply(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "restart": True}

        monkeypatch.setattr(main_web, "apply_git_update", fake_apply)

        result = api.apply_update()

        assert result["ok"] is True
        assert seen["root"] == str(tmp_path)
        assert seen["branch"] == "main"


class TestOpenRepo:
    def test_it_opens_the_configured_repository(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        api.config["update"] = {"repo": "Annku-z00/OrderCart"}
        opened = []
        monkeypatch.setattr(
            main_web.os, "startfile", lambda url: opened.append(url), raising=False
        )

        result = api.open_repo()

        assert result["ok"] is True
        assert opened == ["https://github.com/Annku-z00/OrderCart"]


class TestUpdateUi:
    """The script and the page must agree on every id the banner touches."""

    ELEMENTS = (
        "update-banner",
        "update-banner-text",
        "update-banner-apply",
        "update-banner-notes",
        "update-banner-close",
        "btn-check-updates",
        "btn-apply-update",
        "btn-open-github",
        "update-current",
        "update-latest",
        "update-status",
        "updates-msg",
        "app-version",
    )

    def test_every_update_element_exists_in_both_files(self):
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ui", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        with open(os.path.join(root, "ui", "app.js"), encoding="utf-8") as fh:
            script = fh.read()

        for element in self.ELEMENTS:
            assert f'id="{element}"' in html, element
            assert f'"{element}"' in script, element

    def test_the_python_side_pushes_the_update_payload(self, tmp_path, monkeypatch):
        api = make_api(tmp_path, monkeypatch)
        window = FakeWindow()
        api.bind_window(window)

        api._push_update({"ok": True, "latest_short": "abc1234"})

        assert len(window.snippets) == 1
        assert window.snippets[0].startswith("setUpdateInfo(")
        assert "abc1234" in window.snippets[0]
