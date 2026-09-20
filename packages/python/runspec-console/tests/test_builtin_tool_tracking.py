"""
test_builtin_tool_tracking.py — visibility + control for built-in agent tools.

When the chat agent runs a built-in tool (repo_*, outlook_*, manage_denylist,
action/memory tools) it now shows as a live Console in-flight card, streams
progress, is cooperatively cancellable, and leaves a History/Analytics audit
record. These tests bind the real Bridge methods onto a light stub (the pattern
used across the suite) and exercise the tracking wrapper + helpers directly.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from runspec_console.bridge import Bridge
from runspec_console.bridge_const import ToolCancelled, _CANCEL_KEY
from runspec_console.bridge_utils import _parse_log_text

# The bridge mixins share one logger (``logging.getLogger(__name__)`` resolves to
# this name in bridge_const.py), so caplog captures the built-in-tool log lines
# under it.
_BRIDGE_LOGGER = "runspec_console.bridge_const"


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    """Point the home dir at the per-test tmp_path. The audit log lands in
    ``Path.home()/logs``; on Windows ``Path.home()`` reads ``USERPROFILE`` (not
    ``HOME``), so a test that only set ``HOME`` wrote to the real home and the
    read-back missed. Setting both isolates it cross-platform."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


class _Stub:
    # Methods under test, bound from the real Bridge mixins. The label/host/args
    # helpers are staticmethods — re-wrap so `self.` doesn't pass an extra arg.
    _run_builtin_tool_tracked = Bridge._run_builtin_tool_tracked
    _builtin_tool_label = staticmethod(Bridge._builtin_tool_label)
    _builtin_tool_host = staticmethod(Bridge._builtin_tool_host)
    _builtin_tool_args = staticmethod(Bridge._builtin_tool_args)
    _audit_builtin_tool = Bridge._audit_builtin_tool
    _TOOL_AUDIT_FILE = Bridge._TOOL_AUDIT_FILE
    _current_user = staticmethod(lambda: "tester")

    def __init__(self):
        self._lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._in_flight: dict = {}
        self._run_origin: dict = {}
        self.events: list = []
        self.during: dict | None = None
        self.seen_kwargs: dict = {}
        self._result = "OK"
        self._raise: Exception | None = None

    def _dispatch(self, event, detail):
        self.events.append((event, detail))
        # Capture the live in-flight state the first time a card is present, so
        # we can assert the card was registered *during* the run (not just after).
        if (
            event == "runspec:in_flight_updated"
            and self._in_flight
            and self.during is None
        ):
            self.during = {k: dict(v) for k, v in self._in_flight.items()}

    def _run_tool_sync(self, name, ti, chat_id="", cancel_event=None, on_progress=None):
        self.seen_kwargs = {"cancel_event": cancel_event, "on_progress": on_progress}
        if on_progress is not None:
            on_progress("working…")
        if self._raise is not None:
            raise self._raise
        return self._result


# ── label / host / args helpers ───────────────────────────────────────────────


def test_builtin_tool_label():
    L = Bridge._builtin_tool_label
    assert L("repo_sync", {"name": "runspec"}) == "repo_sync: runspec"
    assert L("repo_grep", {"pattern": "TODO"}) == "repo_grep: TODO"
    assert L("manage_denylist", {"action": "all"}) == "manage_denylist: all"
    assert L("run_runbook", {"id": "triage"}) == "run_runbook: triage"
    assert L("console_status", {}) == "console_status"  # no salient arg → bare name


def test_builtin_tool_host_and_args():
    assert Bridge._builtin_tool_host("download_file", {"host": "db1"}) == "db1"
    assert (
        Bridge._builtin_tool_host("manage_denylist", {"targets": ["a", "b"]}) == "a, b"
    )
    assert Bridge._builtin_tool_host("repo_sync", {"name": "x"}) == ""  # local-only
    big = Bridge._builtin_tool_args({"content": "z" * 500, "n": 3})
    assert big["content"].endswith("…") and len(big["content"]) <= 201
    assert big["n"] == 3


# ── tracking wrapper: card lifecycle + progress + audit ───────────────────────


