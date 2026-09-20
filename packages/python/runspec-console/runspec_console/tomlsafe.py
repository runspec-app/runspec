"""Shared, audited TOML I/O for the console's hand-rolled config files.

Two recurring hazards live on the seam between the TypeScript editors and the
Python config files, and both have shipped launch-breaking bugs:

1. **Write side** — a serializer that interpolates a value straight into an
   f-string (``f'name = "{v}"'``) emits *invalid TOML* for any value carrying a
   ``"``/``\\``/newline, and a bare Python ``repr`` (``None``, ``[None]``) for a
   ``None`` or a list with a blank element. The bad file then wedges every later
   read. :func:`dump_scalar` / :func:`basic_string` are the one audited
   serializer — full TOML basic-string escaping (control chars as ``\\uXXXX``),
   ``bool`` checked before ``int`` (``bool`` is an ``int`` subclass), lists
   recursed, and an empty string for any value with no valid scalar form.

2. **Read side** — a config file read on the launch path
   (``Bridge.__init__``) or by a feature tab must never crash the app when it is
   malformed. :func:`quarantine_corrupt` moves an unreadable file aside to a
   timestamped ``.corrupt-*`` sidecar (the operator's data is preserved, not
   deleted) so the next save writes a clean file in its place.
   :func:`load_table_array` is the whole read for the ``[[section]]`` registry
   files: parse, quarantine-and-return-``[]`` on failure, else pull one
   array-of-tables key.

Every console config loader read at or near launch routes through here so the
class of bug that bricked the app on a bad ``runspec_hosts.toml`` cannot recur
per-file.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

_LOG = logging.getLogger(__name__)

# The TOML basic-string escapes (see the TOML spec). Anything else below 0x20
# (plus DEL) is emitted as \uXXXX so the written file always re-parses.
_TOML_STR_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def basic_string(s: str) -> str:
    """Serialize ``s`` as a quoted TOML basic string, escaping every character
    that would otherwise make the file fail to re-parse."""
    out: list[str] = []
    for ch in s:
        esc = _TOML_STR_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def dump_scalar(v: Any) -> str:
    """Serialize a scalar (or a flat list of scalars) to its TOML form.

    ``bool`` is checked before ``int`` because ``bool`` is an ``int`` subclass.
    A ``None`` — or any value with no valid TOML scalar form — becomes ``""``
    rather than a Python ``repr`` (``None``/``[None]``) that would corrupt the
    file and wedge every later load.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(dump_scalar(x) for x in v) + "]"
    if isinstance(v, str):
        return basic_string(v)
    if isinstance(v, (int, float)):
        return str(v)
    return '""'


def quarantine_corrupt(
    path: Path, err: Exception, *, log: logging.Logger | None = None
) -> None:
    """Move an unreadable config file aside so it stops crashing every read.

    The operator's data is preserved (renamed to a timestamped ``.corrupt-*``
    sidecar, not deleted) and the next save writes a clean file in its place.
    Never raises — a file that cannot even be moved aside is logged and the
    caller continues with its default.
    """
    log = log or _LOG
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    i = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{i}")
        i += 1
    try:
        path.replace(backup)
        log.error("config file %s was invalid (%s); moved to %s", path, err, backup)
    except OSError:
        log.error(
            "config file %s was invalid (%s) and could not be moved aside",
            path,
            err,
            exc_info=True,
        )


def load_table_array(
    path: Path, key: str, *, log: logging.Logger | None = None
) -> list[dict[str, Any]]:
    """Read a ``[[<key>]]`` array-of-tables registry file, tolerating corruption.

    Returns ``[]`` for a missing file, and for a malformed one quarantines it
    (see :func:`quarantine_corrupt`) and returns ``[]`` rather than raising — so
    a bad registry can never block launch or wedge the tab that reads it.
    """
    if not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError, ValueError) as e:
        quarantine_corrupt(path, e, log=log)
        return []
    value = data.get(key)
    return value if isinstance(value, list) else []
