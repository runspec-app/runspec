"""Microsoft Graph integration for runspec-windows (Outlook + Teams).

Pure HTTP via ``httpx`` + delegated auth via ``msal`` device-code flow — no
pywin32 and no client secret. The optional dependencies live behind the
``[graph]`` extra; importing this package never requires them.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base error for Graph runnables (rendered as a JSON ``error`` payload)."""


class GraphDepError(GraphError):
    """The optional ``[graph]`` dependencies (msal/httpx) are not installed."""


class GraphConfigError(GraphError):
    """No Azure client id configured (RUNSPEC_GRAPH_CLIENT_ID / graph.toml)."""


class GraphAuthError(GraphError):
    """No cached credentials — the user must run ``graph-login`` first."""
