"""
catalog.py — build a helpdesk queue's self-service catalog from discovered
runnables (pure; no I/O).

The console is the catalog authority: it discovers every visible runnable across
the fleet (SSH + ``runspec local``). Which of those a *visitor* may request is a
front-end concern, designated in the console's own ``runspec_self_service.toml``
(see :mod:`runspec_console.self_service`) — **not** in each package's
``runspec.toml``. This module takes that configured self-service name set, applies
the optional per-queue allowlist, and converts each entry to the catalog shape the
room server caches and the visitor frontend renders forms from:

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
    # Pass the form-control hint through so the room form can render a matching
    # control (e.g. a date picker for ui="date"/"datetime"). x-* prefix mirrors
    # the x-kind/x-hosts convention and is ignored by anything that doesn't read it.
    ui = arg.get("ui")
    if ui:
        prop["x-ui"] = str(ui)
    return prop


def runnable_to_catalog_entry(
    runnable: dict[str, Any], *, audience: str = SELF_SERVICE
) -> dict[str, Any]:
    """One discovered runnable → a catalog tool entry (visitor-form contract).

    ``audience`` stamps the entry's ``x-audience`` (``"self-service"`` | ``"operator"``)
    so the room can serve operator-only entries to operator sessions and the
    self-service subset to visitors. ``x-kind`` is ``"runnable"`` — the room and
    frontend tell runnables and runbooks apart by it."""
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
    self_service_names: set[str],
    allowlist: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The catalog for a queue: ``(tools, index)``.

    ``tools`` is what the console publishes to the room (the ``catalog`` frame's
    payload); ``index`` maps each published bare tool name back to the discovered
    runnable (host/group/name) so a visitor request can later be enacted.

    Filters, in order: only names in ``self_service_names`` (the console's
    ``runspec_self_service.toml`` designation); no subcommand-requiring runnables
    (the visitor form is flat in v1); the optional per-queue ``allowlist`` of bare
    names (the ``room_catalog`` narrowing knob — ``None`` means all designated).
    Names collide across hosts on a fleet; first discovered wins, keeping the
    catalog stable for visitors."""
    allowed = set(allowlist) if allowlist is not None else None
    tools: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for runnable in runnables:
        name = str(runnable.get("name") or "")
        if not name or name in index:
            continue
        if name not in self_service_names:
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
    self_service_names: set[str],
    self_service_allowlist: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    """The combined catalog for a queue: ``(tools, index)`` covering **both**
    audiences in one tagged list (the channel the room filters per session).

    Each runnable name yields **one** entry, tagged with the broadest audience it
    qualifies for: ``"self-service"`` when its name is in ``self_service_names``
    (the console's ``runspec_self_service.toml`` designation) *and* it passes the
    optional ``self_service_allowlist`` (the ``room_catalog`` visitor-narrowing
    knob) — visible to everyone; otherwise ``"operator"`` — visible only to operator
    sessions. Every discovered runnable is at least operator-visible (a hidden
    ``discoverable = false`` runnable never reaches the console, having been filtered
    by ``runspec local``). One entry per name keeps the room's name-keyed dedup
    stable; the operator set is deliberately *not* narrowed by the self-service
    allowlist.

    The **same runnable on several hosts** (a fleet) is collapsed to one entry whose
    ``x-hosts`` lists every host it was discovered on (discovery order, deduped) — so
    an operator can see where it would run and pick the target when it matters (e.g.
    "check disk space" on a specific box). The audience + form schema come from the
    first discovered host. ``index`` maps each name to ``{host: discovered runnable}``
    so a request naming a host enacts on that host (falling back to the first when a
    request names none).

    Same flat-form constraints as :func:`build_catalog`: subcommand-requiring
    runnables are skipped and password args are omitted."""
    allowed = (
        set(self_service_allowlist) if self_service_allowlist is not None else None
    )
    order: list[str] = []
    audience_of: dict[str, str] = {}
    first_of: dict[str, dict[str, Any]] = {}
    by_host: dict[str, dict[str, dict[str, Any]]] = {}
    for runnable in runnables:
        name = str(runnable.get("name") or "")
        if not name or runnable.get("requireCommand"):
            continue
        self_service = name in self_service_names and (
            allowed is None or name in allowed
        )
        audience = SELF_SERVICE if self_service else OPERATOR
        if name not in first_of:
            order.append(name)
            audience_of[name] = audience
            first_of[name] = runnable
            by_host[name] = {}
        # Keep one discovered record per host (first wins on a per-host collision).
        # A hostless local runnable lands under the "" key so it stays enactable.
        by_host[name].setdefault(str(runnable.get("host") or ""), runnable)
    tools: list[dict[str, Any]] = []
    for name in order:
        entry = runnable_to_catalog_entry(first_of[name], audience=audience_of[name])
        hosts = [h for h in by_host[name] if h]  # drop the hostless "" placeholder
        if hosts:
            entry["x-hosts"] = hosts
        tools.append(entry)
    return tools, by_host


