"""test_bridge_files.py — the Files tab's folder / view / rename / delete seams.

A stub host whose "SSH" runs the command in a local shell, so the remote
commands really run; the local host goes through the os path.
"""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from runspec_console import fs_browse
from runspec_console.bridge_files import FilesMixin


class _Stub(FilesMixin):
    def __init__(self) -> None:
        self.entries = {
            "local": {"name": "local"},
            "web-01": {"name": "web-01", "ssh": "ops@web-01"},
        }
        self._lock = threading.Lock()
        self._runnables_cache = [
            {"host": "web-01", "name": "a", "loginUser": "ops", "venvOwner": "svc"},
            {"host": "web-01", "name": "b", "loginUser": "ops", "venvOwner": "ops"},
            {"host": "web-02", "name": "c", "loginUser": "ops", "venvOwner": "other"},
        ]
        self.audit: list[dict] = []
        self.commands: list[str] = []

    def _append_tool_audit(self, rec):
        self.audit.append(rec)

    @staticmethod
    def _current_user():
        return "jason"

    def _host_entry(self, name):
        return self.entries.get(name)

    def _ssh_run(self, target, command, *, identity_file=None, timeout=15):
        self.commands.append(command)
        p = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr


posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


# ── pure helpers ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "a/b", "a\\b", "a\0b"])
def test_valid_folder_name_rejects(bad):
    with pytest.raises(ValueError):
        fs_browse.valid_folder_name(bad)


def test_valid_folder_name_strips():
    assert fs_browse.valid_folder_name("  logs 2026 ") == "logs 2026"


def test_remote_mkdir_command_quotes():
    cmd = fs_browse.remote_mkdir_command("/var/log", "my dir; rm -rf /")
    assert (
        cmd
        == "cd /var/log && mkdir -- 'my dir; rm -rf /' && cd -- 'my dir; rm -rf /' && pwd"
    )


def test_view_lines_clamps():
    assert fs_browse.view_lines(0) == 1
    assert fs_browse.view_lines(10**9) == fs_browse.VIEW_MAX_LINES
    assert fs_browse.view_lines("junk") == fs_browse.VIEW_DEFAULT_LINES


def test_parse_view_output_partial_and_full():
    full = fs_browse.parse_view_output("6\nab\ncd\n", 10)
    assert full == {
        "size": 6,
        "binary": False,
        "text": "ab\ncd",
        "lines": 2,
        "partial": False,
    }
    tail = fs_browse.parse_view_output("600\nx\ny\n", 10)
    assert tail["partial"] is True


def test_parse_view_output_binary():
    out = fs_browse.parse_view_output("4\na\0b\n", 10)
    assert out["binary"] is True and out["text"] == ""


# ── local host ────────────────────────────────────────────────────────────────


def test_make_dir_local(tmp_path):
    res = _Stub().make_dir("local", str(tmp_path), "new")
    assert res == {"path": str(tmp_path / "new")}
    assert (tmp_path / "new").is_dir()


def test_make_dir_local_existing_is_error(tmp_path):
    (tmp_path / "new").mkdir()
    assert "error" in _Stub().make_dir("local", str(tmp_path), "new")


def test_make_dir_bad_name(tmp_path):
    assert _Stub().make_dir("local", str(tmp_path), "../x") == {
        "error": "a folder name can't contain / or \\"
    }


def test_view_file_local_last_lines(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("".join(f"line {i}\n" for i in range(1, 101)))
    res = _Stub().view_file("local", str(f), 3)
    assert res["text"] == "line 98\nline 99\nline 100"
    assert (
        res["lines"] == 3 and res["partial"] is True and res["size"] == f.stat().st_size
    )


def test_view_file_local_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_browse, "VIEW_MAX_BYTES", 20)
    f = tmp_path / "big.log"
    f.write_text("a" * 100 + "\nlast line\n")
    res = _Stub().view_file("local", str(f), 50)
    assert res["text"].endswith("last line") and res["partial"] is True


def test_view_file_local_missing(tmp_path):
    assert "error" in _Stub().view_file("local", str(tmp_path / "nope"), 10)


def test_unknown_host():
    assert _Stub().make_dir("nope", "/tmp", "x") == {"error": "unknown host nope"}
    assert _Stub().view_file("nope", "/tmp/x", 10) == {"error": "unknown host nope"}


# ── remote host (stub ssh = local shell) ──────────────────────────────────────


@posix
def test_make_dir_remote(tmp_path):
    res = _Stub().make_dir("web-01", str(tmp_path), "in coming")
    assert res == {"path": str(tmp_path / "in coming")}
    assert (tmp_path / "in coming").is_dir()


@posix
def test_make_dir_remote_existing_is_error(tmp_path):
    (tmp_path / "x").mkdir()
    res = _Stub().make_dir("web-01", str(tmp_path), "x")
    assert "error" in res


@posix
def test_make_dir_remote_needs_parent():
    assert _Stub().make_dir("web-01", "", "x") == {"error": "open a folder first"}


