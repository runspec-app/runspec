"""test_bridge_envfile.py — the .runspec_env manager bridge seam.

A stub host whose "SSH" runs the command in a local shell, with each remote
venv a temp dir whose ``bin/python`` is this interpreter — so the worker
really reads and writes. Escalation is checked with a scripted runner that
refuses the login and answers only under ``sudo -n -u <owner>``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading

import pytest

from runspec_console import _envfile_worker as worker
from runspec_console.bridge_envfile import EnvFileMixin

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


def _venv(root):
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python").symlink_to(sys.executable)
    rs = root / "bin" / "runspec"
    rs.write_text("#!/bin/sh\nexit 0\n")
    rs.chmod(0o755)
    return str(rs)


class _Stub(EnvFileMixin):
    def __init__(self, hosts, records=None):
        self._hosts = hosts
        self._connected_cache = {h["name"]: True for h in hosts if h.get("ssh")}
        self._lock = threading.Lock()
        self._runnables_cache = records or []
        self.audit: list[dict] = []
        self.commands: list[str] = []
        self.stdins: list[bytes | None] = []
        self.runner = None

    def _host_entry(self, name):
        return next((h for h in self._hosts if h["name"] == name), None)

    def _all_host_entries(self):
        return list(self._hosts)

    def _fanout_workers(self):
        return 4

    def _current_user(self):
        return "operator"

    def _append_tool_audit(self, record):
        self.audit.append(record)

    def _ssh_run(self, ssh, command, *, identity_file=None, timeout=15, stdin=None):
        self.commands.append(command)
        self.stdins.append(stdin)
        if self.runner:
            return self.runner(command, stdin)
        r = subprocess.run(
            command, shell=True, input=stdin, capture_output=True, timeout=60
        )
        return r.returncode, r.stdout.decode(), r.stderr.decode()


@pytest.fixture
def fleet(tmp_path):
    a = _venv(tmp_path / "web" / "ops")
    b = _venv(tmp_path / "db" / "ops")
    local = _venv(tmp_path / "local" / "console")
    hosts = [
        {"name": "local", "ssh": None, "runspec_paths": [local]},
        {
            "name": "web-01",
            "ssh": "deploy@web-01",
            "group": "Web",
            "runspec_paths": [a],
        },
        {"name": "db-01", "ssh": "deploy@db-01", "group": "Db", "runspec_paths": [b]},
    ]
    records = [
        {
            "name": "jira",
            "host": "web-01",
            "group": "ops",
            "args": [{"name": "jira-pat", "type": "password"}],
            "rawSpec": {},
        },
    ]
    return _Stub(hosts, records), tmp_path, (local, a, b)


def test_targets_lists_local_and_named_remotes(fleet):
    stub, tmp, (local, a, b) = fleet
    rows = stub.env_file_targets(["web-01"])
    assert [(r["host"], r["venv"]) for r in rows] == [
        ("local", "console"),
        ("web-01", "ops"),
    ]
    assert rows[1]["path"] == str(tmp / "web" / "ops" / ".runspec_env")
    assert all(r["supported"] for r in rows)


def test_targets_flags_windows_remote():
    stub = _Stub(
        [
            {
                "name": "win",
                "ssh": "x@win",
                "runspec_paths": ["C:\\v\\Scripts\\runspec.exe"],
            }
        ]
    )
    (row,) = stub.env_file_targets(["win"])
    assert not row["supported"] and "Linux" in row["reason"]


def test_read_save_and_conflict_remote(fleet):
    stub, tmp, (_l, a, _b) = fleet
    f = tmp / "web" / "ops" / ".runspec_env"
    f.write_text("# hi\nRUNSPEC_JIRA_ARG_JIRA_PAT=hunter2\nURL=x\n")
    view = stub.env_file_read({"host": "web-01", "runspec_path": a})
    assert view["ok"] and view["writable"]
    by = {e["key"]: e for e in view["entries"]}
    assert by["RUNSPEC_JIRA_ARG_JIRA_PAT"]["masked"]
    assert by["RUNSPEC_JIRA_ARG_JIRA_PAT"]["readers"][0]["runnable"] == "jira"
    assert "hunter2" not in json.dumps(view)
    # Nothing secret on any command line; the worker source rides argv.
    assert not any("hunter2" in c for c in stub.commands)

    saved = stub.env_file_save(
        {
            "host": "web-01",
            "runspec_path": a,
            "sha": view["sha"],
            "set": {"URL": "https://y", "NEW": "1"},
            "unset": [],
        }
    )
    assert saved["saved"] and saved["changes"] == {
        "added": ["NEW"],
        "changed": ["URL"],
        "removed": [],
    }
    assert (
        f.read_text()
        == "# hi\nRUNSPEC_JIRA_ARG_JIRA_PAT=hunter2\nURL=https://y\nNEW=1\n"
    )
    # The new text went over stdin, not argv.
    assert any(s and b"https://y" in s for s in stub.stdins)
    assert not any("https://y" in c for c in stub.commands)
    ev = [r["extra"] for r in stub.audit if r["extra"]["event"] == "venv.env.changed"][
        0
    ]
    assert ev["added"] == ["NEW"] and "https://y" not in json.dumps(stub.audit)

    stale = stub.env_file_save(
        {"host": "web-01", "runspec_path": a, "sha": view["sha"], "set": {"URL": "z"}}
    )
    assert not stale["ok"] and stale["conflict"]

    rev = stub.env_file_reveal(
        {"host": "web-01", "runspec_path": a, "key": "RUNSPEC_JIRA_ARG_JIRA_PAT"}
    )
    assert rev == {
        "key": "RUNSPEC_JIRA_ARG_JIRA_PAT",
        "value": "hunter2",
        "found": True,
    }
    assert stub.audit[-1]["extra"]["event"] == "venv.env.revealed"


def test_local_venv_in_process(fleet):
    stub, tmp, (local, _a, _b) = fleet
    view = stub.env_file_read({"host": "local", "runspec_path": local})
    assert view["ok"] and not view["exists"]
    stub.env_file_save(
        {"host": "local", "runspec_path": local, "sha": view["sha"], "set": {"A": "1"}}
    )
    assert (tmp / "local" / "console" / ".runspec_env").read_text() == "A=1\n"
    assert not [c for c in stub.commands if "-c" in c]


def test_read_many_and_save_many(fleet):
    stub, tmp, (local, a, b) = fleet
    (tmp / "web" / "ops" / ".runspec_env").write_text(
        "KEEP=1\nREGION=eu\nRUNSPEC_JIRA_ARG_JIRA_PAT=hunter2\n"
    )
    (tmp / "db" / "ops" / ".runspec_env").write_text(
        "REGION=us\nRUNSPEC_JIRA_ARG_JIRA_PAT=hunter2\n"
    )
    targets = [
        {"host": "web-01", "runspec_path": a},
        {"host": "db-01", "runspec_path": b},
        {"host": "local", "runspec_path": local},
    ]
    views = stub.env_file_read_many({"targets": targets})["views"]
    assert [v["host"] for v in views] == ["web-01", "db-01", "local"]
    assert all(v["ok"] for v in views)
    assert "hunter2" not in json.dumps(views)
    pat = "RUNSPEC_JIRA_ARG_JIRA_PAT"
    fps = [next(e["fp"] for e in v["entries"] if e["key"] == pat) for v in views[:2]]
    # db-01 has no jira reader, so its PAT is masked by name alone; same secret,
    # same fingerprint.
    assert fps[0] == fps[1] and len(fps[0]) == 12

    res = stub.env_file_save_many(
        {
            "changes": [
                {
                    "host": "web-01",
                    "runspec_path": a,
                    "sha": views[0]["sha"],
                    "set": {"REGION": "us"},
                    "unset": ["KEEP"],
                },
                {
                    "host": "db-01",
                    "runspec_path": b,
                    "sha": "stale",
                    "set": {"REGION": "eu"},
                },
                {
                    "host": "local",
                    "runspec_path": local,
                    "sha": views[2]["sha"],
                    "set": {"REGION": "us"},
                },
            ]
        }
    )
    assert (res["ok"], res["saved"], res["failed"]) == (2, 2, 1)
    web, db, loc = res["rows"]
    assert web["saved"] and web["changes"] == {
        "added": [],
        "changed": ["REGION"],
        "removed": ["KEEP"],
    }
    assert (tmp / "web" / "ops" / ".runspec_env").read_text() == (
        "REGION=us\nRUNSPEC_JIRA_ARG_JIRA_PAT=hunter2\n"
    )
    # A venv changed since the grid loaded is refused; the others still save.
    assert not db["ok"] and db["conflict"]
    assert (tmp / "db" / "ops" / ".runspec_env").read_text().startswith("REGION=us")
    assert loc["saved"] and loc["view"]["entries"][0]["value"] == "us"
    assert web["view"]["sha"] != views[0]["sha"]


def test_read_many_isolates_failures(fleet):
    stub, tmp, (local, a, b) = fleet
    (tmp / "web" / "ops" / "bin" / "python").unlink()
    views = stub.env_file_read_many(
        {
            "targets": [
                {"host": "web-01", "runspec_path": a},
                {"host": "local", "runspec_path": local},
            ]
        }
    )["views"]
    assert not views[0]["ok"] and views[0]["error"]
    assert views[1]["ok"]


def test_save_many_needs_changes(fleet):
    stub, _tmp, (local, _a, _b) = fleet
    with pytest.raises(ValueError):
        stub.env_file_save_many({"changes": [{"host": "local", "runspec_path": local}]})
    with pytest.raises(ValueError):
        stub.env_file_read_many({"targets": []})


def test_unknown_path_and_bad_key(fleet):
    stub, _tmp, (_l, a, _b) = fleet
    with pytest.raises(ValueError):
        stub.env_file_read({"host": "web-01", "runspec_path": "/elsewhere/bin/runspec"})
    with pytest.raises(ValueError):
        stub.env_file_save_many(
            {"changes": [{"host": "web-01", "runspec_path": a, "set": {"1X": ""}}]}
        )


def _scripted(owner_ok: bool):
    """A runner where the login (deploy) can't read the 0600 file; only
    ``sudo -n -u svc`` (when ``owner_ok``) runs the worker for real."""

    def run(command, stdin):
        argv = shlex.split(command)
        if argv[:2] == ["sudo", "-n"]:
            user = argv[3]
            if user == "svc" and owner_ok:
                real = argv[4:]
                p = subprocess.run(real, input=stdin, capture_output=True)
                out = p.stdout.decode().replace(
                    '"user": "' + _me() + '"', '"user": "svc"'
                )
                return p.returncode, out, p.stderr.decode()
            return 1, "", "sudo: a password is required"
        if "-c" in argv and argv[argv.index("-c") + 2] == "read":
            res = {
                "ok": True,
                "path": "",
                "user": "deploy",
                "owner": "svc",
                "exists": True,
                "symlink": False,
                "readable": False,
                "writable": False,
                "mode": "0600",
                "sha": "",
                "text": "",
                "error": "Can't read: Permission denied",
            }
            return 0, worker.MARKER + json.dumps(res) + "\n", ""
        return 0, "", ""

    return run


def _me():
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid())


def test_escalates_to_the_venv_owner(fleet):
    stub, tmp, (_l, a, _b) = fleet
    (tmp / "web" / "ops" / ".runspec_env").write_text("A=1\n")
    stub.runner = _scripted(owner_ok=True)
    view = stub.env_file_read({"host": "web-01", "runspec_path": a})
    assert view["ok"] and view["writable"] and view["as_user"] == "svc"
    out = stub.env_file_save(
        {"host": "web-01", "runspec_path": a, "sha": view["sha"], "set": {"A": "2"}}
    )
    assert out["saved"]
    assert any(
        c.startswith("sudo -n -u svc ") and " write " in c for c in stub.commands
    )


def test_escalation_refused_explains(fleet):
    stub, _tmp, (_l, a, _b) = fleet
    stub.runner = _scripted(owner_ok=False)
    view = stub.env_file_read({"host": "web-01", "runspec_path": a})
    assert not view["ok"]
    assert "passwordless sudo" in view["error"] and "svc" in view["error"]
