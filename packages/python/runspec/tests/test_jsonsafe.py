"""Tests for jsonsafe — JSON encoding for log/audit values that never raises."""

from __future__ import annotations

import datetime
import enum
import json
from pathlib import Path

from runspec import jsonsafe


class Kind(enum.Enum):
    IDLE = "session.idle"
    PAIR = ("a", 1)


class Level(enum.IntEnum):
    HIGH = 3


class Broken:
    def __str__(self) -> str:
        raise RuntimeError("no")


def _round(obj):
    return json.loads(jsonsafe.dumps(obj))


def test_plain_values_unchanged():
    obj = {"a": 1, "b": [1.5, None, True], "c": "x"}
    assert jsonsafe.dumps(obj) == json.dumps(obj)


def test_enum_becomes_its_value():
    assert _round({"k": Kind.IDLE}) == {"k": "session.idle"}
    assert _round(Kind.PAIR) == ["a", 1]
    assert _round(Level.HIGH) == 3


def test_common_types():
    when = datetime.datetime(2026, 11, 13, 19, 0, tzinfo=datetime.timezone.utc)
    out = _round(
        {
            "when": when,
            "day": datetime.date(2026, 11, 13),
            "path": Path("/var/log/x.log"),
            "set": frozenset({"only"}),
            "bytes": b"ok\xff",
        }
    )
    assert out == {
        "when": "2026-11-13T19:00:00+00:00",
        "day": "2026-11-13",
        "path": str(Path("/var/log/x.log")),
        "set": ["only"],
        "bytes": "ok�",
    }


def test_unknown_object_uses_str():
    class Thing:
        def __str__(self) -> str:
            return "thing-1"

    assert _round({"t": Thing()}) == {"t": "thing-1"}


def test_broken_str_falls_back_to_repr():
    assert _round([Broken()])[0].startswith("<")


def test_keys_json_cannot_hold():
    assert _round({Kind.IDLE: 1, (1, 2): 2, 3: 3}) == {"session.idle": 1, "(1, 2)": 2, "3": 3}


def test_keys_inside_values_from_default():
    # A key problem nested under a value only reached through default().
    assert _round({"outer": [{Kind.IDLE: Kind.IDLE}]}) == {"outer": [{"session.idle": "session.idle"}]}


def test_circular_container():
    loop: dict = {"a": 1}
    loop["self"] = loop
    assert _round(loop) == {"a": 1, "self": "<circular>"}


def test_kwargs_pass_through():
    assert jsonsafe.dumps({"b": 1, "a": Kind.IDLE}, sort_keys=True, separators=(",", ":")) == '{"a":"session.idle","b":1}'


def test_plain_for_text():
    assert jsonsafe.plain(Kind.IDLE) == "session.idle"
    assert jsonsafe.plain("x") == "x"
