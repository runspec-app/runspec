"""evals.py — pure tool-selection eval harness (prototype).

Given a fixed prompt and the agent's *real* toolset, drive a ``ModelAdapter``
for a single turn (or a short trajectory) and capture which runnable the model
chose and with what arguments — **without executing anything** — then score that
plan against a declared expectation. This is the proactive, CI-facing complement
to :mod:`runspec_console.improve`, which mines the same failure taxonomy
*reactively* from production conversations + run history.

The interception is trivial because the agent loop already separates *deciding*
from *executing*: ``adapter.stream_with_tools`` yields ``("done", ChatResponse)``
whose ``.tool_calls`` are the model's chosen tool + args, and the console only
runs them later via ``_gated_run_tool_async``. This harness reads ``.tool_calls``
and simply never calls the gate.

**Pure and dependency-free** (stdlib only, plus the pure
``improve.classify_bounce`` taxonomy): every function takes already-built inputs
(an adapter, a tools list, case dicts) and returns plain JSON-serialisable data,
so it is unit-testable without a disk, a live model, a host, or the Bridge — the
same discipline as ``memory.py`` and the ``*-core`` packages. The console side (a
future ``bridge_evals.py``) supplies the real toolset (``_runnables_to_tools``),
the configured adapter + system prompt (``_get_adapter``), the read-only tool
resolver, and the preflight validator (``bridge_search._preflight_validate``).

Everything console-specific is injected as a small set of callables with sensible
defaults, so this module never imports the Bridge or hard-codes tool-naming
conventions:

* ``is_terminal(name)``    — is this tool call the terminal *action* (run a
  runnable) rather than read-only discovery? Default mirrors
  ``improve._is_run_attempt`` (``run_runnable`` / ``use_tool`` / a ``__`` leaf).
* ``resolve_readonly(name, input)`` — stubbed output for a discovery tool so a
  search-mode trajectory can continue. Default returns an empty result.
* ``resolve_target(inv)`` — the runnable identity a terminal call invokes
  (``run_runnable`` carries it in ``input['name']``; a direct leaf tool *is* the
  name). Matched against ``expect_tool``.
* ``resolve_args(inv)``   — the runnable args a terminal call carries
  (``run_runnable`` nests them under ``input['args']``; a direct tool passes them
  at top level). Matched against ``expect_args`` / handed to ``validate``.
* ``validate(runnable, args)`` — returns an ``"Error: …"`` string or ``None``;
  wire it to ``bridge_search._preflight_validate`` to assert the model's args
  would not *bounce*. Optional.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from .improve import classify_bounce

# ── outcome categories ────────────────────────────────────────────────────────
# Structural eval outcomes (the harness's own verdicts). Argument *bounce*
# categories reuse improve.classify_bounce (unknown_arg / missing_required /
# bad_choice / out_of_range / bad_command / other) so a failing eval buckets the
# same way a production failure does — one vocabulary across proactive + reactive.
CAT_PASS = "pass"
CAT_NO_RUN = "no_run"  # expected a runnable call, model produced none
CAT_UNEXPECTED_RUN = "unexpected_run"  # expected no run, model ran one
CAT_WRONG_TOOL = "wrong_tool"
CAT_WRONG_HOST = "wrong_host"
CAT_WRONG_ARGS = "wrong_args"  # expected arg missing / value mismatch
CAT_FORBIDDEN_TOOL = "forbidden_tool"


# ── captured plan + case + result ─────────────────────────────────────────────
@dataclass
class ToolInvocation:
    """One tool call the model emitted (never executed by the harness)."""

    name: str
    input: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "input": self.input}


@dataclass
class Trajectory:
    """What one dry run of a case produced: the captured terminal action (or
    ``None`` if the model ran nothing), any read-only discovery calls that
    preceded it, and the model's final free text."""

    terminal: ToolInvocation | None
    discovery: list[ToolInvocation] = field(default_factory=list)
    final_text: str = ""
    steps: int = 0


