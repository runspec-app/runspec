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
CAT_WRONG_COMMAND = "wrong_command"  # right runnable, wrong subcommand/verb
CAT_WRONG_HOST = "wrong_host"
CAT_WRONG_ARGS = "wrong_args"  # expected arg missing / value mismatch
CAT_WRONG_AUTONOMY = "wrong_autonomy"  # would gate at a different level than expected
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
    # The subcommand/verb the model must pick (a runnable with subcommands rides
    # its chosen verb in a top-level ``command`` field, not in ``args``). A nested
    # path compares space-joined (e.g. ``"remote add"``). ``None`` = don't assert.
    expect_command: str | None = None
    expect_args: dict[str, Any] = field(default_factory=dict)
    expect_host: str | None = None
    # The autonomy level the chosen call must resolve to — ``"autonomous"`` /
    # ``"confirm"`` (a confirm *would* be raised) / ``"manual"`` (the gate *would*
    # refuse the agent). Resolved deterministically from the same code the live
    # gate uses, without executing. ``None`` = don't assert. Requires the
    # ``resolve_autonomy`` injectable (the bridge binds it to
    # ``_effective_tool_autonomy``); a bare harness with no resolver can't grade it.
    expect_autonomy: str | None = None
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
            expect_command=data.get("expect_command"),
            expect_args=dict(data.get("expect_args") or {}),
            expect_host=data.get("expect_host"),
            expect_autonomy=data.get("expect_autonomy"),
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
    # The subcommand/verb the terminal call chose (space-joined for a nested
    # path), "" when the runnable has no subcommand or none was chosen.
    command: str = ""
    # The autonomy the terminal call would gate at ("autonomous"/"confirm"/
    # "manual"), "" when no run or no resolver was supplied.
    autonomy: str = ""
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
            "command": self.command,
            "autonomy": self.autonomy,
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


def _bare_runnable(name: str) -> str:
    """Strip a ``group__`` / ``host__`` qualifier to the bare runnable name — the
    last ``__`` segment. A search-mode ``run_runnable`` id comes from a
    ``search_runnables`` row, which is the *dedup tool name* (``group__name``), and
    a dedup/per_host tool is ``group__name`` / ``host__name``; ``expect_tool`` is
    always the bare name, so both must normalise the same way. A name with no
    ``__`` is returned unchanged."""
    return name.rsplit("__", 1)[-1]


def default_resolve_target(inv: ToolInvocation) -> str:
    """The runnable identity a terminal call invokes. ``run_runnable`` carries it
    in ``id`` (the search-mode tool's required key — a ``search_runnables`` row id,
    which is ``group__name``), ``use_tool`` the inner tool in ``name``; a direct
    tool call *is* the runnable (``group__name`` / ``host__name``). All are
    normalised to the bare runnable name so ``expect_tool`` matches across modes."""
    if inv.name == "run_runnable":
        return _bare_runnable(
            str(
                inv.input.get("id")
                or inv.input.get("runnable")
                or inv.input.get("name")
                or inv.name
            )
        )
    if inv.name == "use_tool":
        return str(inv.input.get("name") or inv.input.get("tool") or inv.name)
    # A direct tool call: the tool name IS the runnable identity, used verbatim —
    # this is the record_evals leaf-tool grounding (`filesystem_read-text-file`,
    # `cron__add`), where `expect_tool` is that exact leaf name. The console's own
    # dedup/per_host stripping is done in bridge_evals.resolve_target instead.
    return inv.name


def _command_str(chosen: Any) -> str:
    """Normalise a chosen subcommand to a comparable string: a list (a nested
    path) → space-joined, a plain string → itself, anything else → "". Mirrors
    ``bridge_autonomy._command_path`` (which yields the list form)."""
    if isinstance(chosen, list):
        return " ".join(str(c) for c in chosen if str(c))
    return str(chosen) if isinstance(chosen, str) else ""


def default_resolve_command(inv: ToolInvocation) -> str:
    """The subcommand/verb a terminal call chose. In *every* tool mode the verb
    rides a top-level ``command`` field (a sibling of ``args`` on ``run_runnable``;
    a top-level key on a direct dedup/per_host tool) — never inside ``args`` — so
    read it from ``inv.input`` directly. "" when there is no subcommand."""
    return _command_str(inv.input.get("command"))


