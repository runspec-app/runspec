#!/usr/bin/env python
"""record_evals.py — build a real toolset from installed runnables and record /
validate / replay a tool-selection eval fixture. Prototype companion to
``runspec_console.evals``.

This is the bridge-free driver a future ``bridge_evals.py`` / ``runspec-console
evals`` CLI will formalise. It builds the **exact MCP tool schemas the agent
sees** from the runnables installed in the current venv — via the same
``runspec.serve._expand_tools`` path ``runspec serve`` uses, so subcommands
flatten to leaf tools (``filesystem_read-text-file``) identically — then runs the
pure harness against them.

Four modes:

  build-toolset               Print the real toolset discovered in this venv.
  validate  <fixture.toml>    Offline: assert every case's expect_tool exists and
                              its expect_args are declared on that tool. No model,
                              no key — grounds a fixture against real runnables.
  record    <fixture.toml>    LIVE: drive a real provider adapter over each prompt,
            --out script.json capture the model's chosen tool + args (NEVER
                              executed), and write a replay script. Needs a model
                              API key (reads [llm]-style settings from the env:
                              RUNSPEC_EVAL_PROVIDER / _MODEL, plus the provider's
                              own key var, e.g. ANTHROPIC_API_KEY).
  demo      <fixture.toml>    Replay a recorded (or hand-seeded) script through the
            --script s.json   harness against the real toolset and print scores —
                              free, deterministic, no key. This is the CI path.

In this direct-tool toolset every tool *is* a runnable execution (there are no
read-only discovery meta-tools like ``search_runnables``), so ``is_terminal`` is
"always true" and the runnable identity is the tool name — supplied here as the
injected callables the harness takes. A search-mode console would inject the
``run_runnable``/``__``-leaf variants instead (the harness defaults).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

from runspec.serve import _expand_tools

from runspec_console import evals
from runspec_console.adapters.replay import (
    RecordingAdapter,
    ReplayAdapter,
    load_script,
    save_script,
)


# ── build the real toolset from installed runnables ───────────────────────────
def build_toolset() -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
    """The MCP tool schemas for every runnable installed in this venv, expanded
    exactly as ``runspec serve`` would (subcommands → leaf tools).

    Returns ``(tools, split)``. ``split`` maps each leaf tool name back to the
    ``(runnable, command)`` pair it was flattened from (``cron_add`` →
    ``("cron", "add")``; a nested path space-joined, ``""`` for a runnable with no
    subcommands) — what a console-runtime case, where the runnable is one tool and
    the verb rides a ``command`` field, actually names."""
    raw = subprocess.run(
        [sys.prefix + "/bin/runspec", "local", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tools: list[dict[str, Any]] = []
    split: dict[str, tuple[str, str]] = {}
    for entry in json.loads(raw):
        spec = entry["spec"]
        name = entry["runnable"]
        for tname, schema, _args, _exec in _expand_tools(
            name,
            spec,
            [name],
            spec.get("run_as"),
            spec.get("become_method") or "sudo",
            spec.get("become_flags"),
        ):
            tools.append(schema)
            # _expand_tools joins the runnable and each path segment with "_",
            # so stripping the runnable prefix recovers the verb path.
            verb = tname[len(name) + 1 :] if tname.startswith(name + "_") else ""
            split[tname] = (name, " ".join(verb.split("_")) if verb else "")
    return tools, split


def _tool_props(tool: dict[str, Any]) -> set[str]:
    return set((tool.get("inputSchema") or {}).get("properties", {}).keys())


# Direct-tool injections: every tool is a terminal runnable; its identity is its
# name; its args are the top-level tool input. Split so run_case_async (which
# accepts only is_terminal/resolve_readonly) and score (resolve_target/_args) each
# get the subset they take; _score_injections composes the score-side half per
# case (a case naming a subcommand via expect_command reads the leaf name split).
_DIRECT_RUN = dict(is_terminal=lambda name: True)
_DIRECT_SCORE = dict(
    resolve_target=lambda inv: inv.name,
    resolve_args=lambda inv: dict(inv.input),
)


def _score_injections(
    case: evals.EvalCase, split: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    """Score-side resolvers for one case against this flattened toolset.

    A fixture may write a subcommand call either way, and both describe the same
    decision — so read the model's leaf call in whichever spelling the case used:

      * ``expect_tool = "cron_add"`` — the leaf name, as ``runspec serve`` presents
        it. Resolve to the tool name as-is (the long-standing behaviour).
      * ``expect_tool = "cron"`` + ``expect_command = "add"`` — the console-runtime
        spelling, where the runnable is ONE tool and the verb rides a top-level
        ``command`` field. Split the leaf name back apart so the runnable and the
        verb are graded separately, as the console would grade them.

    Without the split a console-shaped case scores ``wrong_tool`` on a call that
    was in fact correct, which would make ``expect_command`` unusable in the
    shared fixture."""
    if case.expect_command is None:
        return dict(_DIRECT_SCORE)
    return dict(
        resolve_target=lambda inv: split.get(inv.name, (inv.name, ""))[0],
        resolve_command=lambda inv: split.get(inv.name, (inv.name, ""))[1],
        resolve_args=lambda inv: dict(inv.input),
    )


def _init_telemetry(args: argparse.Namespace) -> bool:
    """Turn on OTLP tracing for this run when --telemetry / --telemetry-endpoint
    is given. An explicit endpoint builds an ad-hoc [telemetry] config (no
    console config.toml needed); bare --telemetry reads the console's own config.
    Best-effort: needs the `otel` extra and a reachable endpoint, else a no-op."""
    from runspec_console import observability

    if getattr(args, "telemetry_endpoint", None):
        return observability.init_tracing(
            {
                "enabled": True,
                "endpoint": args.telemetry_endpoint,
                "capture_content": bool(getattr(args, "capture_content", False)),
                "service_name": "runspec-evals",
            }
        )
    if getattr(args, "telemetry", False):
        return observability.init_tracing()
    return False


def _shutdown_telemetry(args: argparse.Namespace) -> None:
    """Flush + shut down tracing so a short run doesn't drop batched spans."""
    if getattr(args, "telemetry", False) or getattr(args, "telemetry_endpoint", None):
        from runspec_console import observability

        observability.shutdown()


