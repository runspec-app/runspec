"""runspec-o365 — Microsoft 365 (Outlook / Teams / Calendar / OneDrive) runnables.

Pure HTTP via ``httpx`` + delegated auth via ``msal`` device-code flow — no
pywin32 and no client secret. These runnables were split out of
``runspec-windows`` so a console / fleet where Microsoft Graph is unusable
(e.g. blocked by tenant policy) doesn't carry them; install this package explicitly to
get them.

Public API (parallels runspec-windows' former re-exports): ``graph_get`` and
``get_token`` let other wrapper runnables reuse the authenticated Microsoft
Graph client without re-implementing the device-code flow.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base error for Graph runnables (rendered as a JSON ``error`` payload)."""


class GraphDepError(GraphError):
    """The msal/httpx dependencies are not installed (reinstall runspec-o365)."""


class GraphConfigError(GraphError):
    """No Azure client id configured (RUNSPEC_GRAPH_CLIENT_ID / graph.toml)."""


class GraphAuthError(GraphError):
    """No cached credentials — the user must run ``graph-login`` first."""


# Re-export the authenticated client after the exceptions are defined (auth.py /
# client.py import the exceptions from this package during their own import).
from runspec_o365.auth import get_token  # noqa: E402
from runspec_o365.client import graph_get  # noqa: E402

__all__ = [
    "GraphError",
    "GraphDepError",
    "GraphConfigError",
    "GraphAuthError",
    "graph_get",
    "get_token",
]
