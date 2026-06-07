"""_cli.py — small helpers shared by the ``main_*`` CLI wrappers.

`runspec` returns each argument as an ``Arg`` wrapper, not a bare value: an unset
optional arg is an ``Arg`` whose ``.value`` is ``None`` (it is *not* Python
``None`` and stringifies to ``"None"``). These helpers read ``.value`` so unset
optionals collapse to ``None`` / ``{}`` / ``False`` as you'd expect.
"""

from __future__ import annotations

from typing import Any

from runspec_snow.client import SnowError


def _value(arg: Any) -> Any:
    """Return the underlying value of a runspec ``Arg`` (or the arg as-is)."""
    return getattr(arg, "value", arg)


def opt(arg: Any) -> str | None:
    """Return a string for an optional arg, or ``None`` when unset/empty."""
    value = _value(arg)
    if value is None:
        return None
    text = str(value)
    return text or None


def flag(arg: Any) -> bool:
    """Return the boolean value of a flag arg."""
    return bool(_value(arg))


def parse_field_pairs(pairs: Any) -> dict[str, Any]:
    """Parse repeated ``field=value`` CLI inputs into a dict.

    ``pairs`` is a ``multiple = true`` arg (an iterable of strings, or unset).
    Raises :class:`SnowError` on a token missing ``=``.
    """
    values = _value(pairs)
    result: dict[str, Any] = {}
    if not values:
        return result
    for item in values:
        token = str(item)
        if "=" not in token:
            raise SnowError(f"Invalid --field {token!r} — expected field=value")
        key, value = token.split("=", 1)
        result[key.strip()] = value
    return result