@dataclass
class EvalCase:
    """A declared expectation for a single prompt. Positive cases assert the
    model runs ``expect_tool`` with ``expect_args`` (a *subset* — extra optional
    args are allowed); negative cases set ``expect_no_run`` to assert the model
    clarifies / answers instead of acting (protects the "clarify-don't-guess"
    behaviour)."""

    id: str
    prompt: str
    expect_tool: str | None = None
    expect_args: dict[str, Any] = field(default_factory=dict)
    expect_host: str | None = None
    expect_no_run: bool = False
    forbid_tools: list[str] = field(default_factory=list)
    system: str | None = None  # per-case system-prompt override (optional)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalCase":
        return cls(
            id=str(data.get("id") or data.get("prompt") or ""),
            prompt=str(data.get("prompt") or ""),
            expect_tool=data.get("expect_tool"),
            expect_args=dict(data.get("expect_args") or {}),
            expect_host=data.get("expect_host"),
            expect_no_run=bool(data.get("expect_no_run", False)),
            forbid_tools=list(data.get("forbid_tools") or []),
            system=data.get("system"),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    category: str
    reason: str
    terminal: ToolInvocation | None = None
    # The runnable identity the terminal call resolved to (``resolve_target`` of
    # the terminal), e.g. ``run_runnable``'s ``input['name']`` — "" when no run.
    target: str = ""
    discovery: list[ToolInvocation] = field(default_factory=list)
    final_text: str = ""
    # Populated by the sampled runner: how many of ``samples`` runs passed.
    samples: int = 1
    pass_count: int = 1

    @property
    def pass_rate(self) -> float:
        return self.pass_count / self.samples if self.samples else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "category": self.category,
            "reason": self.reason,
            "terminal": self.terminal.to_dict() if self.terminal else None,
            "target": self.target,
            "discovery": [d.to_dict() for d in self.discovery],
            "final_text": self.final_text,
            "samples": self.samples,
            "pass_count": self.pass_count,
            "pass_rate": self.pass_rate,
        }


# ── injected-callable defaults (override from the bridge) ──────────────────────
def default_is_terminal(name: str) -> bool:
    """A tool call that *executes a runnable* (vs read-only discovery). Mirrors
    ``improve._is_run_attempt`` and adds the ``use_tool`` search-mode wrapper: the
    search-mode executor is ``run_runnable``, a subcommand leaf carries ``__``,
    and ``use_tool`` runs a deferred built-in."""
    return name in ("run_runnable", "use_tool") or "__" in name


def default_resolve_readonly(name: str, tool_input: dict[str, Any]) -> str:
    """Stubbed output for a read-only discovery tool so a trajectory can proceed.
    A live bridge wrapper would run the real (autonomous, non-mutating) tool
    here; the prototype returns a neutral empty result."""
    return "(no results)"


def default_resolve_target(inv: ToolInvocation) -> str:
    """The runnable identity a terminal call invokes. ``run_runnable`` / ``use_tool``
    carry it in the input; a direct tool call *is* the runnable."""
    if inv.name in ("run_runnable", "use_tool"):
        return str(
            inv.input.get("name")
            or inv.input.get("runnable")
            or inv.input.get("tool")
            or inv.name
        )
    return inv.name


def default_resolve_args(inv: ToolInvocation) -> dict[str, Any]:
    """The runnable args a terminal call carries. ``run_runnable`` / ``use_tool``
    nest them under ``input['args']``; a direct tool passes them at top level."""
    if inv.name in ("run_runnable", "use_tool"):
        args = inv.input.get("args")
        return dict(args) if isinstance(args, dict) else {}
    return dict(inv.input)


IsTerminal = Callable[[str], bool]
ResolveReadonly = Callable[[str, dict[str, Any]], str]
ResolveTarget = Callable[[ToolInvocation], str]
ResolveArgs = Callable[[ToolInvocation], dict[str, Any]]
Validate = Callable[[str, dict[str, Any]], "str | None"]


# ── the dry-run driver ────────────────────────────────────────────────────────
async def _one_turn(
    adapter: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, Any]:
    """Drive one ``stream_with_tools`` call; return (assistant_text, ChatResponse)."""
    parts: list[str] = []
    response: Any = None
    async for kind, payload in adapter.stream_with_tools(messages, tools):
        if kind == "text":
            parts.append(payload)
        elif kind == "done":
            response = payload
    return "".join(parts), response


