"""
test_agent_parallel_tools.py — independent tool calls from one model response run
concurrently.

A model often asks for the same read-only runnable on five hosts in one response.
The loop used to run them one after another; now parallel-safe calls (runnable
runs the gate lets through unprompted, read-only discovery and built-ins) are
batched with ``asyncio.gather``; a gated runnable run joins the batch but asks
its approval one prompt at a time; anything else — a writing built-in, an MCP
tool — is a barrier that runs alone, in order. Results still pair with their
tool_use blocks in the model's order, and every tool_start/tool_end carries the
call's ``call_id`` so the frontend can pair concurrent same-name cards.

Drives the real ``_agentic_chat_turn`` on the same bare stub as
test_agent_continuation.py.
"""

from __future__ import annotations

import asyncio
import types

from runspec_console.adapters.base import ToolCall
from runspec_console.bridge import Bridge
from tests.session_stub import current as _S
from tests.test_agent_continuation import _chat_stub, _ScriptedAdapter


def _stub_with_events(
    parallel_safe=lambda name, _inp: name.startswith("p_"),
    parallel_confirm=lambda name, _inp: name.startswith("c_"),
    **kw,
):
    s = _chat_stub(**kw)
    s._parallel_safe_call = parallel_safe
    s._parallel_confirm_call = parallel_confirm
    s.events: list[tuple[str, dict]] = []
    base_dispatch = s._dispatch

    def dispatch(event, payload):
        if event in ("runspec:tool_start", "runspec:tool_end"):
            s.events.append((event, dict(payload)))
        base_dispatch(event, payload)

    s._dispatch = dispatch
    return s


def _run(s, calls):
    adapter = _ScriptedAdapter([("", "tool_use", calls), ("done", "end_turn")])
    asyncio.run(s._agentic_chat_turn("chat1", "inventory the fleet", adapter))
    return adapter


def _tool_results(s):
    """The tool_result contents the loop handed back to the model, in order."""
    turn = next(
        m
        for m in _S(s).history
        if m["role"] == "user" and isinstance(m["content"], list)
    )
    return [(r["tool_use_id"], r["content"]) for r in turn["content"]]


def test_parallel_safe_calls_run_concurrently_and_results_keep_model_order():
    s = _stub_with_events()
    started: list[str] = []
    all_started = asyncio.Event()

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        started.append(tool_input["host"])
        if len(started) == 3:
            all_started.set()
        # Sequential execution would deadlock here: the first call can't finish
        # until the third has started. The timeout turns that into a failure.
        await asyncio.wait_for(all_started.wait(), timeout=2)
        # Finish in reverse order to prove results are re-ordered afterwards.
        await asyncio.sleep({"a": 0.03, "b": 0.02, "c": 0.0}[tool_input["host"]])
        return f"out-{tool_input['host']}"

    s._gated_run_tool_async = gated_run
    _run(s, [ToolCall(id=f"t{h}", name="p_disk", input={"host": h}) for h in "abc"])

    assert sorted(started) == ["a", "b", "c"]
    assert _tool_results(s) == [("ta", "out-a"), ("tb", "out-b"), ("tc", "out-c")]


def test_events_carry_call_id_so_same_name_cards_pair_correctly():
    s = _stub_with_events()

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        return f"out-{tool_input['host']}"

    s._gated_run_tool_async = gated_run
    _run(s, [ToolCall(id=f"t{h}", name="p_disk", input={"host": h}) for h in "ab"])

    ends = {p["call_id"]: p["output"] for e, p in s.events if e == "runspec:tool_end"}
    starts = [p["call_id"] for e, p in s.events if e == "runspec:tool_start"]
    assert starts == ["ta", "tb"]  # starts are emitted in the model's order
    assert ends == {"ta": "out-a", "tb": "out-b"}


def test_a_gated_call_is_a_barrier_between_parallel_batches():
    s = _stub_with_events()
    log: list[str] = []

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        log.append(f"start {tool_input['n']}")
        await asyncio.sleep(0.01)
        log.append(f"end {tool_input['n']}")
        return "ok"

    s._gated_run_tool_async = gated_run
    calls = [
        ToolCall(id="1", name="p_read", input={"n": 1}),
        ToolCall(id="2", name="p_read", input={"n": 2}),
        ToolCall(id="3", name="restart_service", input={"n": 3}),  # not parallel-safe
        ToolCall(id="4", name="p_read", input={"n": 4}),
    ]
    _run(s, calls)

    # 1 and 2 overlap; 3 starts only after both finished and ends before 4 starts.
    assert log.index("start 2") < log.index("end 1")
    assert log.index("start 3") > max(log.index("end 1"), log.index("end 2"))
    assert log.index("end 3") < log.index("start 4")
    assert [r[0] for r in _tool_results(s)] == ["1", "2", "3", "4"]