@posix
def test_view_file_remote(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("".join(f"l{i}\n" for i in range(10)))
    res = _Stub().view_file("web-01", str(f), 2)
    assert (
        res["text"] == "l8\nl9" and res["size"] == f.stat().st_size and res["partial"]
    )


@posix
def test_view_file_remote_directory_is_error(tmp_path):
    assert "error" in _Stub().view_file("web-01", str(tmp_path), 5)


# ── rename / delete ───────────────────────────────────────────────────────────


def test_parse_delete_output():
    out = "ok\t/a\nerr\t/b\trmdir: failed to remove '/b': Directory not empty\n"
    assert fs_browse.parse_delete_output(out, ["/a", "/b", "/c"]) == {
        "deleted": ["/a"],
        "failed": [
            {"path": "/b", "error": "folder isn't empty"},
            {"path": "/c", "error": "not deleted"},
        ],
    }


def test_rename_local(tmp_path):
    (tmp_path / "a.log").write_text("x")
    stub = _Stub()
    res = stub.rename_path("local", str(tmp_path), "a.log", "b.log")
    assert res == {"path": str(tmp_path / "b.log")}
    assert (tmp_path / "b.log").read_text() == "x"
    assert stub.audit[0]["extra"]["event"] == "files.renamed"
    assert stub.audit[0]["extra"]["new"] == "b.log"


def test_rename_local_never_overwrites(tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("2")
    res = _Stub().rename_path("local", str(tmp_path), "a", "b")
    assert "already exists" in res["error"]
    assert (tmp_path / "b").read_text() == "2"


def test_rename_rejects_paths_and_same_name(tmp_path):
    assert "error" in _Stub().rename_path("local", str(tmp_path), "a", "../b")
    assert "same" in _Stub().rename_path("local", str(tmp_path), "a", "a")["error"]


def test_delete_local(tmp_path):
    (tmp_path / "f").write_text("x")
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    stub = _Stub()
    paths = [str(tmp_path / n) for n in ("f", "empty", "full")]
    res = stub.delete_paths("local", paths)
    assert res["deleted"] == paths[:2]
    assert res["failed"] == [{"path": paths[2], "error": "folder isn't empty"}]
    assert (tmp_path / "full" / "x").exists()
    assert stub.audit[0]["extra"]["event"] == "files.deleted"
    assert stub.audit[0]["extra"]["paths"] == paths[:2]


@pytest.mark.parametrize("bad", ["/", "/var/..", "/tmp/.", ""])
def test_delete_refuses_odd_paths(bad):
    res = _Stub().delete_paths("web-01", [bad])
    assert "error" in res


@posix
def test_rename_remote(tmp_path):
    (tmp_path / "a b").write_text("x")
    res = _Stub().rename_path("web-01", str(tmp_path), "a b", "c; d")
    assert res == {"path": f"{tmp_path}/c; d"}
    assert (tmp_path / "c; d").exists()


@posix
def test_rename_remote_refuses(tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("2")
    assert _Stub().rename_path("web-01", str(tmp_path), "a", "b") == {
        "error": "b already exists"
    }
    assert _Stub().rename_path("web-01", str(tmp_path), "gone", "c") == {
        "error": "gone no longer exists"
    }


@posix
def test_delete_remote(tmp_path):
    (tmp_path / "f 1").write_text("x")
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "full")
    paths = [str(tmp_path / n) for n in ("f 1", "empty", "full", "link", "missing")]
    res = _Stub().delete_paths("web-01", paths)
    assert res["deleted"] == [paths[0], paths[1], paths[3]]
    assert {f["path"]: f["error"] for f in res["failed"]} == {
        paths[2]: "folder isn't empty",
        paths[4]: "no longer exists",
    }
    # The symlink went, not the folder it points at.
    assert (tmp_path / "full" / "x").exists()


# ── browse as a venv owner ────────────────────────────────────────────────────


def test_files_accounts():
    stub = _Stub()
    assert stub.files_accounts("web-01") == {"login": "ops", "accounts": ["svc"]}
    assert stub.files_accounts("local") == {"login": "", "accounts": []}


def test_as_user_must_be_a_venv_owner(tmp_path):
    stub = _Stub()
    assert stub.make_dir("web-01", "/tmp", "x", as_user="root") == {
        "error": "root isn't a venv owner on web-01"
    }
    assert "error" in stub.delete_paths("web-01", ["/tmp/x"], as_user="other")
    assert stub.commands == []


def test_as_user_wraps_in_sudo():
    stub = _Stub()
    stub.view_file("web-01", "/var/log/app.log", 10, as_user="svc")
    assert stub.commands[0].startswith("sudo -n -u svc sh -c ")


def test_account_error_explains_sudo_refusal():
    msg = fs_browse.account_error("sudo: a password is required\n", "svc")
    assert "without a password" in msg and "svc" in msg
    assert fs_browse.account_error("boom\n", "svc") == "boom"
