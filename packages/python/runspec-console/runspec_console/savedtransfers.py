"""
savedtransfers.py — read/validate ``runspec_saved_transfers.toml``, the
saved-transfer registry, and the pure helpers a saved transfer runs on.

A **saved transfer** is a reusable "gather these files" scenario: a remote
source (a fleet host, every host in a group, or an ad-hoc SSH host), one or
more path *patterns*, an optional saved filter, and a local destination (the
current chat's workspace by default). Like a saved run it may declare open
*params* the operator (or agent) fills at run time, so one entry covers
"yesterday's app log on web-01" and "the 3rd of last month's" alike.

  [[saved_transfer]]
  id          = "app-logs"
  title       = "App logs for a day"
  host        = "web-01"               # exactly one of host / group / adhoc
  paths       = ["/var/log/app/app-{{day:%Y-%m-%d}}*.log"]
  latest      = 0                      # 0 = every match; N = newest N per pattern
  max_files   = 50                     # refuse (not truncate) past this many
  filter      = "id-lines"             # optional saved filter (runspec_filters.toml)
  filter_arg  = "{{txn}}"              # its {{arg}} value — a template too
  dest_dir    = ""                     # local folder; "" = the chat's workspace
  subdir      = "logs/{{day:%Y%m%d}}"  # optional, relative, under dest
  autonomy    = "confirm"              # agent gate: confirm | autonomous | manual

  [saved_transfer.params]              # open params — the runspec arg grammar
  day = { default = "yesterday", description = "today, yesterday, -3d or a date" }
  txn = { required = true }

An ad-hoc source names ``adhoc = "<hostname>"`` plus optional ``jump`` (a
configured host to tunnel through), ``user``, ``port`` and ``credential_id``
(a Credentials-tab id — the secret stays in the keychain, never in this file).

**Templates.** ``paths``, ``subdir``, ``dest_dir`` and ``filter_arg`` take
``{{name}}`` / ``{{name:FORMAT}}`` placeholders. ``name`` is a param, or one of
the built-ins ``today`` / ``yesterday`` / ``now`` / ``host`` (the source host).
With ``:FORMAT`` (strftime) the value is read as a date — ``today``,
``yesterday``, ``now``, ``-3d`` / ``+1d`` / ``-12h`` / ``-2w``, or an ISO date /
date-time — so a date picker, a typed "-1d" and the default "yesterday" all work.
A built-in date with no format renders ``%Y-%m-%d``.

**Wildcards.** After rendering, ``*`` / ``?`` / ``[...]`` in a path match on the
remote host (component by component over SFTP, like a shell glob: a leading
``.`` is matched only explicitly). A value substituted from a param is escaped
first, so it always matches literally. This is what lets a daily log such as
``app-2026-10-29.0.log`` match ``app-{{day:%Y-%m-%d}}*.log``.

The file **rides Config Sync** (in ``KNOWN_FILES``), merged by ``id`` like saved
runs — so it must never carry a secret.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import tomli_w

from . import config as config_mod
from . import runbookargs

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Serialises read-modify-write of the registry (re-entrant, mirrors savedruns.py).
_LOCK = threading.RLock()

AUTONOMY_LEVELS = ("autonomous", "confirm", "manual")
DEFAULT_MAX_FILES = 50
# A hard ceiling on max_files — a scenario is for gathering a handful of logs,
# not mirroring a directory tree.
MAX_FILES_CEILING = 1000
# Directory listings one pattern may make while expanding wildcards.
MAX_LISTINGS = 500

BUILTINS = ("today", "yesterday", "now", "host")


@dataclass(frozen=True)
class SavedTransfer:
    """A parsed ``[[saved_transfer]]`` entry."""

    id: str
    title: str = ""
    description: str = ""
    # Exactly one of host / group / adhoc is set.
    host: str = ""
    group: str = ""
    adhoc: str = ""
    jump: str = ""
    user: str = ""
    port: int = 0
    credential_id: str = ""
    paths: list[str] = field(default_factory=list)
    latest: int = 0
    max_files: int = DEFAULT_MAX_FILES
    filter: str = ""
    filter_arg: str = ""
    dest_dir: str = ""
    subdir: str = ""
    autonomy: str = "confirm"
    params: dict[str, Any] = field(default_factory=dict)

    def source_summary(self) -> str:
        """One-line human description of where the files come from."""
        if self.host:
            return self.host
        if self.group:
            return f"group {self.group}"
        via = f" via {self.jump}" if self.jump else ""
        return f"{self.adhoc} (ad-hoc{via})"

    def dest_summary(self) -> str:
        base = self.dest_dir or "chat workspace"
        return f"{base}/{self.subdir}" if self.subdir else base


def _int(value: Any, what: str, sid: str) -> int:
    if value in (None, ""):
        return 0
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"saved transfer '{sid}': {what} must be a whole number")
    if out < 0:
        raise ValueError(f"saved transfer '{sid}': {what} can't be negative")
    return out


def parse_saved_transfer(data: dict[str, Any]) -> SavedTransfer:
    """Validate and parse one ``[[saved_transfer]]`` table.

    Raises ``ValueError`` with a human-first message on bad config: a missing
    id / paths, not exactly one source, a bad ``params`` table, an unknown
    placeholder, or an unsafe ``subdir``."""
    sid = str(data.get("id") or "").strip()
    if not sid:
        raise ValueError("saved transfer is missing an 'id'")

    host = str(data.get("host") or "").strip()
    group = str(data.get("group") or "").strip()
    adhoc = str(data.get("adhoc") or "").strip()
    if sum((bool(host), bool(group), bool(adhoc))) != 1:
        raise ValueError(
            f"saved transfer '{sid}' needs exactly one source: 'host' (a fleet "
            "host), 'group' (every host in a host group) or 'adhoc' (an SSH host "
            "that isn't in the fleet)"
        )
    if host == "local":
        raise ValueError(
            f"saved transfer '{sid}': the source must be a remote host — saved "
            "transfers gather files onto this machine"
        )
    jump = str(data.get("jump") or "").strip()
    user = str(data.get("user") or "").strip()
    credential_id = str(data.get("credential_id") or "").strip()
    port = _int(data.get("port"), "port", sid)
    if not adhoc and (jump or user or credential_id or port):
        raise ValueError(
            f"saved transfer '{sid}': jump / user / port / credential_id only "
            "apply to an 'adhoc' source"
        )

    raw_paths = data.get("paths")
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    paths = [str(p).strip() for p in (raw_paths or []) if str(p).strip()]
    if not paths:
        raise ValueError(f"saved transfer '{sid}' has no 'paths' to fetch")

    max_files = _int(data.get("max_files"), "max_files", sid) or DEFAULT_MAX_FILES
    if max_files > MAX_FILES_CEILING:
        raise ValueError(
            f"saved transfer '{sid}': max_files can be at most {MAX_FILES_CEILING}"
        )

    autonomy = str(data.get("autonomy") or "confirm").strip().lower()
    if autonomy not in AUTONOMY_LEVELS:
        raise ValueError(
            f"saved transfer '{sid}': autonomy must be one of "
            f"{', '.join(AUTONOMY_LEVELS)}"
        )

    params = dict(data.get("params") or {})
    if params:
        try:
            runbookargs.parse_runbook_args(params)
        except Exception as exc:
            raise ValueError(
                f"saved transfer '{sid}' has a bad params table: {exc}"
            ) from exc

    entry = SavedTransfer(
        id=sid,
        title=str(data.get("title") or "").strip(),
        description=str(data.get("description") or "").strip(),
        host=host,
        group=group,
        adhoc=adhoc,
        jump=jump,
        user=user,
        port=port,
        credential_id=credential_id,
        paths=paths,
        latest=_int(data.get("latest"), "latest", sid),
        max_files=max_files,
        filter=str(data.get("filter") or "").strip(),
        filter_arg=str(data.get("filter_arg") or ""),
        dest_dir=str(data.get("dest_dir") or "").strip(),
        subdir=str(data.get("subdir") or "").strip(),
        autonomy=autonomy,
        params=params,
    )

    # Every placeholder must name a param or a built-in — caught at save time so
    # a typo doesn't surface only when someone runs the scenario.
    known = {_norm(p) for p in params} | set(BUILTINS)
    for text in (*paths, entry.subdir, entry.dest_dir, entry.filter_arg):
        for name, _fmt in placeholders(text):
            if _norm(name) not in known:
                raise ValueError(
                    f"saved transfer '{sid}' uses {{{{{name}}}}} but has no param "
                    f"named '{name}' (built-ins: {', '.join(BUILTINS)})"
                )
    if entry.subdir and entry.subdir.replace("\\", "/").startswith("/"):
        raise ValueError(
            f"saved transfer '{sid}': subdir must be relative (use dest_dir for "
            "an absolute folder)"
        )
    return entry


def entry_to_row(entry: SavedTransfer) -> dict[str, Any]:
    """A parsed entry → the minimal TOML row (empty/default fields dropped)."""
    row: dict[str, Any] = {"id": entry.id}
    for key in ("title", "description", "host", "group", "adhoc", "jump", "user"):
        if getattr(entry, key):
            row[key] = getattr(entry, key)
    if entry.port:
        row["port"] = entry.port
    if entry.credential_id:
        row["credential_id"] = entry.credential_id
    row["paths"] = list(entry.paths)
    if entry.latest:
        row["latest"] = entry.latest
    if entry.max_files != DEFAULT_MAX_FILES:
        row["max_files"] = entry.max_files
    for key in ("filter", "filter_arg", "dest_dir", "subdir"):
        if getattr(entry, key):
            row[key] = getattr(entry, key)
    if entry.autonomy != "confirm":
        row["autonomy"] = entry.autonomy
    if entry.params:
        row["params"] = entry.params
    return row


def _norm(name: str) -> str:
    return str(name).replace("-", "_")


# ── values + templates ─────────────────────────────────────────────────────────


def resolve_values(
    entry: SavedTransfer, values: dict[str, Any] | None
) -> dict[str, Any]:
    """The effective param values for a run: declared defaults filled in, then
    ``values`` validated against the params table (required / choice / range /
    pattern / type, via the core runspec validator). A value for an undeclared
    param is rejected. Keys are returned normalised (``-`` → ``_``). Raises
    ``ValueError`` listing every problem."""
    values = {k: v for k, v in dict(values or {}).items() if v not in (None, "")}
    declared = {_norm(p): p for p in entry.params}
    unknown = sorted(v for v in values if _norm(v) not in declared)
    if unknown:
        raise ValueError(
            f"saved transfer '{entry.id}' has no param(s) named: {', '.join(unknown)}"
        )
    errors = runbookargs.validate_runbook_values(entry.params, values)
    if errors:
        raise ValueError("; ".join(errors))
    out: dict[str, Any] = {}
    for key, spec in entry.params.items():
        if isinstance(spec, dict) and spec.get("default") not in (None, ""):
            out[_norm(key)] = spec["default"]
    for key, val in values.items():
        out[_norm(key)] = val
    return out


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*(?::([^}]*))?\}\}")
_RELATIVE = re.compile(r"^([+-]\d+)\s*([hdw])$", re.IGNORECASE)


def placeholders(text: str) -> list[tuple[str, str | None]]:
    """``(name, format)`` for every ``{{name}}`` / ``{{name:FORMAT}}`` in text."""
    return [(m.group(1), m.group(2)) for m in _PLACEHOLDER.finditer(text or "")]


def parse_when(value: Any, now: datetime) -> datetime:
    """Read a param value as a point in time: ``today`` / ``yesterday`` /
    ``now``, a relative ``-3d`` / ``+1d`` / ``-12h`` / ``-2w`` (from now), or an
    ISO date / date-time (a trailing ``Z`` is accepted). Raises ``ValueError``."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    low = text.lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if low == "now":
        return now
    if low == "today":
        return midnight
    if low == "yesterday":
        return midnight - timedelta(days=1)
    rel = _RELATIVE.match(low)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2)
        if unit == "h":
            return now + timedelta(hours=n)
        return midnight + timedelta(days=n * (7 if unit == "w" else 1))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    raise ValueError(
        f"'{text}' isn't a date — use today, yesterday, -3d, or YYYY-MM-DD"
    )


