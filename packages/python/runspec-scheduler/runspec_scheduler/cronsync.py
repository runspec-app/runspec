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
              --label sched:<id> --trigger schedule --lock <lock> <runnable> -- <argv…> >/dev/null 2>&1

* **``runspec exec``** (core ≥ 0.62) records every fire in the runs index under
  the label ``sched:<id>`` — observed exit code, captured stdout/stderr, and
  failures a runnable can't record itself (docs/design/runspec-exec.md).
* **``--lock``** is the overlap guard — a still-running prior fire makes the new
  one a *recorded* ``skipped`` run (the old bare ``flock -n`` skip was silent).
* **``env``** carries only ``RUNSPEC_AGENT`` + the ``RUNSPEC_ENV_FILE`` path (the
  ``0600`` credential file the runnable reads); the secrets themselves never
  touch the crontab.

Reconcile is **declarative** and marker-scoped: :func:`plan_crontab` strips every
``# runspec:sched:*`` block we previously wrote and appends the current set,
leaving the operator's own crontab lines untouched. A tiny machine-local state
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runspec.become import build_become_argv

from .config import ScheduleEntry, load_schedules
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


@dataclass
class SyncResult:
    """Outcome of a :func:`sync` — what was installed, skipped, or errored."""

    installed: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "dry_run": self.dry_run,
            "installed": self.installed,
            "skipped": self.skipped,
            "errors": self.errors,
            "users": sorted(self.users),
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
    is ``shlex``-quoted."""
    lock = f"{home}/{_LOCK_SUBDIR}/{sched_id}.lock"
    plan = plan_invocation(runnable, spec or {}, command, args)
    env_tokens = ["env", "RUNSPEC_AGENT=1"]
    if env_file:
        env_tokens.append(f"RUNSPEC_ENV_FILE={env_file}")
    # e.g. RUNSPEC_<R>_ARG_<FLAG>=0 — a default-true flag switched off (argv can't)
    env_tokens += [f"{k}={v}" for k, v in sorted(plan.env.items())]
    run_tokens = [
        *env_tokens,
        str(runspec_binary),
        "exec",
        "--label",
        f"{LABEL_PREFIX}{sched_id}",
        "--trigger",
        "schedule",
        "--lock",
        lock,
        runnable,
        "--",
        # globals, then the subcommand path, then the command's own args
        *plan.argv,
    ]
    return f"{shlex.join(run_tokens)} >/dev/null 2>&1"


def render_block(sched_id: str, cron_expr: str, command: str) -> list[str]:
    """The two crontab lines for one schedule: the id marker, then the job."""
    return [f"{MARKER_PREFIX}{sched_id}", f"{cron_expr}  {command}"]


def plan_crontab(existing_lines: list[str], blocks: list[list[str]]) -> list[str]:
    """Reconcile a user's crontab: drop every block we previously wrote, keep the
    operator's own lines verbatim, then append the current ``blocks``.

    Our block is a ``# runspec:sched:<id>`` marker line plus the single line that
    follows it (the job), so both are dropped together. Idempotent: planning twice
    over its own output yields the same result."""
    kept: list[str] = []
    skip_next = False
    for line in existing_lines:
        if skip_next:
            skip_next = False
            continue
        if line.startswith(MARKER_PREFIX):
            skip_next = True  # drop the marker and its job line
            continue
        kept.append(line)
    while kept and kept[-1].strip() == "":
        kept.pop()  # trim trailing blanks so appended blocks sit cleanly
    out = list(kept)
    for block in blocks:
        out.extend(block)
    return out


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


def _installable(entry: ScheduleEntry) -> tuple[bool, str]:
    """Whether ``entry`` can be installed on the cron backend, else (False, reason)."""
    if not entry.enabled:
        return False, "disabled"
    if entry.when.every:
        return False, "interval schedules ('every') are not supported on the cron backend; use a cron expression"
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
    entries = load_schedules(config_path)

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
            )
            blocks.append(render_block(entry.id, entry.when.cron, command))
            result.installed.append({"id": entry.id, "runnable": entry.runnable, "user": user or "(self)"})
        become_method, become_flags = escalation.get(user, ("sudo", ""))
        try:
            existing = _read_crontab(user, become_method, become_flags or None)
            new_lines = plan_crontab(existing, blocks)
            if not dry_run:
                _write_crontab(new_lines, user, become_method, become_flags or None)
            result.users.append(user or "(self)")
        except RuntimeError as exc:
            # Roll back the optimistic "installed" entries for this failed account.
            result.installed = [r for r in result.installed if r["user"] != (user or "(self)")]
            result.errors.append({"user": user or "(self)", "error": str(exc)})

    if not dry_run and not result.errors:
        write_managed_users(config_path, sorted(current_users))
    return result
