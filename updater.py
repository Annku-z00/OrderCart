"""
updater.py
==========

Online update check for OrderCart, driven by the public GitHub repository.

The seller's install is one of two things:

* a **git clone** of the repository (the developer / "run from source" case),
* a **PyInstaller build** that carries a ``build_info.json`` written at build
  time by ``make_build_info.py`` (see ``build.bat``).

Both record the commit they came from, so the check is a single comparison
with the newest commit on ``main`` at GitHub.  No token is needed for a public
repository, nothing is downloaded during the check, and pressing **Update now**
only ever runs ``git pull --ff-only`` — a build that was not cloned cannot be
patched in place and is pointed at the download page instead.

Every network / git problem is reported as ``{"ok": False, "error": ...}``;
nothing here raises into the GUI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

#: The public repository the app checks and updates from.
DEFAULT_REPO = "Annku-z00/OrderCart"
DEFAULT_BRANCH = "main"

#: Where the commits list lives (public: 60 requests/hour per IP, no token).
_COMMITS_API = "https://api.github.com/repos/{repo}/commits/{branch}"

#: A short, human-sized commit id.
_SHORT = 7

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: ``subprocess`` flags that keep a console window from flashing on Windows.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

Runner = Callable[[list, str, int], Tuple[int, str, str]]
Fetcher = Callable[[str, str, int], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    """Local timestamp for ``checked_at`` (ISO-ish, seconds)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short(commit: str) -> str:
    return commit[:_SHORT] if commit else ""


