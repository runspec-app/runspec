"""
Tests for the parse() stub generator (runspec.stubgen / `runspec stubs`).

Unit tests cover the pure rendering helpers; drift guards keep the hand-written
metadata surface in sync with RunSpec/Arg/Group; and a mypy integration test
proves a generated stub actually types `parse("name")` precisely against the
installed runspec (self-contained — no source tree on the path).
"""

import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from runspec import stubgen
from runspec.models import Arg, Group, RunSpec

# ── pure rendering helpers ────────────────────────────────────────────────────


def test_py_type_scalar_and_collections() -> None:
    assert stubgen._py_type({"type": "int"}) == "int"
    assert stubgen._py_type({"type": "path"}) == "Path"
    assert stubgen._py_type({"type": "flag"}) == "bool"
    assert stubgen._py_type({"type": "str", "multiple": True}) == "list[str]"
    assert stubgen._py_type({"type": "rest"}) == "list[str]"


def test_py_type_choice_becomes_literal() -> None:
    assert stubgen._py_type({"type": "choice", "options": ["json", "yaml"]}) == "Literal['json', 'yaml']"
    # options win over a multiple flag too, wrapped in list
    assert stubgen._py_type({"options": ["a", "b"], "multiple": True}) == "list[Literal['a', 'b']]"


def test_view_name_camelcases() -> None:
    assert stubgen._view_name("demo") == "_DemoArgs"
    assert stubgen._view_name("my-tool") == "_MyToolArgs"
    assert stubgen._view_name("deploy_db") == "_DeployDbArgs"


def test_collect_args_includes_subcommand_superset() -> None:
    script = {
        "args": {"verbose": {"type": "flag"}},
        "commands": {
            "run": {"args": {"target": {"type": "str"}}, "commands": {}},
            "validate": {"args": {"strict": {"type": "flag"}}, "commands": {}},
        },
    }
    collected = stubgen.collect_args(script)
    assert set(collected) == {"verbose", "target", "strict"}


def test_render_stub_is_deterministic_and_sorted() -> None:
    specs = {
        "beta": {"x": {"type": "int"}},
        "alpha": {"y": {"type": "str"}},
    }
    out1 = stubgen.render_stub(specs)
    out2 = stubgen.render_stub(specs)
    assert out1 == out2
    # sorted: alpha's view precedes beta's
    assert out1.index("_AlphaArgs") < out1.index("_BetaArgs")
    # a literal overload per runnable + a broad fallback
    assert out1.count("@overload") == 3
    assert 'def parse(script_name: Literal["alpha"]' in out1
    assert "hyphen" not in out1  # sanity: identifiers normalised


def test_hyphenated_arg_becomes_underscore_attribute() -> None:
    out = stubgen.render_stub({"demo": {"dry-run": {"type": "flag"}}})
    assert "dry_run: Arg[bool]" in out


# ── drift guards: the stub's hand-written surface must track the real classes ──


def test_view_base_covers_all_runspec_properties() -> None:
    """Every public `runspec_*` property of RunSpec must be in the view base,
    or generated views would silently lose it."""
    props = [name for name, val in vars(RunSpec).items() if isinstance(val, property) and name.startswith("runspec_")]
    missing = [p for p in props if f"def {p}(" not in stubgen._VIEW_BASE]
    assert not missing, f"_VIEW_BASE is missing RunSpec properties: {missing}"


def test_rendered_stub_covers_all_arg_and_group_fields() -> None:
    out = stubgen.render_stub({"demo": {"x": {"type": "int"}}})
    for cls in (Arg, Group):
        for f in dataclasses.fields(cls):
            if f.name.startswith("_"):
                continue
            assert f"{f.name}:" in out, f"{cls.__name__}.{f.name} missing from stub"


# ── mypy integration: the generated stub really types parse() ─────────────────

pytestmark_mypy = pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy not installed")


def _write_stub(typings: Path, specs: dict) -> None:
    target = typings / "runspec" / "__init__.pyi"
    target.parent.mkdir(parents=True)
    target.write_text(stubgen.render_stub(specs))


def _mypy(consumer: Path, typings: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "--no-error-summary", str(consumer)],
        capture_output=True,
        text=True,
        env=os.environ | {"MYPYPATH": str(typings)},
    )


@pytestmark_mypy
def test_generated_stub_types_parse_precisely(tmp_path: Path) -> None:
    typings = tmp_path / "typings"
    _write_stub(
        typings,
        {"greeter": {"name": {"type": "str"}, "times": {"type": "int"}, "format": {"type": "choice", "options": ["text", "json"]}}},
    )
    consumer = tmp_path / "use.py"
    consumer.write_text(
        "from typing import assert_type\n"
        "from runspec import parse\n"
        "args = parse('greeter')\n"
        "assert_type(args.times.value, int)\n"
        "assert_type(args.name.value, str)\n"
        "reveal_type(args.format.value)\n"
        "assert_type(args.runspec_agent, bool)\n"
    )
    result = _mypy(consumer, typings)
    assert result.returncode == 0, f"mypy errors:\n{result.stdout}\n{result.stderr}"
    # mypy expands a multi-member Literal into a union of single-member Literals.
    assert "Literal['text']" in result.stdout and "Literal['json']" in result.stdout


@pytestmark_mypy
def test_generated_stub_flags_unknown_arg(tmp_path: Path) -> None:
    typings = tmp_path / "typings"
    _write_stub(typings, {"greeter": {"name": {"type": "str"}}})
    consumer = tmp_path / "use.py"
    consumer.write_text("from runspec import parse\nargs = parse('greeter')\nargs.naem\n")
    result = _mypy(consumer, typings)
    assert result.returncode != 0
    assert "attr-defined" in result.stdout
