#!/usr/bin/env python3
"""Build runspec-console wheel.

Steps:
  1. npm run build  — Vite bundles the UI into runspec_console/dist/
  2. python -m build — hatchling packages the wheel (force-includes dist/)

Run from any directory:
    python packages/python/runspec-console/build.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent  # repo root
CONSOLE_UI = ROOT / "packages" / "console-ui"
PYTHON_PKG = ROOT / "packages" / "python" / "runspec-console"


def run(cmd: list[str], cwd: Path, *, shell: bool = False) -> None:
    display = " ".join(cmd)
    print(f"\n>>> {display}  (in {cwd.relative_to(ROOT)})")
    result = subprocess.run(cmd, cwd=cwd, shell=shell)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    # On Windows, npm is npm.cmd — use shell=True so PATH resolution works
    is_windows = sys.platform == "win32"
    npm = ["npm.cmd", "run", "build"] if is_windows else ["npm", "run", "build"]

    run(npm, CONSOLE_UI, shell=is_windows)

    dist_dir = PYTHON_PKG / "runspec_console" / "dist"
    if not dist_dir.exists():
        print(f"ERROR: Vite output not found at {dist_dir}", file=sys.stderr)
        sys.exit(1)

    run([sys.executable, "-m", "build"], PYTHON_PKG)

    wheels = list((PYTHON_PKG / "dist").glob("*.whl"))
    if wheels:
        print(f"\nWheel ready: {wheels[-1].name}")


if __name__ == "__main__":
    main()
