"""
test_autonomy_gate.py — the agent (chat) path must honour runnable autonomy.

Binds the real gate methods onto a light stub (no Bridge.__init__ side effects).
_run_tool_sync and _dispatch are stubbed so nothing actually executes or touches
a UI; we assert on the gate decision and the confirm round-trip.
"""

from __future__ import annotations

import asyncio
import threading
import time

from runspec_console.bridge import Bridge
from runspec_console.bridge_autonomy import _command_leaf_node, _command_path


class _StubBridge:
    def _confirm_timeout_s(self, headless=False):
        return 5  # short so a "no answer" path doesn't hang the suite

    def __init__(self, runnables, run_result="RAN"):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._pending_confirms = {}
        self._pending_confirm_meta = {}
        self._chat_cancels = {}
        self._in_flight = {}
        self._dispatched = []
        self._run_result = run_result

    def _dispatch(self, event, detail):
        self._dispatched.append((event, detail))

    def _notify_confirm(self, *a, **k):
        # Desktop toast is orthogonal to the gate logic under test; no-op here.
        pass

    def _run_tool_sync(self, name, tool_input, chat_id=""):
        return self._run_result

    def get_config(self):
        return {}  # default tool_mode = "dedup"

    def _visible_runnables(self, surface):
        # Tool-visibility filtering is orthogonal to the gate under test.
        return self._runnables_cache

    _tool_mode = Bridge._tool_mode
    _sanitize_tool_name = staticmethod(Bridge._sanitize_tool_name)
    _dedup_tool_name = Bridge._dedup_tool_name
    _dedup_tool_aliases = Bridge._dedup_tool_aliases
    _declared_arg_name_set = staticmethod(Bridge._declared_arg_name_set)
    _host_selector_key = Bridge._host_selector_key
    _resolve_tool_target = Bridge._resolve_tool_target
    _fallback_for_host = Bridge._fallback_for_host
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    # run_runnable resolution path
    _runnables_for_id = Bridge._runnables_for_id
    _resolve_id_host = Bridge._resolve_id_host
    _runbook_id_hint = staticmethod(Bridge._runbook_id_hint)
    _coerce_run_args = staticmethod(Bridge._coerce_run_args)
    _confirm_decision = Bridge._confirm_decision
    _confirm_card_view = Bridge._confirm_card_view
    # Pre-gate command/arg validation for run_runnable (search mode).
    _group_fanout_calls = Bridge._group_fanout_calls
    _preflight_run_runnable = Bridge._preflight_run_runnable
    _preflight_validate = Bridge._preflight_validate
    _node_at = staticmethod(Bridge._node_at)
    _register_confirm = Bridge._register_confirm
    _finish_confirm = Bridge._finish_confirm
    get_pending_confirms = Bridge.get_pending_confirms
    _gated_run_tool = Bridge._gated_run_tool
    _gated_run_tool_async = Bridge._gated_run_tool_async
    _await_confirmation = Bridge._await_confirmation
    resolve_tool_confirmation = Bridge.resolve_tool_confirmation
    cancel_chat = Bridge.cancel_chat


def _runnable(autonomy="confirm", args=None):
    return [
        {"host": "local", "name": "flush-dns", "autonomy": autonomy, "args": args or []}
    ]


# ── effective autonomy ────────────────────────────────────────────────────────


def test_effective_autonomy_base_level():
    b = _StubBridge(_runnable("confirm"))
    assert b._effective_tool_autonomy("local__flush-dns", {}) == "confirm"


def test_effective_autonomy_unknown_runnable_defaults_confirm():
    b = _StubBridge([])
    assert b._effective_tool_autonomy("local__nope", {}) == "confirm"


def test_effective_autonomy_arg_escalates():
    args = [{"name": "force", "autonomy": "manual"}]
    b = _StubBridge(_runnable("confirm", args))
    # arg not provided → base level
    assert b._effective_tool_autonomy("local__flush-dns", {}) == "confirm"
    # arg provided → escalates to the more restrictive per-arg level
    assert b._effective_tool_autonomy("local__flush-dns", {"force": True}) == "manual"


