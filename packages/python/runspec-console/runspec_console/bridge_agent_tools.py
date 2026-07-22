"""bridge_agent_tools.py — AgentToolsMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from . import hosts as hosts_mod

from .bridge_const import (
    logger,
    ToolCancelled,
    _AGENT_TOOL_OUTPUT_CAP,
    _EXEC_MAX_ROWS,
    _SPILL_ENVELOPE_HEAD_BYTES,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 datetime string from a tool arg into a naive local-time
    ``datetime`` (or ``None`` if blank/unparseable). Any timezone offset is dropped
    — Outlook COM takes wall-clock local times — so an aware input is treated as
    the operator's local time."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _is_web_url(value: Any) -> bool:
    """True only for a well-formed ``http(s)://`` URL with a host. Pure/testable.

    The ``open_url`` action tool hands the string to the OS default handler, so it
    is deliberately restricted to web URLs — ``file:``/``javascript:`` and arbitrary
    protocol handlers are refused so the agent can't be steered into opening a local
    file or a custom-scheme URI. Teams join links are ``https``, so this is enough."""
    from urllib.parse import urlparse

    try:
        parts = urlparse(str(value or "").strip())
    except Exception:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# Extensions the ``open_file`` action tool will hand to the OS default app. Kept to
# **passive viewers** — documents, text, and images that render without executing —
# so a jailed file can't turn "open" into code execution. Everything else (any
# executable/script, Office macro-capable formats, .html/.svg which can run script
# in a browser, .lnk/.reg, …) is refused; use reveal_in_folder to let the operator
# decide. See docs/design/console-session-workspace.md.
_OPENABLE_EXTS = frozenset(
    {
        ".pdf",
        ".txt",
        ".md",
        ".csv",
        ".log",
        ".json",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    }
)


def _openable_ext(name: str) -> bool:
    """True when ``name``'s extension is in the passive-viewer allowlist. Pure."""
    return Path(str(name)).suffix.lower() in _OPENABLE_EXTS


