"""runspec-logops-core — pure-Python log-condensing + code-mapping helpers.

This package has **no dependency on runspec** and ships **no runspec.toml and no
entry points**, so installing it exposes the helper functions for import without
surfacing any runnables (it is invisible to ``runspec local`` / ``runspec serve``
discovery). ``runspec-logops`` depends on it and wraps each helper in a runnable;
a private (e.g. Nexus-hosted) package can instead import these helpers directly,
bake in corporate defaults/paths as plain params, and ship its own runnables —
so only the wrapped runnables ever surface in the venv.

Each function returns plain data and *raises* on failure (see
``runspec_logops_core.errors``).
"""

from runspec_logops_core.bundle import build_bundle
from runspec_logops_core.codemap import map_trace_to_sources
from runspec_logops_core.digest import summarize_log
from runspec_logops_core.errors import LogopsCoreError, SourceNotFoundError
from runspec_logops_core.signatures import detect_level, error_signature, normalize_line

__all__ = [
    # errors
    "LogopsCoreError",
    "SourceNotFoundError",
    # signatures
    "normalize_line",
    "error_signature",
    "detect_level",
    # digest
    "summarize_log",
    # codemap
    "map_trace_to_sources",
    # bundle
    "build_bundle",
]
