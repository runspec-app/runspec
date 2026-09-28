"""bridge_history_sync.py — the History tab's local cache of remote hosts' runs.

``HistorySyncMixin`` keeps :class:`~runspec_console.history_cache.HistoryCache`
current by **pulling only what changed** in each remote venv's runs index since the
last pull (core ``runspec`` >= 0.63.0 — ``runsdb.changes``; the remote half is
``bridge_utils._DB_SYNC_SNIPPET``), and serves the History tab's 7d/30d views from
the cache so they render without waiting on SSH. Design:
``docs/design/history-incremental-pull.md``.

**Nothing gets worse.** The cache engages only for a remote host with at least one
v4 index; a host without one (older runspec everywhere), the local host (already
in-process and instant), the 90d/All views, and ``[history] cache = false`` all
take the existing read path unchanged. Inside a cached host, a dir with a pre-v4
index is read directly and a dir with no index is log-parsed, exactly as the direct
read does — both stored as wholesale snapshots.

**Freshness.** A read returns the cache immediately and kicks a background pull
(deduplicated per host, throttled); a pull that changed anything emits
``runspec:history_updated {host}`` and the tab refetches that host (from the
cache). A run finishing on a host, or Runs index maintenance, pulls that host at
once. A failed pull keeps the cache and records the error so the tab can say
"as of …" (:meth:`history_sync_status`).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from . import bridge_utils as utils_mod
from . import config as config_mod
from .bridge_const import logger
from .bridge_utils import _remote_log_script
from .history_cache import HistoryCache

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

# The window the cache holds and serves (the tab's 7d default and 30d option);
# wider views read the host directly.
HISTORY_CACHE_DAYS = 30
# Rows per pull round-trip, and round-trips per pull — a busy venv's first
# (reset) pull continues on the next pull if it doesn't finish in one.
_PULL_PAGE = 2000
_PULL_MAX_PAGES = 10
# A background pull for a host is skipped when one finished this recently — the
# tab refetches a host right after its history_updated event, which would
# otherwise trigger a second pull straight away.
_PULL_MIN_INTERVAL = 5.0
# Dirs read directly (a pre-v4 index, or no index → log-parse) cost a full read,
# so a pull refreshes them at most this often — or at once when forced (a run just
# finished on the host, Runs index maintenance).
_SNAPSHOT_INTERVAL = 120.0
# A host whose pull found no v4 index is retried after this long (its venvs may
# be upgraded), reading directly meanwhile.
_UNCACHABLE_RETRY = 600.0
# Records a cached read returns per host — a local query, so higher than the
# direct read's SSH-bounded cap.
_HISTORY_CACHE_CAP = 1000
# Pseudo-dir holding a cached host's log-parsed records (dirs without an index),
# replaced wholesale on each pull — the stream doesn't say which dir a file came from.
_LOGPARSE_DIR = "\x00logs"


class HistorySyncMixin:
    """Mixin for :class:`~runspec_console.bridge.Bridge` — the History cache."""

    _history_cache_obj: HistoryCache | None = None

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _history_cache(self: Bridge) -> HistoryCache:
        cache = self._history_cache_obj
        if cache is None:
            cache = self.__dict__.setdefault(
                "_history_cache_obj", HistoryCache(config_mod.history_cache_path())
            )
        return cache

    def _history_sync_state(self: Bridge) -> dict[str, Any]:
        # setdefault: atomic, so two first callers can't each make their own.
        st: dict[str, Any] = self.__dict__.setdefault(
            "_history_sync_mem",
            {
                "lock": threading.Lock(),
                "pulling": set(),
                "again": set(),
                "last": {},
                "uncachable": {},
                "snapshot": {},
            },
        )
        return st

    def _history_cache_enabled(self: Bridge) -> bool:
        try:
            return bool(config_mod.read_history().get("cache", True))
        except Exception:
            return True

    @staticmethod
    def _remote_history_layout(
        entry: dict[str, Any],
    ) -> tuple[list[str], dict[str, str], str, str] | None:
        """``(logs dirs, dir → venv group, primary group, remote python)`` for a
        remote host entry — each venv's ``logs`` dir plus ``~/logs`` — or ``None``
        when the entry has no runspec paths."""
        paths = [rp for rp in utils_mod._paths(entry) if rp]
        if not paths:
            return None
        primary_group = PurePosixPath(paths[0]).parent.parent.name
        dir_group: dict[str, str] = {}
        dirs: list[str] = []
        for rp in paths:
            vd = str(PurePosixPath(rp).parent.parent / "logs")
            if vd not in dir_group:
                dir_group[vd] = PurePosixPath(rp).parent.parent.name
                dirs.append(vd)
        if "~/logs" not in dir_group:
            dir_group["~/logs"] = primary_group
            dirs.append("~/logs")
        sv_python = str(PurePosixPath(paths[0]).parent / "python")
        return dirs, dir_group, primary_group, sv_python

    # ── the read ──────────────────────────────────────────────────────────────

    def _history_from_cache(
        self: Bridge, entry: dict[str, Any], since_days: int | None
    ) -> list[dict[str, Any]] | None:
        """Slim History rows for a remote host from the cache, or ``None`` to read
        the host directly (cache off, a window wider than the cache, no v4 index on
        the host, or the first pull failed)."""
        records = self._cached_records(entry, since_days, limit=_HISTORY_CACHE_CAP)
        if records is None:
            return None
        return [self._slim_history_row(r) for r in records]

    def _cached_records(
        self: Bridge, entry: dict[str, Any], since_days: int | None, *, limit: int
    ) -> list[dict[str, Any]] | None:
        """A remote host's full cached records (``extra`` included — what Analytics
        aggregates), newest first, within ``since_days``; ``None`` when the host
        can't be served from the cache. A primed host kicks a background pull; an
        unprimed one is pulled in line first."""
        if not since_days or since_days <= 0 or since_days > HISTORY_CACHE_DAYS:
            return None
        if not self._history_cache_enabled():
            return None
        host = str(entry.get("name") or "")
        state = self._history_sync_state()
        with state["lock"]:
            retry_at = state["uncachable"].get(host)
        if retry_at is not None and time.monotonic() < retry_at:
            return None
        cache = self._history_cache()
        if cache.is_primed(host):
            self._kick_history_pull(host)
        else:
            capable = self._history_pull(
                entry
            )  # first pull: in line, so the view is complete
            if not capable:
                return None
        since_date = self._since_date(since_days)
        records = cache.read(host, since_date=since_date, limit=limit)
        return [r for r in records if r.get("kind", "runnable") == "runnable"]

    # ── the pull ──────────────────────────────────────────────────────────────

    def _kick_history_pull(self: Bridge, host: str, *, force: bool = False) -> None:
        """Pull ``host`` in the background (one at a time per host; skipped when a
        pull finished within ``_PULL_MIN_INTERVAL`` unless ``force``)."""
        if not self._history_cache_enabled():
            return
        entry = self._host_entry(host)
        if entry is None or not entry.get("ssh"):
            return
        state = self._history_sync_state()
        with state["lock"]:
            if host in state["pulling"]:
                if force:
                    # A run just finished but a pull is already under way (and may
                    # have read before it) — pull once more when that one ends.
                    state["again"].add(host)
                return
            if (
                not force
                and time.monotonic() - state["last"].get(host, 0.0) < _PULL_MIN_INTERVAL
            ):
                return
            if not self._history_cache().is_primed(host):
                return  # never pulled (or not cachable) — the next read does it in line
            state["pulling"].add(host)

        def run() -> None:
            forced = force
            try:
                while True:
                    try:
                        self._history_pull(entry, claimed=True, force=forced)
                    except Exception:
                        logger.exception("history: background pull failed for %s", host)
                    with state["lock"]:
                        if host not in state["again"]:
                            break
                        state["again"].discard(host)
                    forced = True
            finally:
                with state["lock"]:
                    state["pulling"].discard(host)
                    state["again"].discard(host)

        threading.Thread(target=run, name=f"history-pull-{host}", daemon=True).start()

    def _history_pull(
        self: Bridge,
        entry: dict[str, Any],
        *,
        claimed: bool = False,
        force: bool = False,
    ) -> bool:
        """Bring ``entry``'s cached history up to date. Returns ``True`` when the host
        is cachable (at least one v4 index) and the pull succeeded; emits
        ``runspec:history_updated`` when anything shown changed."""
        host = str(entry.get("name") or "")
        state = self._history_sync_state()
        if not claimed:
            with state["lock"]:
                if host in state["pulling"]:
                    return self._history_cache().is_primed(host)
                state["pulling"].add(host)
        try:
            ok, changed = self._history_pull_once(entry, force=force)
        finally:
            with state["lock"]:
                state["last"][host] = time.monotonic()
                if not claimed:
                    state["pulling"].discard(host)
        if changed:
            self._dispatch("runspec:history_updated", {"host": host})
        return ok

    def _history_pull_once(
        self: Bridge, entry: dict[str, Any], *, force: bool = False
    ) -> tuple[bool, bool]:
        """One pull: ``(cachable_and_ok, changed)``. Snapshot dirs (read directly)
        are refreshed only when ``force``d or older than ``_SNAPSHOT_INTERVAL``."""
        host = str(entry.get("name") or "")
        cache = self._history_cache()
        layout = self._remote_history_layout(entry)
        if layout is None:
            return False, False
        dirs, dir_group, primary_group, sv_python = layout
        ssh, idf = entry["ssh"], entry.get("identityFile")
        window_start = (
            datetime.now(timezone.utc) - timedelta(days=HISTORY_CACHE_DAYS)
        ).isoformat(timespec="seconds")
        known = cache.cursors(host)
        # A dir's cursor, or {} (→ reset) when it has none or was a snapshot dir.
        pending = {
            d: (
                {"db_id": known[d]["db_id"], "seq": known[d]["seq"]}
                if (known.get(d) or {}).get("db_id")
                else {}
            )
            for d in dirs
        }
        changed = False
        synced_any = False
        legacy: list[str] = []
        unindexed: list[str] = []
        for _ in range(_PULL_MAX_PAGES):
            if not pending:
                break
            cmd = utils_mod.db_sync_cmd(
                sv_python, pending, limit=_PULL_PAGE, window_start=window_start
            )
            try:
                _, out, _ = self._ssh_run(ssh, cmd, identity_file=idf, timeout=30)
                results = json.loads(out) if out.strip() else {}
            except Exception as exc:
                return self._history_pull_failed(
                    host, synced_any, f"{type(exc).__name__}: {exc}"
                ), changed
            if not isinstance(results, dict):
                return self._history_pull_failed(
                    host, synced_any, "unexpected reply from the remote host"
                ), changed
            nxt: dict[str, dict[str, Any]] = {}
            for d in list(pending):
                res = results.get(d)
                mode = res.get("mode") if isinstance(res, dict) else None
                if mode in ("delta", "reset"):
                    assert isinstance(res, dict)
                    synced_any = True
                    group = dir_group.get(d, primary_group)
                    recs, drop = [], [str(x) for x in res.get("deleted") or []]
                    for row in res.get("rows") or []:
                        if row.get("status") == "running":
                            drop.append(
                                str(row.get("run_id") or "")
                            )  # finished runs only
                        else:
                            recs.append(utils_mod.db_row_to_record(row, group, host))
                    changed |= cache.apply(host, d, res, recs, drop)
                    if res.get("more"):
                        nxt[d] = {"db_id": res.get("db_id"), "seq": res.get("seq")}
                elif mode == "legacy":
                    legacy.append(d)
                elif mode == "none":
                    unindexed.append(d)
                else:  # error / missing — leave that dir's cache as it was
                    logger.info(
                        "history: pull of %s:%s failed: %s",
                        host,
                        d,
                        (res or {}).get("error")
                        if isinstance(res, dict)
                        else "no reply",
                    )
            pending = nxt
        if not synced_any:
            # No v4 index on this host: it isn't cachable yet — read it directly.
            with self._history_sync_state()["lock"]:
                self._history_sync_state()["uncachable"][host] = (
                    time.monotonic() + _UNCACHABLE_RETRY
                )
            return False, changed
        state = self._history_sync_state()
        now = time.monotonic()
        with state["lock"]:
            state["uncachable"].pop(host, None)
            refresh = (
                force
                or now - state["snapshot"].get(host, -_SNAPSHOT_INTERVAL)
                >= _SNAPSHOT_INTERVAL
            )
            if refresh:
                state["snapshot"][host] = now
        if not refresh:
            legacy, unindexed = [], []
        since_date = self._since_date(HISTORY_CACHE_DAYS)
        since_iso = f"{since_date}T00:00:00+00:00" if since_date else None
        if legacy:
            changed |= self._history_snapshot_legacy(
                entry, legacy, dir_group, primary_group, sv_python, since_iso
            )
        for d in unindexed:
            # An index that disappeared (deleted by hand) leaves no stale records
            # beside the log-parsed ones.
            changed |= cache.replace(host, d, [])
        if unindexed:
            script = _remote_log_script(unindexed, None, limit=self._HISTORY_CAP + 50)
            try:
                _, stdout, _ = self._ssh_run(ssh, script, identity_file=idf, timeout=30)
                parsed = self._parse_remote_stream(
                    stdout, host, primary_group, include_extra=True
                )
                if since_date:
                    parsed = [r for r in parsed if r.get("ts", "")[:10] >= since_date]
                changed |= cache.replace(host, _LOGPARSE_DIR, parsed)
            except Exception:
                logger.exception(
                    "history: log-parse of unindexed dirs failed for %s", host
                )
        elif refresh and _LOGPARSE_DIR in known:
            changed |= cache.replace(host, _LOGPARSE_DIR, [])
        changed |= cache.drop_dirs(host, set(dirs) | {_LOGPARSE_DIR})
        cache.trim(window_start)
        cache.mark(host)
        return True, changed

    def _history_snapshot_legacy(
        self: Bridge,
        entry: dict[str, Any],
        dirs: list[str],
        dir_group: dict[str, str],
        primary_group: str,
        sv_python: str,
        since_iso: str | None,
    ) -> bool:
        """Read pre-v4 indexes directly (the existing windowed read) and store each
        as a snapshot. Returns whether anything shown changed."""
        cmd = utils_mod.db_read_cmd(sv_python, dirs, since_iso, _HISTORY_CACHE_CAP)
        try:
            _, out, _ = self._ssh_run(
                entry["ssh"], cmd, identity_file=entry.get("identityFile"), timeout=30
            )
            results = json.loads(out) if out.strip() else {}
        except Exception:
            logger.exception(
                "history: direct read of pre-v4 indexes failed for %s",
                entry.get("name"),
            )
            return False
        changed = False
        host = str(entry.get("name") or "")
        for d in dirs:
            res = results.get(d) if isinstance(results, dict) else None
            if not isinstance(res, dict) or res.get("error"):
                continue
            group = dir_group.get(d, primary_group)
            recs = [
                utils_mod.db_row_to_record(r, group, host)
                for r in res.get("rows") or []
            ]
            changed |= self._history_cache().replace(host, d, recs)
        return changed

    def _history_pull_failed(
        self: Bridge, host: str, partial: bool, error: str
    ) -> bool:
        """Record a failed pull. The cache keeps what it had; the result says whether
        the host can still be served from it."""
        cache = self._history_cache()
        cache.mark(host, error=error)
        logger.info("history: pull failed for %s: %s", host, error)
        return partial or cache.is_primed(host)

    # ── public ────────────────────────────────────────────────────────────────

    def history_sync_status(self: Bridge) -> dict[str, dict[str, Any]]:
        """Per remote host: whether its History comes from the local cache, when it
        was last pulled (``synced_at``, ISO UTC) and the last pull's ``error`` —
        the History tab shows "as of …" for a host whose latest pull failed."""
        out: dict[str, dict[str, Any]] = {}
        if not self._history_cache_enabled():
            return out
        cache = self._history_cache()
        with self._lock:
            names = [
                str(h.get("name"))
                for h in self._hosts
                if h.get("name") and h.get("ssh")
            ]
        for name in names:
            st = cache.status(name)
            if st.get("synced_at") or st.get("error"):
                out[name] = {"cached": bool(st.get("synced_at")), **st}
        return out

    def _history_forget_hosts(self: Bridge) -> None:
        """Drop cached history for hosts no longer in the host list."""
        if self._history_cache_obj is None:
            return
        with self._lock:
            names = {str(h.get("name")) for h in self._hosts if h.get("name")}
        self._history_cache_obj.retain_hosts(names)
