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

        ``nextRun`` is the next fire time (ISO-8601) for an enabled schedule,
        derived from its ``when`` via APScheduler. It is **display-only** —
        omitted when APScheduler isn't installed or the schedule is disabled, and
        never written back to the TOML (see :meth:`_read_schedules`)."""
        out = []
        for s in self._read_schedules():
            entry = dict(s)
            if entry.get("enabled", True):
                nxt = self._schedule_next_run(s)
                if nxt:
                    entry["nextRun"] = nxt
            out.append(entry)
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

    def create_schedule(self, data: dict[str, Any]) -> None:
        path = config_mod.hosts_path().parent / "runspec_schedules.toml"
        schedules = self._read_schedules()
        schedules.append(data)
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
        path = config_mod.hosts_path().parent / "runspec_schedules.toml"
        entry = {k: v for k, v in data.items() if k != "nextRun"}
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
        if job.action == "saved_run":
            inv_id = self._invoke_scheduled_saved(job)
            self._record_schedule_fire(job)
            return {
                "action": "saved_run",
                "inv_id": inv_id,
                "runnable": job.saved_run,
                "host": "saved run",
            }
        inv_id = self._invoke_scheduled(
            job.host,
            job.runnable,
            dict(job.args),
            schedule_id=job.id,
            group=job.group,
            hosts=list(job.hosts),
            selected_hosts=list(job.selected_hosts),
            credential_ids=list(job.credential_ids),
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
        elif job.action == "saved_run":
            etype, summary = "saved_run", f"saved run: {job.saved_run}"
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
        credential_ids: list[str] | None = None,
    ) -> None:
        """Run a runnable-action schedule on the timer: fire it directly and
        autonomously — no agent, no confirm gate (scheduling it is the
        authorization). A ``group``-targeted schedule fans the run out across every
        member of the host group (one run card per host); a ``hosts`` candidate list
        runs on the first connected one (failover); a ``selected_hosts`` list fans
        the run out to every host in it. ``credential_ids`` are console credentials
        applied to the run (secrets injected from the keychain at fire time). The
        callback ignores the return; the manual *Run now* path uses
        :meth:`_invoke_scheduled` so it can surface the in-flight id."""
        self._invoke_scheduled(
            host,
            runnable,
            args,
            schedule_id=schedule_id,
            group=group,
            hosts=hosts,
            selected_hosts=selected_hosts,
            credential_ids=credential_ids,
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

    def _run_scheduled_saved(self, job: Any) -> None:
        """Run a saved_run-action schedule on the timer (ScheduleSource's
        ``run_saved`` callback). The manual *Run now* path uses
        :meth:`_invoke_scheduled_saved` directly so it can surface the id."""
        self._invoke_scheduled_saved(job)

    def _invoke_scheduled_saved(self, job: Any) -> str:
        """Fire a saved run from a schedule: load + parse the preset, fill its
        open params from the schedule's ``values``, then run deterministically
        via the shared :meth:`_invoke_fanout` engine — the preset's *own* stored
        targeting decides where it lands (the schedule carries no host). Returns
        the first ``inv_id`` (``""`` on failure). A missing preset or invalid
        values is logged and skipped, mirroring a runbook schedule whose
        runbook can't be rendered."""
        from . import savedruns as savedruns_mod

        entry = self._saved_run_entry(str(job.saved_run or ""))
        if isinstance(entry, str):
            logger.warning("schedule '%s': %s", job.id, entry)
            return ""
        try:
            merged = savedruns_mod.merged_args(entry, dict(job.values or {}))
        except ValueError as exc:
            logger.warning(
                "schedule '%s': saved run %r values invalid — %s",
                job.id,
                job.saved_run,
                exc,
            )
            return ""
        runs = self._invoke_fanout(
            entry.host,
            entry.runnable,
            merged,
            operator="Scheduled Task",
            trigger="schedule",
            label=job.id,
            command_path=entry.command or None,
            group=entry.group,
            hosts=entry.hosts or None,
            selected_hosts=entry.selected_hosts or None,
            credential_ids=entry.credential_ids or None,
        )
        return runs[0][1] if runs else ""

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
                    workspace_prefix="schedule",
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
        credential_ids: list[str] | None = None,
        command_path: list[str] | None = None,
    ) -> str:
        """Invoke a runnable as the scheduler would and return its in-flight
        ``inv_id`` (``""`` on failure). Labelled as the 'Scheduled Task' operator
        and tagged ``trigger="schedule"`` so it reads identically in
        History/Analytics whether the timer or *Run now* started it. Thin wrapper
        over :meth:`_invoke_fanout` (the shared deterministic targeting +
        invoke path), returning the first fan-out member's ``inv_id``."""
        runs = self._invoke_fanout(
            host,
            runnable,
            args,
            operator="Scheduled Task",
            trigger="schedule",
            label=schedule_id,
            command_path=command_path,
            group=group,
            hosts=hosts,
            selected_hosts=selected_hosts,
            credential_ids=credential_ids,
        )
        return runs[0][1] if runs else ""

    def _invoke_fanout(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        *,
        operator: str | None,
        trigger: str,
        label: str = "",
        command_path: list[str] | None = None,
        group: str = "",
        hosts: list[str] | None = None,
        selected_hosts: list[str] | None = None,
        credential_ids: list[str] | None = None,
        schedule_card: bool = True,
    ) -> list[tuple[str, str]]:
        """Resolve a deterministic run's targeting and invoke it, returning
        ``[(host, inv_id)]`` — one pair per host actually run (``[]`` on failure).
        The shared engine behind runnable-action schedules and saved runs: no
        agent, no confirm gate (persisting the schedule / saved run is the
        authorization). Reuses the normal invoke path (SSH/local exec + the
        runnable's own run_summary).

        Targeting: ``group`` fans out across every member of the host group that
        carries the runnable; ``hosts`` is a candidate list and the run goes to
        the first connected one (failover); ``selected_hosts`` fans out to every
        host in it; otherwise ``host`` is the single target. ``credential_ids``
        are console credentials applied to the run (secrets injected from the
        keychain at fire time).

        ``operator``/``trigger`` label the run in History/Analytics; ``label``
        names the source entry (schedule id / saved-run id) on the run card.
        With ``schedule_card`` (default) each per-host run gets a Schedules-tab
        inbox card — a ``running`` card up front, finalised to ``done``/
        ``failed`` from the exit code; a caller whose runs surface elsewhere
        (the Forms saved-run path renders Console run blocks) passes ``False``."""
        if selected_hosts:
            members = [
                h for h in selected_hosts if self._host_has_runnable(h, runnable)
            ]
            if not members:
                logger.warning(
                    "%s: selected_hosts %r has no host carrying %r — nothing run",
                    trigger,
                    selected_hosts,
                    runnable,
                )
                return []
            return [
                run
                for m in members
                for run in self._invoke_fanout(
                    m,
                    runnable,
                    dict(args or {}),
                    operator=operator,
                    trigger=trigger,
                    label=label,
                    command_path=command_path,
                    credential_ids=credential_ids,
                    schedule_card=schedule_card,
                )
            ]
        if hosts:
            chosen = self._pick_connected_host(list(hosts))
            if not chosen:
                logger.warning(
                    "%s: no candidate host in %r — nothing run", trigger, hosts
                )
                return []
            return self._invoke_fanout(
                chosen,
                runnable,
                dict(args or {}),
                operator=operator,
                trigger=trigger,
                label=label,
                command_path=command_path,
                credential_ids=credential_ids,
                schedule_card=schedule_card,
            )
        if group:
            members = [
                m
                for m in self._group_members(group)
                if self._host_has_runnable(m, runnable)
            ]
            if not members:
                logger.warning(
                    "%s: group %r has no host carrying %r — nothing run",
                    trigger,
                    group,
                    runnable,
                )
                return []
            return [
                run
                for m in members
                for run in self._invoke_fanout(
                    m,
                    runnable,
                    dict(args or {}),
                    operator=operator,
                    trigger=trigger,
                    label=label,
                    command_path=command_path,
                    credential_ids=credential_ids,
                    schedule_card=schedule_card,
                )
            ]
        inv_id = uuid.uuid4().hex[:12]
        on_progress = None
        if schedule_card:
            created = time.time()
            # card_id == inv_id so runspec:output / runspec:run_end route to this
            # card.
            card: dict[str, Any] = {
                "card_id": inv_id,
                "trigger": label or runnable,
                "source": "schedule",
                "event_type": "runnable",
                "summary": f"/{runnable} on {host}",
                "column": "running",
                "detail": {
                    "host": host,
                    "runnable": runnable,
                    "args": dict(args or {}),
                    "schedule_id": label,
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
            got = self.invoke_runnable(
                host,
                runnable,
                dict(args or {}),
                command_path=list(command_path) if command_path else None,
                credential_ids=list(credential_ids) if credential_ids else None,
                operator=operator,
                trigger=trigger,
                inv_id=inv_id,
                on_progress=on_progress,
            )
            return [(host, got)] if got else []
        except Exception:
            logger.exception("%s: run of %s on %s failed", trigger, runnable, host)
            if schedule_card:
                now = time.time()
                card["column"] = "failed"
                card["detail"] = {**card["detail"], "error": "failed to start the run"}
                card["timeline"] = [*card["timeline"], {"ts": now, "text": "failed"}]
                self._on_schedule_card(card)
            return []

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