# ── per-subcommand autonomy (the gate must read the invoked leaf) ──────────────
#
# Mirrors the discovery cache shape (discovery._spec_to_runnables): a top-level
# runnable with its own autonomy + global args, and a `commands` map whose leaf
# nodes carry their OWN autonomy and effective args (inherited globals + own).


def _cmd(autonomy, args=None, commands=None):
    node = {"name": "verb", "autonomy": autonomy, "args": args or []}
    if commands:
        node["commands"] = commands
    return node


def _runnable_with_commands(parent_autonomy="confirm", commands=None, args=None):
    return [
        {
            "host": "local",
            "name": "svc",
            "autonomy": parent_autonomy,
            "args": args or [],
            "commands": commands or {},
        }
    ]


def test_subcommand_autonomy_replaces_parent_looser():
    # Parent confirm, subcommand autonomous → the leaf wins (over-gating fix:
    # the console must NOT prompt on a verb the author marked safe).
    b = _StubBridge(_runnable_with_commands("confirm", {"list": _cmd("autonomous")}))
    assert b._effective_tool_autonomy("local__svc", {"command": "list"}) == "autonomous"


def test_subcommand_autonomy_replaces_parent_stricter():
    # Parent autonomous, subcommand confirm → the leaf wins (under-gating fix:
    # the console MUST prompt even though the parent is autonomous).
    b = _StubBridge(_runnable_with_commands("autonomous", {"add": _cmd("confirm")}))
    assert b._effective_tool_autonomy("local__svc", {"command": "add"}) == "confirm"


def test_subcommand_manual_from_autonomous_parent():
    b = _StubBridge(_runnable_with_commands("autonomous", {"wipe": _cmd("manual")}))
    assert b._effective_tool_autonomy("local__svc", {"command": "wipe"}) == "manual"


def test_no_command_uses_top_level_level():
    # A runnable with subcommands, invoked without a command → top-level level
    # (unchanged behaviour; core require-command surfaces the real error later).
    b = _StubBridge(_runnable_with_commands("autonomous", {"add": _cmd("confirm")}))
    assert b._effective_tool_autonomy("local__svc", {}) == "autonomous"


def test_subcommand_scoped_arg_escalates_only_when_supplied():
    cmds = {"add": _cmd("autonomous", args=[{"name": "force", "autonomy": "manual"}])}
    b = _StubBridge(_runnable_with_commands("confirm", cmds))
    # command chosen, escalating arg absent → the leaf's own level
    assert b._effective_tool_autonomy("local__svc", {"command": "add"}) == "autonomous"
    # escalating arg supplied → most-restrictive wins
    assert (
        b._effective_tool_autonomy("local__svc", {"command": "add", "force": True})
        == "manual"
    )


def test_nested_command_path_resolves_deep_leaf():
    inner = {"endpoint": _cmd("manual")}
    cmds = {"orders": _cmd("confirm", commands=inner)}
    b = _StubBridge(_runnable_with_commands("autonomous", cmds))
    assert (
        b._effective_tool_autonomy("local__svc", {"command": ["orders", "endpoint"]})
        == "manual"
    )


def test_unknown_command_segment_falls_back_to_last_valid_node():
    # A bad segment stops the walk at the last valid node (here the top node) —
    # never crashes, stays gated at the parent level.
    b = _StubBridge(_runnable_with_commands("confirm", {"add": _cmd("autonomous")}))
    assert b._effective_tool_autonomy("local__svc", {"command": "nope"}) == "confirm"


def test_run_runnable_reads_subcommand_from_envelope():
    # Search mode: the run_runnable schema carries `command` as a TOP-LEVEL
    # envelope field (sibling to `args`), not inside `args`. The gate must read it
    # from the envelope and resolve the leaf — otherwise it falls back to the
    # top-level runnable's level (the search-mode over/under-gating bug).
    b = _StubBridge(_runnable_with_commands("autonomous", {"add": _cmd("confirm")}))
    # parent autonomous, leaf 'add' confirm → must gate (under-gating guard)
    ti = {"id": "local__svc", "host": "local", "command": "add", "args": {}}
    assert b._effective_tool_autonomy("run_runnable", ti) == "confirm"


