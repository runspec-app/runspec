"""envfile.py — pure helpers for the console's ``.runspec_env`` manager.

A venv's ``{venv}/.runspec_env`` is the dotenv file core loads into
``os.environ`` before every run (``runspec.env``; existing variables win). The
console edits it *by key*: a change is ``set`` / ``unset`` applied to the text,
so comments, blank lines and the order of untouched lines survive, and a bulk
change across many venvs never overwrites the keys it doesn't name.

Parsing mirrors ``runspec.env._parse_dotenv`` exactly (strip, ``#`` comments,
first ``=`` splits, one pair of matching outer quotes stripped, last duplicate
wins), so what the console shows is what a runnable reads.

No I/O here: :mod:`bridge_envfile` reads and writes over SSH (or locally) via
:mod:`_envfile_worker`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

FILE_NAME = ".runspec_env"
ABSENT = "absent"

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Names that read like a secret. A value under one of these is masked in the UI
# and only sent to the frontend on an explicit reveal.
_SECRET_RE = re.compile(
    r"(PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|(^|_)PAT($|_)|API_?KEY|PRIVATE|"
    r"CREDENTIAL|(^|_)AUTH($|_)|COOKIE|SESSION|(^|_)KEY$)",
    re.IGNORECASE,
)


class EnvFileError(ValueError):
    """A change that can't be written (bad key / value)."""


@dataclass
class Entry:
    key: str
    value: str
    line: int  # 1-based line of the effective (last) assignment
    duplicates: int = 0  # earlier assignments of the same key, shadowed


@dataclass
class Parsed:
    entries: list[Entry] = field(default_factory=list)
    comments: int = 0
    ignored: list[int] = field(default_factory=list)  # lines core skips (no '=')

    def values(self) -> dict[str, str]:
        return {e.key: e.value for e in self.entries}


