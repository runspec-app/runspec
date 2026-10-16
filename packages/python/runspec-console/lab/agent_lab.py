"""agent_lab.py — measure how the console's agent loop handles a workload.

Drives the real ``Bridge`` headless (no window) against fake runnables, once per
scenario per variant, and reports what each model call carried: input size,
history growth, how much a prompt cache could reuse, and whether the model got
what it needed to answer. See ``lab/README.md``.

Two tiers:

* **scripted** (default) — a scripted stand-in model makes the scenario's tool
  calls, greps a spilled result for anything it still needs, then answers. No
  key, no network, same numbers every run; it measures the console's plumbing
  (what it sends), not model judgement. Token counts are estimates (chars / 4).
* **live** (``--live``) — the configured model drives. Reports the provider's
  real token usage (input, cache read/write, output) and checks each answer
  against the turn's ``expect`` regex. Needs a key, e.g. ``ANTHROPIC_API_KEY``,
  or ``--llm my-llm.toml`` holding an ``[llm]`` table (any provider the console
  supports, including a corporate proxy).

The script bootstraps its own venv (``--venv``) holding the console, the core
library and ``lab/fakes``, then re-runs itself inside it — the console discovers
local runnables from the venv it runs in.

    python lab/agent_lab.py                       # scripted, every scenario × variant
    python lab/agent_lab.py -s follow-up-chat -v baseline,compact
    python lab/agent_lab.py --live --samples 3 --md report.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LAB = Path(__file__).resolve().parent
CONSOLE = LAB.parent
CORE = CONSOLE.parent / "runspec"
FAKES = LAB / "fakes"
_IN_VENV = "RUNSPEC_AGENT_LAB_VENV"

# Prompt-cache pricing relative to base input: a cache read costs 0.1x, a cache
# write 1.25x (Anthropic's published 5-minute-TTL multipliers). Used only for
# the scripted tier's estimate; the live tier reports the provider's counts.
_CACHE_READ = 0.1
_CACHE_WRITE = 1.25
_CHARS_PER_TOKEN = 4


# ── bootstrap ─────────────────────────────────────────────────────────────────


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _bootstrap(venv: Path, live: bool) -> None:
    """Create/refresh the lab venv and re-exec this script inside it."""
    py = _venv_python(venv)
    if not py.exists():
        print(f"creating lab venv at {venv}", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    stamp = venv / ".agent-lab-installed"
    if not stamp.exists():
        pip = [str(py), "-m", "pip", "install", "-q"]
        deps = ["tomli-w", "paramiko", "cryptography"] + (["anthropic"] if live else [])
        subprocess.run([*pip, "-e", str(CORE), *deps, "-e", str(FAKES)], check=True)
        # --no-deps: pywebview (the window) isn't needed headless and doesn't
        # build everywhere.
        subprocess.run([*pip, "--no-deps", "-e", str(CONSOLE)], check=True)
        stamp.write_text("ok\n")
    elif live:
        subprocess.run([str(py), "-m", "pip", "install", "-q", "anthropic"], check=True)
    env = dict(os.environ, **{_IN_VENV: "1"})
    os.execve(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env)


# ── scenarios and variants ────────────────────────────────────────────────────


def _load(path: Path, key: str) -> list[dict[str, Any]]:
    with open(path, "rb") as f:
        return list(tomllib.load(f).get(key, []))


def _pick(items: list[dict[str, Any]], names: str) -> list[dict[str, Any]]:
    if not names:
        return items
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    known = {i["name"] for i in items}
    missing = [n for n in wanted if n not in known]
    if missing:
        sys.exit(f"unknown: {', '.join(missing)} (known: {', '.join(sorted(known))})")
    return [i for i in items if i["name"] in wanted]


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = (
            _merge(out[k], v)
            if isinstance(v, dict) and isinstance(out.get(k), dict)
            else v
        )
    return out


# ── metering ──────────────────────────────────────────────────────────────────


@dataclass
class Call:
    tools_chars: int
    messages_chars: int
    cached_chars: int  # longest prefix shared with the previous call's request


@dataclass
class TurnResult:
    prompt: str
    calls: list[Call] = field(default_factory=list)
    answer: str = ""
    passed: bool = False
    usage: dict[str, int] = field(default_factory=dict)  # live tier only
    error: str = ""


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    lo, hi = 0, n
    while lo < hi:  # binary search on prefix equality — requests run to ~1 MB
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


class Meter:
    """Wraps any adapter and records the size of every request it is sent."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._prev = ""
        self.calls: list[Call] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def reset_turn(self) -> list[Call]:
        calls, self.calls = self.calls, []
        return calls

    async def stream_with_tools(self, messages, tools, *args, **kwargs):
        t = json.dumps(tools, default=str, sort_keys=True)
        m = json.dumps(messages, default=str)
        req = t + m
        self.calls.append(Call(len(t), len(m), _common_prefix(self._prev, req)))
        self._prev = req
        async for item in self._inner.stream_with_tools(
            messages, tools, *args, **kwargs
        ):
            yield item