def test_run_runnable_autonomous_leaf_not_over_gated():
    # parent confirm, leaf 'list' autonomous → must NOT prompt (the reported
    # symptom: an autonomous subcommand still confirming in search mode).
    b = _StubBridge(_runnable_with_commands("confirm", {"list": _cmd("autonomous")}))
    ti = {"id": "local__svc", "host": "local", "command": "list", "args": {}}
    assert b._effective_tool_autonomy("run_runnable", ti) == "autonomous"


def test_run_runnable_subcommand_arg_escalates_from_envelope():
    # The escalating arg lives in `args`; the command lives on the envelope.
    cmds = {"add": _cmd("autonomous", args=[{"name": "force", "autonomy": "manual"}])}
    b = _StubBridge(_runnable_with_commands("confirm", cmds))
    base = {"id": "local__svc", "host": "local", "command": "add"}
    assert (
        b._effective_tool_autonomy("run_runnable", {**base, "args": {}}) == "autonomous"
    )
    assert (
        b._effective_tool_autonomy("run_runnable", {**base, "args": {"force": True}})
        == "manual"
    )


# ── leaf-resolution helpers (pure) ────────────────────────────────────────────


def test_command_path_normalisation():
    assert _command_path({}) == []
    assert _command_path({"command": ""}) == []
    assert _command_path({"command": "add"}) == ["add"]
    assert _command_path({"command": ["a", "b"]}) == ["a", "b"]
    assert _command_path({"command": ["a", "", "b"]}) == ["a", "b"]


def test_command_leaf_node_walks_and_falls_back():
    root = {
        "autonomy": "confirm",
        "commands": {"a": {"autonomy": "autonomous", "commands": {"b": {"x": 1}}}},
    }
    assert _command_leaf_node(root, [])["autonomy"] == "confirm"  # empty → root
    assert _command_leaf_node(root, ["a"])["autonomy"] == "autonomous"
    assert _command_leaf_node(root, ["a", "b"]) == {"x": 1}
    # bad segment → last valid node (root here)
    assert _command_leaf_node(root, ["nope"])["autonomy"] == "confirm"
    assert _command_leaf_node({}, ["a"]) == {}


# ── gate decisions ────────────────────────────────────────────────────────────


def test_autonomous_runs_without_prompt():
    b = _StubBridge(_runnable("autonomous"), run_result="RAN")
    out = b._gated_run_tool("local__flush-dns", {}, "chat1")
    assert out == "RAN"
    assert b._dispatched == []  # no confirmation event


def test_manual_is_hard_blocked():
    b = _StubBridge(_runnable("manual"), run_result="RAN")
    out = b._gated_run_tool("local__flush-dns", {}, "chat1")
    assert "manual-only" in out
    assert out != "RAN"  # never executed
    assert b._dispatched == []  # not even a prompt — hard block


def _drive(b, tool_input, decision):
    """Run _gated_run_tool in a thread, answer the confirmation with `decision`."""
    result = {}
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "out", b._gated_run_tool("local__flush-dns", tool_input, "chat1")
        )
    )
    t.start()
    for _ in range(400):  # wait for the confirm event to be dispatched
        if b._dispatched:
            break
        time.sleep(0.005)
    assert b._dispatched, "expected a tool_confirm dispatch"
    event, detail = b._dispatched[0]
    assert event == "runspec:tool_confirm"
    b.resolve_tool_confirmation(detail["request_id"], decision)
    t.join(timeout=5)
    return result["out"], detail


def test_confirm_approve_runs():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out, detail = _drive(b, {}, True)
    assert out == "RAN"
    assert detail["autonomy"] == "confirm"


def test_confirm_deny_does_not_run():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out, _ = _drive(b, {}, False)
    assert "declined" in out
    assert out != "RAN"


def test_supervised_also_prompts():
    b = _StubBridge(_runnable("supervised"), run_result="RAN")
    out, detail = _drive(b, {}, True)
    assert out == "RAN"
    assert detail["autonomy"] == "supervised"


