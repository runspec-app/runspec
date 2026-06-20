"""enable_passwordless_sudo: content generation, validation, install + rollback.

No real sudo/visudo is invoked — the subprocess layer is stubbed so the control
flow (pre-validate, idempotency, install, post-validate, rollback) is exercised
deterministically. The password is asserted to travel via stdin, never argv.
"""

import subprocess

import pytest

from runspec_linux_core import sudoers
from runspec_linux_core.errors import CommandError, ToolNotFoundError


def _ok(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.sudoers.shutil.which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr("runspec_linux_core.sudoers._runspec_bin_dir", lambda: "/opt/rs/bin")


# ── content ───────────────────────────────────────────────────────────────────


def test_scoped_content_targets_venv_bin() -> None:
    out = sudoers._sudoers_content("alice", "scoped", "/opt/rs/bin")
    assert "alice ALL=(root) NOPASSWD: /opt/rs/bin/\n" in out
    assert out.startswith("# Managed by runspec-linux")


def test_all_content_is_nopasswd_all() -> None:
    out = sudoers._sudoers_content("alice", "all", "/opt/rs/bin")
    assert "alice ALL=(ALL) NOPASSWD: ALL\n" in out


# ── input validation ──────────────────────────────────────────────────────────


def test_rejects_bad_scope() -> None:
    with pytest.raises(ValueError):
        sudoers.enable_passwordless_sudo("pw", "alice", "everything")


@pytest.mark.parametrize("bad", ["root\nbad ALL=(ALL) NOPASSWD: ALL", "al ice", "-x", "a;b"])
def test_rejects_injection_in_user(bad: str) -> None:
    with pytest.raises(ValueError):
        sudoers.enable_passwordless_sudo("pw", bad, "scoped")


# ── install flow ──────────────────────────────────────────────────────────────


def test_install_happy_path_pipes_password_via_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "input": kw.get("input")})
        if cmd[:2] == ["visudo", "-cf"]:  # local pre-validate
            return _ok()
        if cmd[:4] == ["sudo", "-S", "-p", ""]:
            sub = cmd[4:]  # args after the `sudo -S -p ''` prefix
            if sub[0] == "cat":  # idempotency probe: not yet installed
                return _ok(returncode=1, stderr="No such file")
            if sub[0] == "install":
                return _ok()
            if sub[0] == "visudo":  # post-validate
                return _ok()
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("runspec_linux_core.sudoers.subprocess.run", fake_run)

    result = sudoers.enable_passwordless_sudo("s3cret", "alice", "all")
    assert result == {
        "user": "alice",
        "scope": "all",
        "path": "/etc/sudoers.d/runspec",
        "installed": True,
        "already_configured": False,
    }
    # Password rides stdin on every sudo call — never appears in any argv.
    sudo_calls = [c for c in calls if c["cmd"][:1] == ["sudo"]]
    assert sudo_calls and all(c["input"] == "s3cret\n" for c in sudo_calls)
    assert not any("s3cret" in " ".join(c["cmd"]) for c in calls)


def test_idempotent_when_identical_drop_in_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    content = sudoers._sudoers_content("alice", "all", "/opt/rs/bin")

    def fake_run(cmd, **kw):
        if cmd[:2] == ["visudo", "-cf"]:
            return _ok()
        if cmd[4:5] == ["cat"]:
            return _ok(stdout=content)  # already present, identical
        raise AssertionError(f"should not install: {cmd}")

    monkeypatch.setattr("runspec_linux_core.sudoers.subprocess.run", fake_run)
    result = sudoers.enable_passwordless_sudo("pw", "alice", "all")
    assert result["already_configured"] is True


def test_wrong_password_raises_command_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        if cmd[:2] == ["visudo", "-cf"]:
            return _ok()
        if cmd[4:5] == ["cat"]:
            return _ok(returncode=1)
        if cmd[4:5] == ["install"]:
            return _ok(returncode=1, stderr="sudo: 1 incorrect password attempt")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr("runspec_linux_core.sudoers.subprocess.run", fake_run)
    with pytest.raises(CommandError, match="authentication failed"):
        sudoers.enable_passwordless_sudo("wrong", "alice", "all")


def test_post_validate_failure_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[list[str]] = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["visudo", "-cf"]:
            return _ok()
        sub = cmd[4:] if cmd[:4] == ["sudo", "-S", "-p", ""] else []
        if sub[:1] == ["cat"]:
            return _ok(returncode=1)
        if sub[:1] == ["install"]:
            return _ok()
        if sub[:1] == ["visudo"]:  # whole-tree check fails
            return _ok(returncode=1, stderr=">>> syntax error")
        if sub[:1] == ["rm"]:
            removed.append(sub)
            return _ok()
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr("runspec_linux_core.sudoers.subprocess.run", fake_run)
    with pytest.raises(CommandError, match="rolled back"):
        sudoers.enable_passwordless_sudo("pw", "alice", "scoped")
    assert removed and removed[0] == ["rm", "-f", "/etc/sudoers.d/runspec"]


def test_requires_visudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.sudoers.shutil.which", lambda name: None if name == "visudo" else "/usr/bin/sudo")
    with pytest.raises(ToolNotFoundError, match="visudo"):
        sudoers.enable_passwordless_sudo("pw", "alice", "scoped")
