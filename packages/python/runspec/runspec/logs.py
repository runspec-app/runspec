"""runspec logs — view, prune, and compact per-invocation audit logs.

The per-run store (``[config.logging] store = "per-run"``) writes one
``{runnable}.{utc-ts}.{run_id}.log`` file per invocation, with **no** in-process
rotation — that's what makes it safe for many users / parallel runs. This
module is the read + maintenance side:

* ``view``    — merge a runnable's per-invocation files into one
  timestamp-ordered stream (so N files read like one file).
* ``prune``   — delete old per-invocation files (the retention an operator
  schedules in place of rotation).
* ``compact`` — roll old per-invocation files into a dated archive.

``prune``/``compact`` only ever touch per-run files and archives — never a
``single``-mode ``{runnable}.log`` active file (see ``_is_managed``).

Pure functions (``plan_prune``, ``plan_compact``, ``collect_records``) are kept
separate from the I/O so they can be unit-tested without a filesystem fixture.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

# Filename grammar (all under {logs}/):
#   {runnable}.log                                   single-mode active file
#   {runnable}.{YYYYMMDDThhmmssZ}.{run_id}.log       per-run invocation file
#   {runnable}.archive.{YYYYMMDD}.log[.gz]           compacted archive
_PER_RUN_RE = re.compile(r"\.\d{8}T\d{6}Z\.[0-9a-fA-F-]{8,}\.log$")
_ARCHIVE_RE = re.compile(r"\.archive\.\d{8}\.log(?:\.gz)?$")

_DUR_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


# ── parsing helpers ──────────────────────────────────────────────────────────


def parse_duration(s: str) -> timedelta:
    """'30m' / '24h' / '7d' / '2w' → timedelta."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"invalid duration {s!r} — use e.g. 30m, 24h, 7d, 2w")
    return timedelta(**{_DUR_UNITS[m.group(2).lower()]: int(m.group(1))})


