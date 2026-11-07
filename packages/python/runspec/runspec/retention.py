"""Log retention policy — a venv that cleans up its own logs.

Per-run files (every run by another account lands in ``logs/`` as one) and the
archives ``runspec logs compact`` makes are never rotated, so a shared venv's
``logs/`` grows until something removes them. A policy file at the venv root,
``runspec_logs_retention.toml``, says how long to keep them::

    [logs]
    compact_after  = "7d"    # roll per-run files older than this into a gzip archive
    delete_after   = "90d"   # delete per-run files + archives older than this
    max_total_size = "5GB"   # optional: delete the oldest over this total

The owner of the venv root applies it at the end of their own runs, at most
once a day, in a detached background process (:func:`maybe_apply`, called from
the runs-index finalize) — so no cron line, no keeper and nothing for
deprovisioning to clean up. ``runspec logs retention`` shows / sets / applies it.

It acts only on ``{venv}/logs/`` and only on the files ``compact``/``prune``
manage (per-run files + archives) — never ``audit.jsonl``, the runs index, the
inbox or a single-mode ``{runnable}.log``. See SPEC → "Log retention policy".
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10
    import tomli as tomllib  # type: ignore[no-redef]

POLICY_FILE = "runspec_logs_retention.toml"
LAST_FILE = ".retention-last.json"
LOCK_FILE = ".retention.lock"
INTERVAL_S = 24 * 3600
OFF = "off"
FIELDS = ("compact_after", "delete_after", "max_total_size")


@dataclass
class Policy:
    compact_after: str = ""
    delete_after: str = ""
    max_total_size: str = ""

    def empty(self) -> bool:
        return not (self.compact_after or self.delete_after or self.max_total_size)

    def as_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in FIELDS if getattr(self, k)}


# ── paths ─────────────────────────────────────────────────────────────────────


def venv_root(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else Path(sys.prefix)


def policy_path(root: Path | str | None = None) -> Path:
    return venv_root(root) / POLICY_FILE


def logs_dir(root: Path | str | None = None) -> Path:
    return venv_root(root) / "logs"


# ── policy file ───────────────────────────────────────────────────────────────


def validate(policy: Policy) -> Policy:
    """Raise ``ValueError`` for a policy that can't be applied; return it unchanged."""
    from . import logs as _logs

    if policy.empty():
        raise ValueError("a retention policy needs at least one of compact_after / delete_after / max_total_size")
    compact = _logs.parse_duration(policy.compact_after) if policy.compact_after else None
    delete = _logs.parse_duration(policy.delete_after) if policy.delete_after else None
    if policy.max_total_size:
        _logs.parse_size(policy.max_total_size)
    if compact is not None and delete is not None and compact >= delete:
        raise ValueError(f"compact_after ({policy.compact_after}) must be shorter than delete_after ({policy.delete_after})")
    return policy


def parse_policy(data: dict[str, Any]) -> Policy:
    """A parsed TOML document → a validated :class:`Policy`."""
    section = data.get("logs")
    if not isinstance(section, dict):
        raise ValueError(f"{POLICY_FILE} needs a [logs] table")
    unknown = sorted(set(section) - set(FIELDS))
    if unknown:
        raise ValueError(f"unknown key(s) in [logs]: {', '.join(unknown)} (known: {', '.join(FIELDS)})")
    values = {}
    for k in FIELDS:
        v = section.get(k, "")
        if not isinstance(v, str):
            raise ValueError(f'[logs] {k} must be a string such as "7d" or "5GB"')
        values[k] = v.strip()
    return validate(Policy(**values))


def read_policy(root: Path | str | None = None) -> Policy | None:
    """The venv's policy, or ``None`` when there is none. Raises ``ValueError``
    for a file that exists but is unreadable or invalid."""
    path = policy_path(root)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    try:
        return parse_policy(data)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def render_policy(policy: Policy) -> str:
    lines = [
        "# Log retention for this venv's logs/ — applied by the venv owner's runs,",
        "# at most once a day. Edit with `runspec logs retention`. See the SPEC,",
        '# "Log retention policy".',
        "[logs]",
    ]
    for k in FIELDS:
        v = getattr(policy, k)
        if v:
            lines.append(f'{k} = "{v}"')
    return "\n".join(lines) + "\n"


