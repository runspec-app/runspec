"""
test_schedule_bridge.py — the Schedules-tab bridge seam (get/update/run-now).

Covers the bits the Schedules UI added on top of create/delete:

  - get_schedules attaches a display-only ``nextRun`` (and never persists it),
  - update_schedule replaces an entry in place keeping its id,
  - run_schedule_now routes a runnable-action schedule to a direct run and an
    event-action schedule to an engine.feed() emit, ignoring ``enabled``.

The methods are bound onto a bare stub (à la test_email_bridge) so we dodge
``Bridge.__init__`` side effects; only the collaborators each method actually
touches are supplied.
"""

from __future__ import annotations

import types

import pytest

import runspec_console.bridge as bmod
from runspec_console.bridge import Bridge, _write_schedules


def _stub(tmp_path, monkeypatch, *, engine=None):
    monkeypatch.setattr(bmod, "hosts_path", lambda: tmp_path / "runspec_hosts.toml")
    ns = types.SimpleNamespace()
    for name in (
        "_read_schedules",
        "get_schedules",
        "_schedule_next_run",
        "create_schedule",
        "update_schedule",
        "delete_schedule",
        "run_schedule_now",
        "_invoke_scheduled",
    ):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns.restart_triggers = lambda: None
    ns._trigger_engine = engine
    ns.runs = []
    ns.invoke_runnable = lambda host, runnable, args, **kw: (
        ns.runs.append((host, runnable, args, kw)) or "inv-xyz"
    )
    return ns


def _seed(tmp_path, schedules):
    _write_schedules(tmp_path / "runspec_schedules.toml", schedules)


# ── get_schedules: computed, non-persisted nextRun ────────────────────────────


def test_get_schedules_attaches_next_run(tmp_path, monkeypatch):
    pytest.importorskip("apscheduler")
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "h",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
            }
        ],
    )
    out = ns.get_schedules()
    assert out[0]["nextRun"]  # ISO string present for an enabled schedule
    # and it is display-only — the raw read never carries it
    assert "nextRun" not in ns._read_schedules()[0]


def test_get_schedules_disabled_has_no_next_run(tmp_path, monkeypatch):
    pytest.importorskip("apscheduler")
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "h",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
                "enabled": False,
            }
        ],
    )
    assert "nextRun" not in ns.get_schedules()[0]


# ── update_schedule: in-place replace, transient-key stripped ─────────────────


def test_update_schedule_replaces_in_place(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {"id": "a", "action": "event", "event": "e1", "when": {"every": "5m"}},
            {"id": "b", "action": "event", "event": "e2", "when": {"every": "5m"}},
        ],
    )
    ns.update_schedule(
        "a",
        {
            "id": "a",
            "action": "event",
            "event": "e1-renamed",
            "when": {"every": "10m"},
            "nextRun": "2026-01-01T00:00:00",  # must not be persisted
        },
    )
    raw = ns._read_schedules()
    assert [s["id"] for s in raw] == ["a", "b"]  # order + count preserved
    assert raw[0]["event"] == "e1-renamed"
    assert raw[0]["when"]["every"] == "10m"
    assert "nextRun" not in raw[0]  # transient key stripped on write


def test_update_schedule_keeps_id_stable(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [{"id": "a", "action": "event", "event": "e", "when": {"every": "5m"}}],
    )
    # Even if the payload carries a different id, the addressed id wins.
    ns.update_schedule(
        "a", {"id": "WRONG", "action": "event", "event": "e", "when": {"every": "5m"}}
    )
    assert [s["id"] for s in ns._read_schedules()] == ["a"]


# ── run_schedule_now: route by action, ignore enabled ─────────────────────────


def test_run_now_runnable_invokes_directly(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "prod-1",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
                "args": {"db": "main"},
                "enabled": False,  # ignored — running now is the authorization
            }
        ],
    )
    res = ns.run_schedule_now("r1")
    assert res == {
        "action": "runnable",
        "inv_id": "inv-xyz",
        "host": "prod-1",
        "runnable": "backup",
    }
    host, runnable, args, kw = ns.runs[0]
    assert (host, runnable, args) == ("prod-1", "backup", {"db": "main"})
    assert kw == {"operator": "Scheduled Task", "trigger": "schedule"}


def test_run_now_event_feeds_engine(tmp_path, monkeypatch):
    fed = []
    engine = types.SimpleNamespace(feed=fed.append)
    ns = _stub(tmp_path, monkeypatch, engine=engine)
    _seed(
        tmp_path,
        [
            {
                "id": "e1",
                "action": "event",
                "event": "nightly",
                "when": {"every": "5m"},
                "fields": {"scope": "prod"},
            }
        ],
    )
    res = ns.run_schedule_now("e1")
    assert res == {"action": "event", "event": "nightly"}
    assert len(fed) == 1
    ev = fed[0]
    assert ev.source == "schedule" and ev.type == "nightly"
    assert ev.payload["scope"] == "prod" and ev.payload["schedule"] == "e1"


def test_run_now_event_without_engine_raises(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch, engine=None)
    _seed(
        tmp_path,
        [{"id": "e1", "action": "event", "event": "n", "when": {"every": "5m"}}],
    )
    with pytest.raises(RuntimeError):
        ns.run_schedule_now("e1")


def test_run_now_unknown_id_raises(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(tmp_path, [])
    with pytest.raises(ValueError):
        ns.run_schedule_now("nope")