def _split(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    key, _, value = s.partition("=")
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return (key, value) if key else None


def parse(text: str) -> Parsed:
    out = Parsed()
    seen: dict[str, Entry] = {}
    order: list[str] = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        s = line.strip()
        if s.startswith("#"):
            out.comments += 1
            continue
        kv = _split(line)
        if kv is None:
            if s:
                out.ignored.append(i)
            continue
        key, value = kv
        if key in seen:
            prev = seen[key]
            seen[key] = Entry(key, value, i, prev.duplicates + 1)
        else:
            seen[key] = Entry(key, value, i)
            order.append(key)
    out.entries = [seen[k] for k in order]
    return out


def validate_key(key: str) -> str:
    k = (key or "").strip()
    if not _KEY_RE.match(k):
        raise EnvFileError(
            f"{key!r} is not a variable name (letters, digits and _, not starting with a digit)."
        )
    return k


def validate_value(key: str, value: Any) -> str:
    v = "" if value is None else str(value)
    if "\n" in v or "\r" in v or "\0" in v:
        raise EnvFileError(f"{key}: a value must be one line.")
    return v


def format_line(key: str, value: str) -> str:
    """``KEY=value``, quoted only when core would otherwise change the value
    (outer whitespace, or one pair of matching outer quotes it would strip)."""
    needs = value != value.strip() or (
        len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'")
    )
    if needs:
        q = "'" if '"' in value and "'" not in value else '"'
        return f"{key}={q}{value}{q}"
    return f"{key}={value}"


def normalize_changes(
    set_: dict[str, Any] | None, unset: list[Any] | None
) -> tuple[dict[str, str], list[str]]:
    sets = {validate_key(k): validate_value(k, v) for k, v in (set_ or {}).items()}
    drops = [validate_key(str(k)) for k in (unset or [])]
    both = sorted(set(sets) & set(drops))
    if both:
        raise EnvFileError(f"Both set and removed: {', '.join(both)}.")
    return sets, list(dict.fromkeys(drops))


def apply_changes(text: str, set_: dict[str, str], unset: list[str]) -> str:
    """``text`` with ``set_`` written and ``unset`` removed, everything else
    untouched. A set key replaces its first assignment in place (later
    duplicates — shadowing it — are dropped); a new key is appended."""
    sets, drops = normalize_changes(set_, unset)
    lines = (text or "").splitlines()
    out: list[str] = []
    written: set[str] = set()
    for line in lines:
        kv = _split(line)
        key = kv[0] if kv else None
        if key is not None and key in drops:
            continue
        if key is not None and key in sets:
            if key in written:
                continue
            out.append(format_line(key, sets[key]))
            written.add(key)
            continue
        out.append(line)
    out.extend(format_line(k, sets[k]) for k in sets if k not in written)
    if not out:
        return ""
    return "\n".join(out) + "\n"


def changed_keys(before: str, after: str) -> dict[str, list[str]]:
    a, b = parse(before).values(), parse(after).values()
    return {
        "added": sorted(k for k in b if k not in a),
        "changed": sorted(k for k in b if k in a and a[k] != b[k]),
        "removed": sorted(k for k in a if k not in b),
    }


def is_secret_name(key: str) -> bool:
    return bool(_SECRET_RE.search(key or ""))


def world_readable(mode: str) -> bool:
    """``"0644"`` → True (group or others can read)."""
    try:
        return bool(int(mode, 8) & 0o044)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Venv paths
# --------------------------------------------------------------------------- #


def is_windows_path(path: str) -> bool:
    return (
        "\\" in path
        or path.lower().endswith(".exe")
        or bool(re.match(r"^[A-Za-z]:", path))
    )


def local_root(runspec_path: str) -> str:
    """``…/venv/bin/runspec`` or ``…\\venv\\Scripts\\runspec.exe`` → the venv root."""
    return str(Path(runspec_path).parent.parent)


def remote_root(runspec_path: str) -> str:
    """POSIX venv root for a remote runspec path (raises for Windows/relative)."""
    p = (runspec_path or "").strip()
    if not p or is_windows_path(p):
        raise EnvFileError("Only Linux/macOS remote venvs are supported for now.")
    if not p.startswith("/"):
        raise EnvFileError(f"{runspec_path!r} is not an absolute path.")
    norm = posixpath.normpath(p)
    if posixpath.basename(norm) in ("runspec", "runspec.exe"):
        norm = posixpath.dirname(norm)
    if posixpath.basename(norm) == "bin":
        norm = posixpath.dirname(norm)
    if norm in ("/", ""):
        raise EnvFileError(f"Can't find the venv root for {runspec_path!r}.")
    return norm


def remote_python(root: str) -> str:
    return posixpath.join(root, "bin", "python")


def venv_label(root: str) -> str:
    if is_windows_path(root):
        return PureWindowsPath(root).name
    return posixpath.basename(root.rstrip("/")) or root


# --------------------------------------------------------------------------- #
# Which runnables read which variable
# --------------------------------------------------------------------------- #


def _auto_key(runnable: str, arg: str) -> str:
    return f"RUNSPEC_{runnable.upper().replace('-', '_')}_ARG_{arg.upper().replace('-', '_')}"


def _walk_args(rec: dict[str, Any], path: str):
    for a in rec.get("args") or []:
        if isinstance(a, dict) and a.get("name") and not a.get("inherited"):
            yield path, a
    for sub_name, sub in (rec.get("commands") or {}).items():
        if isinstance(sub, dict):
            yield from _walk_args(sub, f"{path} {sub_name}".strip())


def readers(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``{VAR: [{runnable, command, arg, via, password, description}]}`` — every
    env variable the given (discovered) runnables read an arg from: the
    automatic ``RUNSPEC_<RUNNABLE>_ARG_<ARG>`` and any declared ``env`` alias."""
    out: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        name = str(rec.get("name") or "")
        if not name:
            continue
        for cmd, a in _walk_args(rec, ""):
            arg = str(a["name"])
            base = {
                "runnable": name,
                "command": cmd,
                "arg": arg,
                "password": a.get("type") == "password",
                "description": str(a.get("description") or "")[:200],
            }
            seen: set[str] = set()
            aliases = a.get("env") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            for var, via in [(_auto_key(name, arg), "auto")] + [
                (str(x), "alias") for x in aliases
            ]:
                if not var or var in seen:
                    continue
                seen.add(var)
                rows = out.setdefault(var, [])
                row = {**base, "via": via}
                if row not in rows:
                    rows.append(row)
    return out


def overrides(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Runnables that name their own ``runspec_env`` file in runspec.toml (so
    they don't read the venv's default ``.runspec_env``)."""
    out: list[dict[str, str]] = []
    for rec in records:
        raw = rec.get("rawSpec") or {}
        f = raw.get("runspec_env") if isinstance(raw, dict) else None
        if f:
            out.append({"runnable": str(rec.get("name") or ""), "file": str(f)})
    return out


def secret_keys(
    keys: list[str], reader_map: dict[str, list[dict[str, Any]]]
) -> set[str]:
    return {
        k
        for k in keys
        if is_secret_name(k) or any(r.get("password") for r in reader_map.get(k, []))
    }


def entry_rows(
    text: str, reader_map: dict[str, list[dict[str, Any]]], *, reveal: bool = False
) -> dict[str, Any]:
    """The table the UI renders: one row per key with readers, secret values
    masked (``value`` empty, ``masked`` true) unless ``reveal``."""
    parsed = parse(text)
    secrets = secret_keys([e.key for e in parsed.entries], reader_map)
    rows = []
    for e in parsed.entries:
        secret = e.key in secrets
        masked = secret and not reveal and e.value != ""
        rows.append(
            {
                "key": e.key,
                "value": "" if masked else e.value,
                "secret": secret,
                "masked": masked,
                "line": e.line,
                "duplicates": e.duplicates,
                "readers": reader_map.get(e.key, []),
            }
        )
    present = {e.key for e in parsed.entries}
    suggestions = [
        {
            "key": var,
            "secret": var in secret_keys([var], reader_map),
            "readers": rs,
        }
        for var, rs in sorted(reader_map.items())
        if var not in present
    ]
    return {
        "entries": rows,
        "comments": parsed.comments,
        "ignored_lines": parsed.ignored,
        "suggestions": suggestions,
        "has_secrets": any(r["secret"] and r["key"] in present for r in rows),
    }
