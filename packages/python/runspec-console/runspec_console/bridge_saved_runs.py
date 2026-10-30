"""bridge_saved_runs.py — SavedRunsMixin: the saved-run registry seam.

A saved run is a deterministic preset (runnable + target + fixed args + open
params) stored in ``runspec_saved_runs.toml`` (synced — see savedruns.py).
This mixin is the bridge surface over it: CRUD for the Settings editor / Forms
tab, and ``run_saved_run`` — the operator execution path, which resolves the
preset's targeting via the shared :meth:`_invoke_fanout` engine (the same one
runnable-action schedules use) and returns one ``(host, inv_id)`` per run so
the UI renders a Console run block per target. The agent's path is separate
(``run_saved_run`` action tool → grouped confirm → per-call exec) — see
``_saved_run_calls`` here and the gate intercepts in bridge_autonomy.py.
"""

from __future__ import annotations

from typing import Any

from . import savedruns as savedruns_mod
from .bridge_const import logger

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class SavedRunsMixin:
    def get_saved_runs(self) -> list[dict[str, Any]]:
        """All saved runs with their ``params`` table parsed to the same
        ``ArgDef`` shape runnable forms use — feeds the Forms-tab lane, the run
        modal, and the Settings editor. ``paramsRaw`` is the stored table for
        faithful editor round-trip; a malformed entry is surfaced via
        ``argsError`` rather than breaking the list."""
        from . import runbookargs

        out: list[dict[str, Any]] = []
        for row in savedruns_mod.load_saved_runs():
            if not row.get("id"):
                continue
            raw = row.get("params") if isinstance(row.get("params"), dict) else {}
            entry: dict[str, Any] = {
                "id": row.get("id"),
                "title": row.get("title", ""),
                "description": row.get("description", ""),
                "runnable": row.get("runnable", ""),
                "command": list(row.get("command", []) or []),
                "host": row.get("host", ""),
                "group": row.get("group", ""),
                "hosts": list(row.get("hosts", []) or []),
                "selected_hosts": list(row.get("selected_hosts", []) or []),
                "profile": row.get("profile", ""),
                "args": dict(row.get("args", {}) or {}),
                "credential_ids": list(row.get("credential_ids", []) or []),
                "paramsRaw": raw,
            }
            try:
                parsed = savedruns_mod.parse_saved_run(row)
                entry["target"] = parsed.target_summary()
                entry["params"] = runbookargs.parse_runbook_args(raw)
            except Exception as exc:
                entry["params"] = []
                entry.setdefault("target", "")
                entry["argsError"] = str(exc)
            out.append(entry)
        return out

    def save_saved_runs(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist the whole saved-runs collection from the Settings editor
        (mirrors save_runbooks). Each entry is validated via
        ``savedruns.parse_saved_run`` up front so a bad one is rejected before
        it lands in the synced file."""
        normalised: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows or []:
            raw_params = r.get("paramsRaw")
            if not isinstance(raw_params, dict):
                raw_params = (
                    r.get("params") if isinstance(r.get("params"), dict) else {}
                )
            data = {
                "id": r.get("id"),
                "title": r.get("title"),
                "description": r.get("description"),
                "runnable": r.get("runnable"),
                "command": r.get("command"),
                "host": r.get("host"),
                "group": r.get("group"),
                "hosts": r.get("hosts"),
                "selected_hosts": r.get("selected_hosts"),
                "profile": r.get("profile"),
                "args": r.get("args"),
                "params": raw_params,
                "credential_ids": r.get("credential_ids"),
            }
            try:
                parsed = savedruns_mod.parse_saved_run(data)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if parsed.id in seen:
                continue
            seen.add(parsed.id)
            entry: dict[str, Any] = {"id": parsed.id, "runnable": parsed.runnable}
            if parsed.title:
                entry["title"] = parsed.title
            if parsed.description:
                entry["description"] = parsed.description
            if parsed.command:
                entry["command"] = parsed.command
            if parsed.host:
                entry["host"] = parsed.host
            if parsed.group:
                entry["group"] = parsed.group
            if parsed.hosts:
                entry["hosts"] = parsed.hosts
            if parsed.selected_hosts:
                entry["selected_hosts"] = parsed.selected_hosts
            if parsed.profile:
                entry["profile"] = parsed.profile
            if parsed.args:
                entry["args"] = parsed.args
            if parsed.params:
                entry["params"] = parsed.params
            if parsed.credential_ids:
                entry["credential_ids"] = parsed.credential_ids
            normalised.append(entry)
        savedruns_mod.save_saved_runs(normalised)
        # Refresh the Forms lane / Schedules picker / agent context live.
        self._dispatch("runspec:saved_runs_changed", {})
        return {"ok": True, "count": len(normalised)}

    def run_saved_run(
        self, id: str, values: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a saved run now (the operator path — Forms tab / run modal).

        Validates ``values`` against the preset's params, merges them over the
        fixed args, then resolves the stored targeting and invokes via the
        shared deterministic engine. Returns ``{id, runnable, runs: [{host,
        inv_id}]}`` — one entry per host actually run, so the UI renders one
        Console run block each — or ``{error}``."""
        entry = self._saved_run_entry(str(id or ""))
        if isinstance(entry, str):
            return {"error": entry}
        try:
            merged = savedruns_mod.merged_args(entry, values)
        except ValueError as exc:
            return {"error": str(exc)}
        selected = self._saved_run_selected_hosts(entry)
        if entry.profile and not selected:
            return {
                "error": f"nothing was run — host profile '{entry.profile}' "
                "has no hosts"
            }
        runs = self._invoke_fanout(
            entry.host,
            entry.runnable,
            merged,
            operator=None,  # invoke_runnable defaults to the current user
            trigger="saved_run",
            label=entry.id,
            command_path=entry.command or None,
            group=entry.group,
            hosts=entry.hosts or None,
            selected_hosts=selected or None,
            credential_ids=entry.credential_ids or None,
            schedule_card=False,
        )
        if not runs:
            return {
                "error": f"nothing was run — no target of saved run '{entry.id}' "
                f"carries '{entry.runnable}' (or the run failed to start)"
            }
        return {
            "id": entry.id,
            "runnable": entry.runnable,
            # What ran, for the run cards' label + args strip.
            "command": list(entry.command or []),
            "args": merged,
            "runs": [{"host": h, "inv_id": inv} for h, inv in runs],
        }

    # ── internals (agent path) ────────────────────────────────────────────────

    def _saved_run_selected_hosts(self, entry: savedruns_mod.SavedRun) -> list[str]:
        """The explicit fan-out list for the deterministic engine: a ``profile``
        resolves to its current members (so the preset follows the profile's
        membership), otherwise the stored ``selected_hosts``."""
        if entry.profile:
            return self._profile_members(entry.profile)
        return list(entry.selected_hosts)

    def _saved_run_entry(self, sid: str):
        """Load + parse a saved run by id → ``SavedRun``, or an error string."""
        raw = savedruns_mod.load_saved_run(sid)
        if raw is None:
            known = [str(r.get("id") or "") for r in savedruns_mod.load_saved_runs()]
            hint = f" Known ids: {', '.join(known)}." if known else ""
            return f"Error: no saved run with id '{sid}'.{hint}"
        try:
            return savedruns_mod.parse_saved_run(raw)
        except ValueError as exc:
            return f"Error: saved run '{sid}' is invalid — {exc}"

    def _saved_run_gate_calls(
        self, tool_input: dict[str, Any]
    ) -> tuple[str, str, list[tuple[str, dict[str, Any]]]] | str:
        """Expand a ``run_saved_run`` tool call for the autonomy gates:
        ``(saved_run_id, runnable, calls)``, or an error string (unknown id,
        invalid values, no eligible target) returned to the model before any
        confirm is registered."""
        ti = dict(tool_input or {})
        entry = self._saved_run_entry(str(ti.get("id") or ""))
        if isinstance(entry, str):
            return entry
        calls, err = self._saved_run_calls(entry, dict(ti.get("values") or {}))
        if err:
            return err
        return entry.id, entry.runnable, calls

    def _saved_run_calls(
        self, entry: savedruns_mod.SavedRun, values: dict[str, Any] | None
    ) -> tuple[list[tuple[str, dict[str, Any]]], str]:
        """Expand a saved run to the ``(host__runnable, args)`` calls the agent's
        grouped-confirm machinery runs (``_group_calls_level`` resolves each
        call's underlying-runnable autonomy; ``_run_calls_parallel`` executes).
        Mirrors ``_group_fanout_calls``: ``command``/``credential_ids`` ride the
        args dict and are popped by ``_run_tool_sync`` before dispatch.

        Returns ``(calls, "")`` or ``([], error)``. Target resolution matches
        the deterministic engine: ``selected_hosts``/``group``/``profile`` fan
        out across members carrying the runnable, ``hosts`` picks the first
        connected one."""
        try:
            merged = savedruns_mod.merged_args(entry, values)
        except ValueError as exc:
            return [], f"Error: {exc}"
        if entry.command:
            merged["command"] = list(entry.command)
        if entry.credential_ids:
            merged["credential_ids"] = list(entry.credential_ids)

        name = entry.runnable
        if entry.selected_hosts:
            targets = [
                h for h in entry.selected_hosts if self._host_has_runnable(h, name)
            ]
            if not targets:
                return [], (
                    f"Error: no host in {', '.join(entry.selected_hosts)} carries "
                    f"'{name}' — nothing to run."
                )
        elif entry.profile:
            targets = [
                m
                for m in self._profile_members(entry.profile)
                if self._host_has_runnable(m, name)
            ]
            if not targets:
                return [], (
                    f"Error: no host in profile '{entry.profile}' carries "
                    f"'{name}' — nothing to run."
                )
        elif entry.hosts:
            chosen = self._pick_connected_host(list(entry.hosts))
            if not chosen:
                return [], f"Error: no candidate host in {', '.join(entry.hosts)}."
            targets = [chosen]
        elif entry.group:
            targets = [
                m
                for m in self._group_members(entry.group)
                if self._host_has_runnable(m, name)
            ]
            if not targets:
                return [], (
                    f"Error: no host in group '{entry.group}' carries '{name}' — "
                    "nothing to run."
                )
        else:
            targets = [entry.host]
        logger.info(
            "saved run '%s': expanding to %d call(s) on %s",
            entry.id,
            len(targets),
            ", ".join(targets),
        )
        return [(f"{h}__{name}", dict(merged)) for h in targets], ""