def _add_telemetry_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--telemetry",
        action="store_true",
        help="emit a turn→llm→tool trace per case (reads the console [telemetry] config)",
    )
    parser.add_argument(
        "--telemetry-endpoint",
        default=None,
        help="OTLP endpoint to trace to (ad-hoc; e.g. http://127.0.0.1:6006/v1/traces for local Phoenix)",
    )
    parser.add_argument(
        "--capture-content",
        action="store_true",
        help="attach prompt/completion/arg text to spans (off by default; secrets scrubbed)",
    )


# ── modes ─────────────────────────────────────────────────────────────────────
def cmd_build_toolset(_args: argparse.Namespace) -> int:
    tools, _split = build_toolset()
    print(f"{len(tools)} MCP tools from runnables installed in {sys.prefix}\n")
    for t in tools:
        props = ", ".join(sorted(_tool_props(t))) or "—"
        print(f"  {t['name']:40s} args: {props}")
    return 0


def _grounding_name(case: evals.EvalCase) -> str:
    """The installed tool name a case grounds against.

    This toolset is the flattened ``runspec serve`` one, where a subcommand is its
    own leaf tool (``cron_add``). A console-runtime case instead names the runnable
    and its verb separately (``expect_tool = "cron"`` + ``expect_command = "add"``),
    because in the console's dedup/search modes the runnable is ONE tool and the
    verb rides a top-level ``command`` field. Both spellings describe the same
    decision, so join them the way ``_expand_tools`` does — a nested path's spaces
    become underscores too — and ground either form against the same leaf."""
    tool = case.expect_tool or ""
    if not tool or case.expect_command is None:
        return tool
    verb = "_".join(str(case.expect_command).split())
    return f"{tool}_{verb}" if verb else tool


def cmd_validate(args: argparse.Namespace) -> int:
    """Ground a fixture against the real toolset — no model needed."""
    tools = {t["name"]: t for t in build_toolset()[0]}
    cases = evals.load_cases(args.fixture)
    ok = True
    for case in cases:
        problems: list[str] = []
        target = _grounding_name(case)
        if case.expect_no_run:
            pass  # nothing to ground — negative case
        elif not case.expect_tool:
            problems.append("no expect_tool set")
        elif target not in tools:
            problems.append(
                f"expect_tool {case.expect_tool!r} is not an installed tool"
                if case.expect_command is None
                else (
                    f"expect_tool {case.expect_tool!r} + expect_command "
                    f"{case.expect_command!r} is not an installed tool "
                    f"(looked for {target!r})"
                )
            )
        else:
            declared = _tool_props(tools[target])
            for key in {**case.expect_args}:
                if key not in declared:
                    problems.append(f"expect_args key {key!r} not declared on {target}")
        for forbidden in case.forbid_tools:
            # A forbid entry that names no real tool is a fixture typo worth flagging.
            if forbidden not in tools:
                problems.append(f"forbid_tools {forbidden!r} is not an installed tool")
        mark = "ok " if not problems else "ERR"
        print(f"[{mark}] {case.id}")
        for p in problems:
            print(f"        - {p}")
        ok = ok and not problems
    print(
        "\nfixture is grounded against real runnables" if ok else "\nFIXTURE HAS ERRORS"
    )
    return 0 if ok else 1


