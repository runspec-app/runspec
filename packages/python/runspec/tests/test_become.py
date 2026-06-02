"""
test_become.py — privilege escalation command construction (run_as / sudo).

Covers the SPEC "Remote Execution" command table and run_as resolution.
"""

from __future__ import annotations

import pytest

from runspec.become import (
    build_become_argv,
    resolve_run_as,
    validate_run_as_patterns,
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
    out = build_become_argv(CMD, env={"RUNSPEC_AGENT": "1"})
    assert out == ["RUNSPEC_AGENT=1", *CMD]


def test_sudo_default():
    out = build_become_argv(CMD, run_as="deploy")
    assert out == ["sudo", "-u", "deploy", *CMD]


def test_sudo_with_env_uses_env_binary():
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
