"""The wrapper-side escalation resolver: --run-as / --become-method →
(run_as, become_method), refusing any root target.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from runspec_linux._escalation import is_root, refuse_root, resolve_escalation


def _spec(run_as: str, method: str = "sudo") -> SimpleNamespace:
    # A parsed spec exposes each arg as an object with a .value.
    return SimpleNamespace(run_as=SimpleNamespace(value=run_as), become_method=SimpleNamespace(value=method))


def test_run_as_user() -> None:
    assert resolve_escalation(_spec("deploy")) == ("deploy", "sudo")


def test_empty_is_none() -> None:
    assert resolve_escalation(_spec("")) == (None, "sudo")


def test_become_method_passthrough() -> None:
    assert resolve_escalation(_spec("svc", method="su")) == ("svc", "su")


@pytest.mark.parametrize("target", ["root", "0"])
def test_root_is_refused(target: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        resolve_escalation(_spec(target))
    assert exc.value.code == 2
    assert "runspec-linux-root" in json.loads(capsys.readouterr().out)["error"]


def test_is_root() -> None:
    assert is_root("root") and is_root("0")
    assert not is_root("svc-no-such-user")
    refuse_root(None)
    refuse_root("svc")


def test_no_root_tools_left() -> None:
    from importlib.resources import files

    tomllib = pytest.importorskip("tomllib")
    spec = tomllib.loads((files("runspec_linux") / "runspec.toml").read_text())
    for name, body in spec.items():
        if name == "config":
            continue
        assert body.get("run_as") != "root", name
        assert "sudo" not in (body.get("args") or {}), name