def parse_size(s: str) -> int:
    """'500KB' / '10MB' / '5GB' → bytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)?\s*", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"invalid size {s!r} — use e.g. 500KB, 10MB, 5GB")
    return int(float(m.group(1)) * _SIZE_UNITS[(m.group(2) or "B").upper()])


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(mtime: float | None) -> str | None:
    """A file mtime (epoch seconds) → UTC ISO-8601 'Z' string, or None."""
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── discovery ────────────────────────────────────────────────────────────────


def log_dirs() -> list[Path]:
    """Existing logs directories, in priority order (venv root, then ~/logs)."""
    out: list[Path] = []
    for c in (Path(sys.prefix) / "logs", Path.home() / "logs"):
        if c.is_dir() and c not in out:
            out.append(c)
    return out


def _globs(runnable: str | None) -> list[str]:
    if runnable:
        return [f"{runnable}.*.log", f"{runnable}.log", f"{runnable}.*.log.gz"]
    return ["*.log", "*.log.gz"]


def _discover(dirs: Iterable[Path], runnable: str | None, *, include_inbox: bool = False) -> list[Path]:
    """The log files in ``dirs``. ``include_inbox`` overlays each dir's
    ``inbox/`` (runs other accounts haven't had swept yet) — **read-only** callers
    only (view / status): prune / compact / reindex never see the inbox, so they
    can never delete or rewrite a pending file."""
    seen: set[Path] = set()
    files: list[Path] = []
    for d in dirs:
        for pat in _globs(runnable):
            for f in d.glob(pat):
                if f.is_file() and f not in seen:
                    seen.add(f)
                    files.append(f)
        if include_inbox:
            for f in _inbox_files(d, runnable):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
    return files


def _inbox_files(d: Path, runnable: str | None) -> list[Path]:
    """Per-run files waiting in ``d/inbox`` — regular, single-link files only,
    never following a symlink (the inbox is world-writable: a planted link must
    not make a reader print a file it can read but its writer can't)."""
    import stat as _stat

    box = d / "inbox"
    if box.is_symlink() or not box.is_dir():
        return []
    out: list[Path] = []
    try:
        names = sorted(os.listdir(box))
    except OSError:
        return []
    for name in names:
        if not _PER_RUN_RE.search(name) or (runnable and _runnable_of(Path(name)) != runnable):
            continue
        try:
            st = os.lstat(box / name)
        except OSError:
            continue
        if _stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            out.append(box / name)
    return out


def resolve_log_file(log_dir: Path, name: str) -> Path | None:
    """Where a run's log file is now: ``log_dir/name``, else — not swept yet —
    ``log_dir/inbox/name``. ``name`` is a bare basename (anything with a path
    separator is refused); an inbox entry must be a regular, single-link file,
    never a symlink. ``None`` when it's in neither."""
    import stat as _stat

    if not name or name != os.path.basename(name) or name in (".", ".."):
        return None
    for p in (Path(log_dir) / name, Path(log_dir) / "inbox" / name):
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if _stat.S_ISREG(st.st_mode) and (p.parent == Path(log_dir) or st.st_nlink == 1):
            return p
    return None


def _is_managed(path: Path) -> bool:
    """True only for per-run files and archives — never a single {runnable}.log.

    Uses the exact per-run/archive suffix (not "has a dot") so a runnable whose
    name itself contains a dot (e.g. ``deploy.web``) can never have its active
    single-mode file deleted by prune/compact.
    """
    return bool(_PER_RUN_RE.search(path.name) or _ARCHIVE_RE.search(path.name))


def _is_archive(path: Path) -> bool:
    return bool(_ARCHIVE_RE.search(path.name))


def _runnable_of(path: Path) -> str:
    """The runnable a managed file belongs to (strip the per-run/archive tail)."""
    for rx in (_PER_RUN_RE, _ARCHIVE_RE):
        m = rx.search(path.name)
        if m:
            return path.name[: m.start()]
    return path.name[:-7] if path.name.endswith(".log.gz") else path.name[:-4]


# ── public classification (shared filename grammar) ──────────────────────────
#
# The console lists log files over a shell command and classifies them itself,
# so it needs the same grammar as prune/compact/status without spawning a remote
# `runspec` process. These are the public face of the private regex helpers
# above — the single source of truth for "what kind of log file is this / which
# runnable owns it".


def classify_name(name: str) -> str:
    """Classify a log *filename*: ``"per_run"`` / ``"archive"`` / ``"single"``.

    ``per_run`` and ``archive`` are the *managed* files prune/compact act on; a
    ``single`` ``{runnable}.log`` is the active single-mode file, never managed.
    """
    if _ARCHIVE_RE.search(name):
        return "archive"
    if _PER_RUN_RE.search(name):
        return "per_run"
    return "single"


def runnable_of(name: str) -> str:
    """The runnable a log *filename* belongs to (strip the per-run/archive tail)."""
    return _runnable_of(Path(name))


def scope_dirs(scope: str | None, *, destructive: bool) -> list[Path] | None:
    """Resolve a ``--scope`` choice to a concrete ``dirs`` list, or ``None`` for
    the :func:`log_dirs` union.

    ``"venv"`` → ``[{sys.prefix}/logs]`` (this venv's own dir); ``"home"`` →
    ``[~/logs]`` (the shared home fallback); ``"all"`` → ``None`` (both, via
    ``log_dirs()``). When unset, **destructive** ops (prune/compact) default to
    ``venv`` — they never touch the shared ``~/logs`` unless asked — while reads
    (status/view) default to the informative union.
    """
    if scope == "venv":
        return [Path(sys.prefix) / "logs"]
    if scope == "home":
        return [Path.home() / "logs"]
    if scope == "all":
        return None
    return [Path(sys.prefix) / "logs"] if destructive else None


def _read_lines(path: Path) -> list[str]:
    """JSON-line records from a log file, transparently decompressing .gz.
    An unreadable file reads as empty — see :func:`_read_lines_checked` for
    callers that must tell the two apart."""
    return _read_lines_checked(path) or []


def _read_lines_checked(path: Path) -> list[str] | None:
    """Like :func:`_read_lines`, but ``None`` when the file couldn't be read (vs
    ``[]`` for a readable, empty file)."""
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, EOFError):
        return None
    return [ln for ln in text.splitlines() if ln.strip()]


# ── view ─────────────────────────────────────────────────────────────────────


def collect_records(
    dirs: Iterable[Path],
    runnable: str,
    *,
    since: timedelta | None = None,
    run: str | None = None,
    user: str | None = None,
) -> list[tuple[dict[str, Any], str]]:
    """Merge a runnable's files into one timestamp-sorted list of (record, raw).

    Filters: ``since`` (records newer than now-since), ``run`` (one run_id, or
    a unique prefix of one — the 8 characters the text view prints are enough),
    ``user`` (every record of any run that user launched — resolved from the
    run_summary's ``extra.user``).
    """
    # A per-run file contributes records only for its own run (the id in its
    # name): a file dropped into the world-writable inbox can't inject records
    # into another run's view.
    raw_lines: list[str] = []
    for f in _discover(dirs, runnable, include_inbox=True):
        own = _file_run_id(f.name)
        for ln in _read_lines(f):
            if own is not None and _line_run_id(ln) != own:
                continue
            raw_lines.append(ln)

    parsed: list[tuple[dict[str, Any], str]] = []
    user_of_run: dict[str, str] = {}
    for ln in raw_lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        extra = rec.get("extra", {}) if isinstance(rec.get("extra"), dict) else {}
        if extra.get("event") == "run_summary" and extra.get("run_id"):
            user_of_run[extra["run_id"]] = extra.get("user", "")
        parsed.append((rec, ln))

    if run:
        run = resolve_run_id(run, (_line_extra(rec).get("run_id", "") for rec, _ in parsed), runnable)

    cutoff = datetime.now(timezone.utc) - since if since else None

    def keep(rec: dict[str, Any]) -> bool:
        extra = rec.get("extra", {}) if isinstance(rec.get("extra"), dict) else {}
        rid = extra.get("run_id", "")
        if run and rid != run:
            return False
        if user and user_of_run.get(rid, extra.get("user", "")) != user:
            return False
        if cutoff is not None:
            dt = _parse_ts(rec.get("ts", ""))
            if dt is None or dt < cutoff:
                return False
        return True

    parsed = [(r, ln) for (r, ln) in parsed if keep(r)]
    parsed.sort(key=lambda pair: pair[0].get("ts", ""))
    return parsed


def _line_extra(rec: dict[str, Any]) -> dict[str, Any]:
    extra = rec.get("extra")
    return extra if isinstance(extra, dict) else {}


def resolve_run_id(run: str, run_ids: Iterable[str], runnable: str | None = None) -> str:
    """The full run id ``run`` names among ``run_ids``: an exact id, else the one
    id it is a prefix of (case-insensitive, like the hex it is). Raises
    ``ValueError`` when the prefix matches several runs (naming them) or none."""
    ids = {rid for rid in run_ids if rid}
    if run in ids:
        return run
    want = run.strip().lower()
    matches = sorted(rid for rid in ids if want and rid.lower().startswith(want))
    if len(matches) == 1:
        return matches[0]
    where = f" in {runnable}'s logs" if runnable else ""
    if not matches:
        raise ValueError(f"no run matching {run!r}{where}")
    shown = ", ".join(matches[:10]) + (f" (+{len(matches) - 10} more)" if len(matches) > 10 else "")
    raise ValueError(f"{run!r} matches {len(matches)} runs{where}: {shown} — give more of the id")


def runnables_with_logs(dirs: Iterable[Path]) -> list[str]:
    """Sorted names of the runnables that have log files in ``dirs`` (per-run,
    archive or single-mode, including the inbox) — what ``runspec logs`` can show.

    ``~/logs`` is shared with whatever else logs there, so a plain ``{name}.log``
    counts only when it holds runspec records (see :func:`_is_runspec_log`);
    per-run files and archives are known by their names."""
    names: set[str] = set()
    for f in _discover(dirs, None, include_inbox=True):
        name = _runnable_of(f)
        if name and name not in names and (_is_managed(f) or _is_runspec_log(f)):
            names.add(name)
    return sorted(names)


def _is_runspec_log(path: Path, *, max_lines: int = 5) -> bool:
    """True when one of the first ``max_lines`` lines of ``path`` is a runspec
    JSON record — an object with ``ts``/``level``/``message`` and an
    ``extra.run_id``, which runspec stamps on every record it writes."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(max_lines):
                line = fh.readline(65536)
                if not line:
                    return False
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and {"ts", "level", "message"} <= rec.keys() and _line_extra(rec).get("run_id"):
                    return True
    except OSError:
        pass
    return False


def _file_run_id(name: str) -> str | None:
    from .inbox import file_run_id

    return file_run_id(name)


def _line_run_id(line: str) -> str:
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ""
    extra = rec.get("extra") if isinstance(rec, dict) else None
    return str(extra.get("run_id") or "") if isinstance(extra, dict) else ""


def read_run_rows(dirs: Iterable[Path]) -> list[dict[str, Any]]:
    """Every run's row across the log files in ``dirs`` (never the inbox), via the
    one parser (:func:`runspec.inbox.rows_from_lines`) — the rebuild source for
    ``runspec history reindex``. Unlike :func:`read_run_summaries` it keeps
    ``runspec exec``'s observed exit codes, labels and skips, and ``log_file``."""
    from .inbox import rows_from_lines

    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.log")) + sorted(d.glob("*.log.gz")):
            for row in rows_from_lines(f.name, _read_lines(f)):
                row["log_file"] = f.name
                out.append(row)
    return out


def read_run_summaries(dirs: Iterable[Path]) -> list[dict[str, Any]]:
    """Every ``run_summary`` record across all log files in ``dirs``, flattened to
    one dict per run (the record's ``extra`` merged with the top-level ``ts``).

    The rebuild source for the runs index — ``runsdb.reindex`` maps these 1:1.
    Reads all runnables' files (unlike :func:`collect_records`, which is scoped to
    one), transparently decompressing archives.
    """
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.log")) + sorted(d.glob("*.log.gz")):
            for ln in _read_lines(f):
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
                if extra.get("event") == "run_summary" and extra.get("run_id"):
                    out.append({**extra, "ts": rec.get("ts")})
    return out


def _format_line(rec: dict[str, Any], user_of_run: dict[str, str]) -> str:
    extra = rec.get("extra", {}) if isinstance(rec.get("extra"), dict) else {}
    rid = extra.get("run_id", "")
    user = extra.get("user") or user_of_run.get(rid, "")
    ts = rec.get("ts", "")
    level = rec.get("level", "")
    msg = rec.get("message", "")
    return f"{ts} {level:<8} run={(rid or '-')[:8]} user={user or '-'} {msg}"


def _emit(records: list[tuple[dict[str, Any], str]], *, as_json: bool, out: TextIO) -> None:
    user_of_run: dict[str, str] = {}
    for rec, _ in records:
        extra = rec.get("extra", {}) if isinstance(rec.get("extra"), dict) else {}
        if extra.get("event") == "run_summary" and extra.get("run_id"):
            user_of_run[extra["run_id"]] = extra.get("user", "")
    for rec, raw in records:
        out.write(raw + "\n" if as_json else _format_line(rec, user_of_run) + "\n")


def view(
    runnable: str,
    *,
    dirs: Iterable[Path] | None = None,
    since: timedelta | None = None,
    run: str | None = None,
    user: str | None = None,
    as_json: bool = False,
    follow: bool = False,
    out: TextIO | None = None,
    poll_interval: float = 1.0,
) -> None:
    """Write the merged stream to ``out`` (default stdout). SIGPIPE-clean."""
    dirs = list(dirs) if dirs is not None else log_dirs()
    out = out or sys.stdout
    try:
        records = collect_records(dirs, runnable, since=since, run=run, user=user)
        _emit(records, as_json=as_json, out=out)
        out.flush()
        if not follow:
            return
        if run and records:
            # Keep following the run the prefix named, even if a later run's id
            # happens to share it.
            run = _line_extra(records[0][0]).get("run_id") or run
        seen = {(r.get("ts", ""), r.get("extra", {}).get("run_id", ""), r.get("message", "")) for r, _ in records}
        while True:
            time.sleep(poll_interval)
            fresh = [
                (r, ln) for (r, ln) in collect_records(dirs, runnable, since=since, run=run, user=user) if (r.get("ts", ""), r.get("extra", {}).get("run_id", ""), r.get("message", "")) not in seen
            ]
            if fresh:
                _emit(fresh, as_json=as_json, out=out)
                out.flush()
                seen.update((r.get("ts", ""), r.get("extra", {}).get("run_id", ""), r.get("message", "")) for r, _ in fresh)
    except (BrokenPipeError, KeyboardInterrupt):
        # `| head`/`less` closed the pipe, or Ctrl-C on --follow — exit quietly.
        with contextlib.suppress(BrokenPipeError):
            out.flush()


# ── status ───────────────────────────────────────────────────────────────────


def inventory(dirs: Iterable[Path], runnable: str | None = None) -> dict[str, dict[str, Any]]:
    """Per-runnable inventory of managed files. Pure — only stats files.

    Returns ``{runnable: {per_run_files, archives, total_bytes, oldest, newest}}``
    where ``oldest``/``newest`` are file mtimes (epoch seconds). Only per-run
    files and archives are counted — never a single-mode ``{runnable}.log``.
    """
    per: dict[str, dict[str, Any]] = {}
    for f in _discover(dirs, runnable, include_inbox=True):
        if not _is_managed(f):
            continue
        name = _runnable_of(f)
        try:
            st = f.stat()
        except OSError:
            continue
        row = per.setdefault(
            name,
            {"runnable": name, "per_run_files": 0, "archives": 0, "total_bytes": 0, "oldest": None, "newest": None},
        )
        if _is_archive(f):
            row["archives"] += 1
        else:
            row["per_run_files"] += 1
        row["total_bytes"] += st.st_size
        if row["oldest"] is None or st.st_mtime < row["oldest"]:
            row["oldest"] = st.st_mtime
        if row["newest"] is None or st.st_mtime > row["newest"]:
            row["newest"] = st.st_mtime
    return per


def status(
    runnable: str | None = None,
    *,
    dirs: Iterable[Path] | None = None,
    as_json: bool = False,
    out: TextIO | None = None,
) -> None:
    """Report the per-runnable file inventory to ``out`` (default stdout).

    This is the read-only "what's on disk" view the console's Logs tab reads to
    populate per-venv usage before offering compact/prune. ``--json`` emits a
    single machine-readable object; text prints an aligned table.
    """
    dirs = list(dirs) if dirs is not None else log_dirs()
    out = out or sys.stdout
    per = inventory(dirs, runnable)
    rows = sorted(per.values(), key=lambda r: r["runnable"])
    total_bytes = sum(r["total_bytes"] for r in rows)
    total_files = sum(r["per_run_files"] + r["archives"] for r in rows)

    if as_json:
        payload = {
            "dirs": [str(d) for d in dirs],
            "runnables": [
                {
                    "runnable": r["runnable"],
                    "per_run_files": r["per_run_files"],
                    "archives": r["archives"],
                    "total_bytes": r["total_bytes"],
                    "oldest": _iso(r["oldest"]),
                    "newest": _iso(r["newest"]),
                }
                for r in rows
            ],
            "total_bytes": total_bytes,
            "total_files": total_files,
        }
        out.write(json.dumps(payload) + "\n")
        return

    if not rows:
        out.write("No per-invocation logs found.\n")
        return

    name_w = max(len("RUNNABLE"), max(len(r["runnable"]) for r in rows))
    out.write(f"{'RUNNABLE':<{name_w}}  {'RUNS':>6}  {'ARCHIVES':>8}  {'SIZE':>10}  NEWEST\n")
    for r in rows:
        out.write(f"{r['runnable']:<{name_w}}  {r['per_run_files']:>6}  {r['archives']:>8}  {_human_size(r['total_bytes']):>10}  {_iso(r['newest']) or '-'}\n")
    out.write(f"\n{total_files} file(s), {_human_size(total_bytes)} total across {len(rows)} runnable(s).\n")


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── prune ────────────────────────────────────────────────────────────────────


def plan_prune(
    dirs: Iterable[Path],
    runnable: str | None,
    *,
    older_than: timedelta | None = None,
    max_files: int | None = None,
    max_total_size: int | None = None,
    now: float | None = None,
) -> list[Path]:
    """Return the managed files to delete. Pure — touches nothing.

    A file is selected if it violates **any** policy: older than ``older_than``,
    beyond the newest ``max_files`` (per runnable), or pushing cumulative size
    over ``max_total_size`` (oldest first). At least one policy is required.
    """
    if older_than is None and max_files is None and max_total_size is None:
        raise ValueError("prune needs at least one of --older-than / --max-files / --max-total-size")

    files = [f for f in _discover(dirs, runnable) if _is_managed(f)]
    stats = {f: f.stat() for f in files}
    now = now if now is not None else time.time()
    doomed: set[Path] = set()

    if older_than is not None:
        cut = now - older_than.total_seconds()
        doomed |= {f for f in files if stats[f].st_mtime < cut}

    if max_files is not None:
        per: dict[str, list[Path]] = {}
        for f in files:
            per.setdefault(_runnable_of(f), []).append(f)
        for group in per.values():
            group.sort(key=lambda f: stats[f].st_mtime, reverse=True)  # newest first
            doomed |= set(group[max_files:])

    if max_total_size is not None:
        ordered = sorted(files, key=lambda f: stats[f].st_mtime, reverse=True)
        total = 0
        for f in ordered:
            total += stats[f].st_size
            if total > max_total_size:
                doomed.add(f)

    return sorted(doomed, key=lambda f: stats[f].st_mtime)


def prune(
    runnable: str | None,
    *,
    dirs: Iterable[Path] | None = None,
    older_than: timedelta | None = None,
    max_files: int | None = None,
    max_total_size: int | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: TextIO | None = None,
) -> tuple[int, int]:
    """Delete (or, with dry_run, report) prunable files. Returns (count, bytes).

    With ``as_json`` a single JSON object is written instead of the per-file
    text lines — the shape the console parses out of ``runspec logs prune --json``.
    """
    dirs = list(dirs) if dirs is not None else log_dirs()
    out = out or sys.stdout
    targets = plan_prune(dirs, runnable, older_than=older_than, max_files=max_files, max_total_size=max_total_size)
    freed = 0
    deleted: list[dict[str, Any]] = []
    pruned_by_dir: dict[Path, list[str]] = {}
    for f in targets:
        size = f.stat().st_size
        if not dry_run:
            f.unlink(missing_ok=True)
            pruned_by_dir.setdefault(f.parent, []).append(f.name)
        freed += size
        deleted.append({"path": str(f), "bytes": size})
        if not as_json:
            verb = "would delete" if dry_run else "deleted"
            out.write(f"{verb}  {f}  ({size} bytes)\n")
    # §8.1: stamp the runs index for the files just deleted, so a surviving row
    # whose log we removed can say when/by whom (turning a bare "file not found" on
    # expand into "log pruned by X on Y — record retained"). Best-effort, after the
    # unlink so a DB hiccup never strands the delete.
    if pruned_by_dir:
        with contextlib.suppress(Exception):
            import getpass

            from . import runsdb

            who = getpass.getuser()
            ts = datetime.now(timezone.utc).isoformat()
            for d, names in pruned_by_dir.items():
                runsdb.mark_log_pruned(d, names, pruned_by=who, pruned_at=ts)
    if pruned_by_dir:
        _audit_retention(
            "logs.prune",
            dirs=list(pruned_by_dir),
            detail={
                "files": sum(len(v) for v in pruned_by_dir.values()),
                "freed_bytes": freed,
                "runnable": runnable or "",
                "older_than": str(older_than or ""),
                "max_files": max_files,
                "max_total_size": max_total_size,
            },
        )
    if as_json:
        out.write(json.dumps({"dry_run": dry_run, "count": len(targets), "freed_bytes": freed, "deleted": deleted}) + "\n")
    else:
        label = "Would free" if dry_run else "Freed"
        out.write(f"{label} {freed} bytes across {len(targets)} file(s).\n")
    return len(targets), freed


# ── compact ──────────────────────────────────────────────────────────────────


def plan_compact(
    dirs: Iterable[Path],
    runnable: str | None,
    *,
    older_than: timedelta,
    now: float | None = None,
) -> dict[Path, list[Path]]:
    """Map each per-runnable archive target → the per-run files to fold into it.

    Only per-run files (never existing archives, never single-mode files) older
    than ``older_than`` are eligible. The archive lands in the same directory as
    the files, dated with today's UTC date.
    """
    now = now if now is not None else time.time()
    cut = now - older_than.total_seconds()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    plan: dict[Path, list[Path]] = {}
    for f in _discover(dirs, runnable):
        if not _is_managed(f) or _is_archive(f):
            continue
        if f.stat().st_mtime >= cut:
            continue
        archive = f.parent / f"{_runnable_of(f)}.archive.{today}.log"
        plan.setdefault(archive, []).append(f)
    return plan


def compact(
    runnable: str | None,
    *,
    older_than: timedelta,
    dirs: Iterable[Path] | None = None,
    gzip_: bool = False,
    dry_run: bool = False,
    as_json: bool = False,
    out: TextIO | None = None,
) -> int:
    """Roll eligible per-run files into dated archives. Returns files compacted.

    Lines are concatenated raw (no reformatting — every field, including
    ``exc_structured``, survives) and re-sorted by timestamp. Append-merges into
    an existing same-day archive idempotently. Originals are deleted after the
    archive is written. With ``as_json`` a single JSON object is written instead
    of the per-archive text lines.
    """
    dirs = list(dirs) if dirs is not None else log_dirs()
    out = out or sys.stdout
    plan = plan_compact(dirs, runnable, older_than=older_than)
    compacted = 0
    archives: list[dict[str, Any]] = []
    for archive, sources in plan.items():
        target = archive.with_suffix(archive.suffix + ".gz") if gzip_ else archive
        if dry_run:
            archives.append({"archive": str(target), "count": len(sources), "sources": [str(s) for s in sources]})
            compacted += len(sources)
            if not as_json:
                out.write(f"would compact {len(sources)} file(s) → {target}\n")
            continue

        # Only fold in what we could actually read: a source we can't read (a
        # file another account made unreadable, a transient I/O error) is left in
        # place rather than deleted without being archived. An unreadable existing
        # archive would be overwritten, so that whole group is skipped.
        lines: list[str] = []
        readable: list[Path] = []
        archive_ok = True
        for existing in (archive, archive.with_suffix(archive.suffix + ".gz")):
            if existing.exists():
                got = _read_lines_checked(existing)
                if got is None:
                    archive_ok = False
                    break
                lines.extend(got)
        if not archive_ok:
            if not as_json:
                out.write(f"skipped {target}: the existing archive couldn't be read\n")
            continue
        for s in sources:
            got = _read_lines_checked(s)
            if got is None:
                if not as_json:
                    out.write(f"skipped {s}: couldn't be read — left in place\n")
                continue
            lines.extend(got)
            readable.append(s)
        if not readable:
            continue
        sources = readable
        lines.sort(key=lambda ln: _line_ts(ln))

        body = "\n".join(lines) + "\n"
        if gzip_:
            with gzip.open(target, "wt", encoding="utf-8") as fh:
                fh.write(body)
            # If a non-gz archive existed for today, supersede it.
            if archive.exists() and archive != target:
                archive.unlink(missing_ok=True)
        else:
            target.write_text(body, encoding="utf-8")

        for s in sources:
            s.unlink(missing_ok=True)
        # §8.1: the content moved into the archive (it wasn't deleted), so repoint
        # the runs-index rows from each per-run file to the archive rather than
        # marking them pruned — keeps per-log fetch and a later prune-stamp match
        # working. Best-effort. (target.parent == the sources' dir == archive dir.)
        with contextlib.suppress(Exception):
            from . import runsdb

            runsdb.repoint_logs(target.parent, {s.name: target.name for s in sources})
        archives.append({"archive": str(target), "count": len(sources), "sources": [str(s) for s in sources]})
        compacted += len(sources)
        if not as_json:
            out.write(f"compacted {len(sources)} file(s) → {target}\n")

    if compacted and not dry_run:
        _audit_retention(
            "logs.compact",
            dirs=sorted({Path(a["archive"]).parent for a in archives}),
            detail={"files": compacted, "archives": len(archives), "runnable": runnable or "", "older_than": str(older_than), "gzip": gzip_ or None},
        )
    if as_json:
        out.write(json.dumps({"dry_run": dry_run, "compacted": compacted, "archives": archives}) + "\n")
    elif not plan:
        out.write("nothing to compact.\n")
    return compacted


def _grant_acl(directory: Path, users: list[str]) -> str:
    """Best-effort: grant each user ``rwX`` on ``directory`` via a POSIX ACL — an
    **access** entry (recursive, so existing files + the runs index become
    writable) plus a **default** entry (so files created later inherit it). This
    lets an account that isn't in the dir's group still write there, with no group
    change and no root — the owner (who runs this) grants it directly.

    Returns ``"applied"``, ``"unsupported: …"`` (no ``setfacl`` / a filesystem
    without ACL support), ``"error: …"``, or ``"skipped"`` (no users). Never raises
    — the ACL grant is a bonus on top of the group share, so a failure here must
    not fail the share.
    """
    users = [u for u in (users or []) if u]
    if not users:
        return "skipped"
    setfacl = shutil.which("setfacl")
    if not setfacl:
        return "unsupported: setfacl not installed"
    spec = ",".join(f"u:{u}:rwX" for u in users)

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [setfacl, *args, str(directory)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    try:
        access = _run(["-R", "-m", spec])
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    if access.returncode != 0:
        msg = (access.stderr or access.stdout or "").strip().splitlines()
        first = msg[0] if msg else f"exit {access.returncode}"
        if "not supported" in first.lower():
            return "unsupported: filesystem has no ACL support"
        return f"error: {first}"
    # Default ACL so new files inherit the grant (setfacl applies -d to
    # directories only under -R, skipping regular files). Best-effort — the access
    # ACL above is the load-bearing part.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _run(["-R", "-d", "-m", spec])
    return "applied"


def share(
    dirs: Iterable[Path] | None = None,
    *,
    group: str | None = None,
    acl_users: list[str] | None = None,
    inbox: bool = False,
    no_inbox: bool = False,
    owner_only: bool = False,
    as_json: bool = False,
    out: TextIO | None = None,
) -> list[dict[str, Any]]:
    """Share each logs dir with other accounts.

    **The inbox (recommended — docs/design/venv-logs-inbox.md):** ``inbox=True``
    creates ``logs/inbox`` (``01777``, owned by the venv owner; a ``.no-inbox``
    marker is removed) so every other account drops its per-run file there and
    the owner's sweep indexes it — no group, ACL or sudo rule. With the inbox,
    ``logs/`` itself keeps its mode unless ``group``/``acl_users`` also ask for
    group sharing; ``owner_only`` makes it ``0755`` (drops group write — only
    the owner writes it). ``no_inbox`` writes the ``.no-inbox`` opt-out marker
    instead. Must run as the venv owner (or root, which the CLI drops to the
    owner first).

    **Group sharing (the alternative):** make each logs dir writable by a shared Unix group, so several accounts
    (e.g. a console operator plus the service account a ``run_as`` runnable runs
    as) can write the per-venv runs index and audit logs into the *same* dir.

    For each dir: ensure it exists, ``chmod 2775`` (group-writable **and setgid**,
    so files created in it inherit the dir's group), optionally ``chgrp`` it to
    ``group``, and repair the existing ``runspec_runs.db`` (``chgrp`` + ``chmod
    664``). A dir ``chmod`` never touches a file already inside it, so without the
    repair a group member still couldn't write an index a non-member created.

    ``acl_users`` additionally grants each named OS user ``rwX`` via a POSIX ACL
    (see :func:`_grant_acl`) — the way to let an account that isn't in the dir's
    group write there without a group change or root. Best-effort: the per-dir
    result carries an ``acl`` status and the share still succeeds if ACLs aren't
    available.

    **POSIX only** — setgid + group ownership are POSIX concepts, so this raises
    ``ValueError`` on a non-POSIX host. It is *sudo-free*: it changes only what the
    invoking user is allowed to change, so escalation — running it as the dir's
    owner (``sudo -u <owner> {venv}/bin/runspec logs share``) — is the caller's
    job. A ``chmod``/``chgrp`` the caller isn't permitted to make raises
    ``OSError``. Returns a per-dir result list.
    """
    if not hasattr(os, "getuid"):  # Windows — no setgid / group-writable model
        raise ValueError("`runspec logs share` applies to POSIX hosts only (setgid + group permissions)")
    from . import runsdb

    targets = list(dirs) if dirs is not None else log_dirs()
    acl_users = [u for u in (acl_users or []) if u]
    out = out or sys.stdout
    results: list[dict[str, Any]] = []
    if inbox and no_inbox:
        raise ValueError("--inbox and --no-inbox are mutually exclusive")
    group_share = not (inbox or no_inbox or owner_only) or bool(group or acl_users)
    for d in targets:
        d.mkdir(parents=True, exist_ok=True)
        if not group_share:
            res = _share_inbox(d, inbox=inbox, no_inbox=no_inbox, owner_only=owner_only)
            results.append(res)
            if not as_json:
                out.write(_inbox_line(res) + "\n")
            continue
        if group:
            shutil.chown(d, group=group)
        os.chmod(d, 0o2775)
        # A dir chmod leaves files already inside untouched, so repair the runs
        # index's group access too — else a group member's writes to an index
        # another account created still fail. Best-effort: it may not exist yet, or
        # be owned by another user this caller can't chmod.
        db = runsdb.db_path(d)
        db_repaired = False
        if db.exists():
            with contextlib.suppress(OSError):
                if group:
                    shutil.chown(db, group=group)
                os.chmod(db, 0o664)
                db_repaired = True
        result: dict[str, Any] = {
            "dir": str(d),
            "group": group or "",
            "mode": "2775",
            "db_repaired": db_repaired,
        }
        if acl_users:
            result["acl"] = _grant_acl(d, acl_users)
            result["acl_users"] = list(acl_users)
        if inbox or no_inbox:
            result.update(_share_inbox(d, inbox=inbox, no_inbox=no_inbox, owner_only=False))
            result["mode"] = "2775"
        results.append(result)
        if not as_json:
            g = f" (group {group})" if group else ""
            extra = " + runs index" if db_repaired else ""
            acl = f" + acl {result['acl']} for {','.join(acl_users)}" if acl_users else ""
            out.write(f"shared {d}{g} — 2775 setgid{extra}{acl}\n")
            if inbox or no_inbox:
                out.write(_inbox_line(result) + "\n")
    _audit_retention(
        "logs.share",
        dirs=[Path(r["dir"]) for r in results if r.get("dir")],
        detail={"inbox": "on" if inbox else ("off" if no_inbox else ""), "group": group or "", "acl_users": acl_users or None, "owner_only": owner_only or None},
    )
    if as_json:
        out.write(json.dumps({"shared": results}) + "\n")
    return results


def _audit_retention(event: str, *, dirs: list[Path], detail: dict[str, Any]) -> None:
    """Record a retention / sharing change to the logs as an audit event —
    deleting or rolling up run logs is exactly what the ledger is for. The
    ledger itself (``audit.jsonl``) is never a prune/compact target. Best-effort."""
    with contextlib.suppress(Exception):
        from .auditlog import audit

        audit(event, "succeeded", target={"dirs": [str(d) for d in dirs]}, detail={k: v for k, v in detail.items() if v not in (None, "")})


def _share_inbox(d: Path, *, inbox: bool, no_inbox: bool, owner_only: bool) -> dict[str, Any]:
    """The inbox half of :func:`share` for one dir. Raises ``OSError`` (like the
    group share) when the inbox can't be set up — e.g. not run as the owner."""
    from . import inbox as _inbox

    res: dict[str, Any] = {"dir": str(d)}
    if owner_only:
        os.chmod(d, 0o755)
        res["mode"] = "0755"
    if no_inbox:
        _inbox.disable_inbox(d)
        res["inbox"] = "disabled"
        return res
    if inbox:
        owner = _inbox.owner_of_logs(d)
        if owner is not None and hasattr(os, "geteuid") and os.geteuid() not in (owner, 0):
            raise PermissionError(f"only the venv owner ({_inbox.user_name(owner)}) can set up {d / 'inbox'} — run it as that account (sudo -u {_inbox.user_name(owner)} …)")
        got = _inbox.ensure_inbox(d, owner, force=True)
        if got not in ("created", "repaired", "ok"):
            raise OSError(f"couldn't set up {d / 'inbox'}: {got}")
        res["inbox"] = got
        res["inbox_path"] = str(_inbox.inbox_dir(d))
    return res


def _inbox_line(res: dict[str, Any]) -> str:
    parts = []
    if res.get("inbox") == "disabled":
        parts.append(f"inbox disabled for {res['dir']} (.no-inbox marker written)")
    elif res.get("inbox"):
        verb = {"created": "created", "repaired": "repaired", "ok": "already set up"}.get(res["inbox"], res["inbox"])
        parts.append(f"logs inbox {verb}: {res.get('inbox_path')} (01777) — other accounts' runs now reach this venv's history")
    if res.get("mode") == "0755":
        parts.append(f"{res['dir']} is owner-only (0755)")
    return "; ".join(parts) or f"nothing to change in {res['dir']}"


def _line_ts(line: str) -> str:
    try:
        return str(json.loads(line).get("ts", ""))
    except json.JSONDecodeError:
        return ""
