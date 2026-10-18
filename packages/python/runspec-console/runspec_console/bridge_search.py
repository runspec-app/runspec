"""bridge_search.py — SearchMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import bridge_utils as utils_mod
from . import knowledge as kn
from .bridge_const import _HEADLESS_EXCLUDED_NAMES, _HOT_BUILTIN_NAMES

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

logger = logging.getLogger("runspec_console")

# How many runbook rows one search returns. Deliberately smaller than the
# runnable limit (10): runbooks are a curated set an operator wrote by hand, so a
# handful of well-ranked matches is the useful answer — and the folded index
# already lists every one of them by id/title/symptom, so search is the
# narrowing tool, not the only way to see what exists.
_RUNBOOK_SEARCH_LIMIT = 5

# How many per-command arg rows one subcommand runnable may contribute to a
# search result before the row drops the detail and points at describe_runnable
# instead. Search mode exists to keep the tool surface small, so a 37-verb
# runnable must not be allowed to dominate a 10-row result payload; a handful of
# verbs (the common shape — cron, filesystem) fits comfortably under it.
_SEARCH_COMMAND_ARG_BUDGET = 40

# The same all-or-nothing budget for a describe_runnable call that names no
# command. describe is the tool the agent calls *because* the search row was too
# small, so it gets a far larger budget (a 37-verb Jira fits); past it each
# command keeps its description + arg names and the agent asks for one command.
_DESCRIBE_COMMAND_ARG_BUDGET = 300

# The heading under which the search-mode catalogue index rides as a TRAILING
# section of the system prompt. Before 0.262.0 the index was folded into the
# first *user* message; it now composes into the system prompt instead, so a
# gateway that marks only the system block as cacheable actually caches it. In
# search mode the tools block is small, so a system-only cache marker used to
# fall under the model's minimum cacheable prefix and silently cache nothing.
_CATALOG_SYSTEM_HEADING = "What you can reach right now, by category:"


def _debug_property() -> dict[str, Any]:
    """The ``debug`` JSON-schema property (shared with the dedup/per-host tool
    schema)."""
    return {
        "type": "boolean",
        "description": (
            "Optional: run with the runnable's debug logging on (DEBUG lines and "
            "full tracebacks in the output). Set it to re-run a call that failed "
            "unclearly; leave it off otherwise."
        ),
    }


def _deliver_property() -> dict[str, Any]:
    """The ``deliver`` JSON-schema property (shared with the dedup/per-host tool
    schema via ``workspaces.deliver_schema_property``)."""
    from .workspaces import deliver_schema_property

    return deliver_schema_property()


def _wiki_notes_of(
    bridge: Any, names: list[str], *, per_entity: int = kn.NOTES_PER_ENTITY
) -> dict[str, list[str]]:
    """Wiki facts per runnable name (``KnowledgeMixin._wiki_notes``); ``{}`` on a
    bridge without the wiki mixin (test stubs)."""
    fn = getattr(bridge, "_wiki_notes", None)
    return fn(names, per_entity=per_entity) if fn else {}


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
                    "Find runnables across the fleet by keyword. Returns matches under "
                    "'results' with their id, hosts, description, typed argument "
                    "list, any subcommands (with their own args), and any saved "
                    "credentials in scope. A 'runs_as' map ({host: account}) means "
                    "the runnable executes as that OS account on that host (e.g. a "
                    "shared venv's service account) — not as you; its files, "
                    "crontab, home and permissions are that account's. "
                    "Call this first, then run_runnable with the id and host. "
                    "Use an empty query (optionally with group/host) to browse. "
                    "Matching saved runbooks — the operator's standing procedures — "
                    "come back under a separate 'runbooks' key; follow one with "
                    "run_runbook, not run_runnable. Prefer an existing runbook over "
                    "improvising a sequence of runnables."
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
                            "description": "Optional: only runnables available on this host, or on any host in this host group.",
                        },
                        "group": {
                            "type": "string",
                            "description": "Optional: only runnables from this package/venv (e.g. 'linux-utils'). Not a host group — to filter by host group, pass its name as host.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "run_runnable",
                "description": (
                    "Run a runnable found via search_runnables. Pass its id, the host "
                    "to run on, and arguments by name (per the row's args). If the "
                    "row has a 'command' block, also pass 'command' — its args are "
                    "PER COMMAND (the row's flat 'args' are only the inherited "
                    "globals shared by every command). Arguments "
                    "are validated before execution; a clear error comes back if "
                    "anything is missing or wrong. The host may instead be a host "
                    "GROUP name (see Console context) to run on every host in that "
                    "group at once, behind a single grouped confirm. "
                    "'command', 'credential_id', 'deliver' and 'debug' are TOP-LEVEL "
                    "fields — never put any of them inside 'args'."
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
                            "description": (
                                "Optional: force a specific saved credential, by an "
                                "id from the row's 'credentials' list. A top-level "
                                "field — NOT an entry in 'args'. In-scope "
                                "credentials already apply on their own, so set "
                                "this only to override that choice. An unknown id "
                                "is rejected before the run."
                            ),
                        },
                        "deliver": _deliver_property(),
                        "debug": _debug_property(),
                    },
                    "required": ["id", "host"],
                },
            },
            {
                "name": "describe_runnable",
                "description": (
                    "Get one runnable's full interface by id when the "
                    "search_runnables row isn't enough to build the call: its "
                    "hosts, the global args every command accepts, and each "
                    "command's OWN args (type, choices, default, required). Pass "
                    "'command' to get that one command's complete argument list "
                    "(globals included) — do this before running a command whose "
                    "args you haven't seen, instead of guessing or switching to "
                    "another command."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The runnable id from a search_runnables result.",
                        },
                        "command": {
                            "type": "string",
                            "description": (
                                "Optional: one command (space-separated path for a "
                                "nested one, e.g. 'restore full') to describe in "
                                "full. Omit to list every command."
                            ),
                        },
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

    def _deferred_builtin_tools(self, headless: bool = False) -> list[dict[str, Any]]:
        """The currently *available* built-in tools that are deferred in search
        mode — availability-aware (e.g. no Outlook tools off-Windows, no
        transfer tools without a remote), so use_tool can't name a tool that
        wouldn't have been offered inline. ``headless`` (a trigger/schedule
        turn) also leaves out the chat-only tools a headless turn never gets."""
        try:
            full = self._custom_agent_tools()
        except Exception:
            return []
        return [
            t
            for t in full
            if t["name"] not in _HOT_BUILTIN_NAMES
            and not (headless and t["name"] in _HEADLESS_EXCLUDED_NAMES)
        ]

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
                    "and an invalid call is rejected with that schema, so fix "
                    "the input and call again. The "
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
            # Carry the schema in the bounce so the retry needs no
            # describe_tool round trip (an agent that skipped describe_tool
            # used to take three calls: guess, describe, retry).
            schema = json.dumps(
                tool.get("input_schema") or {}, separators=(",", ":"), default=str
            )
            err = err.replace(" Call describe_tool for the full schema.", "")
            return f"{err}\n{name} input schema: {schema}"
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
        it, across every venv it ships in). A legacy group-qualified ``group__name``
        id still resolves — scoped to that venv — and an explicit ``host__name``
        addresses one host directly. Empty when nothing matches. Hidden-from-agent
        runnables are excluded here too, so a hidden tool can't be run/described by
        id."""
        runnables = self._visible_runnables("agent")
        matches = [r for r in runnables if rid in self._dedup_tool_aliases(r)]
        if matches:
            return matches
        if "__" in rid:
            host, name = rid.split("__", 1)
            return [
                r for r in runnables if r.get("host") == host and r.get("name") == name
            ]
        return []

    @staticmethod
    def _coerce_run_args(raw: Any) -> dict[str, Any]:
        """Coerce the search-mode ``run_runnable`` ``args`` field to a dict.

        Its schema is an object, but models sometimes emit it as a JSON *string*
        (``'{"host": "web1"}'``) or, worse, a bare string. A raw ``dict(raw)`` on
        that string raises ``ValueError: dictionary update sequence element #0 has
        length 1; 2 is required`` — which propagated out of the gate, failed the
        trigger card with that cryptic message, and stranded the tool spinner (no
        ``tool_end``). Coerce leniently: a dict passes through, a JSON-string
        object is parsed, anything else becomes ``{}`` so the normal preflight
        surfaces a readable "missing required argument" the agent can retry from."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _runbook_id_hint(rid: str) -> str:
        """If ``rid`` names a saved runbook (bare or ``runbook:``-prefixed), the
        error string pointing at the tool that can actually run it; else ``""``.

        Shared by ``_resolve_id_host`` and ``_run_describe_tool``, which each carry
        their own copy of the unknown-id error. Since ``search_runnables`` now
        returns runbooks, a runbook id reaching either is the likeliest mistake, and
        "unknown runnable id" is a dead end that doesn't hint the right tool."""
        bare = rid.split(":", 1)[1] if rid.startswith("runbook:") else rid
        from . import memory

        try:
            known = {r["id"] for r in memory.list_runbooks()}
        except Exception:  # noqa: BLE001 — the hint is a nicety, never a blocker
            return ""
        if bare not in known:
            return ""
        return (
            f"Error: '{bare}' is a runbook, not a runnable — follow it with "
            f"run_runbook (runbook_id='{bare}'), which returns its steps."
        )

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
            # search_runnables returns runbooks alongside runnables, so the most
            # likely wrong id here is a runbook's. Name the right tool instead of
            # dead-ending on "unknown runnable". (A `runbook:` prefix can never be
            # a real runnable id — _dedup_tool_name's sanitiser rewrites ':'.)
            hint = self._runbook_id_hint(rid)
            if hint:
                return hint
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
            # The runnable isn't in the warm/in-profile cache for this host, but
            # automation reaches any *configured* host on demand (execution
            # lazy-connects — see invoke_runnable). A host that hasn't been
            # discovered yet borrows a warm host's definition; one that *has*
            # been discovered and doesn't carry the runnable errors instead of
            # running a binary that isn't installed there.
            fallback = self._fallback_for_host(host, name, hosts, matches[0])
            if isinstance(fallback, str):
                return fallback
            return host, name, fallback
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
        base = self._coerce_run_args(ti.get("args"))
        if ti.get("command"):
            base["command"] = ti["command"]
        if ti.get("credential_id"):
            base["credential_id"] = ti["credential_id"]
        if ti.get("debug"):
            base["debug"] = True
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

    # The fields run_runnable itself takes; everything else is a runnable
    # argument and belongs inside `args`.
    _RUN_RUNNABLE_FIELDS = frozenset(
        {"id", "host", "args", "command", "credential_id", "deliver", "debug"}
    )

    @classmethod
    def _misplaced_run_fields(cls, ti: dict[str, Any]) -> str | None:
        """An error when a ``run_runnable`` call carries runnable arguments as
        top-level fields (siblings of ``command``) instead of inside ``args``.

        They used to be dropped without a word, so the runnable ran without
        them — e.g. ``link-type`` given beside ``command`` was simply lost. Bounce
        it in-turn so the agent re-sends them where they are read."""
        extra = [k for k in ti if k not in cls._RUN_RUNNABLE_FIELDS]
        if not extra:
            return None
        return (
            f"Error: unexpected top-level field(s): {', '.join(extra)}. "
            "Runnable arguments go INSIDE 'args', e.g. args={"
            + ", ".join(f'"{k}": ...' for k in extra)
            + "}. Only id, host, command, args, credential_id, deliver and debug are "
            "top-level fields."
        )

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

    @staticmethod
    def _command_arg_rows(
        paths: list[tuple[list[str], dict[str, Any]]],
        inherited: set[str],
        budget: int = _SEARCH_COMMAND_ARG_BUDGET,
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        """Per-command arg rows for a subcommand runnable's search row.

        Returns ``({command path: [arg rows]}, over_budget)``. Each command lists
        only the args it adds on top of the runnable's ``inherited`` globals (which
        the row already carries in its flat ``args``), so nothing is repeated once
        per verb. ``password`` args are omitted exactly as they are at the top
        level — secrets never travel inline.

        ``over_budget`` is True when the total row count would exceed ``budget``;
        the caller then emits a describe_runnable pointer instead of the detail.
        Counting happens before anything is returned, so the decision is
        all-or-nothing — a half-listed interface is worse than none, since the
        agent would read the truncation as "this command has no args"."""
        from .catalog import arg_to_property

        out: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for path, node in paths:
            rows: list[dict[str, Any]] = []
            for a in node.get("args") or []:
                if not isinstance(a, dict):
                    continue
                nm = str(a.get("name") or "")
                # Discovery merges inherited globals into every node's args, so
                # skip the ones the flat `args` list already showed.
                if not nm or nm in inherited or str(a.get("type")) == "password":
                    continue
                row = {"name": nm, "required": bool(a.get("required"))}
                row.update(arg_to_property(a))
                rows.append(row)
            if rows:
                total += len(rows)
                if total > budget:
                    return {}, True
                out[" ".join(path)] = rows
        return out, False

    @staticmethod
    def _arg_hosts(records: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Map each top-level arg name to the hosts whose copy declares it.

        A runnable collapsed across hosts can be at different versions on each
        (an older install lacks a newer arg); an arg missing from any host's copy
        is marked ``only_on_hosts`` in the search row so the agent doesn't pass
        it where it will bounce on "unknown argument(s)"."""
        out: dict[str, list[str]] = {}
        for r in records:
            h = str(r.get("host", "local"))
            for a in r.get("args") or []:
                n = str(a.get("name") or "") if isinstance(a, dict) else ""
                if n and h not in out.setdefault(n, []):
                    out[n].append(h)
        return out

    def _runs_as_by_host(self, records: list[dict[str, Any]]) -> dict[str, str]:
        """``{host: account}`` for the hosts where this runnable runs as someone
        other than the account the console logs in as (a shared venv's owner, or
        ``run_as = "root"``). Hosts that run it as the login account are left out."""
        out: dict[str, str] = {}
        for rec in records:
            host = str(rec.get("host") or "local")
            if host in out:
                continue
            try:
                account = self._become_params(rec, host)[0]
            except Exception:  # noqa: BLE001 — a display hint, never fatal
                continue
            if account:
                out[host] = str(account)
        return out

    def _search_runnables(
        self, query: str, host: str = "", group: str = "", limit: int = 10
    ) -> dict[str, Any]:
        """Rank cached runnables for a search query and build compact, typed rows.

        Collapses copies by runnable *name* like the dedup layout — across hosts
        and across venvs/packages, so a cross-platform runnable is one row: one
        row per runnable, ``hosts`` is the union, ``id`` is the dedup tool name.
        ``password`` args are omitted (they flow via credentials/env, never inline).
        Ranking is deterministic lexical token overlap over name/description/group;
        an empty query browses (optionally filtered by host/group)."""
        from .catalog import arg_to_property

        # Row-level credential lists read the cache, which the dedup path refreshes
        # when it builds its enums (_custom_agent_tools). Search mode never calls
        # that, so refresh here or a credential added since start-up is invisible.
        try:
            self._reload_credentials()
        except Exception:  # noqa: BLE001 — a bad store must not sink the search
            logger.debug("credential store unavailable for search", exc_info=True)

        runnables = self._visible_runnables("agent")
        groups: dict[str, dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for r in runnables:
            key = str(r.get("name") or "")
            g = groups.get(key)
            if g is None:
                g = {"rep": r, "hosts": [], "groups": [], "records": []}
                groups[key] = g
                order.append(key)
            g["records"].append(r)
            grp = str(r.get("group") or "")
            if grp and grp not in g["groups"]:
                g["groups"].append(grp)
            h = r.get("host", "local")
            if h not in g["hosts"]:
                g["hosts"].append(h)

        tokens = [t for t in re.split(r"\W+", query.lower()) if t]

        # BM25F ranking (see search_rank): term-frequency saturation, per-field
        # length normalisation and a rarity (idf) weight, over each runnable's
        # name, package, description and args plus the rest of its runspec.toml —
        # subcommand names and descriptions, arg descriptions and choice options,
        # example descriptions — and any team-wiki facts about it, so a query
        # phrased as a task ("add a cron job") finds the runnable whose verb does
        # it. Scores are computed for the whole deduped set in `order`, then the
        # host/group filters select which rows to return.
        from .search_rank import bm25f_scores, runnable_doc

        bm25: list[float] = []
        if tokens:
            wiki = _wiki_notes_of(self, order, per_entity=kn.NOTES_PER_DESCRIBE)
            docs = [
                runnable_doc(
                    groups[key]["rep"],
                    # Every venv/package this runnable ships in, so a query naming
                    # one still matches a runnable collapsed across several.
                    groups=groups[key]["groups"],
                    wiki=wiki.get(key),
                )
                for key in order
            ]
            bm25 = bm25f_scores(query, docs)

        # A host GROUP name is accepted wherever a host is: `host` naming a group
        # (not a literal host) keeps rows available on any member. And `group`
        # naming no package but a host group is the agent's natural mix-up (the
        # two share a word) — treat it as that host group rather than return
        # nothing, which is what sent the agent searching twice.
        package_groups = {g for info in groups.values() for g in info["groups"]}
        host_members: set[str] | None = None
        host_groups = self._search_host_groups()
        if host and host in host_groups and not self._is_literal_host(host):
            host_members = host_groups[host]
            host = ""
        if group and group not in package_groups and group in host_groups:
            host_members = (host_members or set()) | host_groups[group]
            group = ""

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for idx, key in enumerate(order):
            name = key
            grps = groups[key]["groups"]
            grp = grps[0] if grps else ""
            rep = groups[key]["rep"]
            hosts = groups[key]["hosts"]
            if host and host not in hosts:
                continue
            if host_members is not None and not host_members.intersection(hosts):
                continue
            # Membership, not equality: a collapsed row spans every venv the
            # runnable ships in, so filtering by any one of them must find it.
            if group and group not in grps:
                continue
            if tokens:
                sc = bm25[idx]
                if sc <= 0:
                    continue
            else:
                sc = 1.0  # browse: everything qualifies equally
            scored.append(
                (
                    sc,
                    name,
                    {
                        "rep": rep,
                        "hosts": hosts,
                        "group": grp,
                        "records": groups[key]["records"],
                    },
                )
            )
        # Highest score first; stable by name for ties (deterministic output).
        scored.sort(key=lambda t: (-t[0], t[1]))
        truncated = len(scored) > limit
        rows: list[dict[str, Any]] = []
        for _sc, name, info in scored[:limit]:
            rep = info["rep"]
            arg_rows: list[dict[str, Any]] = []
            arg_hosts = self._arg_hosts(info["records"])
            seen_args: set[str] = set()
            # The rep's args first, then any a different host's copy adds — hosts
            # can run different versions of the same package, so the collapsed
            # row must not claim every host takes an arg only some copies know.
            for rec in [rep, *info["records"]]:
                for a in rec.get("args") or []:
                    aname = str(a.get("name") or "")
                    if not aname or aname in seen_args:
                        continue
                    seen_args.add(aname)
                    if str(a.get("type")) == "password":
                        continue  # secrets never travel inline
                    row = {"name": a.get("name"), "required": bool(a.get("required"))}
                    row.update(arg_to_property(a))  # type, enum, default, min/max
                    only = arg_hosts.get(aname, [])
                    if len(only) < len(info["hosts"]):
                        row["only_on_hosts"] = only
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
            # Saved credentials in scope for this runnable on any of its hosts —
            # id + label only, never a secret. Dedup mode puts these in the tool
            # schema as a `credential_id` enum; search mode has no per-runnable
            # schema, so without this the agent can't know a credential exists (or
            # what its id spells) and passes a guessed one — or none at all, on a
            # runnable that needs an out-of-scope pick.
            creds = [
                {"id": str(c.get("id", "")), "label": str(c.get("label", ""))}
                for c in self._union_scoped_credentials(list(info["hosts"]), name)
                if c.get("id")
            ]
            # The account each host's copy runs as, where it isn't the account the
            # console logs in as (a shared venv's owner, or run_as = "root"). The
            # agent otherwise assumes "the current user", e.g. says a cron job
            # lands in the login user's crontab when it goes to the owner's.
            runs_as = self._runs_as_by_host(info["records"])
            if runs_as:
                entry["runs_as"] = runs_as
            if creds:
                entry["credentials"] = creds
                entry["credentials_hint"] = (
                    "These apply automatically. Pass one's id as run_runnable's "
                    "TOP-LEVEL credential_id field only to force a specific one — "
                    "never inside args."
                )
            commands = rep.get("commands") or {}
            if commands:
                # Every runnable path (nested included) as a space-joined string;
                # for a flat runnable these are just the top-level verb names.
                paths = utils_mod.subcommand_paths(rep)
                cmd: dict[str, Any] = {
                    "required": bool(rep.get("requireCommand")),
                    "options": [" ".join(p) for p, _ in paths] or list(commands.keys()),
                    # `args` above are the runnable's inherited globals — valid for
                    # every command; each command adds its OWN args on top. Stated
                    # explicitly because the row's flat `args` list otherwise reads
                    # like the whole interface, and a call built from it alone
                    # bounces on "unknown argument(s)".
                    "args_are_per_command": True,
                }
                per_command, over_budget = self._command_arg_rows(
                    paths, {str(a.get("name")) for a in (rep.get("args") or [])}
                )
                if over_budget:
                    # A many-verb runnable (dozens of commands) would dominate the
                    # result payload — the one thing search mode exists to avoid.
                    # Name the tool that has the full answer instead of guessing.
                    cmd["note"] = (
                        "Arguments differ per command and are too many to list "
                        "here — call describe_runnable with this id and "
                        "command='<command>' for that command's arguments before "
                        "running it."
                    )
                elif per_command:
                    cmd["args"] = per_command
                entry["command"] = cmd
            rows.append(entry)
        # Team wiki facts about each runnable ride its row, so knowledge specific
        # to one tool reaches the agent when it is choosing that tool — no pin in
        # every prompt, no extra search_wiki step.
        notes = _wiki_notes_of(self, [str(e["name"]) for e in rows])
        for e in rows:
            if notes.get(e["name"]):
                e["wiki"] = notes[e["name"]]
        result: dict[str, Any] = {"count": len(rows), "results": rows}
        if notes:
            result["wiki_hint"] = (
                "A row's `wiki` lists what the team wiki knows about that "
                "runnable — follow it when building the call."
            )
        if truncated:
            result["truncated"] = True
            result["hint"] = (
                "More matches exist — refine the query or pass a group/host filter."
            )
        # Runbooks are the operator's saved procedures, so a query like "502s on
        # the web tier" should surface the triage runbook alongside the runnables
        # it would call. One search covering both kinds is deliberate: an agent
        # only searches for what it thinks to search for, so a separate
        # search_runbooks tool would need it to decide to look.
        #
        # Kept under their OWN key, never mixed into `results`: run_runnable's
        # description tells the model to pass any id from a result straight to it,
        # and its `id` is a free string with no enum — a runbook id in `results`
        # would be passed there and bounce (see _resolve_id_host's hint). The
        # shared limit would also let the two kinds crowd each other out.
        #
        # Skipped when a host/group filter is set: a runbook belongs to no host and
        # no package, so it cannot honour either filter — returning them anyway
        # would make the filter a lie, and the menu in run_runbook's description
        # still lists every one.
        if not host and not group:
            runbooks, rb_truncated = self._search_runbooks(tokens)
            if runbooks:
                result["runbooks"] = runbooks
                result["runbooks_hint"] = (
                    "Saved procedures — follow one with run_runbook (NOT "
                    "run_runnable). Prefer one of these over improvising a "
                    "sequence of runnables."
                )
                if rb_truncated:
                    result["runbooks_truncated"] = True
        return result

    @staticmethod
    def _score_runbook(rb: dict[str, Any], tokens: list[str]) -> int:
        """Keyword score for one runbook. Mirrors the runnable ``score()`` above —
        same substring matching, same "no tokens = browse" rule.

        Weights differ from the runnable version on purpose. For a runnable the
        *name* is what the operator knows and the description is prose nobody
        searches for. For a runbook it is the other way round: the query usually
        *is* a restatement of the symptom ("the web nodes are 502ing"), while the
        id is a slug the operator may never have seen. So id/title score 10 when
        they do match, but the symptom is weighted 5 rather than a description's 2."""
        if not tokens:
            return 1
        rid = str(rb.get("id") or "").lower()
        title = str(rb.get("title") or "").lower()
        symptom = str(rb.get("symptom") or "").lower()
        tags = " ".join(str(t) for t in (rb.get("tags") or [])).lower()
        s = 0
        for t in tokens:
            if t in rid or t in title:
                s += 10
            if t in symptom:
                s += 5
            if t in tags:
                s += 3
        return s

    def _search_runbooks(
        self, tokens: list[str], limit: int = _RUNBOOK_SEARCH_LIMIT
    ) -> tuple[list[dict[str, Any]], bool]:
        """Runbook rows for a ``search_runnables`` call, ranked and capped.
        Returns ``(rows, truncated)``.

        Reads ``memory.load_runbooks()`` rather than ``list_runbooks()`` — the
        latter returns only id/title/symptom and drops ``tags``, which we rank on.
        Args are shaped exactly like the runnable rows above (and like
        ``catalog.runbook_to_catalog_entry``) so a runbook's inputs describe
        identically wherever they surface, and the model can fill ``run_runbook``'s
        ``values`` without a second round-trip.

        Every step is defensive: this runs inside fleet-wide runnable discovery, and
        a hand-edited or Config-Sync'd ``runspec_runbooks.toml`` is not guaranteed to
        parse (``validate_runbook_arg_table`` only guards the agent's *save* path).
        A bad store, or one bad ``[runbook.args]`` table, must never take down
        ``search_runnables``."""
        from . import memory
        from .catalog import arg_to_property
        from .runbookargs import parse_runbook_args

        try:
            all_runbooks = memory.load_runbooks()
        except Exception:  # noqa: BLE001 — a bad store must not sink the search
            logger.debug("runbook store unavailable for search", exc_info=True)
            return [], False

        scored: list[tuple[int, str, dict[str, Any]]] = []
        for rb in all_runbooks:
            rid = str(rb.get("id") or "")
            if not rid:
                continue
            sc = self._score_runbook(rb, tokens)
            if sc <= 0:
                continue
            scored.append((sc, rid, rb))
        scored.sort(key=lambda t: (-t[0], t[1]))
        truncated = len(scored) > limit

        rows: list[dict[str, Any]] = []
        for _sc, rid, rb in scored[:limit]:
            arg_rows: list[dict[str, Any]] = []
            try:
                parsed = parse_runbook_args(rb.get("args"))
            except Exception:  # noqa: BLE001 — one bad table must not hide the rest
                logger.debug("runbook %s has an unparseable args table", rid)
                parsed = []
            for a in parsed:
                if str(a.get("type")) == "password":
                    continue  # secrets never travel inline
                row = {"name": a.get("name"), "required": bool(a.get("required"))}
                row.update(arg_to_property(a))
                arg_rows.append(row)
            entry: dict[str, Any] = {
                "id": rid,
                # Explicit kind: a runbook id is operator-chosen and stored verbatim
                # (only *derived* ids get slugified), so nothing stops one being
                # named like a runnable's dedup id. The tag removes any doubt about
                # which tool takes it, alongside the separate result key.
                "kind": "runbook",
                "title": str(rb.get("title") or ""),
                "symptom": str(rb.get("symptom") or ""),
                "args": arg_rows,
            }
            tags = [str(t) for t in (rb.get("tags") or []) if str(t).strip()]
            if tags:
                entry["tags"] = tags
            rows.append(entry)
        return rows, truncated

    def _search_host_groups(self) -> dict[str, set[str]]:
        """``{host group: {member hosts}}`` for the search filters; empty when the
        registry can't be read (search must never fail on it)."""
        try:
            return {
                str(g): {str(h) for h in members}
                for g, members in (self._groups_map() or {}).items()
            }
        except Exception:  # noqa: BLE001
            logger.debug("host groups unavailable for search", exc_info=True)
            return {}

    def _is_literal_host(self, name: str) -> bool:
        """A real host of that name wins over a same-named group (as in fan-out)."""
        try:
            return self._host_entry(name) is not None
        except Exception:  # noqa: BLE001
            return False

    def _run_search_tool(self, ti: dict[str, Any]) -> str:
        """Execute the ``search_runnables`` meta-tool → JSON string for the agent."""
        rows = self._search_runnables(
            str(ti.get("query") or ""),
            host=str(ti.get("host") or "").strip(),
            group=str(ti.get("group") or "").strip(),
        )
        return json.dumps(rows, default=str)

    @staticmethod
    def _describe_arg_rows(
        args: list[Any], skip: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Arg rows (name, required, type, enum, default, …) in the same shape as
        a search row's, minus ``skip`` names and any ``password`` arg — secrets
        never travel inline."""
        from .catalog import arg_to_property

        rows: list[dict[str, Any]] = []
        seen: set[str] = set(skip or ())
        for a in args or []:
            if not isinstance(a, dict):
                continue
            nm = str(a.get("name") or "")
            if not nm or nm in seen or str(a.get("type")) == "password":
                continue
            seen.add(nm)
            row: dict[str, Any] = {"name": nm, "required": bool(a.get("required"))}
            row.update(arg_to_property(a))
            rows.append(row)
        return rows

    def _run_describe_tool(self, ti: dict[str, Any]) -> str:
        """Execute ``describe_runnable`` → one runnable's interface, shaped like
        the ``run_runnable`` call it is for (``args`` by name, ``command`` beside
        them), not like a dedup tool schema.

        A subcommand runnable lists every command with its OWN args (globals
        once, at the top). With ``command`` it returns that one command's
        complete arg list — globals included, required flags exact — so the
        agent can build the call for the command it chose rather than guessing
        and drifting to another one whose args it happens to know."""
        rid = str(ti.get("id") or "")
        matches = self._runnables_for_id(rid)
        if not matches:
            # Same runbook hint as _resolve_id_host: "tell me more about this row"
            # is the natural next move on a runbook returned by search_runnables,
            # and this tool has its own copy of the unknown-id error.
            hint = self._runbook_id_hint(rid)
            if hint:
                return hint
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
        result: dict[str, Any] = {
            "id": rid,
            "name": name,
            "description": str(rep.get("description") or ""),
            "hosts": hosts,
            "autonomy": str(rep.get("autonomy") or "confirm"),
        }
        commands = rep.get("commands") or {}
        raw_cmd = ti.get("command")
        if isinstance(raw_cmd, list):
            path = [str(c) for c in raw_cmd if str(c).strip()]
        else:
            path = str(raw_cmd or "").split()
        if path:
            node: dict[str, Any] = rep
            for seg in path:
                nxt = (node.get("commands") or {}).get(seg)
                if not isinstance(nxt, dict):
                    options = [" ".join(p) for p, _ in utils_mod.subcommand_paths(rep)]
                    return (
                        f"Error: '{name}' has no command '{' '.join(path)}'. "
                        f"Commands: {', '.join(options) or '(none)'}."
                    )
                node = nxt
            # Discovery merges inherited globals into every node, so the node's
            # own args are the complete set this command accepts.
            result["command"] = " ".join(path)
            if node.get("description"):
                result["command_description"] = str(node["description"])
            if node.get("autonomy"):
                result["autonomy"] = str(node["autonomy"])
            result["args"] = self._describe_arg_rows(
                list(node.get("args") or []) + list(rep.get("args") or [])
            )
            sub = utils_mod.subcommand_paths(node)
            if node.get("requireCommand") and sub:
                result["subcommands"] = [" ".join(path + p) for p, _ in sub]
            result["call"] = (
                f"run_runnable(id='{rid}', host=<one of hosts>, "
                f"command='{' '.join(path)}', args={{<name>: <value>}})"
            )
        else:
            globals_ = list(rep.get("args") or [])
            result["args"] = self._describe_arg_rows(globals_)
            if commands:
                inherited = {
                    str(a.get("name")) for a in globals_ if isinstance(a, dict)
                }
                paths = utils_mod.subcommand_paths(rep)
                per: dict[str, dict[str, Any]] = {}
                total = 0
                for p, node in paths:
                    entry: dict[str, Any] = {}
                    if node.get("description"):
                        entry["description"] = str(node["description"])
                    rows = self._describe_arg_rows(node.get("args") or [], inherited)
                    total += len(rows)
                    entry["args"] = rows
                    per[" ".join(p)] = entry
                if total > _DESCRIBE_COMMAND_ARG_BUDGET:
                    # All-or-nothing, like the search row: past the budget every
                    # command keeps only its arg names, never a half-listed set.
                    for entry in per.values():
                        entry["args"] = [r["name"] for r in entry["args"]]
                    result["commands_note"] = (
                        "Too many arguments to describe in full — each command "
                        "lists only its arg names. Call describe_runnable again "
                        "with command='<command>' for that command's full args."
                    )
                result["command_required"] = bool(rep.get("requireCommand"))
                result["commands"] = per
                result["args_note"] = (
                    "'args' are globals every command accepts; each command adds "
                    "its own. Pass the command as run_runnable's TOP-LEVEL "
                    "'command' field, never inside args. Every argument — global "
                    "or the command's own — goes inside run_runnable's 'args'."
                )
                result["call"] = (
                    f"run_runnable(id='{rid}', host=<one of hosts>, "
                    "command='<command>', args={<name>: <value>, ...})"
                )
        creds = [
            {"id": str(c.get("id", "")), "label": str(c.get("label", ""))}
            for c in self._union_scoped_credentials(hosts, name)
            if c.get("id")
        ]
        if creds:
            result["credentials"] = creds
        runs_as = self._runs_as_by_host(matches)
        if runs_as:
            result["runs_as"] = runs_as
        notes = _wiki_notes_of(self, [name], per_entity=kn.NOTES_PER_DESCRIBE)
        if notes.get(name):
            result["wiki"] = notes[name]
        return json.dumps(result, default=str)

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
        one per runnable name, sorted, bounded by ``[llm] tool_index_limit``
        (default 300) with an overflow note. Empty when disabled or nothing's
        discovered."""
        if not self._catalog_index_enabled():
            return ""
        try:
            limit = int(self.get_config().get("llm", {}).get("tool_index_limit", 300))
        except (TypeError, ValueError):
            limit = 300
        runnables = self._visible_runnables("agent")
        with self._lock:
            ready = self._discovery_ready
            done, total = self._discovery_progress
        seen: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for r in runnables:
            key = str(r.get("name") or "")
            if key not in seen:
                seen[key] = r
                order.append(key)
        lines: list[str] = []
        for key in sorted(order):
            r = seen[key]
            rid = self._dedup_tool_name(r)
            desc = self._first_sentence(r.get("description") or "")
            lines.append(f"- {rid}" + (f" — {desc}" if desc else ""))
        total_count = len(lines)
        overflow = 0
        if total_count > limit > 0:
            overflow = total_count - limit
            lines = lines[:limit]
            lines.append(f"- …and {overflow} more — use search_runnables to find them.")
        # Record the overflow so the context indicator can surface "N beyond the
        # inline menu", and log once when the count changes (a state transition,
        # not every turn — the index is rebuilt per turn) so a fleet that outgrows
        # the menu isn't silent.
        self._catalog_overflow = overflow
        if overflow != getattr(self, "_catalog_overflow_logged", None):
            self._catalog_overflow_logged = overflow
            if overflow > 0:
                logger.info(
                    "catalog index: %d runnables exceed [llm] tool_index_limit=%d "
                    "— showing %d inline, %d beyond the menu (agent still finds them "
                    "via search_runnables; raise tool_index_limit to list more, or "
                    "hide some via [tool_visibility] hidden_from_agent)",
                    total_count,
                    limit,
                    limit,
                    overflow,
                )
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

    def _build_builtin_index(self, headless: bool = False) -> str:
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
            for t in self._deferred_builtin_tools(headless)
        )

    def _chat_index_text(
        self, headless: bool = False, builtins_deferred: bool = True
    ) -> str:
        """The full always-on menu for a search-mode agent turn: the runnable
        catalog index plus, when built-ins are deferred, a labelled deferred-tools
        section. Empty outside search mode. Composed into the system prompt as a
        trailing section (see ``_catalog_system_section``), not folded into the
        first user message — so a gateway caching only the system block caches it.
        ``builtins_deferred=False`` (a headless turn whose built-ins are inline)
        leaves the deferred-tools section out; ``headless`` drops the chat-only
        tools from it.

        Each section labels itself; ``_catalog_system_section`` adds only a generic
        heading over the whole blob."""
        if self._tool_mode() != "search":
            return ""
        index = self._build_catalog_index()
        if index:
            index = (
                "Runnables you can execute via run_runnable (id — description). "
                "Call search_runnables or describe_runnable for a runnable's "
                "arguments and hosts before running it:\n" + index
            )
        builtin_lines = self._build_builtin_index(headless) if builtins_deferred else ""
        if builtin_lines:
            block = (
                "Built-in console tools deferred from the tool list (name — "
                "description). Call describe_tool for a tool's input schema, "
                "then use_tool to run it — each tool's own confirm rules still "
                "apply:\n" + builtin_lines
            )
            index = f"{index}\n\n{block}" if index else block
        return index

    @staticmethod
    def _catalog_system_section(index_text: str) -> str:
        """Wrap the search-mode catalogue index (``_chat_index_text``) as a
        trailing section of the system prompt, under a generic heading. Empty
        when ``index_text`` is empty (dedup/per-host mode, or the index disabled),
        so the caller appends nothing.

        The heading is generic on purpose: each section inside ``index_text``
        already labels itself (runnables menu, deferred-built-ins menu). Byte-stable
        across a turn — ``index_text`` is already sorted and carries nothing
        time-varying — so the cached system prefix holds turn to turn."""
        if not index_text:
            return ""
        return f"{_CATALOG_SYSTEM_HEADING}\n\n{index_text}"

    # ── terminal sessions ─────────────────────────────────────────────────────
