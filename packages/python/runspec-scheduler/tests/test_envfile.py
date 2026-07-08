"""Credential env-file tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from runspec_scheduler import envfile


def test_serialize_dotenv():
    out = envfile.serialize_dotenv({"B": "2", "A": "1"})
    # Sorted, quoted.
    assert out == 'A="1"\nB="2"\n'
    assert envfile.serialize_dotenv({}) == ""


def test_serialize_escapes():
    out = envfile.serialize_dotenv({"K": 'a"b\\c'})
    assert out == 'K="a\\"b\\\\c"\n'


def test_capture_env(monkeypatch):
    monkeypatch.setenv("FOO_USER", "alice")
    monkeypatch.setenv("FOO_PASSWORD", "s3cret")
    monkeypatch.delenv("BAR_TOKEN", raising=False)
    got = envfile.capture_env(["FOO_USER", "FOO_PASSWORD", "BAR_TOKEN"])
    assert got == {"FOO_USER": "alice", "FOO_PASSWORD": "s3cret"}


def test_write_env_file_same_user(monkeypatch, tmp_path):
    me = envfile._current_user()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    class FakePw:
        pw_dir = str(fake_home)

    monkeypatch.setattr(envfile.pwd, "getpwnam", lambda u: FakePw())
    path = envfile.write_env_file("sid1", {"JIRA_PASSWORD": "pw"}, run_as=me)
    assert path == str(fake_home / ".config" / "runspec-scheduler" / "env" / "sid1.env")
    assert os.path.exists(path)
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert 'JIRA_PASSWORD="pw"' in Path(path).read_text()


def test_write_env_file_sudo_uses_stdin(monkeypatch):
    """Cross-user write must pass the secret on stdin, never in argv."""
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input")

        class R:
            returncode = 0
            stdout = "/home/svc/.config/runspec-scheduler/env/sid.env\n"
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    path = envfile.write_env_file("sid", {"JIRA_PASSWORD": "topsecret"}, run_as="svc-acct")
    assert path == "/home/svc/.config/runspec-scheduler/env/sid.env"
    # Secret is on stdin, not anywhere in argv.
    assert "topsecret" in captured["input"]
    assert not any("topsecret" in str(a) for a in captured["argv"])
    assert "svc-acct" in captured["argv"]
