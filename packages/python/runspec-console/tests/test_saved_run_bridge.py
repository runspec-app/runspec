"""
test_saved_run_bridge.py — the SavedRunsMixin bridge seam.

Covers get_saved_runs (params → ArgDef, argsError tolerance), save_saved_runs
(validation, normalisation), run_saved_run (targeting resolution over the
shared _invoke_fanout engine: single host, group fan-out, hosts failover,
selected_hosts, command/credential threading, values merge), and the agent-path
_saved_run_calls expansion.
"""

from __future__ import annotations

import threading
import types

from runspec_console import savedruns
from runspec_console.bridge import Bridge


def _stub(tmp_path, monkeypatch, *, connected=None, groups=None, has=None):
    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path",
        lambda: tmp_path / "runspec_saved_runs.toml",
    )
    ns = types.SimpleNamespace()
    for name in (
        "get_saved_runs",
        "save_saved_runs",
        "run_saved_run",
        "_saved_run_entry",
        "_saved_run_calls",
        "_invoke_fanout",
        "_pick_connected_host",
    ):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._lock = threading.Lock()
    ns._connected_cache = dict(connected or {})
    ns._host_entry = lambda h: {"name": h, "ssh": "user@" + h}
    ns._group_members = lambda g: list((groups or {}).get(g, []))
    # has: set of (host, runnable) pairs that exist; None → everything exists.
    ns._host_has_runnable = lambda h, r: has is None or (h, r) in has
    ns._on_schedule_card = lambda card: None
    ns.dispatched = []
    ns._dispatch = lambda ev, payload: ns.dispatched.append((ev, payload))
    ns.runs = []
    ns.invoke_runnable = lambda host, runnable, args, **kw: (
        ns.runs.append((host, runnable, args, kw)) or f"inv-{host}"
    )
    return ns


def _seed(rows):
    savedruns.save_saved_runs(rows)


# ── get_saved_runs ────────────────────────────────────────────────────────────


def test_get_saved_runs_parses_params_and_target(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        [
            {
                "id": "restart-web",
                "title": "Restart web",
                "runnable": "svc-restart",
                "group": "Production",
                "args": {"service": "nginx"},
                "params": {"reason": {"required": True}},
            }
        ]
    )
    out = ns.get_saved_runs()
    assert len(out) == 1
    entry = out[0]
    assert entry["target"] == "group Production"
    assert entry["paramsRaw"] == {"reason": {"required": True}}
    names = [a["name"] for a in entry["params"]]
    assert names == ["reason"]
    assert "argsError" not in entry


def test_get_saved_runs_surfaces_bad_entry_without_breaking_list(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        [
            {"id": "bad", "runnable": "r"},  # no targeting mode
            {"id": "good", "runnable": "r", "host": "h"},
        ]
    )
    out = ns.get_saved_runs()
    assert [e["id"] for e in out] == ["bad", "good"]
    assert "argsError" in out[0]
    assert "argsError" not in out[1]


# ── save_saved_runs ───────────────────────────────────────────────────────────


