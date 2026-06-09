"""
test_relay_filter.py — source-side filter path for relay & download.

The pure command/quoting helpers live in test_fs_transfer.py. Here we exercise
the bridge plumbing: the shared streaming pump (_run_filter_stream) and its
exit-code / cleanup / cancel semantics, the command wiring in the per-file
filtered helpers, and the validation in start_relay / start_transfer. Real
Bridge methods are bound onto a light harness so Bridge.__init__'s SSH side
effects never run; a scripted fake channel stands in for paramiko.
"""

from __future__ import annotations

import threading
import types

import pytest

from runspec_console.bridge import Bridge


class FakeChannel:
    """A paramiko-channel stand-in that hands back scripted stdout/stderr then
    an exit status. recv/recv_stderr drain their buffers; exit_status_ready
    flips true once both are empty (mirroring a process that has exited)."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0):
        self._out = stdout
        self._err = stderr
        self._exit = exit_code
        self.closed = False
        self.cmd = None

    def set_combine_stderr(self, _v):  # noqa: D401
        pass

    def exec_command(self, cmd):
        self.cmd = cmd

    def recv_ready(self):
        return bool(self._out)

    def recv(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv_stderr(self, n):
        chunk, self._err = self._err[:n], self._err[n:]
        return chunk

    def exit_status_ready(self):
        return not self._out and not self._err

    def recv_exit_status(self):
        return self._exit

    def close(self):
        self.closed = True


class FakeFile:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, chunk):
        self.data += chunk

    def close(self):
        self.closed = True


class _Harness:
    """Binds just the methods under test; no Bridge.__init__."""

    _run_filter_stream = Bridge._run_filter_stream
    _relay_one_filtered = Bridge._relay_one_filtered
    _transfer_one_filtered = Bridge._transfer_one_filtered

    def __init__(self):
        self.events: list[tuple] = []

    def _dispatch(self, event, detail):
        self.events.append((event, detail))


def _item():
    return types.SimpleNamespace(
        name="app.log", src="/var/log/app.log", dst="/dest/app.log"
    )


def _run(harness, channel, *, in_size=100, cancel=None):
    chan_release = {"called": False}
    dst = FakeFile()
    removed = {"called": False}

    def session_factory():
        return channel, lambda: chan_release.__setitem__("called", True)

    harness._run_filter_stream(
        session_factory,
        "sh -c 'grep x' < /var/log/app.log",
        dst_open=lambda: dst,
        dst_remove=lambda: removed.__setitem__("called", True),
        in_size=in_size,
        item=_item(),
        tid="t1",
        idx=0,
        count=1,
        cancel_event=cancel or threading.Event(),
    )
    return dst, removed, chan_release


def test_stream_success_writes_output_and_releases():
    h = _Harness()
    ch = FakeChannel(stdout=b"line1\nline2\n", exit_code=0)
    dst, removed, chan_release = _run(h, ch)

    assert dst.data == b"line1\nline2\n"
    assert dst.closed
    assert not removed["called"]
    assert ch.closed
    assert chan_release["called"]
    # Final progress tick reports bytes written + the indeterminate flag.
    last = h.events[-1]
    assert last[0] == "runspec:transfer_progress"
    assert last[1]["transferred"] == len(b"line1\nline2\n")
    assert last[1]["indeterminate"] is True
    assert last[1]["total"] == 100


def test_grep_no_match_exit1_empty_stderr_is_success():
    h = _Harness()
    ch = FakeChannel(stdout=b"", stderr=b"", exit_code=1)
    dst, removed, _ = _run(h, ch)

    assert dst.data == b""  # legitimately empty result
    assert not removed["called"]  # NOT treated as a failure


def test_exit1_with_stderr_fails_and_removes_partial():
    h = _Harness()
    ch = FakeChannel(stdout=b"partial", stderr=b"sed: bad flag", exit_code=1)
    dst = FakeFile()
    removed = {"called": False}

    def session_factory():
        return ch, lambda: None

    with pytest.raises(RuntimeError, match="sed: bad flag"):
        h._run_filter_stream(
            session_factory,
            "cmd",
            dst_open=lambda: dst,
            dst_remove=lambda: removed.__setitem__("called", True),
            in_size=0,
            item=_item(),
            tid="t",
            idx=0,
            count=1,
            cancel_event=threading.Event(),
        )
    assert removed["called"]  # exit 1 WITH stderr is a real failure → cleaned up


def test_exit2_fails_with_stderr_and_removes():
    h = _Harness()
    ch = FakeChannel(stdout=b"x", stderr=b"boom", exit_code=2)
    chan_release = {"called": False}
    dst = FakeFile()
    removed = {"called": False}

    def session_factory():
        return ch, lambda: chan_release.__setitem__("called", True)

    with pytest.raises(RuntimeError, match="boom"):
        h._run_filter_stream(
            session_factory,
            "cmd",
            dst_open=lambda: dst,
            dst_remove=lambda: removed.__setitem__("called", True),
            in_size=0,
            item=_item(),
            tid="t",
            idx=0,
            count=1,
            cancel_event=threading.Event(),
        )
    assert removed["called"]
    assert chan_release["called"]  # slot freed even on failure
    assert ch.closed


def test_cancel_midstream_raises_and_removes():
    h = _Harness()
    # Lots of output so the loop iterates; cancel is set before we start.
    ch = FakeChannel(stdout=b"x" * (32768 * 4), exit_code=0)
    cancel = threading.Event()
    cancel.set()
    dst = FakeFile()
    removed = {"called": False}

    def session_factory():
        return ch, lambda: None

    with pytest.raises(RuntimeError, match="cancelled"):
        h._run_filter_stream(
            session_factory,
            "cmd",
            dst_open=lambda: dst,
            dst_remove=lambda: removed.__setitem__("called", True),
            in_size=0,
            item=_item(),
            tid="t",
            idx=0,
            count=1,
            cancel_event=cancel,
        )
    assert removed["called"]


def test_relay_one_filtered_builds_command_and_delegates(monkeypatch):
    h = _Harness()
    captured = {}

    def fake_run(session_factory, cmd, **kw):
        captured["cmd"] = cmd
        captured["dst_open"] = kw["dst_open"]

    h._run_filter_stream = types.MethodType(
        lambda self, sf, cmd, **kw: fake_run(sf, cmd, **kw), h
    )
    src_sftp = types.SimpleNamespace(stat=lambda p: types.SimpleNamespace(st_size=42))
    dest_sftp = object()

    h._relay_one_filtered(
        session_factory=lambda: (None, lambda: None),
        dest_sftp=dest_sftp,
        item=_item(),
        rendered_pipeline="grep -F -- foo",
        src_sftp=src_sftp,
        tid="t",
        idx=0,
        count=1,
        cancel_event=threading.Event(),
    )
    assert captured["cmd"] == "sh -c 'grep -F -- foo' < /var/log/app.log"


# --- start_relay / start_transfer validation (no SSH) ---------------------


class _ValidationBridge:
    start_relay = Bridge.start_relay
    start_transfer = Bridge.start_transfer

    def __init__(self, filters):
        self._filters = filters
        self._filter_entry = types.MethodType(Bridge._filter_entry, self)

    def _host_entry(self, host):
        return {"name": host, "ssh": f"deploy@{host}"}  # always reachable


def test_start_relay_unknown_filter_errors():
    b = _ValidationBridge(filters=[])
    res = b.start_relay(
        {"kind": "host", "host": "src"},
        ["/a.log"],
        "dst",
        "/incoming",
        filter_name="nope",
    )
    assert res == {"error": "unknown filter: nope"}


def test_start_relay_missing_arg_errors():
    b = _ValidationBridge(filters=[{"name": "byid", "pipeline": "grep -F -- {{arg}}"}])
    res = b.start_relay(
        {"kind": "host", "host": "src"},
        ["/a.log"],
        "dst",
        "/incoming",
        filter_name="byid",
        filter_arg=None,
    )
    assert res == {"error": "this filter needs a value"}


def test_start_transfer_filter_ignored_on_push():
    # A filter selected for an upload (push, local source) is silently ignored —
    # there's no remote source to exec on. The bad filter name must NOT error.
    b = _ValidationBridge(filters=[])
    # local→remote push; we only check it gets past filter resolution. _host_entry
    # returns reachable, so it proceeds to spawn a thread — give it the pieces.
    b.get_config = lambda: {"ssh": {}}
    b._current_user = lambda: "tester"
    b._lock = threading.Lock()
    b._in_flight = {}
    b._open_sftp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop"))
    b._dispatch = lambda *a, **k: None
    res = b.start_transfer(
        "local",
        ["/a.log"],
        "dst",
        "/incoming",
        filter_name="nonexistent",  # would error if push honoured filters
    )
    assert "id" in res  # accepted; filter ignored for push