class ScriptedModel:
    """Makes a turn's scripted tool calls, greps a spill for what's missing,
    then answers. Decides from what it was actually sent, like a model would."""

    model = "scripted"

    def __init__(self, tool_for: dict[str, str]) -> None:
        self._tool_for = tool_for
        self.turn: dict[str, Any] = {}
        self._step = 0
        self._grepped: set[str] = set()
        self._n = 0

    def begin(self, turn: dict[str, Any]) -> None:
        self.turn, self._step, self._grepped = turn, 0, set()

    def _seen(self, messages: list[Any]) -> str:
        # Only what this turn's tool results carried — earlier turns don't count
        # toward this turn's answer, matching how `need` is written.
        text = json.dumps(messages, default=str)
        marker = json.dumps(self.turn["prompt"])[1:-1]
        cut = text.rfind(marker)
        return text[cut:] if cut >= 0 else text

    async def stream_with_tools(self, messages, tools, *args, **kwargs):
        from runspec_console.adapters.base import ChatResponse, ToolCall

        self._n += 1
        steps = self.turn.get("steps", [])
        seen = self._seen(messages)
        missing = [
            n for n in self.turn.get("need", []) if json.dumps(n)[1:-1] not in seen
        ]
        call = None
        if self._step < len(steps):
            st = steps[self._step]
            self._step += 1
            name = self._tool_for.get(st["runnable"])
            if not name:
                raise RuntimeError(f"no agent tool for runnable {st['runnable']!r}")
            call = (name, dict(st.get("args", {})))
        elif missing and "tool-results/" in seen:
            todo = [n for n in missing if n not in self._grepped]
            if todo:
                self._grepped.add(todo[0])
                call = (
                    "workspace_grep",
                    {"pattern": todo[0], "fixed": True, "path": "tool-results"},
                )
        if call:
            tc = ToolCall(id=f"call{self._n}", name=call[0], input=call[1])
            yield (
                "done",
                ChatResponse(text=None, tool_calls=[tc], stop_reason="tool_use"),
            )
            return
        text = "ANSWERED" if not missing else "MISSING: " + ", ".join(missing)
        yield ("text", text)
        yield ("done", ChatResponse(text=text, tool_calls=[], stop_reason="end_turn"))

    def make_tool_turn(self, response, results):
        return [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    }
                    for tc in response.tool_calls
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": out}
                    for tc, out in results
                ],
            },
        ]


# ── running ───────────────────────────────────────────────────────────────────


class Events:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, detail: dict[str, Any]) -> None:
        with self._lock:
            self.items.append((event, detail))

    def for_id(self, chat_id: str) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(e, d) for e, d in self.items if d.get("id") == chat_id]


def _wait_discovery(bridge: Any, names: set[str], timeout: float = 60) -> None:
    bridge.refresh_hosts()
    end = time.time() + timeout
    while time.time() < end:
        found = {r.get("name") for r in (bridge._runnables_cache or [])}
        if names <= found:
            return
        time.sleep(0.25)
    sys.exit(f"fake runnables not discovered: {sorted(names - found)}")


def _run_turn(
    bridge: Any, events: Events, session: str, prompt: str, timeout: float
) -> tuple[str, dict[str, int], str]:
    chat_id = bridge.send_chat(prompt, session_id=session)
    end = time.time() + timeout
    while time.time() < end:
        evs = events.for_id(chat_id)
        if any(e == "runspec:run_end" for e, _ in evs):
            answer = "".join(d.get("token", "") for e, d in evs if e == "runspec:token")
            usage = next((d for e, d in evs if e == "runspec:chat_usage"), {})
            usage = {k: int(v) for k, v in usage.items() if k.endswith("_tokens")}
            return answer, usage, ""
        time.sleep(0.05)
    return "", {}, f"turn timed out after {timeout:.0f}s"


