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

import os
import shutil
import threading
import types

import pytest

from runspec_console.bridge import Bridge

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on PATH")


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
    _run_filter_stream_local = Bridge._run_filter_stream_local
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


# --- local filter pump (upload path), real subprocess ---------------------


@needs_sh
def test_local_filter_stream_keeps_matching_lines(tmp_path):
    h = _Harness()
    src = tmp_path / "in.log"
    src.write_text("alpha txn-1\nbeta\ngamma txn-1\n")
    dst = tmp_path / "out.log"
    h._run_filter_stream_local(
        "grep -F -- txn-1",
        str(src),
        dst_open=lambda: open(dst, "wb"),
        dst_remove=lambda: os.remove(dst),
        in_size=src.stat().st_size,
        item=types.SimpleNamespace(name="in.log", src=str(src), dst=str(dst)),
        tid="t",
        idx=0,
        count=1,
        cancel_event=threading.Event(),
    )
    assert dst.read_text() == "alpha txn-1\ngamma txn-1\n"


@needs_sh
def test_local_filter_no_match_is_empty_success(tmp_path):
    h = _Harness()
    src = tmp_path / "in.log"
    src.write_text("alpha\nbeta\n")
    dst = tmp_path / "out.log"
    removed = {"called": False}
    h._run_filter_stream_local(
        "grep -F -- zzz-nomatch",
        str(src),
        dst_open=lambda: open(dst, "wb"),
        dst_remove=lambda: removed.__setitem__("called", True),
        in_size=src.stat().st_size,
        item=types.SimpleNamespace(name="in.log", src=str(src), dst=str(dst)),
        tid="t",
        idx=0,
        count=1,
        cancel_event=threading.Event(),
    )
    assert dst.read_text() == ""  # empty result, kept (grep exit 1, no stderr)
    assert not removed["called"]


