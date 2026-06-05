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
    return chan.command


def test_run_remote_no_escalation_secret_is_assignment_prefix():
    cmd = _run_remote_capture({"db-password": "p@ss w0rd"})
    # The runnable is invoked with the secret as a leading shell assignment whose
    # value is quoted — so an assignment stays an assignment, no injection.
    assert cmd.startswith("RUNSPEC_DEPLOY_ARG_DB_PASSWORD=")
    # The value never appears as a --flag argument.
    assert "--db-password" not in cmd
    # The shell parses the assignment back to the exact value.
    tokens = shlex.split(cmd)
    assert "RUNSPEC_DEPLOY_ARG_DB_PASSWORD=p@ss w0rd" in tokens


def test_run_remote_with_escalation_secret_after_env():
    cmd = _run_remote_capture({"db-password": "p@ss w0rd"}, run_as="deploy")
    tokens = shlex.split(cmd)
    # Escalated form: sudo -u deploy env ... RUNSPEC_..=value ... runnable
    assert tokens[:3] == ["sudo", "-u", "deploy"]
    assert "env" in tokens
    assert "RUNSPEC_DEPLOY_ARG_DB_PASSWORD=p@ss w0rd" in tokens
    assert "--db-password" not in cmd
