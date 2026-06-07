import pytest

from runspec_console.fs_browse import (
    _local_parent,
    _posix_parent,
    list_local,
    parse_remote_listing,
    remote_list_command,
)


def test_remote_list_command_builds_cd_pwd_find():
    cmd = remote_list_command("/var/log/app")
    # No shell metacharacters → shlex.quote leaves the path bare.
    assert "cd /var/log/app" in cmd
    assert "pwd" in cmd
    assert "find -L ." in cmd


def test_remote_list_command_defaults_to_home():
    cmd = remote_list_command("")
    assert 'cd "$HOME"' in cmd


def test_remote_list_command_quotes_spaces_and_metachars():
    cmd = remote_list_command("/data/end of day; rm -rf x")
    # The whole path is a single shell-quoted token — no unescaped ; survives.
    assert "'/data/end of day; rm -rf x'" in cmd


def _sample_stdout():
    return "\n".join(
        [
            "/var/log/app",
            "d\t4096\t1717000000.0\tarchive",
            "f\t2048\t1717000500.5\tfix.log",
            "f\t100\t1717000400\talpha.log",
            "d\t4096\t1717000100\tBeta",  # capital — case-insensitive sort
        ]
    )


def test_parse_remote_listing_cwd_and_parent():
    result = parse_remote_listing(_sample_stdout())
    assert result["cwd"] == "/var/log/app"
    assert result["parent"] == "/var/log"
    assert result["truncated"] is False


def test_parse_remote_listing_sorts_dirs_first_then_caseless():
    result = parse_remote_listing(_sample_stdout())
    names = [e["name"] for e in result["entries"]]
    # Directories first (archive, Beta), then files (alpha.log, fix.log).
    assert names == ["archive", "Beta", "alpha.log", "fix.log"]


def test_parse_remote_listing_types_sizes_mtime():
    entries = {e["name"]: e for e in parse_remote_listing(_sample_stdout())["entries"]}
    assert entries["archive"]["type"] == "directory"
    assert entries["fix.log"]["type"] == "file"
    assert entries["fix.log"]["size"] == 2048
    assert entries["fix.log"]["mtime"] == 1717000500  # float epoch truncated to int


def test_parse_remote_listing_dirs_only():
    entries = parse_remote_listing(_sample_stdout(), dirs_only=True)["entries"]
    assert {e["name"] for e in entries} == {"archive", "Beta"}


def test_parse_remote_listing_skips_malformed_lines():
    out = "/tmp\nd\t4096\t1\tgood\ngarbage-without-tabs\nf\t1\t2"  # last has 3 fields
    entries = parse_remote_listing(out)["entries"]
    assert [e["name"] for e in entries] == ["good"]


def test_parse_remote_listing_empty():
    result = parse_remote_listing("")
    assert result == {"cwd": "", "parent": None, "entries": [], "truncated": False}


def test_posix_parent():
    assert _posix_parent("/var/log/app") == "/var/log"
    assert _posix_parent("/var") == "/"
    assert _posix_parent("/") is None
    assert _posix_parent("") is None


def test_list_local(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "zeta.txt").write_text("hello")
    (tmp_path / "alpha.txt").write_text("hi")

    result = list_local(str(tmp_path))
    assert result["cwd"] == str(tmp_path)
    assert result["parent"] == str(tmp_path.parent)
    names = [e["name"] for e in result["entries"]]
    assert names == ["sub", "alpha.txt", "zeta.txt"]  # dir first, then caseless
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["sub"]["type"] == "directory"
    assert by_name["zeta.txt"]["type"] == "file"
    assert by_name["zeta.txt"]["size"] == 5


def test_list_local_dirs_only(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")
    result = list_local(str(tmp_path), dirs_only=True)
    assert [e["name"] for e in result["entries"]] == ["sub"]


def test_list_local_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    try:
        list_local(str(f))
        raise AssertionError("expected NotADirectoryError")
    except NotADirectoryError:
        pass


def test_local_parent_root():
    assert _local_parent("/") is None


class _FakeAttr:
    def __init__(self, name, mode, size, mtime):
        self.filename = name
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class _FakeSftp:
    def __init__(self, entries, cwd="/var/log/app"):
        self._entries = entries
        self._cwd = cwd

    def normalize(self, p):
        return self._cwd if p in (".", "") else p.rstrip("/") or "/"

    def listdir_attr(self, path):
        return self._entries


def test_list_sftp():
    import stat as st

    from runspec_console.fs_browse import list_sftp

    entries = [
        _FakeAttr("sub", st.S_IFDIR | 0o755, 4096, 1717000000),
        _FakeAttr("zeta.log", st.S_IFREG | 0o644, 2048, 1717000500),
        _FakeAttr("alpha.log", st.S_IFREG | 0o644, 100, 1717000400),
    ]
    res = list_sftp(_FakeSftp(entries), "")
    assert res["cwd"] == "/var/log/app"
    assert res["parent"] == "/var/log"
    assert [e["name"] for e in res["entries"]] == ["sub", "alpha.log", "zeta.log"]
    by = {e["name"]: e for e in res["entries"]}
    assert by["sub"]["type"] == "directory"
    assert by["zeta.log"]["type"] == "file"
    assert by["zeta.log"]["size"] == 2048


def test_list_sftp_dirs_only():
    import stat as st

    from runspec_console.fs_browse import list_sftp

    entries = [
        _FakeAttr("sub", st.S_IFDIR | 0o755, 4096, 1),
        _FakeAttr("file.log", st.S_IFREG | 0o644, 1, 1),
    ]
    res = list_sftp(_FakeSftp(entries), "", dirs_only=True)
    assert [e["name"] for e in res["entries"]] == ["sub"]


def test_remote_list_command_service_account_wraps_in_become():
    # Needs core runspec for build_become_argv; CI's console-test env omits it.
    pytest.importorskip("runspec")
    cmd = remote_list_command(
        "/var/log/app", run_as="svc_app", become_method="sudo"
    )
    assert cmd.startswith("sudo ")
    assert "-u svc_app" in cmd
    assert "sh -c" in cmd
    # The listing itself is preserved inside the quoted `sh -c` argument.
    assert "find -L ." in cmd
    assert "pwd" in cmd


def test_remote_list_command_no_run_as_is_plain():
    # Login-user path must NOT import runspec / wrap in become.
    cmd = remote_list_command("/var/log/app")
    assert cmd.startswith("cd /var/log/app")
    assert "sudo" not in cmd
