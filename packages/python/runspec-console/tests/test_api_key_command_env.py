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

import pytest

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


# ── Windows (cmd.exe) path handling ──────────────────────────────────────────
#
# shell=True runs the command through cmd.exe on Windows, which reads "/" as a
# switch and resolves ".\x" against the working directory. A helper placed next
# to the console must still run however the operator spelled its path.

_SCRIPTS = r"C:\venvs\corp\Scripts"
_CWD = r"C:\Users\op\Desktop"


def _win(command, files=(), *, env=None, cwd=_CWD, dirs=(_SCRIPTS,)):
    present = {f.lower() for f in files}
    return base.prepare_api_key_command(
        command,
        env if env is not None else {"PATHEXT": ".COM;.EXE;.BAT;.CMD"},
        windows=True,
        cwd=cwd,
        search_dirs=list(dirs),
        is_file=lambda p: p.lower() in present,
    )


def test_windows_dot_slash_resolves_to_the_console_folder():
    exe = _SCRIPTS + r"\get-key.exe"
    # "./get-key.exe" would make cmd run "." — it becomes the absolute file.
    assert _win("./get-key.exe --scope llm", [exe]) == exe + " --scope llm"
    assert _win(r".\get-key.exe --scope llm", [exe]) == exe + " --scope llm"


def test_windows_relative_path_prefers_working_directory():
    here = _CWD + r"\get-key.exe"
    there = _SCRIPTS + r"\get-key.exe"
    assert _win("./get-key.exe", [here, there]) == here


def test_windows_relative_path_tries_pathext():
    exe = _SCRIPTS + r"\get-key.cmd"
    assert _win("./get-key token", [exe]).lower() == (exe + " token").lower()


def test_windows_forward_slash_absolute_path_normalised():
    assert _win("C:/tools/get-key.exe -q") == r"C:\tools\get-key.exe -q"


def test_windows_unresolved_relative_path_still_gets_backslashes():
    # Not found anywhere: cmd still must not see "/" as a switch.
    assert _win("tools/get-key.exe") == r"tools\get-key.exe"


def test_windows_path_with_spaces_is_quoted():
    folder = r"C:\Program Files\Corp\Scripts"
    exe = folder + r"\get-key.exe"
    assert _win("./get-key.exe a", [exe], dirs=[folder]) == f'"{exe}" a'
    assert _win('"C:/Program Files/Corp/get-key.exe" a') == (
        r'"C:\Program Files\Corp\get-key.exe" a'
    )


def test_windows_bare_name_and_arguments_untouched():
    assert _win("get-key --url https://vault/x") == "get-key --url https://vault/x"
    assert _win(r"keyring get a/b c\d") == r"keyring get a/b c\d"


def test_windows_bare_name_in_console_folder_becomes_absolute():
    # A helper shipped next to the console exe: run by full path, so it is found
    # however the command is executed (a plugin adapter's own subprocess, a
    # console launched from a shortcut with no venv Scripts on PATH).
    exe = _SCRIPTS + r"\get-key.exe"
    assert _win("get-key --scope llm", [exe]).lower() == (exe + " --scope llm").lower()
    assert _win("get-key.exe", [exe]) == exe
    folder = r"C:\Program Files\Corp\Scripts"
    spaced = folder + r"\get-key.cmd"
    assert _win("get-key a", [spaced], dirs=[folder]).lower() == f'"{spaced}" a'.lower()


def test_windows_bare_name_ignores_working_directory():
    # Only the console's folders, never the cwd: a shortcut's "Start in" folder
    # must not change which helper runs.
    here = _CWD + r"\get-key.exe"
    assert _win("get-key", [here]) == "get-key"


def test_resolved_command_is_stable_when_prepared_again():
    exe = _SCRIPTS + r"\get-key.exe"
    once = _win("get-key x", [exe])
    assert _win(once, [exe]) == once
    folder = r"C:\Program Files\Corp"
    spaced = folder + r"\get-key.exe"
    once = _win("get-key x", [spaced], dirs=[folder])
    assert _win(once, [spaced], dirs=[folder]) == once


