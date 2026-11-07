"""keeper.py — what keeps this venv's daemon running (``install-service``).

The daemon (``runspec-scheduler run``) is one process per venv, run as the venv
owner. Something has to start it at boot and restart it when it dies; that is
the *keeper*. Four are supported, picked with ``--manager``:

``systemd-user``
    A ``systemctl --user`` unit in the owner's ``~/.config/systemd/user``. No
    root, but the account needs lingering (``loginctl enable-linger``) or the
    daemon stops at logout, and systemd newer than 219 (CentOS 7's ``--user``
    manager is unreliable).
``supervisor``
    ``supervisord`` installed in the venv (``pip install
    'runspec-scheduler[supervisor]'``), its config under ``{venv}/supervisor``,
    started at boot and checked every 5 minutes from the owner's crontab. No
    root. ``--system`` prints a program block for a system supervisord instead.
``cron``
    Two crontab lines running ``runspec-scheduler run --detach`` at boot and
    every 5 minutes; the venv lock makes every start after the first a no-op.
    No root, no extra package; a crashed daemon is back within 5 minutes.
``systemd``
    A system unit with ``User=<owner>``. Needs root once, so it is only ever
    printed, never written to ``/etc``.

``auto`` takes systemd-user when lingering is on, systemd is newer than 219 and
the user bus answers; else supervisor when supervisord is in the venv; else
cron. The result says why.

Nothing changes without ``--write``: the verb prints the plan (files, crontab
lines, commands, and the steps root has to run) and ``--write`` carries it out.
``--remove`` plans the reverse — every keeper this venv has, then stops the
daemon if it is still running — so deprovisioning a venv can't leave a keeper
restarting a daemon from a deleted folder.

Crontab lines are tagged ``# runspec:scheduler-keeper:<venv path>`` (one tag
per line), apart from the ``# runspec:sched:`` lines of the cron backend, so the
two never touch each other's lines.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import config_path as default_config_path
from .runner import scripts_dir
from .service import _is_bus_error, _login_shell_hint, _systemctl_env, render_system_unit, render_unit, system_unit_path, unit_dir, unit_name

MANAGERS = ("auto", "systemd", "systemd-user", "supervisor", "cron")
KEEPER_MARKER = "# runspec:scheduler-keeper:"
#: systemd at or below this version has an unreliable ``--user`` manager.
MIN_USER_SYSTEMD = 220
WATCHDOG = "*/5 * * * *"


# ── facts about this host ───────────────────────────────────────────────────────


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):  # pragma: no cover - exotic environments
        return str(uid)


@dataclass
class Facts:
    """What ``auto`` looks at. Built by :func:`gather`; tests build their own."""

    user: str
    venv_owner: str
    linger: bool | None = None  # None: no systemd here
    systemd_version: int | None = None
    user_bus: bool = False
    supervisord: Path | None = None
    crontab: bool = True

    @property
    def root(self) -> bool:
        return self.user == "root"


def _systemd_version() -> int | None:
    try:
        out = subprocess.run(["systemctl", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"systemd (\d+)", out.strip())
    return int(m.group(1)) if m else None


def _linger(user: str) -> bool | None:
    base = Path("/var/lib/systemd/linger")
    if not base.parent.is_dir():
        return None
    return (base / user).exists()


def _user_bus_ok() -> bool:
    try:
        proc = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, env=_systemctl_env(), timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    text = (proc.stdout + proc.stderr).strip()
    return bool(proc.stdout.strip()) and not _is_bus_error(text)


def supervisord_path() -> Path | None:
    exe = scripts_dir() / "supervisord"
    return exe if exe.exists() else None


def gather() -> Facts:
    user = _user_name(os.geteuid())
    try:
        owner = _user_name(Path(sys.prefix).stat().st_uid)
    except OSError:  # pragma: no cover
        owner = user
    version = _systemd_version()
    return Facts(
        user=user,
        venv_owner=owner,
        linger=_linger(user) if version else None,
        systemd_version=version,
        user_bus=_user_bus_ok() if version else False,
        supervisord=supervisord_path(),
        crontab=shutil.which("crontab") is not None,
    )


def choose(facts: Facts) -> tuple[str, str]:
    """``auto``'s pick and the reason, in one sentence."""
    if facts.systemd_version is None:
        why = "no systemd on this host"
    elif facts.systemd_version < MIN_USER_SYSTEMD:
        why = f"systemd {facts.systemd_version} is too old for a reliable --user manager"
    elif not facts.linger:
        why = f"lingering is off for {facts.user} (root can turn it on: sudo loginctl enable-linger {facts.user})"
    elif not facts.user_bus:
        why = "the systemd --user bus doesn't answer (run from a login shell: sudo -iu <owner>)"
    else:
        return "systemd-user", f"lingering is on for {facts.user} and systemd {facts.systemd_version} has a working --user manager"
    if facts.supervisord is not None:
        return "supervisor", f"{why}; supervisord is installed in this venv"
    return "cron", f"{why}, and supervisord isn't installed in this venv (pip install 'runspec-scheduler[supervisor]' to use it); a crontab watchdog restarts the daemon within 5 minutes"