async def run_case_async(
    adapter: Any,
    tools: list[dict[str, Any]],
    case: EvalCase,
    *,
    is_terminal: IsTerminal = default_is_terminal,
    resolve_readonly: ResolveReadonly = default_resolve_readonly,
    max_steps: int = 6,
) -> Trajectory:
    """Run the model over ``case.prompt`` and the real ``tools``, letting read-only
    discovery tools resolve (stubbed) but **capturing the first terminal action
    without executing it**. Stops at the terminal call, when the model produces no
    tool call, or after ``max_steps`` discovery rounds."""
    if case.system is not None and hasattr(adapter, "system"):
        adapter.system = case.system
    if hasattr(adapter, "begin"):
        adapter.begin(case.id)

    messages: list[dict[str, Any]] = [{"role": "user", "content": case.prompt}]
    discovery: list[ToolInvocation] = []
    final_text = ""
    terminal: ToolInvocation | None = None
    steps = 0

    for steps in range(1, max_steps + 1):
        text, response = await _one_turn(adapter, messages, tools)
        if text:
            final_text = text
        if response is None or not response.tool_calls:
            break

        results: list[tuple[Any, str]] = []
        hit_terminal = False
        for tc in response.tool_calls:
            inv = ToolInvocation(tc.name, dict(tc.input or {}))
            if is_terminal(tc.name):
                terminal = inv  # captured — NOT executed
                hit_terminal = True
                break
            discovery.append(inv)
            results.append((tc, resolve_readonly(tc.name, inv.input)))
        if hit_terminal:
            break
        # Feed stubbed discovery results back and let the model take another step.
        messages = messages + adapter.make_tool_turn(response, results)

    return Trajectory(
        terminal=terminal, discovery=discovery, final_text=final_text, steps=steps
    )


def _value_matches(got: Any, expected: Any) -> bool:
    """Compare an observed arg value against an expectation. Strings compare
    case-insensitively after strip; everything else compares by value with a
    str() fallback (a model may send ``"3"`` where the case says ``3``)."""
    if isinstance(expected, str) and isinstance(got, str):
        return got.strip().lower() == expected.strip().lower()
    if got == expected:
        return True
    return str(got) == str(expected)


def score(
    case: EvalCase,
    traj: Trajectory,
    *,
    resolve_target: ResolveTarget = default_resolve_target,
    resolve_args: ResolveArgs = default_resolve_args,
    validate: Validate | None = None,
) -> CaseResult:
    """Grade a captured trajectory against the case's expectation. Assertions run
    cheapest-first: no-run → tool name → forbidden → host → arg subset →
    (optional) preflight validation. A validation failure is bucketed via
    ``improve.classify_bounce`` so it matches the production taxonomy."""

    # Resolve the runnable identity once (empty when the model ran nothing), and
    # stamp it on every result so reports / telemetry can show observed_tool.
    resolved = resolve_target(traj.terminal) if traj.terminal is not None else ""

    def result(passed: bool, category: str, reason: str) -> CaseResult:
        return CaseResult(
            case_id=case.id,
            passed=passed,
            category=category,
            reason=reason,
            terminal=traj.terminal,
            target=resolved,
            discovery=traj.discovery,
            final_text=traj.final_text,
        )

    # Negative case: the model must NOT run a runnable (clarify / answer instead).
    if case.expect_no_run:
        if traj.terminal is not None:
            return result(
                False,
                CAT_UNEXPECTED_RUN,
                f"expected no runnable to be invoked, but {resolved!r} was called",
            )
        return result(True, CAT_PASS, "no runnable invoked, as expected")

    # Positive case: a terminal action is required.
    if traj.terminal is None:
        return result(
            False, CAT_NO_RUN, "expected a runnable call but the model produced none"
        )

    target = resolved

    if case.forbid_tools and target in case.forbid_tools:
        return result(False, CAT_FORBIDDEN_TOOL, f"called forbidden tool {target!r}")

    if case.expect_tool and target != case.expect_tool:
        return result(
            False,
            CAT_WRONG_TOOL,
            f"expected tool {case.expect_tool!r}, got {target!r}",
        )

    if case.expect_host is not None:
        got_host = traj.terminal.input.get("host")
        if got_host != case.expect_host:
            return result(
                False,
                CAT_WRONG_HOST,
                f"expected host {case.expect_host!r}, got {got_host!r}",
            )

    got_args = resolve_args(traj.terminal)
    problems: list[str] = []
    for key, expected in case.expect_args.items():
        if key not in got_args:
            problems.append(f"{key} missing")
        elif not _value_matches(got_args[key], expected):
            problems.append(f"{key}={got_args[key]!r} != {expected!r}")
    if problems:
        return result(False, CAT_WRONG_ARGS, "; ".join(problems))

    # Optional: assert the model's args would not bounce from the validator.
    if validate is not None:
        err = validate(target, got_args)
        if err:
            return result(False, classify_bounce(err), err)

    detail = "tool + args as expected"
    if validate is not None:
        detail += " and validate-clean"
    return result(True, CAT_PASS, detail)


