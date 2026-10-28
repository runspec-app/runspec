"""Stand-ins for the venv helpers removed in 0.14.0.

``create_venv``, ``install_into_venv``, ``configure_pip`` and
``share_venv_logs`` moved to runspec-console (``runspec_console.provision``).
runspec-linux 0.19.0 and older import all four from this package when it
loads, so without these names every one of its runnables failed with
``cannot import name 'configure_pip'``. Importing works again; calling one
raises ``LinuxCoreError`` saying where the job went.
"""

from typing import Any, NoReturn

from runspec_linux_core.errors import LinuxCoreError

_MOVED = (
    "{name} was removed in runspec-linux-core 0.14.0. Provision venvs from "
    "runspec-console (Settings > Jump Hosts > Provision venv), or use "
    "`python -m venv`, `pip install` and `runspec logs share --inbox`. "
    "If an older runspec-linux called this, upgrade it to 0.20.0 or later."
)


def _removed(name: str) -> NoReturn:
    raise LinuxCoreError(_MOVED.format(name=name))


def create_venv(*args: Any, **kwargs: Any) -> NoReturn:
    """Removed in 0.14.0; raises ``LinuxCoreError``."""
    _removed("create_venv")


def install_into_venv(*args: Any, **kwargs: Any) -> NoReturn:
    """Removed in 0.14.0; raises ``LinuxCoreError``."""
    _removed("install_into_venv")


def configure_pip(*args: Any, **kwargs: Any) -> NoReturn:
    """Removed in 0.14.0; raises ``LinuxCoreError``."""
    _removed("configure_pip")


def share_venv_logs(*args: Any, **kwargs: Any) -> NoReturn:
    """Removed in 0.14.0; raises ``LinuxCoreError``."""
    _removed("share_venv_logs")