def test_resolve_unknown_id_is_noop():
    b = _StubBridge(_runnable())
    b.resolve_tool_confirmation("does-not-exist", True)  # must not raise


# ── pre-gate command/arg validation (search-mode run_runnable) ─────────────────


def _subcmd_runnable(autonomy="confirm"):
    return [
        {
            "host": "local",
            "name": "svc",
            "autonomy": autonomy,
            "args": [],
            "commands": {"restart": {"args": []}, "status": {"args": []}},
        }
    ]


def test_preflight_run_runnable_flags_unknown_command():
    b = _StubBridge(_subcmd_runnable())
    err = b._preflight_run_runnable({"id": "svc", "command": "bogus"})
    assert err and "unknown command 'bogus'" in err


def test_preflight_run_runnable_passes_valid_command():
    b = _StubBridge(_subcmd_runnable())
    assert b._preflight_run_runnable({"id": "svc", "command": "restart"}) is None


def test_run_runnable_unknown_command_bounces_before_confirm():
    # A confirm-autonomy subcommand runnable invoked with an unknown verb must be
    # rejected in-turn, BEFORE the operator is asked to approve a call that can
    # only error — and without registering or dispatching any confirmation.
    b = _StubBridge(_subcmd_runnable("confirm"))
    out = b._gated_run_tool("run_runnable", {"id": "svc", "command": "bogus"}, "chat1")
    assert "unknown command 'bogus'" in out
    assert "restart" in out and "status" in out
    assert b._pending_confirms == {}  # no confirm was registered
    assert b._dispatched == []  # nothing was shown to the operator


# ── async gate (the path the agentic loop actually uses) ───────────────────────
#
# The async gate dispatches the prompt itself (from the event-loop thread, which
# is the fix for the silent-timeout bug) and offloads only the blocking wait.


def _run_async_gate(b, tool_input, decision):
    """Drive _gated_run_tool_async, answering from a watcher thread."""

    def answer():
        for _ in range(400):
            if b._dispatched:
                break
            time.sleep(0.005)
        assert b._dispatched, "expected a tool_confirm dispatch"
        event, detail = b._dispatched[0]
        assert event == "runspec:tool_confirm"
        b.resolve_tool_confirmation(detail["request_id"], decision)

    async def main():
        watcher = threading.Thread(target=answer)
        watcher.start()
        out = await b._gated_run_tool_async("local__flush-dns", tool_input, "chat1")
        watcher.join(timeout=5)
        return out

    return asyncio.run(main())


def test_async_autonomous_runs_without_prompt():
    b = _StubBridge(_runnable("autonomous"), run_result="RAN")
    out = asyncio.run(b._gated_run_tool_async("local__flush-dns", {}, "chat1"))
    assert out == "RAN"
    assert b._dispatched == []


def test_async_manual_is_hard_blocked():
    b = _StubBridge(_runnable("manual"), run_result="RAN")
    out = asyncio.run(b._gated_run_tool_async("local__flush-dns", {}, "chat1"))
    assert "manual-only" in out
    assert b._dispatched == []


def test_async_confirm_approve_runs():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out = _run_async_gate(b, {}, True)
    assert out.endswith("RAN")  # tool ran; a [gate: …] note is prefixed


def test_async_confirm_deny_does_not_run():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out = _run_async_gate(b, {}, False)
    assert "declined" in out
    assert out != "RAN"


def test_async_approved_output_carries_gate_note_for_agent():
    # The agent must know the call was human-gated: an approved gated result is
    # prefixed with a [gate: …] note. The real tool output still follows.
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out = _run_async_gate(b, {}, True)
    assert out.startswith("[gate: operator approved this confirm-gated call]")
    assert out.endswith("RAN")


def test_async_autonomous_output_has_no_gate_note():
    # An ungated (autonomous) call carries no gate note — nothing to disclose.
    b = _StubBridge(_runnable("autonomous"), run_result="RAN")
    out = asyncio.run(b._gated_run_tool_async("local__flush-dns", {}, "chat1"))
    assert out == "RAN"


