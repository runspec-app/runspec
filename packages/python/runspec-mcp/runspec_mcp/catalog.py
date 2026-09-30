"""
catalog.py — discover runnables across the fleet and hold them as a live tool set.

Discovery mirrors runspec-console: run ``runspec local --format json`` in each
configured venv (locally as a subprocess, remotely over the pooled SSH transport),
which carries the full spec — including each arg's ``env`` alias, which the MCP
schema drops but credential matching needs. Each discovered runnable is expanded
into leaf MCP tools by reusing ``runspec.serve._expand_tools`` /
``runspec.cli._build_schema``, so the tools are byte-for-byte what ``runspec serve``
on that host would advertise (subcommands flattened, ``x-autonomy`` present,
``password`` args dropped).

The catalog is a **mutable, lock-guarded registry**: a background thread re-runs
discovery on a cadence and swaps the tool set atomically; the server reads a
snapshot on every ``tools/list`` and emits ``notifications/tools/list_changed``
when the set actually changes.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import FleetConfig, local_runspec_paths
from .hosts import venv_name

logger = logging.getLogger("runspec-mcp.catalog")

# Runnables that are infrastructure, never surfaced as fleet tools.
_SELF_EXCLUDE = frozenset({"runspec-console", "runspec-mcp"})


@dataclass
class Tool:
    """One leaf MCP tool: its schema plus everything needed to route + execute it."""

    name: str  # namespaced MCP tool name (host__group__leaf, disambiguated)
    schema: dict[str, Any]  # the MCP tool object (name/description/inputSchema/x-autonomy…)
    host: dict[str, Any]  # the host entry (ssh target / jump / identity)
    runspec_path: str
    group: str
    runnable: str  # top-level runnable name
    command_path: list[str]  # subcommand path (leaf), [] for a bare runnable
    arg_spec: dict[str, Any]  # effective inferred args (carries env/position/autonomy)
    autonomy: str
    run_as: str = ""
    become_method: str = "sudo"
    become_flags: str | None = None

    @property
    def is_local(self) -> bool:
        return not str(self.host.get("ssh") or "").strip()

    @property
    def arg_env_names(self) -> list[str]:
        """Every env var name the leaf's args declare they read (for cred matching)."""
        out: list[str] = []
        for spec in self.arg_spec.values():
            env = spec.get("env")
            if isinstance(env, str):
                out.append(env)
            elif isinstance(env, list):
                out.extend(str(e) for e in env)
        return out

    @property
    def positionals(self) -> dict[str, int]:
        return {n: s["position"] for n, s in self.arg_spec.items() if s.get("position") is not None}


@dataclass
class HostStatus:
    name: str
    ssh: str
    connected: bool
    venvs: int
    tools: int
    error: str = ""


