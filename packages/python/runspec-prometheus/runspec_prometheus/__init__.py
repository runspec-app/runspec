"""runspec-prometheus — Prometheus as a runspec runnable (the ``prometheus`` runnable).

Ported from the official
`Prometheus MCP server <https://github.com/prometheus/prometheus-mcp>`_. The pure
HTTP logic lives in ``runspec-prometheus-core`` (importable with **no** runspec
dependency); this package wraps each helper as a verb of the ``prometheus``
runnable and is what ``runspec local`` / ``runspec serve`` discover.

The core helpers are re-exported here for convenience, so a caller that already
depends on ``runspec-prometheus`` can ``from runspec_prometheus import query``
without also naming ``runspec-prometheus-core``.
"""

from runspec_prometheus_core import *  # noqa: F403
from runspec_prometheus_core import __all__  # noqa: F401
