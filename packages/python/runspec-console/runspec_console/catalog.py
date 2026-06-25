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


def runnable_to_catalog_entry(
    runnable: dict[str, Any], *, audience: str = SELF_SERVICE
) -> dict[str, Any]:
    """One discovered runnable → a catalog tool entry (visitor-form contract).

    ``audience`` stamps the entry's ``x-audience`` (``"self-service"`` | ``"operator"``)
    so the room can serve operator-only entries to operator sessions and the
    self-service subset to visitors. ``x-kind`` is ``"runnable"`` — the room and
    frontend tell runnables and skills apart by it."""
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
        "x-kind": "runnable",
        "x-audience": audience,
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


def build_full_catalog(
    runnables: list[dict[str, Any]],
    *,
    self_service_allowlist: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The combined catalog for a queue: ``(tools, index)`` covering **both**
    audiences in one tagged list (the channel the room filters per session).

    Each runnable yields **one** entry, tagged with the broadest audience it
    qualifies for: ``"self-service"`` when it is self-service-marked *and* passes the
    optional ``self_service_allowlist`` (the ``room_catalog`` visitor-narrowing
    knob) — visible to everyone; otherwise ``"operator"`` when it is
    operator-discoverable (the default audience) — visible only to operator sessions.
    A runnable discoverable to nobody (``discoverable = false``) is dropped. One
    entry per name avoids a name collision in the room's name-keyed dedup; the
    operator set is deliberately *not* narrowed by the self-service allowlist.

    Same flat-form constraints as :func:`build_catalog`: subcommand-requiring
    runnables are skipped and password args are omitted. Names collide across hosts
    on a fleet; first discovered wins. ``index`` maps every published name back to
    its discovered runnable for enactment."""
    allowed = (
        set(self_service_allowlist) if self_service_allowlist is not None else None
    )
    tools: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for runnable in runnables:
        name = str(runnable.get("name") or "")
        if not name or name in index:
            continue
        if runnable.get("requireCommand"):
            continue
        raw = runnable.get("rawSpec")
        audiences = discoverable_audiences(
            raw.get("discoverable") if isinstance(raw, dict) else None
        )
        self_service = SELF_SERVICE in audiences and (
            allowed is None or name in allowed
        )
        if self_service:
            audience = SELF_SERVICE
        elif OPERATOR in audiences:
            audience = OPERATOR
        else:
            continue  # discoverable to nobody
        tools.append(runnable_to_catalog_entry(runnable, audience=audience))
        index[name] = runnable
    return tools, index


# ── skills (published runbooks) ──────────────────────────────────────────────


def skill_audiences(skill: dict[str, Any]) -> list[str]:
    """The audiences a runbook is published to, from its ``discoverable`` field.

    Unlike a runnable, a skill is **opt-in**: absent / ``False`` / unknown → ``[]``
    (not published), so an ordinary runbook never lands in a queue's catalog by
    accident. ``True`` → ``["operator"]``; a list of strings → itself."""
    value = skill.get("discoverable")
    if value is True:
        return [OPERATOR]
    if isinstance(value, list):
        return [a for a in value if isinstance(a, str)]
    return []


def skill_to_catalog_entry(
    skill: dict[str, Any], *, audience: str = OPERATOR
) -> dict[str, Any]:
    """One runbook → a catalog tool entry, the same visitor-form contract a runnable
    uses so the room/frontend render it identically. The wire ``name`` is
    ``skill:<id>`` — namespaced so it can never collide with a runnable name in the
    room's name-keyed dedup/holder logic; ``x-title`` is the human label and
    ``x-kind`` is ``"skill"``. ``inputSchema`` is built from the skill's
    ``[skill.args]`` table via the shared arg machinery, so a runbook's inputs behave
    exactly like a runnable's. ``audience`` stamps ``x-audience`` — ``operator`` keeps
    a runbook operator-only, ``self-service`` opens it to visitors (the room filters
    on it)."""
    from .skillargs import parse_skill_args

    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in parse_skill_args(skill.get("args")):
        if str(arg.get("type")) == "password":
            continue
        name = str(arg.get("name"))
        properties[name] = arg_to_property(arg)
        if arg.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    title = str(skill.get("title") or skill.get("id") or "")
    return {
        "name": "skill:" + str(skill.get("id") or ""),
        "x-title": title,
        "description": str(skill.get("symptom") or title),
        "x-autonomy": "confirm",
        "x-kind": "skill",
        "x-audience": audience,
        "inputSchema": schema,
    }


def build_skill_catalog(
    skills: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The publishable runbooks for a queue: ``(tools, index)``.

    ``tools`` are skill catalog entries; ``index`` maps each entry's ``skill:<id>``
    wire name back to the bare skill id so an assigned skill request can be rendered
    + run. A skill is included only if it opts in via its ``discoverable`` audience
    (``operator`` or ``self-service``) — runbooks are not published by default. Each
    entry is tagged with the **broadest** audience it qualifies for (``self-service``
    when self-service-marked, else ``operator``), mirroring how a runnable is tagged,
    so a runbook a team chooses to expose to visitors becomes a self-service form.
    First id wins on a duplicate."""
    tools: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    for skill in skills:
        sid = str(skill.get("id") or "")
        if not sid:
            continue
        audiences = set(skill_audiences(skill))
        if not audiences & {OPERATOR, SELF_SERVICE}:
            continue  # not published — opt-in only
        name = "skill:" + sid
        if name in index:
            continue
        # Broadest audience wins: a self-service-marked runbook is visible to
        # visitors (and operators); otherwise it stays operator-only.
        audience = SELF_SERVICE if SELF_SERVICE in audiences else OPERATOR
        tools.append(skill_to_catalog_entry(skill, audience=audience))
        index[name] = sid
    return tools, index
