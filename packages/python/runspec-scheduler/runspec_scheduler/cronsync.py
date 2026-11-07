"""cronsync.py — render schedules into the OS crontab (the v1 shared-scheduler backend).

Instead of a long-running daemon, each enabled cron-timed schedule becomes one
**marked line in its ``run_as`` account's own crontab**, and the OS ``cron``
executes it — so there is no process of ours to supervise (design:
``docs/design/cron-scheduler.md``). The line runs the runnable through core's
``runspec exec`` wrapper, which records each fire in the per-venv runs index
(``runspec_runs.db``) and the run's audit log — that is the observability
channel (read back by ``runspec-scheduler status``); this module writes no
feedback of its own.

Shape of one installed schedule (in ``run_as``'s crontab, so cron runs it *as*
``run_as`` — no per-fire ``sudo``; the escalation is at crontab-**write** time):

    # runspec:sched:<id>
    <cron>  env RUNSPEC_AGENT=1 [RUNSPEC_ENV_FILE=…] <venv>/bin/runspec exec \
              --label sched:<id> --trigger schedule:cron --lock <lock> <runnable> -- <argv…> >/dev/null 2>&1

* **``runspec exec``** (core ≥ 0.62) records every fire in the runs index under
  the label ``sched:<id>`` — observed exit code, captured stdout/stderr, and
  failures a runnable can't record itself (docs/design/runspec-exec.md).
* **``--lock``** is the overlap guard — a still-running prior fire makes the new
  one a *recorded* ``skipped`` run (the old bare ``flock -n`` skip was silent).
* **``env``** carries only ``RUNSPEC_AGENT`` + the ``RUNSPEC_ENV_FILE`` path (the
  ``0600`` credential file the runnable reads); the secrets themselves never
  touch the crontab.

Reconcile is **declarative** and marker-scoped: :func:`plan_crontab` strips every
``# runspec:sched:*`` block *this venv* wrote (its job line runs this venv's
``bin/``; another venv's blocks in the same account are left alone) and appends
the current set, leaving the operator's own crontab lines untouched. A tiny machine-local state
file records which accounts we manage, so an account that loses all its schedules
is cleaned up on the next :func:`sync` rather than leaking stale lines.

Only **cron-timed** schedules are installable here: cron cannot express arbitrary
``every N`` intervals (that is the deferred systemd-timer backend), so ``every``
schedules are reported skipped, along with disabled or not-installed ones.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runspec.become import build_become_argv

from .config import ScheduleEntry, load_config, load_schedules
from .discovery import Target, check_invocation, resolve_target
from .invocation import plan_invocation
from .runner import scripts_dir

#: Comment that tags every crontab line this tool owns (so reconcile touches only
#: ours). One safe whitespace-free token per id, mirroring the SSH-key / denylist
#: marker discipline.
MARKER_PREFIX = "# runspec:sched:"

#: Label ``runspec exec`` records on every fire of schedule ``<id>`` — the join key
#: between the config, the crontab and the runs index (see :mod:`status`).
LABEL_PREFIX = "sched:"

#: Where per-schedule lock files (``runspec exec --lock``) live under the run_as
#: account's home.
_LOCK_SUBDIR = ".cache/runspec-scheduler"

#: Sidecar next to the config listing the accounts we've written crontab lines to,
#: so a fully-removed account is reconciled (its lines stripped) on the next sync.
#: Machine-local — never synced.
_STATE_NAME = "runspec_scheduler_cron_state.json"

#: Folder in the venv holding a copy of each crontab as it was before we rewrote
#: it (owner-only files, newest :data:`_BACKUPS_KEPT` per account).
_BACKUP_DIRNAME = "runspec_scheduler_backups"
_BACKUPS_KEPT = 10


@dataclass
class SyncResult:
    """Outcome of a :func:`sync` — what was installed, skipped, or errored."""

    installed: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    dry_run: bool = False
    backend: str = "cron"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "dry_run": self.dry_run,
            "installed": self.installed,
            "skipped": self.skipped,
            "errors": self.errors,
            "users": sorted(self.users),
            "backend": self.backend,
        }


# ── pure rendering ────────────────────────────────────────────────────────────


def _home_of(user: str) -> str:
    """The home directory of ``user`` (empty ``user`` → the invoking user).

    Used only to build an absolute, shell-quotable lock path — resolving it here
    avoids relying on cron's ``$HOME`` (which we would not be able to quote)."""
    try:
        return os.path.expanduser(f"~{user}" if user else "~")
    except (KeyError, RuntimeError):  # pragma: no cover - exotic passwd setups
        return "/tmp"


