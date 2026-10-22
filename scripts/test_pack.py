#!/usr/bin/env python3
"""
test_pack.py — one command to run the whole pre-publish test pack.

Runs, per Python package: ``ruff check`` / ``ruff format --check`` / ``mypy`` /
``pytest``; for the frontends: ``npm run typecheck`` / ``npm test`` (+ ``npm run
build`` for the Node pack); and the docs-drift guard (``scripts/check_docs.py``).
Every step runs to completion — a failure never aborts the others — and the exit
code is non-zero if *any* step failed, so you see the full picture in one pass.

    python scripts/test_pack.py                 # everything
    python scripts/test_pack.py --package runspec-console
    python scripts/test_pack.py --python-only --skip-mypy
    python scripts/test_pack.py --frontend-only -v
    python scripts/test_pack.py --list          # show steps, run nothing

Gate: do not publish unless this pack is fully green.

Stdlib only, cross-platform (Windows/git-bash included). It runs the tools that
are already installed in the current environment — install each package's deps
first (``pip install -e ".[dev]"`` per package, plus the optional extras the
console tests import: anthropic, credentials, git-sync, schedule, otel, mcp,
sandbox, voice as available) so nothing is skipped for a missing dependency.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_PKGS_DIR = REPO_ROOT / "packages" / "python"
CONSOLE_UI = REPO_ROOT / "packages" / "console-ui"
NODE_PKG = REPO_ROOT / "packages" / "node"

# Core first (its wheel underpins every other package), then the rest sorted.
_CORE = "runspec"

# The packages CI actually type-checks (ci.yml ``run: mypy …``). Many packages
# carry a ``[tool.mypy]`` section for local/editor use but are NOT gated on mypy
# in CI — runspec-console most notably (documented pre-existing debt: Windows-only
# attrs + http-handler typing). Mirroring the real gate keeps the pack from
# failing on debt CI tolerates. console-room lives outside packages/python and is
# not discovered here.
_MYPY_GATED = frozenset(
    {
        "runspec",
        "runspec-jsm",
        "runspec-snow",
        "runspec-claude",
        "runspec-scheduler",
        "runspec-mcp",
        "runspec-webops-core",
        "runspec-fs-core",
        "runspec-gitlab-core",
        "runspec-prometheus-core",
        "runspec-grafana-core",
        "runspec-wireshark-core",
        "runspec-o365",
        "runspec-jira-core",
        "runspec-confluence-core",
    }
)


@dataclass
class Step:
    name: str  # human label, e.g. "runspec-console: pytest"
    cmd: list[str]
    cwd: Path


@dataclass
class Result:
    step: Step
    rc: int
    seconds: float
    output: str = ""
    skipped_reason: str = field(default="")

    @property
    def ok(self) -> bool:
        return self.skipped_reason != "" or self.rc == 0


def _import_dir(pkg: Path) -> Path | None:
    """The package's single import directory (top-level dir with __init__.py,
    excluding tests) — the mypy target, mirroring CI's ``mypy runspec/``."""
    candidates = [
        d
        for d in sorted(pkg.iterdir())
        if d.is_dir() and (d / "__init__.py").exists() and d.name != "tests"
    ]
    if not candidates:
        return None
    underscored = pkg.name.replace("-", "_")
    for d in candidates:
        if d.name == underscored:
            return d
    return candidates[0]


def _mypy_gated(pkg: Path) -> bool:
    """True when CI type-checks this package (see ``_MYPY_GATED``)."""
    return pkg.name in _MYPY_GATED


def discover_python_packages(only: list[str] | None) -> list[Path]:
    pkgs = [
        p
        for p in PYTHON_PKGS_DIR.iterdir()
        if p.is_dir() and (p / "pyproject.toml").exists()
    ]
    if only:
        wanted = set(only)
        pkgs = [p for p in pkgs if p.name in wanted]
    pkgs.sort(key=lambda p: (p.name != _CORE, p.name))
    return pkgs


def build_python_steps(
    pkgs: list[Path], *, skip_lint: bool, skip_mypy: bool, skip_tests: bool
) -> list[Step]:
    py = sys.executable
    steps: list[Step] = []
    for pkg in pkgs:
        if not skip_lint:
            steps.append(
                Step(f"{pkg.name}: ruff check", [py, "-m", "ruff", "check", "."], pkg)
            )
            steps.append(
                Step(
                    f"{pkg.name}: ruff format --check",
                    [py, "-m", "ruff", "format", "--check", "."],
                    pkg,
                )
            )
        if not skip_mypy and _mypy_gated(pkg):
            target = _import_dir(pkg)
            if target is not None:
                steps.append(
                    Step(f"{pkg.name}: mypy", [py, "-m", "mypy", target.name], pkg)
                )
        if not skip_tests and (pkg / "tests").is_dir():
            steps.append(Step(f"{pkg.name}: pytest", [py, "-m", "pytest", "-q"], pkg))
    return steps


