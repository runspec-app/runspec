"""bridge_helpers.py — helper classes + small functions used by the Bridge.

Not part of the JS API surface. The trigger-ops adapter references the Bridge
only via duck typing / string annotations, so no runtime Bridge import is needed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bridge import Bridge


from .bridge_const import (
    logger,
)

# paramiko numbers each channel-open attempt, so a reconnect storm logs
# "Secsh channel 1 open FAILED…", "…channel 2…", … — same event, different id.
# Normalise the id out of the dedup key so the burst collapses to one row.
_CHANNEL_ID_RE = re.compile(r"\bchannel \d+\b", re.IGNORECASE)


class _ParamikoDevHandler(logging.Handler):
    """Forward paramiko log records into the Dev tab as ``runspec:ssh`` events.

    paramiko logs the full "Error reading SSH protocol banner" traceback from
    its transport thread at ERROR. With no handler configured those records hit
    Python's last-resort handler and spill into the launching terminal as raw
    tracebacks. Routing them here turns each into a filterable, timestamped
    Dev-tab row (the connect/banner/auth lifecycle, with the exact failure), and
    — because the paramiko logger's ``propagate`` is turned off when this is
    installed — keeps the terminal clean.

    Identical records are **coalesced** within a short window so a reconnect
    storm (e.g. paramiko logging a channel-open failure on every backoff while a
    room server is down) shows once instead of N times. A record that persists
    past the window resurfaces, so an ongoing problem is never hidden for long.
    """

    # Seconds an identical record is suppressed after it's first forwarded.
    _DEDUP_WINDOW = 60.0

    def __init__(
        self, bridge: Bridge, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._clock = clock
        self._recent: dict[tuple[str, str], float] = {}
        self.setFormatter(logging.Formatter("%(message)s"))

    def _is_duplicate(self, level: str, message: str) -> bool:
        """True if this (level, message) was forwarded within the dedup window.

        The channel id is normalised out so paramiko's per-attempt
        "Secsh channel N open FAILED" lines collapse to one. Updates the
        last-seen time either way (a steady stream of repeats keeps suppressing)
        and bounds the map so it can't grow without limit."""
        key = (level, _CHANNEL_ID_RE.sub("channel N", message))
        now = self._clock()
        last = self._recent.get(key)
        self._recent[key] = now
        if len(self._recent) > 256:
            self._recent = {
                k: t for k, t in self._recent.items() if now - t < self._DEDUP_WINDOW
            }
        return last is not None and (now - last) < self._DEDUP_WINDOW

    @staticmethod
    def _host_from_channel(name: str) -> str:
        """Pull the host out of a per-transport log channel name.

        ``executor._make_ssh_client`` tags each transport's log channel as
        ``paramiko.transport.<host>`` (paramiko appends ``.sftp`` for the SFTP
        sub-channel), so a steady-state record like an idle-disconnect names the
        host it happened on. Untagged records (``paramiko.transport`` itself,
        connect-time banner errors before the transport exists) yield ``""``.
        """
        prefix = "paramiko.transport."
        if not name.startswith(prefix):
            return ""
        host = name[len(prefix) :]
        if host.endswith(".sftp"):
            host = host[: -len(".sftp")]
        return host

    def emit(self, record: logging.LogRecord) -> None:
        try:
            host = self._host_from_channel(record.name)
            message = record.getMessage()
            detail: dict[str, Any] = {
                "level": record.levelname,
                "logger": record.name,
                "message": f"[{host}] {message}" if host else message,
            }
            if host:
                detail["host"] = host
            # format() appends the traceback after the (bare) message when the
            # record carries exc_info — that's the banner-timeout stack we want
            # expandable in the Dev tab.
            has_tb = bool(record.exc_info or record.exc_text)
            if has_tb:
                detail["traceback"] = self.format(record)
            # Coalesce repeats of the same (host-tagged) line — but never a record
            # carrying a traceback, which is higher-signal and worth seeing each time.
            if not has_tb and self._is_duplicate(detail["level"], detail["message"]):
                return
            self._bridge._emit_ssh_log(detail)
        except Exception:
            # A logging handler must never raise — that would break the call
            # site that emitted the log (here, paramiko's transport thread).
            pass


