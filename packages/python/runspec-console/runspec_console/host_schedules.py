"""host_schedules.py — the pure half of editing a venv's runspec-scheduler
schedules from the Schedules tab (console 0.385.0).

The console never writes a host's crontab or ``runspec_scheduler.toml`` itself:
it runs that venv's own ``runspec-scheduler`` as the venv owner (``add`` /
``remove``, then ``sync`` on a cron-backed venv; a daemon reloads on its own).
This module validates what the editor sends and builds those argv lists;
``bridge_schedules`` runs them. No credentials: a host schedule never carries an
operator's secrets (the 0.163.0 decision), so ``password`` args are refused.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: A schedule id is a crontab marker token (``# runspec:sched:<id>``) and a
#: file name for its lock: keep it to one safe word.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EVERY_RE = re.compile(r"^\d+\s*[smhd]$")
_TIMEOUT_RE = re.compile(r"^\d+\s*[smhd]?$")
_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class HostScheduleError(ValueError):
    """What the editor sent can't be saved (message is shown to the operator)."""


def validate_id(value: Any) -> str:
    sid = str(value or "").strip()
    if not _ID_RE.match(sid):
        raise HostScheduleError(
            "Schedule id: letters, digits, '.', '_' or '-' (up to 64), starting with a letter or digit."
        )
    return sid


def _cron(value: str) -> str:
    text = " ".join(value.split())
    if text.startswith("@"):
        if text not in (
            "@hourly",
            "@daily",
            "@weekly",
            "@monthly",
            "@yearly",
            "@annually",
            "@midnight",
        ):
            raise HostScheduleError(f"Unknown cron shortcut {text!r}.")
        return text
    if len(text.split(" ")) != 5:
        raise HostScheduleError(
            "Cron needs 5 fields: minute hour day-of-month month day-of-week."
        )
    return text


#: ``enabled`` defaults to true and a runspec flag can only be switched on from
#: the command line, so a paused schedule is saved with this variable set to 0.
ENABLED_ENV = "RUNSPEC_RUNSPEC_SCHEDULER_ARG_ENABLED"


def save_argv(
    payload: dict[str, Any], *, backend: str
) -> tuple[str, list[str], dict[str, str]]:
    """``(id, argv, env)`` for ``runspec-scheduler add …`` from the editor's
    payload (``env`` carries ``enabled = false``, see :data:`ENABLED_ENV`).

    ``payload``: ``id``, ``runnable``, optional ``command`` (list or space-joined
    string), exactly one of ``cron`` / ``every`` (``every`` needs the daemon
    backend), optional ``timezone``, ``args`` (a JSON object), ``enabled``,
    ``timeout`` (``90m`` / ``6h`` / seconds; ``0`` = none), ``catch_up``."""
    sid = validate_id(payload.get("id"))
    runnable = str(payload.get("runnable") or "").strip()
    if not _NAME_RE.match(runnable):
        raise HostScheduleError("Pick a runnable.")
    raw_cmd = payload.get("command") or []
    if isinstance(raw_cmd, str):
        raw_cmd = raw_cmd.split()
    command = [str(c).strip() for c in raw_cmd if str(c).strip()]
    for c in command:
        if not _NAME_RE.match(c):
            raise HostScheduleError(f"Subcommand {c!r} isn't a command name.")
    cron = str(payload.get("cron") or "").strip()
    every = str(payload.get("every") or "").strip()
    if bool(cron) == bool(every):
        raise HostScheduleError("Set either a cron expression or an interval.")
    argv = ["add", "--id", sid, "--runnable", runnable]
    if command:
        argv += ["--command", " ".join(command)]
    if cron:
        argv += ["--cron", _cron(cron)]
    else:
        if backend != "daemon":
            raise HostScheduleError(
                "Intervals need the scheduler daemon; this venv uses cron. Use a cron expression."
            )
        if not _EVERY_RE.match(every):
            raise HostScheduleError("Interval: a number then s, m, h or d (e.g. 15m).")
        argv += ["--every", every.replace(" ", "")]
    tz = str(payload.get("timezone") or "").strip()
    if tz:
        if not _TZ_RE.match(tz):
            raise HostScheduleError(
                f"Timezone {tz!r} isn't an IANA name like Europe/London."
            )
        argv += ["--timezone", tz]
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        raise HostScheduleError("Arguments must be a set of name = value pairs.")
    secret = [str(k) for k in payload.get("password_args") or [] if str(k) in args]
    if secret:
        raise HostScheduleError(
            f"{', '.join(secret)}: a host schedule can't carry a password. "
            "Set it on the host (the venv's .runspec_env) instead."
        )
    clean = {str(k): v for k, v in args.items() if v is not None and v != ""}
    if clean:
        argv += ["--args", json.dumps(clean, separators=(",", ":"), sort_keys=True)]
    env = {ENABLED_ENV: "0"} if payload.get("enabled") is False else {}
    timeout = str(payload.get("timeout") or "").strip()
    if timeout:
        if not _TIMEOUT_RE.match(timeout):
            raise HostScheduleError(
                "Timeout: seconds, or a number then s, m, h or d (0 = no limit)."
            )
        argv += ["--timeout", timeout.replace(" ", "")]
    if payload.get("catch_up"):
        if backend != "daemon":
            raise HostScheduleError(
                "Catch-up needs the scheduler daemon; cron skips missed fires."
            )
        argv.append("--catch-up")
    return sid, argv, env