def test_max_parallel_tools_one_restores_sequential_execution():
    s = _stub_with_events()
    s._config["llm"]["max_parallel_tools"] = 1
    log: list[str] = []

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        log.append(f"start {tool_input['n']}")
        await asyncio.sleep(0.01)
        log.append(f"end {tool_input['n']}")
        return "ok"

    s._gated_run_tool_async = gated_run
    _run(s, [ToolCall(id=str(n), name="p_read", input={"n": n}) for n in (1, 2)])
    assert log == ["start 1", "end 1", "start 2", "end 2"]


def test_concurrency_is_capped_by_max_parallel_tools():
    s = _stub_with_events()
    s._config["llm"]["max_parallel_tools"] = 2
    active = 0
    peak = 0

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    s._gated_run_tool_async = gated_run
    _run(s, [ToolCall(id=str(n), name="p_read", input={"n": n}) for n in range(6)])
    assert peak == 2


def test_a_raising_parallel_call_does_not_sink_its_siblings():
    s = _stub_with_events()

    async def gated_run(
        tool_name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        if tool_input["host"] == "b":
            raise RuntimeError("ssh dropped")
        return f"out-{tool_input['host']}"

    s._gated_run_tool_async = gated_run
    _run(s, [ToolCall(id=f"t{h}", name="p_disk", input={"host": h}) for h in "abc"])
    results = dict(_tool_results(s))
    assert results["ta"] == "out-a" and results["tc"] == "out-c"
    assert "failed: ssh dropped" in results["tb"]


def test_max_parallel_tools_config_is_clamped():
    s = types.SimpleNamespace(get_config=lambda: cfg)
    for raw, want in ((None, 8), (0, 1), (-3, 1), (4, 4), (500, 32), ("x", 8)):
        cfg = {"llm": {} if raw is None else {"max_parallel_tools": raw}}
        assert Bridge._max_parallel_tools(s) == want, raw


# ── the real classifier ─────────────────────────────────────────────────────


def _classifier_stub(decision="run", group=False, mcp=False):
    s = types.SimpleNamespace(
        _confirm_decision=lambda name, inp: decision,
        _group_fanout_calls=lambda inp: ("g", "x", []) if group else None,
        _mcp=object() if mcp else None,
        _is_mcp_tool=lambda name: mcp,
    )
    s._runnable_run_decision = types.MethodType(Bridge._runnable_run_decision, s)
    return s


def _classifier(decision="run", group=False, mcp=False):
    s = _classifier_stub(decision, group, mcp)
    return lambda name, inp=None: Bridge._parallel_safe_call(s, name, inp or {})


def _confirm_classifier(decision="confirm", group=False, mcp=False):
    s = _classifier_stub(decision, group, mcp)
    return lambda name, inp=None: Bridge._parallel_confirm_call(s, name, inp or {})


def test_classifier_allows_ungated_runnable_runs_and_discovery():
    safe = _classifier("run")
    assert safe("run_runnable", {"id": "disk-usage", "host": "a"})
    assert safe("linux-utils__disk-usage", {"host": "a"})  # dedup runnable tool
    assert safe("search_runnables")


def test_classifier_keeps_gated_builtin_mcp_and_fanout_calls_sequential():
    assert not _classifier("confirm")("run_runnable", {"id": "x"})
    assert not _classifier("manual")("run_runnable", {"id": "x"})
    assert not _classifier("run")("propose_fact")  # built-in: writes shared state
    assert not _classifier("run")("use_tool")
    assert not _classifier("run", mcp=True)("mcp_jira_search")
    assert not _classifier("run", group=True)("run_runnable", {"host": "little"})


def test_classifier_fails_safe_when_the_decision_raises():
    s = types.SimpleNamespace(
        _confirm_decision=lambda *_: (_ for _ in ()).throw(KeyError("gone")),
        _group_fanout_calls=lambda inp: None,
        _mcp=None,
    )
    assert Bridge._parallel_safe_call(s, "run_runnable", {"id": "x"}) is False


def test_classifier_allows_read_only_builtins_the_gate_lets_through():
    safe = _classifier("run")
    for name in (
        "fetch_url",
        "list_files",
        "search_history",
        "search_wiki",
        "workspace_read",
        "repo_grep",
        "console_status",
        "describe_tool",
    ):
        assert safe(name), name
    # Reached through the search-mode wrapper too.
    assert safe("use_tool", {"name": "repo_read", "input": {}})
    assert not safe("use_tool", {"name": "repo_write", "input": {}})
    # Writes, Outlook (COM) and the marketplace (cache rewrite) stay sequential.
    for name in (
        "workspace_write",
        "propose_fact",
        "repo_sync",
        "outlook_inbox",
        "search_marketplace",
        "save_runbook",
    ):
        assert not safe(name), name
    # An operator who gated a read-only built-in gets it asked, alone.
    assert not _classifier("confirm")("fetch_url")


def test_read_only_parallel_set_names_real_tools():
    from runspec_console.bridge_const import (
        _BUILTIN_TOOL_NAMES,
        _PARALLEL_READONLY_BUILTINS,
    )

    assert _PARALLEL_READONLY_BUILTINS - _BUILTIN_TOOL_NAMES == {"describe_tool"}


def test_confirm_classifier_admits_only_gated_single_runnable_runs():
    assert _confirm_classifier("confirm")("run_runnable", {"id": "restart"})
    assert _confirm_classifier("supervised")("ops__restart", {"host": "a"})
    assert not _confirm_classifier("run")("run_runnable", {"id": "x"})
    assert not _confirm_classifier("manual")("run_runnable", {"id": "x"})
    assert not _confirm_classifier("confirm")("outlook_send")
    assert not _confirm_classifier("confirm")("use_tool", {"name": "repo_write"})
    assert not _confirm_classifier("confirm", mcp=True)("mcp_jira_create")
    assert not _confirm_classifier("confirm", group=True)("run_runnable", {"host": "g"})


def test_approvals_are_asked_in_order_and_approved_runs_overlap():
    """Two gated runs and an ungated one in one response: the prompts come one
    at a time, in the model's order, while the ungated run and the first
    approved run go ahead without waiting for the second answer."""
    s = _stub_with_events()
    log: list[str] = []

    async def gated_run(
        tool_name,
        tool_input,
        chat_id,
        stage=None,
        on_decision=None,
        headless=False,
        confirm_lock=None,
        is_cancelled=None,
    ):
        n = tool_input["n"]
        if tool_name.startswith("c_"):
            assert confirm_lock is not None and is_cancelled is not None
            async with confirm_lock:
                log.append(f"ask {n}")
                await asyncio.sleep(0.03)  # the operator reading the card
                log.append(f"answer {n}")
        else:
            assert confirm_lock is None
        log.append(f"start {n}")
        await asyncio.sleep(0.02)
        log.append(f"end {n}")
        return f"out-{n}"

    s._gated_run_tool_async = gated_run
    calls = [
        ToolCall(id="1", name="c_restart", input={"n": 1}),
        ToolCall(id="2", name="c_restart", input={"n": 2}),
        ToolCall(id="3", name="p_read", input={"n": 3}),
    ]
    _run(s, calls)

    # Prompts never overlap and follow the model's order.
    assert log.index("answer 1") < log.index("ask 2")
    # The ungated read runs while the first prompt is still up.
    assert log.index("start 3") < log.index("answer 1")
    # The first approved run overlaps the second prompt.
    assert log.index("start 1") < log.index("answer 2")
    assert _tool_results(s) == [("1", "out-1"), ("2", "out-2"), ("3", "out-3")]


def test_gate_skips_the_prompt_when_stopped_while_waiting_its_turn():
    asked: list[str] = []
    s = types.SimpleNamespace(
        _confirm_decision=lambda name, inp: "confirm",
        _confirm_card_view=lambda name, inp: (name, inp),
        _register_confirm=lambda *a: asked.append("asked"),
    )
    out = asyncio.run(
        Bridge._gated_run_tool_async(
            s,
            "ops__restart",
            {"host": "a"},
            "chat1",
            confirm_lock=asyncio.Lock(),
            is_cancelled=lambda: True,
        )
    )
    assert asked == []
    assert "was not run" in out and "stopped" in out
