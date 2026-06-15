"""
trigger_state.py — durable per-trigger thread dedup for ``once_per_thread``.

A trigger that opts into ``once_per_thread`` fires for the first message in a
conversation and skips later replies (see triggers.py). That decision needs to
survive a console restart — otherwise every open thread looks new again and the
trigger re-fires on the next reply. This stores the conversation/thread ids a
trigger has already fired for as JSON in the app dir (internal state, *not*
user-edited config), bounded per trigger.

The engine only needs ``already_fired(trigger, key) -> bool``; this is the
durable implementation it injects, mirroring ``triggers._MemoryThreadStore``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("runspec.console.trigger_state")


class ThreadDedupStore:
    """File-backed thread dedup, loaded once and written through on each new id.

    Writes happen only when a trigger fires for a *new* thread (the first message
    of a conversation), so the I/O is rare. Atomic (temp + ``os.replace``) and
    best-effort — a write failure is logged, never raised into routing."""

    def __init__(self, path: Path, *, cap: int = 5000) -> None:
        self._path = path
        self._cap = cap
        self._lock = threading.Lock()
        self._seen: dict[str, dict[str, None]] = self._load()

    def _load(self) -> dict[str, dict[str, None]]:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning(
                "trigger thread-state unreadable; starting empty", exc_info=True
            )
            return {}
        out: dict[str, dict[str, None]] = {}
        if isinstance(raw, dict):
            for trig, ids in raw.items():
                if isinstance(ids, list):
                    out[str(trig)] = {str(i): None for i in ids}
        return out

    def already_fired(self, trigger: str, key: str) -> bool:
        """True if ``trigger`` already fired for thread ``key`` (recording + saving
        it if not). Empty ``key`` is never recorded (the engine fails open)."""
        if not key:
            return False
        with self._lock:
            ids = self._seen.setdefault(trigger, {})
            if key in ids:
                return True
            ids[key] = None
            if len(ids) > self._cap:
                for old in list(ids)[: len(ids) - self._cap]:
                    del ids[old]
            self._save()
            return False

    def _save(self) -> None:
        """Atomically persist the current state. Caller holds the lock."""
        data = {trig: list(ids) for trig, ids in self._seen.items() if ids}
        tmp = self._path.with_name(f"{self._path.name}.tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:
            logger.warning("could not persist trigger thread-state", exc_info=True)
