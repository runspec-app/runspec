"""
compose.py — resolve ``include`` and ``shared-args`` in a parsed runspec.toml.

Two authoring features that let a big ``runspec.toml`` be split up and stop
repeating itself. Both are resolved here, on the raw TOML dict, *before* the
loader normalises it, so every later stage (inference, validation, ``--help``,
``serve``, stubs, emit, the console) sees an ordinary single-file spec.

``include``
    A runnable or command table may say ``include = "path/to/file.toml"``. The
    file holds that table's body (``description``, ``args``, ``commands``, …,
    with no ``[runnable.commands.x]`` prefix) and is merged into the table. The
    path is relative to the file that names it and must stay inside the
    directory holding ``runspec.toml``. Included files may include others; a
    cycle is an error, and the chain is capped at ``MAX_INCLUDE_DEPTH`` files.
    A key set both in the table and in the included file is an error.

``shared-args`` / ``use``
    ``[config.shared-args]``, ``[<runnable>.shared-args]`` and
    ``[<…>.commands.<c>.shared-args]`` define args once. An arg written as
    ``project = {use = "project"}`` takes that definition; any other keys next
    to ``use`` override it. The nearest definition wins (the command's own,
    then each parent up to the runnable, then ``[config]``). A definition can't
    itself contain ``use``.

The functions are pure: file access goes through a ``reader`` callback that
takes a path relative to the ``runspec.toml`` directory (POSIX ``/``
separators) and returns the file's text, or ``None`` when it doesn't exist —
so the console can resolve a spec it fetched over SSH without a filesystem.
"""

from __future__ import annotations

import copy
import difflib
import posixpath
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

Reader = Callable[[str], "str | None"]

ROOT_FILE = "runspec.toml"
MAX_INCLUDE_DEPTH = 8
SHARED_ARGS_KEY = "shared-args"


def resolve(data: dict[str, Any], reader: Reader | None = None, source: str = ROOT_FILE) -> dict[str, Any]:
    """Return a copy of ``data`` with every ``include`` and ``use`` resolved and
    every ``shared-args`` table removed.

    A file that uses neither feature comes back equal to ``data``.

    Args:
        data:   the parsed ``runspec.toml``.
        reader: reads an included file by its package-relative POSIX path;
                returns ``None`` when the file doesn't exist. Without a reader
                an ``include`` is an error.
        source: the package-relative name of the file ``data`` came from.

    Raises:
        ValueError: a bad include path, a missing or malformed included file,
            a cycle, a key defined twice, or an unknown ``use`` name.
    """
    out = copy.deepcopy(data)
    for name, table in out.items():
        if name == "config" or not isinstance(table, dict):
            continue
        _resolve_includes(table, f"[{name}]", source, reader, (source,))
    _resolve_uses(out)
    return out


def has_include(text: str) -> bool:
    """Cheap textual check: could this runspec.toml text use ``include``?

    Used by the console to decide whether a scanned file needs its fragments.
    False positives are harmless (a key named ``include`` inside ``args`` …).
    """
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("include") and stripped[len("include") :].lstrip().startswith("="):
            return True
    return False


def fs_reader(root: Path) -> Reader:
    """A reader over the real filesystem, rooted at the ``runspec.toml`` directory.

    Refuses a path that resolves outside ``root`` (a symlink pointing out of the
    package), so a wheel can only include its own files.
    """
    base = root.resolve()

    def read(rel: str) -> str | None:
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"✗  include '{rel}' resolves outside the package directory ({base}).")
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    return read


# ── include ────────────────────────────────────────────────────────────────────