def _apply_patches(patches: dict[str, Any]) -> None:
    """Set module attributes named ``package.module.ATTR`` — for trying an option
    that only exists as a code constant (e.g. the 32 KB spill floor)."""
    import importlib

    for dotted, value in patches.items():
        mod_name, _, attr = dotted.rpartition(".")
        mod = importlib.import_module(mod_name)
        if not hasattr(mod, attr):
            sys.exit(f"patch target not found: {dotted}")
        setattr(mod, attr, value)


def run_variant(
    variant: dict[str, Any], scenarios: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    import tomli_w

    from runspec_console import config as config_mod

    cfg_dir = Path(tempfile.mkdtemp(prefix=f"agent-lab-{variant['name']}-"))
    cfg = dict(variant.get("config", {}))
    if args.live:
        llm = {"provider": "anthropic", "model": args.model}
        if args.llm:
            with open(args.llm, "rb") as f:
                llm = tomllib.load(f).get("llm", llm)
        cfg = _merge(cfg, {"llm": llm})
    (cfg_dir / "config.toml").write_text(tomli_w.dumps(cfg))
    config_mod.set_app_dir(cfg_dir)
    _apply_patches(variant.get("patch", {}))

    from runspec_console.bridge import Bridge

    bridge = Bridge()
    events = Events()
    bridge._dispatch = events
    runnables = {
        s["runnable"]
        for sc in scenarios
        for t in sc["turn"]
        for s in t.get("steps", [])
    }
    _wait_discovery(bridge, runnables)
    tool_for = {
        t["name"]: t["name"]
        for t in bridge._runnables_to_tools()
        if t["name"] in runnables
    }
    if args.live:
        inner = bridge._get_adapter()
        if inner is None:
            sys.exit("--live: no model adapter (check [llm] and the API key)")
    else:
        inner = ScriptedModel(tool_for)
    meter = Meter(inner)
    bridge._adapter = meter
    bridge._get_adapter = lambda *a, **k: meter

    out = []
    for sc in scenarios:
        for sample in range(args.samples):
            session = bridge.new_chat_session()["id"]
            meter._prev = ""
            turns = []
            for turn in sc["turn"]:
                if isinstance(inner, ScriptedModel):
                    inner.begin(turn)
                answer, usage, err = _run_turn(
                    bridge, events, session, turn["prompt"], args.timeout
                )
                tr = TurnResult(
                    turn["prompt"], meter.reset_turn(), answer, usage=usage, error=err
                )
                if args.live:
                    tr.passed = bool(
                        re.search(turn.get("expect", "."), answer, re.IGNORECASE)
                    )
                else:
                    tr.passed = answer.strip() == "ANSWERED"
                turns.append(tr)
            out.append(
                {
                    "variant": variant["name"],
                    "scenario": sc["name"],
                    "sample": sample,
                    "turns": [asdict(t) for t in turns],
                }
            )
            print(
                f"  {variant['name']:<18} {sc['name']:<26} #{sample} "
                f"{'pass' if all(t.passed for t in turns) else 'FAIL'}",
                file=sys.stderr,
            )
    return out


# ── reporting ─────────────────────────────────────────────────────────────────


def _tok(chars: float) -> int:
    return round(chars / _CHARS_PER_TOKEN)


def summarise(run: dict[str, Any]) -> dict[str, Any]:
    calls = [c for t in run["turns"] for c in t["calls"]]
    sent = [c["tools_chars"] + c["messages_chars"] for c in calls]
    cached = [min(c["cached_chars"], s) for c, s in zip(calls, sent)]
    billed_cache = sum(
        c * _CACHE_READ + (s - c) * _CACHE_WRITE for s, c in zip(sent, cached)
    )
    usage: dict[str, int] = {}
    for t in run["turns"]:
        for k, v in t.get("usage", {}).items():
            usage[k] = usage.get(k, 0) + v
    return {
        "variant": run["variant"],
        "scenario": run["scenario"],
        "passed": all(t["passed"] for t in run["turns"]),
        "calls": len(calls),
        "input_tok": _tok(sum(sent)),
        "input_tok_cached": _tok(billed_cache),
        "peak_call_tok": _tok(max(sent, default=0)),
        "history_tok": _tok(calls[-1]["messages_chars"]) if calls else 0,
        "usage": usage,
    }


def report_md(rows: list[dict[str, Any]], live: bool) -> str:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["variant"]), []).append(r)

    def avg(rs: list[dict[str, Any]], key: str) -> int:
        return round(sum(r[key] for r in rs) / len(rs))

    lines = []
    if live:
        lines.append(
            "| Scenario | Variant | Pass | Calls | Input | Cache read | Cache write | Output |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
    else:
        lines.append(
            "| Scenario | Variant | Pass | Calls | Input tok (no cache) | Input tok-equiv (cache) | Peak call tok | History tok at end |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
    for (sc, var), rs in groups.items():
        passed = f"{sum(r['passed'] for r in rs)}/{len(rs)}"
        if live:
            u = {
                k: round(sum(r["usage"].get(k, 0) for r in rs) / len(rs))
                for k in (
                    "input_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                    "output_tokens",
                )
            }
            lines.append(
                f"| {sc} | {var} | {passed} | {avg(rs, 'calls')} | {u['input_tokens']:,} | "
                f"{u['cache_read_tokens']:,} | {u['cache_creation_tokens']:,} | {u['output_tokens']:,} |"
            )
        else:
            lines.append(
                f"| {sc} | {var} | {passed} | {avg(rs, 'calls')} | {avg(rs, 'input_tok'):,} | "
                f"{avg(rs, 'input_tok_cached'):,} | {avg(rs, 'peak_call_tok'):,} | {avg(rs, 'history_tok'):,} |"
            )
    if not live:
        lines += [
            "",
            (
                "Scripted tier: tokens are estimates (chars / 4) of the tool schemas plus messages "
                "sent on each model call, excluding the system prompt. *Input tok (no cache)* is "
                "the sum over calls; *Input tok-equiv (cache)* prices the part shared with the "
                "previous call as a cache read (0.1x) and the rest as a cache write (1.25x). "
                "*Pass* means every turn's `need` strings reached the model."
            ),
        ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "-s",
        "--scenarios",
        default="",
        help="comma-separated scenario names (default: all)",
    )
    ap.add_argument(
        "-v",
        "--variants",
        default="",
        help="comma-separated variant names (default: all)",
    )
    ap.add_argument("--scenario-file", type=Path, default=LAB / "scenarios.toml")
    ap.add_argument("--variant-file", type=Path, default=LAB / "variants.toml")
    ap.add_argument(
        "--samples", type=int, default=1, help="runs per scenario × variant"
    )
    ap.add_argument(
        "--live", action="store_true", help="use a real model (needs a key)"
    )
    ap.add_argument(
        "--model",
        default="claude-sonnet-5-5",
        help="--live model when --llm isn't given",
    )
    ap.add_argument("--llm", type=Path, help="TOML file with an [llm] table for --live")
    ap.add_argument("--timeout", type=float, default=300, help="seconds per turn")
    ap.add_argument("--json", type=Path, help="write raw per-call results here")
    ap.add_argument("--md", type=Path, help="write the markdown report here")
    ap.add_argument(
        "--venv",
        type=Path,
        default=Path.home() / ".cache" / "runspec-agent-lab" / "venv",
    )
    ap.add_argument("--child-variant", help=argparse.SUPPRESS)
    ap.add_argument("--child-out", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not os.environ.get(_IN_VENV):
        _bootstrap(args.venv, args.live)

    os.environ.setdefault("RUNSPEC_AUDIT", "off")  # don't write run audit records
    scenarios = _pick(_load(args.scenario_file, "scenario"), args.scenarios)
    variants = _pick(_load(args.variant_file, "variant"), args.variants)
    if args.child_variant:
        # Child process: one variant, its env already applied by the parent
        # (the console snapshots os.environ at import, so env can't change
        # in-process), results to a file.
        (variant,) = [v for v in variants if v["name"] == args.child_variant]
        runs = run_variant(variant, scenarios, args)
        args.child_out.write_text(json.dumps(runs))
        return
    runs = []
    for v in variants:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "runs.json"
            env = dict(os.environ, **{k: str(x) for k, x in v.get("env", {}).items()})
            argv = [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
                "--child-variant",
                v["name"],
                "--child-out",
                str(out),
            ]
            subprocess.run(argv, env=env, check=True)
            runs += json.loads(out.read_text())
    rows = [summarise(r) for r in runs]
    md = report_md(rows, args.live)
    print(md)
    if args.md:
        args.md.write_text(md)
    if args.json:
        args.json.write_text(json.dumps({"runs": runs, "summary": rows}, indent=2))


if __name__ == "__main__":
    main()
