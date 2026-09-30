"""Parity: the vendored pure helpers must match runspec-console's originals.

Dev-only. Skipped when runspec-console isn't importable (it isn't a runtime dep).
This guards against the vendored copies drifting from the source of truth.
"""

from __future__ import annotations

import pytest

console = pytest.importorskip("runspec_console")

from runspec_mcp import configmerge as f_merge  # noqa: E402
from runspec_mcp import credentials as f_cred  # noqa: E402
from runspec_mcp import executor as f_exec  # noqa: E402
from runspec_mcp import hosts as f_hosts  # noqa: E402


def test_credentials_derivation_parity():
    from runspec_console import credentials as c_cred

    assert f_cred._KIND_ENV_FIELDS == c_cred._KIND_ENV_FIELDS
    for label in ["Jira", "Jira Dev", "Windows", "DB", "!!!", "SNOW"]:
        assert f_cred.env_prefix(label) == c_cred.env_prefix(label)
        for kind in ["userpass", "password", "token", "ssh-key"]:
            assert f_cred.env_var_names(label, kind) == c_cred.env_var_names(label, kind)
            entry = {"label": label, "kind": kind, "username": "u"}
            assert f_cred.env_vars_for(entry, "s") == c_cred.env_vars_for(entry, "s")


def test_configmerge_parity():
    from runspec_console import configmerge as c_merge

    assert f_merge.COLLECTION_FILES == c_merge.COLLECTION_FILES
    assert f_merge.DEFAULT_PRESERVE == c_merge.DEFAULT_PRESERVE
    local = [{"name": "a", "x": 1}, {"name": "b", "x": 2}]
    incoming = [{"name": "b", "x": 9}, {"name": "c", "x": 3}]
    assert f_merge.merge_collection(local, incoming, "name", []) == c_merge.merge_collection(local, incoming, "name", [])


def test_executor_pure_helpers_parity():
    from runspec_console import executor as c_exec

    args = {"env": "prod", "force": True, "skip": False, "tags": ["a", "b"], "blank": ""}
    assert f_exec.args_to_argv(args) == c_exec.args_to_argv(args)
    assert f_exec.shell_join(["a", "b c", "d;e"]) == c_exec.shell_join(["a", "b c", "d;e"])
    assert f_exec.secret_env("my-tool", {"db-pass": "hunter2"}) == c_exec.secret_env("my-tool", {"db-pass": "hunter2"})
    for msg in ["Error reading SSH protocol banner", "auth failed", "connection reset", "codec can't decode"]:
        assert f_exec.is_transient_ssh_error(msg) == c_exec.is_transient_ssh_error(msg)
    # The SPEC env-frame command building (values on stdin, never in the line).
    argv = ["/v/bin/deploy", "--x", "a b"]
    for kw in [{}, {"env": {"K": "v"}}, {"run_as": "svc", "env": {"K": "p@ss w0rd"}}, {"run_as": "svc", "become_method": "su", "env": {"K": "v"}}]:
        assert f_exec.remote_exec_command(argv, **kw) == c_exec.remote_exec_command(argv, **kw)


def test_hosts_parity():
    from runspec_console import hosts as c_hosts

    p = "/opt/venvs/platform-core/bin/runspec"
    assert f_hosts.venv_name(p) == c_hosts.venv_name(p)
    assert str(f_hosts.venv_root(p)) == str(c_hosts.venv_root(p))