def _sanitize(part: str) -> str:
    """Make a name fragment safe for an MCP tool name ([A-Za-z0-9_-])."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", part).strip("_")


def _resolve_run_as(run_as: Any) -> str:
    """Simple run_as resolution: a string as-is, a table's ``default``.

    (Per-host/pattern resolution against the remote hostname is a refinement; the
    common cases — a plain user or a ``default`` — are covered here.)"""
    if isinstance(run_as, str):
        return run_as
    if isinstance(run_as, dict):
        return str(run_as.get("default", "") or "")
    return ""


def _item_run_as(item: dict[str, Any], spec: dict[str, Any]) -> str:
    """The account to escalate to for one ``runspec local --format json`` item.

    ``run_as`` applies in order runnable → ``[config] run_as`` → ``"@owner"``
    (SPEC → Remote Execution → run_as). ``"@owner"`` becomes the item's
    ``env_owner`` (core 0.68.0+; ``""`` when root-owned, absent on an older core
    ⇒ no escalation), and no escalation happens when the target is the account
    that listed the venv (``user`` — the login account)."""
    from runspec.become import OWNER, effective_run_as

    run_as = _resolve_run_as(effective_run_as(spec.get("run_as"), item.get("config_run_as")))
    if run_as == OWNER:
        owner = item.get("env_owner")
        run_as = owner if isinstance(owner, str) else ""
    if run_as and run_as == item.get("user"):
        return ""
    return run_as


def _discover_items(
    host: dict[str, Any],
    runspec_path: str,
    ssh_cfg: dict[str, Any],
    pool: Any,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """Run ``runspec local --format json`` in one venv; return its raw items."""
    ssh_target = str(host.get("ssh") or "").strip()
    if not ssh_target:
        try:
            result = subprocess.run(
                [runspec_path, "local", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []
        if result.returncode != 0:
            return []
        out = result.stdout
    else:
        identity = host.get("identity_file")
        cmd = f"{shlex.quote(runspec_path)} local --format json"
        code, out, _err = pool.run(ssh_target, cmd, identity_file=identity, global_ssh_config=ssh_cfg, timeout=timeout)
        if code != 0:
            return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def _build_host_tools(host: dict[str, Any], runspec_path: str, items: list[dict[str, Any]]) -> list[Tool]:
    """Expand one venv's discovered items into leaf Tools (serve-identical schemas)."""
    from runspec.inference import infer_script
    from runspec.serve import _expand_tools

    group = venv_name(runspec_path)
    tools: list[Tool] = []
    for item in items:
        rname = item.get("runnable")
        spec = item.get("spec")
        if not rname or not isinstance(spec, dict) or rname in _SELF_EXCLUDE:
            continue
        if spec.get("discoverable") is False:
            continue
        run_as = _item_run_as(item, spec)
        become_method = str(spec.get("become_method", "sudo") or "sudo")
        become_flags = spec.get("become_flags")
        try:
            inferred = infer_script(spec, item.get("config_autonomy") or "confirm")
            expanded = _expand_tools(rname, inferred, [], run_as, become_method, become_flags)
        except Exception:
            continue
        for flat_name, schema, arg_spec, exec_spec in expanded:
            tools.append(
                Tool(
                    name=flat_name,  # namespaced later, once host/venv multiplicity is known
                    schema=schema,
                    host=host,
                    runspec_path=runspec_path,
                    group=group,
                    runnable=rname,
                    command_path=list(exec_spec.get("command") or []),
                    arg_spec=arg_spec,
                    autonomy=str(schema.get("x-autonomy", "confirm")),
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                )
            )
    return tools


def _drop_invalid_schemas(registry: dict[str, Tool]) -> dict[str, Tool]:
    """Drop (and log) any tool whose ``inputSchema`` isn't valid draft 2020-12.

    The Anthropic API rejects the WHOLE ``tools`` array over one bad schema (``400
    tools.N.custom.input_schema: ... must match JSON Schema draft 2020-12``), so a
    single malformed runnable on any fleet host would otherwise make Claude Code
    see no fleet tools at all. Dropping just the offender keeps the rest usable;
    the log line names the tool + reason to fix it at the source."""
    from runspec.schemacheck import schema_issue

    keep: dict[str, Tool] = {}
    for name, tool in registry.items():
        reason = schema_issue((tool.schema or {}).get("inputSchema") or {})
        if reason:
            logger.warning(
                "not publishing tool %r — its inputSchema is not valid JSON Schema draft 2020-12 (%s); the rest of the fleet's tools are unaffected.",
                name,
                reason,
            )
            continue
        keep[name] = tool
    return keep


def _namespace(tools: list[Tool], multi_host: bool, venvs_per_host: dict[str, int]) -> dict[str, Tool]:
    """Assign each tool its final, collision-free MCP name.

    Host prefix when more than one host; group (venv) prefix when a host exposes
    more than one venv; then a numeric suffix as a last resort so no two tools
    ever share a name."""
    registry: dict[str, Tool] = {}
    for t in tools:
        parts: list[str] = []
        if multi_host:
            parts.append(_sanitize(str(t.host.get("name") or "host")))
        if venvs_per_host.get(str(t.host.get("name") or ""), 1) > 1:
            parts.append(_sanitize(t.group))
        parts.append(t.name)
        base = "__".join(p for p in parts if p)
        name = base
        n = 2
        while name in registry:
            name = f"{base}_{n}"
            n += 1
        t.name = name
        t.schema = {**t.schema, "name": name}
        registry[name] = t
    return registry