class _BridgeTriggerOps:
    """Adapts the Bridge to trigger_runner.BridgeOps without exposing these
    methods on the JS api surface (only public Bridge methods are exposed)."""

    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge

    def run_agent_turn(
        self,
        chat_id: str,
        system: str,
        message: str,
        *,
        draft: bool,
        reply_to: str | None = None,
        room: str | None = None,
        from_mailbox: str | None = None,
        allowed_runnables: list[str] | None = None,
        action_allow: list[str] | None = None,
        record: Any = None,
        stage: Any = None,
        trigger: str = "",
    ) -> str:
        return self._bridge._trigger_run_agent_turn(
            chat_id,
            system,
            message,
            draft=draft,
            reply_to=reply_to,
            room=room,
            from_mailbox=from_mailbox,
            allowed_runnables=allowed_runnables,
            action_allow=action_allow,
            record=record,
            stage=stage,
            trigger=trigger,
        )

    def queue_mailbox_for(self, payload: dict[str, Any]) -> str:
        # Reply *as* the watched mailbox the mail landed in (a shared mailbox you
        # have send-as rights on); blank for your own default-account inbox, so a
        # personal reply goes from your own identity. See email-folder-watch.md.
        return str(payload.get("mailbox") or "")

    def run_rule_tool(
        self,
        chat_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        trigger: str = "",
    ) -> str:
        # A rule run is a fixed, deterministic runnable call — not an LLM turn —
        # so record agent=False with the causing event source as provenance.
        self._bridge._set_run_origin(chat_id, trigger, agent=False)
        try:
            return self._bridge._gated_run_tool(tool_name, tool_input, chat_id)
        finally:
            self._bridge._clear_run_origin(chat_id)

    def run_rule_group(
        self,
        chat_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        trigger: str = "",
    ) -> list[str]:
        return self._bridge._run_rule_group(chat_id, calls, trigger=trigger)

    def run_pre_turn(
        self, chat_id: str, instructions: str, event_text: str
    ) -> dict[str, Any]:
        return self._bridge._trigger_run_pre_turn(chat_id, instructions, event_text)

    def expand_targets(self, run: str) -> list[str]:
        return self._bridge.expand_targets(run)

    def mirror_to_chat(self, kind: str, label: str, text: str) -> None:
        # The Console tab renders this as a finished block matching the trigger
        # mode: kind="run" → a slash-command run card, kind="chat" → an agent
        # card — the same blocks a manual invocation makes, so a triggered run
        # looks like the same command run by hand. The Triggers tab's activity
        # list listens for this event too. The id keys the Console block.
        self._bridge._dispatch(
            "runspec:trigger_note",
            {
                "id": uuid.uuid4().hex[:12],
                "kind": kind,
                "label": label,
                "text": text,
            },
        )


def _chat_connect_hint(exc: BaseException, *, via: str) -> str | None:
    """A concise, actionable message for a known-*permanent* chat-connect failure,
    or ``None`` to fall back to a full traceback.

    ``Administratively prohibited`` on a ``direct-tcpip`` channel means the host
    refuses SSH port-forwarding (``AllowTcpForwarding no``) — retrying the tunnel
    will never succeed, so a 20-line traceback every backoff is just noise. The
    fix is the exec relay, so point the operator straight at it."""
    if via != "exec" and "Administratively prohibited" in str(exc):
        return (
            "SSH port-forwarding is blocked on this host (Administratively "
            'prohibited). Set room_via = "exec" on the host entry to reach the '
            "room over an exec channel (console-room connect) instead of a tunnel."
        )
    return None


