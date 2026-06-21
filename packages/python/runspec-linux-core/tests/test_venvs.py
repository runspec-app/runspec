"""venv creation, install-into-venv, and pip.conf writing.

The subprocess layer (``_run``) is stubbed so no real venv/pip is touched; the
tests assert on the argv that *would* run (escalation prefix, flags, ordering)
and on the input validation that happens before any subprocess.
"""

import configparser
import os
import subprocess

import pytest

from runspec_linux_core import venvs
from runspec_linux_core.errors import CommandError


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _record(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub ``venvs._run`` to record every argv and always succeed."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return _completed("Successfully installed foo\n")

    monkeypatch.setattr(venvs, "_run", fake_run)
    return calls


# ── _become ───────────────────────────────────────────────────────────────────


def test_become_no_user_is_noop() -> None:
    assert venvs._become(["echo", "hi"], None) == ["echo", "hi"]
    assert venvs._become(["echo", "hi"], "") == ["echo", "hi"]


def test_become_sudo_prefix() -> None:
    assert venvs._become(["echo", "hi"], "svc") == ["sudo", "-n", "-u", "svc", "echo", "hi"]


def test_become_su_quotes() -> None:
    assert venvs._become(["pip", "install", "a b"], "svc", "su") == ["su", "svc", "-c", "pip install 'a b'"]


def test_become_rejects_bad_user() -> None:
    with pytest.raises(ValueError):
        venvs._become(["echo"], "bad;user")


def test_become_rejects_bad_method() -> None:
    with pytest.raises(ValueError):
        venvs._become(["echo"], "svc", "doas")


# ── create_venv ───────────────────────────────────────────────────────────────


def test_create_venv_uses_symlinks_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    import sys

    result = venvs.create_venv(sys.executable, "/opt/v1")

    venv_cmd = calls[0]
    assert venv_cmd[:4] == [sys.executable, "-m", "venv", "--symlinks"]
    assert venv_cmd[-1] == "/opt/v1"
    # mkdir -p logs, then chmod 2775 logs (no group → no chgrp step).
    assert calls[1] == ["mkdir", "-p", "/opt/v1/logs"]
    assert calls[2] == ["chmod", "2775", "/opt/v1/logs"]
    assert result == {
        "python": sys.executable,
        "dest": "/opt/v1",
        "run_as": "",
        "group": "",
        "logs_dir": "/opt/v1/logs",
        "created": True,
    }


def test_create_venv_only_sets_chosen_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    import sys

    venvs.create_venv(sys.executable, "/opt/v1", system_site_packages=True, clear=True)
    venv_cmd = calls[0]
    assert "--system-site-packages" in venv_cmd
    assert "--clear" in venv_cmd
    assert "--upgrade-deps" not in venv_cmd


def test_create_venv_run_as_escalates_every_step(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    import sys

    venvs.create_venv(sys.executable, "/opt/v1", run_as="svc", group="consoles")
    for cmd in calls:
        assert cmd[:4] == ["sudo", "-n", "-u", "svc"]
    # chgrp -R consoles /opt/v1 runs between mkdir and chmod.
    assert ["sudo", "-n", "-u", "svc", "chgrp", "-R", "consoles", "/opt/v1"] in calls


def test_create_venv_rejects_non_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch)
    with pytest.raises(ValueError):
        venvs.create_venv("/no/such/python", "/opt/v1")


def test_create_venv_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(venvs, "_run", lambda argv: _completed("", returncode=1, stderr="boom"))
    with pytest.raises(CommandError):
        venvs.create_venv(sys.executable, "/opt/v1")


# ── install_into_venv ─────────────────────────────────────────────────────────


def _fake_venv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A venv dir whose bin/python passes the executable check."""
    import sys

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    os.symlink(sys.executable, py)
    return str(venv)


def test_install_into_venv_builds_pip_argv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = _fake_venv(tmp_path, monkeypatch)
    calls = _record(monkeypatch)

    result = venvs.install_into_venv(venv, "httpx rich", upgrade=True)
    cmd = calls[0]
    assert cmd[:4] == [f"{venv}/bin/python", "-m", "pip", "install"]
    assert "-U" in cmd
    assert cmd[-2:] == ["httpx", "rich"]
    assert result["installed"] is True
    assert result["output"] == ["Successfully installed foo"]


def test_install_into_venv_editable_project(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = _fake_venv(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    calls = _record(monkeypatch)

    venvs.install_into_venv(venv, project=str(proj), editable=True, run_as="svc")
    cmd = calls[0]
    assert cmd[:4] == ["sudo", "-n", "-u", "svc"]
    assert cmd[-2:] == ["-e", str(proj)]


def test_install_into_venv_requires_a_target(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = _fake_venv(tmp_path, monkeypatch)
    _record(monkeypatch)
    with pytest.raises(ValueError):
        venvs.install_into_venv(venv)


def test_install_into_venv_editable_needs_project(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = _fake_venv(tmp_path, monkeypatch)
    _record(monkeypatch)
    with pytest.raises(ValueError):
        venvs.install_into_venv(venv, "httpx", editable=True)


def test_install_into_venv_rejects_missing_venv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch)
    with pytest.raises(ValueError):
        venvs.install_into_venv(str(tmp_path / "nope"), "httpx")


def test_install_into_venv_rejects_injection(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = _fake_venv(tmp_path, monkeypatch)
    _record(monkeypatch)
    with pytest.raises(ValueError):
        venvs.install_into_venv(venv, "httpx; rm -rf /")


# ── configure_pip ─────────────────────────────────────────────────────────────


def test_configure_pip_user_scope_writes_ini(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    result = venvs.configure_pip(
        "https://nexus/repository/pypi/simple",
        trusted_host="nexus",
        timeout=60,
        retries=5,
        require_virtualenv=True,
    )
    path = result["path"]
    assert path == str(tmp_path / ".config" / "pip" / "pip.conf")

    cfg = configparser.ConfigParser()
    cfg.read(path)
    assert cfg["global"]["index-url"] == "https://nexus/repository/pypi/simple"
    assert cfg["global"]["trusted-host"] == "nexus"
    assert cfg["global"]["timeout"] == "60"
    assert cfg["global"]["retries"] == "5"
    assert cfg["global"]["require-virtualenv"] == "true"
    assert cfg["global"]["disable-pip-version-check"] == "true"


def test_configure_pip_venv_scope(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = tmp_path / "venv"
    venv.mkdir()
    result = venvs.configure_pip("https://nexus/simple", scope="venv", venv=str(venv))
    assert result["path"] == str(venv / "pip.conf")
    cfg = configparser.ConfigParser()
    cfg.read(result["path"])
    assert cfg["global"]["index-url"] == "https://nexus/simple"


def test_configure_pip_venv_scope_requires_venv() -> None:
    with pytest.raises(ValueError):
        venvs.configure_pip("https://nexus/simple", scope="venv")


def test_configure_pip_bad_scope() -> None:
    with pytest.raises(ValueError):
        venvs.configure_pip("https://nexus/simple", scope="global")


def test_configure_pip_run_as_installs_via_become(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    # run_as path resolves the user's home via pwd; stub it to tmp_path.
    monkeypatch.setattr(
        venvs.pwd,
        "getpwnam",
        lambda u: type("PW", (), {"pw_dir": str(tmp_path)})(),
    )
    result = venvs.configure_pip("https://nexus/simple", run_as="svc")
    target = str(tmp_path / ".config" / "pip" / "pip.conf")
    assert result["path"] == target
    # An install -D -m 0644 <tmp> <target> ran as svc.
    install_cmds = [c for c in calls if "install" in c and "-D" in c]
    assert install_cmds, calls
    cmd = install_cmds[0]
    assert cmd[:4] == ["sudo", "-n", "-u", "svc"]
    assert cmd[4:7] == ["install", "-D", "-m"]
    assert cmd[-1] == target
