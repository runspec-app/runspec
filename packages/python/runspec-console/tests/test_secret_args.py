"""
test_secret_args.py — password-typed args ride the environment, never argv.

A `password` arg must not appear on the command line (it would leak into shell
history / `ps`). The bridge pulls secret values out of the argv args and the
executor injects them as RUNSPEC_<RUNNABLE>_ARG_<NAME> env vars: into the child
process environment locally, and as a shell assignment / env(1) prefix remotely.
"""

from __future__ import annotations

import shlex

import pytest

from runspec_console import executor
from runspec_console.bridge import Bridge
from runspec_console.executor import secret_env


# ── secret_env mapping ──────────────────────────────────────────────────────────


def test_secret_env_maps_to_runspec_arg_var():
    assert secret_env("deploy", {"db-password": "hunter2"}) == {
        "RUNSPEC_DEPLOY_ARG_DB_PASSWORD": "hunter2"
    }


def test_secret_env_uses_root_runnable_name_and_uppercases():
    # Hyphens in both runnable and arg become underscores; everything uppercased.
    assert secret_env("backup-logs", {"api_key": "x"}) == {
        "RUNSPEC_BACKUP_LOGS_ARG_API_KEY": "x"
    }


def test_secret_env_drops_blank_and_none():
    assert secret_env("deploy", {"a": "", "b": None, "c": "v"}) == {
        "RUNSPEC_DEPLOY_ARG_C": "v"
    }


def test_secret_env_empty():
    assert secret_env("deploy", None) == {}
    assert secret_env("deploy", {}) == {}


# ── Bridge._secret_arg_names ─────────────────────────────────────────────────────


def test_secret_arg_names_flat():
    cached = {
        "args": [{"name": "host", "type": "str"}, {"name": "pw", "type": "password"}]
    }
    assert Bridge._secret_arg_names(cached, []) == {"pw"}


def test_secret_arg_names_resolves_subcommand_leaf():
    cached = {
        "args": [{"name": "token", "type": "password"}],
        "commands": {"run": {"args": [{"name": "secret", "type": "password"}]}},
    }
    assert Bridge._secret_arg_names(cached, ["run"]) == {"secret"}


def test_secret_arg_names_uncached_empty():
    assert Bridge._secret_arg_names(None, []) == set()


# ── run_local: secret goes to the env, never argv ────────────────────────────────


def test_run_local_injects_secret_into_env_not_argv(monkeypatch):
    captured: dict[str, object] = {}

    def fake_stream(cmd, on_line, on_done, **kwargs):
        captured["cmd"] = cmd
        captured["extra_env"] = kwargs.get("extra_env")

    monkeypatch.setattr(executor, "_stream", fake_stream)
    executor.run_local(
        "/venv/bin/runspec",
        "deploy",
        {"host": "web-01"},
        [],
        lambda *_: None,
        lambda *_: None,
        secrets={"db-password": "s3cr3t; rm -rf"},
    )
    # Secret value is not anywhere in the argv.
    assert "s3cr3t; rm -rf" not in " ".join(captured["cmd"])
    assert "db-password" not in " ".join(captured["cmd"])
    # It is supplied via the child environment instead.
    assert captured["extra_env"] == {"RUNSPEC_DEPLOY_ARG_DB_PASSWORD": "s3cr3t; rm -rf"}


# ── run_remote: secret becomes a shell-safe env prefix, never an argv flag ────────


class _FakeChannel:
    def __init__(self) -> None:
        self.command = ""
        self.stdin = b""
        self.eof = False

    def sendall(self, data):
        self.stdin += data

    def shutdown_write(self):
        self.eof = True

    def set_combine_stderr(self, _v):  # noqa: D401
        pass

    def exec_command(self, command):
        self.command = command

    # Exit immediately with no output.
    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return True

    def recv(self, _n):
        return b""

    def recv_stderr(self, _n):
        return b""

    def recv_exit_status(self):
        return 0

    def close(self):
        pass


def _run_remote_capture(secrets, run_as=""):
    # run_remote escalation/command building imports runspec.become (lazy in the
    # executor). The console test env doesn't install runspec core, so skip there.
    pytest.importorskip("runspec")
    chan = _FakeChannel()

    def session_factory():
        return chan, lambda: None

    executor.run_remote(
        "user@host",
        "/venv/bin/runspec",
        "deploy",
        {"host": "web-01"},
        [],
        lambda *_: None,
        lambda *_: None,
        run_as=run_as,
        session_factory=session_factory,
        secrets=secrets,
    )
    return chan


def test_run_remote_no_escalation_secret_rides_stdin_not_the_command():
    from runspec.become import PY_ENV_PRELUDE, parse_env_frame

    chan = _run_remote_capture({"db-password": "p@ss w0rd"})
    cmd = chan.command
    # The secret is in neither the command line (readable by every user via ps)
    # nor a --flag: it travels as the SPEC env frame on the channel's stdin.
    assert "p@ss" not in cmd and "--db-password" not in cmd
    # The runnable is in a venv, so the prelude runs on that venv's python.
    assert shlex.split(cmd)[:3] == ["/venv/bin/python", "-c", PY_ENV_PRELUDE]
    assert parse_env_frame(chan.stdin)["RUNSPEC_DEPLOY_ARG_DB_PASSWORD"] == "p@ss w0rd"
    assert chan.eof


