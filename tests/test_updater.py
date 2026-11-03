"""
tests/test_updater.py
=====================

The online update check (:mod:`updater`).

Everything here runs offline: the git runner and the GitHub fetch are injected,
so the tests cover the comparison logic, the "cannot know" cases and the
"built copy cannot git pull" guard without touching the network.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import updater

REMOTE_SHA = "b" * 40
LOCAL_SHA = "a" * 40


def _build_info(root: str, commit: str) -> None:
    with open(os.path.join(root, "build_info.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": "1.1.0", "commit": commit}, fh)


def _payload(sha: str = REMOTE_SHA, message: str = "Fix the notepad fill") -> dict:
    return {
        "sha": sha,
        "commit": {
            "message": message + "\n\nlonger body",
            "author": {"date": "2026-11-03T10:00:00Z"},
        },
        "html_url": "https://github.com/Annku-z00/OrderCart/commit/" + sha,
    }


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestVersion:
    def test_version_file_wins(self, tmp_path):
        (tmp_path / "VERSION").write_text("2.3.4\n", encoding="utf-8")
        assert updater.app_version(str(tmp_path)) == "2.3.4"

    def test_missing_version_file_is_not_an_error(self, tmp_path):
        assert updater.app_version(str(tmp_path)) == "0.0.0"


class TestCurrentCommit:
    def test_build_info_is_the_stamp_of_a_built_copy(self, tmp_path):
        _build_info(str(tmp_path), LOCAL_SHA)

        def explode(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("git must not run when build_info.json exists")

        assert updater.current_commit(str(tmp_path), run=explode) == LOCAL_SHA

    def test_a_clone_falls_back_to_git(self, tmp_path):
        os.makedirs(os.path.join(str(tmp_path), ".git"))
        calls = []

        def run(args, cwd, timeout):
            calls.append(args)
            return 0, LOCAL_SHA, ""

        assert updater.current_commit(str(tmp_path), run=run) == LOCAL_SHA
        assert calls == [["rev-parse", "HEAD"]]

    def test_a_clone_ignores_a_stale_build_stamp(self, tmp_path):
        """A source checkout always trusts HEAD over a leftover build stamp."""
        os.makedirs(os.path.join(str(tmp_path), ".git"))
        _build_info(str(tmp_path), "c" * 40)

        assert updater.current_commit(
            str(tmp_path), run=lambda *a: (0, LOCAL_SHA, "")
        ) == LOCAL_SHA

    def test_unknown_without_stamp_or_git(self, tmp_path):
        assert updater.current_commit(str(tmp_path), run=None) == ""

    def test_git_error_means_unknown(self, tmp_path):
        os.makedirs(os.path.join(str(tmp_path), ".git"))
        assert updater.current_commit(
            str(tmp_path), run=lambda *a: (128, "", "not a repo")
        ) == ""


class TestCheck:
    def test_reports_an_available_update(self, tmp_path):
        _build_info(str(tmp_path), LOCAL_SHA)

        result = updater.check_for_update(
            root=str(tmp_path), fetch=lambda repo, branch, timeout: _payload()
        )

        assert result["ok"] is True
        assert result["update_available"] is True
        assert result["current"] == LOCAL_SHA
        assert result["latest"] == REMOTE_SHA
        assert result["latest_short"] == "bbbbbbb"
        assert result["message"] == "Fix the notepad fill"
        assert result["published"] == "2026-11-03T10:00:00Z"
        assert result["can_update"] is False          # no .git
        assert result["url"] == "https://github.com/Annku-z00/OrderCart"

    def test_up_to_date(self, tmp_path):
        _build_info(str(tmp_path), REMOTE_SHA)

        result = updater.check_for_update(
            root=str(tmp_path), fetch=lambda *a: _payload()
        )

        assert result["ok"] is True
        assert result["update_available"] is False

    def test_a_build_without_a_stamp_never_claims_an_update(self, tmp_path):
        result = updater.check_for_update(
            root=str(tmp_path), fetch=lambda *a: _payload()
        )

        assert result["ok"] is True
        assert result["update_available"] is False
        assert "no commit stamp" in result["note"]

    def test_network_failure_is_reported_not_raised(self, tmp_path):
        def boom(*args):
            raise OSError("no internet")

        result = updater.check_for_update(root=str(tmp_path), fetch=boom)

        assert result["ok"] is False
        assert "cannot reach GitHub" in result["error"]

    def test_a_payload_without_a_sha_is_an_error(self, tmp_path):
        result = updater.check_for_update(
            root=str(tmp_path), fetch=lambda *a: {"commit": {}}
        )

        assert result["ok"] is False
        assert "no commit" in result["error"]

    def test_a_clone_can_update(self, tmp_path):
        _build_info(str(tmp_path), LOCAL_SHA)
        os.makedirs(os.path.join(str(tmp_path), ".git"))

        result = updater.check_for_update(
            root=str(tmp_path), fetch=lambda *a: _payload()
        )

        assert result["can_update"] is True

    def test_a_bad_repo_or_branch_falls_back_to_the_defaults(self, tmp_path):
        seen = {}

        def fetch(repo, branch, timeout):
            seen["repo"], seen["branch"] = repo, branch
            return _payload()

        updater.check_for_update(
            repo="../../etc", branch="--upload-pack=x", root=str(tmp_path), fetch=fetch
        )

        assert seen["repo"] == updater.DEFAULT_REPO
        assert seen["branch"] == updater.DEFAULT_BRANCH

    def test_the_default_repo_points_at_the_project(self):
        assert updater.DEFAULT_REPO == "Annku-z00/OrderCart"
        assert updater.repo_url() == "https://github.com/Annku-z00/OrderCart"


class TestGithubFetch:
    def test_it_reads_the_commit_json(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["agent"] = request.get_header("User-agent")
            return _FakeResponse(_payload())

        payload = updater.github_commit("Annku-z00/OrderCart", "main", 5, opener=opener)

        assert payload["sha"] == REMOTE_SHA
        assert seen["url"].endswith("/repos/Annku-z00/OrderCart/commits/main")
        assert seen["agent"].startswith("OrderCart/")


class TestApplyUpdate:
    def test_a_built_copy_is_pointed_at_github(self, tmp_path):
        result = updater.apply_update(root=str(tmp_path))

        assert result["ok"] is False
        assert result["needs_reinstall"] is True
        assert result["url"].endswith("Annku-z00/OrderCart")

    def test_a_clone_is_pulled(self, tmp_path):
        os.makedirs(os.path.join(str(tmp_path), ".git"))
        calls = []

        def run(args, cwd, timeout):
            calls.append((args, cwd))
            return 0, "Already up to date.", ""

        result = updater.apply_update(root=str(tmp_path), branch="main", run=run)

        assert result["ok"] is True
        assert result["restart"] is True
        assert calls == [(["pull", "--ff-only", "origin", "main"], str(tmp_path))]

    def test_a_failed_pull_reports_gits_message(self, tmp_path):
        os.makedirs(os.path.join(str(tmp_path), ".git"))

        result = updater.apply_update(
            root=str(tmp_path),
            run=lambda *a: (1, "", "error: Your local changes would be overwritten"),
        )

        assert result["ok"] is False
        assert "local changes" in result["error"]


class TestGitRunner:
    def test_a_missing_git_binary_is_reported(self, monkeypatch):
        def boom(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(updater.subprocess, "run", boom)

        code, out, err = updater._run_git(["rev-parse", "HEAD"], ".", 5)

        assert code == 127
        assert out == ""
        assert "not installed" in err

    def test_a_timeout_is_reported(self, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(updater.subprocess, "run", boom)

        code, _out, err = updater._run_git(["pull"], ".", 5)

        assert code == 124
        assert "timed out" in err


@pytest.mark.parametrize("value", ["", None, "  ", "evil;rm -rf /"])
def test_a_bad_branch_never_reaches_git_as_an_option(value):
    # Branch names are validated before they are passed to git as an argument.
    assert not updater._clean_branch(value).startswith("-")
