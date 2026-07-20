"""
test_api_key_command_env.py — venv-relative execution of api_key_command.

A config-seed commonly ships a key helper into the same venv as the console, but
the venv can be created anywhere, so a hardcoded path into its Scripts/bin dir
breaks. ``api_key_command`` therefore runs with the current venv made
discoverable: its script dir on PATH, plus RUNSPEC_PYTHON / RUNSPEC_VENV. These
tests pin that contract (no adapter/SDK needed — the helpers live in base.py).
"""

from __future__ import annotations

import os
import sys
import sysconfig

from runspec_console.adapters import base


def test_env_prepends_venv_scripts_dir_to_path():
    env = base.api_key_command_env()
    scripts = sysconfig.get_path("scripts") or os.path.dirname(sys.executable)
    # The venv's script dir leads PATH, so a console-script installed alongside
    # the console (e.g. `get-key`) resolves by bare name wherever the venv lives.
    assert env["PATH"].split(os.pathsep)[0] == scripts
    # The prior PATH is preserved after it (nothing dropped).
    assert os.environ.get("PATH", "") in env["PATH"]


def test_env_exports_python_and_venv():
    env = base.api_key_command_env()
    assert env["RUNSPEC_PYTHON"] == sys.executable
    assert env["RUNSPEC_VENV"] == sys.prefix


def test_env_starts_from_process_environment(monkeypatch):
    monkeypatch.setenv("MYCORP_TOKEN_URL", "https://vault.corp/token")
    env = base.api_key_command_env()
    # Inherited so a helper can read the operator's own environment.
    assert env["MYCORP_TOKEN_URL"] == "https://vault.corp/token"


def test_run_api_key_command_passes_the_venv_env(monkeypatch):
    seen: dict[str, object] = {}

    class _Result:
        stdout = "the-key\n"
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        seen["shell"] = kwargs.get("shell")
        return _Result()

    monkeypatch.setattr(base.subprocess, "run", fake_run)
    key = base.run_api_key_command("get-key")

    assert key == "the-key"
    assert seen["cmd"] == "get-key"
    assert seen["shell"] is True
    # The subprocess got the venv-augmented env, not a bare/None one.
    assert seen["env"]["RUNSPEC_PYTHON"] == sys.executable
    assert seen["env"]["PATH"].split(os.pathsep)[0] == (
        sysconfig.get_path("scripts") or os.path.dirname(sys.executable)
    )


def test_run_api_key_command_raises_on_empty_output(monkeypatch):
    class _Result:
        stdout = "  \n"
        stderr = "backend down"
        returncode = 7

    monkeypatch.setattr(base.subprocess, "run", lambda *a, **k: _Result())
    import pytest

    with pytest.raises(RuntimeError, match="no output.*exit 7.*backend down"):
        base.run_api_key_command("broken-helper")
