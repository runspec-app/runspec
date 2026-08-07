"""mdconvert.py — best-effort Markdown → Confluence storage (XHTML) conversion.

Confluence's REST create/update API has no ``markdown`` representation, so when a
caller asks for ``--content-format markdown`` we convert client-side to the
``storage`` representation here. ``python-markdown`` with ``output_format="xhtml"``
emits self-closed tags (``<br />``, ``<hr />``) so the result is well-formed XHTML,
which Confluence storage requires.

This is deliberately basic: headings, paragraphs, lists, tables, fenced code,
links, and emphasis convert cleanly; Confluence-specific macros are not produced.
"""

from __future__ import annotations

import markdown as _markdown

_EXTENSIONS = ["fenced_code", "tables", "sane_lists"]


def markdown_to_storage(text: str) -> str:
    """Convert Markdown to Confluence storage-format XHTML (best-effort)."""
    return _markdown.markdown(text, output_format="xhtml", extensions=_EXTENSIONS)
