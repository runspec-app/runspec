"""bridge_termsend.py — the *Send to terminals* bar (Windows).

Every PuTTY window the console opens (``launch_terminal`` /
``launch_terminals``) is remembered by process id. The bar — a small,
always-on-top second window loading the same frontend at ``#send-bar`` — lists
those windows and types what the person enters into the ticked ones
(``termsend.keystrokes``), so a command can be run on many hosts at once while
each window stays a normal PuTTY session (``sudo -iu``, prompts and all).

Human-only: these are JS API methods for the bar, never agent tools or console
MCP server tools. A send is recorded as a ``terminal.send`` audit event (hosts
and the typed text; a hidden send — a password — records only that something
hidden was sent).
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

from . import termsend
from .bridge_const import logger

SEND_BAR_TITLE = "Send to terminals"
SEND_BAR_HASH = "#send-bar"
# Logical pixels the bar takes along the bottom of the screen when the
# terminals are tiled with it.
SEND_BAR_HEIGHT = 230


class TermSendMixin:
    """State lives on the instance, created on first use (``_ts_state``)."""

    _frontend_url: str = ""

    # ── state ────────────────────────────────────────────────────────────────

    def _ts_state(self) -> dict[str, Any]:
        st = self.__dict__.get("_ts")
        if st is None:
            st = {"lock": threading.Lock(), "windows": {}, "seq": 0, "bar": None}
            self.__dict__["_ts"] = st
        return st

    def _register_terminals(self, opened: list[tuple[str, int]]) -> None:
        """Remember ``(host, pid)`` for windows just launched, newest last."""
        st = self._ts_state()
        with st["lock"]:
            for host, pid in opened:
                st["seq"] += 1
                st["windows"][str(pid)] = {
                    "id": str(pid),
                    "host": host,
                    "pid": int(pid),
                    "hwnd": 0,
                    "seq": st["seq"],
                    "opened": time.time(),
                }

    def _resolve_terminal_windows(self) -> list[dict[str, Any]]:
        """Find window handles for new entries, drop closed windows, and return
        the live list in launch order. A window not found yet (PuTTY still
        starting, host-key prompt) stays listed as ``ready: False`` for a minute."""
        st = self._ts_state()
        with st["lock"]:
            entries = list(st["windows"].values())
        if termsend.supported():
            pending = [e["pid"] for e in entries if not e["hwnd"]]
            found: dict[int, int] = {}
            if pending:
                from . import wintile

                with contextlib.suppress(Exception):
                    found = wintile._find_windows(pending, "PuTTY")
            now = time.time()
            with st["lock"]:
                for e in entries:
                    cur = st["windows"].get(e["id"])
                    if cur is None:
                        continue
                    if not cur["hwnd"] and cur["pid"] in found:
                        cur["hwnd"] = found[cur["pid"]]
                    if cur["hwnd"] and not termsend.is_window(cur["hwnd"]):
                        st["windows"].pop(e["id"], None)
                    elif not cur["hwnd"] and now - cur["opened"] > 60:
                        st["windows"].pop(e["id"], None)
        with st["lock"]:
            live = sorted(st["windows"].values(), key=lambda e: e["seq"])
            return [dict(e) for e in live]

    # ── JS API (the bar) ─────────────────────────────────────────────────────

    def terminal_windows(self) -> dict[str, Any]:
        """The PuTTY windows the console opened that are still open:
        ``{"supported": bool, "windows": [{"id", "host", "ready"}…],
        "keys": [...]}``. ``ready`` is false until the window has appeared."""
        wins = self._resolve_terminal_windows()
        return {
            "supported": termsend.supported(),
            "windows": [
                {"id": e["id"], "host": e["host"], "ready": bool(e["hwnd"])}
                for e in wins
            ],
            "keys": list(termsend.KEY_NAMES),
        }

    def send_to_terminals(
        self,
        ids: list[str],
        text: str = "",
        enter: bool = True,
        key: str = "",
        hidden: bool = False,
    ) -> dict[str, Any]:
        """Type ``text`` (then Enter when ``enter``) — or press the named
        ``key`` instead — in each window in ``ids``.

        ``hidden`` marks a password: it is typed the same way but never
        logged or audited. Returns ``{"sent": [host…], "missing": [host…]}``;
        a window that closed or isn't ready yet is ``missing``."""
        if not termsend.supported():
            raise ValueError("Sending to terminals works on Windows only.")
        if key:
            events = termsend.key_events(key)
        else:
            if not text and not enter:
                raise ValueError("Nothing to send.")
            events = termsend.keystrokes(str(text), enter=bool(enter))
        want = [str(i) for i in (ids or [])]
        if not want:
            raise ValueError("Tick at least one terminal.")
        wins = {e["id"]: e for e in self._resolve_terminal_windows()}
        sent: list[str] = []
        missing: list[str] = []
        for wid in want:
            e = wins.get(wid)
            if e is None:
                continue
            if e["hwnd"] and termsend.post(e["hwnd"], events):
                sent.append(e["host"])
            else:
                missing.append(e["host"])
        what = (
            f"key {key}"
            if key
            else ("hidden text" if hidden else f"{len(str(text))} characters")
        )
        logger.info(
            "Send to terminals: %s to %d window(s)%s",
            what,
            len(sent),
            f", {len(missing)} missing" if missing else "",
        )
        if sent:
            self._audit_terminal_send(
                sent,
                text="" if hidden or key else str(text),
                key=key,
                hidden=bool(hidden and not key),
            )
        return {"sent": sent, "missing": missing}

    def focus_terminal(self, id: str) -> bool:  # noqa: A002 — JS API name
        """Bring one terminal window to the front."""
        for e in self._resolve_terminal_windows():
            if e["id"] == str(id) and e["hwnd"]:
                return termsend.focus(e["hwnd"])
        return False

    def forget_terminals(self, ids: list[str] | None = None) -> None:
        """Stop listing these windows in the bar (all when ``ids`` is empty).
        The windows themselves stay open."""
        st = self._ts_state()
        with st["lock"]:
            if not ids:
                st["windows"].clear()
            else:
                for wid in ids:
                    st["windows"].pop(str(wid), None)

    def open_send_bar(self) -> dict[str, Any]:
        """Open the *Send to terminals* bar (or bring it back if open)."""
        if not termsend.supported():
            raise ValueError("Sending to terminals works on Windows only.")
        st = self._ts_state()
        with st["lock"]:
            existing = st["bar"]
        if existing is not None:
            with contextlib.suppress(Exception):
                existing.restore()
            with contextlib.suppress(Exception):
                existing.show()
            return {"opened": True, "existing": True}
        if not self._frontend_url:
            raise ValueError("The console's page address isn't known yet.")
        import webview  # type: ignore[import-not-found]

        bar: Any = webview.create_window(
            SEND_BAR_TITLE,
            self._frontend_url + SEND_BAR_HASH,
            js_api=self,
            width=820,
            height=SEND_BAR_HEIGHT,
            min_size=(560, 180),
            on_top=True,
            background_color="#262e33",
            text_select=True,
        )
        with st["lock"]:
            st["bar"] = bar

        def _bar_closed(*_a: Any) -> None:
            with st["lock"]:
                if st["bar"] is bar:
                    st["bar"] = None

        def _main_closed(*_a: Any) -> None:
            with contextlib.suppress(Exception):
                bar.destroy()

        with contextlib.suppress(Exception):
            bar.events.closed += _bar_closed
        main = getattr(self, "_window", None)
        if main is not None:
            with contextlib.suppress(Exception):
                main.events.closed += _main_closed
        return {"opened": True, "existing": False}

    # ── internals ────────────────────────────────────────────────────────────

    def _audit_terminal_send(
        self, hosts: list[str], *, text: str, key: str, hidden: bool
    ) -> None:
        """``terminal.send`` audit event, off the calling thread (the OS-log copy
        can start a process on Windows)."""
        detail: dict[str, Any] = {"windows": len(hosts)}
        if key:
            detail["key"] = key
        elif hidden:
            detail["hidden"] = True
        else:
            detail["text"] = text

        def _record() -> None:
            try:
                import runspec

                runspec.audit(
                    "terminal.send",
                    "sent",
                    target={"hosts": hosts},
                    detail=detail,
                )
            except Exception:  # noqa: BLE001 — never block a send on the ledger
                logger.debug("terminal.send audit failed", exc_info=True)

        threading.Thread(
            target=_record, name="terminal-send-audit", daemon=True
        ).start()
