"""build_become command construction + user validation."""

import pytest

from runspec_fs_core.become import (
    BECOME_METHODS,
    BecomeError,
    build_become,
    current_user,
    escalation_supported,
    validate_user,
)


def test_no_run_as_returns_argv_unchanged() -> None:
    argv = ["python", "-m", "runspec_fs._worker"]
    assert build_become(argv, None) is argv
    assert build_become(argv, "") is argv


def test_sudo_wrap() -> None:
    assert build_become(["python", "-c", "x"], "svc") == [
        "sudo",
        "-n",
        "-u",
        "svc",
        "python",
        "-c",
        "x",
    ]


def test_su_wrap_joins_argv() -> None:
    out = build_become(["python", "-m", "runspec_fs._worker"], "svc", method="su")
    assert out == ["su", "svc", "-c", "python -m runspec_fs._worker"]


def test_invalid_user_rejected() -> None:
    # An empty user means "no escalation" (passthrough), not an error — so it is
    # covered by test_no_run_as_returns_argv_unchanged, not here.
    for bad in ["-rf", "a b", "root; rm", "$(whoami)"]:
        with pytest.raises(BecomeError):
            build_become(["python"], bad)
    # validate_user, called directly, does reject the empty name.
    with pytest.raises(BecomeError):
        validate_user("")


def test_invalid_method_rejected() -> None:
    with pytest.raises(BecomeError):
        build_become(["python"], "svc", method="doas")


def test_validate_user_accepts_common_names() -> None:
    for good in ["svc", "www-data", "_apt", "user1"]:
        validate_user(good)  # no raise


def test_current_user_is_str() -> None:
    assert isinstance(current_user(), str) and current_user()


def test_escalation_supported_matches_platform() -> None:
    import os

    assert escalation_supported() == hasattr(os, "geteuid")
    assert set(BECOME_METHODS) == {"sudo", "su"}
