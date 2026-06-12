"""
catalog.py — build a helpdesk queue's self-service catalog from discovered
runnables (pure; no I/O).

The console is the catalog authority: it already discovers runnables across the
fleet (SSH + ``runspec local``) and sees each runnable's ``discoverable``
audiences (H0). This module filters that cache to the runnables a *visitor* may
request — ``discoverable`` contains ``"self-service"`` — applies the optional
per-queue allowlist, and converts each to the catalog entry shape the room
server caches and the visitor frontend renders forms from:

    {"name": ..., "description": ..., "inputSchema": {JSON Schema}}

Conversion mirrors the core's ``runspec local --format mcp`` emit (`x-autonomy`
included, password args omitted) so the visitor form renderer sees the same
contract everywhere. Validation here is shape-level only — runspec itself
validates authoritatively when an agent console enacts a request.

See docs/design/helpdesk-rooms.md §5/§6.
"""

from __future__ import annotations

from typing import Any

OPERATOR = "operator"
SELF_SERVICE = "self-service"


def discoverable_audiences(value: Any) -> list[str]:
    """Canonical audience list for a raw ``discoverable`` value — the same
    normalisation as the core's ``runspec.discovery``: absent/``True`` →
    ``["operator"]``, ``False`` → ``[]``, a list → itself (strings only)."""
    if value is None or value is True:
        return [OPERATOR]
    if value is False:
        return []
    if isinstance(value, list):
        return [a for a in value if isinstance(a, str)]
    return [OPERATOR]


def is_self_service(runnable: dict[str, Any]) -> bool:
    """True if a discovered runnable is marked for the visitor catalog.

    ``discoverable`` travels in the runnable's ``rawSpec`` (discovery keeps the
    full spec minus args/commands), so this works for both discovery paths —
    and a host running an older runspec simply has no key → operator-only →
    never visitor-facing. Safe by default."""
    raw = runnable.get("rawSpec")
    value = raw.get("discoverable") if isinstance(raw, dict) else None
    return SELF_SERVICE in discoverable_audiences(value)


_TYPE_MAP = {
    "str": "string",
    "path": "string",
    "choice": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "flag": "boolean",
}


def arg_to_property(arg: dict[str, Any]) -> dict[str, Any]:
    """One discovered ArgDef → a JSON-Schema property for the visitor form."""
    prop: dict[str, Any] = {"type": _TYPE_MAP.get(str(arg.get("type")), "string")}
    if arg.get("description"):
        prop["description"] = str(arg["description"])
    if arg.get("default") is not None:
        prop["default"] = arg["default"]
    if arg.get("options"):
        prop["enum"] = list(arg["options"])
    rng = arg.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        prop["minimum"], prop["maximum"] = rng[0], rng[1]
    return prop


def runnable_to_catalog_entry(runnable: dict[str, Any]) -> dict[str, Any]:
    """One discovered runnable → a catalog tool entry (visitor-form contract)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in runnable.get("args", []):
        # Secrets never cross the visitor boundary — same rule as the core's
        # agent-schema emit: a password comes from the operator/environment.
        if str(arg.get("type")) == "password":
            continue
        name = str(arg.get("name"))
        properties[name] = arg_to_property(arg)
        if arg.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {
        "name": str(runnable.get("name")),
        "description": str(runnable.get("description") or ""),
        "x-autonomy": str(runnable.get("autonomy") or "confirm"),
        "inputSchema": schema,
    }


def build_catalog(
    runnables: list[dict[str, Any]],
    *,
    allowlist: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The catalog for a queue: ``(tools, index)``.

    ``tools`` is what the console publishes to the room (the ``catalog`` frame's
    payload); ``index`` maps each published bare tool name back to the discovered
    runnable (host/group/name) so a visitor request can later be enacted.

    Filters, in order: ``self-service``-marked only (the H0 opt-in); no
    subcommand-requiring runnables (the visitor form is flat in v1); the optional
    per-queue ``allowlist`` of bare names (the narrowing knob — ``None`` means
    all marked). Names collide across hosts on a fleet; first discovered wins,
    keeping the catalog stable for visitors."""
    allowed = set(allowlist) if allowlist is not None else None
    tools: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for runnable in runnables:
        name = str(runnable.get("name") or "")
        if not name or name in index:
            continue
        if not is_self_service(runnable):
            continue
        if runnable.get("requireCommand"):
            continue
        if allowed is not None and name not in allowed:
            continue
        tools.append(runnable_to_catalog_entry(runnable))
        index[name] = runnable
    return tools, index
