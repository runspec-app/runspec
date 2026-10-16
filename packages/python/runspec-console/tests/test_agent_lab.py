"""test_agent_lab.py — the agent lab's pure parts (lab/agent_lab.py, lab/fakes).

The lab itself drives a real Bridge in its own venv; these keep its scripted
model, metering maths and fake payloads honest without that setup.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent / "lab"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lab = _load("agent_lab", LAB / "agent_lab.py")
jira = _load("runspec_lab_fakes_jira", LAB / "fakes" / "runspec_lab_fakes" / "jira.py")


def _drain(model, messages, tools=()):
    async def go():
        return [item async for item in model.stream_with_tools(messages, list(tools))]

    return asyncio.run(go())[-1][1]


def test_common_prefix():
    assert lab._common_prefix("abcdef", "abcxyz") == 3
    assert lab._common_prefix("", "abc") == 0
    assert lab._common_prefix("same", "same") == 4


def test_scripted_model_steps_then_greps_a_spill_then_answers():
    m = lab.ScriptedModel()
    tools = [{"name": "fake-jira-issue"}, {"name": "workspace_grep"}]
    turn = {
        "prompt": "status?",
        "steps": [{"runnable": "fake-jira-issue", "args": {"key": "OPS-1"}}],
        "need": ["To Do"],
    }
    m.begin(turn)
    first = _drain(m, [{"role": "user", "content": "status?"}], tools)
    assert first.tool_calls[0].name == "fake-jira-issue"
    spilled = [
        {"role": "user", "content": "status?"},
        {"role": "user", "content": "[spilled to workspace: tool-results/x.json]"},
    ]
    grep = _drain(m, spilled)
    assert grep.tool_calls[0].name == "workspace_grep"
    assert grep.tool_calls[0].input["pattern"] == "To Do"
    found = spilled + [{"role": "user", "content": '"status": "To Do"'}]
    assert _drain(m, found).text == "ANSWERED"


def test_scripted_model_in_search_mode_describes_once_then_runs():
    m = lab.ScriptedModel()
    tools = [{"name": n} for n in ("describe_runnable", "run_runnable")]
    step = {"runnable": "fake-jira-issue", "args": {"key": "OPS-1"}}
    m.begin({"prompt": "q", "steps": [step, step], "need": []})
    msgs = [{"role": "user", "content": "q"}]
    calls = [_drain(m, msgs, tools).tool_calls[0] for _ in range(3)]
    assert [c.name for c in calls] == [
        "describe_runnable",
        "run_runnable",
        "run_runnable",
    ]
    assert calls[1].input == {
        "id": "fake-jira-issue",
        "host": "local",
        "args": {"key": "OPS-1"},
    }
    m.new_chat()
    m.begin({"prompt": "q", "steps": [step], "need": []})
    assert _drain(m, msgs, tools).tool_calls[0].name == "describe_runnable"


def test_scripted_model_ignores_earlier_turns():
    m = lab.ScriptedModel()
    m.begin({"prompt": "second", "steps": [], "need": ["Done"]})
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "Done"},
        {"role": "user", "content": "second"},
    ]
    assert _drain(m, msgs).text == "MISSING: Done"


def test_summarise_prices_cache_reads_and_writes():
    call = {"tools_chars": 400, "messages_chars": 400, "cached_chars": 0}
    again = {"tools_chars": 400, "messages_chars": 800, "cached_chars": 800}
    run = {
        "variant": "v",
        "scenario": "s",
        "turns": [{"passed": True, "calls": [call, again], "usage": {}}],
    }
    row = lab.summarise(run)
    assert row["calls"] == 2
    assert row["input_tok"] == (800 + 1200) // 4
    # call 1: all written (800 * 1.25); call 2: 800 read (0.1) + 400 written
    assert row["input_tok_cached"] == round((1000 + 80 + 500) / 4)
    assert row["history_tok"] == 200


def test_fake_jira_compact_is_small_and_keeps_the_answer():
    raw = jira.raw_issue("OPS-101", 12)
    compact = jira.compact_issue(raw, set())
    assert len(json.dumps(compact)) * 10 < len(json.dumps(raw))
    assert compact["status"] == raw["fields"]["status"]["name"]
    assert compact["comment_count"] == 12 and len(compact["comments"]) == 3
    assert jira.compact_issue(raw, {"all"}) == raw


def test_scenarios_and_variants_parse():
    scenarios = lab._load(LAB / "scenarios.toml", "scenario")
    variants = lab._load(LAB / "variants.toml", "variant")
    assert scenarios and variants
    assert all(t.get("need") and t.get("expect") for s in scenarios for t in s["turn"])
    assert {v["name"] for v in variants} >= {"baseline", "compact"}