def test_async_denied_output_has_no_gate_prefix():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out = _run_async_gate(b, {}, False)
    assert not out.startswith("[gate:")  # denial has its own wording, not the note


def _run_async_gate_with_stage(b, decision):
    """Drive the async gate with a stage callback, returning (output, stages)."""
    stages: list[str] = []

    def answer():
        for _ in range(400):
            if b._dispatched:
                break
            time.sleep(0.005)
        assert b._dispatched, "expected a tool_confirm dispatch"
        _, detail = b._dispatched[0]
        b.resolve_tool_confirmation(detail["request_id"], decision)

    async def main():
        watcher = threading.Thread(target=answer)
        watcher.start()
        out = await b._gated_run_tool_async(
            "local__flush-dns", {}, "chat1", stages.append
        )
        watcher.join(timeout=5)
        return out

    return asyncio.run(main()), stages


def test_async_stage_running_on_approve():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out, stages = _run_async_gate_with_stage(b, True)
    assert out.endswith("RAN")  # tool ran; a [gate: …] note is prefixed
    assert stages == ["awaiting_human", "running"]


def test_async_stage_running_on_deny():
    # A denied tool must still leave 'awaiting_human' for 'running' — the turn
    # continues (the agent reasons about the denial), so the card mustn't look
    # stuck waiting on a human who already answered.
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out, stages = _run_async_gate_with_stage(b, False)
    assert "declined" in out
    assert stages == ["awaiting_human", "running"]


def test_async_cancel_during_pending_confirm_unblocks_and_refuses():
    """Stop pressed while the Approve/Deny dialog is up must wake the gate
    promptly (not wait out the 5-min timeout) and refuse the tool."""
    b = _StubBridge(_runnable("confirm"), run_result="RAN")

    def stop():
        for _ in range(400):
            if b._dispatched:
                break
            time.sleep(0.005)
        assert b._dispatched, "expected a tool_confirm dispatch"
        b.cancel_chat("chat1")  # user hits Stop instead of answering

    async def main():
        watcher = threading.Thread(target=stop)
        watcher.start()
        out = await b._gated_run_tool_async("local__flush-dns", {}, "chat1")
        watcher.join(timeout=5)
        return out

    start = time.monotonic()
    out = asyncio.run(main())
    elapsed = time.monotonic() - start

    assert out != "RAN"  # tool never executed
    assert "stopped" in out.lower()
    assert elapsed < 2  # woke immediately, didn't sit on the confirm timeout
    assert not b._pending_confirms  # entry cleaned up


# ── get_pending_confirms (work-inbox cold-start snapshot) ─────────────────────


def test_get_pending_confirms_lists_parked_gates_oldest_first():
    b = _StubBridge(_runnable("confirm"))
    r1, _, _ = b._register_confirm(
        "chat1", "local__flush-dns", {"force": True}, "confirm"
    )
    r2, _, _ = b._register_confirm("card9", "local__restart", {}, "supervised")

    rows = b.get_pending_confirms()
    assert [r["request_id"] for r in rows] == [r1, r2]  # oldest first
    first = rows[0]
    # Mirrors the runspec:tool_confirm event shape the live path emits.
    assert first["id"] == "chat1"
    assert first["tool_name"] == "local__flush-dns"
    assert first["tool_input"] == {"force": True}
    assert first["autonomy"] == "confirm"
    assert first["confirm_verb"] == "run"  # a runnable executes → "run"
    assert "created_at" in first


def test_confirm_verb_follows_the_resolved_tool():
    """The confirm card's verb reflects what approving actually does — "save" for
    a runbook store, "run" for a runnable — so the button never claims a
    save_runbook will *run* the runbook."""
    b = _StubBridge(_runnable("confirm"))
    b._register_confirm(
        "chat1", "save_runbook", {"title": "T", "steps": "1."}, "confirm"
    )
    b._register_confirm("chat2", "local__flush-dns", {}, "confirm")
    verbs = {m["tool_name"]: m["confirm_verb"] for m in b.get_pending_confirms()}
    assert verbs == {"save_runbook": "save", "local__flush-dns": "run"}
    # The event payload carries it too (the live path the UI listens on).
    dispatched = {d["tool_name"]: d.get("confirm_verb") for _e, d in b._dispatched}
    assert dispatched["save_runbook"] == "save"