#: ``runspec exec --trigger`` values: which backend fired a schedule (or a
#: ``run-now``). All start with ``schedule`` so a reader that only asks "was it
#: scheduled?" can match the prefix; runs fired before 0.7.0 say ``schedule``.
TRIGGER_CRON = "schedule:cron"
TRIGGER_DAEMON = "schedule:daemon"
TRIGGER_RUN_NOW = "schedule:run-now"

#: The venv's schedule-only env file: credentials and settings that scheduled
#: fires get and interactive runs in the venv don't. Every fire passes it to
#: ``runspec exec --env-file`` (a missing file adds nothing), stacked under the
#: fire's own environment and over the venv's ``.runspec_env``.
SCHEDULES_ENV = "runspec_schedules.env"


def schedules_env_path(runspec_binary: str) -> str:
    """``{venv}/runspec_schedules.env`` for the venv whose ``bin/runspec`` this is."""
    return str(Path(runspec_binary).parent.parent / SCHEDULES_ENV)


def lock_path(home: str, sched_id: str) -> str:
    """The ``runspec exec --lock`` file for one schedule, under ``home``."""
    return f"{home}/{_LOCK_SUBDIR}/{sched_id}.lock"


def exec_invocation(
    runspec_binary: str,
    runnable: str,
    args: dict[str, Any],
    env_file: str,
    *,
    home: str,
    sched_id: str,
    command: tuple[str, ...] | list[str] = (),
    spec: dict[str, Any] | None = None,
    timeout: int | None = None,
    trigger: str = TRIGGER_CRON,
) -> tuple[dict[str, str], list[str]]:
    """The environment and argv of one schedule's ``runspec exec`` fire.

    Shared by both backends, so a cron fire and a daemon fire run the same
    command: ``runspec exec --label sched:<id> --trigger <trigger> --lock <lock>
    --env-file <venv>/runspec_schedules.env [--timeout N] <runnable> -- <globals>
    <command path> <command args>``. The
    environment carries only ``RUNSPEC_AGENT``, the ``RUNSPEC_ENV_FILE`` path
    and any ``RUNSPEC_<R>_ARG_<FLAG>=0`` a default-true flag needs — never a
    secret. ``timeout`` (seconds) adds ``--timeout`` when positive."""
    plan = plan_invocation(runnable, spec or {}, command, args)
    env = {"RUNSPEC_AGENT": "1"}
    if env_file:
        env["RUNSPEC_ENV_FILE"] = env_file
    env.update(plan.env)  # e.g. RUNSPEC_<R>_ARG_<FLAG>=0 — a default-true flag switched off (argv can't)
    argv = [
        str(runspec_binary),
        "exec",
        "--label",
        f"{LABEL_PREFIX}{sched_id}",
        "--trigger",
        trigger,
        "--lock",
        lock_path(home, sched_id),
        "--env-file",
        schedules_env_path(runspec_binary),
    ]
    if timeout:
        argv += ["--timeout", str(int(timeout))]
    # globals, then the subcommand path, then the command's own args
    argv += [runnable, "--", *plan.argv]
    return env, argv


