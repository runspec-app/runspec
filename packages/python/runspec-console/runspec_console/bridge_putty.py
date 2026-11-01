"""PuTTY session sync — one PuTTY saved session per console host.

The pure parts (naming, host selection, registry values, the write/delete
plan) are in :mod:`putty_sync`; this mixin gathers what each host needs from
the console's own state (ssh target, key, jump, HTTP proxy, terminal look) and
writes the sessions. ``[putty] sync = true`` re-syncs automatically whenever
the host list or config changes (:meth:`_putty_sync_auto`, Windows only).
"""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from typing import Any

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import putty_sync
from .bridge_const import PUTTY_PROXY_SESSION, logger
from .bridge_utils import _parse_ssh


class PuttySyncMixin:
    _putty_sync_guard: threading.Lock
    _putty_sync_running: bool
    _putty_sync_dirty: bool
    _putty_sync_last: dict[str, Any] | None

    # ── JS API ────────────────────────────────────────────────────────────

    def putty_sync_status(self) -> dict[str, Any]:
        """What Settings → SSH shows: whether this machine can sync (Windows
        with PuTTY), the config, the sessions the console owns, and the last
        sync's result."""
        cfg = self.get_config()  # type: ignore[attr-defined]
        putty = utils_mod._find_putty_exe("putty.exe")
        plink = utils_mod._find_putty_exe("plink.exe")
        owned: list[str] = []
        supported = True
        try:
            store = putty_sync.RegistrySessions()
            owner = config_mod.app_slug()
            owned = sorted(n for n, m in store.list().items() if m == owner)
        except ImportError:
            supported = False
        except OSError as exc:
            logger.warning("putty sync: could not read PuTTY sessions: %s", exc)
        return {
            "supported": supported,
            "putty": str(putty or ""),
            "plink": str(plink or ""),
            "config": putty_sync.read_putty_sync(cfg),
            "sessions": owned,
            "last": getattr(self, "_putty_sync_last", None),
        }

    def putty_sync_now(self) -> dict[str, Any]:
        """Write the saved sessions now (whether or not ``sync`` is on) and
        remove the console's sessions for hosts that are gone or out of scope."""
        return self._putty_sync_run()

    def putty_sync_remove(self) -> dict[str, Any]:
        """Delete every PuTTY saved session the console wrote."""
        try:
            store = putty_sync.RegistrySessions()
        except ImportError:
            return self._putty_unsupported()
        owner = config_mod.app_slug()
        removed: list[str] = []
        errors: list[dict[str, str]] = []
        for name, marker in store.list().items():
            if marker != owner:
                continue
            try:
                store.delete(name)
                removed.append(name)
            except OSError as exc:
                errors.append({"host": name, "reason": str(exc)})
        result = {
            "ok": not errors,
            "supported": True,
            "written": [],
            "removed": sorted(removed),
            "skipped": errors,
            "at": datetime.now(UTC).isoformat(),
        }
        self._putty_sync_last = result
        return result

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _putty_unsupported() -> dict[str, Any]:
        return {
            "ok": False,
            "supported": False,
            "error": "PuTTY saved sessions are only on Windows.",
            "written": [],
            "removed": [],
            "skipped": [],
        }

    def _putty_sync_desired(
        self, cfg: dict[str, Any]
    ) -> tuple[dict[str, tuple[str, dict[str, int | str]]], list[dict[str, str]]]:
        """Session name → (host, registry values) for every host in scope, and
        the hosts that can't get a session (with the reason)."""
        from . import terminal_look

        ps = putty_sync.read_putty_sync(cfg)
        cfg_ssh = cfg.get("ssh") or {}
        default_user = str(cfg_ssh.get("user") or "")
        proxy = str(cfg_ssh.get("proxy") or "")
        look_cfg = terminal_look.read_terminal(cfg)
        owner = config_mod.app_slug()
        hosts = list(getattr(self, "_hosts", None) or [])

        desired: dict[str, tuple[str, dict[str, int | str]]] = {}
        skipped: list[dict[str, str]] = []
        plink: Any = None
        proxy_session: str | None = None
        for h in putty_sync.select_hosts(hosts, ps["profiles"]):
            host = str(h["name"])
            user, hostname, port = _parse_ssh(str(h["ssh"]).strip())
            try:
                key = self._putty_key_file(h, cfg_ssh)  # type: ignore[attr-defined]
            except ValueError as exc:
                skipped.append({"host": host, "reason": str(exc).splitlines()[0]})
                continue
            proxy_values: dict[str, int | str] | None = None
            jump = utils_mod.host_jump(hosts, h)
            if jump:
                if "," in jump:
                    skipped.append(
                        {
                            "host": host,
                            "reason": "PuTTY sessions can't chain jump hosts",
                        }
                    )
                    continue
                plink = plink or utils_mod._find_putty_exe("plink.exe")
                if plink is None:
                    skipped.append(
                        {
                            "host": host,
                            "reason": f"plink.exe not found (needed for jump host {jump})",
                        }
                    )
                    continue
                if proxy and proxy_session is None:
                    proxy_session = (
                        utils_mod._ensure_putty_proxy_session(
                            proxy, PUTTY_PROXY_SESSION
                        )
                        or ""
                    )
                proxy_values = putty_sync.jump_proxy_values(
                    utils_mod._putty_jump_proxy_command(
                        str(plink),
                        jump,
                        user=default_user,
                        key_file=str(key) if key else "",
                        proxy_session=proxy_session or "",
                    )
                )
            elif proxy:
                proxy_values = putty_sync.http_proxy_values(proxy)
            name = putty_sync.session_name(h, ps["prefix"])
            if name.lower() in (n.lower() for n in desired):
                skipped.append(
                    {"host": host, "reason": f"another host is also '{name}'"}
                )
                continue
            values = putty_sync.host_values(
                hostname=hostname,
                port=port,
                user=user or default_user,
                key_file=str(key) if key else "",
                keepalive=ps["keepalive"],
                proxy=proxy_values,
                look=terminal_look.session_values(look_cfg, str(h.get("group") or "")),
                owner=owner,
            )
            desired[name] = (host, values)
        return desired, skipped

    def _putty_sync_run(self) -> dict[str, Any]:
        """One full sync: write every in-scope host, delete the console's
        sessions that are no longer wanted, never touch anyone else's."""
        try:
            store = putty_sync.RegistrySessions()
        except ImportError:
            return self._putty_unsupported()
        cfg = self.get_config()  # type: ignore[attr-defined]
        owner = config_mod.app_slug()
        desired, skipped = self._putty_sync_desired(cfg)
        try:
            existing = store.list()
        except OSError as exc:
            return {**self._putty_unsupported(), "supported": True, "error": str(exc)}
        steps = putty_sync.plan(
            {n: v for n, (_h, v) in desired.items()}, existing, owner
        )
        written: list[dict[str, str]] = []
        for name in steps["write"]:
            host, values = desired[name]
            try:
                store.write(name, values)
                written.append({"host": host, "session": name})
            except OSError as exc:
                skipped.append({"host": host, "reason": str(exc)})
        for name in steps["conflict"]:
            skipped.append(
                {
                    "host": desired[name][0],
                    "reason": f"a PuTTY session you made is already called '{name}'",
                }
            )
        removed: list[str] = []
        for name in steps["delete"]:
            try:
                store.delete(name)
                removed.append(name)
            except OSError as exc:
                skipped.append({"host": name, "reason": f"could not remove: {exc}"})
        result = {
            "ok": True,
            "supported": True,
            "written": written,
            "removed": removed,
            "skipped": skipped,
            "at": datetime.now(UTC).isoformat(),
        }
        self._putty_sync_last = result
        logger.info(
            "putty sync: %d written, %d removed, %d skipped",
            len(written),
            len(removed),
            len(skipped),
        )
        return result

    def _putty_sync_auto(self) -> None:
        """Re-sync in the background when ``[putty] sync`` is on (Windows only).

        Called after the host list or config changes. Calls that arrive while a
        sync is running are folded into one more pass, never queued up."""
        if sys.platform != "win32":
            return
        try:
            cfg = self.get_config()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a bad config must not break a host reload
            return
        if not putty_sync.read_putty_sync(cfg)["sync"]:
            return
        guard = self._putty_sync_guard
        with guard:
            if self._putty_sync_running:
                self._putty_sync_dirty = True
                return
            self._putty_sync_running = True

        def run() -> None:
            while True:
                try:
                    self._putty_sync_run()
                except Exception:  # noqa: BLE001 — best-effort, never crash the app
                    logger.exception("putty sync failed")
                with guard:
                    if self._putty_sync_dirty:
                        self._putty_sync_dirty = False
                        continue
                    self._putty_sync_running = False
                    return

        threading.Thread(target=run, name="putty-sync", daemon=True).start()