def _include_path(value: Any, cur_file: str, where: str) -> str:
    """The package-relative POSIX path an ``include`` value points at."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'✗  {where} include must be a path string, e.g. include = "commands/deploy.toml".')
    raw = value.strip()
    if "\\" in raw:
        raise ValueError(f"✗  {where} include '{raw}': use '/' in include paths, not '\\'.")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError(f"✗  {where} include '{raw}' must be a relative path inside the package directory.")
    if not raw.endswith(".toml"):
        raise ValueError(f"✗  {where} include '{raw}' must name a .toml file.")
    rel = posixpath.normpath(posixpath.join(posixpath.dirname(cur_file), raw))
    if rel == ".." or rel.startswith("../"):
        raise ValueError(f"✗  {where} include '{raw}' (in {cur_file}) points outside the package directory.")
    return rel


def _read_fragment(rel: str, reader: Reader | None, where: str, cur_file: str) -> dict[str, Any]:
    if reader is None:
        raise ValueError(f"✗  {where} include '{rel}' can't be read here (no file access).")
    text = reader(rel)
    if text is None:
        raise ValueError(f"✗  {where} include '{rel}' (named in {cur_file}) was not found.\n   Is the file shipped in the package?")
    try:
        frag = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"✗  {rel} is not valid TOML: {e}") from e
    return frag


def _resolve_includes(
    table: dict[str, Any],
    where: str,
    cur_file: str,
    reader: Reader | None,
    stack: tuple[str, ...],
) -> None:
    """Merge every ``include`` into ``table`` (in place), then recurse into its commands.

    ``cur_file`` is the file ``table`` was written in; ``stack`` is the chain of
    files from ``runspec.toml`` down to here, for cycle detection.
    """
    origin = dict.fromkeys(table, cur_file)  # key → file it came from
    file = cur_file
    while "include" in table:
        rel = _include_path(table.pop("include"), file, where)
        if rel in stack:
            chain = " → ".join((*stack[stack.index(rel) :], rel))
            raise ValueError(f"✗  {where} include cycle: {chain}")
        if len(stack) > MAX_INCLUDE_DEPTH:
            raise ValueError(f"✗  {where} includes are nested more than {MAX_INCLUDE_DEPTH} files deep: {' → '.join((*stack, rel))}")
        frag = _read_fragment(rel, reader, where, file)
        dupes = sorted(k for k in frag if k in table)
        if dupes:
            keys = ", ".join(dupes)
            raise ValueError(f"✗  {where} sets {keys} both in {origin[dupes[0]]} and in its include {rel}.\n   Set each key in one place.")
        table.update(frag)
        origin.update(dict.fromkeys(frag, rel))
        stack = (*stack, rel)
        file = rel

    commands = table.get("commands")
    if isinstance(commands, dict):
        cmd_file = origin.get("commands", file)
        for name, sub in commands.items():
            if isinstance(sub, dict):
                _resolve_includes(sub, f"{where[:-1]}.commands.{name}]", cmd_file, reader, stack)


# ── shared-args / use ──────────────────────────────────────────────────────────


def _shared_defs(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"✗  {where} must be a table of arg definitions.")
    defs: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        definition = value if isinstance(value, dict) else {"default": value}
        if "use" in definition:
            raise ValueError(f"✗  {where} {name}: a shared arg can't itself use another one.")
        defs[name] = definition
    return defs


def _resolve_uses(data: dict[str, Any]) -> None:
    config = data.get("config")
    scopes: list[dict[str, dict[str, Any]]] = []
    if isinstance(config, dict):
        scopes.append(_shared_defs(config.pop(SHARED_ARGS_KEY, None), f"[config.{SHARED_ARGS_KEY}]"))
    for name, table in data.items():
        if name == "config" or not isinstance(table, dict):
            continue
        _resolve_uses_table(table, f"[{name}]", scopes)


def _resolve_uses_table(table: dict[str, Any], where: str, scopes: list[dict[str, dict[str, Any]]]) -> None:
    if SHARED_ARGS_KEY in table:
        scopes = [*scopes, _shared_defs(table.pop(SHARED_ARGS_KEY), f"{where[:-1]}.{SHARED_ARGS_KEY}]")]
    args = table.get("args")
    if isinstance(args, dict):
        for arg_name, value in args.items():
            if isinstance(value, dict) and "use" in value:
                args[arg_name] = _use(value, arg_name, where, scopes)
    commands = table.get("commands")
    if isinstance(commands, dict):
        for name, sub in commands.items():
            if isinstance(sub, dict):
                _resolve_uses_table(sub, f"{where[:-1]}.commands.{name}]", scopes)


def _use(value: dict[str, Any], arg_name: str, where: str, scopes: list[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    ref = value["use"]
    if not isinstance(ref, str) or not ref:
        raise ValueError(f'✗  {where} arg {arg_name}: use must name a shared arg, e.g. {{use = "{arg_name}"}}.')
    for defs in reversed(scopes):
        if ref in defs:
            merged: dict[str, Any] = copy.deepcopy(defs[ref])
            merged.update({k: v for k, v in value.items() if k != "use"})
            return merged
    known = sorted({n for defs in scopes for n in defs})
    hint = ""
    close = difflib.get_close_matches(ref, known, n=1)
    if close:
        hint = f"\n   Did you mean '{close[0]}'?"
    elif known:
        hint = f"\n   Shared args in scope: {', '.join(known)}"
    else:
        hint = f"\n   No [{SHARED_ARGS_KEY}] table is in scope here."
    raise ValueError(f"✗  {where} arg {arg_name}: unknown shared arg '{ref}'.{hint}")
