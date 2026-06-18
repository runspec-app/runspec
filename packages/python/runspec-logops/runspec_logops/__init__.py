"""runspec-logops — log-condensing + code-mapping runnables.

The pure logic lives in the dependency-free ``runspec-logops-core`` package; the
modules here wrap each helper in a runspec runnable. The helper functions are
re-exported for convenience and back-compat (e.g. ``from runspec_logops import
summarize_log``). To import the helpers *without* surfacing any runnables in a
venv — the corporate-wrapping path — depend on ``runspec-logops-core`` directly
instead.
"""

from runspec_logops_core import (
    build_bundle,
    map_trace_to_sources,
    summarize_log,
)

__all__ = [
    "summarize_log",
    "map_trace_to_sources",
    "build_bundle",
]
