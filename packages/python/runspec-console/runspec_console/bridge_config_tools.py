"""bridge_config_tools.py — ConfigToolsMixin: agent tools that edit console config.

Four chat-only, confirm-gated built-in tools let the agent manage config that
was previously Settings-UI-only:

  manage_hosts      — the fleet (runspec_hosts.toml)
  manage_groups     — the group-name registry (runspec_groups.toml)
  manage_triggers   — event triggers (runspec_triggers.toml)
  manage_rota       — the working-hours rota
  manage_saved_runs — the saved-run registry (runspec_saved_runs.toml)

Read actions (``list``/``get``) run autonomously; every mutation confirm-gates
(see ``_effective_tool_autonomy``). Handlers are thin: they validate agent
input up front (``hosts.validate_editor_hosts`` / ``triggers.Trigger.from_config``
/ ``triggers.validate_rota``) and then delegate to the same public bridge
methods the Settings UI calls (``save_jump_hosts``, ``save_groups``,
``save_trigger``, ``save_rota``, …), so persistence, engine restarts, and UI
refresh events stay single-pathed.

SECURITY — chat-only, enforced twice. A trigger- or schedule-originated agent
turn must never reach these tools: a turn that can edit triggers could widen
its own ``action_allow`` or mint new triggers (automation rewriting itself).
Layer 1 strips ``_CONFIG_TOOL_NAMES`` from every headless turn's toolset
(``_filter_headless_tools`` in bridge_triggers.py) regardless of the trigger's
``action_allow``. Layer 2 is the origin guard here: ``_run_config_tool``
refuses when the turn's registered run origin names a trigger — covering a
hand-written rule trigger whose ``run`` names one of these tools directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import config as config_mod
from . import groups as groups_mod
from . import hosts as hosts_mod
from . import triggers as triggers_mod
from .bridge_const import logger

if TYPE_CHECKING:
    pass


class ConfigToolsMixin:
    # ── schemas ───────────────────────────────────────────────────────────────

    def _config_tools(self) -> list[dict[str, Any]]:
        """Schemas for the console-config tools. Enums (host names, group names,
        trigger names) are computed best-effort from live config at build time —
        an empty collection just omits the enum, like create_schedule's."""
        host_names: list[str] = []
        group_names: list[str] = []
        trigger_names: list[str] = []
        try:
            host_names = [h["name"] for h in self._hosts if h.get("name")]
        except Exception:
            logger.debug("config tools: host enum unavailable", exc_info=True)
        try:
            registry = groups_mod.load_groups(config_mod.groups_path())
            group_names = sorted(
                {g["name"] for g in registry if g.get("name")}
                | set(self._groups_map().keys())
            )
        except Exception:
            logger.debug("config tools: group enum unavailable", exc_info=True)
        try:
            trigger_names = [
                str(t.get("name")) for t in config_mod.read_triggers() if t.get("name")
            ]
        except Exception:
            logger.debug("config tools: trigger enum unavailable", exc_info=True)

        day_table = {
            "type": "object",
            "properties": {
                "on": {"type": "boolean"},
                "start": {"type": "string", "description": '"HH:MM" local time'},
                "end": {"type": "string", "description": '"HH:MM" local time'},
            },
        }
        manage_hosts = {
            "name": "manage_hosts",
            "description": (
                "List or edit the SSH fleet (runspec_hosts.toml). 'list' returns "
                "every configured host. 'add' needs a unique name (plus hostname/"
                "user for a remote host); 'update' merges only the fields you "
                "provide onto the named host; 'remove' deletes it. Saving "
                "reconnects the fleet and refreshes discovery. Room/helpdesk "
                "settings are not editable here (Settings UI only) and survive "
                "updates untouched."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "update", "remove"],
                    },
                    "name": {
                        "type": "string",
                        "description": "Host name (required for add/update/remove).",
                        **({"enum": host_names} if host_names else {}),
                    },
                    "hostname": {
                        "type": "string",
                        "description": "SSH host/IP. Omit for a local (this-machine) entry.",
                    },
                    "user": {"type": "string", "description": "SSH username."},
                    "port": {
                        "type": "integer",
                        "description": "SSH port (default 22).",
                    },
                    "runspec_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "runspec binaries on the host, one per venv to expose "
                            '(e.g. "/opt/venvs/ops/bin/runspec").'
                        ),
                    },
                    "identityFile": {
                        "type": "string",
                        "description": "Path to a private key for this host (optional).",
                    },
                    "group": {
                        "type": "string",
                        "description": "Targeting group ('' clears it).",
                        **({"enum": group_names} if group_names else {}),
                    },
                    "jump": {
                        "type": "string",
                        "description": (
                            "Jump host (ProxyJump) — reach this host via the "
                            "system ssh client with -J, so ~/.ssh/config legacy "
                            "algorithms apply (e.g. CentOS 6 ssh-rsa/ssh-dss). "
                            "An SSH host/alias; '' clears it."
                        ),
                    },
                    "profiles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Working-set profile tags (optional).",
                    },
                },
                "required": ["action"],
            },
        }
        manage_groups = {
            "name": "manage_groups",
            "description": (
                "List or edit the host-group registry (runspec_groups.toml). "
                "Groups are the fan-out targeting unit; membership itself lives "
                "on each host entry (manage_hosts 'group'). 'remove' also clears "
                "the group from any member hosts; 'rename' re-points members."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "update", "remove", "rename"],
                    },
                    "name": {
                        "type": "string",
                        "description": "Group name (required except for list).",
                        **({"enum": group_names} if group_names else {}),
                    },
                    "description": {
                        "type": "string",
                        "description": "Group description (add/update).",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New name (rename only).",
                    },
                },
                "required": ["action"],
            },
        }
        manage_triggers = {
            "name": "manage_triggers",
            "description": (
                "List or edit event triggers (runspec_triggers.toml). 'list' "
                "returns every trigger plus any config errors. 'save' upserts "
                "one trigger by name — provide the full trigger object; it is "
                "validated before writing and rejected with the reason if "
                "invalid. 'enable'/'disable'/'delete' act on the named trigger. "
                "Changes restart the trigger engine immediately."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "save", "delete", "enable", "disable"],
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Trigger name (required except for list)."
                            + (
                                " Existing: " + ", ".join(trigger_names)
                                if trigger_names
                                else ""
                            )
                        ),
                    },
                    "trigger": {
                        "type": "object",
                        "description": (
                            "For save: the trigger definition. Keys: source "
                            "('outlook'|'notifications'|'chat'), event "
                            "('new_mail'|'reminder'|'toast'|'message'), mode "
                            "('rule'|'agent'), autonomy, rules (agent mode), run "
                            "('host__runnable', rule mode), actions "
                            "([{run, map}] rule mode), action_mode "
                            "('sequential'|'parallel'), runnables (allowlist), "
                            "action_allow (allowed action tools), match "
                            "({field: regex}), map ({arg: template}), reply "
                            "({draft, style}), enabled, post_to_chat, "
                            "ignore_rota, once_per_thread, pre_turn."
                        ),
                    },
                },
                "required": ["action"],
            },
        }
        manage_rota = {
            "name": "manage_rota",
            "description": (
                "Read ('get') or update ('set') the working-hours rota that "
                "gates when triggers and schedules run. 'set' merges the keys "
                "you provide onto the current rota — include a full day table "
                "to change that day. Keys: enabled, source "
                "('console' grid | 'outlook' calendar), shift_marker, "
                "hold_during_meetings, lock_suspend, and mon..sun day tables."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "rota": {
                        "type": "object",
                        "description": "For set: the rota keys to change.",
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "source": {
                                "type": "string",
                                "enum": ["console", "outlook"],
                            },
                            "shift_marker": {"type": "string"},
                            "hold_during_meetings": {"type": "boolean"},
                            "lock_suspend": {"type": "boolean"},
                            **{day: day_table for day in triggers_mod._DAYS},
                        },
                    },
                },
                "required": ["action"],
            },
        }
        saved_run_ids: list[str] = []
        try:
            from . import savedruns as savedruns_mod

            saved_run_ids = [
                str(r.get("id")) for r in savedruns_mod.load_saved_runs() if r.get("id")
            ]
        except Exception:
            logger.debug("config tools: saved-run enum unavailable", exc_info=True)
        manage_saved_runs = {
            "name": "manage_saved_runs",
            "description": (
                "List or edit saved runs (runspec_saved_runs.toml) — deterministic "
                "presets (runnable + target hosts + fixed args + open params) that "
                "the operator, you (run_saved_run), and schedules "
                "(action='saved_run') can fire. 'save' upserts one saved run by "
                "id — provide the full entry; it is validated before writing "
                "(exactly one of host/group/hosts/selected_hosts/profile; 'params' is a "
                "runspec args table for the open parameters)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "save", "delete"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Saved-run id (required except for list).",
                        **({"enum": saved_run_ids} if saved_run_ids else {}),
                    },
                    "saved_run": {
                        "type": "object",
                        "description": (
                            "For save: the full saved-run entry. Keys: id, title, "
                            "description, runnable, command (subcommand path list), "
                            "one of host / group / hosts / selected_hosts / "
                            "profile (a host profile name — fans out across its "
                            "members), args "
                            "(fixed values), params (runspec args table, e.g. "
                            "{reason: {required: true}}), credential_ids."
                        ),
                    },
                },
                "required": ["action"],
            },
        }
        return [
            manage_hosts,
            manage_groups,
            manage_triggers,
            manage_rota,
            manage_saved_runs,
        ]

    # ── dispatch + origin guard ───────────────────────────────────────────────

    def _run_config_tool(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str = ""
    ) -> str:
        """Dispatch a config tool call. Refuses any turn whose registered run
        origin names a trigger (layer 2 of the chat-only guarantee — layer 1
        already strips these tools from headless toolsets)."""
        with self._lock:
            origin = dict(self._run_origin.get(chat_id) or {})
        if origin.get("trigger"):
            return (
                "✗ Config tools are chat-only: a trigger- or schedule-originated "
                "turn may not edit hosts, groups, triggers, saved runs, or the "
                "rota."
            )
        action = str(tool_input.get("action") or "").strip().lower()
        try:
            if tool_name == "manage_hosts":
                return self._config_manage_hosts(action, tool_input)
            if tool_name == "manage_groups":
                return self._config_manage_groups(action, tool_input)
            if tool_name == "manage_triggers":
                return self._config_manage_triggers(action, tool_input)
            if tool_name == "manage_rota":
                return self._config_manage_rota(action, tool_input)
            if tool_name == "manage_saved_runs":
                return self._config_manage_saved_runs(action, tool_input)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("config tool %s failed", tool_name)
            return f"✗ {tool_name} failed: {exc}"
        return f"✗ Unknown config tool '{tool_name}'."

    # ── handlers ──────────────────────────────────────────────────────────────

    def _config_manage_hosts(self, action: str, ti: dict[str, Any]) -> str:
        hosts = self.get_jump_hosts()
        if action == "list":
            return json.dumps(hosts, ensure_ascii=False)
        name = str(ti.get("name") or "").strip()
        if not name:
            return "✗ 'name' is required for add/update/remove."
        idx = next((i for i, h in enumerate(hosts) if str(h.get("name")) == name), None)
        editable = (
            "hostname",
            "user",
            "port",
            "runspec_paths",
            "identityFile",
            "group",
            "jump",
            "profiles",
        )
        if action == "add":
            if idx is not None:
                return f"✗ Host '{name}' already exists — use action='update'."
            entry: dict[str, Any] = {"name": name}
            for key in editable:
                if key in ti:
                    entry[key] = ti[key]
            hosts.append(entry)
        elif action == "update":
            if idx is None:
                return f"✗ No host named '{name}'."
            for key in editable:
                if key in ti:
                    hosts[idx][key] = ti[key]
        elif action == "remove":
            if idx is None:
                return f"✗ No host named '{name}'."
            hosts.pop(idx)
        else:
            return f"✗ Unknown action '{action}'."
        problems = hosts_mod.validate_editor_hosts(hosts)
        if problems:
            return "✗ Not saved:\n" + "\n".join(f"- {p}" for p in problems)
        self.save_jump_hosts(hosts)
        verb = {"add": "Added", "update": "Updated", "remove": "Removed"}[action]
        return f"✓ {verb} host '{name}'. The fleet is reconnecting and re-discovering."

    def _config_manage_groups(self, action: str, ti: dict[str, Any]) -> str:
        registry = self.get_groups()
        if action == "list":
            return json.dumps(
                {
                    "groups": registry,
                    "members": self._groups_map(),
                },
                ensure_ascii=False,
            )
        name = str(ti.get("name") or "").strip()
        if not name:
            return "✗ 'name' is required."
        if action == "rename":
            new = str(ti.get("new_name") or "").strip()
            if not new:
                return "✗ 'new_name' is required for rename."
            try:
                self.rename_group(name, new)
            except ValueError as exc:
                return f"✗ {exc}"
            return f"✓ Renamed group '{name}' to '{new}' (member hosts re-pointed)."
        exists = any(g.get("name") == name for g in registry)
        if action in ("add", "update"):
            if action == "add" and exists:
                return f"✗ Group '{name}' already exists — use action='update'."
            if action == "update" and not exists:
                return f"✗ No group named '{name}'."
            desc = str(ti.get("description") or "").strip()
            updated = [g for g in registry if g.get("name") != name]
            updated.append({"name": name, "description": desc})
            self.save_groups(updated)
            return f"✓ Saved group '{name}'."
        if action == "remove":
            if not exists:
                return f"✗ No group named '{name}'."
            members = hosts_mod.group_members(self._hosts, name)
            self.save_groups([g for g in registry if g.get("name") != name])
            note = (
                f" {len(members)} member host(s) reset to ungrouped." if members else ""
            )
            return f"✓ Removed group '{name}'.{note}"
        return f"✗ Unknown action '{action}'."

    def _config_manage_triggers(self, action: str, ti: dict[str, Any]) -> str:
        if action == "list":
            raw = config_mod.read_triggers()
            _parsed, errors = triggers_mod.load_triggers(
                raw, ai_enabled=getattr(self, "_ai_enabled", True)
            )
            return json.dumps({"triggers": raw, "errors": errors}, ensure_ascii=False)
        name = str(ti.get("name") or "").strip()
        if not name:
            return "✗ 'name' is required."
        if action == "save":
            trigger = ti.get("trigger")
            if not isinstance(trigger, dict):
                return "✗ 'trigger' (object) is required for save."
            clean = self._clean_trigger({**trigger, "name": name}, name)
            try:
                triggers_mod.Trigger.from_config(
                    clean, ai_enabled=getattr(self, "_ai_enabled", True)
                )
            except ValueError as exc:
                return f"✗ Not saved — invalid trigger: {exc}"
            result = self.save_trigger(clean)
            if not result.get("ok"):
                return f"✗ Not saved: {result.get('error') or 'unknown error'}"
            self._dispatch("runspec:triggers_updated", {"name": name})
            return f"✓ Saved trigger '{name}' (engine restarted)."
        if action == "delete":
            if not self.delete_trigger(name).get("ok"):
                return f"✗ No trigger named '{name}'."
            self._dispatch("runspec:triggers_updated", {"name": name})
            return f"✓ Deleted trigger '{name}'."
        if action in ("enable", "disable"):
            enabled = action == "enable"
            if not self.set_trigger_enabled(name, enabled).get("ok"):
                return f"✗ No trigger named '{name}'."
            self._dispatch("runspec:triggers_updated", {"name": name})
            return f"✓ Trigger '{name}' {'enabled' if enabled else 'disabled'}."
        return f"✗ Unknown action '{action}'."

    def _config_manage_rota(self, action: str, ti: dict[str, Any]) -> str:
        current = config_mod.read_rota() or {}
        if action == "get":
            return json.dumps(current, ensure_ascii=False)
        if action != "set":
            return f"✗ Unknown action '{action}'."
        patch = ti.get("rota")
        if not isinstance(patch, dict) or not patch:
            return "✗ 'rota' (object) with at least one key is required for set."
        merged = {**current, **patch}
        problems = triggers_mod.validate_rota(merged)
        if problems:
            return "✗ Not saved:\n" + "\n".join(f"- {p}" for p in problems)
        self.save_rota(merged)
        self._dispatch("runspec:triggers_updated", {"rota": True})
        state = "enabled" if merged.get("enabled") else "disabled"
        return f"✓ Rota saved ({state}). Triggers and chat presence restarted."

    def _config_manage_saved_runs(self, action: str, ti: dict[str, Any]) -> str:
        from . import savedruns as savedruns_mod

        rows = savedruns_mod.load_saved_runs()
        if action == "list":
            return json.dumps({"saved_runs": rows}, ensure_ascii=False)
        sid = str(ti.get("id") or "").strip()
        if action == "save":
            entry = ti.get("saved_run")
            if not isinstance(entry, dict):
                return "✗ 'saved_run' (object) is required for save."
            if sid:
                entry = {**entry, "id": sid}
            try:
                parsed = savedruns_mod.parse_saved_run(entry)
            except ValueError as exc:
                return f"✗ Not saved — invalid saved run: {exc}"
            updated = [r for r in rows if str(r.get("id")) != parsed.id]
            updated.append(entry)
            result = self.save_saved_runs(updated)
            if not result.get("ok"):
                return f"✗ Not saved: {result.get('error') or 'unknown error'}"
            return (
                f"✓ Saved saved run '{parsed.id}' "
                f"(/{parsed.runnable} on {parsed.target_summary()})."
            )
        if not sid:
            return "✗ 'id' is required."
        match = next((r for r in rows if str(r.get("id")) == sid), None)
        if action == "get":
            if match is None:
                return f"✗ No saved run with id '{sid}'."
            return json.dumps(match, ensure_ascii=False)
        if action == "delete":
            if match is None:
                return f"✗ No saved run with id '{sid}'."
            result = self.save_saved_runs([r for r in rows if str(r.get("id")) != sid])
            if not result.get("ok"):
                return f"✗ Not deleted: {result.get('error') or 'unknown error'}"
            return f"✓ Deleted saved run '{sid}'."
        return f"✗ Unknown action '{action}'."
