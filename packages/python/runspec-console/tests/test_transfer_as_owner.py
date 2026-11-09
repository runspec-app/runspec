"""test_transfer_as_owner.py — copying to / from / between hosts as venv owners.

The Files tab's "Browse as" pane copies as that owner: an upload is staged as
the login in ``~/.runspec-staging`` and then written into place by the files
worker running as the owner; a download is read by the worker (a filter runs on
its output as the login). A relay does the same on each end; copying to several
hosts skips one where the account owns no venv. Real Bridge methods are bound
onto a light harness. Every "host" is this machine: SFTP is a thin shim over the local filesystem, and
an exec channel runs the command in a local ``sh`` after checking its shape and
dropping the ``sudo -n -u svc`` prefix (the worker runs as the test user).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from runspec_console import _files_worker as worker
from runspec_console.bridge import Bridge

pytestmark = [
    pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths"),
]

APP_PY = "/opt/venvs/app/bin/python"
SUDO = f"sudo -n -u svc {APP_PY} -I -S -c "


class LocalChannel:
    """Runs the exec command locally (minus sudo, on this interpreter)."""

    def __init__(self, harness):
        self.h = harness
        self._out = b""
        self._err = b""
        self._exit = 0
        self.closed = False

    def set_combine_stderr(self, _v):
        pass

    def exec_command(self, cmd):
        self.h.commands.append(cmd)
        assert cmd.startswith(SUDO), cmd
        if self.h.sudo_refuses:
            self._err = b"sudo: a password is required\n"
            self._exit = 1
            return
        local = sys.executable + " -I -S -c " + cmd[len(SUDO) :]
        p = subprocess.run(["sh", "-c", local], capture_output=True)
        self._out, self._err, self._exit = p.stdout, p.stderr, p.returncode

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


class LocalSftp:
    """The SFTP calls the transfer uses, on the local filesystem; ``home`` is
    the login's home."""

    def __init__(self, home: Path):
        self.home = home

    def normalize(self, _p):
        return str(self.home)

    def mkdir(self, path, mode=0o777):
        os.mkdir(path, mode)

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def put(self, src, dst, callback=None):
        shutil.copyfile(src, dst)
        if callback:
            n = os.path.getsize(dst)
            callback(n, n)

    def open(self, path, mode="rb"):
        return open(path, mode)

    def remove(self, path):
        os.remove(path)

    def stat(self, path):
        return os.stat(path)


class _Harness:
    start_transfer = Bridge.start_transfer
    _normalise_transfer_side = Bridge._normalise_transfer_side
    _push_as_owner = Bridge._push_as_owner
    _pull_as_owner = Bridge._pull_as_owner
    _exec_collect = Bridge._exec_collect
    _run_filter_stream = Bridge._run_filter_stream
    _run_filter_stream_local = Bridge._run_filter_stream_local
    _transfer_one = Bridge._transfer_one
    _transfer_one_filtered_local = Bridge._transfer_one_filtered_local
    _owner_command = Bridge._owner_command
    _files_as = Bridge._files_as
    files_accounts = Bridge.files_accounts
    _venv_python = Bridge._venv_python
    _filter_entry = Bridge._filter_entry
    start_relay = Bridge.start_relay
    start_fanout = Bridge.start_fanout
    _owner_venv = Bridge._owner_venv
    _stage_path = Bridge._stage_path
    _place_as_owner = Bridge._place_as_owner
    _owner_read_command = Bridge._owner_read_command
    _stream_as_owner = Bridge._stream_as_owner
    _relay_one_as = Bridge._relay_one_as
    _relay_one = Bridge._relay_one
    _relay_one_filtered = Bridge._relay_one_filtered

    def __init__(self, home: Path, filters=None):
        self._filters = filters or []
        self._lock = threading.Lock()
        self._in_flight: dict = {}
        self._recent_transfer_results: dict = {}
        # web-01 and db-01: svc owns the app venv. db-02: only another
        # account's venv. db-03: svc is the SSH login itself.
        owners = {"web-01": "svc", "db-01": "svc", "db-02": "app2", "db-03": "svc"}
        self._runnables_cache = [
            {
                "host": h,
                "name": "a",
                "group": "app",
                "loginUser": "svc" if h == "db-03" else "ops",
                "venvOwner": owner,
            }
            for h, owner in owners.items()
        ]
        self.entries = {
            h: {
                "name": h,
                "ssh": f"{'svc' if h == 'db-03' else 'ops'}@{h}",
                "runspec_paths": ["/opt/venvs/app/bin/runspec"],
            }
            for h in owners
        }
        self.sftp = LocalSftp(home)
        self.commands: list[str] = []
        self.sudo_refuses = False
        self.done = threading.Event()
        self.result: dict = {}

    def _host_entry(self, name):
        return self.entries.get(name)

    @staticmethod
    def _resolve_adhoc_credential(_cred_id, user, password):
        return user, password

    def get_config(self):
        return {"ssh": {}}

    def _current_user(self):
        return "jason"

    def _dispatch(self, *_a, **_k):
        pass

    def _open_source_conn(self, desc, pw, cfg):
        return self.sftp, (lambda: (LocalChannel(self), lambda: None)), None

    def _open_sftp(self, target, identity, cfg):
        return self.sftp, None

    def _emit_transfer_end(self, tid, payload):
        self.result = payload
        self.done.set()

    def run(self, *args, method="start_transfer", **kw):
        res = getattr(self, method)(*args, **kw)
        assert "id" in res, res
        assert self.done.wait(10)
        return self.result


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    owned = tmp_path / "owned"
    owned.mkdir()
    here = tmp_path / "here"
    here.mkdir()
    return home, owned, here