def test_windows_expands_runspec_vars():
    env = {"RUNSPEC_PYTHON": r"C:\venvs\corp\Scripts\python.exe", "RUNSPEC_VENV": "x"}
    out = _win('"$RUNSPEC_PYTHON" -m corp.auth', env=env)
    assert out == r'"C:\venvs\corp\Scripts\python.exe" -m corp.auth'
    assert _win("${RUNSPEC_PYTHON} -m a", env=env) == env["RUNSPEC_PYTHON"] + " -m a"


def test_posix_leaves_vars_and_bare_names_to_the_shell():
    cmd = '"$RUNSPEC_PYTHON" -m corp.auth'
    assert base.prepare_api_key_command(cmd, {}, windows=False) == cmd
    assert base.prepare_api_key_command("get-key x", {}, windows=False) == "get-key x"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell path handling")
def test_posix_relative_path_falls_back_to_console_folder(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    helper = folder / "get-key"
    helper.write_text("")
    out = base.prepare_api_key_command(
        "./get-key x", {}, windows=False, cwd=str(tmp_path), search_dirs=[str(folder)]
    )
    assert out == f"{helper} x"


def test_dirs_include_the_launcher_folder(monkeypatch, tmp_path):
    launcher = tmp_path / "Scripts"
    launcher.mkdir()
    monkeypatch.setattr(base.sys, "argv", [str(launcher / "runspec-console")])
    assert str(launcher) in base.api_key_command_dirs()
    assert str(launcher) in base.api_key_command_env()["PATH"].split(os.pathsep)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell path handling")
def test_run_api_key_command_runs_the_prepared_command(monkeypatch, tmp_path):
    helper = tmp_path / "get-key"
    helper.write_text("")
    monkeypatch.chdir(tmp_path)
    seen = {}

    class _Result:
        stdout = "k\n"
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(base.subprocess, "run", fake_run)
    assert base.run_api_key_command("./get-key") == "k"
    assert seen["cmd"] == str(helper)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell path handling")
def test_posix_bare_name_in_console_folder_becomes_absolute(tmp_path):
    folder = tmp_path / "my venv" / "bin"
    folder.mkdir(parents=True)
    helper = folder / "get-key"
    helper.write_text("")
    out = base.prepare_api_key_command(
        "get-key x", {}, windows=False, search_dirs=[str(folder)]
    )
    assert out == f"'{helper}' x"


def test_not_found_error_names_the_searched_folders(monkeypatch):
    class _Result:
        stdout = ""
        stderr = "'get-key' is not recognized as an internal or external command"
        returncode = 9009

    monkeypatch.setattr(base.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(base, "api_key_command_dirs", lambda: [r"C:\v\Scripts"])
    with pytest.raises(RuntimeError) as err:
        base.run_api_key_command("get-key")
    msg = str(err.value)
    assert "exit 9009" in msg
    assert r"C:\v\Scripts" in msg
    assert "Browse" in msg


def test_other_failures_have_no_not_found_hint(monkeypatch):
    class _Result:
        stdout = ""
        stderr = "token expired"
        returncode = 1

    monkeypatch.setattr(base.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError) as err:
        base.run_api_key_command("get-key")
    assert "not found" not in str(err.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell path handling")
def test_check_reports_resolution_without_running(monkeypatch, tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    helper = folder / "get-key"
    helper.write_text("")
    monkeypatch.setattr(base, "api_key_command_dirs", lambda: [str(folder)])

    def boom(*a, **k):  # the helper's output is a secret — never run it
        raise AssertionError("check must not run the command")

    monkeypatch.setattr(base.subprocess, "run", boom)
    out = base.check_api_key_command("get-key --scope llm")
    assert out["command"] == f"{helper} --scope llm"
    assert out["executable"] == str(helper)
    assert out["found"] is True
    assert out["searched"] == [str(folder)]

    missing = base.check_api_key_command(str(tmp_path / "nope" / "get-key"))
    assert missing["found"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell path handling")
def test_check_finds_a_spaced_posix_path(monkeypatch, tmp_path):
    folder = tmp_path / "my venv" / "bin"
    folder.mkdir(parents=True)
    helper = folder / "get-key"
    helper.write_text("")
    monkeypatch.setattr(base, "api_key_command_dirs", lambda: [str(folder)])
    out = base.check_api_key_command("get-key")
    assert out["executable"] == str(helper)
    assert out["found"] is True