def write_policy(policy: Policy, root: Path | str | None = None) -> Path:
    """Validate and atomically write the policy (mode 0644). Returns its path."""
    validate(policy)
    path = policy_path(root)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_policy(policy), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def clear_policy(root: Path | str | None = None) -> bool:
    """Remove the policy file. True when there was one."""
    try:
        policy_path(root).unlink()
    except FileNotFoundError:
        return False
    return True


def update_policy(current: Policy | None, **changes: str | None) -> Policy:
    """Apply ``--compact-after``-style changes to ``current``: ``None`` keeps a
    field, ``"off"`` (or ``""``) clears it, anything else sets it."""
    base = Policy(**(current.as_dict() if current else {}))
    for k, v in changes.items():
        if k not in FIELDS or v is None:
            continue
        setattr(base, k, "" if v.strip().lower() in (OFF, "") else v.strip())
    return base


def trusted(root: Path | str | None = None) -> bool:
    """May the background pass act on this policy file? Only when it's owned by
    the venv owner or root and not writable by group or others (POSIX) — anyone
    else able to write it could make the owner's runs delete the logs."""
    if not hasattr(os, "geteuid"):
        return True
    try:
        st = os.stat(policy_path(root))
        owner = os.stat(venv_root(root)).st_uid
    except OSError:
        return False
    return st.st_uid in (owner, 0) and not (st.st_mode & 0o022)


# ── the last pass ─────────────────────────────────────────────────────────────