def render_command(
    runspec_binary: str,
    runnable: str,
    args: dict[str, Any],
    env_file: str,
    *,
    home: str,
    sched_id: str,
    command: tuple[str, ...] | list[str] = (),
    spec: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> str:
    """The shell command a crontab line runs for one schedule.

    Runs the runnable through core's ``runspec exec`` wrapper
    (docs/design/runspec-exec.md), labelled ``sched:<id>``, so every fire is
    recorded in the runs index with its observed exit code and captured output —
    including runnables without ``[config.logging]``, failures before logging
    starts, and overlap skips (``--lock``: a still-running previous fire makes
    this one a recorded ``skipped`` run). ``exec`` resolves the runnable's binary
    and ``runspec.toml`` itself. ``env(1)`` carries only ``RUNSPEC_AGENT`` and the
    ``RUNSPEC_ENV_FILE`` path — never a secret. No ``sudo``: the line lives in
    ``run_as``'s crontab, so cron already runs it as that account (and ``exec``
    doesn't escalate to the account it's already running as). The trailing
    redirect only stops cron mailing the tee'd output. Every interpolated value
    is ``shlex``-quoted. A cron line carries ``--timeout`` only when the schedule
    sets its own ``timeout`` (the 6h default is the daemon's)."""
    env, argv = exec_invocation(runspec_binary, runnable, args, env_file, home=home, sched_id=sched_id, command=command, spec=spec, timeout=timeout)
    agent = [f"RUNSPEC_AGENT={env.pop('RUNSPEC_AGENT')}"]
    env_file_tok = [f"RUNSPEC_ENV_FILE={env.pop('RUNSPEC_ENV_FILE')}"] if "RUNSPEC_ENV_FILE" in env else []
    env_tokens = ["env", *agent, *env_file_tok, *(f"{k}={v}" for k, v in sorted(env.items()))]
    return f"{shlex.join([*env_tokens, *argv])} >/dev/null 2>&1"


def render_block(sched_id: str, cron_expr: str, command: str) -> list[str]:
    """The two crontab lines for one schedule: the id marker, then the job."""
    return [f"{MARKER_PREFIX}{sched_id}", f"{cron_expr}  {command}"]


def is_our_job(line: str, sched_id: str) -> bool:
    """Whether ``line`` is the job line we write for schedule ``sched_id``.

    Every line we have written (the ``runspec exec`` form and the pre-0.3
    ``flock -n`` form) carries the schedule's lock path, and the current form its
    ``--label sched:<id>``; a comment, a blank line or another marker never is."""
    text = line.strip()
    if not text or text.startswith("#"):
        return False
    return f"/{_LOCK_SUBDIR}/{sched_id}.lock" in line or f"--label {LABEL_PREFIX}{sched_id} " in line


def job_in_venv(line: str, bin_dir: str) -> bool:
    """Whether a job line runs from the venv whose scripts folder is ``bin_dir``.

    Every line we write starts this venv's own ``bin/runspec`` (the pre-0.3 form
    its ``bin/<runnable>``), so the folder in the line says which venv wrote it."""
    return f"{bin_dir.rstrip('/')}/" in line


def this_bin_dir() -> str:
    """This venv's scripts folder, as written into its crontab lines."""
    return str(scripts_dir())


def plan_crontab(existing_lines: list[str], blocks: list[list[str]], *, bin_dir: str | None = None) -> list[str]:
    """Reconcile a user's crontab: drop every block we previously wrote, keep the
    operator's own lines verbatim, then append the current ``blocks``.

    Our block is a ``# runspec:sched:<id>`` marker line plus the single line that
    follows it (the job), so both are dropped together — but the line under a
    marker goes only when it is our job (:func:`is_our_job`); a marker left
    behind on its own never takes the operator's next line with it.

    With ``bin_dir`` (every real caller passes this venv's), a block whose job
    runs from another venv's ``bin/`` is kept as it is: several venvs owned by
    one account share its crontab, and a sync or ``use daemon`` in one of them
    must not remove the others' schedules. Idempotent: planning twice over its
    own output yields the same result."""
    kept: list[str] = []
    i = 0
    n = len(existing_lines)
    while i < n:
        line = existing_lines[i]
        if not line.startswith(MARKER_PREFIX):
            kept.append(line)
            i += 1
            continue
        sid = line[len(MARKER_PREFIX) :].strip()
        job = existing_lines[i + 1] if i + 1 < n else None
        if job is not None and is_our_job(job, sid):
            if bin_dir is not None and not job_in_venv(job, bin_dir):
                kept.extend([line, job])  # another venv's schedule — leave it
            i += 2
            continue
        i += 1  # a marker on its own: drop it, keep whatever follows
    while kept and kept[-1].strip() == "":
        kept.pop()  # trim trailing blanks so appended blocks sit cleanly
    out = list(kept)
    for block in blocks:
        out.extend(block)
    return out


def marked_ids(lines: list[str]) -> list[str]:
    """The schedule ids of every ``# runspec:sched:`` marker in ``lines``."""
    return [ln[len(MARKER_PREFIX) :].strip() for ln in lines if ln.startswith(MARKER_PREFIX)]


# ── crontab I/O (per run_as account, escalated at write time) ───────────────────


def _crontab_argv(sub: str, run_as: str, become_method: str, become_flags: str | None) -> list[str]:
    cmd = ["crontab", sub]
    if run_as:
        return build_become_argv(cmd, run_as=run_as, become_method=become_method, become_flags=become_flags)
    return cmd


def _read_crontab(run_as: str, become_method: str, become_flags: str | None) -> list[str]:
    """The target account's current crontab lines ([] when it has none)."""
    try:
        proc = subprocess.run(_crontab_argv("-l", run_as, become_method, become_flags), capture_output=True, text=True)
    except FileNotFoundError as exc:  # no crontab / sudo binary — an error result, not a traceback
        raise RuntimeError(f"{exc.filename} is not installed on this host") from exc
    if proc.returncode != 0:
        if "no crontab for" in (proc.stderr or "").lower():
            return []
        raise RuntimeError((proc.stderr or "").strip() or f"crontab -l failed (exit {proc.returncode})")
    return proc.stdout.splitlines()


def _write_crontab(lines: list[str], run_as: str, become_method: str, become_flags: str | None) -> None:
    """Install ``lines`` as the target account's whole crontab via ``crontab -``."""
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    try:
        proc = subprocess.run(
            _crontab_argv("-", run_as, become_method, become_flags),
            input=content,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{exc.filename} is not installed on this host") from exc
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or f"crontab - failed (exit {proc.returncode})")


def _backup_dir() -> Path:
    return Path(sys.prefix) / _BACKUP_DIRNAME


def backup_crontab(lines: list[str], run_as: str) -> Path | None:
    """Save ``lines`` (an account's crontab before we rewrite it) to an owner-only
    file in the venv, keeping the newest :data:`_BACKUPS_KEPT` for that account.
    Best-effort: returns ``None`` when nothing was saved (an empty crontab, or the
    folder can't be written) — a missing backup never blocks the write."""
    if not lines:
        return None
    folder = _backup_dir()
    who = (run_as or "self").replace("/", "_")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / f"crontab-{who}-{stamp}-{os.getpid()}.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        old = sorted(folder.glob(f"crontab-{who}-*.txt"))
        for stale in old[:-_BACKUPS_KEPT]:
            stale.unlink(missing_ok=True)
        return path
    except OSError:
        return None


def replace_crontab(existing: list[str], lines: list[str], run_as: str, become_method: str, become_flags: str | None) -> bool:
    """Install ``lines`` over ``existing`` (what was just read) — skipped when they
    are the same, and with ``existing`` backed up first. Returns whether it wrote."""
    if lines == existing:
        return False
    backup_crontab(existing, run_as)
    _write_crontab(lines, run_as, become_method, become_flags)
    return True


# ── managed-users state (declarative cross-account cleanup) ─────────────────────


def _state_path(config_path: Path) -> Path:
    return config_path.with_name(_STATE_NAME)


def read_managed_users(config_path: Path) -> list[str]:
    """Accounts we've previously written crontab lines to (for orphan cleanup)."""
    try:
        data = json.loads(_state_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    users = data.get("users") if isinstance(data, dict) else None
    return [str(u) for u in users] if isinstance(users, list) else []


def write_managed_users(config_path: Path, users: list[str]) -> None:
    """Record the accounts we currently manage. Best-effort — a write failure only
    risks a stale line lingering, never a wrong run."""
    tmp = _state_path(config_path).with_name(f".{_STATE_NAME}.tmp")
    try:
        tmp.write_text(json.dumps({"users": sorted(set(users))}), encoding="utf-8")
        os.replace(tmp, _state_path(config_path))
    except OSError:
        pass


# ── reconcile ───────────────────────────────────────────────────────────────────


def _installable(entry: ScheduleEntry, backend: str = "cron") -> tuple[bool, str]:
    """Whether ``entry`` can fire on ``backend``, else (False, reason)."""
    if not entry.enabled:
        return False, "disabled"
    if backend == "daemon":
        return True, ""
    if entry.when.every:
        return False, "interval schedules ('every') need the daemon backend (runspec-scheduler use daemon); on cron use a cron expression"
    if not entry.when.cron:
        return False, "no cron expression"
    return True, ""


def resolve_entry(entry: ScheduleEntry) -> tuple[Target | None, str]:
    """The installed target for ``entry`` if its invocation is valid, else
    ``(None, reason)`` — the runnable isn't installed, or its command path/args
    don't match the runnable's spec (e.g. a hand-edited config naming a verb that
    no longer exists). Shared by ``sync`` (skips it) and ``status`` (reports it)."""
    target = resolve_target(entry.runnable)
    if target is None:
        return None, f"runnable '{entry.runnable}' not installed in this venv"
    problem = check_invocation(target.spec, entry.command, dict(entry.args))
    if problem:
        return None, problem
    return target, ""


def _group_key(entry: ScheduleEntry, target: Target) -> tuple[str, str, str]:
    """(run_as, become_method, become_flags) — the account+escalation a line's
    crontab is written under. Empty run_as = the invoking user's own crontab."""
    return (
        entry.run_as or target.run_as,
        entry.become_method or target.become_method,
        target.become_flags or "",
    )


def sync(config_path: Path, *, dry_run: bool = False) -> SyncResult:
    """Reconcile the OS crontabs to match the config's enabled cron schedules.

    For each account, strips our previously-written blocks and installs the current
    ones; an account that no longer has any schedules is cleaned up via the managed-
    users state. Skips (with a reason) any schedule that is disabled, interval-timed,
    or whose runnable isn't installed in this venv. Best-effort per account — one
    account's crontab error doesn't abort the others."""
    result = SyncResult(dry_run=dry_run)
    settings, entries = load_config(config_path)
    if settings.backend != "cron":
        # The daemon fires these schedules, so the crontab should hold none of this
        # venv's lines (one left behind would fire every schedule twice): on a
        # daemon venv, sync strips them.
        cleaned = remove_all(config_path, dry_run=dry_run)
        cleaned.backend = settings.backend
        return cleaned

    # Resolve + filter, grouping the installable ones by target account.
    groups: dict[tuple[str, str, str], list[tuple[ScheduleEntry, Target]]] = {}
    for entry in entries:
        ok, reason = _installable(entry)
        if not ok:
            result.skipped.append({"id": entry.id, "reason": reason})
            continue
        target, reason = resolve_entry(entry)
        if target is None:
            result.skipped.append({"id": entry.id, "reason": reason})
            continue
        groups.setdefault(_group_key(entry, target), []).append((entry, target))

    # Reconcile the union of currently-targeted accounts and previously-managed
    # ones, so an account emptied of schedules gets its stale lines stripped.
    current_users = {key[0] for key in groups}
    previous_users = set(read_managed_users(config_path))
    escalation = {key[0]: (key[1], key[2]) for key in groups}  # user → (become_method, become_flags)

    for user in sorted(current_users | previous_users):
        members = [(e, t) for key, gs in groups.items() if key[0] == user for (e, t) in gs]
        blocks: list[list[str]] = []
        home = _home_of(user)
        for entry, _target in members:
            command = render_command(
                str(scripts_dir() / "runspec"),
                entry.runnable,
                dict(entry.args),
                entry.env_file,
                home=home,
                sched_id=entry.id,
                command=entry.command,
                spec=_target.spec,
                timeout=entry.timeout or None,
            )
            blocks.append(render_block(entry.id, entry.when.cron, command))
            result.installed.append({"id": entry.id, "runnable": entry.runnable, "user": user or "(self)"})
        become_method, become_flags = escalation.get(user, ("sudo", ""))
        try:
            existing = _read_crontab(user, become_method, become_flags or None)
            new_lines = plan_crontab(existing, blocks, bin_dir=this_bin_dir())
            if not dry_run:
                replace_crontab(existing, new_lines, user, become_method, become_flags or None)
            result.users.append(user or "(self)")
        except RuntimeError as exc:
            # Roll back the optimistic "installed" entries for this failed account.
            result.installed = [r for r in result.installed if r["user"] != (user or "(self)")]
            result.errors.append({"user": user or "(self)", "error": str(exc)})

    if not dry_run and not result.errors:
        write_managed_users(config_path, sorted(current_users))
    return result


def remove_all(config_path: Path, *, dry_run: bool = False) -> SyncResult:
    """Strip every marked ``# runspec:sched:*`` block from every account this venv
    manages (and every account its schedules name), leaving other lines alone.
    Used when a venv moves to the daemon backend so no schedule fires twice."""
    result = SyncResult(dry_run=dry_run)
    try:
        entries = load_schedules(config_path)
    except Exception:  # noqa: BLE001 — a broken config still lets us clean up
        entries = []
    accounts: dict[str, str] = {}
    for e in entries:
        target = None
        try:
            target = resolve_target(e.runnable)
        except Exception:  # noqa: BLE001
            target = None
        user = e.run_as or (target.run_as if target else "")
        accounts.setdefault(user, e.become_method or (target.become_method if target else "sudo"))
    for u in read_managed_users(config_path):
        accounts.setdefault(u, "sudo")
    accounts.setdefault("", "sudo")
    remaining: list[str] = []
    for user, method in sorted(accounts.items()):
        label = user or "(self)"
        try:
            existing = _read_crontab(user, method, None)
            new_lines = plan_crontab(existing, [], bin_dir=this_bin_dir())
            if new_lines != existing:
                removed = list((Counter(marked_ids(existing)) - Counter(marked_ids(new_lines))).elements())
                result.installed.extend({"id": sid, "user": label, "removed": "true"} for sid in removed)
                if not dry_run:
                    replace_crontab(existing, new_lines, user, method, None)
            result.users.append(label)
        except RuntimeError as exc:
            result.errors.append({"user": label, "error": str(exc)})
            remaining.append(user)
    if not dry_run:
        write_managed_users(config_path, remaining)
    return result