def test_upload_as_owner_stages_then_places(dirs):
    home, owned, here = dirs
    src = here / "app.conf"
    src.write_text("key = 1\n")
    (owned / "app.conf").write_text("old\n")
    os.chmod(owned / "app.conf", 0o640)
    h = _Harness(home)
    res = h.run("local", [str(src)], "web-01", str(owned), as_user="svc")
    assert res["ok"] is True, res
    assert (owned / "app.conf").read_text() == "key = 1\n"
    # The existing file kept its mode; no part file or staged copy is left.
    assert (os.stat(owned / "app.conf").st_mode & 0o777) == 0o640
    assert sorted(p.name for p in owned.iterdir()) == ["app.conf"]
    assert list((home / ".runspec-staging").iterdir()) == []
    assert (os.stat(home / ".runspec-staging").st_mode & 0o777) == 0o700
    # The owner's command reads the staged file from stdin, not a shell as svc.
    (cmd,) = h.commands
    assert cmd.startswith(SUDO)
    assert '"op": "place"' in cmd and " < " in cmd


def test_upload_as_owner_with_a_local_filter(dirs):
    home, owned, here = dirs
    src = here / "app.log"
    src.write_text("ok\nERROR one\nok\nERROR two\n")
    h = _Harness(home, filters=[{"name": "errs", "pipeline": "grep ERROR"}])
    res = h.run(
        "local", [str(src)], "web-01", str(owned), filter_name="errs", as_user="svc"
    )
    assert res["ok"] is True, res
    assert (owned / "app.log").read_text() == "ERROR one\nERROR two\n"


def test_download_as_owner_and_with_a_filter(dirs):
    home, owned, here = dirs
    (owned / "app.log").write_text("ok\nERROR one\n")
    h = _Harness(home, filters=[{"name": "errs", "pipeline": "grep ERROR"}])
    res = h.run("web-01", [str(owned / "app.log")], "local", str(here), as_user="svc")
    assert res["ok"] is True, res
    assert (here / "app.log").read_text() == "ok\nERROR one\n"
    assert '"op": "cat"' in h.commands[0]

    h2 = _Harness(home, filters=[{"name": "errs", "pipeline": "grep ERROR"}])
    res = h2.run(
        "web-01",
        [str(owned / "app.log")],
        "local",
        str(here),
        filter_name="errs",
        dest_name="errors.log",
        as_user="svc",
    )
    assert res["ok"] is True, res
    assert (here / "errors.log").read_text() == "ERROR one\n"
    assert h2.commands[0].endswith("| sh -c 'grep ERROR'")


def test_download_as_owner_missing_file_fails_even_through_a_filter(dirs):
    home, owned, here = dirs
    h = _Harness(home, filters=[{"name": "all", "pipeline": "cat"}])
    res = h.run(
        "web-01",
        [str(owned / "gone.log")],
        "local",
        str(here),
        filter_name="all",
        as_user="svc",
    )
    assert res["ok"] is False
    assert "gone.log" in res["error"] and worker.ERROR_PREFIX not in res["error"]
    assert not (here / "gone.log").exists()


def test_sudo_refusal_is_plain_and_cleans_up(dirs):
    home, owned, here = dirs
    src = here / "a.txt"
    src.write_text("x")
    h = _Harness(home)
    h.sudo_refuses = True
    res = h.run("local", [str(src)], "web-01", str(owned), as_user="svc")
    assert res["ok"] is False
    assert "without a password" in res["error"]
    assert not (owned / "a.txt").exists()
    assert list((home / ".runspec-staging").iterdir()) == []


def test_as_user_is_checked(dirs):
    home, owned, here = dirs
    h = _Harness(home)
    assert h.start_transfer(
        "local", [str(here / "a")], "web-01", str(owned), as_user="root"
    ) == {"error": "root isn't a venv owner on web-01"}
    adhoc = {"kind": "adhoc", "host": "10.0.0.5", "jump": "web-01"}
    assert h.start_transfer(
        "local", [str(here / "a")], adhoc, "/tmp", as_user="svc"
    ) == {"error": "copying as another account needs a configured host"}
    assert h.commands == []


