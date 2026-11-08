"""rootfiles: the allowlisted root config-file writer behind ``config-file``.

The suite isn't root, so the "root-owned" checks are pointed at the test's own
uid (``owner_uid``)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from runspec_linux_core import rootfiles as rf
from runspec_linux_core.rootfiles import RootFileError

UID = os.getuid()


@pytest.fixture
def env(tmp_path: Path):
    venv = tmp_path / "venv"
    venv.mkdir(mode=0o755)
    os.chmod(venv, 0o755)
    etc = tmp_path / "etc" / "alloy"
    etc.mkdir(parents=True)
    allowlist = venv / rf.ALLOWLIST_NAME
    rf.allow(allowlist, f"{etc}/*", owner_uid=UID)
    return {
        "venv": venv,
        "etc": etc,
        "allowlist": allowlist,
        "backups": venv / rf.BACKUP_DIR_NAME,
        "tmp": tmp_path,
    }


def _write(env, path, content, **kw):
    return rf.write_file(str(path), content, env["allowlist"], backup_dir=env["backups"], owner_uid=UID, **kw)


# ── patterns + allowlist ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "pattern", "hit"),
    [
        ("/etc/alloy/config.alloy", "/etc/alloy/*", True),
        ("/etc/alloy/sub/config.alloy", "/etc/alloy/*", False),
        ("/etc/alloy/sub/config.alloy", "/etc/alloy/**", True),
        ("/etc/alloy.d/x", "/etc/alloy/*", False),
        ("/etc/alloy/config.alloy", "/etc/alloy/config.?lloy", True),
    ],
)
def test_matches(path, pattern, hit):
    assert rf.matches(path, pattern) is hit


@pytest.mark.parametrize("bad", ["etc/alloy/*", "/*", "/**", "/etc/../x", "/etc//x", "/*/alloy", "/etc", "/etc/a b"])
def test_bad_patterns_are_refused(bad):
    with pytest.raises(RootFileError):
        rf.check_pattern(bad)


def test_allow_and_disallow_round_trip(env):
    al = env["allowlist"]
    text = al.read_text()
    assert text.startswith("# Paths config-file") and f"{env['etc']}/*\n" in text
    assert stat.S_IMODE(al.stat().st_mode) == 0o644
    assert rf.allow(al, f"{env['etc']}/*", owner_uid=UID)["changed"] is False
    assert rf.allow(al, "/etc/systemd/system/alloy.service.d/*", owner_uid=UID)["changed"] is True
    assert rf.disallow(al, "/etc/systemd/system/alloy.service.d/*", owner_uid=UID)["paths"] == [f"{env['etc']}/*"]
    assert rf.disallow(al, "/nope/*", owner_uid=UID)["changed"] is False
    info = rf.allowed(al, owner_uid=UID)
    assert info["paths"] == [f"{env['etc']}/*"] and "/etc/shadow*" in info["never"]


def test_missing_allowlist_refuses_everything(tmp_path):
    al = tmp_path / rf.ALLOWLIST_NAME
    assert rf.read_allowlist(al, owner_uid=UID) == []
    with pytest.raises(RootFileError, match="Nothing is allowed yet"):
        rf.write_file(str(tmp_path / "x.conf"), "x", al, backup_dir=tmp_path / "b", owner_uid=UID)


def test_allowlist_writable_by_others_is_refused(env):
    os.chmod(env["allowlist"], 0o666)
    with pytest.raises(RootFileError, match="not writable by group or others"):
        _write(env, env["etc"] / "config.alloy", "x")


def test_allowlist_owned_by_someone_else_is_refused(env):
    with pytest.raises(RootFileError, match="must be owned by root"):
        rf.read_allowlist(env["allowlist"], owner_uid=UID + 1)


# ── target resolution ────────────────────────────────────────────────────────


def test_outside_the_allowlist_is_refused(env):
    with pytest.raises(RootFileError, match="isn't in the allowlist"):
        _write(env, env["tmp"] / "etc" / "other.conf", "x")


def test_dotdot_and_symlinked_folders_resolve_first(env):
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (env["etc"] / "link").symlink_to(outside)
    with pytest.raises(RootFileError, match="isn't in the allowlist"):
        _write(env, env["etc"] / "link" / "x.conf", "x")
    with pytest.raises(RootFileError, match="isn't in the allowlist"):
        _write(env, f"{env['etc']}/../evil.conf", "x")


def test_symlink_file_is_refused(env):
    target = env["tmp"] / "secret"
    target.write_text("s")
    (env["etc"] / "config.alloy").symlink_to(target)
    with pytest.raises(RootFileError, match="is a symlink"):
        _write(env, env["etc"] / "config.alloy", "x")
    assert target.read_text() == "s"


def test_denied_paths_win_over_the_allowlist(tmp_path):
    al = tmp_path / rf.ALLOWLIST_NAME
    rf.allow(al, "/etc/**", owner_uid=UID)
    for path in ("/etc/shadow", "/etc/sudoers.d/90-x", "/etc/ssh/sshd_config"):
        with pytest.raises(RootFileError, match="never written"):
            rf.resolve_target(path, al, owner_uid=UID)


def test_relative_path_is_refused(env):
    with pytest.raises(RootFileError, match="absolute"):
        _write(env, "config.alloy", "x")


# ── write / edit / read ──────────────────────────────────────────────────────


def test_write_creates_then_updates_with_backup(env):
    f = env["etc"] / "config.alloy"
    first = _write(env, f, "a = 1\n")
    assert first["created"] and first["changed"] and "backup" not in first
    assert stat.S_IMODE(f.stat().st_mode) == 0o644
    second = _write(env, f, "a = 2\n")
    assert second["changed"] and not second["created"]
    assert "-a = 1" in second["diff"] and "+a = 2" in second["diff"]
    backup = Path(second["backup"])
    assert backup.parent == env["backups"] and backup.read_text() == "a = 1\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(env["backups"].stat().st_mode) == 0o700
    # nothing left in the target folder but the file
    assert sorted(p.name for p in env["etc"].iterdir()) == ["config.alloy"]


def test_same_content_changes_nothing(env):
    f = env["etc"] / "config.alloy"
    _write(env, f, "a\n")
    again = _write(env, f, "a\n")
    assert again["changed"] is False and "backup" not in again
    assert not env["backups"].exists()


def test_private_new_file_is_owner_only_and_mode_overrides(env):
    f = env["etc"] / "token"
    _write(env, f, "glc_xxx\n", private=True)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    g = env["etc"] / "token2"
    res = _write(env, g, "glc_xxx\n", private=True, mode="0644")
    assert stat.S_IMODE(g.stat().st_mode) == 0o644 and "warning" in res


def test_existing_mode_is_kept(env):
    f = env["etc"] / "config.alloy"
    f.write_text("a\n")
    os.chmod(f, 0o640)
    _write(env, f, "b\n")
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


def test_mode_only_change_counts(env):
    f = env["etc"] / "config.alloy"
    _write(env, f, "a\n")
    res = _write(env, f, "a\n", mode="0640")
    assert res["changed"] and stat.S_IMODE(f.stat().st_mode) == 0o640


@pytest.mark.parametrize("bad", ["644x", "4755", "0o644"])
def test_bad_modes(env, bad):
    with pytest.raises(RootFileError):
        _write(env, env["etc"] / "x", "x", mode=bad)


def test_unknown_owner(env):
    with pytest.raises(RootFileError, match="no such user"):
        _write(env, env["etc"] / "x", "x", owner="no-such-user-xyz")


def test_dry_run_writes_nothing(env):
    f = env["etc"] / "config.alloy"
    res = _write(env, f, "a\n", dry_run=True)
    assert res["dry_run"] and res["changed"] and not f.exists()


def test_make_dirs(env):
    rf.allow(env["allowlist"], f"{env['etc']}/**", owner_uid=UID)
    f = env["etc"] / "d" / "e" / "x.conf"
    with pytest.raises(RootFileError, match="--make-dirs"):
        _write(env, f, "x")
    _write(env, f, "x", make_dirs=True)
    assert f.read_text() == "x"
    assert stat.S_IMODE((env["etc"] / "d").stat().st_mode) == 0o755


def test_edit_needs_unique_old_text(env):
    f = env["etc"] / "config.alloy"
    f.write_text("url = old\nurl = old\nkey = k\n")
    kw = {"backup_dir": env["backups"], "owner_uid": UID}
    with pytest.raises(RootFileError, match="appears 2 times"):
        rf.edit_file(str(f), [("url = old", "url = new")], env["allowlist"], **kw)
    with pytest.raises(RootFileError, match="isn't in the file"):
        rf.edit_file(str(f), [("nope", "x")], env["allowlist"], **kw)
    res = rf.edit_file(str(f), [("key = k", "key = K")], env["allowlist"], **kw)
    assert res["edits_applied"] == 1 and f.read_text().endswith("key = K\n")
    with pytest.raises(RootFileError, match="doesn't exist"):
        rf.edit_file(str(env["etc"] / "missing"), [("a", "b")], env["allowlist"], **kw)


def test_read(env):
    f = env["etc"] / "config.alloy"
    f.write_text("hello\n")
    os.chmod(f, 0o640)
    res = rf.read_file(str(f), env["allowlist"], owner_uid=UID)
    assert res["content"] == "hello\n" and res["mode"] == "0640"
    with pytest.raises(RootFileError, match="isn't in the allowlist"):
        rf.read_file(str(env["tmp"] / "x"), env["allowlist"], owner_uid=UID)


def test_backups_are_capped(env, monkeypatch):
    f = env["etc"] / "config.alloy"
    _write(env, f, "0\n")
    for i in range(1, rf.BACKUPS_KEPT + 3):
        _write(env, f, f"{i}\n")
    assert len(list(env["backups"].iterdir())) == rf.BACKUPS_KEPT