def test_get_pending_confirms_drops_finished_gate():
    b = _StubBridge(_runnable("confirm"))
    rid, _, holder = b._register_confirm("chat1", "local__flush-dns", {}, "confirm")
    assert len(b.get_pending_confirms()) == 1
    b._finish_confirm(rid, holder, timed_out=False)
    assert b.get_pending_confirms() == []  # leaves the snapshot when answered


def test_finish_confirm_dispatches_resolved_event():
    """Every finalized gate emits runspec:confirm_resolved with its owning
    chat/card id + request_id — the signal the UI needs to clear a stale card
    (esp. a backend timeout the frontend was never told about)."""
    b = _StubBridge(_runnable("confirm"))
    rid, _, holder = b._register_confirm("card-7", "local__flush-dns", {}, "confirm")
    b._finish_confirm(rid, holder, timed_out=True)  # e.g. a timeout
    resolved = [d for ev, d in b._dispatched if ev == "runspec:confirm_resolved"]
    assert resolved == [{"id": "card-7", "request_id": rid}]


# ── editable confirmations ────────────────────────────────────────────────────


def _drive_edit(b, tool_input, decision, edited_input=None):
    """Like _drive but resolves with an operator-edited tool_input, and records
    the input _run_tool_sync actually received."""
    seen: dict = {}
    b._run_tool_sync = lambda name, ti, chat_id="": seen.update(ti) or b._run_result
    result = {}
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "out", b._gated_run_tool("local__flush-dns", tool_input, "chat1")
        )
    )
    t.start()
    for _ in range(400):
        if b._dispatched:
            break
        time.sleep(0.005)
    assert b._dispatched, "expected a tool_confirm dispatch"
    _event, detail = b._dispatched[0]
    b.resolve_tool_confirmation(detail["request_id"], decision, edited_input)
    t.join(timeout=5)
    return result["out"], seen


def test_finish_confirm_returns_approved_and_edit():
    b = _StubBridge(_runnable("confirm"))
    rid, _, holder = b._register_confirm("c", "local__flush-dns", {"a": "1"}, "confirm")
    holder["approved"] = True
    holder["edited_input"] = {"a": "2"}
    approved, edited = b._finish_confirm(rid, holder, timed_out=False)
    assert approved is True and edited == {"a": "2"}


def test_edited_input_used_for_execution():
    b = _StubBridge(_runnable("confirm"))
    out, seen = _drive_edit(b, {"a": "1"}, True, edited_input={"a": "2", "b": "9"})
    assert out == "RAN"
    assert seen == {"a": "2", "b": "9"}  # the edit, not the original


def test_no_edit_runs_original_input():
    b = _StubBridge(_runnable("confirm"))
    out, seen = _drive_edit(b, {"a": "1"}, True, edited_input=None)
    assert out == "RAN"
    assert seen == {"a": "1"}


def test_edit_ignored_on_deny():
    b = _StubBridge(_runnable("confirm"))
    out, seen = _drive_edit(b, {"a": "1"}, False, edited_input={"a": "2"})
    assert "declined" in out
    assert seen == {}  # never executed, edit discarded


# ── run_saved_run interception (gate reflects the underlying runnable) ────────