def test_run_remote_with_escalation_secret_rides_stdin_inside_the_escalation():
    from runspec.become import PY_ENV_PRELUDE, parse_env_frame

    chan = _run_remote_capture({"db-password": "p@ss w0rd"}, run_as="deploy")
    tokens = shlex.split(chan.command)
    # Escalated form: sudo -u deploy <venv>/bin/python -c <prelude> <runnable> …
    # — sudo logs this line, so it must hold no value.
    assert tokens[:6] == [
        "sudo",
        "-u",
        "deploy",
        "/venv/bin/python",
        "-c",
        PY_ENV_PRELUDE,
    ]
    assert "p@ss" not in chan.command and "--db-password" not in chan.command
    assert parse_env_frame(chan.stdin)["RUNSPEC_DEPLOY_ARG_DB_PASSWORD"] == "p@ss w0rd"


def test_run_local_escalated_secret_rides_the_child_stdin(monkeypatch, tmp_path):
    from runspec.become import ENV_PRELUDE, parse_env_frame

    seen: dict = {}
    monkeypatch.setattr(
        executor, "_stream", lambda cmd, *a, **kw: seen.update(cmd=cmd, **kw)
    )
    executor.run_local(
        str(tmp_path / "runspec"),
        "deploy",
        {},
        [],
        lambda *_: None,
        lambda *_: None,
        run_as="deploy",
        secrets={"db-password": "p@ss w0rd"},
        cred_env={"JIRA_PASSWORD": "j1ra"},
    )
    cmd = seen["cmd"]
    assert cmd[:7] == ["sudo", "-u", "deploy", "sh", "-c", ENV_PRELUDE, "sh"]
    assert not any("p@ss" in c or "j1ra" in c for c in cmd)
    env = parse_env_frame(seen["stdin_data"])
    assert env["RUNSPEC_DEPLOY_ARG_DB_PASSWORD"] == "p@ss w0rd"
    assert env["JIRA_PASSWORD"] == "j1ra"
    assert not seen["extra_env"]  # nothing on the (stripped) sudo environment


def test_run_local_unescalated_secret_uses_the_child_environment(monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(
        executor, "_stream", lambda cmd, *a, **kw: seen.update(cmd=cmd, **kw)
    )
    executor.run_local(
        str(tmp_path / "runspec"),
        "deploy",
        {},
        [],
        lambda *_: None,
        lambda *_: None,
        secrets={"db-password": "p@ss w0rd"},
    )
    assert seen["stdin_data"] is None
    assert seen["extra_env"]["RUNSPEC_DEPLOY_ARG_DB_PASSWORD"] == "p@ss w0rd"
    assert not any("p@ss" in c for c in seen["cmd"])


def test_run_remote_jump_transport_feeds_the_frame_to_ssh(monkeypatch):
    from runspec.become import parse_env_frame

    seen: dict = {}
    monkeypatch.setattr(
        executor, "_stream", lambda cmd, *a, **kw: seen.update(cmd=cmd, **kw)
    )
    executor.run_remote(
        "user@legacy",
        "/venv/bin/runspec",
        "deploy",
        {},
        [],
        lambda *_: None,
        lambda *_: None,
        run_as="deploy",
        secrets={"db-password": "p@ss w0rd"},
        jump="bastion",
    )
    # The local ssh argv (visible in the operator's own process list) holds no value.
    assert not any("p@ss" in c for c in seen["cmd"])
    assert (
        parse_env_frame(seen["stdin_data"])["RUNSPEC_DEPLOY_ARG_DB_PASSWORD"]
        == "p@ss w0rd"
    )


def test_stream_writes_stdin_data_then_eof():
    import sys

    lines: list = []
    done: list = []
    code = "import sys; print(repr(sys.stdin.read()))"
    executor._stream(
        [sys.executable, "-c", code],
        lambda line, _s: lines.append(line),
        lambda c, _d: done.append(c),
        stdin_data=b"A=\\0141\n.\n",
    )
    assert done == [0]
    assert lines == [repr("A=\\0141\n.\n")]


def test_ssh_run_feeds_stdin_and_sends_eof():
    class _Chan:
        def __init__(self):
            self.eof = False

        def shutdown_write(self):
            self.eof = True

    class _In:
        def __init__(self):
            self.data = b""
            self.channel = _Chan()

        def write(self, d):
            self.data += d

        def flush(self):
            pass

    sin = _In()
    executor.feed_channel_stdin(sin, b"frame")
    assert sin.data == b"frame" and sin.channel.eof
    untouched = _In()
    executor.feed_channel_stdin(untouched, None)
    assert untouched.data == b"" and not untouched.channel.eof


def test_feeding_stdin_to_a_command_that_already_exited_does_not_raise():
    # sudo refused / the command exited early: the output + exit status still
    # tell the story, and raising would make the pool re-run the command.
    class _Dead:
        channel = None

        def write(self, _d):
            raise OSError("Socket is closed")

    executor.feed_channel_stdin(_Dead(), b"frame")


def test_run_remote_survives_a_channel_that_rejects_the_frame():
    class _Closed(_FakeChannel):
        def sendall(self, data):
            raise OSError("Socket is closed")

    chan = _Closed()
    done: list = []
    executor.run_remote(
        "user@host",
        "/venv/bin/runspec",
        "deploy",
        {},
        [],
        lambda *_: None,
        lambda c, _d: done.append(c),
        run_as="deploy",
        session_factory=lambda: (chan, lambda: None),
        secrets={"db-password": "x"},
    )
    assert done == [0]  # the run still completes and reports its exit status
