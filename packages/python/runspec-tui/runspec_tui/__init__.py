"""runspec-tui — the runspec-console Forms view in a terminal, for the
runnables installed in this venv."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("runspec-tui")
except PackageNotFoundError:  # pragma: no cover — running from a source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