def _make_adapter(provider: str, model: str, system: str) -> Any:
    """Construct a live provider adapter from the environment (needs a key)."""
    from runspec_console.adapters.base import load_adapter

    kwargs: dict[str, Any] = {"system": system}
    if model:
        kwargs["model"] = model
    return load_adapter(provider, **kwargs)


def cmd_record(args: argparse.Namespace) -> int:
    """LIVE record: drive the model over each prompt, capture the chosen tool +
    args (never executed), and write a reviewable replay script."""
    import os

    provider = os.environ.get("RUNSPEC_EVAL_PROVIDER", "anthropic")
    model = os.environ.get("RUNSPEC_EVAL_MODEL", "")
    system = os.environ.get("RUNSPEC_EVAL_SYSTEM", "")
    tools, split = build_toolset()
    cases = evals.load_cases(args.fixture)

    inner = _make_adapter(provider, model, system)
    recorder = RecordingAdapter(inner)
    _init_telemetry(args)

    results: list[evals.CaseResult] = []

    async def run() -> None:
        from runspec_console import evals_trace

        for case in cases:
            traj = await evals.run_case_async(recorder, tools, case, **_DIRECT_RUN)
            result = evals.score(case, traj, **_score_injections(case, split))
            evals_trace.emit_case(case, result, model=recorder.model)
            results.append(result)

    asyncio.run(run())
    save_script(recorder.sink, args.out)
    _shutdown_telemetry(args)
    print(f"recorded {len(cases)} cases via {provider} → {args.out}")

    # Score + (optional) gate. --threshold 0 (default) just records; a positive
    # threshold makes this the CI live tier — nonzero exit when the pass rate
    # falls below it. Print a per-case + summary line either way.
    passed = 0
    by_cat: dict[str, int] = {}
    for res in results:
        mark = "PASS" if res.passed else f"FAIL/{res.category}"
        line = f"  [{mark}] {res.case_id}"
        if not res.passed:
            line += f" — {res.reason}"
            by_cat[res.category] = by_cat.get(res.category, 0) + 1
        else:
            passed += 1
        print(line)
    total = len(results)
    rate = passed / total if total else 1.0
    print(f"\n{passed}/{total} passed (pass_rate {rate:.0%})")
    if by_cat:
        print("by failing category:", by_cat)
    threshold = float(getattr(args, "threshold", 0.0) or 0.0)
    if threshold > 0.0 and rate < threshold:
        print(f"FAIL: pass rate {rate:.0%} below threshold {threshold:.0%}")
        return 1
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Replay a recorded/seeded script through the harness against the real
    toolset and print the scored report (the CI path — no key, deterministic)."""
    from runspec_console import evals_trace

    tools, split = build_toolset()
    cases = evals.load_cases(args.fixture)
    adapter = ReplayAdapter(load_script(args.script))
    _init_telemetry(args)

    results = [
        evals.evaluate_case(
            adapter, tools, case, **_DIRECT_RUN, **_score_injections(case, split)
        )
        for case in cases
    ]
    model = getattr(adapter, "model", "")
    for case, res in zip(cases, results):
        evals_trace.emit_case(case, res, model=model)
        mark = "PASS" if res.passed else f"FAIL/{res.category}"
        line = f"  [{mark}] {res.case_id}"
        if not res.passed:
            line += f" — {res.reason}"
        print(line)
    _shutdown_telemetry(args)

    passed = sum(1 for r in results if r.passed)
    by_cat: dict[str, int] = {}
    for r in results:
        if not r.passed:
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
    rate = passed / len(results) if results else 1.0
    print(f"\n{passed}/{len(results)} passed (pass_rate {rate:.0%})")
    if by_cat:
        print("by failing category:", by_cat)
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build-toolset").set_defaults(func=cmd_build_toolset)

    p_val = sub.add_parser("validate")
    p_val.add_argument("fixture")
    p_val.set_defaults(func=cmd_validate)

    p_rec = sub.add_parser("record")
    p_rec.add_argument("fixture")
    p_rec.add_argument("--out", required=True)
    p_rec.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="fail (exit 1) if the pass rate is below this fraction (0..1); 0 = record only",
    )
    _add_telemetry_flags(p_rec)
    p_rec.set_defaults(func=cmd_record)

    p_demo = sub.add_parser("demo")
    p_demo.add_argument("fixture")
    p_demo.add_argument("--script", required=True)
    _add_telemetry_flags(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
