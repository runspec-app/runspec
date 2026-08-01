"""The wrapper-side escalation resolver: --sudo / --run-as / --become-method →
(run_as, become_method).
"""

from __future__ import annotations

from types import SimpleNamespace

from runspec_linux._escalation import resolve_escalation


def _spec(sudo: bool, run_as: str, method: str = "sudo") -> SimpleNamespace:
    # A parsed spec exposes each arg as an object with a .value.
    return SimpleNamespace(
        sudo=SimpleNamespace(value=sudo),
        run_as=SimpleNamespace(value=run_as),
        become_method=SimpleNamespace(value=method),
    )


def test_sudo_maps_to_root() -> None:
    assert resolve_escalation(_spec(sudo=True, run_as="")) == ("root", "sudo")


def test_run_as_user() -> None:
    assert resolve_escalation(_spec(sudo=False, run_as="deploy")) == ("deploy", "sudo")


def test_neither_is_none() -> None:
    assert resolve_escalation(_spec(sudo=False, run_as="")) == (None, "sudo")


def test_become_method_passthrough() -> None:
    assert resolve_escalation(_spec(sudo=False, run_as="svc", method="su")) == ("svc", "su")
