"""catalog.py — the runnables installed in this venv, shaped for the form.

Discovery is in-process and reads the same sources ``runspec local`` does
(installed distributions that depend on ``runspec`` and ship a
``runspec.toml``), with core's own inference applied, so the TUI sees exactly
the interface ``parse()`` will enforce. Nothing here touches the terminal.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runspec.become import effective_run_as, escalation_target, resolve_run_as
from runspec.execute import find_script
from runspec.inference import infer_script
from runspec.loader import load_raw


@dataclass
class Node:
    """One level of a runnable: the root, or a command under it.

    ``args`` is the *effective* set — the ancestors' args (tagged
    ``inherited``) merged with this node's own, child winning on a name clash —
    which is what core's ``_resolve_subcommand`` parses at this depth."""

    name: str
    path: tuple[str, ...]
    description: str
    autonomy: str
    args: list[dict[str, Any]]
    groups: dict[str, Any]
    commands: dict[str, Node] = field(default_factory=dict)
    require_command: bool = False

    def at(self, path: Iterable[str]) -> Node:
        node = self
        for seg in path:
            node = node.commands[seg]
        return node

    @property
    def can_run(self) -> bool:
        """A node runs at its own level unless it requires a command."""
        return not (self.require_command and self.commands)


@dataclass
class Runnable:
    name: str
    package: str
    source: Path
    root: Node
    run_as: str
    discoverable: bool
    raw: dict[str, Any]

    @property
    def description(self) -> str:
        return self.root.description

    @property
    def autonomy(self) -> str:
        return self.root.autonomy


def build_node(
    name: str,
    spec: dict[str, Any],
    path: tuple[str, ...] = (),
    inherited: list[dict[str, Any]] | None = None,
    inherited_groups: dict[str, Any] | None = None,
) -> Node:
    """A :class:`Node` from an *inferred* spec (``inference.infer_script``)."""
    own = [{**arg, "name": arg.get("name") or arg_name} for arg_name, arg in (spec.get("args") or {}).items()]
    own_names = {a["name"] for a in own}
    args = [a for a in (inherited or []) if a["name"] not in own_names] + own
    groups = {**(inherited_groups or {}), **(spec.get("groups") or {})}
    node = Node(
        name=name,
        path=path,
        description=spec.get("description") or "",
        autonomy=spec.get("autonomy") or "confirm",
        args=args,
        groups=groups,
        require_command=bool(spec.get("require_command")),
    )
    child_inherited = [{**a, "inherited": True} for a in args]
    for cmd_name, cmd_spec in (spec.get("commands") or {}).items():
        if isinstance(cmd_spec, dict):
            node.commands[cmd_name] = build_node(cmd_name, cmd_spec, (*path, cmd_name), child_inherited, groups)
    return node


def build_runnable(name: str, package: str, source: Path, raw: dict[str, Any]) -> Runnable:
    config = raw["config"]
    spec = raw["runnables"][name]
    inferred = infer_script(spec, config.get("autonomy_default") or "confirm")
    run_as = escalation_target(resolve_run_as(effective_run_as(spec.get("run_as"), config.get("run_as")), socket.gethostname()))
    return Runnable(
        name=name,
        package=package,
        source=source,
        root=build_node(name, inferred),
        run_as=run_as,
        discoverable=spec.get("discoverable") is not False,
        raw=raw,
    )


def _installed_specs() -> list[tuple[str, Path]]:
    """``(distribution name, runspec.toml)`` for every runspec-aware distribution.

    Uses the helpers behind ``runspec local``; they're private to core, so this
    is the one place that depends on them."""
    import importlib.metadata as meta

    from runspec.cli import _check_dist_files, _check_editable_source, _requires_runspec

    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for dist in meta.distributions():
        try:
            if not _requires_runspec(dist):
                continue
            items = _check_dist_files(dist) or _check_editable_source(dist)
        except Exception:  # noqa: BLE001 — one broken distribution must not hide the rest
            continue
        if not items:
            continue
        source = Path(items[0]["source"]).resolve()
        if source not in seen:
            seen.add(source)
            found.append((dist.metadata["Name"] or source.parent.name, source))
    return found


def discover(include_hidden: bool = False) -> list[Runnable]:
    """Every runnable in this venv that has an entry-point binary, sorted by
    package then name.

    A name defined by two packages is listed once — the first definition, the
    one ``runspec exec`` runs — and ``discoverable = false`` runnables are left
    out unless ``include_hidden``."""
    runnables: list[Runnable] = []
    names: set[str] = set()
    for package, source in _installed_specs():
        try:
            raw = load_raw(source)
        except Exception:  # noqa: BLE001 — a malformed spec is skipped, as `runspec local` does
            continue
        for name in raw["runnables"]:
            if name in names or find_script(name) is None:
                continue
            try:
                runnable = build_runnable(name, package, source, raw)
            except Exception:  # noqa: BLE001
                continue
            names.add(name)
            if runnable.discoverable or include_hidden:
                runnables.append(runnable)
    return sorted(runnables, key=lambda r: (r.package.lower(), r.name))


def by_package(runnables: Iterable[Runnable]) -> dict[str, list[Runnable]]:
    out: dict[str, list[Runnable]] = {}
    for r in runnables:
        out.setdefault(r.package, []).append(r)
    return out


def matches(runnable: Runnable, query: str) -> bool:
    """Case-insensitive search over name, description and command names."""
    q = query.strip().lower()
    if not q:
        return True
    hay = [runnable.name, runnable.description, runnable.package, *_command_names(runnable.root)]
    return any(q in h.lower() for h in hay)


def _command_names(node: Node) -> list[str]:
    out: list[str] = []
    for name, child in node.commands.items():
        out.append(name)
        out.extend(_command_names(child))
    return out
