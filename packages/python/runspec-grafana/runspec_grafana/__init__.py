"""runspec-grafana — Grafana as a runspec runnable (the ``grafana`` runnable).

Ported from the read tools of the
`Grafana MCP server <https://github.com/grafana/mcp-grafana>`_. The pure HTTP
logic lives in ``runspec-grafana-core`` (importable with **no** runspec
dependency); this package wraps each helper as a verb of the ``grafana``
runnable and is what ``runspec local`` / ``runspec serve`` discover.

The core helpers are re-exported here for convenience, so a caller that already
depends on ``runspec-grafana`` can ``from runspec_grafana import search_dashboards``
without also naming ``runspec-grafana-core``.
"""

from runspec_grafana_core import *  # noqa: F403
from runspec_grafana_core import __all__  # noqa: F401
