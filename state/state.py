"""
state/state.py
==============

SQLite-backed resume state.

Schema::

    CREATE TABLE processed (
        scope       TEXT NOT NULL DEFAULT '',
        id          INTEGER NOT NULL,
        result_json TEXT,
        timestamp   TEXT,
        PRIMARY KEY (scope, id)
    );

Contract (from the spec)
------------------------
* ``is_processed(id)`` is checked **before** doing any work for an id.
* ``mark_processed(...)`` is called **after** the output row is written — never
  before — so a crash between writing the output and updating state only ever
  causes a safe *re-process*, not a lost row.
* When the user ticks "Reprocess done?", the caller simply ignores
  :meth:`is_processed` and re-runs; this store still lets the new row overwrite
  the old state (``INSERT OR REPLACE``).

The **scope**
-------------
The seller can collect from several WhatsApp accounts in one run.  Each
account (each WAHA session) gets its own *scope*, so an internal id that was
already processed for one account is still scanned for the next one — the
accounts stay independent.  A store created without a scope keeps the plain
behaviour, and the old single-column table is migrated on the fly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    scope       TEXT NOT NULL DEFAULT '',
    id          INTEGER NOT NULL,
    result_json TEXT,
    timestamp   TEXT,
    PRIMARY KEY (scope, id)
);
"""

#: The table this module used before the scope was introduced.
_LEGACY_TABLE = "processed_legacy"


class StateStore:
    """Thread-safe SQLite resume store."""

    def __init__(self, path: str = "state/state.db", scope: str = "") -> None:
        self.path = path          # may be ":memory:"
        #: The scope this store reads and writes — the WhatsApp account
        #: (WAHA session) the ids belong to.  Empty = the default scope.
        self.scope = str(scope or "")
        self._lock = threading.RLock()
        self._ensure_parent()
        # check_same_thread=False lets the GUI thread + worker thread share it.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate_legacy()
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    def _ensure_parent(self) -> None:
        if self.path == ":memory:":
            return
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _migrate_legacy(self) -> None:
        """Bring an old single-column ``processed`` table up to date.

        The old table keyed the rows by the internal id alone.  The new one
        keys them by ``(scope, id)``.  The old rows move into the default
        scope, so nothing is lost.
        """
        try:
            cur = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='processed'"
            )
            if cur.fetchone() is None:
                return                      # no table yet -> the new one is created
            cur = self._conn.execute("PRAGMA table_info(processed)")
            columns = {row["name"] for row in cur.fetchall()}
            if "scope" in columns:
                return                      # already the new schema
            logger.info("Migrating the state DB to the scoped schema")
            self._conn.execute(
                f"ALTER TABLE processed RENAME TO {_LEGACY_TABLE}"
            )
            self._conn.execute(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO processed "
                "(scope, id, result_json, timestamp) "
                f"SELECT '', id, result_json, timestamp FROM {_LEGACY_TABLE}"
            )
            self._conn.execute(f"DROP TABLE {_LEGACY_TABLE}")
            self._conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not migrate the state DB: %s", exc)

    # ------------------------------------------------------------------
    def is_processed(self, internal_id: int) -> bool:
        """True when ``internal_id`` already has a stored result."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM processed WHERE scope = ? AND id = ? LIMIT 1",
                (self.scope, int(internal_id)),
            )
            return cur.fetchone() is not None

    def get_result(self, internal_id: int) -> Optional[Dict[str, Any]]:
        """Return the stored result dict for ``internal_id`` (or ``None``)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT result_json FROM processed WHERE scope = ? AND id = ?",
                (self.scope, int(internal_id)),
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def mark_processed(
        self,
        internal_id: int,
        result: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """Record that ``internal_id`` has been fully processed."""
        payload = json.dumps(result or {}, ensure_ascii=False)
        ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO processed "
                "(scope, id, result_json, timestamp) VALUES (?, ?, ?, ?)",
                (self.scope, int(internal_id), payload, ts),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def all_ids(self) -> List[int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM processed WHERE scope = ? ORDER BY id", (self.scope,)
            )
            return [int(r["id"]) for r in cur.fetchall()]

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM processed WHERE scope = ?", (self.scope,)
            )
            return int(cur.fetchone()["c"])

    def stats(self) -> Dict[str, int]:
        """Aggregate counts by status (``OK`` / ``FALLBACK_*`` / ``FAILED``)."""
        out: Dict[str, int] = {"total": 0, "OK": 0, "FALLBACK_NAME": 0,
                               "FALLBACK_ADDRESS": 0, "FAILED": 0}
        with self._lock:
            cur = self._conn.execute(
                "SELECT result_json FROM processed WHERE scope = ?", (self.scope,)
            )
            rows = cur.fetchall()
        for row in rows:
            out["total"] += 1
            try:
                data = json.loads(row["result_json"] or "{}")
                status = str(data.get("type", "FAILED")).upper()
            except (json.JSONDecodeError, TypeError):
                status = "FAILED"
            if status in out:
                out[status] += 1
        return out

    def clear(self) -> None:
        """Wipe all processed records (used by the GUI Reset button)."""
        with self._lock:
            self._conn.execute("DELETE FROM processed")
            self._conn.commit()

    def reset_ids(self, ids: List[int]) -> None:
        """Remove specific ids so they will be processed again."""
        with self._lock:
            self._conn.executemany(
                "DELETE FROM processed WHERE scope = ? AND id = ?",
                [(self.scope, int(i)) for i in ids],
            )
            self._conn.commit()

    def scopes(self) -> List[str]:
        """Every scope that holds at least one record."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT scope FROM processed ORDER BY scope"
            )
            return [str(r["scope"]) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
