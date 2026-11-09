"""bridge_provision.py — ProvisionMixin: create and remove runspec venvs on hosts.

Settings → Jump Hosts gains *Provision venv…* (one host from the host editor, or
many at once — picked directly or by group/profile, like *Copy key to all
hosts*) and a per-path *Deprovision…*. The pure step scripts, guards and
parsers live in :mod:`provision`; this seam owns the I/O: every step runs over
the host's pooled SSH connection (``_ssh_run``) through
:func:`marketplace.remote_command`, so a run-as service account is reached with
``sudo -n -u`` / ``su`` and the index URL rides the SPEC env frame on stdin.

Events:

* ``runspec:provision_progress`` ``{op, kind, host, phase, …}`` — ``phase`` is
  ``step`` (a step started), ``line`` (output), or ``done`` (host finished).
* ``runspec:provision_done`` ``{op, kind, status, results}`` — one per op; the
  host file has already been updated (path added / removed) and a discovery
  refresh kicked.

Every provision and deprovision writes audit events (``venv.provision.*``,
``venv.deprovision.*``, ``venv.logs.retained``) to the console's audit log via
``_append_tool_audit``.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import host_schedules as hs
from . import hosts as hosts_mod
from . import marketplace as mp
from . import provision as prov
from .bridge_const import logger

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

_PROBE_TIMEOUT_S = 60
_CREATE_TIMEOUT_S = 300
_PIP_TIMEOUT_S = 900
_STEP_TIMEOUT_S = 120
#: Steps that run ``runspec-scheduler``: a failure is explained from the JSON it
#: printed and the warning carries the full command to retry on the host.
_SCHEDULER_STEPS = ("settings", "scheduler", "keeper", "sync", "restart")
_SETTINGS_NOTE = (
    "Couldn't set the schedule time zone / package schedules (they need "
    f"runspec-scheduler {hs.PACKAGES_MIN} or later in the venv); set them later "
    "from the venv's Settings on the Schedules tab"
)
_SYNC_NOTE = (
    "Couldn't bring the crontab up to date; Sync crontab on the Schedules tab "
    "does it later"
)
_MAX_LINES = 400
_MAX_OPS = 8

_STEPS = (
    "preflight",
    "create",
    "pip",
    "install",
    "permissions",
    "inbox",
    "retention",
    "verify",
    "access",
)


class ProvisionMixin:
    _connected_cache: dict[str, bool]
    _lock: threading.Lock
    _runnables_cache: list[dict[str, Any]]

    # ── state ──

    def _prov_state(self) -> tuple[threading.Lock, dict[str, dict[str, Any]]]:
        """The op registry, created on first use (keeps ``Bridge.__init__`` lean)."""
        st = getattr(self, "_provision_state", None)
        if st is None:
            st = (threading.Lock(), {})
            self._provision_state = st
        return st

    # ── JS API ──

    def provision_options(self) -> dict[str, Any]:
        """What the provision / deprovision dialogs pick from: the remote hosts
        (connection state, group, profiles, runspec paths), the group and
        profile names, and the package indexes (Marketplace repos)."""
        hosts = self._prov_remote_hosts()
        with self._lock:
            connected = dict(self._connected_cache)
        rows = [
            {
                "name": str(h.get("name")),
                "connected": bool(connected.get(str(h.get("name")), False)),
                "group": str(h.get("group") or ""),
                "profiles": hosts_mod.host_profiles(h),
                "runspec_paths": utils_mod._paths(h),
            }
            for h in hosts
        ]
        repos: list[dict[str, Any]] = []
        try:
            cfg = config_mod.read_marketplace()
            repos = [
                {"id": r.id, "name": r.name, "url": r.url}
                for r in self._marketplace_repos(cfg, resolve_auth=False)  # type: ignore[attr-defined]
                if r.enabled
            ]
        except Exception:  # noqa: BLE001 — a broken pip.conf never breaks the dialog
            logger.warning("provision: package indexes unreadable", exc_info=True)
        return {
            "hosts": rows,
            "groups": list(hosts_mod.groups_map(hosts).keys()),
            "profiles": list(hosts_mod.profiles_map(hosts).keys()),
            "repos": repos,
            "min_python": ".".join(str(x) for x in prov.MIN_RUNSPEC_PYTHON),
        }

    def provision_interpreters(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The Python interpreters found on ``payload.host`` (plus any
        ``extra_paths`` the operator browsed to), each with its version and
        whether runspec can run on it."""
        payload = dict(payload or {})
        name = str(payload.get("host") or "")
        h = self._prov_host(name)
        extra = [str(p) for p in payload.get("extra_paths") or [] if isinstance(p, str)]
        code, out = self._prov_run(h, prov.probe_argv(extra), timeout=_PROBE_TIMEOUT_S)
        res = prov.parse_probe(out)
        if not res.user and code != 0:
            logger.warning(
                "provision: interpreter probe failed on %s (exit %s): %s",
                name,
                code,
                prov.tail(out, 40),
            )
            return {
                "ok": False,
                "host": name,
                "error": prov.failure_text(out, code, limit=12),
                "pythons": [],
            }
        return {
            "ok": True,
            "host": name,
            "user": res.user,
            "os": res.os,
            "pythons": [i.to_dict() for i in res.pythons],
        }

    def provision_preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Check every selected host before anything is changed. Returns one row
        per host: the interpreter it would use, the account the steps run as,
        and its blockers / warnings. Nothing on a host is modified."""
        plan = self._prov_plan(payload)
        rows = self._prov_fanout(
            lambda t: self._prov_preflight_one(plan, t), plan["hosts"]
        )
        return {"dest": plan["dest"], "rows": rows}

    def provision_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Provision the venv on every selected host (each re-checked first;
        a host that fails preflight is skipped, never half-built). Returns
        ``{op}``; progress arrives as ``runspec:provision_progress`` events."""
        plan = self._prov_plan(payload)
        env = self._prov_pip_env(str((payload or {}).get("repo") or ""))
        record = self._prov_new_op("provision", plan["hosts"], dest=plan["dest"])
        threading.Thread(
            target=self._prov_run_provision,
            args=(record, plan, env),
            name="provision",
            daemon=True,
        ).start()
        return {"op": record["op"], "hosts": plan["hosts"]}

    def provision_op(self, op: str) -> dict[str, Any] | None:
        lock, ops = self._prov_state()
        with lock:
            rec = ops.get(str(op))
            if rec is None:
                return None
            return {k: v for k, v in rec.items() if k != "cancel"}

    def provision_cancel(self, op: str) -> bool:
        """Stop an op between steps (a running step finishes; later steps and
        hosts that haven't started are skipped)."""
        lock, ops = self._prov_state()
        with lock:
            rec = ops.get(str(op))
        if rec is None or rec.get("status") != "running":
            return False
        rec["cancel"].set()
        return True

    def deprovision_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read-only facts for the confirm dialog, per ``{host, runspec_path}``
        target: the venv root, whether it really is a venv, owner, size, logs
        size, the runnables discovered in it, and what may reference them."""
        targets = self._deprov_targets(payload)
        rows = self._prov_fanout(self._deprov_inspect_one, targets)
        names = sorted({prov.venv_name(t["root"]) for t in targets})
        return {"rows": rows, "names": names}

    def deprovision_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete the venv on every target after the typed confirmation. The
        operator must type the venv folder name (one name per request); each
        host re-checks the guards in the same command as the delete."""
        payload = dict(payload or {})
        targets = self._deprov_targets(payload)
        names = {prov.venv_name(t["root"]) for t in targets}
        if len(names) != 1:
            raise ValueError("Deprovision one venv name at a time.")
        name = next(iter(names))
        if str(payload.get("confirm") or "").strip() != name:
            raise ValueError(f"Type the venv name ({name}) to confirm.")
        keep_logs = bool(payload.get("keep_logs", True))
        become = prov.validate_become(str(payload.get("become_method") or "sudo"))
        record = self._prov_new_op(
            "deprovision", [t["host"] for t in targets], dest=name, keep_logs=keep_logs
        )
        for t in targets:
            self._prov_audit(
                "venv.deprovision.confirmed",
                host=t["host"],
                path=t["root"],
                keep_logs=keep_logs,
            )
        threading.Thread(
            target=self._prov_run_deprovision,
            args=(record, targets, keep_logs, become),
            name="deprovision",
            daemon=True,
        ).start()
        return {"op": record["op"], "hosts": [t["host"] for t in targets]}

    def venv_update_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read-only state of an existing venv on every ``{host, runspec_path}``
        target, for the Update dialog: owner, runspec version, logs inbox, log
        cleanup policy, and how its schedules fire. Nothing is changed."""
        targets = self._deprov_targets(payload, verb="update")
        return {"rows": self._prov_fanout(self._upd_inspect, targets)}

    def venv_update_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply the chosen options to an existing venv on every target, as the
        venv's owner: upgrade runspec / install packages, set up the logs inbox,
        set the log cleanup policy, move schedules to the daemon (or back to
        crontab). The venv is never recreated and its logs are kept. Progress
        arrives as ``runspec:provision_progress`` events with ``kind`` update."""
        plan = self._upd_plan(payload)
        env = (
            self._prov_pip_env(str((payload or {}).get("repo") or ""))
            if plan["specs"]
            else {}
        )
        hosts = [t["host"] for t in plan["targets"]]
        names = sorted({prov.venv_name(t["root"]) for t in plan["targets"]})
        record = self._prov_new_op("update", hosts, dest=", ".join(names))
        threading.Thread(
            target=self._upd_run,
            args=(record, plan, env),
            name="venv-update",
            daemon=True,
        ).start()
        return {"op": record["op"], "hosts": hosts}

    # ── internals: hosts + transport ──

    def _prov_remote_hosts(self) -> list[dict[str, Any]]:
        all_fn = getattr(self, "_all_host_entries", None)
        entries = list(all_fn()) if callable(all_fn) else []
        return [h for h in entries if h.get("ssh")]

    def _prov_host(self, name: str) -> dict[str, Any]:
        for h in self._prov_remote_hosts():
            if str(h.get("name")) == name:
                return h
        raise ValueError(f"{name or 'That host'} is not a configured remote host.")

    def _prov_run(
        self,
        h: dict[str, Any],
        argv: list[str],
        *,
        run_as: str = "",
        become_method: str = "sudo",
        env: dict[str, str] | None = None,
        timeout: int = _STEP_TIMEOUT_S,
    ) -> tuple[int, str]:
        """Run ``argv`` on host ``h`` (escalated to ``run_as`` when set) and
        return ``(exit_code, combined_output)``. No escalation when ``run_as``
        already is the SSH login: ``sudo -u <self>`` still needs a sudoers rule,
        and an account without one would be refused for its own venv."""
        if run_as and run_as == self._prov_login(h):
            run_as = ""
        cmd, frame = mp.remote_command(
            argv, run_as=run_as, become_method=become_method, env=env
        )
        feed: dict[str, Any] = {"stdin": frame} if frame else {}
        code, out, err = self._ssh_run(  # type: ignore[attr-defined]
            str(h["ssh"]),
            cmd,
            identity_file=h.get("identityFile"),
            timeout=timeout,
            **feed,
        )
        text = "\n".join(p for p in ((out or "").rstrip(), (err or "").rstrip()) if p)
        return int(code), text

    def _prov_login(self, h: dict[str, Any]) -> str:
        """The SSH login's account name (``""`` if it can't be read), cached
        per SSH target for the session."""
        target = str(h.get("ssh") or "")
        cache: dict[str, str] | None = getattr(self, "_prov_logins", None)
        if cache is None:
            cache = {}
            self._prov_logins = cache
        if target in cache:
            return cache[target]
        try:
            code, out = self._prov_run(h, ["id", "-un"], timeout=_PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — best-effort
            return ""
        last = (out or "").strip().splitlines()[-1:] if code == 0 else []
        try:
            login = prov.validate_user(last[0]) if last else ""
        except prov.ProvisionError:
            login = ""
        if login:
            cache[target] = login
        return login

    def _prov_fanout(self, fn: Any, items: list[Any]) -> list[dict[str, Any]]:
        if not items:
            return []
        workers_fn = getattr(self, "_fanout_workers", None)
        try:
            workers = int(workers_fn()) if callable(workers_fn) else 4
        except Exception:
            workers = 4
        with ThreadPoolExecutor(
            max_workers=min(max(1, workers), len(items)), thread_name_prefix="provision"
        ) as ex:
            return list(ex.map(fn, items))

    def _prov_pip_env(self, repo_id: str) -> dict[str, str]:
        """pip's index env for a Marketplace repo id (``{}`` = the host's own
        pip config). The URL can carry a credential, so it rides the env frame."""
        repo_id = (repo_id or "").strip()
        if not repo_id:
            return {}
        cfg = config_mod.read_marketplace()
        repos = self._marketplace_repos(cfg, resolve_auth=True)  # type: ignore[attr-defined]
        repo = next((r for r in repos if r.id == repo_id), None)
        if repo is None:
            raise ValueError(f"Unknown package index {repo_id!r}.")
        return mp.pip_env(repo)

    # ── internals: provision ──

    def _prov_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a provision request into a plan (raises ``ValueError``)."""
        payload = dict(payload or {})
        try:
            dest = prov.normalize_dest(str(payload.get("dest") or ""))
            run_as = prov.validate_user(str(payload.get("run_as") or ""))
            become = prov.validate_become(str(payload.get("become_method") or "sudo"))
        except prov.ProvisionError as exc:
            raise ValueError(str(exc)) from exc
        names = [str(n) for n in payload.get("hosts") or [] if str(n).strip()]
        if not names:
            raise ValueError("Pick at least one host.")
        known = {str(h.get("name")) for h in self._prov_remote_hosts()}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(f"Not a configured remote host: {', '.join(unknown)}.")
        python = str(payload.get("python") or "").strip()
        per_host = {
            str(k): str(v).strip()
            for k, v in (payload.get("per_host_python") or {}).items()
            if str(v or "").strip()
        }
        if not python and not all(n in per_host for n in names):
            raise ValueError(
                "Pick a Python interpreter (a path or a version like 3.12)."
            )
        for v in [python, *per_host.values()]:
            if v and not v.startswith("/") and not prov.parse_version(v):
                raise ValueError(f"Not a Python path or version: {v!r}.")
        try:
            scheduler = prov.validate_scheduler(str(payload.get("scheduler") or ""))
            retention = prov.validate_retention(payload.get("retention"))
            sched_settings = prov.validate_scheduler_settings(
                payload.get("scheduler_settings")
            )
        except prov.ProvisionError as exc:
            raise ValueError(str(exc)) from exc
        packages: list[str] = []
        for p in payload.get("packages") or []:
            s = str(p or "").strip()
            if not s:
                continue
            try:
                packages.append(mp.validate_spec(s))
            except mp.MarketplaceError as exc:
                raise ValueError(str(exc)) from exc
        # The daemon comes from runspec-scheduler; the operator's own pin (if
        # they listed it) still wins over the bare one.
        packages = prov.add_scheduler_spec(packages, scheduler)
        return {
            "dest": dest,
            "run_as": run_as,
            "become_method": become,
            "hosts": list(dict.fromkeys(names)),
            "python": python,
            "per_host_python": per_host,
            "packages": prov.install_specs(packages),
            "scheduler": scheduler,
            "retention": retention,
            "scheduler_settings": sched_settings if scheduler else {},
        }

    def _prov_preflight_one(self, plan: dict[str, Any], name: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "host": name,
            "ok": False,
            "python": "",
            "version": "",
            "user": "",
            "dest_state": "",
            "blockers": [],
            "warnings": [],
        }
        with self._lock:
            connected = bool(self._connected_cache.get(name, False))
        if not connected:
            row["blockers"].append("Host is not connected.")
            return row
        try:
            h = self._prov_host(name)
            wanted = plan["per_host_python"].get(name) or plan["python"]
            python = wanted
            if not wanted.startswith("/"):
                code, out = self._prov_run(
                    h, prov.probe_argv(), timeout=_PROBE_TIMEOUT_S
                )
                found = prov.pick_interpreter(prov.parse_probe(out).pythons, wanted)
                if found is None:
                    row["blockers"].append(f"No Python {wanted} found on this host.")
                    return row
                python = found.path
            row["python"] = python
            code, out = self._prov_run(
                h,
                prov.preflight_argv(python, plan["dest"]),
                run_as=plan["run_as"],
                become_method=plan["become_method"],
                timeout=_PROBE_TIMEOUT_S,
            )
            facts = prov.parse_preflight(out)
            if not facts:
                logger.warning(
                    "provision: check failed on %s (run as %r via %s, exit %s): %s",
                    name,
                    plan["run_as"],
                    plan["become_method"],
                    code,
                    prov.tail(out, 40),
                )
                msg = prov.failure_text(
                    out,
                    code,
                    run_as=plan["run_as"],
                    become_method=plan["become_method"],
                    limit=4,
                )
                row["blockers"].append(
                    f"Preflight failed (exit {code}); check the run-as account."
                    if msg == f"exit {code}"
                    else msg
                )
                return row
            root_via = ""
            if prov.needs_handoff(facts):
                # The run-as account can't make the folder; can the SSH login
                # make it as root and hand it over?
                _c, rc_out = self._prov_run(
                    h, prov.root_check_argv(), timeout=_PROBE_TIMEOUT_S
                )
                root_via = prov.parse_root_check(rc_out)
            blockers, warnings = prov.preflight_verdict(facts, root_via=root_via)
            handoff = (
                not blockers
                and prov.needs_handoff(facts)
                and root_via in ("root", "sudo")
            )
            row.update(
                {
                    "handoff": handoff,
                    "handoff_via": root_via if handoff else "",
                    "version": facts.get("python_version", ""),
                    "user": facts.get("user", ""),
                    "dest_state": facts.get("dest", ""),
                    "blockers": blockers,
                    "warnings": warnings,
                    "ok": not blockers,
                }
            )
        except Exception as exc:  # noqa: BLE001 — one host never breaks the table
            logger.warning("provision: check failed on %s", name, exc_info=True)
            row["blockers"].append(str(exc))
        return row

    def _prov_new_op(self, kind: str, hosts: list[str], **extra: Any) -> dict[str, Any]:
        lock, ops = self._prov_state()
        op = uuid.uuid4().hex[:12]
        record: dict[str, Any] = {
            "op": op,
            "kind": kind,
            "status": "running",
            "started_at": time.time(),
            "hosts": list(hosts),
            "results": [],
            "cancel": threading.Event(),
            **extra,
        }
        with lock:
            ops[op] = record
            finished = [k for k, v in ops.items() if v.get("status") != "running"]
            for k in finished[:-_MAX_OPS]:
                ops.pop(k, None)
        return record

    def _prov_progress(self, **detail: Any) -> None:
        dispatch = getattr(self, "_dispatch", None)
        if callable(dispatch):
            try:
                dispatch("runspec:provision_progress", detail)
            except Exception:
                pass

    def _prov_audit(self, event: str, **fields: Any) -> None:
        append = getattr(self, "_append_tool_audit", None)
        if not callable(append):
            return
        user_fn = getattr(self, "_current_user", None)
        user = user_fn() if callable(user_fn) else ""
        append(prov.audit_record(event, utils_mod._iso_now(), user=user, **fields))

    def _prov_run_provision(
        self, record: dict[str, Any], plan: dict[str, Any], env: dict[str, str]
    ) -> None:
        op = record["op"]
        cancel: threading.Event = record["cancel"]
        dest = plan["dest"]
        paths = prov.venv_paths(dest)

        def _one(name: str) -> dict[str, Any]:
            res: dict[str, Any] = {
                "host": name,
                "ok": False,
                "step": "",
                "summary": "",
                "warnings": [],
                "python": "",
                "lines": [],
            }
            lines: list[str] = res["lines"]

            def _emit(text: str) -> None:
                for ln in (text or "").splitlines():
                    if len(lines) < _MAX_LINES:
                        lines.append(ln)
                    self._prov_progress(
                        op=op, kind="provision", host=name, phase="line", line=ln
                    )

            def _step(step: str) -> None:
                res["step"] = step
                self._prov_progress(
                    op=op, kind="provision", host=name, phase="step", step=step
                )

            # Set once this run starts making the venv folder, so a failure
            # after that point undoes it and a retry isn't refused as "not empty".
            made: dict[str, Any] = {}

            def _cleanup() -> str:
                h, pf = made["host"], made["pf"]
                # Whoever made the folder removes it: root under a parent the
                # run-as account can't write, else the run-as account.
                if pf.get("handoff"):
                    who = "root" if pf.get("handoff_via") == "sudo" else ""
                    via = "sudo"
                else:
                    who, via = plan["run_as"], plan["become_method"]
                try:
                    code, out = self._prov_run(
                        h,
                        prov.cleanup_argv(pf["python"], dest, pf.get("dest_state", "")),
                        run_as=who,
                        become_method=via,
                        timeout=_STEP_TIMEOUT_S,
                    )
                except Exception as exc:  # noqa: BLE001
                    code, out = -1, str(exc)
                _emit(out)
                if code == 0:
                    return "cleaned"
                logger.warning(
                    "provision: cleanup of %s on %s failed: %s",
                    dest,
                    name,
                    prov.tail(out),
                )
                return "failed"

            def _fail(summary: str) -> dict[str, Any]:
                cleaned = _cleanup() if made else ""
                if cleaned == "cleaned":
                    summary += f" Undid the partial venv at {dest}, so you can provision again."
                elif cleaned == "failed":
                    summary += (
                        f" Couldn't undo the partial venv at {dest}; remove it "
                        "before provisioning there again."
                    )
                res["summary"] = summary
                logger.warning(
                    "provision: %s failed at %s: %s", name, res["step"], summary
                )
                self._prov_progress(
                    op=op,
                    kind="provision",
                    host=name,
                    phase="done",
                    ok=False,
                    step=res["step"],
                    summary=summary,
                )
                self._prov_audit(
                    "venv.provision.failed",
                    host=name,
                    path=dest,
                    step=res["step"],
                    run_as=plan["run_as"],
                    error=summary,
                    cleanup=cleaned or "none",
                )
                return res

            if cancel.is_set():
                res["summary"] = "cancelled"
                res["skipped"] = True
                return res
            _step("preflight")
            pf = self._prov_preflight_one(plan, name)
            res["python"] = pf.get("python", "")
            res["warnings"] = list(pf.get("warnings") or [])
            if not pf.get("ok"):
                return _fail("; ".join(pf.get("blockers") or ["preflight failed"]))
            h = self._prov_host(name)
            self._prov_audit(
                "venv.provision.started",
                host=name,
                path=dest,
                python=pf["python"],
                python_version=pf.get("version", ""),
                run_as=plan["run_as"],
                packages=plan["packages"],
                folder_by_root=bool(pf.get("handoff")),
                scheduler=plan.get("scheduler", ""),
                retention=plan.get("retention") or None,
                scheduler_settings=plan.get("scheduler_settings") or None,
            )
            if pf.get("handoff"):
                if cancel.is_set():
                    return _fail("cancelled before folder")
                _step("folder")
                owner = pf.get("user") or plan["run_as"]
                try:
                    code, out = self._prov_run(
                        h,
                        prov.handoff_argv(pf["python"], dest, owner),
                        # Root via passwordless sudo, or directly when the
                        # SSH login already is root.
                        run_as="root" if pf.get("handoff_via") == "sudo" else "",
                        become_method="sudo",
                        timeout=_STEP_TIMEOUT_S,
                    )
                except Exception as exc:  # noqa: BLE001
                    code, out = -1, str(exc)
                _emit(out)
                if code != 0:
                    return _fail(
                        "creating the venv folder as root failed: "
                        + prov.failure_text(
                            out,
                            code,
                            run_as="root" if pf.get("handoff_via") == "sudo" else "",
                        )
                    )
            # From here on this run owns the folder (made by the folder step,
            # or about to be made by create), so a failure undoes it.
            made.update(host=h, pf=pf)
            kw = {"run_as": plan["run_as"], "become_method": plan["become_method"]}
            # A venv owned by another account: the owner also grants the SSH
            # login an ACL on logs/, so this console can read the runs index.
            login = self._prov_login(h) if plan["run_as"] else ""
            if login == plan["run_as"]:
                login = ""
            steps: list[tuple[str, list[str], dict[str, str] | None, int, bool]] = [
                (
                    "create",
                    prov.create_argv(pf["python"], dest),
                    None,
                    _CREATE_TIMEOUT_S,
                    True,
                ),
                ("pip", prov.pip_upgrade_argv(dest), env, _PIP_TIMEOUT_S, False),
                (
                    "install",
                    mp.pip_install_argv(paths["python"], plan["packages"]),
                    env,
                    _PIP_TIMEOUT_S,
                    True,
                ),
                (
                    "permissions",
                    prov.permissions_argv(dest),
                    None,
                    _STEP_TIMEOUT_S,
                    False,
                ),
                (
                    "inbox",
                    prov.inbox_argv(dest, acl_user=login),
                    None,
                    _STEP_TIMEOUT_S,
                    False,
                ),
            ]
            if plan.get("retention"):
                steps.append(
                    (
                        "retention",
                        prov.retention_argv(dest, plan["retention"]),
                        None,
                        _STEP_TIMEOUT_S,
                        False,
                    )
                )
            steps.append(
                ("verify", prov.verify_argv(dest), None, _STEP_TIMEOUT_S, True)
            )
            if login:
                # The owner isn't the SSH login: check the login can run the
                # venv too, since that's the account the console reads it as.
                steps.append(
                    ("access", prov.verify_argv(dest), None, _STEP_TIMEOUT_S, False)
                )
            if plan.get("scheduler") and plan.get("scheduler_settings"):
                steps.append(
                    (
                        "settings",
                        prov.scheduler_settings_argv(dest, plan["scheduler_settings"]),
                        None,
                        _STEP_TIMEOUT_S,
                        False,
                    )
                )
            if plan.get("scheduler"):
                steps += [
                    (
                        "scheduler",
                        prov.scheduler_use_argv(dest),
                        None,
                        _STEP_TIMEOUT_S,
                        False,
                    ),
                    (
                        "keeper",
                        prov.scheduler_keeper_argv(dest, plan["scheduler"]),
                        None,
                        _STEP_TIMEOUT_S,
                        False,
                    ),
                ]
            notes = {
                "inbox": "Logs inbox setup failed; the venv works but other accounts' "
                "runs fall back to their own ~/logs.",
                "pip": "pip self-upgrade failed; continued with the venv's bundled pip.",
                "permissions": _permissions_note(dest),
                "access": _access_note(login, plan["run_as"], dest),
                "retention": "Couldn't set the log retention policy (it needs runspec "
                f"{prov.RETENTION_MIN_RUNSPEC} or later in the venv); the venv works. "
                "On the host, as the owner: "
                + prov.retention_hint(plan.get("retention") or {}),
                "scheduler": "Couldn't switch the venv's schedules to the daemon "
                "(the venv works)",
                "keeper": "Couldn't set up the scheduler daemon's keeper (the venv "
                "works)",
                "settings": _SETTINGS_NOTE,
            }
            version = ""
            skip_keeper = False
            for step, argv, step_env, timeout, required in steps:
                if cancel.is_set():
                    return _fail(f"cancelled before {step}")
                if step == "keeper" and skip_keeper:
                    continue
                _step(step)
                try:
                    code, out = self._prov_run(
                        h,
                        argv,
                        env=step_env,
                        timeout=timeout,
                        **({"run_as": ""} if step == "access" else kw),
                    )
                except Exception as exc:  # noqa: BLE001
                    code, out = -1, str(exc)
                _emit(out)
                if code != 0:
                    sched = step in _SCHEDULER_STEPS
                    msg = (prov.scheduler_failure if sched else prov.failure_text)(
                        out,
                        code,
                        run_as=plan["run_as"],
                        become_method=plan["become_method"],
                    )
                    if required:
                        return _fail(f"{step} failed: {msg}")
                    note = notes.get(step, f"{step} failed")
                    retry = ""
                    if sched:
                        note += f": {msg}."
                        retry = " To retry, on the host: " + prov.host_command(
                            argv,
                            ""
                            if plan["run_as"] == self._prov_login(h)
                            else plan["run_as"],
                            plan["become_method"],
                        )
                        logger.warning(
                            "venv provision: %s:%s %s step failed (exit %s): %s",
                            name,
                            dest,
                            step,
                            code,
                            (out or "").strip(),
                        )
                    elif step == "retention":
                        note += f" ({msg})"
                    if step == "keeper":
                        rescue = self._keeper_rescue(
                            h,
                            dest,
                            plan["scheduler"],
                            run_as=plan["run_as"],
                            become=plan["become_method"],
                            emit=_emit,
                        )
                        note += " " + rescue["text"]
                        if rescue["scheduler"]:
                            res["scheduler"] = rescue["scheduler"]
                        res["warnings"].append(note + retry)
                        res["warnings"].extend(rescue["warnings"])
                        continue
                    # No daemon to keep when the backend didn't switch.
                    skip_keeper = skip_keeper or step == "scheduler"
                    res["warnings"].append(note + retry)
                elif step == "keeper":
                    kept = prov.parse_keeper(out)
                    res["scheduler"] = {
                        "manager": str(kept.get("manager") or plan["scheduler"]),
                        "reason": str(kept.get("reason") or ""),
                    }
                    res["warnings"].extend(str(w) for w in kept.get("warnings") or [])
                if step == "verify":
                    out_lines = (out or "").strip().splitlines()
                    version = out_lines[-1].strip() if out_lines else ""
            # The new venv's own ledger starts with how it was made.
            if not self._prov_host_audit(
                h,
                dest,
                "venv.provision",
                "succeeded",
                run_as=plan["run_as"],
                become_method=plan["become_method"],
                fallback_logger=False,
                detail={
                    "operator": self._prov_operator(),
                    "python": pf["python"],
                    "python_version": pf.get("version", ""),
                    "packages": plan["packages"],
                    "runspec": version,
                    "folder_by_root": bool(pf.get("handoff")),
                    **(
                        {"log_retention": plan["retention"]}
                        if plan.get("retention")
                        else {}
                    ),
                },
                emit=_emit,
            ):
                res["warnings"].append(
                    "Couldn't record the provision in the venv's audit log; "
                    "it's in the console's audit log."
                )
            res.update(
                {
                    "ok": True,
                    "summary": version or "ready",
                    "runspec_path": paths["runspec"],
                }
            )
            self._prov_progress(
                op=op,
                kind="provision",
                host=name,
                phase="done",
                ok=True,
                step="verify",
                summary=res["summary"],
                warnings=res["warnings"],
                **({"scheduler": res["scheduler"]} if res.get("scheduler") else {}),
            )
            self._prov_audit(
                "venv.provision.succeeded",
                host=name,
                path=dest,
                python=pf["python"],
                run_as=plan["run_as"],
                packages=plan["packages"],
                runspec=version,
            )
            return res

        results: list[dict[str, Any]] = record["results"]
        try:
            for r in self._prov_fanout(_one, plan["hosts"]):
                results.append(r)
        finally:
            ok_hosts = [r["host"] for r in results if r.get("ok")]
            if ok_hosts:
                try:
                    self._prov_edit_hosts(
                        ok_hosts, lambda e: prov.add_runspec_path(e, paths["runspec"])
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "provision: could not update hosts file", exc_info=True
                    )
            self._prov_finish(record, results)

    # ── internals: deprovision ──

    def _deprov_targets(
        self, payload: dict[str, Any], verb: str = "deprovision"
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in (payload or {}).get("targets") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("host") or "")
            rp = str(t.get("runspec_path") or "")
            h = self._prov_host(name)
            if rp not in utils_mod._paths(h):
                raise ValueError(f"{rp} is not one of {name}'s runspec paths.")
            try:
                root = prov.root_from_runspec_path(rp)
                prov.check_removable(root)
            except prov.ProvisionError as exc:
                raise ValueError(f"{name}: {exc}") from exc
            out.append({"host": name, "runspec_path": rp, "root": root})
        if not out:
            raise ValueError(f"Pick at least one venv to {verb}.")
        return out

    def _deprov_inspect_one(self, t: dict[str, Any]) -> dict[str, Any]:
        row = self._venv_inspect(t)
        if row.get("kind") == "venv" and row["scheduler_pid"] and not row["scheduler"]:
            row["blockers"].append(
                f"A scheduler daemon (pid {row['scheduler_pid']}) is running from this "
                "venv but bin/runspec-scheduler is gone; stop it on the host first."
            )
            row["ok"] = False
        return row

    def _venv_inspect(self, t: dict[str, Any]) -> dict[str, Any]:
        """Read-only facts about an existing venv on a host (as the SSH login),
        with blockers when it isn't a runspec venv the console may act on."""
        row: dict[str, Any] = {
            **t,
            "ok": False,
            "kind": "",
            "owner": "",
            "user": "",
            "size_kb": 0,
            "logs_kb": 0,
            "runnables": [],
            "references": [],
            "blockers": [],
            "scheduler": False,
            "scheduler_pid": 0,
            "runspec_version": "",
            "inbox": False,
            "retention": {},
            "backend": "",
        }
        with self._lock:
            connected = bool(self._connected_cache.get(t["host"], False))
            cached = list(getattr(self, "_runnables_cache", []) or [])
        venv = hosts_mod.venv_name(t["runspec_path"])
        row["runnables"] = sorted(
            {
                str(r.get("name"))
                for r in cached
                if r.get("host") == t["host"] and r.get("group") == venv
            }
        )
        row["references"] = self._deprov_references(row["runnables"])
        if not connected:
            row["blockers"].append("Host is not connected.")
            return row
        try:
            h = self._prov_host(t["host"])
            code, out = self._prov_run(
                h, prov.inspect_argv(t["root"]), timeout=_PROBE_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            row["blockers"].append(str(exc))
            return row
        facts = prov.parse_inspect(out)
        kind = facts.get("kind", "")
        row.update(
            {
                "kind": kind,
                "owner": facts.get("owner", ""),
                "user": facts.get("user", ""),
                "python": facts.get("python", ""),
                "size_kb": _to_int(facts.get("size_kb")),
                "logs_kb": _to_int(facts.get("logs_kb")),
                "scheduler": facts.get("scheduler") == "1",
                "scheduler_pid": _to_int(facts.get("scheduler_pid")),
                "runspec_version": prov.runspec_version(
                    facts.get("runspec_version", "")
                ),
                "inbox": facts.get("inbox") == "1",
                "retention": prov.parse_retention_fact(facts.get("retention", "")),
                "backend": facts.get("backend", "")
                or ("cron" if facts.get("scheduler") == "1" else ""),
            }
        )
        if kind == "missing":
            row["blockers"].append(
                "Nothing is there any more; remove the path from the host instead."
            )
        elif kind == "symlink":
            row["blockers"].append(
                "The venv path is a symlink; refusing to delete through it."
            )
        elif kind != "venv":
            row["blockers"].append("Not a runspec venv (no pyvenv.cfg + bin/runspec).")
        row["ok"] = not row["blockers"]
        return row

    def _upd_inspect(self, t: dict[str, Any]) -> dict[str, Any]:
        """:meth:`_venv_inspect` plus, when the venv has runspec-scheduler, how
        many schedules a backend switch would move (``schedules``; ``None``
        when ``status`` couldn't be read — ``schedules_error`` says why). Runs
        ``status`` as the venv owner, which changes nothing."""
        row = self._venv_inspect(t)
        row["schedules"] = None
        if not (row.get("ok") and row.get("scheduler")):
            return row
        owner = str(row.get("owner") or "")
        run_as = "" if owner in ("", "root", row.get("user")) else owner
        try:
            code, out = self._prov_run(
                self._prov_host(t["host"]),
                prov.scheduler_status_argv(t["root"]),
                run_as=run_as,
                timeout=_PROBE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — the counts are a hint only
            code, out = -1, str(exc)
        row["schedules"] = prov.schedule_counts(out)
        if row["schedules"] is None:
            lines = [ln for ln in (out or "").splitlines() if ln.strip()]
            row["schedules_error"] = lines[-1] if lines else f"exit {code}"
        return row

    def _deprov_references(self, runnables: list[str]) -> list[str]:
        """Which saved configs mention any of ``runnables`` (a plain text scan —
        a hint for the confirm dialog, not a guarantee)."""
        if not runnables:
            return []
        files = {
            "triggers": config_mod.triggers_path(),
            "schedules": config_mod.hosts_path().parent / "runspec_schedules.toml",
            "saved runs": config_mod.saved_runs_path(),
            "runbooks": config_mod.runbooks_path(),
        }
        pat = re.compile(
            r"(?<![A-Za-z0-9-])("
            + "|".join(re.escape(n) for n in runnables)
            + r")(?![A-Za-z0-9_-])"
        )
        hits: list[str] = []
        for label, path in files.items():
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError:
                continue
            if pat.search(text):
                hits.append(label)
        return hits

    def _prov_run_deprovision(
        self,
        record: dict[str, Any],
        targets: list[dict[str, Any]],
        keep_logs: bool,
        become: str,
    ) -> None:
        op = record["op"]
        cancel: threading.Event = record["cancel"]

        def _one(t: dict[str, Any]) -> dict[str, Any]:
            name, root = t["host"], t["root"]
            res: dict[str, Any] = {
                "host": name,
                "path": root,
                "ok": False,
                "summary": "",
            }

            def _done(ok: bool, summary: str) -> dict[str, Any]:
                res.update({"ok": ok, "summary": summary})
                if not ok:
                    logger.warning("deprovision: %s:%s failed: %s", name, root, summary)
                self._prov_progress(
                    op=op,
                    kind="deprovision",
                    host=name,
                    phase="done",
                    ok=ok,
                    summary=summary,
                )
                self._prov_audit(
                    "venv.deprovision.succeeded" if ok else "venv.deprovision.failed",
                    host=name,
                    path=root,
                    keep_logs=keep_logs,
                    **({} if ok else {"error": summary}),
                )
                if ok and keep_logs:
                    self._prov_audit(
                        "venv.logs.retained", host=name, path=f"{root}/logs"
                    )
                return res

            if cancel.is_set():
                res.update({"summary": "cancelled", "skipped": True})
                return res
            self._prov_progress(
                op=op, kind="deprovision", host=name, phase="step", step="inspect"
            )
            info = self._deprov_inspect_one(t)
            if not info.get("ok"):
                return _done(False, "; ".join(info.get("blockers") or ["refused"]))
            owner = str(info.get("owner") or "")
            run_as = "" if not owner or owner == info.get("user") else owner
            # On the host's record before anything is deleted: the venv's ledger
            # (kept with logs/ when keep_logs) + syslog, or syslog alone.
            self._prov_progress(
                op=op, kind="deprovision", host=name, phase="step", step="audit"
            )
            recorded = self._prov_host_audit(
                self._prov_host(name),
                root,
                "venv.deprovision",
                "confirmed",
                run_as=run_as,
                become_method=become,
                fallback_logger=True,
                detail={
                    "operator": self._prov_operator(),
                    "keep_logs": keep_logs,
                    "owner": owner,
                },
            )
            if not recorded:
                logger.warning(
                    "deprovision: no host-side audit record for %s:%s", name, root
                )
            cron_left = ""
            if info.get("scheduler"):
                # Before the folder goes: no keeper may restart a daemon from it.
                stopped = self._deprov_stop_scheduler(
                    op, name, root, run_as=run_as, become=become
                )
                if stopped:
                    return _done(False, stopped)
                if info.get("backend") != "daemon":
                    # A cron venv's schedules are lines in its owner's crontab;
                    # left behind, they would fire into a deleted venv.
                    cron_left = self._deprov_clear_crontab(
                        op, name, root, run_as=run_as, become=become
                    )
            self._prov_progress(
                op=op, kind="deprovision", host=name, phase="step", step="remove"
            )
            try:
                code, out = self._prov_run(
                    self._prov_host(name),
                    prov.remove_argv(
                        str(info.get("python") or ""), root, keep_logs=keep_logs
                    ),
                    run_as=run_as,
                    become_method=become,
                    timeout=_CREATE_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001
                return _done(False, str(exc))
            for ln in (out or "").splitlines():
                self._prov_progress(
                    op=op, kind="deprovision", host=name, phase="line", line=ln
                )
            if code != 0:
                return _done(
                    False,
                    prov.failure_text(
                        out, code, run_as=run_as, become_method=become, limit=4
                    ),
                )
            summary = prov.tail(out, 1) or "removed"
            return _done(True, f"{summary}; {cron_left}" if cron_left else summary)

        results: list[dict[str, Any]] = record["results"]
        try:
            for r in self._prov_fanout(_one, targets):
                results.append(r)
        finally:
            removed = {r["host"]: r["path"] for r in results if r.get("ok")}
            if removed:
                try:
                    self._prov_edit_hosts(
                        list(removed),
                        lambda e: prov.remove_runspec_path(
                            e, removed[str(e.get("name"))]
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "deprovision: could not update hosts file", exc_info=True
                    )
            self._prov_finish(record, results)

    def _deprov_stop_scheduler(
        self, op: str, name: str, root: str, *, run_as: str, become: str
    ) -> str:
        """Remove the venv's scheduler keepers and stop its daemon. Returns why
        the venv must stay ("" when it may go): a daemon still running after
        the attempt. A runspec-scheduler too old for ``--remove`` with no daemon
        running is no reason to keep the venv."""
        self._prov_progress(
            op=op, kind="deprovision", host=name, phase="step", step="scheduler"
        )
        h = self._prov_host(name)
        try:
            code, out = self._prov_run(
                h,
                prov.scheduler_stop_argv(root),
                run_as=run_as,
                become_method=become,
                timeout=_STEP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            code, out = -1, str(exc)
        for ln in (out or "").splitlines():
            self._prov_progress(
                op=op, kind="deprovision", host=name, phase="line", line=ln
            )
        result = prov.parse_keeper(out)
        for step in result.get("root_steps") or []:
            self._prov_progress(
                op=op,
                kind="deprovision",
                host=name,
                phase="line",
                line=f"root has to run: {step}",
            )
        if code == 0:
            return ""
        try:
            _, again = self._prov_run(
                h, prov.inspect_argv(root), timeout=_PROBE_TIMEOUT_S
            )
            pid = _to_int(prov.parse_inspect(again).get("scheduler_pid"))
        except Exception:  # noqa: BLE001
            pid = -1
        if not pid:
            return ""
        why = prov.failure_text(out, code, run_as=run_as, become_method=become, limit=4)
        running = f"pid {pid}" if pid > 0 else "maybe"
        return (
            f"The venv's scheduler daemon is still running ({running}) and its keeper "
            f"couldn't be removed: {why}. Kept the venv; stop the daemon on the host "
            "(runspec-scheduler install-service --remove --write), then deprovision again."
        )

    def _deprov_clear_crontab(
        self, op: str, name: str, root: str, *, run_as: str, become: str
    ) -> str:
        """Remove the venv's schedule lines from the crontabs it wrote to, by
        moving it to the daemon backend (``runspec-scheduler use daemon``, 0.5.0+:
        it strips every line this venv wrote and starts nothing — the keepers are
        already gone). Returns a note for the summary when lines may be left;
        that never keeps the venv, it only means cron reports failed fires."""
        self._prov_progress(
            op=op, kind="deprovision", host=name, phase="step", step="crontab"
        )
        try:
            code, out = self._prov_run(
                self._prov_host(name),
                prov.scheduler_use_argv(root),
                run_as=run_as,
                become_method=become,
                timeout=_STEP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            code, out = -1, str(exc)
        for ln in (out or "").splitlines():
            self._prov_progress(
                op=op, kind="deprovision", host=name, phase="line", line=ln
            )
        result = prov.parse_cron_clear(out)
        if code == 0 and result["ok"]:
            return ""
        why = result["error"] or prov.failure_text(
            out, code, run_as=run_as, become_method=become, limit=2
        )
        return (
            f"its schedule lines may still be in the crontab ({why}); "
            "remove the '# runspec:sched:' lines by hand"
        )

    # ── internals: update an existing venv ──

    def _upd_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate an update request (raises ``ValueError``)."""
        payload = dict(payload or {})
        targets = self._deprov_targets(payload, verb="update")
        try:
            become = prov.validate_become(str(payload.get("become_method") or "sudo"))
            scheduler = prov.validate_update_scheduler(
                str(payload.get("scheduler") or "")
            )
            retention = prov.validate_retention(payload.get("retention"))
            sched_settings = prov.validate_scheduler_settings(
                payload.get("scheduler_settings")
            )
        except prov.ProvisionError as exc:
            raise ValueError(str(exc)) from exc
        packages: list[str] = []
        for p in payload.get("packages") or []:
            spec = str(p or "").strip()
            if not spec:
                continue
            try:
                packages.append(mp.validate_spec(spec))
            except mp.MarketplaceError as exc:
                raise ValueError(str(exc)) from exc
        specs = prov.update_install_specs(
            packages,
            upgrade_runspec=bool(payload.get("upgrade_runspec")),
            scheduler=scheduler,
        )
        inbox = bool(payload.get("inbox", False))
        if not (specs or inbox or retention or scheduler or sched_settings):
            raise ValueError("Pick at least one thing to update.")
        return {
            "targets": targets,
            "become_method": become,
            "specs": specs,
            "inbox": inbox,
            "retention": retention,
            "scheduler": scheduler,
            "scheduler_settings": sched_settings,
        }

    def _upd_run(
        self, record: dict[str, Any], plan: dict[str, Any], env: dict[str, str]
    ) -> None:
        op = record["op"]
        cancel: threading.Event = record["cancel"]
        become = plan["become_method"]

        def _one(t: dict[str, Any]) -> dict[str, Any]:
            name, root = t["host"], t["root"]
            res: dict[str, Any] = {
                "host": name,
                "path": root,
                "ok": False,
                "step": "",
                "summary": "",
                "warnings": [],
                "lines": [],
            }
            lines: list[str] = res["lines"]

            def _emit(text: str) -> None:
                for ln in (text or "").splitlines():
                    if len(lines) < _MAX_LINES:
                        lines.append(ln)
                    self._prov_progress(
                        op=op, kind="update", host=name, phase="line", line=ln
                    )

            def _step(step: str) -> None:
                res["step"] = step
                self._prov_progress(
                    op=op, kind="update", host=name, phase="step", step=step
                )

            applied: list[str] = []

            def _done(
                ok: bool, summary: str, h: Any = None, **who: str
            ) -> dict[str, Any]:
                res.update({"ok": ok, "summary": summary})
                if not ok:
                    logger.warning(
                        "venv update: %s:%s failed at %s: %s",
                        name,
                        root,
                        res["step"],
                        summary,
                    )
                if h is not None and not self._prov_host_audit(
                    h,
                    root,
                    "venv.update",
                    "succeeded" if ok else "failed",
                    run_as=who.get("run_as", ""),
                    become_method=become,
                    fallback_logger=False,
                    detail={
                        "operator": self._prov_operator(),
                        "applied": applied,
                        "packages": plan["specs"],
                        **(
                            {"log_retention": plan["retention"]}
                            if plan["retention"]
                            else {}
                        ),
                        **(
                            {"scheduler": plan["scheduler"]}
                            if plan["scheduler"]
                            else {}
                        ),
                        **({} if ok else {"error": summary}),
                    },
                    emit=_emit,
                ):
                    res["warnings"].append(
                        "Couldn't record the update in the venv's audit log; "
                        "it's in the console's audit log."
                    )
                self._prov_progress(
                    op=op,
                    kind="update",
                    host=name,
                    phase="done",
                    ok=ok,
                    step=res["step"],
                    summary=summary,
                    warnings=res["warnings"],
                    **({"scheduler": res["scheduler"]} if res.get("scheduler") else {}),
                )
                self._prov_audit(
                    "venv.update.succeeded" if ok else "venv.update.failed",
                    host=name,
                    path=root,
                    applied=applied,
                    **({} if ok else {"step": res["step"], "error": summary}),
                )
                return res

            if cancel.is_set():
                res.update({"summary": "cancelled", "skipped": True})
                return res
            _step("inspect")
            info = self._venv_inspect(t)
            if not info.get("ok"):
                return _done(False, "; ".join(info.get("blockers") or ["refused"]))
            h = self._prov_host(name)
            owner = str(info.get("owner") or "")
            login = str(info.get("user") or "")
            run_as = "" if not owner or owner == login else owner
            self._prov_audit(
                "venv.update.started",
                host=name,
                path=root,
                run_as=run_as,
                packages=plan["specs"],
                inbox=plan["inbox"],
                retention=plan["retention"] or None,
                scheduler=plan["scheduler"],
                scheduler_settings=plan.get("scheduler_settings") or None,
            )
            has_scheduler = bool(info.get("scheduler"))
            if plan["scheduler"] == prov.SCHEDULER_OFF and not has_scheduler:
                res["warnings"].append(
                    "No runspec-scheduler in this venv, so there was no daemon to stop."
                )
            gets_scheduler = has_scheduler or plan["scheduler"] not in (
                "",
                prov.SCHEDULER_OFF,
            )
            if plan.get("scheduler_settings") and not gets_scheduler:
                res["warnings"].append(
                    "No runspec-scheduler in this venv, so the schedule time zone "
                    "and package schedules weren't set."
                )
            steps = prov.update_steps(
                root,
                specs=plan["specs"],
                inbox=plan["inbox"],
                retention=plan["retention"],
                scheduler=plan["scheduler"],
                has_scheduler=has_scheduler,
                acl_user=login if run_as else "",
                daemon_running=bool(info.get("scheduler_pid")),
                check_login=bool(run_as),
                scheduler_settings=plan.get("scheduler_settings") or None,
            )
            off = plan["scheduler"] == prov.SCHEDULER_OFF
            notes = {
                "pip": "pip self-upgrade failed; continued with the venv's pip.",
                "permissions": _permissions_note(root),
                "access": _access_note(login, owner, root),
                "inbox": "Logs inbox setup failed; other accounts' runs fall back "
                "to their own ~/logs.",
                "retention": "Couldn't set the log retention policy (it needs runspec "
                f"{prov.RETENTION_MIN_RUNSPEC} or later in the venv; tick Upgrade "
                "runspec). On the host, as the owner: "
                + prov.retention_hint(plan["retention"] or {}),
                "scheduler": (
                    "Couldn't move the schedules back to crontab, so the daemon was "
                    "left running"
                    if off
                    else "Couldn't switch the venv's schedules to the daemon"
                ),
                "keeper": (
                    "Couldn't remove the daemon's keeper; schedules fire from crontab "
                    "now and the daemon fires nothing"
                    if off
                    else "Couldn't set up the scheduler daemon's keeper"
                ),
                "restart": "Couldn't restart the scheduler daemon, so it still runs "
                "the code it started with (restart needs runspec-scheduler 0.7.0 or "
                "later)",
                "settings": _SETTINGS_NOTE,
                "sync": _SYNC_NOTE,
            }
            version = ""
            skip_keeper = False
            no_daemon = False  # the schedules went back to crontab: nothing to restart
            for st in steps:
                if cancel.is_set():
                    return _done(False, f"cancelled before {st.name}", h, run_as=run_as)
                if st.name == "keeper" and skip_keeper:
                    continue
                if st.name == "restart" and no_daemon:
                    continue
                _step(st.name)
                try:
                    code, out = self._prov_run(
                        h,
                        st.argv,
                        run_as="" if st.as_login else run_as,
                        become_method=become,
                        env=env if st.pip else None,
                        timeout=_PIP_TIMEOUT_S if st.pip else _STEP_TIMEOUT_S,
                    )
                except Exception as exc:  # noqa: BLE001
                    code, out = -1, str(exc)
                _emit(out)
                if code != 0:
                    sched = st.name in _SCHEDULER_STEPS
                    msg = (prov.scheduler_failure if sched else prov.failure_text)(
                        out, code, run_as=run_as, become_method=become
                    )
                    if st.required:
                        return _done(
                            False, f"{st.name} failed: {msg}", h, run_as=run_as
                        )
                    note = notes.get(st.name, f"{st.name} failed")
                    retry = ""
                    if sched:
                        note += f": {msg}."
                        retry = " To retry, on the host: " + prov.host_command(
                            st.argv, run_as, become
                        )
                        logger.warning(
                            "venv update: %s:%s %s step failed (exit %s): %s",
                            name,
                            root,
                            st.name,
                            code,
                            (out or "").strip(),
                        )
                    elif st.name == "retention":
                        note += f" ({msg})"
                    if st.name == "keeper" and not off:
                        rescue = self._keeper_rescue(
                            h,
                            root,
                            plan["scheduler"],
                            run_as=run_as,
                            become=become,
                            emit=_emit,
                        )
                        note += " " + rescue["text"]
                        if rescue["scheduler"]:
                            res["scheduler"] = rescue["scheduler"]
                        if rescue["kept"]:
                            applied.append("keeper")
                        else:
                            no_daemon = True
                        res["warnings"].append(note + retry)
                        res["warnings"].extend(rescue["warnings"])
                        continue
                    skip_keeper = skip_keeper or st.name == "scheduler"
                    res["warnings"].append(note + retry)
                    continue
                if st.name not in ("verify", "access"):
                    applied.append(st.name)
                if st.name == "keeper":
                    kept = prov.parse_keeper(out)
                    if off:
                        res["scheduler"] = {
                            "manager": "crontab",
                            "reason": "Schedules fire from the owner's crontab; "
                            "the daemon and its keepers are gone.",
                        }
                        res["warnings"].extend(
                            f"root has to run: {x}"
                            for x in kept.get("root_steps") or []
                        )
                    else:
                        res["scheduler"] = {
                            "manager": str(kept.get("manager") or plan["scheduler"]),
                            "reason": str(kept.get("reason") or ""),
                        }
                    res["warnings"].extend(str(w) for w in kept.get("warnings") or [])
                if st.name == "restart":
                    res["warnings"].extend(
                        str(w) for w in prov.parse_keeper(out).get("warnings") or []
                    )
                if st.name == "verify":
                    out_lines = (out or "").strip().splitlines()
                    version = out_lines[-1].strip() if out_lines else ""
            res["runspec_version"] = prov.runspec_version(version)
            return _done(True, version or "updated", h, run_as=run_as)

        results: list[dict[str, Any]] = record["results"]
        try:
            for r in self._prov_fanout(_one, plan["targets"]):
                results.append(r)
        finally:
            self._prov_finish(record, results)

    def _keeper_rescue(
        self,
        h: dict[str, Any],
        root: str,
        chosen: str,
        *,
        run_as: str,
        become: str,
        emit: Any,
    ) -> dict[str, Any]:
        """The keeper step failed after the schedules were switched to the
        daemon, so nothing would fire. Try the crontab watchdog (unless that is
        the keeper that failed); if no keeper can be set up, move the schedules
        back to the owner's crontab so they keep firing. Returns ``text`` (what
        happened, for the warning), ``scheduler`` (the result's scheduler
        block, or ``None``), ``warnings`` and ``kept`` (a keeper is set up)."""

        def _run(argv: list[str]) -> tuple[int, str]:
            try:
                code, out = self._prov_run(
                    h,
                    argv,
                    run_as=run_as,
                    become_method=become,
                    timeout=_STEP_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001
                code, out = -1, str(exc)
            emit(out)
            return code, out

        if chosen != "cron":
            code, out = _run(prov.scheduler_keeper_argv(root, "cron"))
            if code == 0:
                kept = prov.parse_keeper(out)
                return {
                    "text": "Set up the crontab watchdog instead; it restarts the "
                    "daemon within 5 minutes if it stops.",
                    "scheduler": {
                        "manager": "cron",
                        "reason": f"The {chosen} keeper couldn't be set up, so a "
                        "crontab watchdog keeps the daemon running.",
                    },
                    "warnings": [str(w) for w in kept.get("warnings") or []],
                    "kept": True,
                }
            logger.warning(
                "venv scheduler: %s cron keeper fallback failed (exit %s): %s",
                root,
                code,
                (out or "").strip(),
            )
        code, out = _run(prov.scheduler_cron_argv(root))
        if code == 0:
            return {
                "text": "No keeper could be set up, so the schedules were moved "
                "back to the owner's crontab and fire from there.",
                "scheduler": {
                    "manager": "crontab",
                    "reason": "No keeper could be set up, so the schedules fire "
                    "from the owner's crontab.",
                },
                "warnings": [],
                "kept": False,
            }
        logger.warning(
            "venv scheduler: %s moving schedules back to crontab failed (exit %s): %s",
            root,
            code,
            (out or "").strip(),
        )
        return {
            "text": "Moving the schedules back to crontab failed too, so none fire "
            "until a keeper is set up.",
            "scheduler": None,
            "warnings": [],
            "kept": False,
        }

    # ── internals: shared ──

    def _prov_operator(self) -> str:
        user_fn = getattr(self, "_current_user", None)
        try:
            return str(user_fn() if callable(user_fn) else "")
        except Exception:  # noqa: BLE001
            return ""

    def _prov_host_audit(
        self,
        h: dict[str, Any],
        dest: str,
        event: str,
        outcome: str,
        *,
        run_as: str,
        become_method: str,
        fallback_logger: bool,
        detail: dict[str, Any],
        emit: Any = None,
    ) -> bool:
        """Record ``event`` on the host: ``runspec audit record`` in the venv as
        its owner (ledger + syslog), else — when ``fallback_logger`` — one
        ``logger`` line to syslog. Best-effort; True when either took it."""
        target = {"path": dest}
        try:
            code, out = self._prov_run(
                h,
                prov.audit_record_argv(
                    dest, event, outcome, target=target, detail=detail
                ),
                run_as=run_as,
                become_method=become_method,
                # Shows as the record's "via" in ``runspec audit list``.
                env={"RUNSPEC_TRIGGER": "runspec-console"},
                timeout=_STEP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            code, out = -1, str(exc)
        if callable(emit):
            emit(out)
        if code == 0:
            return True
        if not fallback_logger:
            return False
        try:
            code, _out = self._prov_run(
                h,
                prov.audit_logger_argv(
                    event,
                    outcome,
                    target=target,
                    detail={**detail, "via": "runspec-console"},
                ),
                timeout=_STEP_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001
            return False
        return code == 0

    def _prov_edit_hosts(self, names: list[str], edit: Any) -> None:
        """Apply ``edit(entry)`` to the named hosts in ``runspec_hosts.toml``
        (re-read fresh, every other key preserved), then save once."""
        path = config_mod.hosts_path()
        with self._lock:
            entries = hosts_mod.load_hosts(path)
            changed = False
            for e in entries:
                if str(e.get("name")) in names and edit(e):
                    changed = True
            if changed:
                hosts_mod.save_hosts(path, entries)
        if changed:
            reload_fn = getattr(self, "_reload_hosts", None)
            if callable(reload_fn):
                reload_fn()
            dispatch = getattr(self, "_dispatch", None)
            if callable(dispatch):
                dispatch("runspec:hosts_updated", {})

    def _prov_finish(
        self, record: dict[str, Any], results: list[dict[str, Any]]
    ) -> None:
        record["status"] = "cancelled" if record["cancel"].is_set() else "finished"
        record["finished_at"] = time.time()
        dispatch = getattr(self, "_dispatch", None)
        if callable(dispatch):
            try:
                dispatch(
                    "runspec:provision_done",
                    {
                        "op": record["op"],
                        "kind": record["kind"],
                        "status": record["status"],
                        "results": [
                            {k: v for k, v in r.items() if k != "lines"}
                            for r in results
                        ],
                    },
                )
            except Exception:
                pass
        kick = getattr(self, "_kick_refresh", None)
        if callable(kick) and any(r.get("ok") for r in results):
            try:
                kick(force=True)
            except Exception:
                pass


def _permissions_note(root: str) -> str:
    return (
        "Couldn't make every file of the venv's code readable by other accounts, "
        "so they may not be able to run it. On the host, as the owner: "
        + prov.permissions_hint(root)
    )


def _access_note(login: str, owner: str, root: str) -> str:
    """The venv runs for its owner but not for the console's SSH login — the
    account the console lists and runs the venv's runnables as."""
    who = login or "the console's SSH login"
    return (
        f"The venv works for {owner or 'its owner'} but {who} can't run it, so the "
        "console won't list its runnables. Usually a strict umask on the owner left "
        "files unreadable; on the host, as the owner: " + prov.permissions_hint(root)
    )


def _to_int(v: Any) -> int:
    try:
        return int(str(v or "0").strip() or 0)
    except ValueError:
        return 0
