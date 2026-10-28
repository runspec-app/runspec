"""The venv helpers removed in 0.14.0 stay importable and raise when called."""

import pytest

import runspec_linux_core
from runspec_linux_core import configure_pip, create_venv, install_into_venv, share_venv_logs
from runspec_linux_core.errors import LinuxCoreError

REMOVED = {
    "create_venv": create_venv,
    "install_into_venv": install_into_venv,
    "configure_pip": configure_pip,
    "share_venv_logs": share_venv_logs,
}


@pytest.mark.parametrize("name", sorted(REMOVED))
def test_removed_helper_raises_with_pointer(name):
    with pytest.raises(LinuxCoreError) as exc:
        REMOVED[name]("/opt/venvs/x", run_as="svc")
    msg = str(exc.value)
    assert name in msg
    assert "runspec-console" in msg
    assert "0.20.0" in msg


def test_removed_helpers_not_advertised():
    assert not set(REMOVED) & set(runspec_linux_core.__all__)
