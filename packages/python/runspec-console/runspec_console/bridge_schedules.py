"""bridge_schedules.py — SchedulesMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import hosts as hosts_mod

from .bridge_const import (
    logger,
)
from .bridge_utils import (
    _write_schedules,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

# Display-only / routing keys that must never be persisted to a console
# schedule's runspec_schedules.toml entry.
_TRANSIENT_KEYS = ("runner", "status", "credential_ids", "nextRun")


class SchedulesMixin:
    def _read_schedules(self) -> list[dict[str, Any]]:
        """The raw ``[[schedule]]`` entries from ``runspec_schedules.toml``.

        Internal/persistence callers use this — it never carries the transient
        ``nextRun`` field that :meth:`get_schedules` attaches for the UI, so a
        round-trip back through ``_write_schedules`` can't accidentally persist
        a computed value.
        """
        path = config_mod.hosts_path().parent / "runspec_schedules.toml"
        if not path.exists():
            return []
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("schedule", [])

    def get_schedules(self) -> list[dict[str, Any]]:
        """The schedules for the Schedules tab, each with a computed ``nextRun``.

        Two kinds are merged: this operator's own **console** schedules (from
        ``runspec_schedules.toml``, fired by the console's APScheduler) tagged
        ``runner="console"``, and the shared **remote** schedules read live from
        each host's ``runspec-scheduler`` daemon (source of truth = the host)
        tagged ``runner="host"`` with a live ``status``.

        ``nextRun`` is the next fire time (ISO-8601) for an enabled schedule. It
        is **display-only** — never written back to the TOML (see
        :meth:`_read_schedules`)."""
        out = []
        for s in self._read_schedules():
            entry = dict(s)
            entry.setdefault("runner", "console")
            if entry.get("enabled", True):
                nxt = self._schedule_next_run(s)
                if nxt:
                    entry["nextRun"] = nxt
            out.append(entry)
        out.extend(self._remote_schedules())
        return out

    def _schedule_next_run(self, raw: dict[str, Any]) -> str:
        """The next fire time of one schedule as an ISO string, or ``""``.

        Best-effort: any parse/timing error (or a missing APScheduler) yields
        ``""`` so the Schedules tab simply shows ``—`` rather than failing."""
        try:
            from .sources.schedule import next_fire_time, parse_schedule_entry

            job = parse_schedule_entry(raw)
            nxt = next_fire_time(job.when)
            return nxt.isoformat(timespec="seconds") if nxt is not None else ""
        except Exception:
            return ""

    # ── remote (host-run) schedules ─────────────────────────────────────────
    #
    # Remote schedules live on the host in its runspec-scheduler daemon and are
    # SHARED across operators — the host is the source of truth. The console
    # reads them live (``runspec-scheduler list``) and CRUDs against the host by
    # invoking the scheduler's own verbs over SSH (reusing invoke_runnable, so
    # subcommands + credential injection come for free). They are never written
    # to this operator's user-specific runspec_schedules.toml.

    def _strip_transient(self, data: dict[str, Any]) -> dict[str, Any]:
        """Drop display-only / routing keys before persisting a console schedule."""
        return {k: v for k, v in data.items() if k not in _TRANSIENT_KEYS}

    def _scheduler_targets(self) -> list[dict[str, Any]]:
        """Each venv on the fleet with a ``runspec-scheduler`` installed:
        ``{host, group, runspec_path, ssh, identityFile}`` (one per host+venv)."""
        targets: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        with self._lock:
            cache = list(self._runnables_cache)
        for r in cache:
            if r.get("name") != "runspec-scheduler":
                continue
            host, group = r.get("host", ""), r.get("group", "")
            key = (host, group)
            if not host or key in seen:
                continue
            entry = self._host_entry(host)
            if entry is None:
                continue
            rp = next(
                (p for p in utils_mod._paths(entry) if hosts_mod.venv_name(p) == group),
                "",
            )
            if not rp:
                continue
            seen.add(key)
            targets.append(
                {
                    "host": host,
                    "group": group,
                    "runspec_path": rp,
                    "ssh": entry.get("ssh"),
                    "identityFile": entry.get("identityFile"),
                }
            )
        return targets

    def _run_scheduler_list(self, target: dict[str, Any]) -> list[dict[str, Any]]:
        """Run ``runspec-scheduler list`` on one venv; return its schedule dicts.
        Raises on a non-zero exit or unparseable output."""
        bin_dir = Path(target["runspec_path"]).parent
        sched_bin = f"{bin_dir.as_posix()}/runspec-scheduler"
        ssh = target.get("ssh")
        if ssh:
            runner = self._ssh_pool.run if self._ssh_pool is not None else None
            if runner is None:
                from .executor import ssh_run as runner
            code, out, err = runner(
                ssh,
                f"{sched_bin} list",
                identity_file=target.get("identityFile"),
                global_ssh_config=self.get_config().get("ssh"),
            )
        else:
            import subprocess

            proc = subprocess.run([sched_bin, "list"], capture_output=True, text=True)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        if code != 0:
            raise RuntimeError(err or f"runspec-scheduler list exited {code}")
        data = json.loads(out)
        return list(data.get("schedules", []))

    def _refresh_remote_schedules(self) -> None:
        """Read every scheduler venv's schedules into the cache (each refresh
        cycle). An unreachable host keeps its last-known schedules but is flagged
        ``status="unreachable"``."""
        fresh: dict[tuple[str, str], dict[str, Any]] = {}
        for t in self._scheduler_targets():
            key = (t["host"], t["group"])
            try:
                fresh[key] = {"schedules": self._run_scheduler_list(t), "status": "ok"}
            except Exception:
                logger.debug(
                    "schedule: remote list failed for %s/%s",
                    t["host"],
                    t["group"],
                    exc_info=True,
                )
                with self._lock:
                    prev = self._remote_sched_cache.get(key, {})
                fresh[key] = {
                    "schedules": prev.get("schedules", []),
                    "status": "unreachable",
                }
        with self._lock:
            self._remote_sched_cache = fresh

    def _remote_schedules(self) -> list[dict[str, Any]]:
        """Flatten the remote-schedule cache into UI schedule dicts (runner=host)."""
        out: list[dict[str, Any]] = []
        with self._lock:
            cache = {k: dict(v) for k, v in self._remote_sched_cache.items()}
        for (host, group), info in cache.items():
            status = info.get("status", "ok")
            for s in info.get("schedules", []):
                entry = dict(s)
                entry["runner"] = "host"
                entry["host"] = host
                entry["group"] = group
                entry["status"] = status
                if s.get("next_run"):
                    entry["nextRun"] = s["next_run"]
                out.append(entry)
        return out

    def _host_has_scheduler(self, host: str, group: str) -> bool:
        """Whether ``host`` has a ``runspec-scheduler`` in the ``group`` venv."""
        with self._lock:
            return any(
                r.get("name") == "runspec-scheduler"
                and r.get("host") == host
                and r.get("group") == group
                for r in self._runnables_cache
            )

    def _remote_schedule_lookup(self, sid: str) -> tuple[str, str] | None:
        """``(host, group)`` for a remote schedule id, or ``None``."""
        with self._lock:
            cache = {k: dict(v) for k, v in self._remote_sched_cache.items()}
        for (host, group), info in cache.items():
            if any(s.get("id") == sid for s in info.get("schedules", [])):
                return host, group
        return None

    def _target_group(self, host: str, runnable: str) -> str:
        """The venv group of ``runnable`` on ``host`` (so the scheduler in the same
        venv is chosen for the delegated command)."""
        with self._lock:
            for r in self._runnables_cache:
                if r.get("host") == host and r.get("name") == runnable:
                    return str(r.get("group", ""))
        return ""

    def _cred_var_names(self, credential_ids: list[str] | None) -> list[str]:
        """The env-var names the selected credentials would set (no values)."""
        from . import credentials as cred_mod

        names: list[str] = []
        for cid in credential_ids or []:
            entry = self._credential_entry(cid)
            if not entry:
                continue
            for n in cred_mod.env_var_names(
                str(entry.get("label", "")), str(entry.get("kind", "userpass"))
            ):
                if n not in names:
                    names.append(n)
        return names

    def _invoke_scheduler_sync(
        self,
        host: str,
        group: str,
        command_path: list[str],
        args: dict[str, Any],
        credential_ids: list[str] | None = None,
        timeout: float = 45.0,
    ) -> tuple[int, str]:
        """Run a ``runspec-scheduler`` verb on a host and block for its result.

        Reuses ``invoke_runnable`` (SSH exec + credential injection); an ``Event``
        set from the ``done`` progress event turns the streaming call synchronous
        so the UI gets success/failure inline."""
        done = threading.Event()
        state: dict[str, Any] = {"exit_code": -1, "output": []}

        def on_progress(kind: str, data: dict[str, Any]) -> None:
            if kind == "line":
                state["output"].append(data.get("line", ""))
            elif kind == "done":
                state["exit_code"] = int(data.get("exit_code", -1))
                done.set()

        self.invoke_runnable(
            host,
            "runspec-scheduler",
            dict(args),
            command_path=list(command_path),
            group=group,
            credential_ids=credential_ids,
            operator="Console",
            trigger="schedule-remote",
            on_progress=on_progress,
        )
        if not done.wait(timeout):
            raise RuntimeError("runspec-scheduler command timed out")
        return int(state["exit_code"]), "\n".join(state["output"])

    def _remote_add_args(self, data: dict[str, Any]) -> dict[str, Any]:
        """Build the ``runspec-scheduler add`` arg dict from a schedule payload."""
        when = data.get("when") or {}
        args: dict[str, Any] = {"id": data["id"], "runnable": data["runnable"]}
        if when.get("cron"):
            args["cron"] = when["cron"]
        if when.get("every"):
            args["every"] = when["every"]
        if when.get("timezone"):
            args["timezone"] = when["timezone"]
        if data.get("args"):
            args["args"] = json.dumps(data["args"])
        args["enabled"] = bool(data.get("enabled", True))
        names = self._cred_var_names(data.get("credential_ids"))
        if names:
            args["cred-vars"] = json.dumps(names)
        return args

    def _remote_create(self, data: dict[str, Any]) -> None:
        """Create/replace a shared schedule on the host's scheduler daemon."""
        host = data.get("host", "")
        group = self._target_group(host, data.get("runnable", ""))
        if not host or not group:
            raise ValueError("a remote schedule needs a host that carries the runnable")
        if not self._host_has_scheduler(host, group):
            raise ValueError(
                f"host {host!r} has no runspec-scheduler in the {group!r} venv"
            )
        args = self._remote_add_args(data)
        code, out = self._invoke_scheduler_sync(
            host,
            group,
            ["add"],
            args,
            credential_ids=data.get("credential_ids") or None,
        )
        if code != 0:
            raise RuntimeError(f"remote add failed: {out.strip()[:400]}")
        self._refresh_remote_schedules()
        self._dispatch("runspec:schedules_changed", {})

    def _remote_delete(self, host: str, group: str, sid: str) -> None:
        code, out = self._invoke_scheduler_sync(host, group, ["remove"], {"id": sid})
        if code != 0:
            raise RuntimeError(f"remote remove failed: {out.strip()[:400]}")
        self._refresh_remote_schedules()
        self._dispatch("runspec:schedules_changed", {})

    def create_schedule(self, data: dict[str, Any]) -> None:
        if data.get("runner") == "host":
            self._remote_create(data)
            return
        path = config_mod.hosts_path().parent / "runspec_schedules.toml"
        schedules = self._read_schedules()
        schedules.append(self._strip_transient(data))
        _write_schedules(path, schedules)
        # Rebuild the engine so the new schedule's APScheduler job starts now
        # (mirrors save_trigger / set_trigger_enabled).
        self.restart_triggers()

    def update_schedule(self, id: str, data: dict[str, Any]) -> None:
        """Replace the schedule with ``id`` in place, keeping its id stable.

        Used by the Schedules tab's edit dialog and the inline enable/disable
        toggle. Falls back to appending if no entry matches (so a stale id can't
        silently drop an edit). Restarts the engine so the change takes effect
        immediately — same as create/delete."""
        if data.get("runner") == "host":
            # A shared host schedule: re-add on the host (add replaces by id).
            self._remote_create({**data, "id": id})
            return
        path = config_mod.hosts_path().parent / "runspec_schedules.toml"
        entry = self._strip_transient(data)
        entry["id"] = id
        schedules = self._read_schedules()
        replaced = False
        for i, s in enumerate(schedules):
            if s.get("id") == id:
                schedules[i] = entry
                replaced = True
                break
        if not replaced:
            schedules.append(entry)
        _write_schedules(path, schedules)
        self.restart_triggers()

    def delete_schedule(self, id: str) -> None:
        # A console schedule lives in the local TOML; a remote one on its host.
        local_ids = {s.get("id") for s in self._read_schedules()}
        if id not in local_ids:
            found = self._remote_schedule_lookup(id)
            if found is not None:
                self._remote_delete(found[0], found[1], id)
                return
        path = config_mod.hosts_path().parent / "runspec_schedules.toml"
        schedules = [s for s in self._read_schedules() if s.get("id") != id]
        _write_schedules(path, schedules)
        self.restart_triggers()

    def run_schedule_now(self, id: str) -> dict[str, Any]:
        """Fire a schedule immediately, exactly as its timer would.

        Runnable-action → run the runnable directly (autonomous + audited, the
        same path a timed fire uses), returning its in-flight ``inv_id``.
        Event-action → emit the matchable event into the trigger engine, fanning
        out to ``source="schedule"`` triggers. The ``enabled`` flag — and the
        working-hours rota — are ignored: clicking *Run now* is the explicit
        authorization. The fire is recorded in the Schedules event log either
        way."""
        raw = next((s for s in self._read_schedules() if s.get("id") == id), None)
        if raw is None:
            # Not a console schedule — try a shared host schedule (fire it on the
            # host's daemon via run-now).
            found = self._remote_schedule_lookup(id)
            if found is not None:
                host, group = found
                code, out = self._invoke_scheduler_sync(
                    host, group, ["run-now"], {"id": id}
                )
                payload: dict[str, Any] = {}
                last = out.strip().splitlines()[-1] if out.strip() else ""
                try:
                    payload = json.loads(last) if last else {}
                except Exception:
                    payload = {}
                return {
                    "action": "runnable",
                    "runner": "host",
                    "host": host,
                    "runnable": payload.get("runnable", ""),
                    "exit_code": payload.get("exit_code"),
                }
            raise ValueError(f"no schedule with id {id!r}")
        from .sources.schedule import event_payload, parse_schedule_entry

        job = parse_schedule_entry(raw)
        if job.action == "event":
            from datetime import datetime

            from .events import Event

            engine = self._trigger_engine
            if engine is None:
                raise RuntimeError(
                    "the trigger engine isn't running — can't emit the event right now"
                )
            engine.feed(
                Event.new("schedule", job.event, event_payload(job, datetime.now()))
            )
            self._record_schedule_fire(job)
            return {"action": "event", "event": job.event}
        if job.action in ("prompt", "runbook"):
            inv_id = self._invoke_scheduled_agent(job)
            self._record_schedule_fire(job)
            return {
                "action": job.action,
                "inv_id": inv_id,
                "runnable": job.runbook if job.action == "runbook" else "(prompt)",
                "host": "agent",
            }
        inv_id = self._invoke_scheduled(
            job.host,
            job.runnable,
            dict(job.args),
            schedule_id=job.id,
            group=job.group,
            hosts=list(job.hosts),
            selected_hosts=list(job.selected_hosts),
        )
        self._record_schedule_fire(job)
        if job.group:
            where = f"group:{job.group}"
        elif job.selected_hosts:
            where = f"selected:{','.join(job.selected_hosts)}"
        elif job.hosts:
            where = f"any:{','.join(job.hosts)}"
        else:
            where = job.host
        return {
            "action": "runnable",
            "inv_id": inv_id,
            "host": where,
            "runnable": job.runnable,
        }

    def _record_schedule_fire(self, job: Any) -> None:
        """Record a manual *Run now* fire in the Schedules event log (the timer
        path records inside ScheduleSource; this mirrors it for the explicit fire,
        which deliberately bypasses the rota gate)."""
        if job.action == "event":
            etype, summary = job.event, f"emit {job.event}"
        elif job.action == "prompt":
            etype, summary = "prompt", f"prompt: {job.prompt[:60]}"
        elif job.action == "runbook":
            etype, summary = "runbook", f"runbook: {job.runbook}"
        else:
            etype = job.runnable or "runnable"
            if job.group:
                where = f"group:{job.group}"
            elif job.selected_hosts:
                where = f"selected:{','.join(job.selected_hosts)}"
            elif job.hosts:
                where = f"any:{','.join(job.hosts)}"
            else:
                where = job.host
            summary = f"/{job.runnable} on {where}"
        self._on_schedule_event(
            {
                "id": uuid.uuid4().hex[:12],
                "source": "schedule",
                "type": etype,
                "summary": summary,
                "matched": [job.id],
                "skipped": [],
                "ts": time.time(),
            }
        )

    def _run_scheduled(
        self,
        schedule_id: str,
        host: str,
        runnable: str,
        args: dict[str, Any],
        group: str = "",
        hosts: list[str] | None = None,
        selected_hosts: list[str] | None = None,
    ) -> None:
        """Run a runnable-action schedule on the timer: fire it directly and
        autonomously — no agent, no confirm gate (scheduling it is the
        authorization). A ``group``-targeted schedule fans the run out across every
        member of the host group (one run card per host); a ``hosts`` candidate list
        runs on the first connected one (failover); a ``selected_hosts`` list fans
        the run out to every host in it. The callback ignores the return; the
        manual *Run now* path uses :meth:`_invoke_scheduled` so it can surface the
        in-flight id."""
        self._invoke_scheduled(
            host,
            runnable,
            args,
            schedule_id=schedule_id,
            group=group,
            hosts=hosts,
            selected_hosts=selected_hosts,
        )

    def _pick_connected_host(self, hosts: list[str]) -> str:
        """The first host in ``hosts`` that is currently connected (failover), else
        the first host in the list, else ``""``. A host with no SSH (local) counts
        as connected, mirroring the host-list connectivity default elsewhere."""
        candidates = [h for h in (hosts or []) if h]
        if not candidates:
            return ""
        with self._lock:
            connected = dict(self._connected_cache)
        for h in candidates:
            entry = self._host_entry(h)
            default = entry is not None and not entry.get("ssh")
            if connected.get(h, default):
                return h
        return candidates[0]

    def _run_scheduled_agent(self, job: Any) -> None:
        """Run a prompt/runbook-action schedule on the timer (ScheduleSource's
        ``run_agent`` callback). The manual *Run now* path uses
        :meth:`_invoke_scheduled_agent` directly so it can surface the card id."""
        self._invoke_scheduled_agent(job)

    def _scheduled_agent_message(self, job: Any) -> str:
        """The agent's first user turn for a prompt/runbook schedule, or ``""`` if a
        runbook can't be rendered (missing id, bad values, empty body).

        A ``prompt`` schedule hands its text straight through. A ``runbook`` schedule
        renders the saved runbook (its ``{{placeholders}}`` filled from ``values``)
        the same way the ``run_runbook`` action tool does, so a scheduled runbook and
        an agent-invoked one read identically."""
        if job.action == "prompt":
            return str(job.prompt or "").strip()
        from . import memory, runbookargs

        runbook = memory.load_runbook(str(job.runbook or ""))
        if not runbook:
            logger.warning("schedule '%s': no runbook with id %r", job.id, job.runbook)
            return ""
        vals = dict(job.values or {})
        raw = runbook.get("args") if isinstance(runbook.get("args"), dict) else {}
        errors = runbookargs.validate_runbook_values(raw, vals)
        if errors:
            logger.warning(
                "schedule '%s': runbook %r values invalid — %s",
                job.id,
                job.runbook,
                "; ".join(errors),
            )
            return ""
        body = memory.render_runbook_body(str(runbook.get("steps") or ""), vals)
        if not body.strip():
            return ""
        title = runbook.get("title") or runbook.get("id")
        return f"Runbook '{title}' — follow these steps now using your tools:\n\n{body}"

    def _invoke_scheduled_agent(self, job: Any) -> str:
        """Run a prompt/runbook schedule as a headless **agent turn** and return its
        card id (``""`` on failure). Builds a Schedules-tab run card up front, then
        runs the turn on its own daemon thread — the same isolated, autonomy-gated
        loop a reactive agent-mode trigger uses (``_trigger_run_agent_turn``), so a
        destructive tool the agent reaches for still parks at the confirm gate. The
        turn is non-blocking: the timer thread (or the *Run now* dispatch) returns
        immediately while the agent works."""
        card_id = uuid.uuid4().hex[:12]
        created = time.time()
        if job.action == "prompt":
            summary = f"prompt: {job.prompt[:80]}"
            detail: dict[str, Any] = {"action": "prompt", "prompt": job.prompt}
        else:
            summary = f"runbook: {job.runbook}"
            detail = {
                "action": "runbook",
                "runbook": job.runbook,
                "values": dict(job.values),
            }
        detail["schedule_id"] = job.id
        card: dict[str, Any] = {
            "card_id": card_id,
            "trigger": job.id,
            "source": "schedule",
            "event_type": job.action,
            "summary": summary,
            "column": "triage",
            "detail": detail,
            "created_at": created,
            "timeline": [{"ts": created, "text": "triage"}],
        }
        self._on_schedule_card(card)

        message = self._scheduled_agent_message(job)
        if not message:
            now = time.time()
            card["column"] = "failed"
            card["detail"] = {
                **card["detail"],
                "error": "could not build the agent prompt (empty prompt or "
                "missing/invalid runbook runbook)",
            }
            card["timeline"] = [*card["timeline"], {"ts": now, "text": "failed"}]
            self._on_schedule_card(card)
            return ""

        def record(note: str) -> None:
            card["timeline"] = [*card["timeline"], {"ts": time.time(), "text": note}]
            self._on_schedule_card(card)

        def stage(col: str) -> None:
            card["column"] = col
            self._on_schedule_card(card)

        def work() -> None:
            try:
                summary_text = self._trigger_run_agent_turn(
                    card_id,
                    self._base_system(),
                    message,
                    draft=False,
                    record=record,
                    stage=stage,
                    trigger="schedule",
                )
            except Exception:
                logger.exception("schedule: agent turn failed for %r", job.id)
                now = time.time()
                card["column"] = "failed"
                card["detail"] = {**card["detail"], "error": "the agent turn failed"}
                card["timeline"] = [*card["timeline"], {"ts": now, "text": "failed"}]
                self._on_schedule_card(card)
                return
            now = time.time()
            card["column"] = "done"
            card["detail"] = {**card["detail"], "result": (summary_text or "")[:2000]}
            card["timeline"] = [*card["timeline"], {"ts": now, "text": "done"}]
            self._on_schedule_card(card)

        threading.Thread(target=work, name=f"sched-agent-{job.id}", daemon=True).start()
        return card_id

    def _invoke_scheduled(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        schedule_id: str = "",
        group: str = "",
        hosts: list[str] | None = None,
        selected_hosts: list[str] | None = None,
    ) -> str:
        """Invoke a runnable as the scheduler would and return its in-flight
        ``inv_id`` (``""`` on failure). Labelled as the 'Scheduled Task' operator
        and tagged ``trigger="schedule"`` so it reads identically in
        History/Analytics whether the timer or *Run now* started it. Reuses the
        normal invoke path (SSH/local exec + the runnable's own run_summary), and
        builds a run card for the Schedules-tab inbox from the invoke lifecycle —
        a ``running`` card up front, finalised to ``done``/``failed`` on exit.

        When ``group`` is set the run fans out across every member of the host
        group that carries the runnable — each member gets its own run card (same
        autonomous, pre-authorised path); the first member's ``inv_id`` is
        returned. When ``hosts`` is a candidate list the run goes to a single host —
        the first connected one (failover). When ``selected_hosts`` is an explicit
        list the run fans out to every host in it (same per-host filter and per-
        member recursion as ``group``, but the operator picked the set directly)."""
        if selected_hosts:
            members = [
                h for h in selected_hosts if self._host_has_runnable(h, runnable)
            ]
            if not members:
                logger.warning(
                    "schedule: selected_hosts %r has no host carrying %r — nothing run",
                    selected_hosts,
                    runnable,
                )
                return ""
            inv_ids = [
                self._invoke_scheduled(
                    m, runnable, dict(args or {}), schedule_id=schedule_id
                )
                for m in members
            ]
            return inv_ids[0] if inv_ids else ""
        if hosts:
            chosen = self._pick_connected_host(list(hosts))
            if not chosen:
                logger.warning("schedule: no candidate host in %r — nothing run", hosts)
                return ""
            return self._invoke_scheduled(
                chosen, runnable, dict(args or {}), schedule_id=schedule_id
            )
        if group:
            members = [
                m
                for m in self._group_members(group)
                if self._host_has_runnable(m, runnable)
            ]
            if not members:
                logger.warning(
                    "schedule: group %r has no host carrying %r — nothing run",
                    group,
                    runnable,
                )
                return ""
            inv_ids = [
                self._invoke_scheduled(
                    m, runnable, dict(args or {}), schedule_id=schedule_id
                )
                for m in members
            ]
            return inv_ids[0] if inv_ids else ""
        inv_id = uuid.uuid4().hex[:12]
        created = time.time()
        # card_id == inv_id so runspec:output / runspec:run_end route to this card.
        card: dict[str, Any] = {
            "card_id": inv_id,
            "trigger": schedule_id or runnable,
            "source": "schedule",
            "event_type": "runnable",
            "summary": f"/{runnable} on {host}",
            "column": "running",
            "detail": {
                "host": host,
                "runnable": runnable,
                "args": dict(args or {}),
                "schedule_id": schedule_id,
            },
            "created_at": created,
            "timeline": [{"ts": created, "text": "running"}],
        }
        self._on_schedule_card(card)

        # Accumulate a bounded output tail for the card's detail.result, then
        # finalise the card's column from the exit code when the run ends.
        output: list[str] = []

        def on_progress(kind: str, data: dict[str, Any]) -> None:
            if kind == "line":
                output.append(data.get("line", ""))
                if len(output) > 500:
                    del output[: len(output) - 500]
                return
            # kind == "done"
            ec = int(data.get("exit_code", -1))
            now = time.time()
            tail = "\n".join(output[-200:])
            card["column"] = "done" if ec == 0 else "failed"
            card["detail"] = {**card["detail"], "exit_code": ec, "result": tail}
            if ec != 0:
                card["detail"]["error"] = tail or f"exited with code {ec}"
            card["timeline"] = [
                *card["timeline"],
                {"ts": now, "text": f"{card['column']} (exit {ec})"},
            ]
            self._on_schedule_card(card)

        try:
            return self.invoke_runnable(
                host,
                runnable,
                dict(args or {}),
                operator="Scheduled Task",
                trigger="schedule",
                inv_id=inv_id,
                on_progress=on_progress,
            )
        except Exception:
            logger.exception(
                "schedule: scheduled run of %s on %s failed", runnable, host
            )
            now = time.time()
            card["column"] = "failed"
            card["detail"] = {**card["detail"], "error": "failed to start the run"}
            card["timeline"] = [*card["timeline"], {"ts": now, "text": "failed"}]
            self._on_schedule_card(card)
            return ""

    # ── today digest ──────────────────────────────────────────────────────────

    def get_today(self, host: str, group: str) -> dict[str, Any] | None:
        entry = self._host_entry(host)
        if entry is None:
            return None
        paths = utils_mod._paths(entry)
        rp = next(
            (p for p in paths if hosts_mod.venv_name(p) == group),
            paths[0] if paths else "",
        )
        if not rp:
            return None
        today_file = Path(rp).parent.parent / "runspec_today.json"
        if not today_file.exists():
            return None
        try:
            return json.loads(today_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── config ────────────────────────────────────────────────────────────────
