"""The run record's ``invocation_args``: text values, a list for a multi-valued arg.

A ``multiple`` choice used to be recorded as ``str(['Open'])`` == "['Open']";
a History rerun then passed that string back as one value and the run failed
its choice check.
"""

from __future__ import annotations

from typing import Any

import pytest

import runspec.parser as parser_mod
from runspec.testing import ParseHarness

TOML = """\
[demo]
description = "demo"

[demo.args]
state = { options = ["Open", "Closed", "Pending"], multiple = true, default = ["Open"] }
level = { options = ["low", "high"], default = "low" }
count = { default = 3 }
"""


def _recorded(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    real = parser_mod.configure_logging

    def capture(*a: Any, **kw: Any) -> Any:
        seen.update(kw.get("invocation_args") or {})
        return real(*a, **kw)

    monkeypatch.setattr(parser_mod, "configure_logging", capture)
    h = ParseHarness(script_name="demo", toml=TOML)
    try:
        h.expect_ok(argv)
    finally:
        h.close()
    return {k: v["value"] for k, v in seen.items()}


def test_multiple_choice_is_recorded_as_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _recorded(monkeypatch, ["--state", "Open", "--state", "Pending"])
    assert got["state"] == ["Open", "Pending"]


def test_one_pick_of_a_multiple_choice_is_a_one_item_list(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _recorded(monkeypatch, ["--state", "Open"])
    assert got["state"] == ["Open"]


def test_single_values_stay_text(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _recorded(monkeypatch, ["--level", "high", "--count", "5"])
    assert got["level"] == "high"
    assert got["count"] == "5"


def test_record_value() -> None:
    assert parser_mod._record_value(["a", 1]) == ["a", "1"]
    assert parser_mod._record_value(("x",)) == ["x"]
    assert parser_mod._record_value(42) == "42"