# ── the plan ────────────────────────────────────────────────────────────────────


@dataclass
class Action:
    """One step. ``kind`` is write | unlink | rmdir | crontab | run | stop."""

    kind: str
    desc: str
    path: Path | None = None
    content: str | None = None
    argv: list[str] | None = None
    lines: list[str] | None = None  # crontab: this venv's keeper lines after the step
    systemd_env: bool = False
    pid: int | None = None


@dataclass
class Plan:
    manager: str
    action: str  # install | remove
    reason: str = ""
    actions: list[Action] = field(default_factory=list)
    root_steps: list[str] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)  # shown, incl. ones root writes
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    hint: str | None = None

    def to_dict(self, written: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.error is None,
            "action": self.action,
            "manager": self.manager,
            "reason": self.reason,
            "written": written,
            "steps": [a.desc for a in self.actions],
            "files": self.files,
            "crontab": next((a.lines for a in self.actions if a.kind == "crontab"), None),
            "root_steps": self.root_steps,
            "warnings": self.warnings,
        }
        if self.error:
            out["error"] = self.error
        if self.hint:
            out["hint"] = self.hint
        elif not written and self.actions and self.error is None:
            out["hint"] = "nothing was changed — re-run with --write to apply"
        return out


def venv_root() -> Path:
    return Path(sys.prefix)


def _exe() -> Path:
    return scripts_dir() / "runspec-scheduler"


def run_argv(config: str | None = None, *, detach: bool = False) -> list[str]:
    """How a keeper starts the daemon (``--config`` only when one was given)."""
    argv = [str(_exe()), "run"]
    if detach:
        argv.append("--detach")
    if config:
        argv += ["--config", str(Path(config).resolve())]
    return argv


def marker() -> str:
    return f"{KEEPER_MARKER}{venv_root()}"


