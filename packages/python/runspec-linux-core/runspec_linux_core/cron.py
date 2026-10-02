"""Cron (per-user crontab) management: list, add, remove, clear jobs.

Every helper drives the ``crontab`` binary and manages **one user's crontab**.
Which user is chosen *per invocation* via the shared ``run_as`` escalation
(mechanism B, like the container helpers): with ``run_as`` set, the
``crontab`` command is wrapped in ``sudo -n -u <user>`` / ``su <user> -c`` by
:func:`runspec_linux_core.become.build_become`, so ``crontab -l`` reads and
``crontab -`` writes *that user's* own crontab. With no ``run_as`` the caller's
own crontab is used. ``-n`` makes sudo fail fast instead of hanging on the
TTY-less SSH channel; passwordless sudo is assumed (see ``enable-passwordless-sudo``).

Editing a crontab is a read-modify-write: ``crontab -l`` to read the current
table, then the whole new table is piped to ``crontab -`` (crontab has no
append/delete verbs of its own). Schedules, commands, and comments are guarded
against newline injection so a crafted value can't smuggle extra crontab lines.

Each function returns plain data and raises on failure (``ToolNotFoundError``
when ``crontab`` is absent, ``CommandError`` for a non-zero subprocess,
``ValueError`` for bad input). The thin ``runspec-linux`` wrappers render the
JSON/exit.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error, current_user
from runspec_linux_core.errors import ToolNotFoundError

# The nickname schedules cron accepts in place of the 5 time fields.
_SPECIALS = frozenset(
    {
        "@reboot",
        "@yearly",
        "@annually",
        "@monthly",
        "@weekly",
        "@daily",
        "@midnight",
        "@hourly",
    }
)

# Characters allowed in a single cron time field (digits, ``*``, ranges, steps,
# lists, and month/day-of-week names). Deliberately permissive — cron itself is
# the final arbiter of a field's meaning; this only rejects obvious garbage and
# stray whitespace so we don't write a malformed table.
_FIELD_RE = re.compile(r"^[0-9*/,\-A-Za-z]+$")

# An environment assignment line in a crontab (``NAME=value``), as cron treats it.
_ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _crontab_available() -> bool:
    return shutil.which("crontab") is not None


def _no_such_crontab(stderr: str) -> bool:
    """True when ``crontab -l`` failed only because the user has no crontab yet
    (an empty table, not a real error)."""
    return "no crontab for" in stderr.lower()


def _reject_newline(value: str, what: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{what} must not contain a newline")
    return value


def validate_schedule(schedule: str) -> str:
    """Return ``schedule`` unchanged if it is a well-formed cron schedule — a
    recognised ``@nickname`` or exactly five whitespace-separated time fields —
    else raise :class:`ValueError`.

    This is a shape check, not a full semantic validation: each field only has to
    use cron's grammar characters. crontab itself rejects out-of-range values.
    """
    _reject_newline(schedule, "schedule")
    text = schedule.strip()
    if not text:
        raise ValueError("empty schedule")
    if text.startswith("@"):
        if text.lower() not in _SPECIALS:
            raise ValueError(f"unknown schedule nickname: {text!r} (expected one of {', '.join(sorted(_SPECIALS))})")
        return text.lower()
    fields = text.split()
    if len(fields) != 5:
        raise ValueError(f"schedule needs 5 fields (min hour day-of-month month day-of-week) or an @nickname; got {len(fields)}: {text!r}")
    for field in fields:
        if not _FIELD_RE.match(field):
            raise ValueError(f"invalid character in schedule field {field!r}")
    return " ".join(fields)


def _parse_line(line: str) -> dict:
    """Classify one crontab line into a typed record (job / special / env /
    comment / blank / unknown). Job/special records carry ``schedule`` +
    ``command``; the caller stitches on any preceding comment."""
    stripped = line.strip()
    if not stripped:
        return {"kind": "blank"}
    if stripped.startswith("#"):
        return {"kind": "comment", "text": stripped.lstrip("#").strip(), "raw": line}
    if stripped.startswith("@"):
        parts = stripped.split(None, 1)
        nickname = parts[0].lower()
        if nickname in _SPECIALS and len(parts) == 2:
            return {"kind": "special", "schedule": nickname, "command": parts[1], "raw": line}
        return {"kind": "unknown", "raw": line}
    env = _ENV_RE.match(stripped)
    if env:
        return {"kind": "env", "name": env.group(1), "value": env.group(2).strip(), "raw": line}
    parts = stripped.split(None, 5)
    if len(parts) == 6:
        return {"kind": "job", "schedule": " ".join(parts[:5]), "command": parts[5], "raw": line}
    return {"kind": "unknown", "raw": line}


def _read_crontab(run_as: str | None, become_method: str) -> tuple[list[str], bool]:
    """Return ``(lines, exists)`` for the target user's current crontab.

    ``exists`` is False (and ``lines`` empty) when the user simply has no crontab
    yet — that is the normal empty state, not an error. Raises CommandError on a
    real ``crontab -l`` failure (with a passwordless-sudo hint when escalating).
    """
    result = subprocess.run(build_become(["crontab", "-l"], run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        if _no_such_crontab(result.stderr):
            return [], False
        check_become_error("read crontab", result.stderr, run_as)
    return result.stdout.splitlines(), True


def _write_crontab(lines: list[str], run_as: str | None, become_method: str) -> None:
    """Install ``lines`` as the target user's whole crontab via ``crontab -``.
    Raises CommandError on failure (with a passwordless-sudo hint when escalating)."""
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    result = subprocess.run(
        build_become(["crontab", "-"], run_as, become_method),
        input=content,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        check_become_error("write crontab", result.stderr, run_as)


def _target_user(run_as: str | None) -> str:
    return run_as or current_user()


def list_cron(*, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """List the jobs in a user's crontab as structured records.

    Reads ``crontab -l`` (as ``run_as`` when set) and parses it. A ``#`` comment
    line directly above a job is attached to that job as its ``comment`` (the
    convention :func:`add_cron` writes). Environment assignments are returned
    separately. A user with no crontab yet is reported as ``exists: false`` with
    no jobs — not an error.

    Returns ``{user, run_as, exists, jobs: [{index, schedule, command, comment,
    special, raw}], env: [{name, value}], count}``. Raises ToolNotFoundError if
    ``crontab`` is not installed, CommandError on a real read failure.
    """
    if not _crontab_available():
        raise ToolNotFoundError("crontab not installed or not in PATH")

    raw_lines, exists = _read_crontab(run_as, become_method)
    jobs: list[dict] = []
    env: list[dict] = []
    pending_comment: str | None = None
    index = 0
    for line in raw_lines:
        rec = _parse_line(line)
        kind = rec["kind"]
        if kind == "comment":
            pending_comment = rec["text"]
        elif kind in ("job", "special"):
            jobs.append(
                {
                    "index": index,
                    "schedule": rec["schedule"],
                    "command": rec["command"],
                    "comment": pending_comment,
                    "special": kind == "special",
                    "raw": rec["raw"],
                }
            )
            index += 1
            pending_comment = None
        elif kind == "env":
            env.append({"name": rec["name"], "value": rec["value"]})
            pending_comment = None
        else:  # blank / unknown — drop any dangling comment association
            pending_comment = None

    return {
        "user": _target_user(run_as),
        "run_as": run_as or "",
        "exists": exists,
        "jobs": jobs,
        "env": env,
        "count": len(jobs),
    }


def add_cron(
    schedule: str,
    command: str,
    *,
    comment: str | None = None,
    run_as: str | None = None,
    become_method: str = "sudo",
    allow_duplicate: bool = False,
) -> dict:
    """Add a job (``schedule command``) to a user's crontab.

    ``schedule`` is validated (a 5-field spec or an ``@nickname``); ``command`` is
    written verbatim (cron runs it via ``/bin/sh``). When ``comment`` is given it
    is written as a ``# comment`` line directly above the job, so
    :func:`list_cron` reads it back as the job's ``comment``. Unless
    ``allow_duplicate`` is set, an identical ``schedule command`` line already
    present is left as-is (idempotent) and reported with ``added: false``.

    Returns ``{user, run_as, schedule, command, comment, added, count}``. Raises
    ToolNotFoundError if ``crontab`` is absent, ValueError on bad input,
    CommandError on a read/write failure.
    """
    if not _crontab_available():
        raise ToolNotFoundError("crontab not installed or not in PATH")

    schedule = validate_schedule(schedule)
    command = _reject_newline(command.strip(), "command")
    if not command:
        raise ValueError("empty command")
    if comment is not None:
        comment = _reject_newline(comment.strip(), "comment") or None

    lines, _ = _read_crontab(run_as, become_method)
    new_line = f"{schedule} {command}"

    if not allow_duplicate and any(line.strip() == new_line for line in lines):
        listing = list_cron(run_as=run_as, become_method=become_method)
        return {
            "user": _target_user(run_as),
            "run_as": run_as or "",
            "schedule": schedule,
            "command": command,
            "comment": comment,
            "added": False,
            "count": listing["count"],
        }

    if lines and lines[-1].strip() != "":
        # keep a clean separation; harmless if a blank already trails
        pass
    if comment:
        lines.append(f"# {comment}")
    lines.append(new_line)
    _write_crontab(lines, run_as, become_method)

    listing = list_cron(run_as=run_as, become_method=become_method)
    return {
        "user": _target_user(run_as),
        "run_as": run_as or "",
        "schedule": schedule,
        "command": command,
        "comment": comment,
        "added": True,
        "count": listing["count"],
    }


def remove_cron(
    *,
    match: str | None = None,
    index: int | None = None,
    run_as: str | None = None,
    become_method: str = "sudo",
) -> dict:
    """Remove job(s) from a user's crontab.

    Exactly one selector must be given:

    - ``index`` — remove the job at that 0-based position among the crontab's
      jobs (the ``index`` reported by :func:`list_cron`); a ``# comment`` line
      directly above it is removed too.
    - ``match`` — remove every job whose command contains ``match`` as a
      substring, or whose full ``schedule command`` line equals it; each removed
      job's preceding ``# comment`` line is removed too.

    Removing nothing is not an error (``removed: []``); the crontab is only
    rewritten when something was actually removed. Returns ``{user, run_as,
    removed: [{schedule, command}], count}`` (``count`` = jobs remaining). Raises
    ToolNotFoundError if ``crontab`` is absent, ValueError on a bad selector,
    CommandError on a read/write failure.
    """
    if not _crontab_available():
        raise ToolNotFoundError("crontab not installed or not in PATH")
    if (index is None) == (match is None):
        raise ValueError("give exactly one of index or match")

    lines, exists = _read_crontab(run_as, become_method)

    # Walk the raw lines, tagging each job with its 0-based job index and the raw
    # line-number of a comment directly above it, so we can drop both together.
    job_index = 0
    drop_lines: set[int] = set()
    removed: list[dict] = []
    prev_comment_lineno: int | None = None
    for lineno, line in enumerate(lines):
        rec = _parse_line(line)
        kind = rec["kind"]
        if kind == "comment":
            prev_comment_lineno = lineno
            continue
        if kind in ("job", "special"):
            if index is not None:
                selected = index == job_index
            else:
                assert match is not None  # guaranteed by the XOR selector check above
                selected = match in rec["command"] or line.strip() == match.strip()
            if selected:
                drop_lines.add(lineno)
                if prev_comment_lineno is not None:
                    drop_lines.add(prev_comment_lineno)
                removed.append({"schedule": rec["schedule"], "command": rec["command"]})
            job_index += 1
        prev_comment_lineno = None

    if index is not None and not removed:
        raise ValueError(f"no job at index {index} (crontab has {job_index} job(s))")

    if removed:
        kept = [line for lineno, line in enumerate(lines) if lineno not in drop_lines]
        _write_crontab(kept, run_as, become_method)

    listing = list_cron(run_as=run_as, become_method=become_method) if exists else {"count": 0}
    return {
        "user": _target_user(run_as),
        "run_as": run_as or "",
        "removed": removed,
        "count": listing["count"],
    }


def clear_cron(*, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Remove a user's entire crontab (``crontab -r``).

    A no-op (reported ``existed: false``) when the user has no crontab. Returns
    ``{user, run_as, cleared, existed}``. Raises ToolNotFoundError if ``crontab``
    is absent, CommandError on a real removal failure.
    """
    if not _crontab_available():
        raise ToolNotFoundError("crontab not installed or not in PATH")

    _, exists = _read_crontab(run_as, become_method)
    if not exists:
        return {"user": _target_user(run_as), "run_as": run_as or "", "cleared": False, "existed": False}

    result = subprocess.run(build_become(["crontab", "-r"], run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("clear crontab", result.stderr, run_as)
    return {"user": _target_user(run_as), "run_as": run_as or "", "cleared": True, "existed": True}
