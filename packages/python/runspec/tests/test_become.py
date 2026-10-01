"""
test_become.py — privilege escalation command construction (run_as / sudo).

Covers the SPEC "Remote Execution" command table and run_as resolution.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from runspec.become import (
    ENV_PRELUDE,
    PY_ENV_PRELUDE,
    build_become_argv,
    build_exec_argv,
    env_frame,
    parse_env_frame,
    resolve_run_as,
    validate_run_as_patterns,
    venv_python_for,
)

# ── resolve_run_as ────────────────────────────────────────────────────────────


def test_resolve_none_and_empty():
    assert resolve_run_as(None, "host1") == ""
    assert resolve_run_as("", "host1") == ""


def test_resolve_simple_string():
    assert resolve_run_as("oracle", "anything") == "oracle"


def test_resolve_env_var(monkeypatch):
    monkeypatch.setenv("ORACLE_RUN_AS", "orasvc")
    assert resolve_run_as("$ORACLE_RUN_AS", "h") == "orasvc"
    # Unset env var resolves to empty (no escalation) rather than crashing.
    assert resolve_run_as("$NOPE_NOT_SET", "h") == ""


def test_resolve_hosts_exact_match_wins():
    spec = {"default": "oracle", "hosts": {"special-box-01": "dba", "no-priv": ""}}
    assert resolve_run_as(spec, "special-box-01") == "dba"
    assert resolve_run_as(spec, "no-priv") == ""  # explicit no-escalation
    assert resolve_run_as(spec, "other") == "oracle"  # falls back to default


def test_resolve_patterns_first_match_in_order():
    spec = {
        "default": "oracle",
        "patterns": {"[lg]pexp[0-9]*": "orasvc", "prod[0-9]*": "produser"},
    }
    assert resolve_run_as(spec, "lpexp42") == "orasvc"
    assert resolve_run_as(spec, "prod7") == "produser"
    assert resolve_run_as(spec, "dev1") == "oracle"


def test_resolve_hosts_beats_patterns():
    spec = {
        "default": "oracle",
        "hosts": {"prod1": "vip"},
        "patterns": {"prod[0-9]*": "produser"},
    }
    assert resolve_run_as(spec, "prod1") == "vip"  # exact host beats pattern
    assert resolve_run_as(spec, "prod2") == "produser"


def test_validate_patterns_flags_bad_regex():
    errors = validate_run_as_patterns({"patterns": {"good[0-9]*": "u", "bad(": "u"}})
    assert len(errors) == 1
    assert "bad(" in errors[0]
    assert validate_run_as_patterns("not-a-table") == []


# ── build_become_argv ─────────────────────────────────────────────────────────

CMD = ["/venv/bin/restart-service", "--name", "nginx"]


def test_no_run_as_no_env_is_passthrough():
    assert build_become_argv(CMD) == CMD


def test_no_run_as_with_env_uses_shell_assignment():
    with pytest.warns(DeprecationWarning, match="command line"):
        out = build_become_argv(CMD, env={"RUNSPEC_AGENT": "1"})
    assert out == ["RUNSPEC_AGENT=1", *CMD]


def test_sudo_default():
    out = build_become_argv(CMD, run_as="deploy")
    assert out == ["sudo", "-u", "deploy", *CMD]


def test_sudo_with_env_uses_env_binary():
    with pytest.warns(DeprecationWarning):
        out = build_become_argv(CMD, run_as="deploy", env={"RUNSPEC_AGENT": "1"})
    assert out == ["sudo", "-u", "deploy", "env", "RUNSPEC_AGENT=1", *CMD]


def test_become_flags_precede_user():
    out = build_become_argv(CMD, run_as="deploy", become_flags="-H")
    assert out == ["sudo", "-H", "-u", "deploy", *CMD]
    # multiple flags split on whitespace
    out = build_become_argv(CMD, run_as="deploy", become_flags="-H -i")
    assert out == ["sudo", "-H", "-i", "-u", "deploy", *CMD]


def test_pbrun_and_dzdo_share_dash_u_syntax():
    assert build_become_argv(CMD, run_as="svc", become_method="pbrun") == [
        "pbrun",
        "-u",
        "svc",
        *CMD,
    ]
    assert build_become_argv(CMD, run_as="svc", become_method="dzdo") == [
        "dzdo",
        "-u",
        "svc",
        *CMD,
    ]


def test_unknown_method_falls_back_to_sudo():
    out = build_become_argv(CMD, run_as="svc", become_method="bogus")
    assert out[0] == "sudo"


def test_su_wraps_command_in_dash_c_string():
    out = build_become_argv(CMD, run_as="deploy", become_method="su")
    assert out[0] == "su"
    assert out[1] == "-c"
    assert out[-1] == "deploy"
    # the command is a single shell-quoted string argument
    assert out[2] == "/venv/bin/restart-service --name nginx"


def test_su_with_env_and_flags():
    with pytest.warns(DeprecationWarning):
        out = build_become_argv(
            CMD,
            run_as="deploy",
            become_method="su",
            become_flags="-l",
            env={"RUNSPEC_AGENT": "1"},
        )
    assert out[0] == "su"
    assert "-l" in out
    assert out[-1] == "deploy"
    assert out[out.index("-c") + 1] == "env RUNSPEC_AGENT=1 /venv/bin/restart-service --name nginx"


def test_su_quotes_args_with_spaces():
    out = build_become_argv(["/venv/bin/note", "--msg", "hello world"], run_as="deploy", become_method="su")
    # the inner -c string must keep "hello world" as one quoted token
    assert "'hello world'" in out[out.index("-c") + 1]


@pytest.mark.parametrize("method", ["sudo", "pbrun", "dzdo"])
def test_empty_run_as_never_escalates(method):
    # An explicit empty run_as (e.g. a host mapped to "") must not escalate.
    assert build_become_argv(CMD, run_as="", become_method=method) == CMD


# ── build_exec_argv / env frame: values never on the command line ────────────

SECRET = "hunter2 'q\"$x `id` \\n \\0101 %s ünï\ntrailing\n\n"


def test_env_frame_escapes_every_byte():
    assert env_frame({"A": "a\n", "B": ""}) == b"A=\\0141\\0012\nB=\n.\n"
    assert env_frame({}) == b".\n"


@pytest.mark.parametrize("name", ["", "1A", "A-B", "A B", "A=B", "é"])
def test_env_frame_refuses_bad_names(name):
    with pytest.raises(ValueError, match="invalid environment variable name"):
        env_frame({name: "x"})


def test_env_frame_refuses_nul():
    with pytest.raises(ValueError, match="NUL"):
        env_frame({"A": "x\x00y"})


def test_exec_without_env_is_the_plain_command():
    assert build_exec_argv(CMD) == (CMD, None)
    assert build_exec_argv(CMD, run_as="deploy") == (["sudo", "-u", "deploy", *CMD], None)


# A command outside any venv (a system prefix) takes the shell prelude.
SYS_CMD = ["/usr/local/bin/restart-service", "--name", "nginx"]
VENV_PY = "/venv/bin/python"


def test_exec_sudo_with_env_wraps_a_venv_command_in_the_python_prelude():
    argv, frame = build_exec_argv(CMD, run_as="deploy", become_flags="-H", env={"K": SECRET})
    assert argv == ["sudo", "-H", "-u", "deploy", VENV_PY, "-c", PY_ENV_PRELUDE, *CMD]
    assert frame == env_frame({"K": SECRET})
    assert not any("hunter2" in a for a in argv)


def test_exec_sudo_with_env_wraps_other_commands_in_the_shell_prelude():
    argv, frame = build_exec_argv(SYS_CMD, run_as="deploy", become_flags="-H", env={"K": SECRET})
    assert argv == ["sudo", "-H", "-u", "deploy", "sh", "-c", ENV_PRELUDE, "sh", *SYS_CMD]
    assert frame == env_frame({"K": SECRET})


def test_exec_no_run_as_with_env_still_uses_the_prelude():
    argv, frame = build_exec_argv(CMD, env={"K": SECRET})
    assert argv == [VENV_PY, "-c", PY_ENV_PRELUDE, *CMD]
    assert frame is not None
    argv, _ = build_exec_argv(SYS_CMD, env={"K": SECRET})
    assert argv == ["sh", "-c", ENV_PRELUDE, "sh", *SYS_CMD]


def test_exec_su_puts_the_prelude_inside_the_dash_c_string():
    import shlex

    argv, _ = build_exec_argv(CMD, run_as="deploy", become_method="su", env={"K": SECRET})
    assert argv[0] == "su" and argv[-1] == "deploy"
    inner = argv[argv.index("-c") + 1]
    assert shlex.split(inner) == [VENV_PY, "-c", PY_ENV_PRELUDE, *CMD]
    assert "hunter2" not in inner
    argv, _ = build_exec_argv(SYS_CMD, run_as="deploy", become_method="su", env={"K": SECRET})
    inner = argv[argv.index("-c") + 1]
    assert inner.startswith("sh -c '" + ENV_PRELUDE + "' sh /usr/local/bin/restart-service")


def test_the_preludes_have_no_quotes_or_expansions():
    # Both embed verbatim in a single-quoted shell word (and inside su's -c string).
    assert "'" not in ENV_PRELUDE
    for ch in "'\"$`":
        assert ch not in PY_ENV_PRELUDE


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/opt/venvs/ops/bin/system-info", "/opt/venvs/ops/bin/python"),
        ("/opt/venvs/ops/bin/python", "/opt/venvs/ops/bin/python"),
        ("/home/alice/venvs/tools/bin/runspec", "/home/alice/venvs/tools/bin/python"),
        ("/usr/bin/system-info", None),
        ("/usr/local/bin/x", None),
        ("/bin/x", None),
        ("/snap/bin/x", None),
        ("/opt/homebrew/bin/x", None),
        ("/home/alice/.local/bin/x", None),
        ("/opt/venvs/ops/x", None),  # not in a bin/
        ("system-info", None),  # not absolute
        ("C:\\venvs\\ops\\Scripts\\x.exe", None),
        ("/opt/venvs/ops/bin/", None),
    ],
)
def test_venv_python_for_judges_a_remote_path_by_shape(command, expected):
    assert venv_python_for(command) == expected


def test_venv_python_for_requires_pyvenv_cfg_for_a_local_path(tmp_path):
    bin_dir = tmp_path / "v" / "bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "tool"
    tool.write_text("")
    assert venv_python_for(str(tool)) is None  # exists here, but no pyvenv.cfg
    (tmp_path / "v" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert venv_python_for(str(tool)) == str(bin_dir / "python")


def test_the_python_prelude_matches_the_spec():
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[4] / "spec" / "SPEC.md").read_text(encoding="utf-8")
    assert "```python\n" + PY_ENV_PRELUDE + "\n```" in spec


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_the_python_prelude_delivers_values_exactly(tmp_path):
    # A real venv shape: pyvenv.cfg + bin/python. The command is that python, so the
    # prelude runs on the same interpreter (as for a Marketplace pip run).
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    py = venv / "bin" / "python"
    py.symlink_to(sys.executable)
    env = {"A": "", "B": "trail\n\n", "C": SECRET, "D": "a\nb", "E": "-n", "F": "."}
    probe = "import json, os, sys; print(json.dumps({k: os.environ.get(k) for k in 'ABCDEF'})); print(sys.stdin.read(), end='')"
    argv, frame = build_exec_argv([str(py), "-c", probe], env=env)
    assert argv[:3] == [str(py), "-c", PY_ENV_PRELUDE]
    res = subprocess.run(argv, input=frame + b"rest of stdin\n", capture_output=True, timeout=30)
    assert res.returncode == 0, res.stderr
    first, rest = res.stdout.decode().split("\n", 1)
    assert json.loads(first) == env
    assert rest == "rest of stdin\n"  # nothing past the frame was consumed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_the_python_prelude_searches_path_and_tolerates_eof(tmp_path):
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    py = venv / "bin" / "python"
    py.symlink_to(sys.executable)
    # A frame cut short (no "." line) still runs the command with what was read,
    # like the shell prelude; a bare command name is found on PATH (execvp).
    frame = env_frame({"K": "v"})[: -len(b".\n")]
    res = subprocess.run([str(py), "-c", PY_ENV_PRELUDE, "env"], input=frame, capture_output=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert b"K=v" in res.stdout


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX sh")
@pytest.mark.parametrize("shell", ["sh", "dash", "bash", "busybox", "ksh", "mksh"])
def test_the_prelude_delivers_values_exactly(shell):
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    env = {"A": "", "B": "trail\n\n", "C": SECRET, "D": "a\nb", "E": "-n", "F": "."}
    probe = "import json, os, sys; print(json.dumps({k: os.environ.get(k) for k in 'ABCDEF'})); print(sys.stdin.read(), end='')"
    # Built by hand: sys.executable may live in a venv, which takes the Python prelude.
    frame = env_frame(env)
    argv = [exe, *(["sh"] if shell == "busybox" else []), "-c", ENV_PRELUDE, "sh", sys.executable, "-c", probe]
    res = subprocess.run(argv, input=frame + b"rest of stdin\n", capture_output=True, timeout=30)
    assert res.returncode == 0, res.stderr
    first, rest = res.stdout.decode().split("\n", 1)
    assert json.loads(first) == env
    assert rest == "rest of stdin\n"


def test_parse_env_frame_round_trips():
    env = {"A": "", "B": SECRET, "C": "a\nb"}
    assert parse_env_frame(env_frame(env)) == env
    assert parse_env_frame(env_frame(env) + b"trailing stdin") == env


@pytest.mark.parametrize("bad", [b"A=x\n.\n", b"A=\\0141", b"1A=\\0141\n.\n", b"A=\\9999\n.\n"])
def test_parse_env_frame_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_env_frame(bad)


# ── "@owner" resolution ───────────────────────────────────────────────────────

from runspec import become as _become  # noqa: E402,I001


def test_effective_run_as_order():
    assert _become.effective_run_as("svc", "other") == "svc"
    assert _become.effective_run_as(None, "other") == "other"
    assert _become.effective_run_as(None, None) == "@owner"
    # "" is a real value at either level — no escalation — not "absent".
    assert _become.effective_run_as("", "other") == ""
    assert _become.effective_run_as(None, "") == ""


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX only")
def test_environment_owner_names_the_owner(tmp_path):
    import pwd

    expected = "" if os.geteuid() == 0 else pwd.getpwuid(os.geteuid()).pw_name
    assert _become.environment_owner(tmp_path) == expected


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX only")
def test_environment_owner_never_resolves_to_root():
    assert os.stat("/").st_uid == 0
    assert _become.environment_owner("/") == ""


def test_environment_owner_missing_root_is_empty(tmp_path):
    assert _become.environment_owner(tmp_path / "nope") == ""


def test_environment_owner_without_posix_is_empty(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert _become.environment_owner(tmp_path) == ""


def test_resolve_account_only_touches_owner(monkeypatch):
    monkeypatch.setattr(_become, "environment_owner", lambda env_root=None: "svc")
    assert _become.resolve_account("@owner") == "svc"
    assert _become.resolve_account("alice") == "alice"
    assert _become.resolve_account("") == ""


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX only")
def test_escalation_target_skips_the_current_account(monkeypatch):
    import pwd

    me = pwd.getpwuid(os.geteuid()).pw_name
    assert _become.escalation_target(me) == ""
    assert _become.escalation_target(str(os.geteuid())) == ""
    monkeypatch.setattr(_become, "environment_owner", lambda env_root=None: me)
    assert _become.escalation_target("@owner") == ""
    monkeypatch.setattr(_become, "environment_owner", lambda env_root=None: "svc-xyz")
    assert _become.escalation_target("@owner") == "svc-xyz"
    assert _become.escalation_target("nope-user-xyz") == "nope-user-xyz"


def test_local_json_reports_owner_and_config_run_as(tmp_path, monkeypatch, capsys):
    from runspec import cli

    item = {"source": "x", "runnable": "python3", "spec": {}, "config_autonomy": None, "config_run_as": ""}
    monkeypatch.setattr(cli, "_discover_installed", lambda: [item])
    monkeypatch.setattr(_become, "environment_owner", lambda env_root=None: "svc")
    monkeypatch.setattr(sys, "executable", shutil.which("python3") or sys.executable)
    cli.cmd_local(["--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert out and out[0]["env_owner"] == "svc"
    assert out[0]["user"] == _become.current_user()
    assert out[0]["config_run_as"] == ""


def test_windows_owner_default_is_a_noop(monkeypatch):
    # No POSIX uid model: "@owner" never escalates and the identity check never fires.
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert _become.environment_owner() == ""
    assert _become.escalation_target("@owner") == ""
    assert _become.current_user() == ""
    assert _become.verify_run_as_identity("@owner") is None
