"""bridge_schedules.py — SchedulesMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import executor as executor_mod
from . import hosts as hosts_mod

from .bridge_const import (
    logger,
    AI_OFF_MESSAGE,
)
from .sources.schedule import schedule_label
from .bridge_utils import (
    _write_schedules,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


def _refuse_ai_schedule(data: dict[str, Any], ai_enabled: bool) -> None:
    """Reject a prompt/runbook/eval schedule when AI is off (`[console] ai`).

    create/update do no other validation (the UI editors pre-validate), but the
    ``create_schedule`` *action tool* is reachable over the MCP server, so this
    door needs its own lock. Module-level so stub-bound tests need no method."""
    from .sources.schedule import AI_SCHEDULE_ACTIONS

    action = str(data.get("action") or "runnable").strip()
    if action == "skill":
        action = "runbook"
    if not ai_enabled and action in AI_SCHEDULE_ACTIONS:
        raise ValueError(AI_OFF_MESSAGE)


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
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            # A file corrupted by the pre-fix hand-rolled serialiser (unescaped
            # backslashes/quotes → "Invalid hex value") would otherwise make
            # every schedule read — and so every create/edit — fail forever.
            # Back it up and start clean so the tab and the create tool recover;
            # the raw file is preserved beside it for manual salvage.
            self._backup_corrupt_schedules(path)
            return []
        return data.get("schedule", [])

    def _backup_corrupt_schedules(self, path: Path) -> None:
        """Move an unparseable runspec_schedules.toml aside so the next write
        doesn't clobber it; best-effort, never raises into the caller."""
        try:
            backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            path.replace(backup)
            logger.error(
                "runspec_schedules.toml was unparseable; backed it up to %s and "
                "starting from empty schedules. Recover any entries from that file.",
                backup,
            )
        except Exception:
            logger.exception(
                "runspec_schedules.toml is unparseable and could not be backed up"
            )

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

            job = parse_schedule_entry(
                raw, ai_enabled=getattr(self, "_ai_enabled", True)
            )
            nxt = next_fire_time(job.when)
            return nxt.isoformat(timespec="seconds") if nxt is not None else ""
        except Exception:
            return ""

    def create_schedule(self, data: dict[str, Any]) -> None:
        _refuse_ai_schedule(data, getattr(self, "_ai_enabled", True))
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
        _refuse_ai_schedule(data, getattr(self, "_ai_enabled", True))
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

        job = parse_schedule_entry(raw, ai_enabled=getattr(self, "_ai_enabled", True))
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
        if job.action == "saved_transfer":
            inv_id = self._invoke_scheduled_transfer(job)
            self._record_schedule_fire(job)
            return {
                "action": "saved_transfer",
                "inv_id": inv_id,
                "runnable": job.saved_transfer,
                "host": "saved transfer",
            }
        if job.action == "eval":
            result = self._run_scheduled_eval(job)
            self._record_schedule_fire(job)
            return {"action": "eval", "eval_fixture": job.eval_fixture, **result}
        inv_id = self._invoke_scheduled(
            job.host,
            job.runnable,
            dict(job.args),
            schedule_id=job.id,
            group=job.group,
            hosts=list(job.hosts),
            selected_hosts=list(job.selected_hosts),
            credential_ids=list(job.credential_ids),
            command_path=list(job.command),
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
        elif job.action == "saved_transfer":
            etype = "saved_transfer"
            summary = f"saved transfer: {job.saved_transfer}"
        elif job.action == "eval":
            etype, summary = "eval", f"eval: {job.eval_fixture}"
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
        command: list[str] | None = None,
    ) -> None:
        """Run a runnable-action schedule on the timer: fire it directly and
        autonomously — no agent, no confirm gate (scheduling it is the
        authorization). A ``group``-targeted schedule fans the run out across every
        member of the host group (one run card per host); a ``hosts`` candidate list
        runs on the first connected one (failover); a ``selected_hosts`` list fans
        the run out to every host in it. ``credential_ids`` are console credentials
        applied to the run (secrets injected from the keychain at fire time);
        ``command`` is the subcommand path to descend into (empty = root). The
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
            command_path=command,
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
        # Defence in depth — parse already refused an agent job when AI is off.
        if not getattr(self, "_ai_enabled", True):
            logger.warning("schedule %r skipped: %s", job.id, AI_OFF_MESSAGE)
            return
        self._invoke_scheduled_agent(job)

    def _run_scheduled_saved(self, job: Any) -> None:
        """Run a saved_run-action schedule on the timer (ScheduleSource's
        ``run_saved`` callback). The manual *Run now* path uses
        :meth:`_invoke_scheduled_saved` directly so it can surface the id."""
        self._invoke_scheduled_saved(job)

    def _run_scheduled_eval(self, job: Any) -> dict[str, Any]:
        """Run an eval-action schedule (ScheduleSource's ``run_eval`` callback):
        drive the named fixture against the live agent via :meth:`run_evals` — a
        dry run that never executes a runnable. The report persists to the machine-
        local store, so it appears in the Evals tab. Returns the run summary (used
        by *Run now*); best-effort — a failure is logged, never raised into the
        scheduler thread."""
        # Defence in depth — parse already refused an agent job when AI is off.
        if not getattr(self, "_ai_enabled", True):
            return {"ok": False, "reason": AI_OFF_MESSAGE}
        # A Schedules-tab run card (and so a Console session chip) like every
        # other scheduled fire — evals never stream, so the card carries just
        # the outcome.
        card_id = uuid.uuid4().hex[:12]
        created = time.time()
        card: dict[str, Any] = {
            "card_id": card_id,
            "trigger": schedule_label(job),
            "source": "schedule",
            "event_type": "eval",
            "summary": f"eval: {job.eval_fixture}",
            "column": "running",
            "detail": {
                "action": "eval",
                "fixture": str(job.eval_fixture or ""),
                "schedule_id": job.id,
            },
            "created_at": created,
            "timeline": [{"ts": created, "text": "running"}],
        }
        self._on_schedule_card(card)

        def finish(column: str, **detail: Any) -> None:
            now = time.time()
            card["column"] = column
            card["detail"] = {**card["detail"], **detail}
            card["timeline"] = [*card["timeline"], {"ts": now, "text": column}]
            self._on_schedule_card(card)

        try:
            res = self.run_evals(  # type: ignore[attr-defined]
                fixture_id=str(job.eval_fixture or ""),
                samples=int(getattr(job, "eval_samples", 1) or 1),
                validate=bool(getattr(job, "eval_validate", False)),
                telemetry=bool(getattr(job, "eval_telemetry", False)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedule: eval fixture %r failed", job.eval_fixture)
            finish("failed", error=str(exc))
            return {"ok": False, "reason": str(exc)}
        s = res.get("summary") or {}
        if res.get("ok"):
            logger.info(
                "schedule: eval %s → %s/%s passed",
                job.eval_fixture,
                s.get("passed"),
                s.get("total"),
            )
            finish("done", result=f"{s.get('passed')}/{s.get('total')} passed")
        else:
            finish("failed", error=str(res.get("error") or "eval run failed"))
        return {"ok": bool(res.get("ok")), "summary": res.get("summary")}

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
        selected = self._saved_run_selected_hosts(entry)
        if entry.profile and not selected:
            logger.warning(
                "schedule '%s': host profile %r has no hosts — nothing run",
                job.id,
                entry.profile,
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
            selected_hosts=selected or None,
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
        body = memory.render_runbook(runbook, vals)
        if not body.strip():
            return ""
        title = runbook.get("title") or runbook.get("id")
        return f"Runbook '{title}' — follow these steps now using your tools:\n\n{body}"

    @staticmethod
    def _scheduled_agent_model(job: Any) -> str:
        """The model for a prompt/runbook schedule's turn: the schedule's own
        ``model``, else (runbook action) the runbook's ``model``, else "" (the
        ``[llm]`` model)."""
        own = str(getattr(job, "model", "") or "").strip()
        if own or getattr(job, "action", "") != "runbook":
            return own
        from . import memory

        runbook = memory.load_runbook(str(getattr(job, "runbook", "") or "")) or {}
        return str(runbook.get("model") or "").strip()

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
            "trigger": schedule_label(job),
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
                    model=self._scheduled_agent_model(job),
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

    def _schedule_title(self, label: str) -> str:
        """The display name for a run card whose ``label`` is a schedule id (or
        a saved-run id): the schedule's name or derived label, else ``label``."""
        if not label:
            return ""
        try:
            entry = next(
                (e for e in self._read_schedules() if str(e.get("id")) == label), None
            )
        except Exception:  # noqa: BLE001 — a label is never worth failing a run
            entry = None
        return schedule_label(entry) if entry else label

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
                "trigger": self._schedule_title(label) or runnable,
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

    # ── shared (host cron) schedules — read-only ──────────────────────────────
    #
    # A host venv's *shared* schedules are managed outside the console by
    # runspec-scheduler's cron backend (docs/design/cron-scheduler.md): crontab
    # lines that run `runspec exec --label sched:<id>`. The console only observes
    # them — `runspec-scheduler status` joins config ⋈ crontab ⋈ the runs index
    # (+ the last run's captured output) and we render that. No credentials, no
    # writes; distinct from the console's own local schedules above.

    _SHARED_SCHED_MIN = "0.3.0"

    def shared_schedules(self, host: str) -> list[dict[str, Any]]:
        """Per venv on ``host``: ``{group, available, ok, error?, status?}`` where
        ``status`` is ``runspec-scheduler status`` output. ``available`` is false
        when the venv has no ``runspec-scheduler`` installed (not an error). Venvs
        are read concurrently, bounded like the other fleet fan-outs."""
        entry = self._host_entry(host)
        if entry is None:
            return []
        venvs = [(hosts_mod.venv_name(rp), rp) for rp in utils_mod._paths(entry) if rp]
        if not venvs:
            return []
        remote = bool(entry.get("ssh"))

        def one(item: tuple[str, str]) -> dict[str, Any]:
            group, rp = item
            try:
                return self._shared_schedules_venv(host, group, rp, remote)
            except Exception as exc:  # noqa: BLE001 — one venv never sinks the rest
                logger.exception("shared schedules: %s/%s failed", host, group)
                return {
                    "group": group,
                    "available": True,
                    "ok": False,
                    "error": str(exc),
                }

        if len(venvs) == 1:
            return [one(venvs[0])]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=min(self._fanout_workers(), len(venvs))
        ) as ex:
            return list(ex.map(one, venvs))

    def shared_schedules_all(self, hosts: list[str]) -> list[dict[str, Any]]:
        """:meth:`shared_schedules` across several hosts in one bounded fan-out —
        the Schedules tab's *All schedules* list over the active profile. The UI
        passes the connected hosts (plus ``local``); every (host, venv) pair is
        read in one pool sized by ``[refresh] workers``, so a large profile never
        nests pools. Each item is a :meth:`shared_schedules` venv dict plus
        ``host``. An unknown host is skipped; one venv failing never sinks the
        rest (it comes back ``ok: False`` and the UI shows a *can't read* row)."""
        pairs: list[tuple[str, str, str, bool]] = []
        seen: set[str] = set()
        for host in hosts or []:
            if not host or host in seen:
                continue
            seen.add(host)
            entry = self._host_entry(host)
            if entry is None:
                continue
            remote = bool(entry.get("ssh"))
            if not remote and sys.platform == "win32":
                # runspec-scheduler needs crontab / systemd / supervisord; a
                # Windows console's own venvs can't run it (their schedules are
                # the console's own, on the This console card).
                continue
            for rp in utils_mod._paths(entry):
                if rp:
                    pairs.append((host, hosts_mod.venv_name(rp), rp, remote))
        if not pairs:
            return []

        def one(item: tuple[str, str, str, bool]) -> dict[str, Any]:
            host, group, rp, remote = item
            try:
                got = self._shared_schedules_venv(host, group, rp, remote)
            except Exception as exc:  # noqa: BLE001 — one venv never sinks the rest
                logger.exception("shared schedules: %s/%s failed", host, group)
                got = {
                    "group": group,
                    "available": True,
                    "ok": False,
                    "error": str(exc),
                }
            return {"host": host, **got}

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=max(1, min(self._fanout_workers(), len(pairs)))
        ) as ex:
            return list(ex.map(one, pairs))

    def _shared_schedules_venv(
        self, host: str, group: str, rp: str, remote: bool
    ) -> dict[str, Any]:
        """``runspec-scheduler status`` for one venv — the sibling of its runspec
        binary — parsed from the last JSON-object line of stdout."""
        if remote:
            binary = str(PurePosixPath(rp).parent / "runspec-scheduler")
        else:
            local = Path(rp)
            binary = str(local.with_name("runspec-scheduler" + local.suffix))
        # runspec-scheduler runs as its venv's owner (core `@owner` +
        # enforce_run_as), so a service-account venv refuses the login user —
        # escalate to the owner the way a runnable run does.
        owner, venv_owner = self._scheduler_owner(host, group, remote)
        if venv_owner == "root":
            # Root venvs get no shared schedules (every fire would run as root,
            # and the console never escalates to root to manage the daemon);
            # their runnables are scheduled from the console instead.
            return {
                "group": group,
                "available": False,
                "ok": True,
                "owner": venv_owner,
                "root_owned": True,
            }
        if owner:
            code, stdout, stderr = self._run_scheduler_as(
                host, binary, ["status"], owner, remote
            )
        else:
            code, stdout, stderr = self._run_logs(host, binary, ["status"], timeout=45)
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "schedules" in payload:
                return {
                    "group": group,
                    "available": True,
                    "ok": True,
                    "owner": venv_owner,
                    "status": payload,
                }
        err = stdout.strip() or stderr.strip() or f"exit {code}"
        low = err.lower()
        if code in (-1, 127) and ("not found" in low or "no such file" in low):
            return {"group": group, "available": False, "ok": True}
        if (
            "unknown command" in low
        ):  # "Unknown command for 'runspec-scheduler': 'status'"
            err += (
                f" — this venv's runspec-scheduler predates `status` (needs "
                f">= {self._SHARED_SCHED_MIN}); upgrade it to see its schedules here."
            )
        elif owner and "sudo" in low and "password" in low:
            err = (
                f"runspec-scheduler runs as this venv's owner {owner!r}, and "
                f"passwordless sudo to {owner!r} was refused — the same grant "
                f"running this venv's runnables needs. ({err})"
            )
        return {
            "group": group,
            "available": True,
            "ok": False,
            "owner": venv_owner,
            "error": err,
        }

    def _scheduler_owner(self, host: str, group: str, remote: bool) -> tuple[str, str]:
        """``(run_as, owner)`` for ``runspec-scheduler`` in this venv. ``run_as``
        is the account it must run as: the venv root's owner, ``""`` when that is
        the login user, root (``@owner`` never escalates to root) or unknown —
        then it runs as the login user. ``owner`` is the venv root's owner as
        read (``""`` when unknown), shown in the All schedules list."""
        if not remote and sys.platform == "win32":
            return "", ""
        info = self.venv_owner(host, group)
        owner = str(info.get("owner") or "") if info.get("ok") else ""
        if owner in ("", "root", info.get("self")):
            return "", owner
        return owner, owner

    def _run_scheduler_as(
        self,
        host: str,
        binary: str,
        argv: list[str],
        owner: str,
        remote: bool,
        env: dict[str, str] | None = None,
        timeout: int = 45,
    ) -> tuple[int, str, str]:
        """``<binary> <argv>`` as ``owner``, built like a runnable run
        (``executor.remote_exec_command``): ``sudo -n -u <owner> {venv}/bin/python
        -c <prelude> <binary> …`` with the env frame on stdin, so whatever sudoers
        rule lets the console run this venv's runnables also covers this. A
        missing binary reports ``not found`` with exit 127 before any sudo."""
        from runspec.become import build_exec_argv

        parts, frame = build_exec_argv(
            [binary, *argv],
            owner,
            "sudo",
            "-n",
            env={"RUNSPEC_LABEL": "console", **(env or {})},
        )
        if remote:
            entry = self._host_entry(host)
            if entry is None:
                return -1, "", f"unknown host {host}"
            b = shlex.quote(binary)
            command = (
                f"if [ ! -e {b} ]; then echo {b}: not found >&2; exit 127; fi; "
                + executor_mod.shell_join(parts)
            )
            return self._ssh_run(
                entry["ssh"],
                command,
                identity_file=entry.get("identityFile"),
                timeout=timeout,
                stdin=frame,
            )
        if not Path(binary).exists():
            return 127, "", f"{binary}: not found"
        import subprocess

        try:
            proc = subprocess.run(
                parts, input=frame, capture_output=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, "", str(exc)
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    # ── editing a venv's host schedules (console 0.385.0) ───────────────────────
    #
    # The console runs the venv's own runspec-scheduler as the venv owner —
    # ``add`` / ``remove``, then ``sync`` on a cron venv so the crontab follows
    # (a daemon reloads its config by itself). It never writes the crontab or
    # the scheduler config directly, and a host schedule carries no credentials.

    def host_schedule_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or replace one schedule in a venv's runspec-scheduler config.

        ``payload``: ``host``, ``venv``, ``backend`` (``cron`` | ``daemon``, from
        the status the editor was opened from), ``original_id`` when editing, and
        the schedule fields :func:`host_schedules.save_argv` takes. Renaming (a
        new ``id``) adds the new schedule first, then removes the old one.
        Returns ``{ok, id, error?, warnings, sync?}``."""
        from . import host_schedules as hs

        payload = payload or {}
        backend = "daemon" if payload.get("backend") == "daemon" else "cron"
        try:
            sid, argv, env = hs.save_argv(payload, backend=backend)
        except hs.HostScheduleError as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
        original = str(payload.get("original_id") or "").strip()
        res = self._host_sched_call(payload, argv, env=env)
        if not res["ok"]:
            self._host_sched_audit(
                "schedule.host.save_failed", payload, sid, error=res["error"]
            )
            return {"ok": False, "id": sid, "error": res["error"], "warnings": []}
        warnings: list[str] = []
        if original and original != sid:
            gone = self._host_sched_call(payload, ["remove", "--id", original])
            if not gone["ok"]:
                warnings.append(
                    f"Saved as {sid}, but the old schedule {original} couldn't be removed: "
                    f"{gone['error']}. Remove it from the list, or on the host: "
                    f"runspec-scheduler remove --id {original}"
                )
        out: dict[str, Any] = {"ok": True, "id": sid, "warnings": warnings}
        self._host_sched_sync_after(payload, backend, out)
        self._host_sched_audit(
            "schedule.host.saved",
            payload,
            sid,
            runnable=str(payload.get("runnable") or ""),
            replaced=original if original and original != sid else "",
        )
        return out

    def host_schedule_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove one schedule (``host``, ``venv``, ``backend``, ``id``) — its
        config entry and credential file — then sync a cron venv's crontab."""
        from . import host_schedules as hs

        payload = payload or {}
        try:
            sid = hs.validate_id(payload.get("id"))
        except hs.HostScheduleError as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
        res = self._host_sched_call(payload, ["remove", "--id", sid])
        if not res["ok"]:
            self._host_sched_audit(
                "schedule.host.remove_failed", payload, sid, error=res["error"]
            )
            return {"ok": False, "id": sid, "error": res["error"], "warnings": []}
        out: dict[str, Any] = {"ok": True, "id": sid, "warnings": []}
        if res["json"] and res["json"].get("ok") is False:
            out["warnings"].append(
                f"{sid} wasn't in this venv's config (already removed?)."
            )
        backend = "daemon" if payload.get("backend") == "daemon" else "cron"
        self._host_sched_sync_after(payload, backend, out)
        self._host_sched_audit("schedule.host.removed", payload, sid)
        return out

    def host_schedule_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``runspec-scheduler sync`` for one venv (``host``, ``venv``): the
        crontab is made to match the config. Only this venv's marked lines
        change. Returns ``{ok, error?, sync}``."""
        payload = payload or {}
        out: dict[str, Any] = {"ok": True, "warnings": []}
        self._host_sched_sync_after(payload, "cron", out)
        if out.get("sync_error"):
            out["ok"] = False
            out["error"] = out.pop("sync_error")
        self._host_sched_audit("schedule.host.synced", payload, "", ok=out["ok"])
        return out

    def host_schedule_enable(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn one host schedule on or off (``host``, ``venv``, ``backend``,
        ``id``, ``enabled``) with the venv's ``runspec-scheduler enable`` /
        ``disable`` (0.10.0+), then sync a cron venv's crontab. Unlike saving
        the whole schedule, it writes only ``enabled``, so a schedule a package
        offers keeps following the package's time. Returns ``{ok, id, error?,
        warnings, sync?}``."""
        from . import host_schedules as hs

        payload = payload or {}
        try:
            argv = hs.enable_argv(payload)
        except hs.HostScheduleError as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
        sid = argv[2]
        res = self._host_sched_call(payload, argv)
        if not res["ok"]:
            err = res["error"]
            if hs.too_old(err):
                err = (
                    f"This venv's runspec-scheduler is older than {hs.PACKAGES_MIN}, which "
                    "turns schedules on and off by themselves; upgrade it (Update venv…)."
                )
            self._host_sched_audit(
                "schedule.host.toggle_failed", payload, sid, error=err
            )
            return {"ok": False, "id": sid, "error": err, "warnings": []}
        out: dict[str, Any] = {"ok": True, "id": sid, "warnings": []}
        backend = "daemon" if payload.get("backend") == "daemon" else "cron"
        self._host_sched_sync_after(payload, backend, out)
        self._host_sched_audit(
            "schedule.host.enabled"
            if argv[0] == "enable"
            else "schedule.host.disabled",
            payload,
            sid,
            origin=str((res["json"] or {}).get("origin") or ""),
        )
        return out

    def host_schedule_run_now(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fire one host schedule now (``host``, ``venv``, ``id``) with the venv's
        own ``runspec-scheduler run-now --detach``, run as the venv owner. The fire
        starts in the background as a scheduled fire would run (the schedule's
        ``run_as`` on cron, the owner on the daemon) and is recorded in History
        under ``sched:<id>``. Returns ``{ok, error?, pid?, user?, finished?,
        exit_code?}``."""
        from . import host_schedules as hs

        payload = payload or {}
        try:
            sid = hs.validate_id(payload.get("id"))
        except hs.HostScheduleError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._host_sched_call(
            payload, ["run-now", "--id", sid, "--detach"], timeout=60
        )
        got = res["json"] or {}
        if not res["ok"] or got.get("ok") is False:
            err = str(got.get("error") or "") or self._host_daemon_error(res["error"])
            if hs.too_old(res["error"]) or "--detach" in (res["error"] or ""):
                err = (
                    f"This venv's runspec-scheduler is older than {hs.DAEMON_VERBS_MIN}; "
                    "upgrade it (Update venv…) to run schedules from here."
                )
            self._host_sched_audit(
                "schedule.host.run_now_failed", payload, sid, error=err
            )
            return {"ok": False, "error": err}
        self._host_sched_audit(
            "schedule.host.run_now",
            payload,
            sid,
            pid=got.get("pid"),
            run_as=got.get("user"),
        )
        return {
            "ok": True,
            "id": sid,
            "pid": got.get("pid"),
            "user": got.get("user") or "",
            **(
                {"finished": True, "exit_code": got.get("exit_code")}
                if got.get("finished")
                else {}
            ),
        }

    def host_schedule_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Say who is handling failing host schedules in one venv (``host``,
        ``venv``, ``verb`` = claim / resolve / release, ``runs`` = the failed run
        ids, ``note``). Runs the venv's own ``runspec history <verb>`` as the venv
        owner, so the note lands in the venv's runs index where every console and
        ``runspec history jobs`` on the host see it; the person is recorded as the
        sudo-ing login. A note covers the schedule's failures until it next
        succeeds. Returns ``{ok, error?, notes?}``."""
        from . import host_schedules as hs

        payload = payload or {}
        try:
            argv = hs.note_argv(payload)
        except hs.HostScheduleError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._host_sched_call(payload, argv, program="runspec")
        got = res["json"] or {}
        if not res["ok"] or got.get("ok") is False:
            err = str(got.get("error") or "") or res["error"]
            if hs.notes_too_old(err):
                err = (
                    f"This venv's runspec is older than {hs.NOTES_MIN}, which keeps "
                    "who is handling a failure; upgrade it (Update venv…)."
                )
            return {"ok": False, "error": err}
        for rec in got.get("notes") or []:
            self._host_sched_audit(
                f"schedule.host.{argv[1]}",
                payload,
                str(rec.get("label") or "").removeprefix("sched:"),
                run_id=rec.get("run_id"),
                note=rec.get("note"),
            )
        return {"ok": True, "notes": got.get("notes") or []}

    def host_scheduler_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Change a venv's scheduler settings (``host``, ``venv``, ``backend``,
        and any of ``timeout`` / ``misfire_grace`` / ``catch_up_window`` /
        ``timezone`` / ``package_schedules``) with the venv's own
        ``runspec-scheduler settings``, run as the owner. A running daemon
        reloads them by itself; a cron venv's crontab is synced after a zone or
        package-policy change (both change what cron installs). Returns ``{ok,
        error?, settings?, warnings, sync?}``."""
        from . import host_schedules as hs

        payload = payload or {}
        try:
            argv = hs.settings_argv(payload)
        except hs.HostScheduleError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._host_sched_call(payload, argv)
        keys = [*hs.SETTING_FLAGS, "package_schedules"]
        changed = {
            k: str(payload.get(k)) for k in keys if payload.get(k) not in (None, "")
        }
        if "timezone" in payload and payload["timezone"] is not None:
            changed["timezone"] = str(payload["timezone"]).strip() or "host"
        if not res["ok"]:
            err = self._host_daemon_error(res["error"])
            if "--timezone" in argv or "--package-schedules" in argv:
                if "unknown" in err.lower() and ("timezone" in err or "package" in err):
                    err = (
                        f"This venv's runspec-scheduler is older than {hs.PACKAGES_MIN}, "
                        "which has the venv time zone and package schedules; upgrade it (Update venv…)."
                    )
            self._host_sched_audit(
                "scheduler.host.settings_failed", payload, "", error=err, **changed
            )
            return {"ok": False, "error": err, "warnings": []}
        got = res["json"] or {}
        out: dict[str, Any] = {
            "ok": True,
            "settings": got.get("settings") or {},
            "warnings": [str(w) for w in got.get("warnings") or []],
        }
        if "timezone" in changed or "package_schedules" in changed:
            backend = "daemon" if payload.get("backend") == "daemon" else "cron"
            self._host_sched_sync_after(payload, backend, out)
        self._host_sched_audit("scheduler.host.settings", payload, "", **changed)
        return out

    def host_scheduler_restart(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Restart (or start) a venv's scheduler daemon (``host``, ``venv``)
        through the keeper it has, as the owner — ``runspec-scheduler
        restart``. Fires already running carry on. Returns ``{ok, error?,
        daemon?, warnings, hint?}``."""
        payload = payload or {}
        res = self._host_sched_call(payload, ["restart", "--wait", "15"], timeout=90)
        got = res["json"] or {}
        if not res["ok"]:
            err = self._host_daemon_error(res["error"])
            self._host_sched_audit(
                "scheduler.host.restart_failed", payload, "", error=err
            )
            return {
                "ok": False,
                "error": err,
                "hint": got.get("hint") or "",
                "warnings": [],
            }
        daemon = got.get("daemon") or {}
        self._host_sched_audit(
            "scheduler.host.restarted",
            payload,
            "",
            manager=str(got.get("manager") or ""),
            pid=daemon.get("pid"),
        )
        return {
            "ok": True,
            "daemon": daemon,
            "manager": got.get("manager") or "",
            "warnings": list(got.get("warnings") or []),
        }

    def _host_daemon_error(self, err: str) -> str:
        from . import host_schedules as hs

        if hs.too_old(err):
            return (
                f"This venv's runspec-scheduler is older than {hs.DAEMON_VERBS_MIN}; "
                "upgrade it (Update venv…) to change the daemon from here."
            )
        return err

    def _host_sched_sync_after(
        self, payload: dict[str, Any], backend: str, out: dict[str, Any]
    ) -> None:
        """Run ``sync`` on a cron venv after a change and fold its result into
        ``out`` (``sync`` = installed/removed counts; a failure is a warning,
        since the config change itself already succeeded)."""
        from . import host_schedules as hs

        if backend != "cron":
            return
        res = self._host_sched_call(payload, ["sync"], timeout=120)
        got = res["json"] or {}
        if res["ok"] and got.get("ok", True):
            out["sync"] = {
                "installed": len(got.get("installed") or []),
                "skipped": got.get("skipped") or [],
                "users": got.get("users") or [],
            }
            return
        err = res["error"] or hs.failure(1, res.get("stdout", ""), "")
        out["sync_error"] = err
        out["warnings"].append(
            f"Saved, but the crontab wasn't updated: {err}. "
            "Use Sync crontab once that's fixed."
        )

    def _host_sched_call(
        self,
        payload: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 60,
        program: str = "runspec-scheduler",
    ) -> dict[str, Any]:
        """``<program> <argv>`` (``runspec-scheduler`` by default, or ``runspec``)
        in ``payload``'s venv as its owner. ``{ok, error, json, stdout}`` — ok
        means exit 0."""
        from . import host_schedules as hs

        host = str(payload.get("host") or "")
        venv = str(payload.get("venv") or "")
        entry = self._host_entry(host)
        if entry is None:
            return {
                "ok": False,
                "error": f"unknown host {host}",
                "json": None,
                "stdout": "",
            }
        rp = next(
            (
                p
                for p in utils_mod._paths(entry)
                if p and hosts_mod.venv_name(p) == venv
            ),
            "",
        )
        if not rp:
            return {
                "ok": False,
                "error": f"{host} has no venv {venv}",
                "json": None,
                "stdout": "",
            }
        remote = bool(entry.get("ssh"))
        if not remote and sys.platform == "win32":
            return {
                "ok": False,
                "error": "runspec-scheduler fires with cron or its daemon, which need Linux.",
                "json": None,
                "stdout": "",
            }
        if remote:
            binary = str(PurePosixPath(rp).parent / program)
        else:
            binary = str(Path(rp).with_name(program))
        owner, _ = self._scheduler_owner(host, venv, remote)
        code, stdout, stderr = self._run_scheduler_as(
            host, binary, argv, owner, remote, env=env, timeout=timeout
        )
        got = hs.last_json(stdout)
        if code == 0 and not (got and got.get("error")):
            return {"ok": True, "error": "", "json": got, "stdout": stdout}
        err = hs.failure(code, stdout, stderr)
        low = err.lower()
        if owner and "sudo" in low and "password" in low:
            err = (
                f"{program} runs as this venv's owner {owner!r}, and "
                f"passwordless sudo to {owner!r} was refused. ({err})"
            )
        return {"ok": False, "error": err, "json": got, "stdout": stdout}

    def _host_sched_audit(
        self, event: str, payload: dict[str, Any], sid: str, **fields: Any
    ) -> None:
        append = getattr(self, "_append_tool_audit", None)
        if not callable(append):
            return
        from . import provision as prov

        user_fn = getattr(self, "_current_user", None)
        user = user_fn() if callable(user_fn) else ""
        try:
            append(
                prov.audit_record(
                    event,
                    utils_mod._iso_now(),
                    user=user,
                    host=str(payload.get("host") or ""),
                    venv=str(payload.get("venv") or ""),
                    id=sid,
                    **fields,
                )
            )
        except Exception:  # noqa: BLE001 — audit is best-effort
            logger.exception("host schedule audit failed")

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
