"""
test_host_groups.py — host groups as a first-class targeting concept.

Covers the pure resolvers (hosts.group_members / groups_map, triggers.parse_target,
schedule host-XOR-group validation) and the bridge fan-out seam (group detection,
target expansion, most-restrictive grouped run) bound onto a bare stub so we dodge
``Bridge.__init__`` side effects, à la test_schedule_bridge.
"""

from __future__ import annotations

import threading
import types

import pytest

from runspec_console.bridge import Bridge
from runspec_console.hosts import group_members, groups_map
from runspec_console.sources.schedule import parse_schedule_entry
from runspec_console.triggers import parse_target

# ── pure resolvers ────────────────────────────────────────────────────────────

_HOSTS = [
    {"name": "h1", "group": "Prod", "ssh": "x"},
    {"name": "h2", "group": "prod", "ssh": "y"},  # case-insensitive match
    {"name": "h3", "ssh": "z"},  # ungrouped
    {"name": "h4", "group": "Stage"},
]


def test_group_members_case_insensitive_and_ordered():
    assert group_members(_HOSTS, "Prod") == ["h1", "h2"]
    assert group_members(_HOSTS, "prod") == ["h1", "h2"]
    assert group_members(_HOSTS, "Stage") == ["h4"]


def test_group_members_empty_group_never_matches():
    assert group_members(_HOSTS, "") == []
    assert group_members(_HOSTS, "   ") == []
    assert group_members(_HOSTS, "nope") == []


def test_groups_map_keeps_declared_casing_and_skips_ungrouped():
    m = groups_map(_HOSTS)
    assert m == {"Prod": ["h1"], "prod": ["h2"], "Stage": ["h4"]}


def test_parse_target_group_and_host():
    assert parse_target("group:Production__restart-service") == (
        "group",
        "Production",
        "restart-service",
    )
    assert parse_target("web01__restart-service") == (
        "host",
        "web01__restart-service",
        "restart-service",
    )
    assert parse_target("  group:Prod__x  ") == ("group", "Prod", "x")


def test_parse_schedule_entry_requires_exactly_one_of_host_group():
    base = {"id": "s1", "when": {"every": "5m"}, "action": "runnable", "runnable": "r"}
    # host only
    j = parse_schedule_entry({**base, "host": "h1"})
    assert j.host == "h1" and j.group == ""
    # group only
    j = parse_schedule_entry({**base, "group": "Prod"})
    assert j.group == "Prod" and j.host == ""
    # neither → error
    with pytest.raises(ValueError, match="exactly one of"):
        parse_schedule_entry(base)
    # both → error
    with pytest.raises(ValueError, match="exactly one of"):
        parse_schedule_entry({**base, "host": "h1", "group": "Prod"})
    # missing runnable → error
    with pytest.raises(ValueError, match="missing 'runnable'"):
        parse_schedule_entry({"id": "s2", "when": {"every": "5m"}, "host": "h1"})


# ── bridge fan-out seam (bare stub) ───────────────────────────────────────────


def _stub(**attrs):
    ns = types.SimpleNamespace()
    for name in (
        "_group_fanout_calls",
        "_format_group_output",
        "expand_targets",
        "_group_calls_level",
        "_grouped_confirm_run_sync",
        "_run_calls_parallel",
        "_apply_group_edit",
    ):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._lock = threading.Lock()
    for k, v in attrs.items():
        setattr(ns, k, v)
    return ns


def test_group_fanout_calls_expands_present_members_only():
    ns = _stub(
        _groups_map=lambda: {"Prod": ["h1", "h2", "h3"]},
        _host_entry=lambda h: None,  # no host-name collision
        _runnables_for_id=lambda rid: [
            {"host": "h1", "name": "restart"},
            {"host": "h2", "name": "restart"},
        ],  # h3 lacks the runnable
    )
    out = ns._group_fanout_calls({"id": "restart", "host": "Prod", "args": {"x": "1"}})
    assert out is not None
    group, name, calls = out
    assert group == "Prod" and name == "restart"
    assert calls == [("h1__restart", {"x": "1"}), ("h2__restart", {"x": "1"})]


def test_group_fanout_calls_host_name_wins_on_collision():
    ns = _stub(
        _groups_map=lambda: {"Prod": ["h1"]},
        _host_entry=lambda h: {"name": "Prod"},  # a real host named "Prod"
        _runnables_for_id=lambda rid: [{"host": "h1", "name": "restart"}],
    )
    assert ns._group_fanout_calls({"id": "restart", "host": "Prod"}) is None


def test_group_fanout_calls_not_a_group_returns_none():
    ns = _stub(_groups_map=lambda: {"Prod": ["h1"]})
    assert ns._group_fanout_calls({"id": "restart", "host": "web01"}) is None
    assert ns._group_fanout_calls({"id": "restart", "host": ""}) is None


def test_group_fanout_calls_threads_command_and_credential():
    ns = _stub(
        _groups_map=lambda: {"Prod": ["h1"]},
        _host_entry=lambda h: None,
        _runnables_for_id=lambda rid: [{"host": "h1", "name": "deploy"}],
    )
    _g, _n, calls = ns._group_fanout_calls(
        {"id": "deploy", "host": "Prod", "command": "rollback", "credential_id": "c1"}
    )
    assert calls == [("h1__deploy", {"command": "rollback", "credential_id": "c1"})]


def test_expand_targets_group_and_plain():
    ns = _stub(
        _group_members=lambda g: ["h1", "h2"],
        _host_has_runnable=lambda h, n: h != "h2",  # h2 lacks it
    )
    assert ns.expand_targets("group:Prod__restart") == ["h1__restart"]
    assert ns.expand_targets("web01__restart") == ["web01__restart"]


def test_format_group_output_has_per_host_headers():
    calls = [("h1__restart", {}), ("h2__restart", {})]
    text = Bridge._format_group_output("Prod", "restart", calls, ["ok1", "ok2"])
    assert "Ran 'restart' on group 'Prod' (2 hosts):" in text
    assert "── h1 ──\nok1" in text
    assert "── h2 ──\nok2" in text


def test_grouped_confirm_run_sync_autonomous_runs_all_parallel():
    ran: list[str] = []

    def fake_run(name, inp, chat_id):
        ran.append(name)
        return f"out:{name}"

    ns = _stub(
        _effective_tool_autonomy=lambda n, i: "autonomous",
        _run_tool_sync=fake_run,
    )
    calls = [("h1__r", {}), ("h2__r", {})]
    out = ns._grouped_confirm_run_sync("c", calls, noun="group 'Prod' run")
    assert out == ["out:h1__r", "out:h2__r"]
    assert set(ran) == {"h1__r", "h2__r"}


def test_grouped_confirm_run_sync_manual_refuses_whole_group():
    ns = _stub(
        _effective_tool_autonomy=lambda n, i: (
            "manual" if n == "h2__r" else "autonomous"
        ),
        _run_tool_sync=lambda *a: pytest.fail("must not run when group is manual"),
    )
    out = ns._grouped_confirm_run_sync(
        "c", [("h1__r", {}), ("h2__r", {})], noun="group 'Prod' run"
    )
    assert len(out) == 2
    assert all("manual-only" in line for line in out)
