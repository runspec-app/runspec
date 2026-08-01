"""The shared privilege-escalation command builder (runspec_linux_core.become).

Pure argv construction + validation — no subprocess is run. These assert the
exact command shape that the venv helpers and the ``--run-as`` / ``--sudo``
runnables build, and that a crafted user name can't inject extra tokens.
"""

import pytest

from runspec_linux_core.become import (
    BecomeError,
    build_become,
    check_become_error,
    current_user,
    escalation_supported,
    validate_user,
)
from runspec_linux_core.errors import CommandError


def test_no_user_is_noop() -> None:
    assert build_become(["echo", "hi"], None) == ["echo", "hi"]
    assert build_become(["echo", "hi"], "") == ["echo", "hi"]


def test_sudo_prefix() -> None:
    assert build_become(["echo", "hi"], "svc") == ["sudo", "-n", "-u", "svc", "echo", "hi"]


def test_su_quotes() -> None:
    assert build_become(["pip", "install", "a b"], "svc", "su") == ["su", "svc", "-c", "pip install 'a b'"]


def test_root_via_sudo() -> None:
    # --sudo maps to run_as="root" in the wrappers.
    assert build_become(["systemctl", "restart", "nginx"], "root") == ["sudo", "-n", "-u", "root", "systemctl", "restart", "nginx"]


def test_rejects_injection() -> None:
    with pytest.raises(BecomeError):
        build_become(["echo"], "bad;user")
    with pytest.raises(BecomeError):
        build_become(["echo"], "a b")


def test_dotted_name_allowed() -> None:
    # linux-core's canonical regex allows a dot (svc.deploy), a superset of
    # fs-core's — so names runspec-linux already accepts keep validating.
    assert build_become(["id"], "svc.deploy") == ["sudo", "-n", "-u", "svc.deploy", "id"]


def test_rejects_bad_method() -> None:
    with pytest.raises(CommandError):
        build_become(["echo"], "svc", "doas")


def test_become_error_is_a_command_error() -> None:
    # so the thin wrappers' `except CommandError` catch an invalid run-as too.
    assert issubclass(BecomeError, CommandError)


def test_validate_user() -> None:
    assert validate_user("deploy") == "deploy"
    with pytest.raises(BecomeError):
        validate_user("")
    with pytest.raises(BecomeError):
        validate_user("Bad")  # uppercase not allowed


def test_check_become_error_passwordless_hint() -> None:
    with pytest.raises(CommandError, match="passwordless sudo is not configured for run-as 'svc'"):
        check_become_error("restart", "sudo: a password is required", "svc")


def test_check_become_error_plain() -> None:
    with pytest.raises(CommandError, match="boom"):
        check_become_error("restart", "boom", None)
    # no run_as → no passwordless hint even on a password error
    with pytest.raises(CommandError, match="password"):
        check_become_error("restart", "sudo: a password is required", None)


def test_escalation_supported_and_current_user() -> None:
    # POSIX host in CI: both are meaningful and don't raise.
    assert escalation_supported() is True
    assert isinstance(current_user(), str) and current_user()
