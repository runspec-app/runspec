"""which() falls back to sbin/bin dirs when a tool isn't on PATH."""

import pytest

from runspec_linux_core._paths import which


def test_which_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core._paths.shutil.which", lambda n: "/usr/bin/" + n)
    assert which("sudo") == "/usr/bin/sudo"


def test_which_falls_back_to_sbin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not on PATH (the non-login SSH case), but present in /usr/sbin.
    monkeypatch.setattr("runspec_linux_core._paths.shutil.which", lambda n: None)
    monkeypatch.setattr("runspec_linux_core._paths.os.path.isfile", lambda p: p == "/usr/sbin/visudo")
    monkeypatch.setattr("runspec_linux_core._paths.os.access", lambda p, mode: True)
    assert which("visudo") == "/usr/sbin/visudo"


def test_which_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core._paths.shutil.which", lambda n: None)
    monkeypatch.setattr("runspec_linux_core._paths.os.path.isfile", lambda p: False)
    assert which("nope") is None
