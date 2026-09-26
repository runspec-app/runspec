"""resolve.py — shared name-resolution helpers (slugs, fuzzy suggestions).

Admin and request-type helpers accept human names (a role name, a request type
name, a portal field label) as well as ids. When a name doesn't resolve, the
error carries the closest matches so an agent can correct itself in one step
instead of making a separate discovery call.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable


def suggest(wanted: str, candidates: Iterable[str], *, n: int = 3) -> str:
    """Return ``" — did you mean 'a', 'b'?"`` (or ``""``) for an unresolved name."""
    by_fold: dict[str, str] = {}
    for c in candidates:
        if c:
            by_fold.setdefault(c.casefold(), c)  # first spelling wins
    close = difflib.get_close_matches(wanted.casefold(), list(by_fold), n=n, cutoff=0.5)
    if not close:
        return ""
    return " — did you mean " + ", ".join(repr(by_fold[c]) for c in close) + "?"


def slugify(text: str, *, max_len: int = 48) -> str:
    """Lower-case, hyphen-separated identifier safe as a runspec arg / verb name."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    if not slug:
        slug = "field"
    if slug[0].isdigit():
        slug = f"f-{slug}"
    return slug[:max_len].rstrip("-")


def split_list(value: object) -> list[object]:
    """Normalise a list-ish value: a list/tuple as-is, a comma string split, a scalar wrapped."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]
