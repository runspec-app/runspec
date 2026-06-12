"""Discoverability audiences for runnables (the `discoverable` keyword).

`discoverable` declares *which audiences* may discover a runnable, orthogonal to
`serve` (which declares *the context*). It is metadata only — it adds no runtime
behaviour to the runnable; the surfaces (`runspec serve`, `runspec local`, and a
future self-service catalog) read it to decide what to expose to whom.

Canonical form is a list of audience strings. The TOML value is normalised:

    absent / true          → ["operator"]          (the default; operator-visible)
    false                  → []                     (hidden from all surfaces)
    ["operator", ...]      → as written (validated)

`self-service` is never implied — a runnable reaches a visitor-facing catalog only
by listing it explicitly, so the default never exposes a runnable to untrusted
users.
"""

from __future__ import annotations

from typing import Any

#: The audiences a runnable may be discoverable to.
OPERATOR = "operator"
SELF_SERVICE = "self-service"
AUDIENCES: tuple[str, ...] = (OPERATOR, SELF_SERVICE)


def normalise_discoverable(value: Any) -> list[str]:
    """Return the canonical audience list for a raw `discoverable` value.

    Lenient — mirrors the rest of the loader: unknown shapes fall back to the
    default (operator-visible). Strict validation is `validate_discoverable`,
    surfaced by `runspec serve`/`local`.
    """
    if value is None or value is True:
        return [OPERATOR]
    if value is False:
        return []
    if isinstance(value, list):
        # Preserve order, drop duplicates; leave unknowns for the validator.
        seen: dict[str, None] = {}
        for item in value:
            if isinstance(item, str):
                seen.setdefault(item, None)
        return list(seen)
    return [OPERATOR]


def validate_discoverable(value: Any) -> list[str]:
    """Return human-readable error messages for an invalid `discoverable` value."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, list):
        if not value:
            return ["discoverable = [] is invalid — write false to hide everywhere, or omit to default to true"]
        unknown = [a for a in value if a not in AUDIENCES]
        if unknown:
            return [f"discoverable contains unknown audience(s) {unknown!r} — valid values are 'operator' and 'self-service'"]
        return []
    return [f"discoverable must be true, false, or a list of audiences ('operator', 'self-service'), got {type(value).__name__}"]


def is_discoverable_to(value: Any, audience: str) -> bool:
    """Return True if a runnable with this `discoverable` value reaches `audience`."""
    return audience in normalise_discoverable(value)
