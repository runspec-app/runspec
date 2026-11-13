"""
test_on_demand_hosts.py — runs on hosts the refresh cycle doesn't cover.

Only the active profile's hosts are probed and discovered, but schedules, saved
runs, triggers and the agent reach every configured host. A host outside the
cycle has no cached records, so a run there went ahead blind (first venv, no
run_as, password args on argv) or failed with an exception. These cover the
on-demand look (``_discover_on_demand``) and the run path that uses it.
"""

from __future__ import annotations

import threading

from runspec_console import executor as executor_mod
from runspec_console import playbook_repo as playbook_repo_mod
from runspec_console.bridge import Bridge


class _Pool:
    def __init__(self, error=""):
        self.error = error

    def last_error(self, ssh, identity_file=None, global_ssh_config=None):
        return self.error


class _Harness:
    _discover_on_demand = Bridge._discover_on_demand
    _unreachable_message = Bridge._unreachable_message
    _live_host_records = Bridge._live_host_records
    _host_records = Bridge._host_records
    _host_has_runnable = Bridge._host_has_runnable
    _host_unreachable_reason = Bridge._host_unreachable_reason
    _warm_hosts = Bridge._warm_hosts
    _dedup_host_runnables = staticmethod(Bridge._dedup_host_runnables)
    _ON_DEMAND_FAIL_TTL = Bridge._ON_DEMAND_FAIL_TTL

    def __init__(self, hosts, *, reachable=(), found=None, profile="", in_profile=()):
        self._lock = threading.Lock()
        self._runnables_cache: list = []
        self._hosts = hosts
        self._reachable = set(reachable)
        self._found = found or {}
        self._profile = profile
        self._in_profile = set(in_profile)
        self._ssh_pool = _Pool("Connection timed out")
        self.discovered: list[str] = []

    def get_config(self):
        return {"ssh": {}, "refresh": {"discovery_interval": 300}}

    def _host_entry(self, host):
        return next((h for h in self._hosts if h["name"] == host), None)

    def _check_connected(self, entry, *, ssh_cfg=None):
        return entry["name"] in self._reachable

    def _discover_one(self, entry, *, ssh_cfg=None, assume_connected=False):
        assert assume_connected
        self.discovered.append(entry["name"])
        return [dict(r) for r in self._found.get(entry["name"], [])]

    def _active_profile_name(self):
        return self._profile

    def _fanout_workers(self):
        return 4


def _host(name, *profiles):
    return {
        "name": name,
        "ssh": f"u@{name}",
        "runspec_paths": ["/opt/venvs/ops-tools/bin/runspec"],
        "profiles": list(profiles),
    }


def _rec(host, name, group="ops-tools", **extra):
    return {"host": host, "name": name, "group": group, "args": [], **extra}


def test_cold_host_is_discovered_once_and_cached(monkeypatch):
    monkeypatch.setattr(playbook_repo_mod, "registered_names", lambda: [])
    b = _Harness(
        [_host("little-boy", "Homelab")],
        reachable={"little-boy"},
        found={"little-boy": [_rec("little-boy", "upgrade-packages")]},
    )
    records, error = b._discover_on_demand("little-boy")
    assert error == ""
    assert [r["name"] for r in records] == ["upgrade-packages"]
    assert b._host_has_runnable("little-boy", "upgrade-packages")
    assert not b._host_has_runnable("little-boy", "other")
    # One look, however many checks follow.
    assert b.discovered == ["little-boy"]
    # Nothing leaks into the live cache (the sidebar / Forms / tool list).
    assert b._runnables_cache == []


def test_unreachable_host_names_the_profile_and_the_ssh_error(monkeypatch):
    monkeypatch.setattr(playbook_repo_mod, "registered_names", lambda: [])
    b = _Harness([_host("little-boy", "Homelab")], profile="lab")
    records, error = b._discover_on_demand("little-boy")
    assert records == []
    assert error == (
        "couldn't connect to host 'little-boy' — it isn't in the active profile "
        "'lab': Connection timed out"
    )
    assert b._host_unreachable_reason("little-boy") == error
    # A failure is remembered briefly, so a fan-out doesn't retry it per check.
    b._discover_on_demand("little-boy")
    assert b.discovered == []


def test_a_host_in_the_live_cache_is_never_looked_at(monkeypatch):
    monkeypatch.setattr(playbook_repo_mod, "registered_names", lambda: [])
    b = _Harness([_host("web")], reachable={"web"})
    b._runnables_cache = [_rec("web", "ping")]
    assert b._host_has_runnable("web", "ping")
    b._warm_hosts(["web"])
    assert b.discovered == []