class _SavedRunStub(_StubBridge):
    """Extends the gate stub with the saved-run expansion + grouped-confirm
    machinery, so run_saved_run gates on the preset's underlying runnable."""

    _saved_run_gate_calls = Bridge._saved_run_gate_calls
    _saved_run_entry = Bridge._saved_run_entry
    _saved_run_calls = Bridge._saved_run_calls
    _grouped_confirm_run_sync = Bridge._grouped_confirm_run_sync
    _group_calls_level = Bridge._group_calls_level
    _apply_group_edit = staticmethod(Bridge._apply_group_edit)
    _run_calls_parallel = Bridge._run_calls_parallel
    _format_group_output = staticmethod(Bridge._format_group_output)
    _pick_connected_host = Bridge._pick_connected_host

    def __init__(self, runnables, run_result="RAN", groups=None):
        super().__init__(runnables, run_result)
        self._groups = groups or {}
        self._connected_cache = {}

    def _group_members(self, g):
        return list(self._groups.get(g, []))

    def _host_has_runnable(self, h, r):
        return any(
            x.get("host") == h and x.get("name") == r for x in self._runnables_cache
        )

    def _host_entry(self, h):
        return {"name": h, "ssh": "user@" + h}


def _seed_saved_runs(tmp_path, monkeypatch, rows):
    from runspec_console import savedruns

    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path",
        lambda: tmp_path / "runspec_saved_runs.toml",
    )
    savedruns.save_saved_runs(rows)


def test_saved_run_autonomous_runs_without_prompt(tmp_path, monkeypatch):
    _seed_saved_runs(
        tmp_path,
        monkeypatch,
        [{"id": "flush", "runnable": "flush-dns", "host": "local"}],
    )
    b = _SavedRunStub(_runnable("autonomous"), run_result="RAN")
    out = b._gated_run_tool("run_saved_run", {"id": "flush"}, "chat1")
    assert "RAN" in out
    assert "local" in out  # per-host block header
    assert b._dispatched == []  # no confirmation event


def test_saved_run_manual_runnable_blocks_whole_preset(tmp_path, monkeypatch):
    _seed_saved_runs(
        tmp_path,
        monkeypatch,
        [{"id": "flush", "runnable": "flush-dns", "host": "local"}],
    )
    b = _SavedRunStub(_runnable("manual"), run_result="RAN")
    out = b._gated_run_tool("run_saved_run", {"id": "flush"}, "chat1")
    assert "manual-only" in out
    assert "RAN" not in out
    assert b._dispatched == []


def test_saved_run_confirm_prompts_and_runs_on_approve(tmp_path, monkeypatch):
    _seed_saved_runs(
        tmp_path,
        monkeypatch,
        [{"id": "flush", "runnable": "flush-dns", "host": "local"}],
    )
    b = _SavedRunStub(_runnable("confirm"), run_result="RAN")
    holder = {}

    def work():
        holder["out"] = b._gated_run_tool("run_saved_run", {"id": "flush"}, "chat1")

    t = threading.Thread(target=work)
    t.start()
    deadline = time.time() + 3
    while not b._pending_confirms and time.time() < deadline:
        time.sleep(0.01)
    (request_id,) = list(b._pending_confirms)
    b.resolve_tool_confirmation(request_id, True)
    t.join(timeout=5)
    assert "RAN" in holder["out"]
    # The gate registered a run_group confirm (the grouped-confirm card).
    assert any(ev == "runspec:tool_confirm" for ev, _ in b._dispatched) or b._dispatched


def test_saved_run_group_fans_out_and_runs(tmp_path, monkeypatch):
    _seed_saved_runs(
        tmp_path,
        monkeypatch,
        [{"id": "flush", "runnable": "flush-dns", "group": "Prod"}],
    )
    runnables = [
        {"host": "w1", "name": "flush-dns", "autonomy": "autonomous", "args": []},
        {"host": "w2", "name": "flush-dns", "autonomy": "autonomous", "args": []},
    ]
    b = _SavedRunStub(runnables, run_result="RAN", groups={"Prod": ["w1", "w2"]})
    out = b._gated_run_tool("run_saved_run", {"id": "flush"}, "chat1")
    assert "w1" in out and "w2" in out
    assert b._dispatched == []  # autonomous on every member — no prompt


def test_saved_run_unknown_id_errors_before_any_gate(tmp_path, monkeypatch):
    _seed_saved_runs(tmp_path, monkeypatch, [])
    b = _SavedRunStub(_runnable("confirm"))
    out = b._gated_run_tool("run_saved_run", {"id": "nope"}, "chat1")
    assert "no saved run" in out
    assert b._dispatched == []