def _chat_connect_summary(exc: BaseException, *, via: str) -> tuple[str, bool]:
    """A one-line, human-readable summary of a chat room-connect failure, plus
    whether logging a full traceback is worthwhile.

    Returns ``(message, want_traceback)``. The common operational failures — the
    room server being stopped, port-forwarding blocked, a slow proxied connect —
    are *expected states*, not bugs, so they get a concise line and
    ``want_traceback=False``: a 20-line paramiko stack on every backoff is pure
    noise (the symptom this addresses). The biggest one is the **room server being
    down**: the tunnel reaches the host fine, but opening the direct-tcpip channel
    to the room port fails with ``ChannelException(2, 'Connect failed')`` (or, on
    the exec relay, a connection-refused) — transient, the reconnect loop picks it
    up the moment the server is back. Anything unrecognised returns
    ``want_traceback=True`` so genuinely unexpected errors keep their stack."""
    hint = _chat_connect_hint(exc, via=via)
    if hint:
        return (hint, False)
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if "connect failed" in low or "connection refused" in low or "open failed" in low:
        return (
            "room server isn't reachable on the host — is the room process "
            "running on its port? The reconnect loop will keep retrying.",
            False,
        )
    if isinstance(exc, TimeoutError) or "timed out" in low or "timeout" in low:
        return (
            "timed out reaching the host (SSH, through the proxy if configured) — "
            "retrying. Usually transient startup concurrency.",
            False,
        )
    if "in connect-backoff" in low:
        return ("host is in connect-backoff after recent failures — retrying.", False)
    return (text, True)


def _classify_room_connection(exc: BaseException | None, *, via: str) -> dict[str, Any]:
    """Turn a room-connect attempt's outcome into a structured, human-readable
    result for the UI's connection tester: ``{ok, stage, message, hint}``.

    ``stage`` localises the failure — ``ssh`` (reaching/authing the host, incl.
    the HTTP proxy), ``channel`` (opening the tunnel / exec relay), ``room`` (the
    relay reached the host but not the room server), or ``ok``. ``hint`` is an
    actionable next step when we recognise the failure."""
    if exc is None:
        return {"ok": True, "stage": "ok", "message": "", "hint": None}
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if "in connect-backoff" in low:
        return {
            "ok": False,
            "stage": "ssh",
            "message": f"Host is in connect-backoff after recent failures ({text}). The always-on reconnect loop is already retrying — try again in a moment.",
            "hint": None,
        }
    if isinstance(exc, TimeoutError) or "timed out" in low or "timeout" in low:
        return {
            "ok": False,
            "stage": "ssh",
            "message": "Timed out reaching the host — the SSH connection (through the HTTP proxy, if configured) didn't complete in time.",
            "hint": "If the proxy works for discovery/transfers, this is usually transient at startup (many connections open at once) — retry. If it persists, check the [ssh] proxy host:port is reachable.",
        }
    if "administratively prohibited" in low:
        return {
            "ok": False,
            "stage": "channel",
            "message": "The host refused the tunnel — SSH port-forwarding is disabled (AllowTcpForwarding no).",
            "hint": _chat_connect_hint(exc, via=via),
        }
    if "authentication" in low or "not a valid" in low or "no authentication" in low:
        return {
            "ok": False,
            "stage": "ssh",
            "message": f"SSH authentication failed ({text}). Check this host's key / identityFile.",
            "hint": None,
        }
    return {
        "ok": False,
        "stage": "ssh",
        "message": text,
        "hint": _chat_connect_hint(exc, via=via),
    }


class _RotaPresenceWatcher:
    """Pushes console presence to the chat room as the working-hours rota flips.

    A console is *available* in the room while on-shift and *away* otherwise (see
    the design §1). The rota only changes at window boundaries — no event arrives
    to drive it — so this polls ``clock_ok`` on an interval and calls
    ``set_presence`` only when the on-shift state actually flips. The first tick
    aligns presence with the rota immediately (the source announces "available" on
    connect, which the rota may correct to "away")."""

    def __init__(
        self,
        clock_ok: Callable[[], bool],
        set_presence: Callable[[bool], None],
        *,
        interval: float = 30.0,
    ) -> None:
        self._clock_ok = clock_ok
        self._set_presence = set_presence
        self._interval = interval
        self._stop = threading.Event()
        self._last: bool | None = None
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:
        """Evaluate the rota once; push presence only on a change (split out so
        the flip logic is unit-tested without the thread / sleep)."""
        try:
            on = bool(self._clock_ok())
        except Exception:
            logger.exception("chat: rota clock check failed")
            return
        if on == self._last:
            return
        self._last = on
        try:
            self._set_presence(on)
        except Exception:
            logger.exception("chat: presence push failed")

    def start(self) -> None:
        self._tick()  # align presence with the rota right away
        self._thread = threading.Thread(
            target=self._run, name="chat-presence", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._tick()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