def test_warm_hosts_looks_at_every_cold_host(monkeypatch):
    monkeypatch.setattr(playbook_repo_mod, "registered_names", lambda: [])
    names = ["a", "b", "c"]
    b = _Harness([_host(n) for n in names], reachable=set(names))
    b._warm_hosts(names + ["a"])
    assert sorted(b.discovered) == names


# ── invoke_runnable on a cold host ───────────────────────────────────────────


class _InvokeHarness:
    invoke_runnable = Bridge.invoke_runnable
    _ui_output_caps = Bridge._ui_output_caps
    _positionals_for = staticmethod(Bridge._positionals_for)
    _secret_arg_names = staticmethod(Bridge._secret_arg_names)
    _become_params = Bridge._become_params
    _live_host_records = Bridge._live_host_records

    def __init__(self, found, error=""):
        self._entry = {
            "name": "little-boy",
            "ssh": None,
            "runspec_paths": [
                "/opt/venvs/ops-tools/bin/runspec",
                "/opt/venvs/ops-root/bin/runspec",
            ],
        }
        self._runnables_cache: list = []
        self._found = found
        self._error = error
        self._lock = threading.Lock()
        self._in_flight: dict = {}
        self.dispatched: list = []
        self.done = threading.Event()

    def _discover_on_demand(self, host):
        return list(self._found), self._error

    def _host_entry(self, _host):
        return self._entry

    def get_config(self):
        return {}

    def _credential_env(self, *_a):
        return {}, []

    def _apply_selected_credentials(self, *_a, **_k):
        return []

    def _declared_arg_names(self, *_a):
        return set()

    def _current_user(self):
        return "tester"

    def _play_sound(self, _name):
        pass

    def _persist_operator_run(self, **_kw):
        pass

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))
        if event == "runspec:run_end":
            self.done.set()


def test_cold_host_run_uses_its_own_record(monkeypatch):
    """The run lands in the venv the host's own discovery names, runs as the
    runnable's run_as, and keeps a password arg off argv."""
    record = _rec(
        "little-boy",
        "upgrade-packages",
        group="ops-root",
        rawSpec={"run_as": "root"},
        args=[{"name": "token", "type": "password"}],
    )
    b = _InvokeHarness([record])
    seen = {}

    def fake_run_local(rp, runnable, args, cp, on_line, on_done, **kw):
        seen.update(
            rp=rp, args=args, run_as=kw.get("run_as"), secrets=kw.get("secrets")
        )
        on_done(0, 1)

    monkeypatch.setattr(executor_mod, "run_local", fake_run_local)
    b.invoke_runnable("little-boy", "upgrade-packages", {"token": "s3cret", "x": 1})
    assert b.done.wait(5)
    assert seen["rp"] == "/opt/venvs/ops-root/bin/runspec"
    assert seen["run_as"] == "root"
    assert seen["args"] == {"x": 1}
    assert seen["secrets"] == {"token": "s3cret"}


def test_unreachable_cold_host_fails_with_a_reason(monkeypatch):
    b = _InvokeHarness([], error="couldn't connect to host 'little-boy': timed out")
    monkeypatch.setattr(
        executor_mod,
        "run_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    b.invoke_runnable("little-boy", "upgrade-packages", {})
    assert b.done.wait(5)
    lines = [d["line"] for e, d in b.dispatched if e == "runspec:output"]
    assert lines == ["✗  Didn't run: couldn't connect to host 'little-boy': timed out."]
    ends = [d for e, d in b.dispatched if e == "runspec:run_end"]
    assert ends[0]["exit_code"] == -1


def test_cold_host_without_the_runnable_says_so(monkeypatch):
    b = _InvokeHarness([_rec("little-boy", "system-info")])
    monkeypatch.setattr(
        executor_mod,
        "run_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    b.invoke_runnable("little-boy", "upgrade-packages", {})
    assert b.done.wait(5)
    lines = [d["line"] for e, d in b.dispatched if e == "runspec:output"]
    assert lines == [
        "✗  Didn't run: 'upgrade-packages' isn't installed on 'little-boy' "
        "(looked in ops-tools, ops-root)."
    ]


# ── reload: the current chat is marked ───────────────────────────────────────


def test_list_chat_sessions_marks_the_current_chat():
    from runspec_console.session import new_interactive

    class _S:
        list_chat_sessions = Bridge.list_chat_sessions

    s = _S()
    s._lock = threading.Lock()
    a, b = new_interactive(), new_interactive()
    s._sessions = {a.id: a, b.id: b}
    s._chat_session_id = a.id
    rows = {r["id"]: r for r in s.list_chat_sessions()}
    assert rows[a.id]["current"] is True
    assert rows[b.id]["current"] is False