class Catalog:
    """The live, thread-safe tool registry for the fleet."""

    def __init__(
        self,
        cfg: FleetConfig,
        pool: Any,
        on_change: Callable[[], None] | None = None,
    ):
        self._cfg = cfg
        self._pool = pool
        self._on_change = on_change
        self._lock = threading.Lock()
        self._tools: dict[str, Tool] = {}
        self._status: list[HostStatus] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── read side ──────────────────────────────────────────────────────────────

    def list_schemas(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.schema for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        with self._lock:
            return self._tools.get(name)

    def tools(self) -> list[Tool]:
        with self._lock:
            return list(self._tools.values())

    def status(self) -> list[HostStatus]:
        with self._lock:
            return list(self._status)

    # ── discovery ──────────────────────────────────────────────────────────────

    def _host_targets(self) -> list[tuple[dict[str, Any], list[str]]]:
        """(host_entry, runspec_paths) for every configured host, plus the synthetic
        local host carrying the ``[local] venv_globs`` venvs."""
        out: list[tuple[dict[str, Any], list[str]]] = []
        for h in self._cfg.hosts:
            paths = h.get("runspec_paths")
            if not paths and h.get("runspec_path"):
                paths = [h["runspec_path"]]
            out.append((h, [str(p) for p in (paths or [])]))
        local_paths = local_runspec_paths(self._cfg)
        if local_paths and not any(not str(h.get("ssh") or "").strip() for h in self._cfg.hosts):
            out.append(({"name": "local"}, local_paths))
        return out

    def refresh(self) -> bool:
        """Re-discover every host; swap the registry. Returns True if it changed."""
        targets = self._host_targets()
        host_names = {str(h.get("name") or "") for h, _ in targets}
        multi_host = len(host_names) > 1
        venvs_per_host = {str(h.get("name") or ""): len(paths) for h, paths in targets}

        all_tools: list[Tool] = []
        status: list[HostStatus] = []
        for host, paths in targets:
            connected = bool(paths)
            venv_tools = 0
            error = ""
            for rp in paths:
                items = _discover_items(host, rp, self._cfg.ssh, self._pool)
                built = _build_host_tools(host, rp, items)
                if not items and str(host.get("ssh") or "").strip():
                    error = "no runnables discovered (unreachable or empty venv)"
                    connected = False
                all_tools.extend(built)
                venv_tools += len(built)
            status.append(
                HostStatus(
                    name=str(host.get("name") or ""),
                    ssh=str(host.get("ssh") or ""),
                    connected=connected,
                    venvs=len(paths),
                    tools=venv_tools,
                    error=error,
                )
            )

        registry = _drop_invalid_schemas(_namespace(all_tools, multi_host, venvs_per_host))
        new_sig = _signature(registry)
        with self._lock:
            changed = new_sig != _signature(self._tools)
            self._tools = registry
            self._status = status
        if changed and self._on_change is not None:
            self._on_change()
        return changed

    # ── background loop ────────────────────────────────────────────────────────

    def start_background(self) -> None:
        if self._cfg.refresh_interval <= 0:
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="fleet-discovery", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._cfg.refresh_interval):
            try:
                self.refresh()
            except Exception:
                pass  # discovery is best-effort; never kill the loop

    def stop(self) -> None:
        self._stop.set()


def _signature(registry: dict[str, Tool]) -> Any:
    """A hashable fingerprint of the tool set — name + schema — for change detection."""
    return tuple(sorted((name, json.dumps(t.schema, sort_keys=True, default=str)) for name, t in registry.items()))