async def evaluate_case_async(
    adapter: Any,
    tools: list[dict[str, Any]],
    case: EvalCase,
    *,
    is_terminal: IsTerminal = default_is_terminal,
    resolve_readonly: ResolveReadonly = default_resolve_readonly,
    resolve_target: ResolveTarget = default_resolve_target,
    resolve_args: ResolveArgs = default_resolve_args,
    validate: Validate | None = None,
    max_steps: int = 6,
    samples: int = 1,
) -> CaseResult:
    """Run + score one case ``samples`` times (LLMs are stochastic — a suite
    gates on a pass-rate threshold, not a single run). Returns the last run's
    detail with ``samples``/``pass_count`` aggregated; a representative *failing*
    run is preferred as the returned detail so the failure is visible."""
    last: CaseResult | None = None
    first_fail: CaseResult | None = None
    passes = 0
    for _ in range(max(1, samples)):
        traj = await run_case_async(
            adapter,
            tools,
            case,
            is_terminal=is_terminal,
            resolve_readonly=resolve_readonly,
            max_steps=max_steps,
        )
        res = score(
            case,
            traj,
            resolve_target=resolve_target,
            resolve_args=resolve_args,
            validate=validate,
        )
        last = res
        if res.passed:
            passes += 1
        elif first_fail is None:
            first_fail = res
    chosen = first_fail or last
    assert chosen is not None
    chosen.samples = max(1, samples)
    chosen.pass_count = passes
    return chosen


def evaluate_case(
    adapter: Any, tools: list[dict[str, Any]], case: EvalCase, **kwargs: Any
) -> CaseResult:
    """Synchronous convenience wrapper around :func:`evaluate_case_async` (for
    tests / a CLI). The async bridge loop should call the async form directly."""
    return asyncio.run(evaluate_case_async(adapter, tools, case, **kwargs))


async def run_suite_async(
    adapter: Any,
    tools: list[dict[str, Any]],
    cases: list[EvalCase],
    *,
    threshold: float = 1.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run every case and build a JSON-serialisable report. A case *passes the
    suite* when its ``pass_rate >= threshold`` (default 1.0 — every sample must
    pass). ``by_category`` counts failing categories for prioritisation, mirroring
    the Improve report."""
    results = [
        await evaluate_case_async(adapter, tools, case, **kwargs) for case in cases
    ]
    passed = [r for r in results if r.pass_rate >= threshold]
    by_category: dict[str, int] = {}
    for r in results:
        if r.pass_rate < threshold:
            by_category[r.category] = by_category.get(r.category, 0) + 1
    total = len(results)
    return {
        "total": total,
        "passed": len(passed),
        "failed": total - len(passed),
        "pass_rate": (len(passed) / total) if total else 1.0,
        "threshold": threshold,
        "by_category": by_category,
        "cases": [r.to_dict() for r in results],
    }


def run_suite(
    adapter: Any, tools: list[dict[str, Any]], cases: list[EvalCase], **kwargs: Any
) -> dict[str, Any]:
    """Synchronous convenience wrapper around :func:`run_suite_async`."""
    return asyncio.run(run_suite_async(adapter, tools, cases, **kwargs))


# ── fixture loading ───────────────────────────────────────────────────────────
def cases_from_data(data: dict[str, Any]) -> list[EvalCase]:
    """Parse a ``[[eval]]`` table array (already read from TOML) into cases."""
    return [EvalCase.from_dict(d) for d in (data.get("eval") or [])]


def load_cases(path: str) -> list[EvalCase]:
    """Load ``[[eval]]`` cases from a TOML fixture file (stdlib ``tomllib``)."""
    import tomllib

    with open(path, "rb") as fh:
        return cases_from_data(tomllib.load(fh))
