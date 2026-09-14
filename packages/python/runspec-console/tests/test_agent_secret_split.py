"""
test_agent_secret_split.py — the agent (LLM) runnable path must keep secrets off argv.

``_run_tool_sync`` is what the agentic loop calls for a runnable tool. Like the
manual ``invoke_runnable`` path, it must split ``password``-typed args out of the
argv args and hand them to the executor as ``secrets=`` (which become
``RUNSPEC_*_ARG_*`` env vars) — otherwise a secret would land in the remote's
``ps`` / shell history. These tests bind the real method onto a light harness and
capture what reaches ``run_local`` / ``run_remote``.
"""

from __future__ import annotations

import threading

from runspec_console.bridge import Bridge


class _AgentBridge:
    _run_tool_sync = Bridge._run_tool_sync

    _finalize_tool_output = Bridge._finalize_tool_output
    _tool_output_cap = Bridge._tool_output_cap
    # Routing helpers _run_tool_sync delegates to. The host__name tool names below
    # resolve via the per-host fallback under either layout.
    _resolve_tool_target = Bridge._resolve_tool_target
    _tool_mode = Bridge._tool_mode
    _dedup_tool_name = Bridge._dedup_tool_name
    _dedup_tool_aliases = Bridge._dedup_tool_aliases
    _host_selector_key = Bridge._host_selector_key
    # These are @staticmethod on Bridge; re-wrap so binding onto the harness
    # doesn't turn them into instance methods (which would inject ``self``).
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _positionals_for = staticmethod(Bridge._positionals_for)
    # Credential resolution is a no-op here (no credentials configured).
    _apply_credentials = Bridge._apply_credentials
    _credential_env = Bridge._credential_env
    _scoped_credentials = Bridge._scoped_credentials
    _scope_matches = staticmethod(Bridge._scope_matches)
    _credential_entry = Bridge._credential_entry
    _declared_arg_names = staticmethod(Bridge._declared_arg_names)
    # search-mode run_runnable resolution + preflight
    _resolve_id_host = Bridge._resolve_id_host
    _runnables_for_id = Bridge._runnables_for_id
    _runbook_id_hint = staticmethod(Bridge._runbook_id_hint)
    _coerce_run_args = staticmethod(Bridge._coerce_run_args)
    _preflight_validate = Bridge._preflight_validate
    _node_at = staticmethod(Bridge._node_at)

    def _visible_runnables(self, _surface):
        return self._runnables_cache

    # Stubbed: the real _become_params imports the runspec core package, which
    # the console test suite runs without. Escalation isn't under test here.
    def _become_params(self, cached, hostname):
        return "", "sudo", None

    def __init__(self, entry, cached):
        self._entry = entry
        self._runnables_cache = [cached]
        self._lock = threading.Lock()
        self._credentials: list = []

    def _host_entry(self, _host):
        return self._entry

    def get_config(self):
        return {"ssh": {}}

    def _reload_credentials(self):
        pass


def _cached(host: str) -> dict:
    # The runnables cache is matched by (host, name) in _run_tool_sync, so the
    # record's host must equal the tool's host for its args to be found.
    return {
        "host": host,
        "name": "deploy",
        "group": None,
        "args": [
            {"name": "service-username", "type": "str"},
            {"name": "user-password", "type": "password"},
        ],
    }


def test_run_tool_sync_local_splits_password(monkeypatch):
    captured: dict = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["args"] = args
        captured["secrets"] = kw.get("secrets")

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )

    b = _AgentBridge(
        entry={"name": "local"}, cached=_cached("local")
    )  # no "ssh" → local
    b._run_tool_sync(
        "local__deploy",
        {"service-username": "svc", "user-password": "s3cret"},
    )

    assert captured["args"] == {"service-username": "svc"}
    assert captured["secrets"] == {"user-password": "s3cret"}


def _cached_sub(host: str) -> dict:
    # A subcommand runnable: `db restore --target …`, restore marked autonomous.
    return {
        "host": host,
        "name": "db",
        "group": None,
        "args": [],
        "commands": {
            "restore": {
                "name": "restore",
                "autonomy": "autonomous",
                "args": [{"name": "target", "type": "str"}],
            }
        },
    }


def test_run_runnable_list_command_reaches_the_subcommand_leaf(monkeypatch):
    # A model may send `command` as a nested list path (the shape saved runs use).
    # The executor must descend to that leaf — the same node the autonomy gate
    # resolved — not silently run the top-level runnable. Regression: the
    # run_runnable branch used `[cmd] if str` and dropped a list to [].
    captured: dict = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["command_path"] = command_path
        captured["args"] = args

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )

    b = _AgentBridge(entry={"name": "local"}, cached=_cached_sub("local"))
    # command as a list path
    b._run_tool_sync(
        "run_runnable",
        {
            "id": "local__db",
            "host": "local",
            "command": ["restore"],
            "args": {"target": "x"},
        },
    )
    assert captured["command_path"] == ["restore"]
    assert captured["args"] == {"target": "x"}

    # command as a plain string resolves to the same leaf
    captured.clear()
    b._run_tool_sync(
        "run_runnable",
        {
            "id": "local__db",
            "host": "local",
            "command": "restore",
            "args": {"target": "x"},
        },
    )
    assert captured["command_path"] == ["restore"]


def test_run_tool_sync_remote_splits_password(monkeypatch):
    captured: dict = {}

    def fake_run_remote(
        ssh, rp, runnable, args, command_path, on_line, on_done, idf, **kw
    ):
        captured["args"] = args
        captured["secrets"] = kw.get("secrets")

    monkeypatch.setattr("runspec_console.executor.run_remote", fake_run_remote)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )

    entry = {"name": "prod", "ssh": "deploy@prod", "identityFile": "/k"}
    b = _AgentBridge(entry=entry, cached=_cached("prod"))
    b._run_tool_sync(
        "prod__deploy",
        {"service-username": "svc", "user-password": "s3cret"},
    )

    assert captured["args"] == {"service-username": "svc"}
    assert captured["secrets"] == {"user-password": "s3cret"}