def _tagged(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        out += [marker(), line]
    return out


def strip_keeper_lines(existing: list[str], tag: str | None = None) -> list[str]:
    """``existing`` without this venv's keeper lines (each tag + the line after it).

    The line after a tag goes only when it is a keeper job for this venv (a cron
    line naming the venv folder), so a tag left on its own never takes the
    operator's next line with it."""
    tag = tag or marker()
    root = tag[len(KEEPER_MARKER) :] if tag.startswith(KEEPER_MARKER) else str(venv_root())
    out: list[str] = []
    skip = False
    for line in existing:
        if skip:
            skip = False
            text = line.strip()
            if text and not text.startswith("#") and root in line:
                continue
        if line.strip() == tag:
            skip = True
            continue
        out.append(line)
    return out


def keeper_lines(existing: list[str], tag: str | None = None) -> list[str]:
    """This venv's keeper job lines in ``existing`` (without the tags)."""
    tag = tag or marker()
    return [existing[i + 1] for i, line in enumerate(existing[:-1]) if line.strip() == tag]


def _read_own_crontab() -> list[str]:
    from .cronsync import _read_crontab

    return _read_crontab("", "sudo", None)


def _write_own_crontab(lines: list[str], existing: list[str]) -> None:
    """Replace the owner's crontab (``existing``, just read) with ``lines`` —
    nothing when unchanged, and the old crontab backed up first."""
    from .cronsync import backup_crontab, replace_crontab

    if lines:
        replace_crontab(existing, lines, "", "sudo", None)
    elif existing:  # an empty `crontab -` leaves an empty file; same effect, but -r is tidier
        backup_crontab(existing, "")
        subprocess.run(["crontab", "-r"], capture_output=True)


# supervisor layout under {venv}/supervisor


def supervisor_dir() -> Path:
    return venv_root() / "supervisor"


def supervisor_conf() -> Path:
    return supervisor_dir() / "supervisord.conf"


def supervisor_program_conf() -> Path:
    return supervisor_dir() / "conf.d" / "runspec-scheduler.conf"


def render_supervisord_conf() -> str:
    d = supervisor_dir()
    logs = venv_root() / "logs"
    return (
        "; written by runspec-scheduler install-service --manager supervisor\n"
        "[unix_http_server]\n"
        f"file={d / 'supervisor.sock'}\n"
        "chmod=0700\n\n"
        "[supervisord]\n"
        f"logfile={logs / 'supervisord.log'}\n"
        f"pidfile={d / 'supervisord.pid'}\n"
        f"childlogdir={logs}\n\n"
        "[rpcinterface:supervisor]\n"
        "supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface\n\n"
        "[supervisorctl]\n"
        f"serverurl=unix://{d / 'supervisor.sock'}\n\n"
        "[include]\n"
        "files = conf.d/*.conf\n"
    )


def render_supervisor_program(argv: list[str], *, user: str | None = None, name: str = "runspec-scheduler") -> str:
    logs = venv_root() / "logs"
    text = (
        f"[program:{name}]\n"
        f"command={shlex.join(argv)}\n"
        "autostart=true\n"
        "autorestart=true\n"
        "startsecs=5\n"
        "stopsignal=TERM\n"
        "stopwaitsecs=30\n"
        "redirect_stderr=true\n"
        f"stdout_logfile={logs / 'scheduler.stdout.log'}\n"
    )
    if user:
        text += f"user={user}\n"
    return text


def _supervisord_pid() -> int | None:
    try:
        pid = int((supervisor_dir() / "supervisord.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _crontab_entries(config: str | None) -> dict[str, list[str]]:
    """The keeper job lines each crontab-based keeper uses."""
    sup = [shlex.quote(str(scripts_dir() / "supervisord")), "-c", shlex.quote(str(supervisor_conf()))]
    sup_cmd = " ".join(sup) + " >/dev/null 2>&1"
    det_cmd = shlex.join(run_argv(config, detach=True)) + " >/dev/null 2>&1"
    return {
        "supervisor": [f"@reboot {sup_cmd}", f"{WATCHDOG} {sup_cmd}"],
        "cron": [f"@reboot {det_cmd}", f"{WATCHDOG} {det_cmd}"],
    }


def _owner_check(facts: Facts, manager: str) -> tuple[str, str] | None:
    """Keepers that run as the invoking account must be set up by the venv owner."""
    if manager == "systemd":
        return None
    if facts.user != facts.venv_owner:
        return (
            f"the daemon must run as the venv owner ({facts.venv_owner}), and this keeper runs it as {facts.user}",
            f"run it as the owner: sudo -iu {facts.venv_owner} {_exe()} install-service --manager {manager}",
        )
    return None


def plan_install(manager: str, *, facts: Facts | None = None, config: str | None = None, system: bool = False) -> Plan:
    facts = facts or gather()
    reason = ""
    if manager == "auto":
        manager, reason = choose(facts)
    p = Plan(manager=manager, action="install", reason=reason or "chosen with --manager")
    if not (manager == "supervisor" and system):
        bad = _owner_check(facts, manager)
        if bad:
            p.error, p.hint = bad
            return p
    argv = run_argv(config)
    if facts.venv_owner == "root":
        p.warnings.append("this venv belongs to root, so the daemon and every fire run as root; a venv owned by a service account is safer")

    if manager == "systemd":
        unit = render_system_unit(facts.venv_owner, argv)
        staged = venv_root() / unit_name()
        target = system_unit_path()
        p.actions.append(Action("write", f"save the unit for root at {staged}", path=staged, content=unit))
        p.files.append({"path": str(target), "content": unit, "by": "root"})
        p.root_steps = [f"sudo cp {staged} {target}", "sudo systemctl daemon-reload", f"sudo systemctl enable --now {unit_name()}"]
        p.warnings.append("root has to run root_steps once; this account can't write /etc")
        return p

    if manager == "systemd-user":
        if facts.systemd_version is None:
            p.error, p.hint = "there is no systemd on this host", "use --manager supervisor or --manager cron"
            return p
        path = unit_dir() / unit_name()
        unit = render_unit(argv)
        p.actions += [
            Action("write", f"write {path}", path=path, content=unit),
            Action("run", "systemctl --user daemon-reload", argv=["systemctl", "--user", "daemon-reload"], systemd_env=True),
            Action("run", f"systemctl --user enable --now {unit_name()}", argv=["systemctl", "--user", "enable", "--now", unit_name()], systemd_env=True),
        ]
        p.files.append({"path": str(path), "content": unit, "by": facts.user})
        if not facts.linger:
            p.warnings.append(f"lingering is off for {facts.user}, so the daemon stops at logout; ask root: sudo loginctl enable-linger {facts.user}")
        if facts.systemd_version < MIN_USER_SYSTEMD:
            p.warnings.append(f"systemd {facts.systemd_version} has an unreliable --user manager; --manager supervisor or cron is safer here")
        return p

    if manager == "supervisor":
        if system:
            name = f"runspec-scheduler-{venv_root().name}"
            block = render_supervisor_program(argv, user=facts.venv_owner, name=name)
            p.files.append({"path": f"/etc/supervisord.d/{name}.ini", "content": block, "by": "root"})
            p.root_steps = [f"add the program block to the system supervisord's config (e.g. /etc/supervisord.d/{name}.ini)", "sudo supervisorctl update"]
            p.warnings.append("root has to run root_steps once; nothing is written here")
            return p
        if facts.supervisord is None:
            p.error = "supervisord isn't installed in this venv"
            p.hint = f"install it, then re-run: {scripts_dir() / 'python'} -m pip install 'runspec-scheduler[supervisor]'"
            return p
        if not facts.crontab:
            p.error, p.hint = "crontab isn't installed, and it starts supervisord at boot", "use --manager systemd-user or ask root for --manager systemd"
            return p
        conf, prog = supervisor_conf(), supervisor_program_conf()
        p.actions += [
            Action("write", f"write {conf}", path=conf, content=render_supervisord_conf()),
            Action("write", f"write {prog}", path=prog, content=render_supervisor_program(argv)),
            Action("crontab", "add the @reboot + every-5-minutes supervisord lines to the crontab", lines=_crontab_entries(config)["supervisor"]),
        ]
        if _supervisord_pid():
            sctl = [str(scripts_dir() / "supervisorctl"), "-c", str(conf), "update"]
            p.actions.append(Action("run", "supervisorctl update (supervisord is already running)", argv=sctl))
        else:
            p.actions.append(Action("run", "start supervisord", argv=[str(facts.supervisord), "-c", str(conf)]))
        p.files += [{"path": str(conf), "content": render_supervisord_conf(), "by": facts.user}, {"path": str(prog), "content": render_supervisor_program(argv), "by": facts.user}]
        return p

    if manager == "cron":
        if not facts.crontab:
            p.error, p.hint = "crontab isn't installed on this host", "use --manager systemd-user or supervisor"
            return p
        p.actions += [
            Action("crontab", "add the @reboot + every-5-minutes `run --detach` lines to the crontab", lines=_crontab_entries(config)["cron"]),
            Action("run", "start the daemon now (run --detach)", argv=run_argv(config, detach=True)),
        ]
        return p

    p.error = f"unknown manager {manager!r} (one of: {', '.join(MANAGERS)})"
    return p


# ── what's installed now ────────────────────────────────────────────────────────


def installed(*, facts: Facts | None = None) -> list[dict[str, Any]]:
    """The keepers this venv has, each ``{"manager", "where", ...}``."""
    out: list[dict[str, Any]] = []
    unit = unit_dir() / unit_name()
    if unit.is_file() and str(_exe()) in unit.read_text(encoding="utf-8", errors="replace"):
        out.append({"manager": "systemd-user", "where": str(unit)})
    sys_unit = system_unit_path()
    with contextlib.suppress(OSError):
        if sys_unit.is_file() and str(_exe()) in sys_unit.read_text(encoding="utf-8", errors="replace"):
            out.append({"manager": "systemd", "where": str(sys_unit)})
    try:
        lines = keeper_lines(_read_own_crontab()) if shutil.which("crontab") else []
    except RuntimeError:
        lines = []
    sup_lines = [line for line in lines if "supervisord" in line]
    cron_lines = [line for line in lines if "supervisord" not in line]
    if supervisor_conf().is_file() or sup_lines:
        out.append({"manager": "supervisor", "where": str(supervisor_conf()), "crontab": sup_lines, "supervisord_pid": _supervisord_pid()})
    if cron_lines:
        out.append({"manager": "cron", "where": "crontab", "crontab": cron_lines})
    return out


def _daemon_pid(config: str | None) -> int | None:
    from .status import _pid_is_scheduler, daemon_status

    pid = daemon_status(default_config_path(config)).get("pid")
    if isinstance(pid, int) and pid > 0 and _pid_is_scheduler(pid):
        return pid
    return None


def plan_remove(manager: str = "auto", *, config: str | None = None, facts: Facts | None = None) -> Plan:
    """Undo every keeper this venv has (or just ``manager``), then stop the daemon."""
    p = Plan(manager=manager, action="remove", reason="every keeper this venv has" if manager == "auto" else "chosen with --manager")
    found = [k for k in installed(facts=facts) if manager == "auto" or k["manager"] == manager]
    for k in found:
        m = k["manager"]
        if m == "systemd-user":
            path = Path(k["where"])
            p.actions += [
                Action("run", f"systemctl --user disable --now {unit_name()}", argv=["systemctl", "--user", "disable", "--now", unit_name()], systemd_env=True),
                Action("unlink", f"delete {path}", path=path),
                Action("run", "systemctl --user daemon-reload", argv=["systemctl", "--user", "daemon-reload"], systemd_env=True),
            ]
        elif m == "systemd":
            p.root_steps += [f"sudo systemctl disable --now {unit_name()}", f"sudo rm {k['where']}", "sudo systemctl daemon-reload"]
            p.warnings.append("the system unit belongs to root; root has to run root_steps")
        elif m == "supervisor":
            if k.get("supervisord_pid"):
                p.actions.append(Action("run", "supervisorctl shutdown (stops the daemon and supervisord)", argv=[str(scripts_dir() / "supervisorctl"), "-c", str(supervisor_conf()), "shutdown"]))
            p.actions += [
                Action("unlink", f"delete {supervisor_program_conf()}", path=supervisor_program_conf()),
                Action("unlink", f"delete {supervisor_conf()}", path=supervisor_conf()),
                Action("rmdir", f"delete {supervisor_dir()} if nothing else is in it", path=supervisor_dir()),
            ]
    if any(k["manager"] in ("supervisor", "cron") for k in found):
        if manager == "auto":
            keep: list[str] = []
        else:  # removing one crontab keeper leaves the other's lines
            other = "cron" if manager == "supervisor" else "supervisor"
            keep = next((k["crontab"] for k in installed(facts=facts) if k["manager"] == other), [])
        p.actions.insert(0, Action("crontab", "remove this venv's keeper lines from the crontab", lines=keep))
    pid = _daemon_pid(config)
    if pid and not any(k["manager"] == "systemd" for k in found):
        p.actions.append(Action("stop", f"stop the daemon (pid {pid})", pid=pid))
    if not p.actions and not p.root_steps:
        p.hint = "no keeper is installed for this venv and the daemon isn't running"
    return p


def plan_restart(*, config: str | None = None, facts: Facts | None = None) -> Plan:
    """Restart (or start) this venv's daemon through the keeper it has, so new
    code — after ``pip install -U runspec-scheduler`` — is loaded. Fires already
    running carry on in their own sessions and are still recorded; only the
    daemon process is replaced. Config changes never need this (the daemon
    reloads them by itself)."""
    facts = facts or gather()
    found = installed(facts=facts)
    managers = [k["manager"] for k in found]
    pid = _daemon_pid(config)
    p = Plan(manager=managers[0] if managers else "none", action="restart", reason="the keeper this venv has" if managers else "no keeper is installed")
    if "systemd" in managers:
        p.manager = "systemd"
        p.root_steps = [f"sudo systemctl restart {unit_name()}"]
        p.error = "the daemon runs from a system unit, which only root can restart"
        p.hint = f"ask root: sudo systemctl restart {unit_name()}"
        return p
    keeper_kind = next((m for m in ("systemd-user", "supervisor", "cron") if m in managers), "none")
    bad = _owner_check(facts, keeper_kind if keeper_kind != "none" else "cron")
    if bad:
        p.error, p.hint = bad
        return p
    if keeper_kind == "systemd-user":
        p.actions.append(Action("run", f"systemctl --user restart {unit_name()}", argv=["systemctl", "--user", "restart", unit_name()], systemd_env=True))
        return p
    if keeper_kind == "supervisor":
        conf = supervisor_conf()
        if _supervisord_pid():
            sctl = [str(scripts_dir() / "supervisorctl"), "-c", str(conf), "restart", "runspec-scheduler"]
            p.actions.append(Action("run", "supervisorctl restart runspec-scheduler", argv=sctl))
        elif facts.supervisord is not None:
            p.actions.append(Action("run", "start supervisord (it starts the daemon)", argv=[str(facts.supervisord), "-c", str(conf)]))
        else:
            p.error = "supervisord isn't installed in this venv any more"
            p.hint = f"reinstall it: {scripts_dir() / 'python'} -m pip install 'runspec-scheduler[supervisor]'"
        return p
    # The cron watchdog (or no keeper at all): stop the daemon, start it detached.
    if pid:
        p.actions.append(Action("stop", f"stop the daemon (pid {pid})", pid=pid))
    p.actions.append(Action("run", "start the daemon (run --detach)", argv=run_argv(config, detach=True)))
    if keeper_kind == "none":
        p.warnings.append("no keeper is installed, so nothing restarts the daemon after a crash or reboot; set one up: runspec-scheduler install-service --write")
    return p


# ── carrying a plan out ─────────────────────────────────────────────────────────


def _stop(pid: int, wait: float = 30) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not _alive(pid) or _zombie(pid):
            return
        time.sleep(0.2)
    raise RuntimeError(f"the daemon (pid {pid}) didn't stop within {int(wait)}s of SIGTERM")


def _zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def apply(plan: Plan) -> dict[str, Any]:
    """Carry out ``plan``; stops at the first failing step."""
    if plan.error:
        return plan.to_dict(written=False)
    done: list[str] = []
    for a in plan.actions:
        try:
            if a.kind == "write":
                assert a.path is not None and a.content is not None
                a.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = a.path.with_name(f".{a.path.name}.tmp")
                tmp.write_text(a.content, encoding="utf-8")
                os.replace(tmp, a.path)
            elif a.kind == "unlink":
                assert a.path is not None
                a.path.unlink(missing_ok=True)
            elif a.kind == "rmdir":
                assert a.path is not None
                with contextlib.suppress(OSError):
                    (a.path / "conf.d").rmdir()
                for leftover in ("supervisor.sock", "supervisord.pid"):
                    (a.path / leftover).unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    a.path.rmdir()
            elif a.kind == "crontab":
                existing = _read_own_crontab()
                _write_own_crontab(strip_keeper_lines(existing) + _tagged(a.lines or []), existing)
            elif a.kind == "run":
                assert a.argv is not None
                env = _systemctl_env() if a.systemd_env else None
                proc = subprocess.run(a.argv, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120)
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
                    raise RuntimeError(f"{shlex.join(a.argv)}: {err}")
            elif a.kind == "stop":
                assert a.pid is not None
                _stop(a.pid)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            plan.error = f"{a.desc}: {exc}"
            if a.systemd_env and _is_bus_error(str(exc)):
                plan.hint = _login_shell_hint()
            out = plan.to_dict(written=bool(done))
            out["done"] = done
            return out
        done.append(a.desc)
    out = plan.to_dict(written=True)
    out["done"] = done
    return out