def read_last(root: Path | str | None = None) -> dict[str, Any] | None:
    try:
        return dict(json.loads((logs_dir(root) / LAST_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def _last_age(root: Path | str | None, now: float) -> float | None:
    try:
        return now - (logs_dir(root) / LAST_FILE).stat().st_mtime
    except OSError:
        return None


def due(root: Path | str | None = None, *, now: float | None = None) -> bool:
    """True when no pass ran in the last :data:`INTERVAL_S` seconds."""
    age = _last_age(root, now if now is not None else time.time())
    return age is None or age >= INTERVAL_S or age < 0


def _write_last(root: Path | str | None, result: dict[str, Any]) -> None:
    path = logs_dir(root) / LAST_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── applying it ───────────────────────────────────────────────────────────────


def apply(policy: Policy, root: Path | str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Prune what's past ``delete_after``, compact the rest past
    ``compact_after``, then enforce ``max_total_size`` (on the compressed sizes)
    — pruning first so compaction never folds a run that's due for deletion into
    a newer archive. Returns a summary
    (``compacted``, ``deleted``, ``freed_bytes``, ``dry_run``; ``error`` on a
    failure part-way). Never raises for an I/O problem — it's reported."""
    from . import logs as _logs

    d = logs_dir(root)
    result: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "policy": policy.as_dict(),
        "dry_run": dry_run,
        "compacted": 0,
        "deleted": 0,
        "freed_bytes": 0,
    }
    if not d.is_dir():
        return result
    sink = io.StringIO()

    def _prune(**kw: Any) -> None:
        count, freed = _logs.prune(None, dirs=[d], dry_run=dry_run, as_json=True, out=sink, **kw)
        result["deleted"] += count
        result["freed_bytes"] += freed

    try:
        if policy.delete_after:
            _prune(older_than=_logs.parse_duration(policy.delete_after))
        if policy.compact_after:
            result["compacted"] = _logs.compact(
                None,
                dirs=[d],
                older_than=_logs.parse_duration(policy.compact_after),
                gzip_=True,
                dry_run=dry_run,
                as_json=True,
                out=sink,
            )
        if policy.max_total_size:
            _prune(max_total_size=_logs.parse_size(policy.max_total_size))
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


@contextlib.contextmanager
def _locked(root: Path | str | None) -> Iterator[bool]:
    """Yield True while holding the venv's retention lock, False when another
    pass holds it (POSIX ``flock``; elsewhere always True)."""
    try:
        import fcntl
    except ImportError:
        yield True
        return
    path = logs_dir(root) / LOCK_FILE
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except OSError:
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def run_pass(root: Path | str | None = None, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """One locked pass: read the policy, apply it, record the outcome. ``force``
    ignores the once-a-day clock (``--now``); ``dry_run`` records nothing.
    Returns the summary, or ``{"skipped": reason}``."""
    try:
        policy = read_policy(root)
    except ValueError as exc:
        return {"skipped": "invalid", "error": str(exc)}
    if policy is None:
        return {"skipped": "no-policy"}
    if not logs_dir(root).is_dir():
        return {"skipped": "no-logs"}
    with _locked(root) as got:
        if not got:
            return {"skipped": "busy"}
        if not force and not due(root):
            return {"skipped": "not-due"}
        result = apply(policy, root, dry_run=dry_run)
        if not dry_run:
            with contextlib.suppress(OSError):
                _write_last(root, result)
        return result


def maybe_apply(log_dir: Path | str) -> bool:
    """The end-of-run hook: when ``log_dir`` is this venv's own ``logs/``, the
    caller owns the venv root, a trusted policy exists and a pass is due, start
    a detached ``python -m runspec.retention`` and return True. Never waits,
    never raises."""
    with contextlib.suppress(Exception):
        log_dir = Path(log_dir)
        root = log_dir.parent
        if log_dir.resolve() != logs_dir().resolve():
            return False
        if not policy_path(root).is_file() or not trusted(root) or not due(root):
            return False
        if hasattr(os, "geteuid") and os.stat(root).st_uid != os.geteuid():
            return False
        _spawn(root)
        return True
    return False


def _spawn(root: Path) -> None:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(root),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNSPEC_RUN_ID")}
    subprocess.Popen([sys.executable, "-m", "runspec.retention", str(root)], env=env, **kwargs)


# ── show ──────────────────────────────────────────────────────────────────────


def show(root: Path | str | None = None, *, as_json: bool = False, out: TextIO | None = None) -> dict[str, Any]:
    out = out or sys.stdout
    info: dict[str, Any] = {"venv": str(venv_root(root)), "file": str(policy_path(root))}
    try:
        policy = read_policy(root)
        info["policy"] = policy.as_dict() if policy else None
    except ValueError as exc:
        info["policy"] = None
        info["error"] = str(exc)
    info["last"] = read_last(root)
    if as_json:
        out.write(json.dumps(info, sort_keys=True) + "\n")
        return info
    if info.get("error"):
        out.write(f"✗  {info['error']}\n")
    elif not info["policy"]:
        out.write(f"No log retention policy for {info['venv']}.\n")
        out.write("   Set one: runspec logs retention --compact-after 7d --delete-after 90d\n")
    else:
        p = info["policy"]
        parts = []
        if p.get("compact_after"):
            parts.append(f"compress runs older than {p['compact_after']}")
        if p.get("delete_after"):
            parts.append(f"delete older than {p['delete_after']}")
        if p.get("max_total_size"):
            parts.append(f"keep at most {p['max_total_size']}")
        out.write(f"Log retention for {info['venv']}: " + ", ".join(parts) + ".\n")
    last = info["last"]
    if last:
        line = f"Last pass {last.get('ts', '?')}: compressed {last.get('compacted', 0)}, deleted {last.get('deleted', 0)} ({last.get('freed_bytes', 0)} bytes)"
        if last.get("error"):
            line += f" — failed: {last['error']}"
        out.write(line + ".\n")
    return info


def main(argv: list[str] | None = None) -> int:
    """``python -m runspec.retention <venv root>`` — the detached pass."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else None
    with contextlib.suppress(Exception):
        run_pass(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