def test_save_saved_runs_validates_and_normalises(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    res = ns.save_saved_runs(
        [
            {
                "id": "a",
                "runnable": "r",
                "host": "h",
                "title": "  A  ",
                "args": {"x": 1},
                "paramsRaw": {"p": {"required": True}},
            },
            {"id": "a", "runnable": "dup", "host": "h"},  # dup id — skipped
        ]
    )
    assert res == {"ok": True, "count": 1}
    rows = savedruns.load_saved_runs()
    assert rows[0]["id"] == "a"
    assert rows[0]["title"] == "A"
    assert rows[0]["params"] == {"p": {"required": True}}
    assert ("runspec:saved_runs_changed", {}) in ns.dispatched


def test_save_saved_runs_rejects_invalid_entry(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    res = ns.save_saved_runs([{"id": "a", "runnable": "r"}])  # no targeting
    assert res["ok"] is False
    assert "exactly one of" in res["error"]
    assert savedruns.load_saved_runs() == []


# ── run_saved_run ─────────────────────────────────────────────────────────────


def test_run_saved_run_single_host_merges_values(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        [
            {
                "id": "a",
                "runnable": "backup",
                "host": "prod-1",
                "command": ["db"],
                "args": {"target": "main"},
                "params": {"reason": {"required": True}},
                "credential_ids": ["c1"],
            }
        ]
    )
    res = ns.run_saved_run("a", {"reason": "patching"})
    assert res["runs"] == [{"host": "prod-1", "inv_id": "inv-prod-1"}]
    host, runnable, args, kw = ns.runs[0]
    assert (host, runnable) == ("prod-1", "backup")
    assert args == {"target": "main", "reason": "patching"}
    assert kw["command_path"] == ["db"]
    assert kw["credential_ids"] == ["c1"]
    assert kw["trigger"] == "saved_run"
    assert kw["operator"] is None  # invoke_runnable defaults to the current user


def test_run_saved_run_group_fans_out(tmp_path, monkeypatch):
    ns = _stub(
        tmp_path,
        monkeypatch,
        groups={"Prod": ["w1", "w2", "w3"]},
        has={("w1", "svc"), ("w3", "svc")},  # w2 lacks the runnable
    )
    _seed([{"id": "a", "runnable": "svc", "group": "Prod"}])
    res = ns.run_saved_run("a")
    assert [r["host"] for r in res["runs"]] == ["w1", "w3"]


def test_run_saved_run_hosts_failover_picks_first_connected(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch, connected={"h1": False, "h2": True})
    _seed([{"id": "a", "runnable": "svc", "hosts": ["h1", "h2"]}])
    res = ns.run_saved_run("a")
    assert [r["host"] for r in res["runs"]] == ["h2"]


def test_run_saved_run_selected_hosts_fans_out(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed([{"id": "a", "runnable": "svc", "selected_hosts": ["h1", "h2"]}])
    res = ns.run_saved_run("a")
    assert [r["host"] for r in res["runs"]] == ["h1", "h2"]


def test_run_saved_run_unknown_id(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed([{"id": "a", "runnable": "svc", "host": "h"}])
    res = ns.run_saved_run("nope")
    assert "no saved run" in res["error"]
    assert "a" in res["error"]  # known ids listed


def test_run_saved_run_invalid_values(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        [
            {
                "id": "a",
                "runnable": "svc",
                "host": "h",
                "params": {"mode": {"options": ["fast", "slow"]}},
            }
        ]
    )
    res = ns.run_saved_run("a", {"mode": "warp"})
    assert "error" in res
    assert ns.runs == []


def test_run_saved_run_no_schedule_card(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    cards = []
    ns._on_schedule_card = lambda card: cards.append(card)
    _seed([{"id": "a", "runnable": "svc", "host": "h"}])
    ns.run_saved_run("a")
    assert cards == []  # operator saved runs render Console blocks, not inbox cards


# ── _saved_run_calls (agent path) ─────────────────────────────────────────────


def test_saved_run_calls_expand_group_with_command_and_credentials(
    tmp_path, monkeypatch
):
    ns = _stub(
        tmp_path,
        monkeypatch,
        groups={"Prod": ["w1", "w2"]},
    )
    _seed(
        [
            {
                "id": "a",
                "runnable": "svc",
                "group": "Prod",
                "command": ["restart"],
                "args": {"x": "1"},
                "credential_ids": ["c1", "c2"],
            }
        ]
    )
    entry = ns._saved_run_entry("a")
    calls, err = ns._saved_run_calls(entry, None)
    assert err == ""
    assert [c[0] for c in calls] == ["w1__svc", "w2__svc"]
    assert calls[0][1]["x"] == "1"
    assert calls[0][1]["command"] == ["restart"]
    assert calls[0][1]["credential_ids"] == ["c1", "c2"]


def test_saved_run_calls_invalid_values_return_error(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        [
            {
                "id": "a",
                "runnable": "svc",
                "host": "h",
                "params": {"reason": {"required": True}},
            }
        ]
    )
    entry = ns._saved_run_entry("a")
    calls, err = ns._saved_run_calls(entry, {})
    assert calls == []
    assert "reason" in err


def test_saved_run_calls_hosts_failover(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch, connected={"h1": False, "h2": True})
    _seed([{"id": "a", "runnable": "svc", "hosts": ["h1", "h2"]}])
    entry = ns._saved_run_entry("a")
    calls, err = ns._saved_run_calls(entry, None)
    assert err == ""
    assert [c[0] for c in calls] == ["h2__svc"]
