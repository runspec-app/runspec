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
import re
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


def _discover(dirs: Iterable[Path], runnable: str | None) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for d in dirs:
        for pat in _globs(runnable):
            for f in d.glob(pat):
                if f.is_file() and f not in seen:
                    seen.add(f)
                    files.append(f)
    return files


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


def _read_lines(path: Path) -> list[str]:
    """JSON-line records from a log file, transparently decompressing .gz."""
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
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

    Filters: ``since`` (records newer than now-since), ``run`` (one run_id),
    ``user`` (every record of any run that user launched — resolved from the
    run_summary's ``extra.user``).
    """
    raw_lines: list[str] = []
    for f in _discover(dirs, runnable):
        raw_lines.extend(_read_lines(f))

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
    for f in _discover(dirs, runnable):
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
    for f in targets:
        size = f.stat().st_size
        if not dry_run:
            f.unlink(missing_ok=True)
        freed += size
        deleted.append({"path": str(f), "bytes": size})
        if not as_json:
            verb = "would delete" if dry_run else "deleted"
            out.write(f"{verb}  {f}  ({size} bytes)\n")
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

        lines: list[str] = []
        for existing in (archive, archive.with_suffix(archive.suffix + ".gz")):
            if existing.exists():
                lines.extend(_read_lines(existing))
        for s in sources:
            lines.extend(_read_lines(s))
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
        archives.append({"archive": str(target), "count": len(sources), "sources": [str(s) for s in sources]})
        compacted += len(sources)
        if not as_json:
            out.write(f"compacted {len(sources)} file(s) → {target}\n")

    if as_json:
        out.write(json.dumps({"dry_run": dry_run, "compacted": compacted, "archives": archives}) + "\n")
    elif not plan:
        out.write("nothing to compact.\n")
    return compacted


def _line_ts(line: str) -> str:
    try:
        return str(json.loads(line).get("ts", ""))
    except json.JSONDecodeError:
        return ""