def _launch_open(path: str) -> None:
    """Open ``path`` with the OS default app (Windows/macOS/Linux). Raises on
    failure. Callers pass only a jailed, extension-allowlisted path."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 — jailed+allowlisted
    else:
        import subprocess

        launcher = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([launcher, path], close_fds=True)


def _launch_reveal(path: str) -> None:
    """Reveal ``path`` in the OS file manager, highlighting the item where the
    platform supports it (Explorer /select, Finder -R). Raises on failure. Safe for
    any file type — it only shows the file, never opens/executes it."""
    p = Path(path)
    if sys.platform == "win32":
        if p.is_dir():
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 — jailed dir
        else:
            import subprocess

            # explorer returns exit code 1 even on success — fire-and-forget.
            subprocess.Popen(["explorer", f"/select,{path}"], close_fds=True)
    elif sys.platform == "darwin":
        import subprocess

        subprocess.Popen(["open", "-R", path], close_fds=True)
    else:
        import subprocess

        target = path if p.is_dir() else str(p.parent)
        subprocess.Popen(["xdg-open", target], close_fds=True)


class AgentToolsMixin:
    def _tool_mode(self) -> str:
        """Agent tool-list layout, from ``[llm] tool_mode``.

        ``"dedup"`` (default) emits one tool per folder/runnable (named
        ``group__name``) with a required ``host`` enum listing every host that has
        it — collapsing the N identical per-host copies a uniform fleet produces
        into a single definition, which is the bulk of the agent's context budget.
        ``"per_host"`` restores the original one-tool-per-(host, runnable) layout
        (``host__name``) as a zero-code escape hatch. ``"search"`` replaces the
        per-runnable tools entirely with discovery meta-tools (search_runnables /
        run_runnable / describe_runnable) — a constant ~3-tool footprint regardless
        of how many distinct runnables the fleet installs."""
        mode = str(self.get_config().get("llm", {}).get("tool_mode", "dedup")).strip()
        return mode if mode in ("dedup", "per_host", "search") else "dedup"

    def _tool_output_cap(self) -> int:
        """Max chars of a single runnable's output handed to the model as a tool
        result, from ``[llm] tool_output_cap`` (default ``_AGENT_TOOL_OUTPUT_CAP``,
        1 MB). Beyond it the result is cut with a visible marker — the agent gets
        incomplete JSON otherwise. Lower it per-deployment to force a chatty
        runnable to fail loudly/early rather than silently at ~250k tokens; the
        1 MB default is kept so data-heavy runnables emitting one big JSON line
        aren't sliced mid-structure out of the box (see bridge_const)."""
        try:
            cap = int(
                self.get_config()
                .get("llm", {})
                .get("tool_output_cap", _AGENT_TOOL_OUTPUT_CAP)
            )
        except (TypeError, ValueError):
            return _AGENT_TOOL_OUTPUT_CAP
        return cap if cap > 0 else _AGENT_TOOL_OUTPUT_CAP

    def _max_continuations(self) -> int:
        """How many times a single chat turn may auto-continue after the model
        stops at the output-token ceiling (``stop_reason == "max_tokens"``), from
        ``[llm] max_continuations`` (default 3, floored at 0). Each continuation
        re-prompts the model to pick up where it was cut, so a long reply streams
        out whole instead of being silently truncated; past this budget the turn
        ends with a visible cut-off marker."""
        try:
            n = int(self.get_config().get("llm", {}).get("max_continuations", 3))
        except (TypeError, ValueError):
            return 3
        return n if n >= 0 else 3

    @staticmethod
    def _sanitize_tool_name(raw: str) -> str:
        """Anthropic tool names allow only ``[a-zA-Z0-9_-]`` and cap at 64 chars."""
        return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:64]

    def _dedup_tool_name(self, r: dict[str, Any]) -> str:
        """Folder-qualified tool name for dedup mode: sanitised ``group__name``
        (the bare name when the runnable has no group)."""
        group = str(r.get("group") or "")
        name = str(r.get("name") or "")
        return self._sanitize_tool_name(f"{group}__{name}" if group else name)

    @staticmethod
    def _declared_arg_name_set(r: dict[str, Any] | None) -> set[str]:
        """Every arg name the runnable declares (globals + all subcommand args)."""
        names: set[str] = set()
        for a in (r or {}).get("args") or []:
            if a.get("name"):
                names.add(a["name"])
        for cmd in ((r or {}).get("commands") or {}).values():
            for a in cmd.get("args") or []:
                if a.get("name"):
                    names.add(a["name"])
        return names

    def _host_selector_key(self, r: dict[str, Any] | None) -> str:
        """Property name for the injected host enum in dedup mode. Normally
        ``"host"``; if the runnable already declares an arg named ``host`` (so the
        selector would shadow a real input), fall back to ``_host`` / ``__host`` —
        derived deterministically so build and dispatch agree without extra state."""
        declared = self._declared_arg_name_set(r)
        key = "host"
        while key in declared:
            key = "_" + key
        return key

    def _union_scoped_credentials(
        self, hosts: list[str], runnable: str
    ) -> list[dict[str, Any]]:
        """Credentials in scope for ``runnable`` on *any* of ``hosts`` (dedup mode
        offers one shared override enum; auto-apply still resolves per actual host
        at dispatch). De-duplicated by id, first occurrence wins."""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for h in hosts:
            for c in self._scoped_credentials(h, runnable):
                if c["id"] not in seen:
                    seen.add(c["id"])
                    out.append(c)
        return out

    def _allowed_hosts(
        self, allowset: set[str], name: str, tool_name: str, hosts: list[str]
    ) -> list[str]:
        """Subset of ``hosts`` a dedup tool may target under a trigger allowlist.

        A bare runnable ``name`` or the dedup ``tool_name`` admits every host; a
        host-qualified entry (``host__name``, raw or sanitised) admits only that
        host — so a trigger scoped to one host's runnable stays scoped even though
        the deduped tool is shared across hosts."""
        if name in allowset or tool_name in allowset:
            return list(hosts)
        out: list[str] = []
        for h in hosts:
            raw = f"{h}__{name}"
            if raw in allowset or self._sanitize_tool_name(raw) in allowset:
                out.append(h)
        return out

    def _build_input_schema(
        self,
        r: dict[str, Any],
        scoped_creds: list[dict[str, Any]],
        hosts: list[str] | None = None,
        host_key: str = "host",
    ) -> dict[str, Any]:
        """Anthropic ``input_schema`` for one runnable.

        ``hosts`` (dedup mode) prepends a required host enum under ``host_key``
        naming the hosts that have this runnable; ``None`` (per-host mode) omits it
        — the host is carried in the tool name instead. ``scoped_creds`` drives the
        optional ``credential_id`` override enum."""
        properties: dict[str, Any] = {}
        required: list[str] = []

        if hosts is not None:
            properties[host_key] = {
                "type": "string",
                "enum": list(hosts),
                "description": "Host to run on. Options: " + ", ".join(hosts) + ".",
            }
            required.append(host_key)

        def add_arg(arg: dict[str, Any], note: str = "") -> None:
            """Add one spec arg as a JSON-schema property. ``note`` prefixes the
            description (used to tag a subcommand's own args) and marks the arg
            as conditional — never added to the tool's top-level ``required``."""
            arg_name = arg.get("name", "")
            if not arg_name or arg_name in properties:
                return
            # Secrets never become agent inputs — the agent must not see, type, or
            # guess a password (a guess just yields an auth failure). A bound
            # credential fills it from secure storage at run time (in-scope ones
            # auto-apply; credential_id picks a specific one). Same rule as the
            # visitor catalog (catalog.py) and the core's --format mcp emit.
            if arg.get("type") == "password":
                return
            arg_type = arg.get("type", "str")
            json_type = {
                "int": "integer",
                "float": "number",
                "flag": "boolean",
                "bool": "boolean",
                "path": "string",
            }.get(arg_type, "string")
            prop: dict[str, Any] = {"type": json_type}
            # Build description: optional note + user-facing text + default hint
            # so the LLM knows what value will be used when the arg is omitted.
            desc_parts: list[str] = []
            if note:
                desc_parts.append(note)
            if arg.get("description") or arg.get("help"):
                desc_parts.append(str(arg.get("description") or arg.get("help")))
            default = arg.get("default")
            if default is not None:
                if isinstance(default, str) and default.startswith("$"):
                    desc_parts.append(f"(default from env var {default})")
                else:
                    desc_parts.append(f"(default: {default})")
            if desc_parts:
                prop["description"] = " ".join(desc_parts)
            if arg.get("options"):
                prop["enum"] = arg["options"]
            properties[arg_name] = prop
            if arg.get("required") and not note:
                required.append(arg_name)

        for arg in r.get("args") or []:
            add_arg(arg)

        # Surface subcommands so the agent can pick one — and knows when it
        # MUST. Without this the agent only saw the global args and had no way
        # to name a command, so require-command runnables were unrunnable.
        commands = r.get("commands") or {}
        if commands:
            cmd_names = list(commands.keys())
            require_command = bool(r.get("requireCommand"))
            summary = "; ".join(
                f"'{cn}'"
                + (
                    f" — {commands[cn].get('description')}"
                    if commands[cn].get("description")
                    else ""
                )
                for cn in cmd_names
            )
            properties["command"] = {
                "type": "string",
                "enum": cmd_names,
                "description": (
                    f"Subcommand to run ({summary}). "
                    + (
                        "Required: this runnable cannot run without choosing "
                        "one of these commands."
                        if require_command
                        else "Optional — omit to run the runnable at its top level."
                    )
                ),
            }
            if require_command:
                required.append("command")
            # Each command's own (non-inherited) args become optional inputs,
            # tagged with the command they belong to. Globals already added above.
            for cn in cmd_names:
                for arg in commands[cn].get("args") or []:
                    if arg.get("inherited"):
                        continue
                    add_arg(arg, note=f"(for the '{cn}' command)")

        # Offer any in-scope credentials by id (override only — in-scope ones
        # apply automatically). The agent picks an *id*, never sees the secret.
        if scoped_creds:
            cred_summary = "; ".join(
                f"'{c['id']}'" + (f" — {c['label']}" if c.get("label") else "")
                for c in scoped_creds
            )
            properties["credential_id"] = {
                "type": "string",
                "enum": [c["id"] for c in scoped_creds],
                "description": (
                    "Optional: apply a saved credential by id to fill its bound "
                    "arguments from secure storage. The matching credential(s) "
                    "already apply automatically — set this only to choose a "
                    f"specific one. Options: {cred_summary}."
                ),
            }

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _runnables_to_tools(
        self, allow: Collection[str] | None = None
    ) -> list[dict[str, Any]]:
        """Convert cached runnables to Anthropic-format tool schemas.

        Layout follows ``_tool_mode()``: ``"dedup"`` (default) emits one tool per
        ``(group, name)`` with a host enum; ``"per_host"`` emits one per
        ``(host, name)``; ``"search"`` emits only the discovery meta-tools. ``allow``
        (when non-empty) scopes the result to an allowlist — each entry matches a
        runnable's bare ``name`` or its qualified ``host__name`` (raw or sanitised) —
        so an agent-mode trigger can be limited to a curated set of tools (in dedup
        mode a host-qualified entry also narrows the shared tool's host enum).
        ``None``/empty = every runnable.

        Search mode applies only to the unrestricted (manual chat) tool list: a
        trigger that passes an ``allow`` set is already curated to a handful of
        runnables, so it falls back to the dedup layout (scoped) — the search
        interface would add nothing there.
        """
        allowset = {str(a).strip() for a in allow} if allow else None
        self._reload_credentials()  # so per-runnable credential_id enums are current
        mode = self._tool_mode()
        if mode == "search" and allowset is None:
            tools = self._search_mode_tools()
            try:
                tools.extend(self._custom_agent_tools())
            except Exception:
                logger.debug("custom agent tools unavailable", exc_info=True)
            return tools
        with self._lock:
            runnables = list(self._runnables_cache)
        tools: list[dict[str, Any]] = []
        seen_tool_names: set[str] = set()

        def _dup_warn(tool_name: str, name: str) -> None:
            sys.stderr.write(
                f"runspec-console: warning: duplicate tool name '{tool_name}' skipped"
                f" ('{name}' defined in multiple installed packages)\n"
            )
            sys.stderr.flush()

        if mode == "per_host":
            for r in runnables:
                host = r.get("host", "local")
                name = r.get("name", "")
                raw_name = f"{host}__{name}"
                tool_name = self._sanitize_tool_name(raw_name)
                if allowset is not None and not (
                    name in allowset or raw_name in allowset or tool_name in allowset
                ):
                    continue  # scoped out by the trigger's runnables allowlist
                if tool_name in seen_tool_names:
                    _dup_warn(tool_name, name)
                    continue
                seen_tool_names.add(tool_name)
                schema = self._build_input_schema(
                    r, self._scoped_credentials(host, name)
                )
                tools.append(
                    {
                        "name": tool_name,
                        "description": r.get("description") or f"Run {name} on {host}",
                        "input_schema": schema,
                    }
                )
        else:
            # dedup: collapse the per-host copies of each (group, name). The first
            # runnable seen for a key drives the schema (a uniform fleet runs an
            # identical spec on every host); the host enum lists every host with it.
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
            for key in order:
                rep = groups[key]["rep"]
                hosts = groups[key]["hosts"]
                name = rep.get("name", "")
                tool_name = self._dedup_tool_name(rep)
                if allowset is not None:
                    hosts = self._allowed_hosts(allowset, name, tool_name, hosts)
                    if not hosts:
                        continue  # no allowed host for this runnable
                if tool_name in seen_tool_names:
                    _dup_warn(tool_name, name)
                    continue
                seen_tool_names.add(tool_name)
                schema = self._build_input_schema(
                    rep,
                    self._union_scoped_credentials(hosts, name),
                    hosts=hosts,
                    host_key=self._host_selector_key(rep),
                )
                tools.append(
                    {
                        "name": tool_name,
                        "description": (
                            rep.get("description") or f"Run {name} on a fleet host"
                        ),
                        "input_schema": schema,
                    }
                )
        # Built-in file-transfer tools are additive — never let a problem building
        # them strip the runnable tools the agent actually needs.
        try:
            tools.extend(self._custom_agent_tools())
        except Exception:
            logger.debug("custom agent tools unavailable", exc_info=True)
        return tools

    def _custom_agent_tools(self) -> list[dict[str, Any]]:
        """Schemas for the built-in file-transfer tools — download a file from a
        remote host, or relay one host→host, each with an optional saved filter.
        Host and filter names are surfaced as enums (with the filter descriptions)
        so the model picks valid values."""
        remote = [h["name"] for h in self._hosts if h.get("ssh")]
        deny_tool = self._denylist_tool()  # available for local-only fleets too
        if not remote:
            # No transfer tools (those need a remote), but status + memory are
            # local and the denylist tool works against any host with a runspec path.
            return [
                *([deny_tool] if deny_tool else []),
                *self._outlook_tools(),
                *self._repo_tools(),
                *self._status_tools(),
                *self._memory_tools(),
                *self._action_tools(),
                *self._workspace_tools(),
                *self._exec_tools(),
                *self._config_tools(),
            ]
        self._reload_credentials()  # for the relay source_credential enum
        filters = self._filters
        filter_names = [f["name"] for f in filters if f.get("name")]

        def filter_props() -> dict[str, Any]:
            if not filter_names:
                return {}
            lines = []
            for f in filters:
                line = f"'{f['name']}'"
                if "{{arg}}" in (f.get("pipeline") or ""):
                    line += f" (needs filter_value: {f.get('param_label') or 'value'})"
                if f.get("description"):
                    line += f" — {f['description']}"
                lines.append(line)
            return {
                "filter": {
                    "type": "string",
                    "enum": filter_names,
                    "description": (
                        "Optional saved filter applied on the source host before "
                        "transfer, so only matching output crosses the network "
                        "(ideal for a few lines from a big log). Available: "
                        + "; ".join(lines)
                        + "."
                    ),
                },
                "filter_value": {
                    "type": "string",
                    "description": "Value for a filter that needs one (its {{arg}} placeholder).",
                },
            }

        cred_ids = [str(c.get("id")) for c in self._credentials if c.get("id")]

        def adhoc_source_props() -> dict[str, Any]:
            """Ad-hoc source fields shared by download_file and relay_file — read
            from an SSH host that is NOT in the fleet. Set source_target to the
            hostname; add source_via only when the console can't reach it directly
            (a jump to tunnel through), and source_credential for its password."""
            props: dict[str, Any] = {
                "source_target": {
                    "type": "string",
                    "description": (
                        "Hostname of an ad-hoc source NOT in the fleet. The console "
                        "connects to it directly unless source_via is also set."
                    ),
                },
                "source_via": {
                    "type": "string",
                    "enum": remote,
                    "description": (
                        "Optional configured jump host to tunnel to source_target "
                        "through — set only when the console can't reach the ad-hoc "
                        "host directly. Omit for a directly reachable host."
                    ),
                },
            }
            if cred_ids:
                props["source_credential"] = {
                    "type": "string",
                    "enum": cred_ids,
                    "description": (
                        "Saved credential id providing the SSH password for the ad-hoc "
                        "source (source_via + source_target). Its secret is read from "
                        "secure storage at connect time — you never see the value. "
                        f"Available: {', '.join(cred_ids)}."
                    ),
                }
            return props

        download = {
            "name": "download_file",
            "description": (
                "Download a file from a remote host to the local machine. The source "
                "is either a configured fleet host (host) or an ad-hoc host not in "
                "the fleet, reached through a configured jump (source_via + "
                "source_target). Optionally filter it on the remote host first — only "
                "the matching output is transferred, so pulling a few relevant lines "
                "out of a multi-gigabyte log is cheap."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "enum": remote,
                        "description": (
                            "Configured fleet host to download from. Omit when "
                            "reading from an ad-hoc host via source_via + "
                            "source_target instead."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file on the remote host.",
                    },
                    "dest_dir": {
                        "type": "string",
                        "description": "Local destination directory.",
                    },
                    "dest_name": {
                        "type": "string",
                        "description": (
                            "Optional new filename for the downloaded file (a bare "
                            "filename, no path). Omit to keep the source's name."
                        ),
                    },
                    **adhoc_source_props(),
                    **filter_props(),
                },
                "required": ["path", "dest_dir"],
            },
        }
        relay_source_props: dict[str, Any] = {
            "source_host": {
                "type": "string",
                "enum": remote,
                "description": (
                    "Configured fleet host to read from. Omit when reading from an "
                    "ad-hoc host via source_via + source_target instead."
                ),
            },
            **adhoc_source_props(),
        }
        relay = {
            "name": "relay_file",
            "description": (
                "Relay a file from one host to another through the console, "
                "optionally filtering it on the source host first. The source is "
                "either a configured fleet host (source_host) or an ad-hoc host "
                "reached via a jump (source_via + source_target, with an optional "
                "source_credential for password auth)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    **relay_source_props,
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file on the source host.",
                    },
                    "dest_host": {
                        "type": "string",
                        "enum": remote,
                        "description": "Host to write the file to.",
                    },
                    "dest_dir": {
                        "type": "string",
                        "description": "Destination directory on dest_host.",
                    },
                    "dest_name": {
                        "type": "string",
                        "description": (
                            "Optional new filename on dest_host (a bare filename, no "
                            "path). Omit to keep the source's name."
                        ),
                    },
                    **filter_props(),
                },
                "required": ["path", "dest_host", "dest_dir"],
            },
        }
        return [
            download,
            relay,
            *([deny_tool] if deny_tool else []),
            *self._outlook_tools(),
            *self._repo_tools(),
            *self._status_tools(),
            *self._memory_tools(),
            *self._action_tools(),
            *self._workspace_tools(),
            *self._exec_tools(),
            *self._config_tools(),
        ]

    def _outlook_tools(self) -> list[dict[str, Any]]:
        """Schemas for the Outlook Classic (desktop COM) tools — read inbox /
        search / calendar / reminders (read-only, autonomous) and draft / send
        (confirm-gated). Offered only when Outlook COM is available (Windows +
        pywin32); empty list otherwise so non-Windows consoles are unaffected."""
        from .config import read_mailbox, read_watches
        from .sources import outlook as ol

        if not ol.outlook_available():
            return []
        mailbox = read_mailbox() or {}
        mbox_hint = (
            f" The monitored mailbox is {mailbox.get('address') or 'your default account'}"
            f"{('/' + mailbox['folder']) if mailbox.get('folder') else ''};"
            " omit mailbox/folder to use it."
            if mailbox
            else " Omit mailbox/folder to use your default account's Inbox."
        )

        def _watch_name(w: dict[str, Any]) -> str:
            addr = w.get("address") or "your default account"
            folder = w.get("folder")
            return f"{addr}/{folder}" if folder and folder != "Inbox" else addr

        watches = read_watches()
        if watches:
            mbox_hint += (
                " Also monitored (pass mailbox/folder to read these): "
                + ", ".join(_watch_name(w) for w in watches)
                + "."
            )
        return [
            {
                "name": "outlook_inbox",
                "description": (
                    "List recent messages from Outlook Classic (desktop COM, newest "
                    "first), unread by default." + mbox_hint
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "unread_only": {
                            "type": "boolean",
                            "description": "Only unread messages (default true).",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Max messages to return (default 20).",
                        },
                        "mailbox": {
                            "type": "string",
                            "description": "Mailbox address; omit for the monitored/default account.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Folder path (e.g. 'Tickets/Open'); omit for Inbox.",
                        },
                    },
                },
            },
            {
                "name": "outlook_search",
                "description": "Search an Outlook folder's messages by subject/body text (desktop COM)."
                + mbox_hint,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text to match in subject or body.",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Max messages to return (default 20).",
                        },
                        "mailbox": {
                            "type": "string",
                            "description": "Mailbox address; omit for the monitored/default account.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Folder path; omit for Inbox.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "outlook_get_message",
                "description": (
                    "Read one Outlook message in full by its entry_id — full body, "
                    "headers, and attachment list. Use this after outlook_inbox / "
                    "outlook_search, whose rows carry only a short preview."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the message (from outlook_inbox/outlook_search or a new_mail trigger).",
                        },
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "outlook_calendar",
                "description": "List upcoming Outlook calendar events over the next N days (desktop COM).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Days ahead to include (default 1 = today).",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Max events to return (default 20).",
                        },
                    },
                },
            },
            {
                "name": "outlook_reminders",
                "description": "List currently-active Outlook reminders (desktop COM).",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "outlook_resolve_recipient",
                "description": (
                    "Resolve a name against the Outlook address book (GAL/contacts) "
                    "to its email address before drafting or sending. Read-only."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Display name or partial name to resolve.",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "outlook_draft",
                "description": (
                    "Save a mail to the Outlook Drafts folder (never sent). Provide "
                    "reply_to (a message entry_id) to draft a reply with the original "
                    "quoted, or to/subject to draft a new message. from_mailbox sends "
                    "as a shared mailbox (send-as/on-behalf)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "string",
                            "description": "Message body (your text; goes above the quoted original on a reply).",
                        },
                        "reply_to": {
                            "type": "string",
                            "description": "entry_id of the message to reply to (from outlook_inbox/search or a new_mail trigger).",
                        },
                        "reply_all": {
                            "type": "boolean",
                            "description": "With reply_to, reply to the sender AND all other recipients (default false).",
                        },
                        "to": {
                            "type": "string",
                            "description": "Recipient(s) for a new message (semicolon-separated).",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Subject for a new message.",
                        },
                        "cc": {
                            "type": "string",
                            "description": "CC recipient(s) for a new message.",
                        },
                        "from_mailbox": {
                            "type": "string",
                            "description": "Shared mailbox to send as (optional).",
                        },
                    },
                    "required": ["body"],
                },
            },
            {
                "name": "outlook_send",
                "description": (
                    "Send mail via Outlook Classic. Provide entry_id to send an "
                    "existing draft, or to/subject/body to compose and send. This "
                    "actually sends — it is confirm-gated, so the operator approves "
                    "each send. from_mailbox sends as a shared mailbox."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of a saved draft to send.",
                        },
                        "to": {
                            "type": "string",
                            "description": "Recipient(s) when composing+sending (semicolon-separated).",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Subject when composing+sending.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Body when composing+sending.",
                        },
                        "cc": {"type": "string", "description": "CC recipient(s)."},
                        "from_mailbox": {
                            "type": "string",
                            "description": "Shared mailbox to send as (optional).",
                        },
                    },
                },
            },
            {
                "name": "outlook_save_attachment",
                "description": (
                    "Save an attachment from an Outlook message to a file on this "
                    "machine (e.g. to then feed a log file into the logops runnables). "
                    "Identify it by name or 1-based index; omit both if the message "
                    "has exactly one attachment. Defaults to the system temp dir."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the message holding the attachment.",
                        },
                        "name": {
                            "type": "string",
                            "description": "Attachment filename to save (case-insensitive; substring allowed).",
                        },
                        "index": {
                            "type": "integer",
                            "description": "1-based attachment index (alternative to name).",
                        },
                        "dest_dir": {
                            "type": "string",
                            "description": "Destination directory; omit for the system temp dir.",
                        },
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "outlook_create_event",
                "description": (
                    "Create an Outlook calendar event. With attendees it sends a "
                    "meeting invite; without, it saves a personal calendar block. "
                    "Times are interpreted in your local timezone. **For a Teams "
                    "meeting, set open_in_outlook=true**: it saves the event (unsent) "
                    "and opens it in an Outlook window so the operator clicks the "
                    "'Teams Meeting' button (one click) and Sends — the only reliable "
                    "way to attach a Teams link on Outlook Classic, since the add-in "
                    "doesn't run for COM-created meetings. Afterwards, read the link "
                    "back with outlook_event_join_url and the returned entry_id. "
                    "(online_meeting=true only toggles a machine-wide Outlook setting "
                    "that does NOT apply to these COM-created meetings — prefer "
                    "open_in_outlook.)"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "Event title.",
                        },
                        "start": {
                            "type": "string",
                            "description": (
                                "Start time in the operator's LOCAL time, ISO 8601 "
                                "(e.g. 2026-06-27T14:00 for 2pm local). Do NOT convert "
                                "to UTC or add a timezone offset. Check console_status "
                                "for the current local time when the request is relative."
                            ),
                        },
                        "end": {
                            "type": "string",
                            "description": (
                                "End time in the operator's LOCAL time, ISO 8601. "
                                "Defaults to 30 minutes after start."
                            ),
                        },
                        "attendees": {
                            "type": "string",
                            "description": "Attendee address(es), semicolon-separated. Provide to send an invite.",
                        },
                        "location": {
                            "type": "string",
                            "description": "Location (optional).",
                        },
                        "body": {
                            "type": "string",
                            "description": "Event body / agenda (optional).",
                        },
                        "reminder_minutes": {
                            "type": "integer",
                            "description": "Reminder lead time in minutes (default 15; negative for no reminder).",
                        },
                        "open_in_outlook": {
                            "type": "boolean",
                            "description": (
                                "Save the event unsent and open it in Outlook so the "
                                "operator adds a Teams meeting (one click) and Sends. "
                                "The reliable path for a Teams link on Outlook Classic."
                            ),
                        },
                        "online_meeting": {
                            "type": "boolean",
                            "description": (
                                "Best-effort: enable Outlook's machine-wide 'add "
                                "online meeting to all meetings' setting. Does NOT "
                                "apply to COM-created meetings — use open_in_outlook "
                                "instead."
                            ),
                        },
                    },
                    "required": ["subject", "start"],
                },
            },
            {
                "name": "outlook_event_join_url",
                "description": (
                    "Read the Teams join URL from a calendar event by its entry_id — "
                    "use after outlook_create_event with open_in_outlook=true, once "
                    "the operator has added the Teams meeting and saved/sent it. "
                    "Returns the join URL (empty if none is attached yet). You can "
                    "then reuse the link elsewhere (e.g. open_url, a message)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the event (from outlook_create_event).",
                        },
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "outlook_respond_invite",
                "description": (
                    "Respond to a meeting invitation in your mailbox — accept, "
                    "decline, or tentative. Sends the response to the organizer."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the meeting invite (from outlook_inbox/outlook_calendar).",
                        },
                        "response": {
                            "type": "string",
                            "enum": ["accept", "tentative", "decline"],
                            "description": "Your response.",
                        },
                    },
                    "required": ["entry_id", "response"],
                },
            },
            {
                "name": "outlook_mark_read",
                "description": (
                    "Mark an Outlook message read (default) or unread by entry_id — "
                    "the 'handled' flag when closing out a helpdesk message."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the message.",
                        },
                        "read": {
                            "type": "boolean",
                            "description": "True to mark read (default), false to mark unread.",
                        },
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "outlook_categorize",
                "description": (
                    "Tag an Outlook message with one or more categories (e.g. "
                    "'Triaged', 'Outage'). Adds to existing categories unless replace "
                    "is set."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the message.",
                        },
                        "categories": {
                            "type": "string",
                            "description": "Category name(s), comma-separated.",
                        },
                        "replace": {
                            "type": "boolean",
                            "description": "Replace existing categories instead of adding (default false).",
                        },
                    },
                    "required": ["entry_id", "categories"],
                },
            },
            {
                "name": "outlook_move",
                "description": (
                    "File an Outlook message into another folder (e.g. 'Tickets/Closed'). "
                    "Folder path resolves within the same mailbox unless mailbox is given."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "entry_id of the message to move.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Destination folder path (e.g. 'Tickets/Closed').",
                        },
                        "mailbox": {
                            "type": "string",
                            "description": "Mailbox address; omit for the monitored/default account.",
                        },
                    },
                    "required": ["entry_id", "folder"],
                },
            },
        ]

    def _run_outlook_tool(self, name: str, ti: dict[str, Any]) -> str:
        """Execute an Outlook Classic tool via desktop COM and return a compact
        result. Reads return JSON rows; draft/send return a one-line summary (the
        autonomy gate has already approved a write)."""
        import json as _json

        from .sources import outlook as ol

        try:
            if name == "outlook_inbox":
                rows = ol.list_messages(
                    str(ti.get("mailbox") or ""),
                    str(ti.get("folder") or ""),
                    bool(ti.get("unread_only", True)),
                    int(ti.get("count") or 20),
                )
                return _json.dumps({"messages": rows, "count": len(rows)}, default=str)
            if name == "outlook_search":
                query = str(ti.get("query") or "").strip()
                if not query:
                    return "Error: 'query' is required for outlook_search."
                rows = ol.search_messages(
                    query,
                    str(ti.get("mailbox") or ""),
                    str(ti.get("folder") or ""),
                    int(ti.get("count") or 20),
                )
                return _json.dumps({"messages": rows, "count": len(rows)}, default=str)
            if name == "outlook_get_message":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_get_message."
                return _json.dumps(ol.get_message(eid), default=str)
            if name == "outlook_calendar":
                rows = ol.upcoming_events(
                    int(ti.get("days") or 1), int(ti.get("count") or 20)
                )
                if rows is None:
                    return "Could not read the Outlook calendar (is Outlook running?)."
                return _json.dumps({"events": rows, "count": len(rows)}, default=str)
            if name == "outlook_reminders":
                rows = ol.list_reminders(int(ti.get("count") or 20))
                if rows is None:
                    return "Could not read Outlook reminders (is Outlook running?)."
                return _json.dumps({"reminders": rows, "count": len(rows)}, default=str)
            if name == "outlook_resolve_recipient":
                who = str(ti.get("name") or "").strip()
                if not who:
                    return "Error: 'name' is required for outlook_resolve_recipient."
                return _json.dumps(ol.resolve_recipient(who), default=str)
            if name == "outlook_draft":
                body = str(ti.get("body") or "")
                fm = str(ti.get("from_mailbox") or "") or None
                if ti.get("reply_to"):
                    res = ol.save_reply_draft(
                        str(ti["reply_to"]), body, fm, bool(ti.get("reply_all", False))
                    )
                else:
                    to = str(ti.get("to") or "")
                    if not to:
                        return "Error: provide reply_to (an entry_id) or to (a recipient) for outlook_draft."
                    res = ol.save_new_draft(
                        to,
                        str(ti.get("subject") or ""),
                        body,
                        str(ti.get("cc") or ""),
                        fm or "",
                    )
                return f"✓ Draft saved to Drafts (entry_id {res.get('entry_id')}): to {res.get('to')} — {res.get('subject')}"
            if name == "outlook_send":
                if ti.get("entry_id"):
                    res = ol.send_draft(str(ti["entry_id"]))
                else:
                    to = str(ti.get("to") or "")
                    if not to:
                        return "Error: provide entry_id (a saved draft) or to/subject/body for outlook_send."
                    res = ol.send_new_mail(
                        to,
                        str(ti.get("subject") or ""),
                        str(ti.get("body") or ""),
                        str(ti.get("cc") or ""),
                        str(ti.get("from_mailbox") or ""),
                    )
                return f"✓ Sent: to {res.get('to')} — {res.get('subject', '')}"
            if name == "outlook_save_attachment":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_save_attachment."
                res = ol.save_attachment(
                    eid,
                    str(ti.get("name") or ""),
                    int(ti.get("index") or 0),
                    str(ti.get("dest_dir") or ""),
                )
                return (
                    f"✓ Saved attachment '{res.get('name')}' "
                    f"({res.get('size')} bytes) to {res.get('path')}"
                )
            if name == "outlook_create_event":
                subject = str(ti.get("subject") or "").strip()
                if not subject:
                    return "Error: 'subject' is required for outlook_create_event."
                start = _parse_dt(ti.get("start"))
                if start is None:
                    return (
                        "Error: 'start' must be an ISO datetime "
                        "(e.g. 2026-06-27T14:00) for outlook_create_event."
                    )
                end = _parse_dt(ti.get("end")) or (start + timedelta(minutes=30))
                rmin = ti.get("reminder_minutes")
                open_in_outlook = bool(ti.get("open_in_outlook"))
                res = ol.create_event(
                    subject,
                    start,
                    end,
                    attendees=str(ti.get("attendees") or ""),
                    location=str(ti.get("location") or ""),
                    body=str(ti.get("body") or ""),
                    reminder_minutes=int(rmin) if rmin is not None else 15,
                    online_meeting=bool(ti.get("online_meeting")),
                    open_in_outlook=open_in_outlook,
                )
                eid = res.get("entry_id")
                if res.get("opened"):
                    return (
                        f"✓ Opened in Outlook (unsent): {res.get('subject')} "
                        f"({res.get('start')} → {res.get('end')}; entry_id {eid}). "
                        "Click the 'Teams Meeting' button to add the join link, then "
                        "Send. Afterwards I can read the link back with "
                        "outlook_event_join_url."
                    )
                verb = (
                    "Invite sent" if res.get("sent") else "Event saved to your calendar"
                )
                teams = (
                    " (Teams online meeting enabled)"
                    if res.get("online_meeting")
                    else ""
                )
                lines = [
                    f"✓ {verb}{teams}: {res.get('subject')} "
                    f"({res.get('start')} → {res.get('end')}; entry_id {eid})"
                ]
                if res.get("online_meeting"):
                    join = str(res.get("join_url") or "")
                    if join:
                        lines.append(f"Teams join link: {join}")
                    else:
                        lines.append(
                            "Teams join link not attached — the add-in doesn't run "
                            "for COM-created meetings; use open_in_outlook=true to add "
                            "one by hand."
                        )
                return "\n".join(lines)
            if name == "outlook_event_join_url":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_event_join_url."
                res = ol.read_event_join_url(eid)
                join = str(res.get("join_url") or "")
                if join:
                    return f"Teams join link for '{res.get('subject')}': {join}"
                return (
                    f"No Teams join link on '{res.get('subject')}' yet — add the Teams "
                    "meeting in Outlook (the 'Teams Meeting' button) and save/send it "
                    "first."
                )
            if name == "outlook_respond_invite":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_respond_invite."
                try:
                    res = ol.respond_invite(eid, str(ti.get("response") or ""))
                except ValueError as exc:
                    return f"Error: {exc}"
                return f"✓ Responded '{res.get('response')}' to: {res.get('subject')}"
            if name == "outlook_mark_read":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_mark_read."
                res = ol.mark_message(eid, bool(ti.get("read", True)))
                state = "unread" if res.get("unread") else "read"
                return f"✓ Marked {state}: {res.get('subject')}"
            if name == "outlook_categorize":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_categorize."
                cats = str(ti.get("categories") or "").strip()
                if not cats:
                    return "Error: 'categories' is required for outlook_categorize."
                res = ol.categorize_message(eid, cats, bool(ti.get("replace", False)))
                return (
                    f"✓ Categorized [{', '.join(res.get('categories') or [])}]: "
                    f"{res.get('subject')}"
                )
            if name == "outlook_move":
                eid = str(ti.get("entry_id") or "").strip()
                if not eid:
                    return "Error: 'entry_id' is required for outlook_move."
                folder = str(ti.get("folder") or "").strip()
                if not folder:
                    return "Error: 'folder' is required for outlook_move."
                res = ol.move_message(eid, str(ti.get("mailbox") or ""), folder)
                return f"✓ Moved to {res.get('folder')}: {res.get('subject')}"
        except Exception as exc:  # noqa: BLE001 — surface COM failures to the agent
            return f"✗ Outlook {name} failed: {exc}"
        return f"Error: unknown Outlook tool '{name}'."

    def _repo_tools(self) -> list[dict[str, Any]]:
        """Schemas for the git-repo tools — list/read/grep/diff (read-only,
        autonomous) and sync/write (confirm-gated, local edits only, no push).
        Offered only when the git engine is available AND at least one repo is
        registered in runspec_repos.toml; empty list otherwise."""
        from . import repos

        if not repos.available():
            return []
        registered = repos.read_repos()
        if not registered:
            return []
        names = ", ".join(r["name"] for r in registered)
        hint = (
            f" Registered repos: {names}. Call repo_sync first to fetch a repo "
            "before reading it; edits are LOCAL ONLY (review with repo_diff — there "
            "is no commit/push)."
        )
        return [
            {
                "name": "repo_status",
                "description": (
                    "List the registered git repos and whether each is checked out "
                    "locally (with its synced ref/commit)." + hint
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "repo_sync",
                "description": (
                    "Clone (or fetch + re-checkout) a registered repo into its local "
                    "working copy at its configured ref. NOTE: re-syncing discards "
                    "uncommitted local edits (remote is source of truth)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered repo name (see repo_status).",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "repo_list",
                "description": "List files/dirs in a synced repo at a path (default repo root).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered repo name.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory path within the repo; omit for the root.",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "repo_grep",
                "description": (
                    "Search a synced repo's files for a pattern. The pattern is a "
                    "regular expression by default and matches anywhere on a line "
                    "(leading whitespace/tabs never matter); set fixed=true to match "
                    "it as a literal string (regex metacharacters like ( ) . [ are "
                    "escaped). Search is case-sensitive unless ignore_case=true. Use "
                    "glob to restrict to file types (e.g. '*.py' or '*.ts,*.tsx') — "
                    "faster on large trees. If the result is 'truncated', narrow the "
                    "pattern, glob, or path and search again."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered repo name.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Regex to search for (or a literal string when fixed=true).",
                        },
                        "path": {
                            "type": "string",
                            "description": "Limit the search to this subdirectory (optional).",
                        },
                        "glob": {
                            "type": "string",
                            "description": (
                                "Only scan files matching this shell-style glob; "
                                "comma-separate several, e.g. '*.py' or '*.ts,*.tsx' "
                                "(optional)."
                            ),
                        },
                        "ignore_case": {
                            "type": "boolean",
                            "description": "Case-insensitive search (default false).",
                        },
                        "fixed": {
                            "type": "boolean",
                            "description": (
                                "Match the pattern literally instead of as a regex "
                                "(default false)."
                            ),
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max matches to return (default 100, hard cap 200).",
                        },
                    },
                    "required": ["name", "pattern"],
                },
            },
            {
                "name": "repo_read",
                "description": "Read a file from a synced repo, optionally a 1-based line range.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered repo name.",
                        },
                        "path": {
                            "type": "string",
                            "description": "File path within the repo.",
                        },
                        "start": {
                            "type": "integer",
                            "description": "First line (1-based) to return (optional).",
                        },
                        "end": {
                            "type": "integer",
                            "description": "Last line to return (optional).",
                        },
                    },
                    "required": ["name", "path"],
                },
            },
            {
                "name": "repo_diff",
                "description": (
                    "Show the working-tree changes in a synced repo (unified diff vs "
                    "the synced commit) — review local edits before applying them."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered repo name.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Limit the diff to this path prefix (optional).",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "repo_write",
                "description": (
                    "Create or overwrite a file in a synced repo's LOCAL working copy "
                    "(no commit, no push — review with repo_diff). Provide the full new "
                    "file content."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered repo name.",
                        },
                        "path": {
                            "type": "string",
                            "description": "File path within the repo to write.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full new file content.",
                        },
                    },
                    "required": ["name", "path", "content"],
                },
            },
        ]

    def _run_repo_tool(
        self,
        name: str,
        ti: dict[str, Any],
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Execute a git-repo tool. Reads return JSON; sync/write return a one-line
        summary (the autonomy gate has already approved a side-effecting call).

        ``cancel_event``/``on_progress`` (set when the agent runs this) make the
        slow ops — ``repo_sync`` (clone/fetch) and ``repo_grep`` — interruptible
        and let them stream progress; a set ``cancel_event`` surfaces as
        ``ToolCancelled``, re-raised so the tracking wrapper reports it."""
        import json as _json

        from . import repos

        repo_name = str(ti.get("name") or "").strip()
        if name == "repo_status":
            rows = [
                {
                    "name": r["name"],
                    "url": r["url"],
                    "ref": r["ref"],
                    "checked_out": repos.is_checked_out(r["name"]),
                    "description": r.get("description", ""),
                }
                for r in repos.read_repos()
            ]
            return _json.dumps({"repos": rows, "count": len(rows)}, default=str)

        if not repo_name:
            return f"Error: 'name' is required for {name}."
        entry = repos.find_repo(repo_name)
        if entry is None:
            return (
                f"Error: no repo named {repo_name!r} is registered (see repo_status)."
            )
        dest = repos.checkout_dir(entry["name"])

        try:
            if name == "repo_sync":
                auth = self._sync_auth(entry["url"], entry.get("credential_id", ""))
                res = repos.sync_repo(
                    entry["url"],
                    entry["ref"],
                    auth,
                    dest,
                    cancel_event=cancel_event,
                    on_progress=on_progress,
                )
                return (
                    f"✓ {res['action']} {entry['name']} @ {res['ref']} "
                    f"({res['commit'][:10]}) → {res['path']}"
                )
            # All other ops need a checkout first.
            if not repos.is_checked_out(entry["name"]):
                return f"Error: {entry['name']} is not checked out yet — run repo_sync first."
            if name == "repo_list":
                rows = repos.list_dir(dest, str(ti.get("path") or ""))
                return _json.dumps({"entries": rows, "count": len(rows)}, default=str)
            if name == "repo_grep":
                pattern = str(ti.get("pattern") or "")
                if not pattern:
                    return "Error: 'pattern' is required for repo_grep."
                req_max = int(ti.get("max_results") or 100)
                rows = repos.grep_repo(
                    dest,
                    pattern,
                    str(ti.get("path") or ""),
                    req_max,
                    cancel_event=cancel_event,
                    on_progress=on_progress,
                    ignore_case=bool(ti.get("ignore_case")),
                    fixed=bool(ti.get("fixed")),
                    glob=str(ti.get("glob") or ""),
                )
                payload: dict[str, Any] = {"matches": rows, "count": len(rows)}
                if len(rows) >= min(max(1, req_max), repos._GREP_MAX_RESULTS):
                    payload["truncated"] = True
                    payload["hint"] = (
                        "Result cap reached — more matches may exist. Narrow with a "
                        "more specific pattern, a 'glob' (e.g. '*.py'), or a 'path'."
                    )
                return _json.dumps(payload, default=str)
            if name == "repo_read":
                path = str(ti.get("path") or "")
                if not path:
                    return "Error: 'path' is required for repo_read."
                res = repos.read_file(
                    dest, path, int(ti.get("start") or 0), int(ti.get("end") or 0)
                )
                return _json.dumps(res, default=str)
            if name == "repo_diff":
                res = repos.diff_repo(dest, str(ti.get("path") or ""))
                return _json.dumps(res, default=str)
            if name == "repo_write":
                path = str(ti.get("path") or "")
                if not path:
                    return "Error: 'path' is required for repo_write."
                res = repos.write_file(dest, path, str(ti.get("content") or ""))
                verb = "Created" if res["created"] else "Updated"
                return (
                    f"✓ {verb} {entry['name']}/{res['path']} ({res['bytes']} bytes) "
                    "in the local working copy — review with repo_diff (not committed)."
                )
        except ToolCancelled:
            raise  # let the tracking wrapper report the stop (not a tool failure)
        except Exception as exc:  # noqa: BLE001 — surface git/FS failures to the agent
            return f"✗ Repo {name} failed: {exc}"
        return f"Error: unknown repo tool '{name}'."

    def _denylist_tool(self) -> dict[str, Any] | None:
        """Schema for the built-in ``manage_denylist`` tool — a fleet kill switch.

        Runs ``runspec deny`` over SSH on the selected hosts (one call per venv),
        writing each host's ``runspec_denylist.toml``. ``runspec`` is the core
        binary (never disabled by its own denylist), so ``clear`` always
        re-enables and this can never lock the console out. Returns ``None`` when
        no host has a runspec path to act on."""
        targets = [h["name"] for h in self._hosts if utils_mod._paths(h)]
        if not targets:
            return None
        return {
            "name": "manage_denylist",
            "description": (
                "Inspect or change which runspec runnables are disabled on fleet "
                "hosts — a kill switch. action='list' is READ-ONLY and runs "
                "without a prompt: use it to see what's currently disabled (a run "
                "you attempt may be refused by the denylist, so check here first "
                "if a runnable is unexpectedly blocked). The mutating actions are "
                "confirm-gated. It writes each host's venv denylist via `runspec "
                "deny` over SSH — disabling the *runnables* on those hosts, not "
                "the console — and re-enabling (action='clear') always works "
                "because the core `runspec` binary is exempt from its own "
                "denylist. Actions: 'list' (inspect), 'add' (disable specific "
                "runnable/user/host globs), 'all' (disable every runnable), "
                "'clear' (re-enable everything), 'mode' (set enforce|warn|off)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "all", "clear", "mode"],
                        "description": "What to do to the denylist on each target host.",
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": targets},
                        "description": (
                            "Fleet hosts to apply to. Omit or leave empty to apply "
                            "to ALL hosts. Each host's every venv is updated."
                        ),
                    },
                    "runnable": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "action='add': runnable-name globs to disable (e.g. "
                            "['deploy', 'restart-*']). Omit ⇒ all runnables."
                        ),
                    },
                    "user": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "action='add': username globs the rule applies to. Omit ⇒ all users.",
                    },
                    "host": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "action='add': hostname globs the rule applies to. Omit ⇒ all hosts.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "action='add'/'all': reason recorded and shown on the refusal.",
                    },
                    "setting": {
                        "type": "string",
                        "enum": ["enforce", "warn", "off"],
                        "description": "action='mode': enforcement level for the denylist.",
                    },
                },
                "required": ["action"],
            },
        }

    def _status_tools(self) -> list[dict[str, Any]]:
        """Schema for the read-only ``console_status`` tool — lets the agent read
        the **current local date and time** plus the working-hours rota (inside/
        outside hours) and configured triggers/schedules on demand, e.g. before
        running something in a long session where the clock may have crossed a
        working-hours boundary since the turn began."""
        return [
            {
                "name": "console_status",
                "description": (
                    "Report the live console state: the current local date and "
                    "**time** (the system prompt carries only the date, so call "
                    "this when you need the exact wall-clock time), the "
                    "working-hours rota (on/off and whether it is currently INSIDE "
                    "or OUTSIDE working hours), plus the configured triggers and "
                    "schedules. Read-only. Use it to check the time or re-check "
                    "working hours before running, scheduling, or triggering "
                    "something — especially in a long conversation where the clock "
                    "may have moved on."
                ),
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

    def _run_status_tool(self, ti: dict[str, Any]) -> str:
        """Execute ``console_status`` — return the live rota + triggers + schedules
        snapshot (the autonomy gate has already let it run; it has no side effects).

        Leads with the **current local date and time**: the per-turn system prompt
        carries only the date (kept date-only so the prompt cache stays warm), so
        this on-demand tool is where the agent gets the exact wall-clock time —
        e.g. before scheduling or running something time-sensitive."""
        now = datetime.now().astimezone()
        tz = now.strftime("%Z")
        time_line = (
            "Current local date & time: "
            f"{now.strftime('%A, %Y-%m-%d %H:%M')}{f' {tz}' if tz else ''}."
        )
        snapshot = self._console_context() or (
            "Working-hours rota: OFF — automation runs at any time. No triggers or "
            "schedules are configured."
        )
        snapshot = time_line + "\n\n" + snapshot
        # Zero-cost pointer (no fleet sweep): make the kill switch discoverable from
        # the status tool so the agent knows where to look when a run is blocked.
        if any(utils_mod._paths(h) for h in self._hosts):
            snapshot += (
                "\n\nRunnables may also be disabled by the fleet denylist (a kill "
                "switch). Call manage_denylist with action='list' to see what is "
                "currently disabled, on which hosts."
            )
        return snapshot

    def _workspace_tools(self) -> list[dict[str, Any]]:
        """Schemas for the per-session workspace file tools. The workspace is a
        sandboxed folder attached to the current chat conversation (or
        trigger/schedule turn); when a runnable's output is too large to inline
        (above ``[workspace] spill_threshold``) the raw bytes are written to
        ``tool-results/{tool}-{inv_id}.{json|txt}`` in this workspace, and the
        model reaches for these tools instead of re-running the runnable. All
        are autonomous (read-only or scoped to the workspace)."""
        return [
            {
                "name": "workspace_info",
                "description": (
                    "Report this session's workspace: its id (name) and the "
                    "**absolute path** on the console machine, plus the console "
                    "documents directory. Call this first when you need to tell the "
                    "operator where a file is, or before open_file / "
                    "reveal_in_folder — those take a path relative to one of these "
                    "roots, so you don't need to guess the absolute location."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "workspace_ls",
                "description": (
                    "List entries in the current session's workspace folder "
                    "(the sandbox that holds large tool outputs spilled to "
                    "disk). Start here — ``tool-results/`` holds anything a "
                    "runnable emitted above the inline threshold, and "
                    "``notes/`` holds files you have written. Returns a list "
                    "of ``{name, type, size, modified}`` rows. Pass "
                    "sort='modified' to put the newest file first — handy to "
                    "grab the most recent spill (e.g. ls 'tool-results' "
                    "sort='modified')."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative directory to list (empty "
                                "or '.' = workspace root, 'tool-results' for "
                                "spilled outputs)."
                            ),
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["name", "modified"],
                            "description": (
                                "Order files by name (default) or by "
                                "most-recently-modified first."
                            ),
                        },
                    },
                },
            },
            {
                "name": "workspace_read",
                "description": (
                    "Read a page of lines from a workspace file — use this to "
                    "inspect a spilled tool output without pulling the whole "
                    "thing into the model context. Returns ``{path, offset, "
                    "limit, total_lines, total_bytes, truncated, content}``."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative file path (e.g. "
                                "'tool-results/local__list-processes-abc123.json')."
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "description": "0-based line number to start at (default 0).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max lines to return (default 200, hard cap 2000).",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "workspace_grep",
                "description": (
                    "Search files in the workspace and return matching lines "
                    "(``{path, line, text}``) — the fast way to find the "
                    "interesting rows in a big spilled log/JSON without reading "
                    "every page. The pattern is a regex by default; set fixed=true "
                    "to match it literally (metacharacters like ( ) . [ escaped). "
                    "Search is case-sensitive unless ignore_case=true. Use glob to "
                    "restrict to files (e.g. 'tool-results/*.json' or '*.log'). If "
                    "the result is 'truncated', narrow the pattern, glob, or path."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Regex (e.g. 'ERROR|WARN', 'user_id.*42'), or a "
                                "literal string when fixed=true."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative file or directory to search "
                                "(default: whole workspace)."
                            ),
                        },
                        "glob": {
                            "type": "string",
                            "description": (
                                "Only scan files matching this shell-style glob; "
                                "comma-separate several, e.g. 'tool-results/*.json' "
                                "or '*.log,*.txt' (optional)."
                            ),
                        },
                        "ignore_case": {
                            "type": "boolean",
                            "description": "Case-insensitive search (default false).",
                        },
                        "fixed": {
                            "type": "boolean",
                            "description": (
                                "Match the pattern literally instead of as a regex "
                                "(default false)."
                            ),
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Cap on matches (default 100, hard cap 200).",
                        },
                        "context": {
                            "type": "integer",
                            "description": "Lines of context to include before/after each match (0-5).",
                        },
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "workspace_head",
                "description": (
                    "First N lines of a workspace file. Same shape as "
                    "``workspace_read`` but skips the offset arg."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative file path.",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "How many lines (default 50, hard cap 500).",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "workspace_tail",
                "description": (
                    "Last N lines of a workspace file — good for seeing how "
                    "a spilled run ended without pulling the whole thing."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative file path.",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "How many lines (default 50, hard cap 500).",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "workspace_write",
                "description": (
                    "Write an agent-authored file into the workspace (e.g. "
                    "``notes/summary.md``, ``notes/extract.json``) — use this "
                    "to save a distilled view of a spilled output, split a "
                    "big file into smaller pieces, or capture intermediate "
                    "work. Cannot write into ``tool-results/`` (that dir is "
                    "system-owned, holding the raw runnable output)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative path (e.g. 'notes/summary.md'). "
                                "Parent dirs are created."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "Text to write.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        ]

    def _exec_tools(self) -> list[dict[str, Any]]:
        """Schemas for the workspace code-execution tools (``run_python`` /
        ``run_sql``). Returns ``[]`` when ``[workspace] code_exec`` is false, so a
        locked-down deployment never sees them (mirrors ``_outlook_tools`` when
        Outlook is unavailable). ``run_python`` is confirm-gated; ``run_sql`` runs
        read-only queries autonomously and confirm-gates writes (see
        ``_effective_tool_autonomy``)."""
        if not config_mod.read_workspace().get("code_exec", True):
            return []
        return [
            {
                "name": "run_python",
                "description": (
                    "Run a short Python script in the current session's workspace "
                    "(the sandbox that holds spilled tool outputs). The working "
                    "directory is the workspace, so you can read a spilled "
                    "'tool-results/…' file, reshape or join data, compute a result, "
                    "and print it. Use this to process what tools/prompts hand you "
                    "instead of doing it by hand. Runs with the console's own "
                    "Python (its installed packages are importable). stdout/stderr "
                    "and the exit code are returned; large output spills to the "
                    "workspace. This runs real code on the machine, so it always "
                    "asks the operator to approve first. **Provide exactly one of "
                    "`code` (the Python source as a string) or `file` (a workspace "
                    "path) — the call fails if you send neither.**"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": (
                                "Python source to run (fed on stdin). Provide this "
                                "OR 'file', not both."
                            ),
                        },
                        "file": {
                            "type": "string",
                            "description": (
                                "Workspace-relative path of a .py file to run "
                                "instead of inline 'code'."
                            ),
                        },
                    },
                },
            },
            {
                "name": "run_sql",
                "description": (
                    "Run a SQL statement against a SQLite database file inside the "
                    "current session's workspace. Read-only queries "
                    "(SELECT/EXPLAIN/PRAGMA) run without asking; anything that "
                    "writes (INSERT/UPDATE/DELETE/CREATE/…) asks the operator to "
                    "approve first. Returns ``{columns, rows, rowcount, "
                    "returned, truncated}``; rows past 'max_rows' are dropped."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "db": {
                            "type": "string",
                            "description": (
                                "Workspace-relative path of the SQLite database "
                                "file (e.g. 'notes/data.db')."
                            ),
                        },
                        "query": {
                            "type": "string",
                            "description": "The SQL statement to execute.",
                        },
                        "max_rows": {
                            "type": "integer",
                            "description": (
                                f"Max rows to return (default {_EXEC_MAX_ROWS})."
                            ),
                        },
                    },
                    "required": ["db", "query"],
                },
            },
        ]

    def _current_workspace_root(self, chat_id: str = "") -> Any:
        """The workspace ``Path`` for whichever session (chat / trigger /
        schedule) is currently active on this Bridge, or ``None`` when no
        session id has been set — the tools then return a clear error rather
        than falling back to a shared / unbounded path.

        ``chat_id`` (the per-turn id) picks the trigger/schedule mapping the
        active isolated-agent turn registered; chat turns leave it empty and
        the resolver falls back to the persistent conversation id."""
        session_id = ""
        if chat_id:
            with self._lock:
                ws_map = getattr(self, "_workspace_by_chat", {})
                session_id = ws_map.get(chat_id, "")
        if not session_id:
            session_id = getattr(self, "_conversation_id", "")
        if not session_id:
            return None
        return config_mod.workspace_dir(session_id)

    def _run_workspace_tool(
        self,
        name: str,
        ti: dict[str, Any],
        chat_id: str = "",
        cancel_event: Any = None,
        on_progress: Any = None,
    ) -> str:
        """Dispatch the ``workspace_*`` tools. All are pure filesystem
        operations inside the current session's workspace root — no network,
        no subprocess."""
        from . import workspaces

        root = self._current_workspace_root(chat_id)
        if root is None:
            return json.dumps(
                {
                    "error": (
                        "No active session workspace — this tool is available "
                        "inside a chat conversation or a trigger/schedule "
                        "agent turn (each of which owns a per-session "
                        "sandbox), not stand-alone."
                    )
                }
            )
        root.mkdir(parents=True, exist_ok=True)
        try:
            if name == "workspace_info":
                docs = config_mod.app_dir() / "documents"
                return json.dumps(
                    {
                        "session_id": root.name,
                        "workspace_root": str(root),
                        "documents_dir": str(docs),
                        "subdirs": [
                            d.name
                            for d in sorted(root.iterdir())
                            if d.is_dir() and d.name not in workspaces._SKIP_DIRS
                        ]
                        if root.exists()
                        else [],
                    }
                )
            if name == "workspace_ls":
                return json.dumps(
                    {
                        "root": str(root),
                        "entries": workspaces.list_dir(
                            root,
                            str(ti.get("path") or ""),
                            sort=str(ti.get("sort") or "name"),
                        ),
                    }
                )
            if name == "workspace_read":
                return json.dumps(
                    workspaces.read_file(
                        root,
                        str(ti.get("path") or ""),
                        offset=int(ti.get("offset") or 0),
                        limit=int(ti.get("limit") or 200),
                    )
                )
            if name == "workspace_grep":
                pattern = str(ti.get("pattern") or "")
                if not pattern:
                    return json.dumps({"error": "workspace_grep needs a pattern"})
                req_max = int(ti.get("max_results") or 100)
                hits = workspaces.grep(
                    root,
                    pattern,
                    rel=str(ti.get("path") or ""),
                    max_results=req_max,
                    context=int(ti.get("context") or 0),
                    ignore_case=bool(ti.get("ignore_case")),
                    fixed=bool(ti.get("fixed")),
                    glob=str(ti.get("glob") or ""),
                )
                payload: dict[str, Any] = {"matches": hits, "count": len(hits)}
                if len(hits) >= min(max(1, req_max), workspaces._GREP_MAX_RESULTS):
                    payload["truncated"] = True
                    payload["hint"] = (
                        "Result cap reached — more matches may exist. Narrow with a "
                        "more specific pattern, a 'glob' (e.g. 'tool-results/*.json'),"
                        " or a 'path'."
                    )
                return json.dumps(payload)
            if name == "workspace_head":
                return json.dumps(
                    workspaces.head(
                        root,
                        str(ti.get("path") or ""),
                        lines=int(ti.get("lines") or 50),
                    )
                )
            if name == "workspace_tail":
                return json.dumps(
                    workspaces.tail(
                        root,
                        str(ti.get("path") or ""),
                        lines=int(ti.get("lines") or 50),
                    )
                )
            if name == "workspace_write":
                return json.dumps(
                    workspaces.write_file(
                        root,
                        str(ti.get("path") or ""),
                        str(ti.get("content") or ""),
                    )
                )
        except (
            FileNotFoundError,
            NotADirectoryError,
            IsADirectoryError,
            PermissionError,
            ValueError,
        ) as e:
            return json.dumps({"error": str(e)})
        except OSError as e:
            return json.dumps({"error": f"filesystem error: {e}"})
        return json.dumps({"error": f"unknown workspace tool: {name}"})

    def _run_exec_tool(
        self,
        name: str,
        ti: dict[str, Any],
        chat_id: str = "",
        cancel_event: Any = None,
        on_progress: Any = None,
    ) -> str:
        """Dispatch ``run_python`` / ``run_sql``, rooted in the current session's
        workspace. The autonomy gate has already approved this call (confirm-gated
        for run_python and for a writing run_sql). Large output spills to the
        workspace just like a big runnable result, so it never blows the model
        context. ``cancel_event`` makes a long ``run_python`` interruptible."""
        import uuid

        from . import codeexec, workspaces

        root = self._current_workspace_root(chat_id)
        if root is None:
            return json.dumps(
                {
                    "error": (
                        "No active session workspace — this tool is available "
                        "inside a chat conversation or a trigger/schedule agent "
                        "turn (each of which owns a per-session sandbox), not "
                        "stand-alone."
                    )
                }
            )
        root.mkdir(parents=True, exist_ok=True)
        try:
            if name == "run_python":
                code = ti.get("code")
                file_rel = ti.get("file")
                # A model (often via a schema-loose gateway) sometimes emits the
                # call with no `code`. Return a directive, self-describing error —
                # naming the keys we did receive — so the agent recovers in one
                # step instead of retrying the same empty call.
                if bool(code) == bool(file_rel):
                    return json.dumps(
                        {
                            "error": (
                                "run_python: no Python source received. Call it "
                                "again with the 'code' argument set to the Python "
                                "source (a string), or 'file' set to a "
                                "workspace-relative .py path — provide exactly one."
                            ),
                            "received_keys": sorted(str(k) for k in ti),
                        }
                    )
                if on_progress is not None:
                    on_progress("running python…")
                result = codeexec.run_python(
                    root,
                    code=code,
                    file_rel=file_rel,
                    timeout_s=config_mod.read_workspace().get("exec_timeout", 30),
                    cancel=(
                        (lambda: bool(cancel_event and cancel_event.is_set()))
                        if cancel_event is not None
                        else None
                    ),
                )
            elif name == "run_sql":
                query = str(ti.get("query") or "")
                if not query.strip():
                    return json.dumps({"error": "run_sql needs a query"})
                db_path = workspaces.jail(root, str(ti.get("db") or ""))
                if not db_path.is_file():
                    return json.dumps(
                        {"error": f"no such workspace database: {ti.get('db')}"}
                    )
                try:
                    max_rows = int(ti.get("max_rows") or _EXEC_MAX_ROWS)
                except (TypeError, ValueError):
                    max_rows = _EXEC_MAX_ROWS
                result = codeexec.run_sql(
                    db_path,
                    query,
                    read_only=codeexec.classify_sql(query) == "read",
                    max_rows=max(1, max_rows),
                )
            else:
                return json.dumps({"error": f"unknown exec tool: {name}"})
        except ToolCancelled:
            raise  # handled by _run_builtin_tool_tracked → "⏹ stopped"
        except ValueError as e:  # path-jail escape / bad args
            return json.dumps({"error": str(e)})
        except FileNotFoundError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:  # noqa: BLE001 — surface any exec failure as a result
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

        try:
            payload = json.dumps(result)
        except (TypeError, ValueError):
            payload = json.dumps({"error": "exec result was not JSON-serialisable"})
        # Spill a large exec result to the workspace, same as a big runnable
        # output, so it never blows the model context. A spill failure (disk
        # full, unwritable results dir) must not fail the tool call — fall back
        # to returning the payload inline, mirroring the runnable-output path.
        try:
            threshold = config_mod.read_workspace().get("spill_threshold", 32_768)
            if threshold and len(payload) > threshold:
                envelope, _rel = workspaces.spill_output(
                    root,
                    name,
                    uuid.uuid4().hex[:12],
                    payload,
                    head_bytes=_SPILL_ENVELOPE_HEAD_BYTES,
                )
                return envelope
        except OSError:
            pass
        return payload

    def _memory_tools(self) -> list[dict[str, Any]]:
        """Schemas for the persistent-memory / runbook tools. Always available (the
        stores are local) — so the agent accrues knowledge even with no remote hosts.
        `load_runbook`'s id is surfaced as an enum (with titles) so the model picks a
        real runbook; it's omitted when none are saved."""
        from . import memory

        tools: list[dict[str, Any]] = [
            {
                "name": "remember",
                "description": (
                    "Save a durable fact about an application for future runs — where "
                    "a log lives, what a recurring error means, which service to check. "
                    "Cheap to call; it persists locally and is recalled later via "
                    "search_memory. Set pin=true for a high-value fact you want folded "
                    "into every future chat's context (use sparingly)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The fact to remember, one sentence.",
                        },
                        "app": {
                            "type": "string",
                            "description": "Application/system this fact is about (groups related facts). Defaults to 'general'.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional keywords to aid later recall.",
                        },
                        "pin": {
                            "type": "boolean",
                            "description": "Pin into every future chat's context (only the most useful facts).",
                        },
                    },
                    "required": ["fact"],
                },
            },
            {
                "name": "search_memory",
                "description": (
                    "Recall previously remembered facts about an application by keyword "
                    "before investigating from scratch — saves re-deriving what a past "
                    "run already learned. Returns the most relevant saved facts."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords to search remembered facts for.",
                        },
                        "app": {
                            "type": "string",
                            "description": "Optional: restrict to one application's facts.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "save_runbook",
                "description": (
                    "Save a reusable runbook — a named procedure with the symptom it "
                    "addresses and the ordered steps (which runnables to call). "
                    "Shareable across the team via Config Sync. This both creates and "
                    "edits: saving with an existing id overwrites that runbook, so to "
                    "edit one, load_runbook it, change the fields, and save_runbook "
                    "with the same id."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "Short id/slug (blank → derived from title). Reuse an "
                                "existing id to edit/overwrite that runbook."
                            ),
                        },
                        "title": {"type": "string", "description": "Human title."},
                        "symptom": {
                            "type": "string",
                            "description": "When to use this runbook.",
                        },
                        "steps": {
                            "type": "string",
                            "description": "Ordered steps, naming which runnables to call.",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "steps"],
                },
            },
        ]
        runbooks = memory.list_runbooks()
        if runbooks:
            ids = [s["id"] for s in runbooks]
            menu = "; ".join(
                f"'{s['id']}'" + (f" — {s['title']}" if s.get("title") else "")
                for s in runbooks
            )
            tools.append(
                {
                    "name": "load_runbook",
                    "description": (
                        "Load a saved runbook's full steps by id, to follow an "
                        f"established procedure for a known symptom. Available: {menu}."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "enum": ids,
                                "description": "Which runbook to load.",
                            }
                        },
                        "required": ["id"],
                    },
                }
            )
        return tools

    def _action_tools(self) -> list[dict[str, Any]]:
        """Schemas for the built-in 'action' tools an agent-mode trigger (or chat)
        uses to *act* beyond running runnables: write a markdown document, follow a
        saved runbook, create a schedule, emit an event into the trigger engine, or
        open a URL in the operator's browser. All are side-effecting, so the autonomy
        gate prompts before each (see ``_ACTION_TOOL_NAMES`` in
        ``_effective_tool_autonomy``)."""
        from . import memory

        remote = [h["name"] for h in self._hosts if h.get("ssh")]
        tools: list[dict[str, Any]] = [
            {
                "name": "create_document",
                "description": (
                    "Write a markdown document to a file. Always saves a copy on "
                    "the local console machine; when dest is a fleet host it also "
                    "uploads it there. Use it to capture a summary, report, or note."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "File name, e.g. 'incident-2026-06-22.md'.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The markdown content to write.",
                        },
                        "dest": {
                            "type": "string",
                            "enum": ["local", *remote],
                            "description": (
                                "Where to write it: 'local' (the console machine) "
                                "or a fleet host to also upload it to. Default local."
                            ),
                        },
                        "dest_dir": {
                            "type": "string",
                            "description": (
                                "Destination directory. Required for a remote host; "
                                "optional locally (defaults to the console docs dir)."
                            ),
                        },
                    },
                    "required": ["filename", "content"],
                },
            },
            {
                "name": "open_url",
                "description": (
                    "Open a web URL in the operator's default browser on the "
                    "console machine — e.g. a Teams meeting join link, a PR, or a "
                    "doc. Only http(s) URLs are allowed. Opening a Teams "
                    "meetup-join link hands off to the Teams desktop app."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The http(s) URL to open.",
                        },
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "open_file",
                "description": (
                    "Open a file from the session workspace (or the console "
                    "documents dir) in its default app on the console machine. "
                    "Restricted to passive viewer types ("
                    + ", ".join(sorted(_OPENABLE_EXTS))
                    + ") — for anything else use reveal_in_folder so the operator "
                    "opens it themselves. 'path' is relative to the chosen location "
                    "(see workspace_info)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to the location root (e.g. "
                                "'notes/report.pdf' or 'tool-results/out.json')."
                            ),
                        },
                        "location": {
                            "type": "string",
                            "enum": ["workspace", "documents"],
                            "description": (
                                "Which root the path is under: 'workspace' (this "
                                "session's sandbox, default) or 'documents' (where "
                                "create_document writes locally)."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "reveal_in_folder",
                "description": (
                    "Show a file or folder from the session workspace (or the "
                    "console documents dir) in the operator's file manager, "
                    "highlighting it — the operator then decides whether to open "
                    "it. Any file type is allowed (it is only revealed, never "
                    "opened/executed), so use this for anything open_file refuses. "
                    "'path' is relative to the chosen location (see workspace_info)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to the location root (a file or a "
                                "directory)."
                            ),
                        },
                        "location": {
                            "type": "string",
                            "enum": ["workspace", "documents"],
                            "description": (
                                "Which root the path is under: 'workspace' "
                                "(default) or 'documents'."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
        ]

        runbooks = memory.list_runbooks()
        if runbooks:
            menu = "; ".join(
                f"'{s['id']}'" + (f" — {s['title']}" if s.get("title") else "")
                for s in runbooks
            )
            tools.append(
                {
                    "name": "run_runbook",
                    "description": (
                        "Load a saved runbook by id and follow its steps as part of "
                        "this turn. Returns the runbook's rendered steps for you to "
                        f"carry out with your other tools. Available: {menu}."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "runbook_id": {
                                "type": "string",
                                "enum": [s["id"] for s in runbooks],
                                "description": "Which runbook to run.",
                            },
                            "values": {
                                "type": "object",
                                "description": "Values for the runbook's {{name}} parameters.",
                            },
                            "extra_instructions": {
                                "type": "string",
                                "description": "Optional extra context to append.",
                            },
                        },
                        "required": ["runbook_id"],
                    },
                }
            )

        from . import savedruns as savedruns_mod

        saved_runs = [r for r in savedruns_mod.load_saved_runs() if r.get("id")]
        if saved_runs:

            def _sr_line(r: dict[str, Any]) -> str:
                label = f"'{r['id']}'"
                if r.get("title"):
                    label += f" — {r['title']}"
                try:
                    target = savedruns_mod.parse_saved_run(r).target_summary()
                except Exception:
                    target = "?"
                params = sorted((r.get("params") or {}).keys())
                extra = f" (params: {', '.join(params)})" if params else ""
                return f"{label} → /{r.get('runnable')} on {target}{extra}"

            sr_menu = "; ".join(_sr_line(r) for r in saved_runs)
            tools.append(
                {
                    "name": "run_saved_run",
                    "description": (
                        "Run a saved run by id — a deterministic preset (runnable + "
                        "target hosts + fixed args) with optional open params you "
                        "fill via 'values'. No agent steps: the preset runs exactly "
                        "as stored, gated by the underlying runnable's autonomy "
                        f"(one grouped confirm for a multi-host preset). Available: "
                        f"{sr_menu}."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "enum": [r["id"] for r in saved_runs],
                                "description": "Which saved run to fire.",
                            },
                            "values": {
                                "type": "object",
                                "description": (
                                    "Values for the saved run's open params "
                                    "(required ones must be supplied)."
                                ),
                            },
                        },
                        "required": ["id"],
                    },
                }
            )

        # Selectors for the schedule's targets, so the agent picks valid values
        # (the runnable "tool", the host, and the host group) rather than guessing
        # a string. Distinct runnable names across the fleet; current host names;
        # configured group names. Best-effort — an empty list just omits the enum.
        try:
            with self._lock:
                runnable_names = sorted(
                    {str(r.get("name")) for r in self._runnables_cache if r.get("name")}
                )
        except Exception:
            runnable_names = []
        host_names = [str(h.get("name")) for h in self._hosts if h.get("name")]
        try:
            group_names = sorted(self._groups_map().keys())
        except Exception:
            group_names = []
        tools.append(
            {
                "name": "create_schedule",
                "description": (
                    "Create a recurring schedule that fires on a cron expression or "
                    "a fixed interval. Pick an action: emit a matchable event into "
                    "the trigger engine (action='event'); run a runnable directly "
                    "(action='runnable'); run an agent turn on a fixed prompt so you "
                    "(the assistant) check or do something later (action='prompt'); "
                    "run a saved runbook as an agent turn (action='runbook'); or "
                    "fire a saved run — a deterministic preset with its own stored "
                    "targeting — (action='saved_run'). Use "
                    "'prompt' to schedule a reminder-to-act in natural language, "
                    "'runbook' to re-run an existing runbook on a cadence, and "
                    "'saved_run' to put an existing preset on a cadence. For a "
                    "runnable, target exactly one of: a single 'host', a 'group' "
                    "(runs on every member), 'hosts' (a candidate list — runs on the "
                    "first connected one), or 'selected_hosts' (an explicit list — "
                    "runs on every host in it)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Short id for the schedule.",
                        },
                        "action": {
                            "type": "string",
                            "enum": [
                                "event",
                                "runnable",
                                "prompt",
                                "runbook",
                                "saved_run",
                            ],
                            "description": (
                                "event = emit an event; runnable = run a runnable; "
                                "prompt = run an agent turn on 'prompt'; runbook = run "
                                "the saved runbook named by 'runbook' as an agent turn; "
                                "saved_run = fire the saved run named by 'saved_run' "
                                "deterministically (its stored targeting decides the "
                                "hosts)."
                            ),
                        },
                        "when": {
                            "type": "object",
                            "description": (
                                "Timing: exactly one of cron='M H D M W' or "
                                "every='15m', plus optional timezone (IANA name)."
                            ),
                            "properties": {
                                "cron": {"type": "string"},
                                "every": {"type": "string"},
                                "timezone": {"type": "string"},
                            },
                        },
                        "event": {
                            "type": "string",
                            "description": "Event name (action='event').",
                        },
                        "fields": {
                            "type": "object",
                            "description": "Event payload fields (action='event').",
                        },
                        "host": {
                            "type": "string",
                            **({"enum": host_names} if host_names else {}),
                            "description": (
                                "Single host to run on (action='runnable'). Use one "
                                "of host / group / hosts / selected_hosts."
                            ),
                        },
                        "group": {
                            "type": "string",
                            **({"enum": group_names} if group_names else {}),
                            "description": (
                                "Host group to run on every member of "
                                "(action='runnable'). Use one of host / group / "
                                "hosts / selected_hosts."
                            ),
                        },
                        "hosts": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                **({"enum": host_names} if host_names else {}),
                            },
                            "description": (
                                "Candidate hosts — runs on the first connected one "
                                "(failover, action='runnable'). Use one of host / "
                                "group / hosts / selected_hosts."
                            ),
                        },
                        "selected_hosts": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                **({"enum": host_names} if host_names else {}),
                            },
                            "description": (
                                "Explicit list of hosts — runs on every host in the "
                                "list (fan-out, one run per host, action='runnable'). "
                                "Use for an ad-hoc multi-host run that doesn't warrant "
                                "a registered host group. Use one of host / group / "
                                "hosts / selected_hosts."
                            ),
                        },
                        "runnable": {
                            "type": "string",
                            **({"enum": runnable_names} if runnable_names else {}),
                            "description": "Runnable name (action='runnable').",
                        },
                        "args": {
                            "type": "object",
                            "description": "Runnable args (action='runnable').",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "The instruction the agent turn runs on "
                                "(action='prompt'). Write it as a task to do later, "
                                "e.g. 'Check disk usage on the web hosts and flag "
                                "anything over 80%.'"
                            ),
                        },
                        "runbook": {
                            "type": "string",
                            **(
                                {"enum": [s["id"] for s in runbooks]}
                                if runbooks
                                else {}
                            ),
                            "description": "Saved runbook id to run (action='runbook').",
                        },
                        "saved_run": {
                            "type": "string",
                            **(
                                {"enum": [r["id"] for r in saved_runs]}
                                if saved_runs
                                else {}
                            ),
                            "description": (
                                "Saved-run id to fire (action='saved_run')."
                            ),
                        },
                        "values": {
                            "type": "object",
                            "description": (
                                "Values for the runbook's {{name}} parameters "
                                "(action='runbook') or the saved run's open params "
                                "(action='saved_run')."
                            ),
                        },
                    },
                    "required": ["id", "action", "when"],
                },
            }
        )

        tools.append(
            {
                "name": "emit_event",
                "description": (
                    "Emit a matchable event into the trigger engine now. Triggers "
                    "with source='schedule' and a matching event type pick it up "
                    "(fan-out) — use it to hand work to other triggers."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event": {
                            "type": "string",
                            "description": "Event type name to emit.",
                        },
                        "fields": {
                            "type": "object",
                            "description": "Payload fields the event carries.",
                        },
                    },
                    "required": ["event"],
                },
            }
        )
        return tools

    def _run_action_tool(self, name: str, ti: dict[str, Any], chat_id: str = "") -> str:
        """Execute a built-in action tool (the gate has already approved it).
        ``create_document`` writes/uploads markdown; ``run_runbook`` renders a saved
        runbook inline (NOT a spawned sub-agent — keeps the turn single-level);
        ``create_schedule``/``emit_event`` drive the scheduler + trigger engine."""
        if name == "create_document":
            return self._action_create_document(ti)
        if name == "run_runbook":
            return self._action_run_runbook(ti)
        if name == "run_saved_run":
            return self._action_run_saved_run(ti, chat_id)
        if name == "create_schedule":
            return self._action_create_schedule(ti)
        if name == "emit_event":
            return self._action_emit_event(ti)
        if name == "open_url":
            return self._action_open_url(ti)
        if name == "open_file":
            return self._action_open_file(ti, chat_id)
        if name == "reveal_in_folder":
            return self._action_reveal_in_folder(ti, chat_id)
        return f"Error: unknown tool '{name}'"

    def _resolve_jailed_path(self, location: str, rel: str, chat_id: str = "") -> Path:
        """Resolve ``rel`` under the chosen console-owned root — the session
        ``workspace`` (default) or the ``documents`` dir create_document writes to —
        and path-jail it (reusing ``workspaces.jail``: no ``..``/absolute/symlink
        escape). Raises ``ValueError`` on an unknown location, a missing workspace,
        or an escaping path — so open_file / reveal_in_folder can only ever touch
        files inside these two roots."""
        from . import workspaces

        loc = (location or "workspace").strip() or "workspace"
        if loc == "documents":
            root = config_mod.app_dir() / "documents"
        elif loc == "workspace":
            root = self._current_workspace_root(chat_id)
            if root is None:
                raise ValueError(
                    "no active session workspace — open_file/reveal_in_folder run "
                    "inside a chat or trigger/schedule turn"
                )
        else:
            raise ValueError(
                f"unknown location {loc!r} (use 'workspace' or 'documents')"
            )
        root.mkdir(parents=True, exist_ok=True)
        return workspaces.jail(root, rel)

    def _action_open_file(self, ti: dict[str, Any], chat_id: str = "") -> str:
        """Open a jailed, passive-viewer file in its default app (gate approved).
        Refuses anything outside the two console-owned roots, a directory, or a
        non-allowlisted extension — the last so "open" can't become code execution
        (the operator uses reveal_in_folder for those)."""
        rel = str(ti.get("path") or "").strip()
        if not rel:
            return "Error: 'path' is required."
        location = str(ti.get("location") or "workspace").strip() or "workspace"
        try:
            target = self._resolve_jailed_path(location, rel, chat_id)
        except ValueError as exc:
            return f"Error: {exc}"
        if not target.exists():
            return f"Error: {rel} does not exist in the {location}."
        if target.is_dir():
            return "Error: that is a directory — use reveal_in_folder for a folder."
        if not _openable_ext(target.name):
            return (
                f"Error: opening '{target.suffix or target.name}' is not allowed. "
                f"Openable types: {', '.join(sorted(_OPENABLE_EXTS))}. "
                "Use reveal_in_folder to show it in the file manager instead."
            )
        try:
            _launch_open(str(target))
        except Exception as exc:  # noqa: BLE001
            return f"✗ Could not open the file: {exc}"
        return f"✓ Opened {rel} ({location}) in its default app."

    def _action_reveal_in_folder(self, ti: dict[str, Any], chat_id: str = "") -> str:
        """Reveal a jailed file/folder in the OS file manager (gate approved). No
        extension restriction — revealing never opens/executes, so it is the safe
        path for the file types open_file refuses; the operator decides."""
        rel = str(ti.get("path") or "").strip()
        if not rel:
            return "Error: 'path' is required."
        location = str(ti.get("location") or "workspace").strip() or "workspace"
        try:
            target = self._resolve_jailed_path(location, rel, chat_id)
        except ValueError as exc:
            return f"Error: {exc}"
        if not target.exists():
            return f"Error: {rel} does not exist in the {location}."
        try:
            _launch_reveal(str(target))
        except Exception as exc:  # noqa: BLE001
            return f"✗ Could not reveal the item: {exc}"
        return f"✓ Revealed {rel} ({location}) in the file manager."

    def _action_open_url(self, ti: dict[str, Any]) -> str:
        """Open a web URL in the operator's default browser (the gate has already
        approved it). Restricted to http(s) so it can't be steered into a local file
        or a custom protocol handler. Windows hands a Teams meetup-join link off to
        the Teams desktop app via its protocol handler."""
        import webbrowser

        url = str(ti.get("url") or "").strip()
        if not url:
            return "Error: 'url' is required."
        if not _is_web_url(url):
            return "Error: only http(s) URLs can be opened."
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:  # noqa: BLE001
            return f"✗ Could not open the URL: {exc}"
        if not opened:
            return f"✗ No browser was available to open {url}."
        return f"✓ Opened {url} in the default browser."

    def _action_create_document(self, ti: dict[str, Any]) -> str:
        content = str(ti.get("content") or "")
        filename = os.path.basename(str(ti.get("filename") or "").strip())
        if not filename:
            return "Error: 'filename' is required."
        if not content.strip():
            return "Error: 'content' is empty — nothing to write."
        dest = str(ti.get("dest") or "local").strip() or "local"
        dest_dir = str(ti.get("dest_dir") or "").strip()
        local_dir = (
            dest_dir
            if (dest == "local" and dest_dir)
            else str(config_mod.app_dir() / "documents")
        )
        try:
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            local_path = Path(local_dir) / filename
            local_path.write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return f"✗ Could not write {filename} locally: {exc}"
        if dest == "local":
            return f"✓ Wrote {filename} to {local_dir}."
        if not dest_dir:
            return "Error: dest_dir is required when dest is a host."
        # Upload the local copy to the host via the normal transfer path.
        res = self.start_transfer("local", [str(local_path)], dest, dest_dir)
        if res.get("error"):
            return (
                f"✗ Wrote {filename} locally, but could not start upload to "
                f"{dest}: {res['error']}"
            )
        tid = res.get("id")
        detail = self._await_transfer(tid) if tid else None
        if detail is None:
            return (
                f"Wrote {filename} locally; upload to {dest} started (id {tid}) — "
                "check the transfer panel for the result."
            )
        if detail.get("ok"):
            return (
                f"✓ Wrote {filename} to {local_dir} and uploaded it to {dest_dir} "
                f"on {dest}."
            )
        return (
            f"✓ Wrote {filename} locally, but upload to {dest} failed: "
            f"{detail.get('error') or 'unknown error'}"
        )

    def _action_run_runbook(self, ti: dict[str, Any]) -> str:
        from . import memory, runbookargs

        runbook = memory.load_runbook(str(ti.get("runbook_id") or ""))
        if not runbook:
            return f"Error: no runbook with id '{ti.get('runbook_id')}'."
        vals = dict(ti.get("values") or {})
        raw = runbook.get("args") if isinstance(runbook.get("args"), dict) else {}
        errors = runbookargs.validate_runbook_values(raw, vals)
        if errors:
            return "Error: " + "; ".join(errors)
        body = memory.render_runbook_body(str(runbook.get("steps") or ""), vals)
        extra = str(ti.get("extra_instructions") or "").strip()
        text = f"{body}\n\n{extra}".strip() if extra else body.strip()
        if not text:
            return "Error: this runbook has an empty body."
        title = runbook.get("title") or runbook.get("id")
        return f"Runbook '{title}' — follow these steps now using your tools:\n\n{text}"

    def _action_run_saved_run(self, ti: dict[str, Any], chat_id: str = "") -> str:
        """Fire a saved run (the gate has already approved this call — the live
        agentic/sync gates intercept ``run_saved_run`` *before* the generic
        action-tool confirm and route it through the grouped-confirm machinery,
        so reaching here means the calls are authorised; a rule-trigger path
        lands here directly with the trigger's own authorization). Expands the
        preset to per-host calls and runs them in parallel."""
        entry = self._saved_run_entry(str(ti.get("id") or ""))
        if isinstance(entry, str):
            return entry
        calls, err = self._saved_run_calls(entry, dict(ti.get("values") or {}))
        if err:
            return err
        outs = self._run_calls_parallel(calls, chat_id)
        n = len(calls)
        header = (
            f"Ran saved run '{entry.id}' (/{entry.runnable}) on "
            f"{n} host{'' if n == 1 else 's'}:"
        )
        blocks = [
            f"── {tool_name.split('__', 1)[0]} ──\n{out}"
            for (tool_name, _), out in zip(calls, outs)
        ]
        return header + "\n\n" + "\n\n".join(blocks)

    def _action_create_schedule(self, ti: dict[str, Any]) -> str:
        from .sources.schedule import parse_schedule_entry

        sid = str(ti.get("id") or "").strip()
        if not sid:
            return "Error: 'id' is required."
        data: dict[str, Any] = {
            "id": sid,
            "action": str(ti.get("action") or "").strip(),
            "when": dict(ti.get("when") or {}),
        }
        for key in (
            "event",
            "fields",
            "host",
            "group",
            "hosts",
            "selected_hosts",
            "runnable",
            "args",
            "prompt",
            "runbook",
            "saved_run",
            "values",
        ):
            if ti.get(key) is not None:
                data[key] = ti[key]
        try:
            parse_schedule_entry(data)  # validate shape before persisting
        except Exception as exc:  # noqa: BLE001
            return f"Error: invalid schedule — {exc}"
        try:
            self.create_schedule(data)
        except Exception as exc:  # noqa: BLE001
            return f"✗ Could not create schedule: {exc}"
        return f"✓ Created schedule '{sid}'."

    def _action_emit_event(self, ti: dict[str, Any]) -> str:
        from .events import Event

        event = str(ti.get("event") or "").strip()
        if not event:
            return "Error: 'event' is required."
        engine = self._trigger_engine
        if engine is None:
            return "✗ The trigger engine isn't running — can't emit the event now."
        payload = {**dict(ti.get("fields") or {}), "event": event}
        try:
            engine.feed(Event.new("schedule", event, payload))
        except Exception as exc:  # noqa: BLE001
            return f"✗ Could not emit event: {exc}"
        return f"✓ Emitted event '{event}' into the trigger engine."

    def _run_memory_tool(self, name: str, ti: dict[str, Any]) -> str:
        """Execute a memory/runbook tool against the local stores (the gate has
        already cleared it as autonomous). Returns a short string the model reads."""
        from . import memory

        if name == "remember":
            fact = str(ti.get("fact") or "").strip()
            if not fact:
                return "Error: 'fact' is required."
            mid = memory.add_memory(
                fact,
                app=str(ti.get("app") or "general"),
                tags=list(ti.get("tags") or []),
                pin=bool(ti.get("pin")),
            )
            pinned = " (pinned into future context)" if ti.get("pin") else ""
            return f"Remembered{pinned}. id={mid}"
        if name == "search_memory":
            app = str(ti.get("app") or "").strip() or None
            hits = memory.search_memory(str(ti.get("query") or ""), app=app)
            if not hits:
                return "No matching memories."
            return json.dumps({"memories": hits})
        if name == "save_runbook":
            sid = memory.save_runbook(
                str(ti.get("id") or ""),
                title=str(ti.get("title") or ""),
                symptom=str(ti.get("symptom") or ""),
                steps=str(ti.get("steps") or ""),
                tags=list(ti.get("tags") or []),
            )
            if not sid:
                return "Error: a runbook needs a title (or an id) and steps."
            # Refresh the `$` menu / Forms tab / Runbooks editor live, the same way
            # the Settings editor does — otherwise an agent-saved runbook only shows
            # up after a console restart (when get_runbooks re-reads the file).
            self._dispatch("runspec:runbooks_changed", {"id": sid})
            return f"Saved runbook '{sid}'."
        if name == "load_runbook":
            runbook = memory.load_runbook(str(ti.get("id") or ""))
            if not runbook:
                return f"Error: no runbook with id '{ti.get('id')}'."
            return json.dumps(runbook)
        return f"Error: unknown tool '{name}'"

    def _run_custom_tool(
        self,
        name: str,
        ti: dict[str, Any],
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Execute a built-in file-transfer tool synchronously and report the
        outcome (the autonomy gate has already approved it). Kicks off the normal
        transfer — so the user sees live progress in the transfer panel — then
        blocks on the result via ``_await_transfer``."""
        if name == "manage_denylist":
            return self._run_manage_denylist(
                ti, cancel_event=cancel_event, on_progress=on_progress
            )
        filter_name = str(ti.get("filter") or "")
        filter_value = ti.get("filter_value")
        dest_dir = ti.get("dest_dir")
        dest_name = ti.get("dest_name")
        if name == "download_file":
            path = ti.get("path")
            if not path or not dest_dir:
                return "Error: path and dest_dir are required."
            host = ti.get("host")
            via, target = ti.get("source_via"), ti.get("source_target")
            if target:
                src: Any = {
                    "kind": "adhoc",
                    "host": target,
                    "jump": via or "",
                    "credential_id": ti.get("source_credential"),
                }
                src_label = f"{target} via {via}" if via else str(target)
            elif host:
                src = host
                src_label = str(host)
            else:
                return (
                    "Error: provide host (a fleet host), or source_target (an "
                    "ad-hoc host, with source_via only if a jump is needed)."
                )
            res = self.start_transfer(
                src, [path], "local", dest_dir, filter_name, filter_value, dest_name
            )
            landed = dest_name or os.path.basename(path)
            label = f"download of {landed} from {src_label}"
        elif name == "relay_file":
            path, dh = ti.get("path"), ti.get("dest_host")
            if not path or not dh or not dest_dir:
                return "Error: path, dest_host and dest_dir are required."
            sh = ti.get("source_host")
            via, target = ti.get("source_via"), ti.get("source_target")
            if target:
                source: dict[str, Any] = {
                    "kind": "adhoc",
                    "host": target,
                    "jump": via or "",
                    "credential_id": ti.get("source_credential"),
                }
                src_label = f"{target} via {via}" if via else str(target)
            elif sh:
                source = {"kind": "host", "host": sh}
                src_label = str(sh)
            else:
                return (
                    "Error: provide source_host (a fleet host), or source_target "
                    "(an ad-hoc host, with source_via only if a jump is needed)."
                )
            res = self.start_relay(
                source, [path], dh, dest_dir, filter_name, filter_value, dest_name
            )
            label = f"relay of {os.path.basename(path)} from {src_label} to {dh}"
        else:
            return f"Error: unknown tool '{name}'"

        if res.get("error"):
            return f"✗ Could not start {label}: {res['error']}"
        tid = res.get("id")
        detail = self._await_transfer(tid) if tid else None
        if detail is None:
            return f"Started {label} (id {tid}); still running — check the transfer panel for the result."
        if detail.get("ok"):
            via = f" through filter '{filter_name}'" if filter_name else ""
            return (
                f"✓ Completed {label}{via}. Wrote {detail.get('files', 0)} file(s) "
                f"to {dest_dir}."
            )
        return f"✗ {label} failed: {detail.get('error') or 'unknown error'}"

    def _run_manage_denylist(
        self,
        ti: dict[str, Any],
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Run ``runspec deny <action>`` on the selected fleet hosts (one call per
        venv), aggregating per-host/per-venv results. The autonomy gate has
        already approved this call.

        ``cancel_event``/``on_progress`` (set when the agent runs this) make the
        SSH fan-out interruptible between hosts and stream a per-host progress
        line; a set ``cancel_event`` raises ``ToolCancelled``."""
        import shlex
        import subprocess

        action = str(ti.get("action") or "").strip().lower()
        valid = {"list", "add", "all", "clear", "mode"}
        if action not in valid:
            return f"Error: action must be one of {', '.join(sorted(valid))}."

        # Build the tail of `runspec deny <action> ...` as an argv list.
        tail: list[str] = []
        if action == "add":
            for flag, key in (("-r", "runnable"), ("-u", "user"), ("-H", "host")):
                vals = ti.get(key) or []
                if isinstance(vals, str):
                    vals = [vals]
                for v in vals:
                    tail += [flag, str(v)]
            if ti.get("reason"):
                tail += ["--reason", str(ti["reason"])]
        elif action == "all":
            if ti.get("reason"):
                tail += ["--reason", str(ti["reason"])]
        elif action == "mode":
            setting = str(ti.get("setting") or "").strip().lower()
            if setting not in {"enforce", "warn", "off"}:
                return "Error: action='mode' requires setting = enforce | warn | off."
            tail += [setting]

        # Select target hosts (omitted/empty ⇒ all hosts that have a runspec path).
        wanted = ti.get("targets") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        wanted_set = {str(t) for t in wanted}
        hosts = [h for h in self._hosts if utils_mod._paths(h)]
        if wanted_set:
            hosts = [h for h in hosts if h.get("name") in wanted_set]
        if not hosts:
            return "Error: no matching fleet host has a runspec path to act on."

        lines: list[str] = []
        ok_count = 0
        total = len(hosts)
        for i, h in enumerate(hosts, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelled()
            host_name = str(h.get("name"))
            if on_progress is not None:
                on_progress(f"host {i} of {total}: {host_name}")
            ssh = h.get("ssh")
            idf = h.get("identityFile")
            for rp in utils_mod._paths(h):
                argv = [rp, "deny", action, *tail]
                venv = hosts_mod.venv_name(rp)
                try:
                    if ssh:
                        cmd = " ".join(shlex.quote(p) for p in argv)
                        code, out, err = self._ssh_run(
                            ssh, cmd, identity_file=idf, timeout=20
                        )
                    else:
                        proc = subprocess.run(
                            argv, capture_output=True, text=True, timeout=20
                        )
                        code, out, err = proc.returncode, proc.stdout, proc.stderr
                except Exception as exc:  # noqa: BLE001 — report any failure per venv
                    lines.append(f"✗ {host_name}/{venv}: {exc}")
                    continue
                body = (out or "").strip() or (err or "").strip() or f"exit {code}"
                if code == 0:
                    ok_count += 1
                    lines.append(f"✓ {host_name}/{venv}: {body}")
                else:
                    lines.append(f"✗ {host_name}/{venv}: {body}")

        header = f"runspec deny {action} — {ok_count}/{len(lines)} venv(s) ok across {len(hosts)} host(s):"
        return header + "\n" + "\n".join(lines)

    def _resolve_tool_target(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any] | None, str | None] | str:
        """Map an agent tool call back to its target, for both tool layouts.

        Returns ``(host, name, cached_runnable, host_key)`` — where ``host_key`` is
        the input key carrying the host (to pop before running), or ``None`` in
        per-host mode where the host lives in the tool name. On a routing failure
        returns an error string the agent can read and retry from.

        ``per_host``: parse ``host__name`` from the tool name.
        ``dedup``: recover the runnable by re-deriving each cached runnable's dedup
        tool name (robust against the lossy name sanitiser, and guaranteed to agree
        with the builder), then pick the host from the call's host argument."""
        with self._lock:
            runnables = list(self._runnables_cache)
        if self._tool_mode() == "per_host":
            if "__" not in tool_name:
                return f"Error: invalid tool name '{tool_name}'"
            host, name = tool_name.split("__", 1)
            cached_r = next(
                (
                    r
                    for r in runnables
                    if r.get("host") == host and r.get("name") == name
                ),
                None,
            )
            return host, name, cached_r, None
        # dedup: all runnables sharing this folder-qualified tool name
        matches = [r for r in runnables if self._dedup_tool_name(r) == tool_name]
        if not matches:
            # Fall back to the explicit host__name form so internal callers that
            # build a per-host tool name directly (e.g. the helpdesk portal enact
            # path) keep working under dedup mode, and so do triggers whose calls
            # name a specific host.
            if "__" in tool_name:
                host, name = tool_name.split("__", 1)
                cached_r = next(
                    (
                        r
                        for r in runnables
                        if r.get("host") == host and r.get("name") == name
                    ),
                    None,
                )
                if cached_r is not None:
                    return host, name, cached_r, None
            return f"Error: unknown tool '{tool_name}'"
        name = matches[0].get("name", "")
        hosts: list[str] = []
        for r in matches:
            h = r.get("host", "local")
            if h not in hosts:
                hosts.append(h)
        host_key = self._host_selector_key(matches[0])
        host = str((tool_input or {}).get(host_key) or "")
        if not host:
            if len(hosts) == 1:
                host = hosts[0]
            else:
                return (
                    f"Error: '{name}' runs on multiple hosts — set the '{host_key}' "
                    f"argument. Options: {', '.join(hosts)}."
                )
        if host not in hosts:
            return (
                f"Error: host '{host}' does not have '{name}'. "
                f"Options: {', '.join(hosts)}."
            )
        cached_r = next((r for r in matches if r.get("host") == host), None)
        return host, name, cached_r, host_key