@needs_sh
def test_local_filter_real_error_removes_partial(tmp_path):
    h = _Harness()
    src = tmp_path / "in.log"
    src.write_text("x\n")
    dst = tmp_path / "out.log"
    removed = {"called": False}
    with pytest.raises(RuntimeError):
        h._run_filter_stream_local(
            "grep --bogus-flag x",  # grep errors → exit 2 + stderr
            str(src),
            dst_open=lambda: open(dst, "wb"),
            dst_remove=lambda: removed.__setitem__("called", True),
            in_size=src.stat().st_size,
            item=types.SimpleNamespace(name="in.log", src=str(src), dst=str(dst)),
            tid="t",
            idx=0,
            count=1,
            cancel_event=threading.Event(),
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
    _normalise_transfer_side = Bridge._normalise_transfer_side
    _emit_transfer_end = Bridge._emit_transfer_end

    def __init__(self, filters):
        self._filters = filters
        self._recent_transfer_results: dict = {}
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


def _push_bridge(filters):
    # A _ValidationBridge wired enough to let start_transfer push spawn its worker
    # thread (which immediately stops at the stubbed _open_sftp).
    b = _ValidationBridge(filters=filters)
    b.get_config = lambda: {"ssh": {}}
    b._current_user = lambda: "tester"
    b._lock = threading.Lock()
    b._in_flight = {}
    b._open_sftp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop"))
    b._open_source_conn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop"))
    b._dispatch = lambda *a, **k: None
    return b


def test_start_transfer_push_honors_filter():
    # An upload (push) now filters locally, so a valid filter is accepted.
    b = _push_bridge([{"name": "errs", "pipeline": "grep -i error"}])
    res = b.start_transfer("local", ["/a.log"], "dst", "/incoming", filter_name="errs")
    assert "id" in res


def test_start_transfer_push_unknown_filter_errors():
    b = _push_bridge([])
    res = b.start_transfer(
        "local", ["/a.log"], "dst", "/incoming", filter_name="nonexistent"
    )
    assert res == {"error": "unknown filter: nonexistent"}


# --- agent tools: download_file / relay_file ------------------------------


class _ToolBridge:
    _custom_agent_tools = Bridge._custom_agent_tools
    _denylist_tool = Bridge._denylist_tool
    _outlook_tools = Bridge._outlook_tools
    _repo_tools = Bridge._repo_tools
    _memory_tools = Bridge._memory_tools
    _status_tools = Bridge._status_tools
    _workspace_tools = Bridge._workspace_tools
    _exec_tools = Bridge._exec_tools
    _action_tools = Bridge._action_tools
    _config_tools = Bridge._config_tools
    _run_custom_tool = Bridge._run_custom_tool
    _await_transfer = Bridge._await_transfer
    _emit_transfer_end = Bridge._emit_transfer_end
    _effective_tool_autonomy = Bridge._effective_tool_autonomy

    def __init__(self, hosts, filters, credentials=None):
        self._hosts = hosts
        self._filters = filters
        self._credentials = credentials or []
        self._lock = threading.Lock()
        self._recent_transfer_results: dict = {}
        self.calls: list = []

    def _reload_credentials(self):
        pass

    def _dispatch(self, *a, **k):
        pass

    def start_transfer(self, *a, **k):
        self.calls.append(("transfer", a, k))
        self._emit_transfer_end("t", {"id": "t", "ok": True, "files": 1, "error": None})
        return {"id": "t"}

    def start_relay(self, *a, **k):
        self.calls.append(("relay", a, k))
        self._emit_transfer_end("r", {"id": "r", "ok": True, "files": 1, "error": None})
        return {"id": "r"}


def test_custom_tools_schema_lists_hosts_and_filters():
    b = _ToolBridge(
        hosts=[{"name": "prod", "ssh": "deploy@prod"}, {"name": "local"}],
        filters=[
            {"name": "byid", "pipeline": "grep -F -- {{arg}}", "param_label": "id"},
        ],
    )
    tools = b._custom_agent_tools()
    names = {t["name"] for t in tools}
    # Transfer tools (gated on a remote host) plus the always-on memory tools.
    assert {"download_file", "relay_file"} <= names
    assert {"remember", "search_memory", "save_runbook"} <= names
    dl = next(t for t in tools if t["name"] == "download_file")
    props = dl["input_schema"]["properties"]
    # Only the reachable remote host is offered (not "local").
    assert props["host"]["enum"] == ["prod"]
    assert props["filter"]["enum"] == ["byid"]
    assert "filter_value" in props
    # The filter's parameterization is surfaced to the model.
    assert "filter_value" in props["filter"]["description"]


def test_custom_tools_empty_without_remote_hosts():
    # With no remote host there are no transfer tools, but the local memory tools
    # are still offered (they don't need SSH).
    b = _ToolBridge(hosts=[{"name": "local"}], filters=[])
    names = {t["name"] for t in b._custom_agent_tools()}
    assert "download_file" not in names and "relay_file" not in names
    assert {"remember", "search_memory", "save_runbook"} <= names


def test_custom_tools_are_gated():
    b = _ToolBridge(hosts=[], filters=[])
    assert b._effective_tool_autonomy("download_file", {}) == "confirm"
    assert b._effective_tool_autonomy("relay_file", {}) == "confirm"


def test_run_download_tool_success_and_args():
    b = _ToolBridge(hosts=[{"name": "prod", "ssh": "x"}], filters=[])
    out = b._run_custom_tool(
        "download_file",
        {
            "host": "prod",
            "path": "/var/log/app.log",
            "dest_dir": "/tmp",
            "filter": "errs",
        },
    )
    assert out.startswith("✓ Completed download of app.log from prod")
    assert "/tmp" in out
    # start_transfer called local-bound with the filter threaded through
    # (trailing None = no rename).
    kind, args, _ = b.calls[0]
    assert kind == "transfer"
    assert args == ("prod", ["/var/log/app.log"], "local", "/tmp", "errs", None, None)


def test_download_schema_offers_adhoc_source_and_credentials():
    b = _ToolBridge(
        hosts=[{"name": "jump", "ssh": "x"}],
        filters=[],
        credentials=[{"id": "svc", "label": "Svc"}],
    )
    dl = next(t for t in b._custom_agent_tools() if t["name"] == "download_file")
    props = dl["input_schema"]["properties"]
    assert props["source_via"]["enum"] == ["jump"]
    assert "source_target" in props
    assert props["source_credential"]["enum"] == ["svc"]
    # host is no longer required — an ad-hoc source is an alternative.
    assert dl["input_schema"]["required"] == ["path", "dest_dir"]


def test_run_download_tool_adhoc_builds_credentialed_source():
    b = _ToolBridge(
        hosts=[{"name": "jump", "ssh": "x"}],
        filters=[],
        credentials=[{"id": "svc", "label": "Svc"}],
    )
    out = b._run_custom_tool(
        "download_file",
        {
            "source_via": "jump",
            "source_target": "10.0.0.9",
            "source_credential": "svc",
            "path": "/v/x.log",
            "dest_dir": "/tmp",
        },
    )
    assert out.startswith("✓ Completed download of x.log from 10.0.0.9 via jump")
    kind, args, _ = b.calls[0]
    assert kind == "transfer"
    # An ad-hoc source descriptor is passed as start_transfer's source; dest local.
    assert args[0] == {
        "kind": "adhoc",
        "host": "10.0.0.9",
        "jump": "jump",
        "credential_id": "svc",
    }
    assert args[2] == "local"


def test_run_download_tool_requires_a_source():
    b = _ToolBridge(hosts=[{"name": "prod", "ssh": "x"}], filters=[])
    out = b._run_custom_tool("download_file", {"path": "/v/x.log", "dest_dir": "/tmp"})
    assert "provide host" in out
    assert b.calls == []  # never started a transfer


def test_run_download_tool_threads_dest_name():
    b = _ToolBridge(hosts=[{"name": "prod", "ssh": "x"}], filters=[])
    out = b._run_custom_tool(
        "download_file",
        {
            "host": "prod",
            "path": "/v/x.log",
            "dest_dir": "/tmp",
            "dest_name": "renamed.log",
        },
    )
    assert out.startswith("✓ Completed download of renamed.log from prod")
    _, args, _ = b.calls[0]
    # dest_name is the 7th positional arg to start_transfer.
    assert args == ("prod", ["/v/x.log"], "local", "/tmp", "", None, "renamed.log")


def test_download_schema_offers_dest_name():
    b = _ToolBridge(hosts=[{"name": "prod", "ssh": "x"}], filters=[])
    dl = next(t for t in b._custom_agent_tools() if t["name"] == "download_file")
    assert "dest_name" in dl["input_schema"]["properties"]


def test_run_relay_tool_success():
    b = _ToolBridge(
        hosts=[{"name": "a", "ssh": "x"}, {"name": "b", "ssh": "y"}], filters=[]
    )
    out = b._run_custom_tool(
        "relay_file",
        {"source_host": "a", "path": "/v/x.log", "dest_host": "b", "dest_dir": "/in"},
    )
    assert out.startswith("✓ Completed relay of x.log from a to b")
    assert b.calls[0][0] == "relay"


def test_relay_schema_offers_adhoc_source_and_credentials():
    b = _ToolBridge(
        hosts=[{"name": "jump", "ssh": "x"}],
        filters=[],
        credentials=[{"id": "svc", "label": "Svc"}],
    )
    relay = next(t for t in b._custom_agent_tools() if t["name"] == "relay_file")
    props = relay["input_schema"]["properties"]
    assert props["source_via"]["enum"] == ["jump"]
    assert "source_target" in props
    assert props["source_credential"]["enum"] == ["svc"]
    # source_host is no longer required (ad-hoc source is an alternative).
    assert relay["input_schema"]["required"] == ["path", "dest_host", "dest_dir"]


def test_relay_schema_omits_credential_enum_when_none():
    b = _ToolBridge(hosts=[{"name": "jump", "ssh": "x"}], filters=[])
    relay = next(t for t in b._custom_agent_tools() if t["name"] == "relay_file")
    assert "source_credential" not in relay["input_schema"]["properties"]


def test_run_relay_tool_adhoc_builds_credentialed_source():
    b = _ToolBridge(
        hosts=[{"name": "jump", "ssh": "x"}, {"name": "dest", "ssh": "y"}],
        filters=[],
        credentials=[{"id": "svc", "label": "Svc"}],
    )
    out = b._run_custom_tool(
        "relay_file",
        {
            "source_via": "jump",
            "source_target": "10.0.0.9",
            "source_credential": "svc",
            "path": "/v/x.log",
            "dest_host": "dest",
            "dest_dir": "/in",
        },
    )
    assert out.startswith("✓ Completed relay of x.log from 10.0.0.9 via jump to dest")
    kind, args, _ = b.calls[0]
    assert kind == "relay"
    source = args[0]
    assert source == {
        "kind": "adhoc",
        "host": "10.0.0.9",
        "jump": "jump",
        "credential_id": "svc",
    }


def test_run_relay_tool_requires_a_source():
    b = _ToolBridge(hosts=[{"name": "dest", "ssh": "y"}], filters=[])
    out = b._run_custom_tool(
        "relay_file", {"path": "/v/x.log", "dest_host": "dest", "dest_dir": "/in"}
    )
    assert "provide source_host" in out


def test_run_download_tool_missing_args():
    b = _ToolBridge(hosts=[{"name": "prod", "ssh": "x"}], filters=[])
    out = b._run_custom_tool("download_file", {"host": "prod"})
    assert out.startswith("Error:")
    assert b.calls == []  # never started a transfer


def test_run_tool_reports_failure():
    b = _ToolBridge(hosts=[{"name": "prod", "ssh": "x"}], filters=[])

    def failing(*a, **k):
        b._emit_transfer_end("t", {"id": "t", "ok": False, "files": 0, "error": "boom"})
        return {"id": "t"}

    b.start_transfer = failing
    out = b._run_custom_tool(
        "download_file", {"host": "prod", "path": "/a.log", "dest_dir": "/tmp"}
    )
    assert out.startswith("✗")
    assert "boom" in out
