"""errors.py — the exception type raised across runspec-confluence-core.

Every operation function raises :class:`ConfluenceError` on configuration
problems, invalid arguments, and non-2xx API responses (the underlying
``atlassian-python-api`` raises ``requests`` exceptions, translated to
``ConfluenceError`` at the operation boundary).
"""

from __future__ import annotations


class ConfluenceError(Exception):
    """Raised on config problems, invalid arguments, and Confluence API errors."""
