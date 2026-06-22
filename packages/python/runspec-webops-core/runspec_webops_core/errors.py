"""Exceptions raised by the webops helpers.

The wrappers in ``runspec-linux`` / ``runspec-windows`` catch these to reproduce
the CLI/agent behaviour (a connect failure exits differently from a bad result).
"""

from __future__ import annotations


class WebopsCoreError(Exception):
    """Base class for all runspec-webops-core failures."""


class ConnectError(WebopsCoreError):
    """A TLS/HTTP connection could not be established (refused, timed out, reset)."""


class ResolveError(WebopsCoreError):
    """A DNS name could not be resolved."""
