"""JSON encoding that never fails on a log or audit value.

Runnables (and the libraries they call) pass all sorts of objects as logging
``extra`` fields and audit ``target``/``detail`` values: enums, paths,
datetimes, sets, bytes, SDK objects. Plain ``json.dumps`` raises on every one of
them, and inside a logging handler that means the record is lost with a
``--- Logging error ---`` block on stderr.

:func:`dumps` turns them into the closest plain JSON value instead:

- an ``Enum`` member → its ``.value`` (``SessionEventType.SESSION_IDLE`` →
  ``"session.idle"``; ``str``/``int`` enums already encode as their value)
- ``datetime``/``date``/``time`` → ISO 8601
- a path (``os.PathLike``) → its string
- ``set``/``frozenset`` → a list
- ``bytes``/``bytearray`` → text, undecodable bytes replaced
- anything else → ``str(obj)``

Dict keys JSON can't hold (an enum, a tuple) become strings, and a container
that contains itself becomes ``"<circular>"``.
"""

from __future__ import annotations

import datetime
import enum
import json
import os
from typing import Any

__all__ = ["default", "dumps", "plain"]

_MAX_DEPTH = 100


def default(obj: Any) -> Any:
    """``json.dumps(default=…)`` hook: the plain JSON value for *obj*."""
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, os.PathLike):
        path = os.fspath(obj)
        return path.decode("utf-8", "replace") if isinstance(path, bytes) else path
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", "replace")
    return _text(obj)


def plain(obj: Any) -> Any:
    """*obj* with an enum member replaced by its value — for ``key=value`` text."""
    return obj.value if isinstance(obj, enum.Enum) else obj


def dumps(obj: Any, **kwargs: Any) -> str:
    """``json.dumps`` with :func:`default`, which does not raise on odd values."""
    kwargs.setdefault("default", default)
    try:
        return json.dumps(obj, **kwargs)
    except (TypeError, ValueError, RecursionError):
        # Non-string keys, a circular container, or a default that returned
        # something still unencodable: clean the structure and try again.
        return json.dumps(_clean(obj, set(), 0), **kwargs)


def _text(obj: Any) -> str:
    try:
        return str(obj)
    except Exception:  # noqa: BLE001 — a broken __str__ must not lose the record
        try:
            return repr(obj)
        except Exception:  # noqa: BLE001
            return f"<{type(obj).__name__}>"


def _key(key: Any) -> Any:
    if key is None or isinstance(key, (str, int, float, bool)):
        return key
    value = default(key)
    return value if isinstance(value, (str, int, float, bool)) else _text(value)


def _clean(obj: Any, seen: set[int], depth: int) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if depth >= _MAX_DEPTH:
        return "<too deep>"
    if isinstance(obj, (dict, list, tuple, set, frozenset)):
        if id(obj) in seen:
            return "<circular>"
        seen = seen | {id(obj)}
        if isinstance(obj, dict):
            return {_key(k): _clean(v, seen, depth + 1) for k, v in obj.items()}
        return [_clean(v, seen, depth + 1) for v in obj]
    return _clean(default(obj), seen, depth + 1)
