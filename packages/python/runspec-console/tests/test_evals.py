"""
test_evals.py — the pure tool-selection eval harness (``evals.py``) and the
record/replay adapters (``adapters/replay.py``).

The harness drives a ``ModelAdapter`` over a fixed prompt + the real toolset and
captures the model's chosen runnable + args **without executing** them, then
scores that plan against a declared expectation. These tests use the
``ReplayAdapter`` (scripted, deterministic, no network) as the model, so they
prove the driver, the scorer, the search-mode trajectory, the validator reuse,
and a record→replay round-trip — no disk, no live model, no bridge.
"""

from __future__ import annotations

import json
import os

from runspec_console import evals
from runspec_console.adapters.base import ModelAdapter
from runspec_console.adapters.replay import (
    RecordingAdapter,
    ReplayAdapter,
    load_script,
    save_script,
)


# A tiny toolset stand-in (the harness only forwards it to the adapter; the
# ReplayAdapter ignores it, so its exact shape doesn't matter for these tests).
TOOLS = [{"name": "run_runnable"}, {"name": "search_runnables"}]


def _run_call(name, args=None, host=None, **extra):
    """A scripted terminal `run_runnable` call carrying a nested args dict."""
    inp = {"name": name, "args": dict(args or {})}
    if host is not None:
        inp["host"] = host
    inp.update(extra)
    return {"name": "run_runnable", "input": inp}


# ── single-shot selection ─────────────────────────────────────────────────────
def test_positive_case_passes_on_correct_tool_and_args():
    script = {
        "c1": [
            {
                "tool_calls": [
                    _run_call("restart-service", {"name": "nginx"}, host="web-01")
                ]
            }
        ]
    }
    adapter = ReplayAdapter(script)
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx on web-01",
        expect_tool="restart-service",
        expect_args={"name": "nginx"},
        expect_host="web-01",
    )
    res = evals.evaluate_case(adapter, TOOLS, case)
    assert res.passed
    assert res.category == evals.CAT_PASS
    assert res.terminal is not None and res.terminal.name == "run_runnable"


