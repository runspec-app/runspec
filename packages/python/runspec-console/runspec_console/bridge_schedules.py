"""bridge_schedules.py — SchedulesMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import sys
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
        inv_id = self._invoke_scheduled(
            job.host, job.runnable, dict(job.args), schedule_id=job.id, group=job.group
        )
        self._record_schedule_fire(job)
        return {
            "action": "runnable",
            "inv_id": inv_id,
            "host": job.host or f"group:{job.group}",
            "runnable": job.runnable,
        }

    def _record_schedule_fire(self, job: Any) -> None:
        """Record a manual *Run now* fire in the Schedules event log (the timer
        path records inside ScheduleSource; this mirrors it for the explicit fire,
        which deliberately bypasses the rota gate)."""
        if job.action == "event":
            etype, summary = job.event, f"emit {job.event}"
        else:
            etype = job.runnable or "runnable"
            where = f"group:{job.group}" if job.group else job.host
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
    ) -> None:
        """Run a runnable-action schedule on the timer: fire it directly and
        autonomously — no agent, no confirm gate (scheduling it is the
        authorization). A ``group``-targeted schedule fans the run out across every
        member of the host group (one run card per host). The callback ignores the
        return; the manual *Run now* path uses :meth:`_invoke_scheduled` so it can
        surface the in-flight id."""
        self._invoke_scheduled(
            host, runnable, args, schedule_id=schedule_id, group=group
        )

    def _invoke_scheduled(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        schedule_id: str = "",
        group: str = "",
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
        returned."""
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
