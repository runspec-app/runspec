"""runspec-windows — Windows system admin + Microsoft 365 runnables for runspec.

Public API (parallels runspec-linux's ``nc_send``): ``graph_get`` and
``get_token`` let other wrapper runnables reuse the authenticated Microsoft
Graph client without re-implementing the device-code flow.

Importing this package never requires the optional ``[graph]`` dependencies —
``msal``/``httpx`` are imported lazily and only when a Graph call is made.
"""

from runspec_windows.graph.auth import get_token
from runspec_windows.graph.client import graph_get

__all__ = ["graph_get", "get_token"]
