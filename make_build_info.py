"""Stamp ``build_info.json`` with the commit (and version) of this build.

Run by ``build.bat`` before PyInstaller, and harmless to run by hand:

    py -3.12 make_build_info.py

The file is copied next to ``OrderCart.exe`` so the in-app update check
(:mod:`updater`) can tell an installed build from the current GitHub head
without shipping the whole ``.git`` folder.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))


def _git(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception:
        return ""
    return (completed.stdout or "").strip() if completed.returncode == 0 else ""


def _version() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def main() -> None:
    info = {
        "version": _version(),
        "commit": _git("rev-parse", "HEAD"),
        "commit_short": _git("rev-parse", "--short", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = os.path.join(ROOT, "build_info.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(info, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print("wrote", path)
    for key in ("version", "commit", "branch"):
        print(f"  {key:8s} {info[key] or '(unknown)'}")


if __name__ == "__main__":
    main()
