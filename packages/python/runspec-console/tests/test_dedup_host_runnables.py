"""Duplicate runnables within one host's discovery (the same name in two venvs)."""

from __future__ import annotations

from runspec_console.bridge_internals import InternalsMixin


def _r(name: str, group: str, *, hidden: bool = False) -> dict:
    return {
        "host": "web-01",
        "name": name,
        "group": group,
        "rawSpec": {"discoverable": False} if hidden else {},
    }


def test_visible_duplicate_keeps_first_and_warns(capsys):
    out = InternalsMixin._dedup_host_runnables(
        [_r("which", "ops"), _r("which", "tools")], "web-01"
    )
    assert [r["group"] for r in out] == ["ops"]
    assert (
        "'which' defined in multiple installed packages on host 'web-01'"
        in capsys.readouterr().err
    )


def test_hidden_duplicate_is_dropped_quietly(capsys):
    runnables = [
        _r("runspec-scheduler", "ops", hidden=True),
        _r("runspec-scheduler", "tools", hidden=True),
        _r("runspec-scheduler", "apps", hidden=True),
    ]
    out = InternalsMixin._dedup_host_runnables(runnables, "web-01")
    assert [r["group"] for r in out] == ["ops"]
    assert capsys.readouterr().err == ""