def last_json(stdout: str) -> dict[str, Any] | None:
    """The last JSON-object line of ``runspec-scheduler`` output (it prints one)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            got = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict):
            return got
    return None


def failure(code: int, stdout: str, stderr: str) -> str:
    """One readable line for a failed ``runspec-scheduler`` call."""
    got = last_json(stdout)
    if got and got.get("error"):
        return str(got["error"])
    if got and got.get("errors"):
        errs = got["errors"]
        if isinstance(errs, list):
            return "; ".join(
                f"{e.get('user', '?')}: {e.get('error', '')}"
                if isinstance(e, dict)
                else str(e)
                for e in errs
            )
    text = (stderr or "").strip() or (stdout or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {code}"


# ── the daemon: settings + restart (runspec-scheduler 0.7.0) ────────────────────

#: Older schedulers answer these verbs with "unknown command".
DAEMON_VERBS_MIN = "0.7.0"

_DURATION = re.compile(r"^\d+[smhd]?$")

#: payload key → ``runspec-scheduler settings`` flag.
SETTING_FLAGS = {
    "timeout": "--timeout",
    "misfire_grace": "--misfire-grace",
    "catch_up_window": "--catch-up-window",
}


def settings_argv(payload: dict[str, Any]) -> list[str]:
    """``settings`` argv for the durations ``payload`` sets (seconds or
    90m / 6h / 1d; the scheduler validates again). Nothing set is an error."""
    argv = ["settings"]
    for key, flag in SETTING_FLAGS.items():
        raw = payload.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip().lower()
        if not _DURATION.match(text):
            raise HostScheduleError(
                f"{key.replace('_', ' ')} must be seconds or a duration like 90m / 6h / 1d (got {raw!r})"
            )
        argv += [flag, text]
    if len(argv) == 1:
        raise HostScheduleError("Nothing to change.")
    return argv


#: runspec version that keeps run notes (`runspec history claim/resolve/release`).
NOTES_MIN = "0.83.0"

NOTE_VERBS = ("claim", "resolve", "release")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,79}$")


def note_argv(payload: dict[str, Any]) -> list[str]:
    """``runspec history <verb> <ids> --note … --json`` for a run note. Run ids
    are checked to the shape runspec writes (they become one argv element, never
    a shell word); resolve needs a note."""
    verb = str(payload.get("verb") or "")
    if verb not in NOTE_VERBS:
        raise HostScheduleError(f"verb must be one of {', '.join(NOTE_VERBS)}")
    runs = [str(r).strip() for r in payload.get("runs") or [] if str(r).strip()]
    if not runs:
        raise HostScheduleError("pick at least one failing schedule")
    bad = [r for r in runs if not _RUN_ID.match(r)]
    if bad:
        raise HostScheduleError(f"not a run id: {bad[0]!r}")
    note = " ".join(str(payload.get("note") or "").split())[:500]
    if verb == "resolve" and not note:
        raise HostScheduleError("say what was done, so the next person knows")
    argv = ["history", verb, ",".join(dict.fromkeys(runs)), "--scope", "venv", "--json"]
    if note:
        argv[3:3] = ["--note", note]
    return argv


def notes_too_old(error: str) -> bool:
    """Whether ``error`` is a runspec that predates run notes."""
    return "unknown history verb" in (error or "").lower()


def too_old(error: str) -> bool:
    """Whether ``error`` is a scheduler that predates the daemon verbs."""
    return "unknown command" in (error or "").lower()