def test_worker_place_and_cat(tmp_path):
    import io

    dest = tmp_path / "f.bin"
    res = worker.op_place({"path": str(dest)}, io.BytesIO(b"\x00\x01data"))
    assert res["ok"] and dest.read_bytes() == b"\x00\x01data"
    assert worker.op_place({"path": str(tmp_path)}, io.BytesIO(b""))["ok"] is False
    out = io.BytesIO()
    assert worker.op_cat({"path": str(dest)}, out) is None
    assert out.getvalue() == b"\x00\x01data"
    assert "folder" in worker.op_cat({"path": str(tmp_path)}, io.BytesIO())


HOST = {"kind": "host", "host": "web-01"}


def test_relay_reads_as_the_source_owner(dirs):
    home, owned, here = dirs
    (owned / "app.log").write_text("ok\nERROR one\n")
    h = _Harness(home, filters=[{"name": "errs", "pipeline": "grep ERROR"}])
    res = h.run(
        dict(HOST),
        [str(owned / "app.log")],
        "db-01",
        str(here),
        filter_name="errs",
        source_as="svc",
        method="start_relay",
    )
    assert res["ok"] is True, res
    assert (here / "app.log").read_text() == "ERROR one\n"
    (cmd,) = h.commands
    assert '"op": "cat"' in cmd and cmd.endswith("| sh -c 'grep ERROR'")


def test_relay_writes_as_the_destination_owner(dirs):
    home, owned, here = dirs
    (here / "app.conf").write_text("key = 2\n")
    h = _Harness(home)
    res = h.run(
        dict(HOST),
        [str(here / "app.conf")],
        "db-01",
        str(owned),
        dest_as="svc",
        method="start_relay",
    )
    assert res["ok"] is True, res
    assert (owned / "app.conf").read_text() == "key = 2\n"
    assert sorted(p.name for p in owned.iterdir()) == ["app.conf"]
    assert list((home / ".runspec-staging").iterdir()) == []
    (cmd,) = h.commands
    assert '"op": "place"' in cmd and " < " in cmd


def test_relay_as_owners_on_both_ends(dirs):
    home, owned, here = dirs
    (owned / "a.bin").write_bytes(b"\x00\x01two")
    h = _Harness(home)
    res = h.run(
        dict(HOST),
        [str(owned / "a.bin")],
        "db-01",
        str(here),
        dest_name="b.bin",
        source_as="svc",
        dest_as="svc",
        method="start_relay",
    )
    assert res["ok"] is True, res
    assert (here / "b.bin").read_bytes() == b"\x00\x01two"
    assert ['"op": "cat"' in c for c in h.commands] == [True, False]
    assert list((home / ".runspec-staging").iterdir()) == []


def test_relay_missing_source_as_owner_fails_and_cleans_up(dirs):
    home, owned, here = dirs
    h = _Harness(home)
    res = h.run(
        dict(HOST),
        [str(owned / "gone.log")],
        "db-01",
        str(here),
        source_as="svc",
        dest_as="svc",
        method="start_relay",
    )
    assert res["ok"] is False
    assert "gone.log" in res["error"]
    assert not (here / "gone.log").exists()
    assert list((home / ".runspec-staging").iterdir()) == []


def test_relay_accounts_are_checked(dirs):
    home, owned, here = dirs
    h = _Harness(home)
    assert h.start_relay(
        dict(HOST), [str(owned / "a")], "db-02", str(here), dest_as="svc"
    ) == {"error": "svc isn't a venv owner on db-02"}
    adhoc = {"kind": "adhoc", "host": "10.0.0.5", "jump": "web-01"}
    assert h.start_relay(
        adhoc, [str(owned / "a")], "db-01", str(here), source_as="svc"
    ) == {"error": "copying as another account needs a configured host"}
    assert h.commands == []


def test_fanout_copies_as_the_owner_and_skips_hosts_without_one(dirs):
    home, owned, here = dirs
    src = here / "app.conf"
    src.write_text("key = 3\n")
    h = _Harness(home)
    res = h.run(
        [str(src)],
        ["web-01", "db-02", "db-03"],
        str(owned),
        as_user="svc",
        method="start_fanout",
    )
    assert res["ok"] is True, res
    assert (owned / "app.conf").read_text() == "key = 3\n"
    by_host = {r["host"]: r for r in res["hosts"]}
    assert by_host["web-01"]["ok"] is True
    # db-03's login is svc, so it copies as usual.
    assert by_host["db-03"]["ok"] is True and not by_host["db-03"].get("skipped")
    assert by_host["db-02"] == {
        "host": "db-02",
        "ok": False,
        "skipped": True,
        "error": "skipped: svc owns no venv on db-02",
    }
    assert "skipped db-02" in res["error"]
    # Only web-01 went through the owner (one place).
    (cmd,) = h.commands
    assert '"op": "place"' in cmd


def test_fanout_first_host_must_have_the_owner(dirs):
    home, owned, here = dirs
    h = _Harness(home)
    assert h.start_fanout(
        [str(here / "a")], ["db-02", "web-01"], str(owned), as_user="svc"
    ) == {"error": "svc isn't a venv owner on db-02"}
