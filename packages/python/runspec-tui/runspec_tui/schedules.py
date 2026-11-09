"""schedules.py — the Schedules screen's talk to runspec-scheduler, no terminal code.

The screen drives this venv's own ``runspec-scheduler`` the way runspec-console
does: through its JSON verbs (``status``, ``enable``, ``add``, ``run-now``, …).
Reads run as you. Anything that writes runs as the venv's owner — the account
that owns the scheduler config and the daemon — through ``sudo -n -u <owner>``
when you aren't that account, so a missing sudo rule is a clear refusal, never
a password prompt inside the TUI.

Everything here is plain data in and out, so ``tests/test_schedules.py`` covers
it without a terminal or a real scheduler.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import form

#: Verbs that change the config, the crontab or the daemon: run as the owner.
WRITE_VERBS = frozenset({"add", "remove", "enable", "disable", "sync", "use", "settings", "restart", "run-now"})

#: ``runspec history`` verbs for "I'm on it" / "dealt with" / "release".
NOTE_VERBS = {"claim": "claim", "resolve": "resolve", "release": "release"}


def scheduler_binary() -> Path | None:
    """This venv's ``runspec-scheduler``, or ``None`` when it isn't installed."""
    from runspec.execute import find_script

    found = find_script("runspec-scheduler")
    return Path(found[0]) if found else None


def owner_and_user() -> tuple[str, str]:
    """``(venv owner, you)``. The owner is ``""`` when there is none to switch
    to (a root-owned venv, or no POSIX accounts)."""
    from runspec.become import current_user, environment_owner

    return environment_owner(), current_user()


@dataclass(frozen=True)
class Command:
    """One command to run: argv, the env frame for its stdin (when it switches
    account and carries variables), and variables for its own environment."""

    argv: list[str]
    stdin: bytes | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Caller:
    """How to run this venv's scheduler: its binary, and who must write."""

    binary: Path
    owner: str = ""
    user: str = ""

    @property
    def as_owner(self) -> bool:
        """Whether writes need ``sudo -n -u <owner>``."""
        return bool(self.owner) and self.owner != self.user

    def _wrap(self, cmd: list[str], write: bool, env: Mapping[str, str] | None) -> Command:
        if not (write and self.as_owner):
            return Command(cmd, None, dict(env or {}))
        from runspec.become import build_exec_argv

        # sudo drops the environment: variables go in the env frame on stdin.
        argv, stdin = build_exec_argv(cmd, self.owner, "sudo", "-n", env=dict(env or {}) or None)
        return Command(argv, stdin)

    def command(self, verb: str, *args: str, env: Mapping[str, str] | None = None) -> Command:
        """``runspec-scheduler <verb> …`` with JSON output."""
        return self._wrap([str(self.binary), verb, *args, "--json"], verb in WRITE_VERBS, env)

    def history(self, verb: str, *args: str) -> Command:
        """``runspec history claim|resolve|release …``: the notes live in the
        runs index, which only the owner writes."""
        runspec = self.binary.with_name("runspec")
        return self._wrap([str(runspec), "history", NOTE_VERBS[verb], *args, "--scope", "venv", "--json"], True, None)


@dataclass
class Result:
    """What a scheduler call came back with."""

    code: int
    payload: dict[str, Any] = field(default_factory=dict)
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0 and self.payload.get("ok", True) is not False

    @property
    def message(self) -> str:
        """The reason it failed, in one line."""
        if "a password is required" in self.stderr or "a terminal is required" in self.stderr:
            return "sudo asked for a password: this needs a passwordless sudo rule to the venv owner"
        text = str(self.payload.get("error") or "").strip()
        if not text:
            lines = [ln.strip() for ln in self.stderr.splitlines() if ln.strip()]
            text = lines[-1] if lines else f"exit {self.code}"
        return text


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def call(cmd: Command, *, timeout: float = 60, run: Runner = subprocess.run) -> Result:
    """Run one command and read its JSON (the last JSON line, so a stray log
    line before it doesn't break the read)."""
    env = {**os.environ, **cmd.env} if cmd.env else None
    try:
        proc = run(
            cmd.argv,
            capture_output=True,
            timeout=timeout,
            input=cmd.stdin if cmd.stdin is not None else b"",
            env=env,
        )
    except FileNotFoundError as exc:
        return Result(127, stderr=str(exc))
    except subprocess.TimeoutExpired:
        return Result(124, stderr=f"no answer after {timeout:.0f}s")
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    payload: dict[str, Any] = {}
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break
    return Result(proc.returncode, payload, stderr)