# ── runbooks (published runbooks) ──────────────────────────────────────────────


def runbook_audiences(runbook: dict[str, Any]) -> list[str]:
    """The audiences a runbook is published to, from its ``discoverable`` field.

    Unlike a runnable, a runbook is **opt-in**: absent / ``False`` / unknown → ``[]``
    (not published), so an ordinary runbook never lands in a queue's catalog by
    accident. ``True`` → ``["operator"]``; a list of strings → itself."""
    value = runbook.get("discoverable")
    if value is True:
        return [OPERATOR]
    if isinstance(value, list):
        return [a for a in value if isinstance(a, str)]
    return []


def runbook_to_catalog_entry(
    runbook: dict[str, Any], *, audience: str = OPERATOR
) -> dict[str, Any]:
    """One runbook → a catalog tool entry, the same visitor-form contract a runnable
    uses so the room/frontend render it identically. The wire ``name`` is
    ``runbook:<id>`` — namespaced so it can never collide with a runnable name in the
    room's name-keyed dedup/holder logic; ``x-title`` is the human label and
    ``x-kind`` is ``"runbook"``. ``inputSchema`` is built from the runbook's
    ``[runbook.args]`` table via the shared arg machinery, so a runbook's inputs behave
    exactly like a runnable's. ``audience`` stamps ``x-audience`` — ``operator`` keeps
    a runbook operator-only, ``self-service`` opens it to visitors (the room filters
    on it)."""
    from .runbookargs import parse_runbook_args

    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in parse_runbook_args(runbook.get("args")):
        if str(arg.get("type")) == "password":
            continue
        name = str(arg.get("name"))
        properties[name] = arg_to_property(arg)
        if arg.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    title = str(runbook.get("title") or runbook.get("id") or "")
    return {
        "name": "runbook:" + str(runbook.get("id") or ""),
        "x-title": title,
        "description": str(runbook.get("symptom") or title),
        "x-autonomy": "confirm",
        "x-kind": "runbook",
        "x-audience": audience,
        "inputSchema": schema,
    }


def build_runbook_catalog(
    runbooks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The publishable runbooks for a queue: ``(tools, index)``.

    ``tools`` are runbook catalog entries; ``index`` maps each entry's ``runbook:<id>``
    wire name back to the bare runbook id so an assigned runbook request can be rendered
    + run. A runbook is included only if it opts in via its ``discoverable`` audience
    (``operator`` or ``self-service``) — runbooks are not published by default. Each
    entry is tagged with the **broadest** audience it qualifies for (``self-service``
    when self-service-marked, else ``operator``), mirroring how a runnable is tagged,
    so a runbook a team chooses to expose to visitors becomes a self-service form.
    First id wins on a duplicate."""
    tools: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    for runbook in runbooks:
        sid = str(runbook.get("id") or "")
        if not sid:
            continue
        audiences = set(runbook_audiences(runbook))
        if not audiences & {OPERATOR, SELF_SERVICE}:
            continue  # not published — opt-in only
        name = "runbook:" + sid
        if name in index:
            continue
        # Broadest audience wins: a self-service-marked runbook is visible to
        # visitors (and operators); otherwise it stays operator-only.
        audience = SELF_SERVICE if SELF_SERVICE in audiences else OPERATOR
        tools.append(runbook_to_catalog_entry(runbook, audience=audience))
        index[name] = sid
    return tools, index