def test_tracked_run_registers_card_streams_progress_and_audits(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _Stub()
    out = asyncio.run(
        b._run_builtin_tool_tracked("repo_sync", {"name": "runspec"}, "chatX")
    )
    assert out == "OK"

    # The card was registered during the run and torn down after.
    assert b._in_flight == {}
    assert b.during is not None
    card = next(iter(b.during.values()))
    assert card["kind"] == "builtin_tool"
    assert card["runnable"] == "repo_sync: runspec"
    assert card["tool"] == "repo_sync"
    assert card["chatId"] == "chatX"
    assert _CANCEL_KEY in card  # carries its own cancel Event

    # cancel_event + on_progress were threaded into the tool.
    assert b.seen_kwargs["cancel_event"] is not None
    assert b.seen_kwargs["on_progress"] is not None

    # in_flight_updated fired on both register and teardown (immediate strip update).
    assert sum(1 for e, _ in b.events if e == "runspec:in_flight_updated") == 2
    # Progress streamed to the chat block, keyed to the chat id.
    prog = [d for e, d in b.events if e == "runspec:tool_progress"]
    assert prog and prog[0]["id"] == "chatX" and prog[0]["message"] == "working…"

    # An audit line landed in ~/logs and parses as a run_summary record.
    log = (tmp_path / "logs" / "runspec-console-tools.log").read_text(encoding="utf-8")
    recs = _parse_log_text("runspec-console-tools", log, "local", include_extra=True)
    assert len(recs) == 1
    assert recs[0]["runnable"] == "repo_sync: runspec"
    assert recs[0]["exitCode"] == 0
    assert recs[0]["initiatedBy"] == "llm"  # built-in tools are agent runs


def test_tracked_run_cancelled_reports_stop_and_audits_130(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _Stub()
    b._raise = ToolCancelled()
    with caplog.at_level(logging.INFO, logger=_BRIDGE_LOGGER):
        out = asyncio.run(
            b._run_builtin_tool_tracked("repo_grep", {"pattern": "x"}, "c1")
        )
    assert "stopped" in out.lower()
    assert b._in_flight == {}  # card always cleaned up
    log = (tmp_path / "logs" / "runspec-console-tools.log").read_text(encoding="utf-8")
    rec = _parse_log_text("t", log, "local", include_extra=True)[0]
    assert rec["exitCode"] == 130  # cancelled
    assert rec["extra"]["agent"] is True
    # A finish line reporting the cancelled outcome is logged (complements audit).
    assert any(
        "repo_grep finished" in m and "outcome=cancelled" in m
        for m in (r.getMessage() for r in caplog.records)
    )


def test_tracked_run_error_is_surfaced_not_raised(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _Stub()
    b._raise = RuntimeError("boom")
    with caplog.at_level(logging.INFO, logger=_BRIDGE_LOGGER):
        out = asyncio.run(b._run_builtin_tool_tracked("repo_read", {"path": "a"}, "c2"))
    assert out.startswith("✗") and "boom" in out  # surfaced as a tool result
    assert b._in_flight == {}
    log = (tmp_path / "logs" / "runspec-console-tools.log").read_text(encoding="utf-8")
    assert _parse_log_text("t", log, "local")[0]["exitCode"] == 1
    # The finish line reports outcome=error (alongside the existing warning log).
    assert any(
        "repo_read finished" in m and "outcome=error" in m
        for m in (r.getMessage() for r in caplog.records)
    )


# ── log visibility: started / finished / progress / heartbeat ─────────────────


def test_tracked_run_logs_start_and_finish(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _Stub()
    with caplog.at_level(logging.INFO, logger=_BRIDGE_LOGGER):
        asyncio.run(b._run_builtin_tool_tracked("repo_sync", {"name": "runspec"}, "cS"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("repo_sync started" in m for m in msgs)
    assert any(
        "repo_sync finished" in m and "outcome=ok" in m and "ms" in m for m in msgs
    )


def test_progress_mirrored_to_log_at_debug(tmp_path, monkeypatch, caplog):
    # _Stub._run_tool_sync calls on_progress("working…") once.
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _Stub()
    with caplog.at_level(logging.DEBUG, logger=_BRIDGE_LOGGER):
        asyncio.run(b._run_builtin_tool_tracked("repo_grep", {"pattern": "x"}, "cD"))
    # Producer progress is mirrored to the log, but only at DEBUG.
    assert any(
        r.levelno == logging.DEBUG
        and "progress" in r.getMessage()
        and "working…" in r.getMessage()
        for r in caplog.records
    )
    # It must NOT surface at INFO — that keeps the default log clean.
    assert all(
        r.levelno != logging.INFO or "progress" not in r.getMessage()
        for r in caplog.records
    )


def _slow_tool_sync(name, ti, chat_id="", cancel_event=None, on_progress=None):
    # Block long enough for several heartbeat ticks; emits no progress of its own.
    time.sleep(0.06)
    return "OK"


def test_heartbeat_emits_progress_and_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Patch the module binding the heartbeat reads (imported into bridge_autonomy).
    monkeypatch.setattr("runspec_console.bridge_autonomy._TOOL_HEARTBEAT_SECS", 0.01)
    b = _Stub()
    b._run_tool_sync = _slow_tool_sync
    with caplog.at_level(logging.INFO, logger=_BRIDGE_LOGGER):
        out = asyncio.run(
            b._run_builtin_tool_tracked("repo_read", {"path": "big"}, "chatH")
        )
    assert out == "OK"
    # The card animates during the silent stretch via a heartbeat tool_progress...
    prog = [d for e, d in b.events if e == "runspec:tool_progress"]
    assert any("still working" in d["message"] for d in prog)
    # ...and the same signal lands in the log so a long gap is explained.
    assert any("still running" in r.getMessage() for r in caplog.records)


def test_heartbeat_empty_chat_id_logs_without_ui_event(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("runspec_console.bridge_autonomy._TOOL_HEARTBEAT_SECS", 0.01)
    b = _Stub()
    b._run_tool_sync = _slow_tool_sync
    with caplog.at_level(logging.INFO, logger=_BRIDGE_LOGGER):
        asyncio.run(b._run_builtin_tool_tracked("repo_read", {"path": "big"}, ""))
    # Empty chat_id (trigger/runbook turn): no card to match → no heartbeat event.
    assert not [
        d
        for e, d in b.events
        if e == "runspec:tool_progress" and "still working" in d.get("message", "")
    ]
    # But the heartbeat still logs — the headless turn's gap is still explained.
    assert any("still running" in r.getMessage() for r in caplog.records)


def test_audit_carries_turn_provenance(tmp_path, monkeypatch):
    """A built-in tool run inside a trigger/runbook turn is attributed to it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _Stub()
    b._run_origin["c3"] = {"trigger": "schedule", "agent": True, "runbook": "nightly"}
    asyncio.run(b._run_builtin_tool_tracked("manage_denylist", {"action": "all"}, "c3"))
    log = (tmp_path / "logs" / "runspec-console-tools.log").read_text(encoding="utf-8")
    rec = _parse_log_text("t", log, "local", include_extra=True)[0]
    assert rec["trigger"] == "schedule" and rec["runbook"] == "nightly"