def default_resolve_args(inv: ToolInvocation) -> dict[str, Any]:
    """The runnable args a terminal call carries. ``run_runnable`` / ``use_tool``
    nest them under ``input['args']``; a direct tool passes them at top level.
    ``command`` is scored separately (``resolve_command``), so it's dropped here."""
    if inv.name in ("run_runnable", "use_tool"):
        args = inv.input.get("args")
        return dict(args) if isinstance(args, dict) else {}
    return {k: v for k, v in inv.input.items() if k != "command"}


IsTerminal = Callable[[str], bool]
ResolveReadonly = Callable[[str, dict[str, Any]], str]
ResolveTarget = Callable[[ToolInvocation], str]
ResolveCommand = Callable[[ToolInvocation], str]
ResolveArgs = Callable[[ToolInvocation], dict[str, Any]]
ResolveAutonomy = Callable[[ToolInvocation], str]
# validate(runnable, command, args) -> "Error: …" | None. ``command`` is the
# chosen subcommand (space-joined for a nested path, "" for none) — the preflight
# validates a subcommand's own arg set at that node and honours require-command,
# so it MUST receive the command, not just the args.
Validate = Callable[[str, str, dict[str, Any]], "str | None"]


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
    resolve_command: ResolveCommand = default_resolve_command,
    resolve_args: ResolveArgs = default_resolve_args,
    resolve_autonomy: ResolveAutonomy | None = None,
    validate: Validate | None = None,
) -> CaseResult:
    """Grade a captured trajectory against the case's expectation. Assertions run
    cheapest-first: no-run → tool name → forbidden → subcommand → host → arg
    subset → autonomy → (optional) preflight validation. A validation failure is
    bucketed via ``improve.classify_bounce`` so it matches the production taxonomy.

    ``expect_command`` / ``expect_autonomy`` are only checked when the case sets
    them; ``expect_autonomy`` additionally needs ``resolve_autonomy`` (the bridge
    binds it to ``_effective_tool_autonomy``) — with no resolver, autonomy is
    recorded as "" and never fails a case."""

    # Resolve the runnable identity, chosen subcommand, and would-be autonomy once
    # (all empty when the model ran nothing), and stamp them on every result so
    # reports / telemetry can show the observed tool + verb + gate level.
    resolved = resolve_target(traj.terminal) if traj.terminal is not None else ""
    resolved_command = (
        resolve_command(traj.terminal) if traj.terminal is not None else ""
    )
    resolved_autonomy = ""
    if traj.terminal is not None and resolve_autonomy is not None:
        try:
            resolved_autonomy = resolve_autonomy(traj.terminal) or ""
        except Exception:  # noqa: BLE001 — autonomy resolution is best-effort
            resolved_autonomy = ""

    def result(passed: bool, category: str, reason: str) -> CaseResult:
        return CaseResult(
            case_id=case.id,
            passed=passed,
            category=category,
            reason=reason,
            terminal=traj.terminal,
            target=resolved,
            command=resolved_command,
            autonomy=resolved_autonomy,
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

    if case.expect_command is not None:
        want = _command_str(case.expect_command)
        if resolved_command.strip().lower() != want.strip().lower():
            return result(
                False,
                CAT_WRONG_COMMAND,
                f"expected subcommand {want!r}, got {resolved_command!r}",
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

    # Autonomy: prove the call would gate at the expected level — a "confirm"
    # would raise the operator prompt, a "manual" would be refused to the agent.
    # Resolved deterministically (no execution); only asserted when the case sets
    # it AND a resolver was supplied.
    if case.expect_autonomy is not None and resolve_autonomy is not None:
        want = case.expect_autonomy.strip().lower()
        if resolved_autonomy.strip().lower() != want:
            return result(
                False,
                CAT_WRONG_AUTONOMY,
                f"expected autonomy {want!r}, got {resolved_autonomy!r}",
            )

    # Optional: assert the model's args would not bounce from the validator. Pass
    # the chosen subcommand so require-command / per-command arg sets validate
    # against the invoked leaf, not the top-level runnable.
    if validate is not None:
        err = validate(target, resolved_command, got_args)
        if err:
            return result(False, classify_bounce(err), err)

    detail = "tool + args as expected"
    if case.expect_command is not None:
        detail = "tool + subcommand + args as expected"
    if case.expect_autonomy is not None and resolve_autonomy is not None:
        detail += f"; would gate at {resolved_autonomy!r}"
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
    resolve_command: ResolveCommand = default_resolve_command,
    resolve_args: ResolveArgs = default_resolve_args,
    resolve_autonomy: ResolveAutonomy | None = None,
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
            resolve_command=resolve_command,
            resolve_args=resolve_args,
            resolve_autonomy=resolve_autonomy,
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
