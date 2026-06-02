"""
Static-typing regression tests.

These run mypy over small fixture programs that use the public API the way a
runnable author would, and assert the checker is happy. They guard the
ergonomics of the parsed result: because args are dynamic (names and value
types come from runspec.toml at runtime), RunSpec.__getattr__ is typed `Any`
so a parsed value flows into an `int`/`str`/`Path`-annotated variable without
the caller having to unwrap it. See RunSpec.__getattr__ in models.py.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Package root that contains the importable `runspec/` directory (tests/..).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("mypy") is None,
    reason="mypy not installed",
)


def _run_mypy(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` with mypy, resolving `runspec` from the local tree."""
    fixture = tmp_path / "fixture.py"
    fixture.write_text(source)
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", str(fixture)],
        capture_output=True,
        text=True,
        env=os.environ | {"MYPYPATH": str(PACKAGE_ROOT)},
    )


def test_parsed_args_flow_into_annotated_variables(tmp_path: Path) -> None:
    """The core ergonomic guarantee: no unwrapping needed at the use site."""
    source = """\
import runspec as rs

args = rs.parse("greeter")

workers: int = args.workers          # Any flows into int
name: str = args.name                # Any flows into str
total: int = args.workers + 1        # arithmetic stays usable
flag: bool = bool(args.verbose)      # explicit conversion still works
"""
    result = _run_mypy(source, tmp_path)
    assert result.returncode == 0, f"mypy reported errors:\n{result.stdout}\n{result.stderr}"


def test_runspec_metadata_properties_are_typed(tmp_path: Path) -> None:
    """The public metadata accessors keep their concrete types (not Any)."""
    source = """\
from pathlib import Path
from typing import assert_type

import runspec as rs

args = rs.parse("greeter")

assert_type(args.runspec_runnable, str)
assert_type(args.runspec_source, Path)
assert_type(args.runspec_agent, bool)
assert_type(args.runspec_command, str | None)
"""
    result = _run_mypy(source, tmp_path)
    assert result.returncode == 0, f"mypy reported errors:\n{result.stdout}\n{result.stderr}"