def build_frontend_steps(*, include_node: bool) -> list[Step]:
    npm = shutil.which("npm")
    steps: list[Step] = []
    if CONSOLE_UI.is_dir():
        if npm:
            steps.append(
                Step("console-ui: typecheck", [npm, "run", "typecheck"], CONSOLE_UI)
            )
            steps.append(Step("console-ui: test", [npm, "test"], CONSOLE_UI))
        else:
            steps.append(Step("console-ui: (npm not found)", ["false"], CONSOLE_UI))
    if include_node and NODE_PKG.is_dir():
        if npm:
            steps.append(Step("node: typecheck", [npm, "run", "typecheck"], NODE_PKG))
            steps.append(Step("node: test", [npm, "test"], NODE_PKG))
            steps.append(Step("node: build", [npm, "run", "build"], NODE_PKG))
        else:
            steps.append(Step("node: (npm not found)", ["false"], NODE_PKG))
    return steps


def build_docs_steps() -> list[Step]:
    checker = REPO_ROOT / "scripts" / "check_docs.py"
    if checker.exists():
        return [Step("docs: check_docs", [sys.executable, str(checker)], REPO_ROOT)]
    return []


def run_step(step: Step, *, verbose: bool) -> Result:
    # A missing npm was encoded as ["false"] — report it as an explicit failure.
    if step.cmd == ["false"]:
        return Result(step, rc=1, seconds=0.0, output="required tool not found on PATH")
    start = time.time()
    try:
        if verbose:
            print(
                f"\n$ ({step.cwd.relative_to(REPO_ROOT)}) {' '.join(step.cmd)}",
                flush=True,
            )
            proc = subprocess.run(step.cmd, cwd=step.cwd)
            output = ""
        else:
            proc = subprocess.run(
                step.cmd, cwd=step.cwd, capture_output=True, text=True
            )
            output = (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError as exc:
        return Result(step, rc=127, seconds=time.time() - start, output=str(exc))
    return Result(step, rc=proc.returncode, seconds=time.time() - start, output=output)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument(
        "--python-only", action="store_true", help="only the Python packages"
    )
    scope.add_argument(
        "--frontend-only", action="store_true", help="only console-ui + node"
    )
    scope.add_argument(
        "--docs-only", action="store_true", help="only the docs-drift check"
    )
    ap.add_argument(
        "--package",
        action="append",
        metavar="NAME",
        help="restrict to this Python package (repeatable)",
    )
    ap.add_argument("--skip-lint", action="store_true", help="skip ruff check + format")
    ap.add_argument("--skip-mypy", action="store_true", help="skip mypy")
    ap.add_argument("--skip-tests", action="store_true", help="skip pytest")
    ap.add_argument(
        "--no-node",
        action="store_true",
        help="skip the Node pack (console-ui still runs)",
    )
    ap.add_argument(
        "--list", action="store_true", help="print the steps and exit without running"
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="stream each step's output live"
    )
    args = ap.parse_args()

    do_python = not (args.frontend_only or args.docs_only)
    do_frontend = not (args.python_only or args.docs_only)
    do_docs = not (args.python_only or args.frontend_only)

    steps: list[Step] = []
    if do_python:
        pkgs = discover_python_packages(args.package)
        if args.package and not pkgs:
            print(f"error: no Python package matched {args.package}", file=sys.stderr)
            return 2
        steps += build_python_steps(
            pkgs,
            skip_lint=args.skip_lint,
            skip_mypy=args.skip_mypy,
            skip_tests=args.skip_tests,
        )
    if do_frontend:
        steps += build_frontend_steps(include_node=not args.no_node)
    if do_docs:
        steps += build_docs_steps()

    if args.list:
        for s in steps:
            print(f"  {s.name:40s}  ({s.cwd.relative_to(REPO_ROOT)}) {' '.join(s.cmd)}")
        print(f"\n{len(steps)} step(s).")
        return 0

    if not steps:
        print("Nothing to run.")
        return 0

    print(f"Running {len(steps)} step(s)…\n")
    results: list[Result] = []
    for s in steps:
        r = run_step(s, verbose=args.verbose)
        results.append(r)
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {s.name}  ({r.seconds:.1f}s)")
        if not r.ok and not args.verbose and r.output:
            tail = "\n".join(r.output.rstrip().splitlines()[-40:])
            print("  " + tail.replace("\n", "\n  "))

    failed = [r for r in results if not r.ok]
    total_s = sum(r.seconds for r in results)
    print("\n" + "=" * 60)
    print(f"{len(results) - len(failed)}/{len(results)} passed in {total_s:.1f}s")
    if failed:
        print("\nFAILED:")
        for r in failed:
            print(f"  - {r.step.name}")
        print("\nDo not publish until the pack is fully green.")
        return 1
    print("\nAll green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