def test_wrong_tool_fails_with_category():
    script = {"c1": [{"tool_calls": [_run_call("stop-service", {"name": "nginx"})]}]}
    case = evals.EvalCase(
        id="c1", prompt="restart nginx", expect_tool="restart-service"
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_WRONG_TOOL
    assert "restart-service" in res.reason and "stop-service" in res.reason


def test_expect_command_passes_and_is_dropped_from_args():
    # A subcommand rides a top-level `command` field (sibling of `args`); it is
    # graded by expect_command and NOT treated as an unknown runnable arg.
    script = {
        "c1": [
            {
                "tool_calls": [
                    _run_call(
                        "cron", {"schedule": "@daily"}, host="web-01", command="add"
                    )
                ]
            }
        ]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="add a daily cron on web-01",
        expect_tool="cron",
        expect_command="add",
        expect_host="web-01",
        expect_args={"schedule": "@daily"},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed
    assert res.command == "add"


def test_run_runnable_id_is_stripped_to_the_bare_runnable_name():
    # A search-mode run_runnable id is a search_runnables row id — the dedup tool
    # name (`group__name`). default_resolve_target must strip that qualifier so a
    # case's bare `expect_tool` matches.
    script = {
        "c1": [
            {
                "tool_calls": [
                    {
                        "name": "run_runnable",
                        "input": {"id": "local__restart-service", "args": {}},
                    }
                ]
            }
        ]
    }
    case = evals.EvalCase(id="c1", prompt="restart", expect_tool="restart-service")
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed
    assert res.target == "restart-service"


def test_wrong_command_fails_with_category():
    script = {"c1": [{"tool_calls": [_run_call("cron", command="remove")]}]}
    case = evals.EvalCase(
        id="c1", prompt="add a cron", expect_tool="cron", expect_command="add"
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_WRONG_COMMAND
    assert res.command == "remove"


def test_nested_command_path_compares_space_joined():
    # A nested subcommand path arrives as a list; it compares space-joined.
    script = {"c1": [{"tool_calls": [_run_call("git", command=["remote", "add"])]}]}
    case = evals.EvalCase(
        id="c1",
        prompt="add a git remote",
        expect_tool="git",
        expect_command="remote add",
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed
    assert res.command == "remote add"


def test_expect_autonomy_needs_a_resolver_then_grades():
    script = {"c1": [{"tool_calls": [_run_call("restart-service", {"name": "nginx"})]}]}
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx",
        expect_tool="restart-service",
        expect_autonomy="confirm",
    )
    # No resolver: autonomy can't be proven, so it is recorded as "" and never
    # fails the case (a bare harness has no gate to consult).
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed and res.autonomy == ""
    # With a resolver returning the expected level: graded and stamped.
    res2 = evals.evaluate_case(
        ReplayAdapter(script), TOOLS, case, resolve_autonomy=lambda inv: "confirm"
    )
    assert res2.passed and res2.autonomy == "confirm"
    # A mismatch is a wrong_autonomy failure.
    res3 = evals.evaluate_case(
        ReplayAdapter(script), TOOLS, case, resolve_autonomy=lambda inv: "autonomous"
    )
    assert not res3.passed and res3.category == evals.CAT_WRONG_AUTONOMY


def test_wrong_host_fails():
    script = {
        "c1": [
            {
                "tool_calls": [
                    _run_call("restart-service", {"name": "nginx"}, host="db-01")
                ]
            }
        ]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx on web-01",
        expect_tool="restart-service",
        expect_host="web-01",
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_WRONG_HOST


def test_wrong_args_reports_missing_and_mismatch():
    script = {
        "c1": [{"tool_calls": [_run_call("restart-service", {"name": "apache"})]}]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx now",
        expect_tool="restart-service",
        expect_args={"name": "nginx", "force": True},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_WRONG_ARGS
    assert "name=" in res.reason  # value mismatch
    assert "force missing" in res.reason  # absent expected arg


def test_extra_optional_args_are_allowed():
    # The model passes an extra optional arg the case doesn't mention — subset
    # matching means this still passes.
    script = {
        "c1": [
            {
                "tool_calls": [
                    _run_call("restart-service", {"name": "nginx", "timeout": 30})
                ]
            }
        ]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx",
        expect_tool="restart-service",
        expect_args={"name": "nginx"},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed


def test_value_matching_tolerates_str_int_and_case():
    script = {
        "c1": [{"tool_calls": [_run_call("scale", {"replicas": "3", "env": "PROD"})]}]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="scale to 3 in prod",
        expect_tool="scale",
        expect_args={"replicas": 3, "env": "prod"},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed


# ── negative / clarify cases ──────────────────────────────────────────────────
def test_expect_no_run_passes_when_model_asks_a_question():
    script = {"c1": [{"text": "Which host did you mean?", "tool_calls": []}]}
    case = evals.EvalCase(id="c1", prompt="delete everything", expect_no_run=True)
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed
    assert res.final_text == "Which host did you mean?"


def test_expect_no_run_fails_when_model_acts():
    script = {"c1": [{"tool_calls": [_run_call("delete-all", {})]}]}
    case = evals.EvalCase(id="c1", prompt="delete everything", expect_no_run=True)
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_UNEXPECTED_RUN


def test_positive_case_fails_when_model_runs_nothing():
    script = {"c1": [{"text": "Sure, here's info.", "tool_calls": []}]}
    case = evals.EvalCase(
        id="c1", prompt="restart nginx", expect_tool="restart-service"
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_NO_RUN


def test_forbidden_tool():
    script = {"c1": [{"tool_calls": [_run_call("rm-rf", {})]}]}
    case = evals.EvalCase(
        id="c1", prompt="clean up logs", forbid_tools=["rm-rf"], expect_tool="rm-rf"
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert not res.passed
    assert res.category == evals.CAT_FORBIDDEN_TOOL


# ── search-mode trajectory (discovery runs, terminal is captured) ─────────────
def test_search_mode_trajectory_captures_terminal_after_discovery():
    # Step 0: the model searches (read-only, resolved by the harness stub).
    # Step 1: it runs the runnable — captured, never executed.
    script = {
        "c1": [
            {
                "tool_calls": [
                    {"name": "search_runnables", "input": {"query": "service"}}
                ]
            },
            {"tool_calls": [_run_call("restart-service", {"name": "nginx"})]},
        ]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx",
        expect_tool="restart-service",
        expect_args={"name": "nginx"},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed
    assert [d.name for d in res.discovery] == ["search_runnables"]


def test_resolve_readonly_is_fed_back_into_the_conversation():
    seen = {}

    def resolver(name, tool_input):
        seen[name] = tool_input
        return "restart-service — restart a systemd unit"

    script = {
        "c1": [
            {"tool_calls": [{"name": "search_runnables", "input": {"query": "svc"}}]},
            {"tool_calls": [_run_call("restart-service", {"name": "nginx"})]},
        ]
    }
    case = evals.EvalCase(
        id="c1", prompt="restart nginx", expect_tool="restart-service"
    )
    res = evals.evaluate_case(
        ReplayAdapter(script), TOOLS, case, resolve_readonly=resolver
    )
    assert res.passed
    assert seen == {"search_runnables": {"query": "svc"}}


# ── validator reuse (improve.classify_bounce taxonomy) ────────────────────────
def test_validate_hook_buckets_a_bounce():
    # Simulate bridge_search._preflight_validate returning an error string.
    def validate(runnable, args):
        if "name" not in args:
            return "Error: missing required argument(s): name."
        return None

    script = {"c1": [{"tool_calls": [_run_call("restart-service", {})]}]}
    case = evals.EvalCase(id="c1", prompt="restart it", expect_tool="restart-service")
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case, validate=validate)
    assert not res.passed
    # bucketed via improve.classify_bounce → "missing_required"
    assert res.category == "missing_required"


def test_validate_clean_passes():
    def validate(runnable, args):
        return None

    script = {"c1": [{"tool_calls": [_run_call("restart-service", {"name": "nginx"})]}]}
    case = evals.EvalCase(
        id="c1",
        prompt="restart nginx",
        expect_tool="restart-service",
        expect_args={"name": "nginx"},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case, validate=validate)
    assert res.passed
    assert "validate-clean" in res.reason


# ── direct-mode terminal naming (a leaf tool, no run_runnable wrapper) ─────────
def test_direct_mode_leaf_tool_is_terminal_and_target_is_the_name():
    # In direct mode the runnable IS the tool; a subcommand leaf carries "__".
    script = {
        "c1": [{"tool_calls": [{"name": "cron__add", "input": {"schedule": "@daily"}}]}]
    }
    case = evals.EvalCase(
        id="c1",
        prompt="add a daily cron",
        expect_tool="cron__add",
        expect_args={"schedule": "@daily"},
    )
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case)
    assert res.passed


# ── suite report ──────────────────────────────────────────────────────────────
def test_run_suite_aggregates_and_counts_categories():
    script = {
        "ok": [{"tool_calls": [_run_call("restart-service", {"name": "nginx"})]}],
        "bad": [{"tool_calls": [_run_call("stop-service", {"name": "nginx"})]}],
    }
    cases = [
        evals.EvalCase(id="ok", prompt="restart nginx", expect_tool="restart-service"),
        evals.EvalCase(id="bad", prompt="restart nginx", expect_tool="restart-service"),
    ]
    report = evals.run_suite(ReplayAdapter(script), TOOLS, cases)
    assert report["total"] == 2
    assert report["passed"] == 1
    assert report["failed"] == 1
    assert report["by_category"] == {evals.CAT_WRONG_TOOL: 1}


def test_sampling_reports_pass_rate():
    script = {"c1": [{"tool_calls": [_run_call("restart-service", {"name": "nginx"})]}]}
    case = evals.EvalCase(
        id="c1", prompt="restart nginx", expect_tool="restart-service"
    )
    # Replay is deterministic, so every sample passes → pass_rate 1.0.
    res = evals.evaluate_case(ReplayAdapter(script), TOOLS, case, samples=5)
    assert res.samples == 5 and res.pass_count == 5
    assert res.pass_rate == 1.0


# ── fixture loading ───────────────────────────────────────────────────────────
def test_cases_from_data_parses_table_array():
    data = {
        "eval": [
            {
                "id": "restart",
                "prompt": "restart nginx on web-01",
                "expect_tool": "restart-service",
                "expect_host": "web-01",
                "expect_args": {"name": "nginx"},
            },
            {"id": "ambiguous", "prompt": "delete it", "expect_no_run": True},
        ]
    }
    cases = evals.cases_from_data(data)
    assert [c.id for c in cases] == ["restart", "ambiguous"]
    assert cases[0].expect_args == {"name": "nginx"}
    assert cases[1].expect_no_run is True


def test_load_cases_reads_the_example_fixture():
    path = os.path.join(
        os.path.dirname(__file__), "fixtures", "agent_evals.example.toml"
    )
    cases = evals.load_cases(path)
    by_id = {c.id: c for c in cases}
    assert "restart-nginx-targeted" in by_id
    # int coercion survives TOML load, and expect_no_run / forbid_tools parse.
    assert by_id["scale-with-coercion"].expect_args["replicas"] == 3
    assert by_id["ambiguous-destructive-should-clarify"].expect_no_run is True
    assert by_id["never-wipe-on-cleanup"].forbid_tools == ["rm-rf", "format-disk"]


# ── record → replay round trip ────────────────────────────────────────────────
class _ScriptedInner(ModelAdapter):
    """A stand-in for a *real* provider adapter: returns canned responses per
    case/step, so RecordingAdapter has something to record."""

    def __init__(self, script):
        self._replay = ReplayAdapter(script)
        self.system = ""
        self.model = "scripted-inner"

    def begin(self, case_id):
        self._replay.begin(case_id)

    async def chat(self, messages, tools):
        return await self._replay.chat(messages, tools)

    async def stream_chat(self, messages, tools):
        async for tok in self._replay.stream_chat(messages, tools):
            yield tok

    async def stream_with_tools(self, messages, tools):
        async for ev in self._replay.stream_with_tools(messages, tools):
            yield ev

    def make_tool_turn(self, response, results):
        return self._replay.make_tool_turn(response, results)


def test_record_then_replay_produces_identical_results(tmp_path):
    source = {
        "c1": [
            {"tool_calls": [{"name": "search_runnables", "input": {"query": "svc"}}]},
            {"tool_calls": [_run_call("restart-service", {"name": "nginx"})]},
        ],
        "c2": [{"text": "Which host?", "tool_calls": []}],
    }
    cases = [
        evals.EvalCase(
            id="c1",
            prompt="restart nginx",
            expect_tool="restart-service",
            expect_args={"name": "nginx"},
        ),
        evals.EvalCase(id="c2", prompt="delete it", expect_no_run=True),
    ]

    # 1. Record: wrap the "real" adapter, run the suite, capture the script.
    recorder = RecordingAdapter(_ScriptedInner(source))
    live = evals.run_suite(recorder, TOOLS, cases)
    assert live["passed"] == 2

    # 2. Persist + reload the fixture (the reviewable artifact).
    path = tmp_path / "script.json"
    save_script(recorder.sink, str(path))
    reloaded = load_script(str(path))

    # 3. Replay: no inner adapter, no network — identical verdicts.
    replayed = evals.run_suite(ReplayAdapter(reloaded), TOOLS, cases)
    assert replayed["passed"] == 2
    assert replayed["by_category"] == live["by_category"]

    # The captured script round-trips through JSON unchanged.
    assert json.loads(json.dumps(recorder.sink)) == reloaded