# ── what a row shows ──────────────────────────────────────────────────────────


def parse_ts(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    try:
        moment = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def span(seconds: float) -> str:
    s = int(max(0, seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            big, rest = divmod(s, size)
            small = {"d": ("h", 3600), "h": ("m", 60), "m": ("s", 1)}[unit]
            part = rest // small[1]
            return f"{big}{unit}" + (f" {part}{small[0]}" if part else "")
    return f"{s}s"


def when_text(rec: Mapping[str, Any]) -> str:
    """A schedule's timing: a package schedule in its package's words."""
    pkg = rec.get("package") or {}
    if pkg and "when" not in (pkg.get("overridden") or []) and pkg.get("words"):
        return str(pkg["words"])
    when = rec.get("when") or {}
    text = f"every {when['every']}" if when.get("every") else str(when.get("cron") or "-")
    zone = str(when.get("timezone") or "")
    if zone and zone.lower() != "local":
        text += f" ({zone})"
    return text


def next_text(rec: Mapping[str, Any], now: _dt.datetime) -> str:
    if not rec.get("enabled", True):
        return "-"
    moment = parse_ts(rec.get("next_run"))
    if moment is None:
        return "-"
    local = moment.astimezone(now.tzinfo)
    if local.date() == now.date():
        day = local.strftime("%H:%M")
    elif local.date() == now.date() + _dt.timedelta(days=1):
        day = local.strftime("tomorrow %H:%M")
    else:
        day = local.strftime("%a %d %b %H:%M")
    return f"{day} (in {span((moment - now).total_seconds())})"


def last_text(rec: Mapping[str, Any], now: _dt.datetime) -> str:
    runs = rec.get("runs") or []
    if not runs:
        return "never"
    run = runs[0]
    status = str(run.get("status") or "")
    began = parse_ts(run.get("start_ts"))
    when = f", {span((now - began).total_seconds())} ago" if began else ""
    if status == "running":
        return "running" + (f" ({span((now - began).total_seconds())})" if began else "")
    if status == "skipped":
        return "skipped" + when
    code = run.get("exit_code")
    if status == "completed" or code == 0:
        return "ok" + when
    return ("failed" if code in (None, "") else f"failed (exit {code})") + when


def state_text(rec: Mapping[str, Any], backend: str) -> tuple[str, str]:
    """``(state words, style)`` for the row's last column."""
    if not rec.get("enabled", True):
        return ("offered", "cyan") if rec.get("offered") else ("paused", "dim")
    attention = rec.get("attention") or {}
    state = attention.get("state")
    if state == "failed":
        count = attention.get("failures") or 0
        return "failing" + (f" ({count})" if count > 1 else ""), "red"
    if state == "handling":
        who = (attention.get("note") or {}).get("actor")
        return "being handled" + (f" by {who}" if who else ""), "yellow"
    if rec.get("installable") is False:
        return "won't run", "red"
    if rec.get("installed") is False:
        return ("not loaded" if backend == "daemon" else "not in crontab"), "yellow"
    if state == "done":
        return "dealt with", "green"
    return "ok", "green"


def daemon_text(payload: Mapping[str, Any]) -> tuple[str, str]:
    """``(words, style)`` for the daemon in the strip."""
    daemon = payload.get("daemon") or {}
    backend = payload.get("backend") or "cron"
    state = daemon.get("state") or "stopped"
    if state == "stopped":
        return ("not running", "red") if backend == "daemon" else ("not used (cron)", "dim")
    if state == "stale":
        return "not responding", "red"
    text = f"running, pid {daemon.get('pid')}"
    if daemon.get("version"):
        text += f", {daemon['version']}"
    if daemon.get("restart_needed"):
        return text + " — restart needed", "yellow"
    if backend != "daemon":
        return text + " (fires nothing: cron)", "yellow"
    return text, "green"


def zone_text(payload: Mapping[str, Any]) -> str:
    settings = payload.get("settings") or {}
    venv, host = settings.get("timezone") or "", payload.get("host_zone") or ""
    if venv:
        return f"{venv} (venv)"
    return f"{host} (host)" if host else "host"


def detail_lines(rec: Mapping[str, Any], now: _dt.datetime) -> list[tuple[str, str]]:
    """``(text, style)`` lines for the selected schedule's detail pane."""
    out: list[tuple[str, str]] = []
    target = " ".join([str(rec.get("runnable") or ""), *[str(c) for c in rec.get("command") or []]])
    out.append((f"{rec.get('id')}  ·  {target}", "bold"))
    if rec.get("description"):
        out.append((str(rec["description"]), ""))
    pkg = rec.get("package") or {}
    if pkg:
        line = f"From the {rec.get('runnable')} package: {pkg.get('words')}"
        if pkg.get("overridden"):
            line += f" (this venv changes: {', '.join(pkg['overridden'])})"
        out.append((line, "dim"))
    if rec.get("args"):
        out.append(("Args: " + ", ".join(f"{k}={v}" for k, v in rec["args"].items()), "dim"))
    if rec.get("run_as"):
        out.append((f"Runs as {rec['run_as']}", "dim"))
    if rec.get("not_installable_reason") and rec.get("installable") is False:
        out.append((f"Won't run: {rec['not_installable_reason']}", "red"))
    attention = rec.get("attention") or {}
    note = attention.get("note") or {}
    if attention.get("state") in ("failed", "handling", "done"):
        head = {"failed": "Failing", "handling": "Being handled", "done": "Dealt with"}[attention["state"]]
        if attention.get("failures"):
            head += f" — {attention['failures']} failed run{'s' if attention['failures'] != 1 else ''}"
        out.append((head, {"failed": "red", "handling": "yellow", "done": "green"}[attention["state"]]))
        if note:
            out.append((f"  {note.get('actor') or 'someone'}: {note.get('note') or '(no note)'}", ""))
    runs = rec.get("runs") or []
    if runs:
        out.append(("Recent runs:", "bold"))
        for run in runs:
            began = parse_ts(run.get("start_ts"))
            stamp = began.astimezone(now.tzinfo).strftime("%a %d %b %H:%M") if began else "?"
            code = run.get("exit_code")
            secs = f", {run['duration_ms'] / 1000:.1f}s" if isinstance(run.get("duration_ms"), (int, float)) else ""
            word = run.get("status") or ""
            style = "green" if code == 0 else ("red" if code not in (None, "") else "")
            out.append((f"  {stamp}  {word}" + (f" (exit {code})" if code not in (None, "", 0) else "") + secs, style))
    output = rec.get("last_output") or {}
    tail: list[str] = []
    for text in (output.get("stderr"), output.get("stdout")):
        if text and not tail:
            tail = str(text).rstrip().splitlines()[-12:]
    if tail:
        out.append(("Last output:", "bold"))
        out += [(f"  | {line}", "dim") for line in tail]
    return out


# ── adding a schedule ─────────────────────────────────────────────────────────


def schedule_args(args: list[dict[str, Any]], values: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """``(args for --args, problems)`` from a filled-in run form.

    Only values that differ from the default are kept (the rest come from the
    spec, ``.runspec_env`` and the schedule env file at fire time). A secret
    can't go in a schedule's args (they sit in the config and crontab): it
    belongs in ``runspec_schedules.env``."""
    out: dict[str, Any] = {}
    problems: list[str] = []
    for arg in args:
        name = arg["name"]
        raw = form.to_raw(arg, values.get(name))
        if raw is None or form.same_as_default(arg, raw):
            continue
        if form.widget_kind(arg) == "password":
            problems.append(f"{name} is a secret: leave it blank here and put it in the venv's runspec_schedules.env")
            continue
        out[name] = raw
    return out, problems


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def id_problem(sid: str) -> str:
    if not _ID_RE.match(sid):
        return "letters, digits, '.', '_' or '-', starting with a letter or digit (at most 64)"
    return ""


def suggest_id(runnable: str, path: tuple[str, ...] | list[str], taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join([runnable, *path])).strip("-.") or "schedule"
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    return sid[:64]


#: The timing kinds the add/edit dialog offers.
TIMING_KINDS = (("daily", "Every day at"), ("weekdays", "Weekdays at"), ("cron", "Cron expression"), ("every", "Every (daemon only)"))


def timing_args(kind: str, value: str, timezone: str = "") -> tuple[list[str], str]:
    """``(add's timing args, problem)`` from the dialog: a time for daily /
    weekdays, a cron expression, or an interval like ``15m``."""
    text = value.strip()
    if kind in ("daily", "weekdays"):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return [], "a time like 02:30"
        dow = "mon-fri" if kind == "weekdays" else "*"
        out = ["--cron", f"{int(m.group(2))} {int(m.group(1))} * * {dow}"]
    elif kind == "cron":
        if not (text.startswith("@") or len(text.split()) == 5):
            return [], "5 fields like '0 2 * * *', or @daily / @hourly / @weekly"
        out = ["--cron", text]
    elif kind == "every":
        if not re.fullmatch(r"\d+[smhd]", text.lower()):
            return [], "a number and s/m/h/d, like 15m"
        out = ["--every", text.lower()]
    else:
        return [], f"unknown kind {kind!r}"
    if timezone.strip():
        out += ["--timezone", timezone.strip()]
    return out, ""


def timing_prefill(rec: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(kind, value, timezone)`` for editing a schedule's time."""
    when = rec.get("when") or {}
    zone = str(when.get("timezone") or "")
    zone = "" if zone.lower() == "local" else zone
    if when.get("every"):
        return "every", str(when["every"]), zone
    cron = str(when.get("cron") or "")
    parts = cron.split()
    if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[0].isdigit() and parts[1].isdigit():
        clock = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        if parts[4] == "*":
            return "daily", clock, zone
        if parts[4] == "mon-fri":
            return "weekdays", clock, zone
    return "cron", cron, zone


#: ``enabled`` defaults to true and a runspec flag can only be switched on from
#: the command line, so a schedule saved switched off sets this to 0.
ENABLED_ENV = "RUNSPEC_RUNSPEC_SCHEDULER_ARG_ENABLED"


def edit_problem(rec: Mapping[str, Any]) -> str:
    """Why a schedule's time can't be changed from here ("" = it can)."""
    if rec.get("env_file"):
        return "it carries credentials, which saving again would drop: change it with runspec-scheduler add on the host"
    return ""


def add_argv(rec: Mapping[str, Any], timing: list[str], *, backend: str, default_timeout: int | None = None) -> tuple[list[str], dict[str, str]]:
    """``(add's args, env)`` to save ``rec`` (a status row, or a new schedule)
    with ``timing``: same id, runnable, command, args and catch-up; switched
    off stays off (an offered package schedule stays offered — the scheduler
    writes only what differs from the package).

    A daemon venv's status shows each schedule's effective timeout, so one
    equal to the venv default isn't written back as the schedule's own."""
    out = ["--id", str(rec["id"]), "--runnable", str(rec["runnable"])]
    if rec.get("command"):
        out += ["--command", " ".join(str(c) for c in rec["command"])]
    if rec.get("args"):
        out += ["--args", json.dumps(rec["args"], separators=(",", ":"), sort_keys=True)]
    timeout = rec.get("timeout")
    if timeout is not None and not (backend == "daemon" and timeout == default_timeout):
        out += ["--timeout", str(timeout)]
    if rec.get("catch_up"):
        out += ["--catch-up"]
    env = {ENABLED_ENV: "0"} if rec.get("enabled") is False else {}
    return out + timing, env


# ── settings ──────────────────────────────────────────────────────────────────


def settings_argv(before: Mapping[str, Any], after: Mapping[str, str]) -> list[str]:
    """``settings``'s args for what the dialog changed (only those)."""
    out: list[str] = []
    for key, flag in (("timeout", "--timeout"), ("misfire_grace", "--misfire-grace"), ("catch_up_window", "--catch-up-window")):
        new = str(after.get(key, "")).strip()
        if new and new != str(before.get(key, "")):
            out += [flag, new]
    zone = str(after.get("timezone", "")).strip()
    if zone != str(before.get("timezone") or ""):
        out += ["--timezone", zone or "host"]
    policy = str(after.get("package_schedules", "")).strip()
    if policy and policy != str(before.get("package_schedules") or "offer"):
        out += ["--package-schedules", policy]
    return out