def glob_escape(text: str) -> str:
    """Make ``text`` match only itself in a glob (``*`` → ``[*]`` …)."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def render(
    template: str,
    values: dict[str, Any],
    *,
    host: str = "",
    now: datetime | None = None,
    escape_glob: bool = False,
) -> str:
    """Substitute placeholders in ``template`` (see the module docstring).

    ``values`` are normalised param values (from :func:`resolve_values`);
    ``host`` fills the built-in ``{{host}}``. With ``escape_glob`` a substituted
    value is escaped so it matches literally inside a path pattern. Raises
    ``ValueError`` for an unknown name or a non-date value given a format."""
    now = now or datetime.now()

    def sub(m: re.Match[str]) -> str:
        name, fmt = m.group(1), m.group(2)
        key = _norm(name)
        if key in values:
            raw: Any = values[key]
        elif key in ("today", "yesterday", "now"):
            raw = key
            fmt = fmt or ("%Y-%m-%d %H%M%S" if key == "now" else "%Y-%m-%d")
        elif key == "host":
            raw = host
        else:
            raise ValueError(f"no value for {{{{{name}}}}}")
        if fmt:
            try:
                when = parse_when(raw, now)
            except ValueError as exc:
                raise ValueError(f"{name}: {exc}") from None
            out = when.strftime(fmt)
        else:
            out = str(raw)
        return glob_escape(out) if escape_glob else out

    return _PLACEHOLDER.sub(sub, template or "")


def safe_subdir(text: str) -> str:
    """Validate a rendered relative folder — no absolute path, no ``..``. Returns
    it with ``/`` separators and no empty segments ("" for none)."""
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    if text.replace("\\", "/").startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"subfolder '{text}' must be relative")
    if any(p == ".." for p in parts):
        raise ValueError(f"subfolder '{text}' can't contain '..'")
    return "/".join(parts)


# ── wildcard expansion (remote, injected I/O) ──────────────────────────────────


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int = 0
    mtime: int = 0


# listdir(dir) -> [(name, is_dir, size, mtime)]; stat(path) -> (is_dir, size, mtime) | None
ListDir = Callable[[str], list[tuple[str, bool, int, int]]]
Stat = Callable[[str], "tuple[bool, int, int] | None"]


def has_magic(text: str) -> bool:
    return bool(re.search(r"[*?\[]", text))


def _component_match(name: str, pat: str) -> bool:
    # Like a shell glob: a wildcard never matches a leading dot by accident.
    if name.startswith(".") and not pat.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, pat)


def _unescape(text: str) -> str:
    """A literal (no-magic) component with ``[*]``-style escapes undone."""
    return re.sub(r"\[([*?\[])\]", r"\1", text)


def expand_pattern(pattern: str, listdir: ListDir, stat: Stat) -> list[RemoteFile]:
    """Expand one rendered remote path pattern into the files it matches.

    Walks component by component: a literal component is appended; a wildcard
    component lists the directory and keeps matching entries (directories for
    an inner component, files for the last). A pattern with no wildcard is
    stat'ed so a missing file reports as "no match" rather than failing later.
    Raises ``ValueError`` past :data:`MAX_LISTINGS` directory listings."""
    pat = pattern.replace("\\", "/")
    if pat.startswith("/"):
        bases, rest = ["/"], pat[1:]
    elif re.match(r"^[A-Za-z]:/", pat):
        bases, rest = [pat[:3]], pat[3:]
    else:
        bases, rest = [""], pat
    comps = [c for c in rest.split("/") if c]
    if not comps:
        return []

    def join(base: str, name: str) -> str:
        if not base:
            return name
        return base + name if base.endswith("/") else f"{base}/{name}"

    listings = 0
    for i, comp in enumerate(comps):
        last = i == len(comps) - 1
        if not has_magic(comp):
            literal = _unescape(comp)
            bases = [join(b, literal) for b in bases]
            continue
        nxt: list[str] = []
        files: list[RemoteFile] = []
        for base in bases:
            listings += 1
            if listings > MAX_LISTINGS:
                raise ValueError(
                    f"'{pattern}' walks too many directories — narrow the wildcard"
                )
            try:
                entries = listdir(base or ".")
            except OSError:
                continue
            for name, is_dir, size, mtime in entries:
                if not _component_match(name, comp):
                    continue
                if last:
                    if not is_dir:
                        files.append(RemoteFile(join(base, name), size, mtime))
                elif is_dir:
                    nxt.append(join(base, name))
        if last:
            return files
        bases = nxt
        if not bases:
            return []

    # No wildcard in the last component: keep the paths that exist as files.
    out: list[RemoteFile] = []
    for path in bases:
        st = stat(path)
        if st is not None and not st[0]:
            out.append(RemoteFile(path, st[1], st[2]))
    return out


def select_files(
    groups: list[list[RemoteFile]], latest: int, max_files: int, entry_id: str = ""
) -> list[RemoteFile]:
    """Combine each pattern's matches: newest-first ``latest`` per pattern when
    set (else every match, sorted by path), de-duplicated across patterns.
    Raises ``ValueError`` when more than ``max_files`` files would be fetched —
    a loose pattern is refused rather than silently truncated."""
    seen: set[str] = set()
    out: list[RemoteFile] = []
    for files in groups:
        if latest:
            picked = sorted(files, key=lambda f: (-f.mtime, f.path))[:latest]
        else:
            picked = sorted(files, key=lambda f: f.path)
        for f in picked:
            if f.path not in seen:
                seen.add(f.path)
                out.append(f)
    if len(out) > max_files:
        who = f"saved transfer '{entry_id}'" if entry_id else "the patterns"
        raise ValueError(
            f"{who} match {len(out)} files, more than max_files ({max_files}) — "
            "narrow the pattern, set latest, or raise max_files"
        )
    return out


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def dest_names(files: list[RemoteFile]) -> list[str]:
    """Relative destination path for each file: its basename, unless two
    selected files share a basename — then every file keeps its path relative
    to the common folder of all of them, so nothing overwrites anything."""
    names = [_basename(f.path) for f in files]
    if len(set(names)) == len(names):
        return names
    dirs = [f.path.replace("\\", "/").rsplit("/", 1)[0].split("/") for f in files]
    common: list[str] = []
    for parts in zip(*dirs):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    out = []
    for f in files:
        parts = f.path.replace("\\", "/").split("/")
        rel = [p for p in parts[len(common) :] if p not in ("", ".", "..")]
        out.append("/".join(rel) or _basename(f.path))
    return out


def local_join(root: str, rel: str) -> str:
    """``root`` + a ``/``-separated relative path, kept inside ``root``."""
    target = os.path.normpath(os.path.join(root, *rel.split("/")))
    root_n = os.path.normpath(root)
    if os.path.commonpath([root_n, target]) != root_n:
        raise ValueError(f"'{rel}' would land outside {root}")
    return target


# ── registry I/O ──────────────────────────────────────────────────────────────


def load_saved_transfers() -> list[dict[str, Any]]:
    """All raw ``[[saved_transfer]]`` entries ([] when absent/unreadable)."""
    path = config_mod.saved_transfers_path()
    with _LOCK:
        if not path.exists():
            return []
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return []
        rows = data.get("saved_transfer")
        return [dict(r) for r in rows] if isinstance(rows, list) else []


def save_saved_transfers(rows: list[dict[str, Any]]) -> None:
    """Write the whole registry (temp file + replace)."""
    path = config_mod.saved_transfers_path()
    with _LOCK:
        text = tomli_w.dumps({"saved_transfer": rows})
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def load_saved_transfer(sid: str) -> dict[str, Any] | None:
    """One raw entry by id, or None."""
    for row in load_saved_transfers():
        if str(row.get("id") or "") == sid:
            return row
    return None
