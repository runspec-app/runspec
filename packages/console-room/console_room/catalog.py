"""
catalog.py — the presence-aware live catalogue with per-agent affinity (pure).

The self-service portal a visitor browses is not one per-queue list; it is the
**live union** of the runnables published by the agent consoles that are *on
shift* in that queue, with **affinity** — which agents hold each runnable. A
runnable only one on-shift agent holds must route to that agent, and it vanishes
from the catalogue the moment that agent goes off shift (reappearing when they
return). See docs/design/runnables-portal.md §3.2.

This registry holds, per queue namespace, each *connected* agent's published tool
list and whether it is *on shift*, and answers the portal's two questions:

- :meth:`live` — the deduplicated union of on-shift agents' runnables (what a
  visitor sees).
- :meth:`holders` — the on-shift agents that hold a runnable (the routing
  candidates). :meth:`connected_holders` is the wider set that also includes
  off-shift-but-connected agents — the audience a page/chime targets.

**Connected** (the console has a live link) is independent of **on shift** (the
rota says it's available): a console stays connected when its rota flips it off
shift, so off-shift agents remain reachable by a page even though they drop out
of the live catalogue.

Agents are keyed by identity string within a namespace; in practice an agent's
identity is host-qualified (``agent@web01``), so one connected console maps to one
key. Pure (no I/O, no sockets) — the hub owns one registry and feeds it the
agents' catalogue / presence frames; the aggregation rules are unit-tested here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _tool_name(tool: Any) -> str:
    """The runnable name of a published tool entry (``""`` if malformed)."""
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name.strip()
    return ""


@dataclass
class _Entry:
    """One connected agent's contribution to a queue: what it published and
    whether the rota currently has it on shift."""

    tools: list[dict[str, Any]] = field(default_factory=list)
    on_shift: bool = True


class LiveCatalog:
    """Per-namespace, per-agent catalogue answering live-union + affinity queries.

    Every mutating method is synchronous and side-effect-free beyond the internal
    map, so the hub can call it inline between ``await``s (atomic on the event
    loop) and the rules are tested directly. Dictionaries preserve insertion
    order, so the union and the holder lists are deterministic (the order agents
    first appeared)."""

    def __init__(self) -> None:
        # namespace -> agent identity -> entry. Insertion order is the agents'
        # arrival order, which gives the union/holders a stable order.
        self._agents: dict[str, dict[str, _Entry]] = {}

    def _entry(self, namespace: str, agent: str) -> _Entry:
        """Get or create the entry for ``agent`` under ``namespace``."""
        return self._agents.setdefault(namespace, {}).setdefault(agent, _Entry())

    def publish(self, namespace: str, agent: str, tools: list[dict[str, Any]]) -> None:
        """Record (or replace) the runnables ``agent`` holds in ``namespace``. An
        empty list means the agent holds nothing right now — it stays connected
        but contributes nothing to the live catalogue. Presence is untouched: a
        re-publish from an already-known agent keeps its on-shift state."""
        self._entry(namespace, agent).tools = list(tools)

    def set_presence(self, namespace: str, agent: str, on_shift: bool) -> None:
        """Set whether ``agent`` is on shift (creating the entry if presence
        arrives before any catalogue). Off-shift agents stay in the registry —
        connected and pageable — but drop out of :meth:`live` / :meth:`holders`."""
        self._entry(namespace, agent).on_shift = bool(on_shift)

    def withdraw(self, namespace: str, agent: str) -> None:
        """Drop ``agent`` entirely (its console disconnected). Idempotent; prunes
        an emptied namespace so a queue with no consoles has no live catalogue."""
        agents = self._agents.get(namespace)
        if not agents:
            return
        agents.pop(agent, None)
        if not agents:
            self._agents.pop(namespace, None)

    def connected(self, namespace: str) -> list[str]:
        """Every agent with a live console in ``namespace`` (any shift)."""
        return list(self._agents.get(namespace, {}).keys())

    def on_shift_agents(self, namespace: str) -> list[str]:
        """The connected agents in ``namespace`` the rota currently has on shift."""
        return [a for a, e in self._agents.get(namespace, {}).items() if e.on_shift]

    def live(self, namespace: str) -> list[dict[str, Any]]:
        """The deduplicated union of the runnables held by the on-shift agents of
        ``namespace`` — the catalogue a visitor browses. Deduped by runnable name,
        first holder wins (its schema is the one shown); unnamed entries skipped."""
        seen: set[str] = set()
        union: list[dict[str, Any]] = []
        for entry in self._agents.get(namespace, {}).values():
            if not entry.on_shift:
                continue
            for tool in entry.tools:
                name = _tool_name(tool)
                if name and name not in seen:
                    seen.add(name)
                    union.append(tool)
        return union

    def holders(self, namespace: str, tool: str) -> list[str]:
        """The on-shift agents in ``namespace`` that hold ``tool`` — the routing
        candidates for a request. A runnable only one on-shift agent holds yields
        a single candidate; an empty list means no one on shift can serve it."""
        return self._holders(namespace, tool, on_shift_only=True)

    def connected_holders(self, namespace: str, tool: str) -> list[str]:
        """Every connected agent that holds ``tool``, on shift or not — the
        audience a page/chime targets when no on-shift holder is available."""
        return self._holders(namespace, tool, on_shift_only=False)

    def _holders(self, namespace: str, tool: str, *, on_shift_only: bool) -> list[str]:
        return [
            agent
            for agent, entry in self._agents.get(namespace, {}).items()
            if (entry.on_shift or not on_shift_only)
            and any(_tool_name(t) == tool for t in entry.tools)
        ]
