"""
test_catalog.py — the presence-aware live catalogue + affinity registry (pure).

No sockets, no hub: drive ``LiveCatalog`` directly and assert the live union and
the holder lists track per-agent publishes and rota flips. Mirrors the portal's
two questions — what a visitor sees, and who a request routes to.
"""

from __future__ import annotations

from console_room.catalog import LiveCatalog, entry_audience, visible_to

IT = "helpdesk/it"
HR = "helpdesk/hr"


def _names(tools):
    return [t["name"] for t in tools]


def tool(name, **extra):
    return {"name": name, **extra}


# ── live union across agents ──────────────────────────────────────────────────


def test_live_is_union_of_on_shift_agents():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password"), tool("request-vm")])
    cat.publish(IT, "b@web02", [tool("unlock-account")])
    # Union across both on-shift consoles, in agent-arrival order.
    assert _names(cat.live(IT)) == ["reset-password", "request-vm", "unlock-account"]


def test_live_dedups_by_name_first_holder_wins():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password", description="A")])
    cat.publish(IT, "b@web02", [tool("reset-password", description="B")])
    live = cat.live(IT)
    # One entry, and it's the first holder's schema.
    assert _names(live) == ["reset-password"]
    assert live[0]["description"] == "A"


def test_live_skips_unnamed_entries():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [{"description": "no name"}, tool("ok")])
    assert _names(cat.live(IT)) == ["ok"]


def test_namespaces_are_isolated():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.publish(HR, "c@web03", [tool("onboard")])
    assert _names(cat.live(IT)) == ["reset-password"]
    assert _names(cat.live(HR)) == ["onboard"]
    assert cat.live("helpdesk/unknown") == []


# ── presence (on shift vs off shift) ──────────────────────────────────────────


def test_off_shift_agent_drops_out_of_live():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.publish(IT, "b@web02", [tool("request-vm")])
    cat.set_presence(IT, "b@web02", False)  # b's rota flips off shift
    assert _names(cat.live(IT)) == ["reset-password"]
    cat.set_presence(IT, "b@web02", True)  # back on shift → reappears
    assert _names(cat.live(IT)) == ["reset-password", "request-vm"]


def test_publish_preserves_presence():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.set_presence(IT, "a@web01", False)
    cat.publish(IT, "a@web01", [tool("reset-password"), tool("request-vm")])
    # Re-publishing while off shift doesn't put the agent back on shift.
    assert cat.live(IT) == []
    assert sorted(cat.on_shift_agents(IT)) == []


def test_presence_before_catalogue():
    cat = LiveCatalog()
    cat.set_presence(IT, "a@web01", True)  # connected, on shift, nothing published
    assert cat.connected(IT) == ["a@web01"]
    assert cat.on_shift_agents(IT) == ["a@web01"]
    assert cat.live(IT) == []  # holds nothing yet


# ── affinity (holders) ────────────────────────────────────────────────────────


def test_holders_are_on_shift_agents_with_the_runnable():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password"), tool("request-vm")])
    cat.publish(IT, "b@web02", [tool("reset-password")])
    assert sorted(cat.holders(IT, "reset-password")) == ["a@web01", "b@web02"]
    # Single holder routes to exactly that agent.
    assert cat.holders(IT, "request-vm") == ["a@web01"]
    assert cat.holders(IT, "unknown") == []


def test_off_shift_holder_excluded_but_still_connected_holder():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.set_presence(IT, "a@web01", False)
    # No on-shift candidate to route to…
    assert cat.holders(IT, "reset-password") == []
    # …but the page can still reach the off-shift-but-connected holder.
    assert cat.connected_holders(IT, "reset-password") == ["a@web01"]


# ── withdraw (console disconnect) ─────────────────────────────────────────────


def test_withdraw_removes_agents_runnables():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.publish(IT, "b@web02", [tool("request-vm")])
    cat.withdraw(IT, "a@web01")  # a's console disconnects
    assert _names(cat.live(IT)) == ["request-vm"]
    assert cat.connected(IT) == ["b@web02"]


def test_withdraw_last_agent_prunes_namespace():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.withdraw(IT, "a@web01")
    assert cat.live(IT) == []
    assert cat.connected(IT) == []
    assert cat.connected_holders(IT, "reset-password") == []


def test_withdraw_is_idempotent():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("reset-password")])
    cat.withdraw(IT, "a@web01")
    cat.withdraw(IT, "a@web01")  # no crash on an already-gone agent
    cat.withdraw("helpdesk/unknown", "ghost")  # unknown namespace, no crash
    assert cat.live(IT) == []


# ── audience filtering (operator view vs visitor) ─────────────────────────────


def test_entry_audience_defaults_to_self_service():
    assert entry_audience(tool("a")) == "self-service"  # untagged → self-service
    assert entry_audience(tool("a", **{"x-audience": "operator"})) == "operator"


def test_visible_to_operator_sees_all_visitor_sees_self_service():
    op_only = tool("rotate-key", **{"x-audience": "operator"})
    ss = tool("reset-password", **{"x-audience": "self-service"})
    assert visible_to(op_only, operator=True) and visible_to(ss, operator=True)
    assert visible_to(ss, operator=False)
    assert not visible_to(op_only, operator=False)


def test_live_filters_by_operator_flag():
    cat = LiveCatalog()
    cat.publish(
        IT,
        "a@web01",
        [
            tool("reset-password", **{"x-audience": "self-service"}),
            tool("rotate-key", **{"x-audience": "operator"}),
            tool("skill:restart", **{"x-audience": "operator", "x-kind": "skill"}),
        ],
    )
    # A visitor sees only the self-service entry; an operator sees everything.
    assert _names(cat.live(IT)) == ["reset-password"]
    assert _names(cat.live(IT, operator=True)) == [
        "reset-password",
        "rotate-key",
        "skill:restart",
    ]


def test_entry_for_spans_connected_agents():
    cat = LiveCatalog()
    cat.publish(IT, "a@web01", [tool("rotate-key", **{"x-audience": "operator"})])
    cat.set_presence(IT, "a@web01", False)  # off shift, still connected
    entry = cat.entry_for(IT, "rotate-key")
    assert entry is not None and entry["x-audience"] == "operator"
    assert cat.entry_for(IT, "nope") is None
