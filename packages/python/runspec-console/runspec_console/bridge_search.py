"""bridge_search.py — SearchMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .bridge_const import _HOT_BUILTIN_NAMES

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class SearchMixin:
    def _search_mode_tools(self) -> list[dict[str, Any]]:
        """The fixed meta-tool set for search mode: discover → run, plus an
        optional full-schema lookup. A constant ~3 entries regardless of fleet
        size — the agent finds runnables on demand instead of carrying every
        runnable's schema in the tool list each turn."""
        return [
            {
                "name": "search_runnables",
                "description": (
                    "Find runnables across the fleet by keyword. Returns matches with "
                    "their id, hosts, description, and typed argument list. Call this "
                    "first, then run_runnable with the id and host. Use an empty query "
                    "(optionally with group/host) to browse."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords matched against runnable name, description, and package/folder.",
                        },
                        "host": {
                            "type": "string",
                            "description": "Optional: only runnables available on this host.",
                        },
                        "group": {
                            "type": "string",
                            "description": "Optional: only runnables from this package/folder.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "run_runnable",
                "description": (
                    "Run a runnable found via search_runnables. Pass its id, the host "
                    "to run on, and arguments by name (per the row's args). Arguments "
                    "are validated before execution; a clear error comes back if "
                    "anything is missing or wrong. The host may instead be a host "
                    "GROUP name (see Console context) to run on every host in that "
                    "group at once, behind a single grouped confirm."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The runnable id from a search_runnables result.",
                        },
                        "host": {
                            "type": "string",
                            "description": (
                                "Which host to run on (from the row's hosts), OR a "
                                "host group name to fan out to every host in that "
                                "group at once."
                            ),
                        },
                        "args": {
                            "type": "object",
                            "description": "Arguments by name, per the row's args list.",
                            "additionalProperties": True,
                        },
                        "command": {
                            "type": "string",
                            "description": "Subcommand to run, if the runnable has one.",
                        },
                        "credential_id": {
                            "type": "string",
                            "description": "Optional saved credential id to apply.",
                        },
                    },
                    "required": ["id", "host"],
                },
            },
            {
                "name": "describe_runnable",
                "description": (
                    "Get the full input schema for one runnable by id — use only when "
                    "the search_runnables row isn't enough to construct the call."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The runnable id from a search_runnables result.",
                        }
                    },
                    "required": ["id"],
                },
            },
        ]

    # ── deferred built-ins (search mode) ──────────────────────────────────────
    #
    # In search mode the chat turn's built-in tools split like the runnables
    # did: a small hot kernel stays inline, and the long tail (Outlook, repo,
    # transfer, action, exec, config tools) leaves the tool list — reached via
    # describe_tool (schema lookup) + use_tool (a wrapper the autonomy gate
    # unwraps to the inner tool BEFORE any decision, so the inner tool's own
    # confirm rules, Console card, and audit record apply unchanged).

    def _deferred_builtin_tools(self) -> list[dict[str, Any]]:
        """The currently *available* built-in tools that are deferred in search
        mode — availability-aware (e.g. no Outlook tools off-Windows, no
        transfer tools without a remote), so use_tool can't name a tool that
        wouldn't have been offered inline."""
        try:
            full = self._custom_agent_tools()
        except Exception:
            return []
        return [t for t in full if t["name"] not in _HOT_BUILTIN_NAMES]

    def _builtin_meta_tools(
        self, deferred: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The two deferral meta-tools, their name enums built from the deferred
        set actually available right now."""
        names = [t["name"] for t in deferred]
        return [
            {
                "name": "describe_tool",
                "description": (
                    "Get the full input schema for one or more built-in console "
                    "tools that are not in the tool list (see the deferred-tools "
                    "menu in the conversation context). Read-only. Call this "
                    "first, then use_tool to run one."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string", "enum": names},
                            "description": "Deferred built-in tool name(s).",
                        }
                    },
                    "required": ["names"],
                },
            },
            {
                "name": "use_tool",
                "description": (
                    "Run a deferred built-in console tool by name, with its "
                    "input as described by describe_tool — call describe_tool "
                    "first; the input is validated against the tool's schema "
                    "and an invalid call is rejected with the valid shape. The "
                    "inner tool's own rules apply exactly as if it were called "
                    "directly — confirm-gated tools still prompt the operator, "
                    "read-only tools run free."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": names,
                            "description": "The deferred built-in tool to run.",
                        },
                        "input": {
                            "type": "object",
                            "additionalProperties": True,
                            "description": (
                                "The tool's input, per its describe_tool schema."
                            ),
                        },
                    },
                    "required": ["name"],
                },
            },
        ]

    def _run_describe_builtin_tool(self, ti: dict[str, Any]) -> str:
        """Execute ``describe_tool`` → full schemas for the requested deferred
        built-ins (JSON). Unknown names come back as per-name errors so a batch
        with one typo still returns the rest."""
        raw = ti.get("names")
        names = (
            [str(n).strip() for n in raw if str(n).strip()]
            if isinstance(raw, list)
            else []
        )
        if not names:
            return (
                "Error: 'names' is required — one or more deferred built-in tool names."
            )
        by_name = {t["name"]: t for t in self._deferred_builtin_tools()}
        out: list[dict[str, Any]] = []
        for n in names:
            t = by_name.get(n)
            if t is None:
                out.append(
                    {
                        "name": n,
                        "error": (
                            "unknown or not deferred — available: "
                            + ", ".join(sorted(by_name))
                        ),
                    }
                )
            else:
                out.append(
                    {
                        "name": n,
                        "description": str(t.get("description") or ""),
                        "input_schema": t.get("input_schema") or {},
                    }
                )
        return json.dumps({"tools": out}, default=str)

    def _unwrap_use_tool(
        self, tool_input: dict[str, Any]
    ) -> tuple[str, dict[str, Any]] | str:
        """Resolve a ``use_tool`` call to its inner ``(name, input)``, or an
        error string the agent can read and retry from. Only currently-available
        deferred built-ins resolve — never a hot-kernel tool (it's already
        inline), a runnable (run_runnable's job), or use_tool itself."""
        ti = dict(tool_input or {})
        name = str(ti.get("name") or "").strip()
        inner = ti.get("input")
        inner_input = dict(inner) if isinstance(inner, dict) else {}
        if not name:
            return "Error: 'name' is required — a deferred built-in tool name (see describe_tool)."
        deferred = {t["name"]: t for t in self._deferred_builtin_tools()}
        tool = deferred.get(name)
        if tool is None:
            return (
                f"Error: '{name}' is not a deferred built-in tool. "
                + "Available: "
                + ", ".join(sorted(deferred))
                + ". (Runnables run via run_runnable; inline tools are called directly.)"
            )
        # Pre-flight the inner input against the tool's real schema — search
        # mode surfaces no per-tool JSON schema up front, so without this a
        # guessed call fails opaquely in the handler (or a wrong field is
        # silently ignored). Mirrors run_runnable's _preflight_validate: the
        # mistake bounces in-turn with the valid shape, before any confirm.
        err = self._preflight_builtin_input(
            name, tool.get("input_schema") or {}, inner_input
        )
        if err:
            return err
        return name, inner_input

    @staticmethod
    def _preflight_builtin_input(
        name: str, schema: dict[str, Any], args: dict[str, Any]
    ) -> str | None:
        """Structural validation of a ``use_tool`` inner input against the
        deferred tool's schema. Returns an error string the agent can read and
        retry from, or ``None`` when the call looks valid.

        Lightweight on purpose (unknown / missing-required / enum, top level
        only — plus enums inside plain string arrays) — the handlers still
        validate semantics. The unknown-key check is skipped when the schema
        opts into open input via ``additionalProperties``."""
        props = schema.get("properties") or {}
        if not isinstance(props, dict) or not props:
            return None  # schemaless tool — nothing to check against
        if not schema.get("additionalProperties"):
            unknown = [k for k in args if k not in props]
            if unknown:
                return (
                    f"Error: unknown argument(s) for {name}: {', '.join(unknown)}. "
                    f"Valid arguments: {', '.join(props)}. "
                    "Call describe_tool for the full schema."
                )
        missing = [k for k in (schema.get("required") or []) if k not in args]
        if missing:
            return (
                f"Error: missing required argument(s) for {name}: "
                f"{', '.join(missing)}. Call describe_tool for the full schema."
            )
        for key, value in args.items():
            spec = props.get(key)
            if not isinstance(spec, dict):
                continue
            enum = spec.get("enum")
            if enum and value not in enum:
                return (
                    f"Error: {name} '{key}' must be one of "
                    f"{', '.join(map(str, enum))} (got {value!r})."
                )
            items = spec.get("items")
            if (
                isinstance(value, list)
                and isinstance(items, dict)
                and items.get("enum")
            ):
                bad = [v for v in value if v not in items["enum"]]
                if bad:
                    return (
                        f"Error: {name} '{key}' contains invalid value(s): "
                        f"{', '.join(map(str, bad))}. Valid: "
                        f"{', '.join(map(str, items['enum']))}."
                    )
        return None

    def _runnables_for_id(self, rid: str) -> list[dict[str, Any]]:
        """Every cached runnable whose dedup id is ``rid`` (one per host that has
        it). Falls back to an explicit ``host__name`` so a caller can address one
        host directly. Empty when nothing matches."""
        with self._lock:
            runnables = list(self._runnables_cache)
        matches = [r for r in runnables if self._dedup_tool_name(r) == rid]
        if matches:
            return matches
        if "__" in rid:
            host, name = rid.split("__", 1)
            return [
                r for r in runnables if r.get("host") == host and r.get("name") == name
            ]
        return []

    def _resolve_id_host(
        self, rid: str, host: str
    ) -> tuple[str, str, dict[str, Any]] | str:
        """Resolve a ``run_runnable``/``describe_runnable`` id (+ chosen host) to
        ``(host, name, cached_runnable)``, or an error string the agent can read
        and retry from. A single-host runnable defaults the host."""
        if not rid:
            return "Error: 'id' is required (use search_runnables to find it)."
        matches = self._runnables_for_id(rid)
        if not matches:
            return (
                f"Error: unknown runnable id '{rid}' (use search_runnables to find it)."
            )
        name = matches[0].get("name", "")
        hosts: list[str] = []
        for r in matches:
            h = r.get("host", "local")
            if h not in hosts:
                hosts.append(h)
        if not host:
            if len(hosts) == 1:
                host = hosts[0]
            else:
                return (
                    f"Error: '{name}' runs on multiple hosts — set 'host'. "
                    f"Options: {', '.join(hosts)}."
                )
        if host not in hosts:
            return (
                f"Error: host '{host}' does not have '{name}'. "
                f"Options: {', '.join(hosts)}."
            )
        cached_r = next(r for r in matches if r.get("host", "local") == host)
        return host, name, cached_r

    def _group_fanout_calls(
        self, tool_input: dict[str, Any]
    ) -> tuple[str, str, list[tuple[str, dict[str, Any]]]] | None:
        """If ``run_runnable``'s ``host`` names a host GROUP (not a literal host),
        expand it to one ``(host__name, args)`` call per group member that carries
        the runnable. Returns ``(group, name, calls)``; ``None`` when ``host`` is
        not a group (the normal single-host path handles it).

        A literal host name wins on a host/group name collision — if a real host
        is named the same as a group, ``host`` addresses that host, not the group.
        Members that lack the runnable are skipped (groups are heterogeneous), so
        ``calls`` may be shorter than the membership — and empty when no member
        carries it."""
        ti = dict(tool_input or {})
        host = str(ti.get("host") or "").strip()
        if not host:
            return None
        members = self._groups_map().get(host)
        if not members:
            return None
        if self._host_entry(host) is not None:
            return None  # host name wins on collision
        matches = self._runnables_for_id(str(ti.get("id") or ""))
        if not matches:
            return None  # let the normal path surface the unknown-id error
        name = matches[0].get("name", "")
        present = {r.get("host", "local") for r in matches}
        base = dict(ti.get("args") or {})
        if ti.get("command"):
            base["command"] = ti["command"]
        if ti.get("credential_id"):
            base["credential_id"] = ti["credential_id"]
        calls = [(f"{h}__{name}", dict(base)) for h in members if h in present]
        return host, name, calls

    @staticmethod
    def _format_group_output(
        group: str,
        name: str,
        calls: list[tuple[str, dict[str, Any]]],
        outputs: list[str],
    ) -> str:
        """Join per-host fan-out outputs into one agent-readable block with a host
        header per section."""
        n = len(calls)
        header = f"Ran '{name}' on group '{group}' ({n} host{'' if n == 1 else 's'}):"
        blocks = [
            f"── {tool_name.split('__', 1)[0]} ──\n{out}"
            for (tool_name, _), out in zip(calls, outputs)
        ]
        return header + "\n\n" + "\n\n".join(blocks)

    def expand_targets(self, run: str) -> list[str]:
        """BridgeOps: expand a trigger/action ``run`` target to concrete
        ``host__runnable`` names. A ``group:<group>__<runnable>`` target expands to
        every group member that has the runnable; a plain ``host__runnable``
        returns itself unchanged. Empty when a named group has no member carrying
        the runnable (the caller surfaces that on the card)."""
        from .triggers import parse_target

        kind, target, runnable = parse_target(run)
        if kind != "group":
            return [run]
        return [
            f"{h}__{runnable}"
            for h in self._group_members(target)
            if self._host_has_runnable(h, runnable)
        ]

    @staticmethod
    def _node_at(
        cached_r: dict[str, Any] | None, command_path: list[str]
    ) -> dict[str, Any]:
        """The spec node reached by descending ``command_path`` from the runnable.
        Its ``args`` already carry inherited globals (discovery merges them)."""
        node = cached_r or {}
        for seg in command_path:
            node = (node.get("commands") or {}).get(seg) or {}
        return node

    def _preflight_validate(
        self,
        cached_r: dict[str, Any] | None,
        command_path: list[str],
        args: dict[str, Any],
    ) -> str | None:
        """Structural validation of ``run_runnable`` args against the cached spec,
        so a bad call fails in-turn instead of after an SSH round-trip. Returns an
        error string, or ``None`` when the call looks valid. Lightweight on
        purpose (required / choice / int+float / range / unknown-arg / command);
        anything subtler still bounces from core on the remote. Unknown spec →
        skip (let the remote validate)."""
        if not cached_r:
            return None
        commands = cached_r.get("commands") or {}
        if commands and not command_path and cached_r.get("requireCommand"):
            return (
                "Error: this runnable requires a command. "
                f"Options: {', '.join(commands.keys())}."
            )
        if command_path:
            seg = command_path[0]
            if seg not in commands:
                return (
                    f"Error: unknown command '{seg}'. "
                    f"Options: {', '.join(commands.keys()) or '(none)'}."
                )
        declared = {
            a["name"]: a
            for a in self._node_at(cached_r, command_path).get("args", [])
            if isinstance(a, dict) and a.get("name")
        }
        unknown = [k for k in args if k not in declared]
        if unknown:
            return (
                f"Error: unknown argument(s): {', '.join(unknown)}. "
                f"Valid arguments: {', '.join(declared) or '(none)'}."
            )
        missing = [
            n for n, a in declared.items() if a.get("required") and n not in args
        ]
        if missing:
            return f"Error: missing required argument(s): {', '.join(missing)}."
        for name, value in args.items():
            arg = declared[name]
            atype = arg.get("type")
            if atype == "choice" and arg.get("options") and value not in arg["options"]:
                return (
                    f"Error: '{name}' must be one of "
                    f"{', '.join(map(str, arg['options']))} (got {value!r})."
                )
            if atype in ("int", "float"):
                try:
                    num: float = int(value) if atype == "int" else float(value)
                except (TypeError, ValueError):
                    kind = "an integer" if atype == "int" else "a number"
                    return f"Error: '{name}' must be {kind} (got {value!r})."
                rng = arg.get("range")
                if (
                    isinstance(rng, (list, tuple))
                    and len(rng) == 2
                    and not (rng[0] <= num <= rng[1])
                ):
                    return (
                        f"Error: '{name}' must be between {rng[0]} and {rng[1]} "
                        f"(got {num})."
                    )
        return None

    def _search_runnables(
        self, query: str, host: str = "", group: str = "", limit: int = 10
    ) -> dict[str, Any]:
        """Rank cached runnables for a search query and build compact, typed rows.

        Collapses per-host copies by ``(group, name)`` like the dedup layout: one
        row per runnable, ``hosts`` is the union, ``id`` is the dedup tool name.
        ``password`` args are omitted (they flow via credentials/env, never inline).
        Ranking is deterministic lexical token overlap over name/description/group;
        an empty query browses (optionally filtered by host/group)."""
        from .catalog import arg_to_property

        with self._lock:
            runnables = list(self._runnables_cache)
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for r in runnables:
            key = (str(r.get("group") or ""), str(r.get("name") or ""))
            g = groups.get(key)
            if g is None:
                g = {"rep": r, "hosts": []}
                groups[key] = g
                order.append(key)
            h = r.get("host", "local")
            if h not in g["hosts"]:
                g["hosts"].append(h)

        tokens = [t for t in re.split(r"\W+", query.lower()) if t]

        def score(rep: dict[str, Any], grp: str) -> int:
            if not tokens:
                return 1  # browse: everything qualifies equally
            name = str(rep.get("name") or "").lower()
            desc = str(rep.get("description") or "").lower()
            grp_l = grp.lower()
            s = 0
            for t in tokens:
                if t in name:
                    s += 10
                if t in grp_l:
                    s += 3
                if t in desc:
                    s += 2
            return s

        scored: list[tuple[int, str, dict[str, Any]]] = []
        for key in order:
            grp, name = key
            rep = groups[key]["rep"]
            hosts = groups[key]["hosts"]
            if host and host not in hosts:
                continue
            if group and group != grp:
                continue
            sc = score(rep, grp)
            if sc <= 0:
                continue
            scored.append((sc, name, {"rep": rep, "hosts": hosts, "group": grp}))
        # Highest score first; stable by name for ties (deterministic output).
        scored.sort(key=lambda t: (-t[0], t[1]))
        truncated = len(scored) > limit
        rows: list[dict[str, Any]] = []
        for _sc, name, info in scored[:limit]:
            rep = info["rep"]
            arg_rows: list[dict[str, Any]] = []
            for a in rep.get("args") or []:
                if str(a.get("type")) == "password":
                    continue  # secrets never travel inline
                row = {"name": a.get("name"), "required": bool(a.get("required"))}
                row.update(arg_to_property(a))  # type, enum, default, min/max, desc
                arg_rows.append(row)
            entry: dict[str, Any] = {
                "id": self._dedup_tool_name(rep),
                "name": name,
                "group": info["group"],
                "hosts": list(info["hosts"]),
                "description": str(rep.get("description") or ""),
                "autonomy": str(rep.get("autonomy") or "confirm"),
                "args": arg_rows,
            }
            commands = rep.get("commands") or {}
            if commands:
                entry["command"] = {
                    "required": bool(rep.get("requireCommand")),
                    "options": list(commands.keys()),
                }
            rows.append(entry)
        result: dict[str, Any] = {"count": len(rows), "results": rows}
        if truncated:
            result["truncated"] = True
            result["hint"] = (
                "More matches exist — refine the query or pass a group/host filter."
            )
        return result

    def _run_search_tool(self, ti: dict[str, Any]) -> str:
        """Execute the ``search_runnables`` meta-tool → JSON string for the agent."""
        rows = self._search_runnables(
            str(ti.get("query") or ""),
            host=str(ti.get("host") or "").strip(),
            group=str(ti.get("group") or "").strip(),
        )
        return json.dumps(rows, default=str)

    def _run_describe_tool(self, ti: dict[str, Any]) -> str:
        """Execute ``describe_runnable`` → the full input schema for one runnable."""
        rid = str(ti.get("id") or "")
        matches = self._runnables_for_id(rid)
        if not matches:
            return (
                f"Error: unknown runnable id '{rid}' (use search_runnables to find it)."
            )
        rep = matches[0]
        name = rep.get("name", "")
        hosts: list[str] = []
        for r in matches:
            h = r.get("host", "local")
            if h not in hosts:
                hosts.append(h)
        schema = self._build_input_schema(
            rep,
            self._union_scoped_credentials(hosts, name),
            hosts=hosts,
            host_key=self._host_selector_key(rep),
        )
        return json.dumps(
            {
                "id": rid,
                "name": name,
                "description": str(rep.get("description") or ""),
                "input_schema": schema,
            },
            default=str,
        )

    # ── always-on catalog index (search mode) ─────────────────────────────────
    #
    # Search mode hides every runnable behind search_runnables, which means the
    # agent can only find a runnable it already thinks to search for — it never
    # sees the menu. The index restores that visibility cheaply: one line per
    # runnable (id + first sentence, no schemas), folded into the conversation so
    # the agent always knows what exists, then uses search/describe for the args.

    @staticmethod
    def _first_sentence(text: str, maxlen: int = 120) -> str:
        """First sentence of a description as a single trimmed line (capped)."""
        s = " ".join(str(text or "").split())
        if not s:
            return ""
        for sep in (". ", "! ", "? "):
            i = s.find(sep)
            if 0 <= i < maxlen:
                return s[: i + 1]
        return s if len(s) <= maxlen else s[: maxlen - 1].rstrip() + "…"

    def _catalog_index_enabled(self) -> bool:
        """``[llm] tool_index`` — on by default (only consulted in search mode)."""
        val = self.get_config().get("llm", {}).get("tool_index", True)
        if isinstance(val, str):
            return val.strip().lower() not in ("false", "0", "no", "off")
        return bool(val)

    def _build_catalog_index(self) -> str:
        """Compact, always-on menu of runnables for search mode: ``id — sentence``,
        one per ``(group, name)``, sorted, bounded by ``[llm] tool_index_limit``
        (default 300) with an overflow note. Empty when disabled or nothing's
        discovered."""
        if not self._catalog_index_enabled():
            return ""
        try:
            limit = int(self.get_config().get("llm", {}).get("tool_index_limit", 300))
        except (TypeError, ValueError):
            limit = 300
        with self._lock:
            runnables = list(self._runnables_cache)
            ready = self._discovery_ready
            done, total = self._discovery_progress
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for r in runnables:
            key = (str(r.get("group") or ""), str(r.get("name") or ""))
            if key not in seen:
                seen[key] = r
                order.append(key)
        lines: list[str] = []
        for key in sorted(order):
            r = seen[key]
            rid = self._dedup_tool_name(r)
            desc = self._first_sentence(r.get("description") or "")
            lines.append(f"- {rid}" + (f" — {desc}" if desc else ""))
        if len(lines) > limit > 0:
            overflow = len(lines) - limit
            lines = lines[:limit]
            lines.append(f"- …and {overflow} more — use search_runnables to find them.")
        # While the first discovery cycle is still running the menu is partial (or
        # empty) — tell the model so it doesn't conclude "no such runnable" from an
        # incomplete fleet and can offer to retry once loading finishes.
        if not ready:
            note = (
                f"(Fleet discovery in progress — {done} of {total} hosts loaded. "
                "More runnables may appear shortly; if you can't find one, say it's "
                "still loading and offer to retry rather than concluding it doesn't exist.)"
            )
            if not lines:
                return note
            lines.append(f"- {note}")
        if not lines:
            return ""
        return "\n".join(lines)

    def _build_builtin_index(self) -> str:
        """Compact menu of the deferred built-ins for search mode: ``name —
        sentence``, one line each, so the agent knows they exist without their
        schemas in the tool list. Empty when built-ins are inline (deferral or
        the index disabled) — the schemas are already visible then."""
        if not self._defer_builtins_enabled() or not self._catalog_index_enabled():
            return ""
        return "\n".join(
            f"- {t['name']}"
            + (
                f" — {self._first_sentence(str(t.get('description') or ''))}"
                if t.get("description")
                else ""
            )
            for t in self._deferred_builtin_tools()
        )

    def _chat_index_text(self) -> str:
        """The full folded menu for a chat turn: the runnable catalog index
        plus, when built-ins are deferred, a labelled deferred-tools section.
        Empty outside search mode. Rides ``_fold_catalog_index`` unchanged —
        the section is part of the index text, not a new fold."""
        if self._tool_mode() != "search":
            return ""
        index = self._build_catalog_index()
        builtin_lines = self._build_builtin_index()
        if builtin_lines:
            block = (
                "Built-in console tools deferred from the tool list (name — "
                "description). Call describe_tool for a tool's input schema, "
                "then use_tool to run it — each tool's own confirm rules still "
                "apply:\n" + builtin_lines
            )
            index = f"{index}\n\n{block}" if index else block
        return index

    def _fold_catalog_index(
        self, history: list[dict[str, Any]], index_text: str
    ) -> list[dict[str, Any]]:
        """Return a copy of ``history`` with the catalog index prepended to the
        first user message — provider-agnostic (every adapter handles user
        messages) and it leaves the cached tools+system prefix untouched. Never
        mutates the stored history."""
        if not index_text:
            return history
        preamble = (
            "Runnables you can execute via run_runnable (id — description). This is "
            "the full menu; call search_runnables or describe_runnable to get a "
            "runnable's arguments and hosts before running it.\n"
            f"{index_text}\n\n"
        )
        out = [dict(m) for m in history]
        for m in out:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                m["content"] = preamble + m["content"]
                break
        return out

    # ── terminal sessions ─────────────────────────────────────────────────────