def _default_root() -> str:
    """The folder ``VERSION`` / ``build_info.json`` / ``.git`` live in."""
    if getattr(sys, "frozen", False):  # PyInstaller: next to the .exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def app_version(root: Optional[str] = None) -> str:
    """The installed version (``VERSION`` file), e.g. ``"1.1.0"``."""
    path = os.path.join(root or _default_root(), "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except Exception:
        value = ""
    return value or "0.0.0"


def _read_build_info(root: str) -> Dict[str, Any]:
    """``build_info.json`` as a dict (empty when missing or malformed)."""
    path = os.path.join(root, "build_info.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _run_git(args: list, cwd: str, timeout: int = 15) -> Tuple[int, str, str]:
    """Run ``git <args>`` and return ``(returncode, stdout, stderr)``.

    Never raises: a missing git, a timeout and a permission problem all become
    a non-zero return code with a readable message.
    """
    try:
        completed = subprocess.run(
            ["git", *[str(arg) for arg in args]],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return (
            completed.returncode,
            (completed.stdout or "").strip(),
            (completed.stderr or "").strip(),
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)


def _clean_repo(repo: Optional[str]) -> str:
    value = str(repo or DEFAULT_REPO).strip().strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value if _REPO_RE.match(value) else DEFAULT_REPO


def _clean_branch(branch: Optional[str]) -> str:
    value = str(branch or DEFAULT_BRANCH).strip()
    if not value or ".." in value or value.startswith("-"):
        return DEFAULT_BRANCH
    return value if _BRANCH_RE.match(value) else DEFAULT_BRANCH


def repo_url(repo: Optional[str] = None) -> str:
    """``https://github.com/<owner>/<name>`` for the given repo."""
    return "https://github.com/" + _clean_repo(repo)


# ---------------------------------------------------------------------------
# Current / latest commit
# ---------------------------------------------------------------------------
def current_commit(
    root: Optional[str] = None, run: Optional[Runner] = None
) -> str:
    """The commit this install came from ("" when it cannot be known).

    ``build_info.json`` wins — it is the only stamp a PyInstaller build has.
    A source checkout falls back to ``git rev-parse HEAD``.
    """
    root = root or _default_root()
    stamped = str(_read_build_info(root).get("commit") or "").strip()
    if stamped:
        return stamped
    if not os.path.isdir(os.path.join(root, ".git")):
        return ""
    runner = run or _run_git
    code, out, _err = runner(["rev-parse", "HEAD"], root, 10)
    return out if code == 0 and out else ""


def is_git_checkout(root: Optional[str] = None) -> bool:
    """True when ``Update now`` can pull into this folder."""
    return os.path.isdir(os.path.join(root or _default_root(), ".git"))


def github_commit(
    repo: str,
    branch: str,
    timeout: int = 8,
    opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """The newest commit on ``branch`` as GitHub reports it.

    ``opener`` is injected by the tests; the default is
    ``urllib.request.urlopen``.  Raises on any network/HTTP problem — the
    caller turns that into an ``ok: False`` result.
    """
    url = _COMMITS_API.format(
        repo=_clean_repo(repo),
        branch=urllib.parse.quote(_clean_branch(branch), safe=""),
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "OrderCart/" + app_version(),
            "Accept": "application/vnd.github+json",
        },
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("sha"):
        raise ValueError("GitHub returned no commit")
    return payload


def _commit_message(payload: Dict[str, Any]) -> str:
    commit = payload.get("commit") or {}
    message = str(commit.get("message") or "").strip()
    return message.splitlines()[0] if message else ""


def _commit_date(payload: Dict[str, Any]) -> str:
    commit = payload.get("commit") or {}
    author = commit.get("author") or commit.get("committer") or {}
    return str(author.get("date") or "")


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------
def check_for_update(
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    root: Optional[str] = None,
    timeout: int = 8,
    fetch: Optional[Fetcher] = None,
    run: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Compare the installed commit with the newest one on GitHub.

    Returns a JSON-safe dict.  ``update_available`` is only true when the
    installed commit is known *and* differs — a build without a stamp says so
    instead of claiming an update it cannot verify.
    """
    repo = _clean_repo(repo)
    branch = _clean_branch(branch)
    root = root or _default_root()
    current = current_commit(root, run=run)
    result: Dict[str, Any] = {
        "ok": False,
        "repo": repo,
        "branch": branch,
        "url": repo_url(repo),
        "version": app_version(root),
        "current": current,
        "current_short": _short(current),
        "can_update": is_git_checkout(root),
        "checked_at": _now(),
    }

    try:
        payload = (fetch or github_commit)(repo, branch, timeout)
    except Exception as exc:
        result["error"] = f"cannot reach GitHub ({type(exc).__name__}: {exc})"
        return result

    latest = str(payload.get("sha") or "").strip()
    if not latest:
        result["error"] = "GitHub returned no commit"
        return result

    result.update({
        "ok": True,
        "latest": latest,
        "latest_short": _short(latest),
        "message": _commit_message(payload),
        "published": _commit_date(payload),
        "update_available": bool(current) and current != latest,
    })
    if not current:
        result["note"] = (
            "this build has no commit stamp - rebuild it to enable updates"
        )
    return result


# ---------------------------------------------------------------------------
# Apply (git only)
# ---------------------------------------------------------------------------
def apply_update(
    root: Optional[str] = None,
    branch: Optional[str] = None,
    timeout: int = 300,
    run: Optional[Runner] = None,
) -> Dict[str, Any]:
    """``git pull --ff-only`` the configured branch into ``root``.

    A build that was not cloned from GitHub cannot be updated this way; the
    result then points the seller at the repository instead of half-patching
    the folder.  The caller restarts the app afterwards.
    """
    root = root or _default_root()
    branch = _clean_branch(branch)
    if not is_git_checkout(root):
        return {
            "ok": False,
            "needs_reinstall": True,
            "url": repo_url(),
            "error": (
                "This copy was installed from a build, not cloned with git - "
                "download the newest release from GitHub instead."
            ),
        }

    runner = run or _run_git
    code, out, err = runner(["pull", "--ff-only", "origin", branch], root, timeout)
    combined = "\n".join(part for part in (out, err) if part).strip()
    if code != 0:
        return {
            "ok": False,
            "error": combined or f"git pull failed (exit {code})",
            "output": combined,
        }
    return {
        "ok": True,
        "branch": branch,
        "output": combined,
        "restart": True,
    }
