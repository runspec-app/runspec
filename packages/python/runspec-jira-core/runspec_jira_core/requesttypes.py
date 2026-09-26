"""requesttypes.py — request-type *signatures* and one-call create / update.

Raising a JSM request through the raw API takes three agent round trips:
list the request types, list one type's fields, then raise with a JSON object
keyed by ``customfield_*`` ids and wire-shaped values (``{"id": "10412"}``).
This module collapses that into one call:

    create_request(cfg, "HR", "New starter", {"Summary": "...", "Office": "Leeds"})

* :func:`describe_request_types` turns each request type of the given projects
  into a :class:`RequestTypeSig` — its fields with a slug ``name``, portal
  ``label``, Jira ``field_id``, ``required``, a normalised ``kind`` and the
  allowed ``options`` (with their ids). ``RequestTypeSig.json_schema()`` is an
  MCP-ready input schema.
* :func:`build_field_values` (pure) resolves friendly keys — slug name, portal
  label or field id — and coerces values into the wire shape for their kind
  (select → ``{"id"}``, cascade → ``{"id", "child"}``, user → ``{"name"}`` /
  ``{"accountId"}``, …). Unknown fields/options and missing required fields
  raise :class:`JiraError` with close matches **and the signature**, so a wrong
  guess is corrected on the next call without a discovery call.
* :func:`create_request` / :func:`update_request` do the resolve + the single
  POST / PUT. Signatures are cached in-process (:func:`clear_signature_cache`),
  or passed in pre-built (e.g. from a codegen manifest — see ``codegen.py``).

Attachment fields are listed in a signature with ``kind = "attachment"`` but
can't be set here — raise the request, then use ``add_attachment``.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from runspec_jira_core.client import build_client, build_servicedesk, translate_errors
from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.resolve import slugify, split_list, suggest
from runspec_jira_core.servicedesk import SD_API, list_service_desks, match_service_desk, sd_get_all

#: Field slugs that would collide with the ``jira`` runnable's / a generated
#: runnable's own args (or runspec's auto-added flags) get a ``-field`` suffix.
RESERVED_NAMES = frozenset({"env", "jira-pat", "help", "h", "debug", "version", "on-behalf-of", "key", "values", "project", "request-type"})

KINDS = ("str", "text", "number", "date", "datetime", "select", "multiselect", "cascade", "user", "multiuser", "labels", "attachment", "unknown")

_CACHE_TTL = 3600.0
_rt_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_sig_cache: dict[tuple[str, str, str], tuple[float, RequestTypeSig]] = {}


# ── model ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Option:
    """One allowed value of a select-like field (``children`` for cascades)."""

    id: str
    label: str
    children: tuple[Option, ...] = ()


@dataclass(frozen=True)
class FieldSig:
    """One field of a request type, as the portal shows it."""

    name: str  # slug of the portal label, unique within the request type
    label: str  # portal label ("Laptop model")
    field_id: str  # "customfield_10412" | "summary" | ...
    required: bool
    kind: str  # one of KINDS
    options: tuple[Option, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class RequestTypeSig:
    """A request type and its fields — everything needed to raise it in one call."""

    project_key: str
    project_name: str
    service_desk_id: str
    request_type_id: str
    name: str
    slug: str
    description: str = ""
    fields: tuple[FieldSig, ...] = field(default_factory=tuple)
    can_raise_on_behalf_of: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RequestTypeSig:
        def opt(o: Mapping[str, Any]) -> Option:
            return Option(id=str(o["id"]), label=str(o["label"]), children=tuple(opt(c) for c in o.get("children") or ()))

        fields = tuple(
            FieldSig(
                name=f["name"],
                label=f["label"],
                field_id=f["field_id"],
                required=bool(f["required"]),
                kind=f["kind"],
                options=tuple(opt(o) for o in f.get("options") or ()),
                description=f.get("description") or "",
            )
            for f in data.get("fields") or ()
        )
        keys = ("project_key", "project_name", "service_desk_id", "request_type_id", "name", "slug", "description", "can_raise_on_behalf_of")
        return cls(fields=fields, **{k: data[k] for k in keys if k in data})

    def summary(self) -> str:
        """Compact one-line-per-field signature, used in error messages."""
        lines = [f"{self.project_key} / {self.name!r} fields:"]
        for f in self.fields:
            bits = [f.kind, "required" if f.required else "optional"]
            if f.options:
                labels = [o.label for o in f.options]
                shown = "|".join(labels[:12]) + ("|…" if len(labels) > 12 else "")
                bits.append(f"one of {shown}")
            lines.append(f"  {f.name} ({f.label!r}): {', '.join(bits)}")
        return "\n".join(lines)

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema for the ``values`` object :func:`create_request` accepts."""
        props: dict[str, Any] = {}
        for f in self.fields:
            if f.kind == "attachment":
                continue
            prop: dict[str, Any] = {"description": f.label + (f" — {f.description}" if f.description else "")}
            if f.kind == "number":
                prop["type"] = "number"
            elif f.kind in ("multiselect", "multiuser", "labels"):
                prop["type"] = "array"
                prop["items"] = {"type": "string", **({"enum": [o.label for o in f.options]} if f.options else {})}
            else:
                prop["type"] = "string"
                if f.kind == "select" and f.options:
                    prop["enum"] = [o.label for o in f.options]
                if f.kind in ("date", "datetime"):
                    prop["format"] = "date" if f.kind == "date" else "date-time"
                if f.kind == "cascade":
                    prop["description"] += " (format: 'Parent > Child')"
            props[f.name] = prop
        required = [f.name for f in self.fields if f.required and f.kind != "attachment"]
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ── building signatures from the API ────────────────────────────────────────


def _kind(schema: Mapping[str, Any], field_id: str) -> str:
    t = schema.get("type")
    items = schema.get("items")
    custom = str(schema.get("custom") or "")
    if t == "string":
        return "text" if custom.endswith(":textarea") or field_id == "description" else "str"
    if t == "number":
        return "number"
    if t in ("date", "datetime"):
        return str(t)
    if t in ("option", "priority", "resolution", "securitylevel", "version", "component", "issuetype"):
        return "select"
    if t == "option-with-child":
        return "cascade"
    if t == "user":
        return "user"
    if t == "attachment":
        return "attachment"
    if t == "array":
        if items in ("option", "component", "version"):
            return "multiselect"
        if items == "user":
            return "multiuser"
        if items == "string":
            return "labels"
        if items == "attachment":
            return "attachment"
    return "unknown"


def _options(valid: Iterable[Mapping[str, Any]] | None) -> tuple[Option, ...]:
    return tuple(Option(id=str(v.get("value")), label=str(v.get("label") or v.get("value")), children=_options(v.get("children"))) for v in valid or ())


def _strip_html(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def build_signature(desk: Mapping[str, Any], request_type: Mapping[str, Any], fields_body: Mapping[str, Any]) -> RequestTypeSig:
    """Pure: build a :class:`RequestTypeSig` from raw ``servicedeskapi`` payloads."""
    used: set[str] = set()
    fields: list[FieldSig] = []
    for raw in fields_body.get("requestTypeFields") or []:
        field_id = str(raw.get("fieldId"))
        label = str(raw.get("name") or field_id)
        name = slugify(label)
        if name in RESERVED_NAMES:
            name = f"{name}-field"
        base, n = name, 2
        while name in used:
            name = f"{base}-{n}"
            n += 1
        used.add(name)
        fields.append(
            FieldSig(
                name=name,
                label=label,
                field_id=field_id,
                required=bool(raw.get("required")),
                kind=_kind(raw.get("jiraSchema") or {}, field_id),
                options=_options(raw.get("validValues")),
                description=_strip_html(raw.get("description")),
            )
        )
    name = str(request_type.get("name"))
    return RequestTypeSig(
        project_key=str(desk.get("projectKey")),
        project_name=str(desk.get("projectName")),
        service_desk_id=str(desk.get("id")),
        request_type_id=str(request_type.get("id")),
        name=name,
        slug=slugify(name),
        description=_strip_html(request_type.get("description") or request_type.get("helpText")),
        fields=tuple(fields),
        can_raise_on_behalf_of=bool(fields_body.get("canRaiseOnBehalfOf")),
    )


def _wanted(request_type: Mapping[str, Any], patterns: Sequence[str] | None) -> bool:
    if not patterns:
        return True
    name = str(request_type.get("name") or "")
    rt_id = str(request_type.get("id"))
    return any(p == rt_id or fnmatch.fnmatch(name.casefold(), p.casefold()) or fnmatch.fnmatch(slugify(name), p.casefold()) for p in patterns)


def _fresh(entry: tuple[float, Any] | None) -> bool:
    return entry is not None and time.monotonic() - entry[0] < _CACHE_TTL


def _raw_request_types(sd: Any, config: JiraConfig, desk: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = (config.url, str(desk["id"]))
    entry = _rt_cache.get(key)
    if not _fresh(entry):
        entry = (time.monotonic(), sd_get_all(sd, f"{SD_API}/servicedesk/{desk['id']}/requesttype"))
        _rt_cache[key] = entry
    return entry[1]  # type: ignore[index]


def _signature(sd: Any, config: JiraConfig, desk: Mapping[str, Any], rt: Mapping[str, Any]) -> RequestTypeSig:
    key = (config.url, str(desk["id"]), str(rt["id"]))
    entry = _sig_cache.get(key)
    if not _fresh(entry):
        body = sd.get(f"{SD_API}/servicedesk/{desk['id']}/requesttype/{rt['id']}/field", headers=sd.experimental_headers) or {}
        entry = (time.monotonic(), build_signature(desk, rt, body))
        _sig_cache[key] = entry
    return entry[1]  # type: ignore[index]


def _describe_desk(sd: Any, config: JiraConfig, desk: Mapping[str, Any], patterns: Sequence[str] | None) -> list[RequestTypeSig]:
    """Signatures for the request types of one desk matching ``patterns`` — fields are
    fetched only for the matching types (one call each), not the whole desk."""
    return [_signature(sd, config, desk, rt) for rt in _raw_request_types(sd, config, desk) if _wanted(rt, patterns)]


def _pick_raw(raw: Sequence[Mapping[str, Any]], request_type: str, desk: Mapping[str, Any]) -> Mapping[str, Any]:
    wanted = request_type.strip().casefold()
    for rt in raw:
        name = str(rt.get("name") or "")
        if wanted in (str(rt.get("id")), name.casefold(), slugify(name)):
            return rt
    names = [str(rt.get("name")) for rt in raw]
    raise JiraError(f"No request type {request_type!r} in {desk.get('projectKey')}{suggest(request_type, names)} (available: {', '.join(names) or 'none'})")


@translate_errors
def describe_request_types(
    config: JiraConfig,
    projects: Sequence[str],
    *,
    request_types: Sequence[str] | None = None,
) -> list[RequestTypeSig]:
    """Signatures for the request types in ``projects`` (keys or names).

    ``request_types`` narrows the result to ids or name globs (``"New starter"``,
    ``"*laptop*"``); fields are only fetched for the types that match. Results
    are cached in-process for an hour (:func:`clear_signature_cache`).
    """
    if not projects:
        raise JiraError("describe-request-types needs at least one project")
    desks = list_service_desks(config)
    sd = build_servicedesk(config)
    out: list[RequestTypeSig] = []
    for project in projects:
        out.extend(_describe_desk(sd, config, match_service_desk(desks, project), request_types))
    return out


def clear_signature_cache() -> None:
    """Forget cached request types / signatures (e.g. after a form was edited in the UI)."""
    _rt_cache.clear()
    _sig_cache.clear()


def find_request_type(sigs: Sequence[RequestTypeSig], request_type: str, *, project: str | None = None) -> RequestTypeSig:
    """Pick a signature by request type id, name or slug (case-insensitive)."""
    pool = [s for s in sigs if project is None or project.casefold() in (s.project_key.casefold(), s.project_name.casefold(), s.service_desk_id)]
    wanted = request_type.strip().casefold()
    for s in pool:
        if wanted in (s.request_type_id, s.name.casefold(), s.slug):
            return s
    names = [s.name for s in pool]
    where = f" in {project}" if project else ""
    raise JiraError(f"No request type {request_type!r}{where}{suggest(request_type, names)} (available: {', '.join(names) or 'none'})")


# ── value coercion (pure) ────────────────────────────────────────────────────


def _match_option(f: FieldSig, options: Sequence[Option], value: Any, sig: RequestTypeSig) -> Option:
    text = str(value).strip().casefold()
    for o in options:
        if text in (o.label.casefold(), o.id):
            return o
    labels = [o.label for o in options]
    raise JiraError(f"{f.name}: {value!r} is not an allowed option{suggest(str(value), labels)} (allowed: {', '.join(labels)})\n{sig.summary()}")


def _select(f: FieldSig, value: Any, sig: RequestTypeSig) -> Any:
    if isinstance(value, dict):
        return value
    if f.options:
        return {"id": _match_option(f, f.options, value, sig).id}
    key = "name" if f.field_id in ("priority", "components", "versions", "fixVersions") else "value"
    return {key: str(value)}


def _user(value: Any, cloud: bool) -> Any:
    if isinstance(value, dict):
        return value
    return {"accountId": str(value)} if cloud else {"name": str(value)}


def _cascade(f: FieldSig, value: Any, sig: RequestTypeSig) -> Any:
    if isinstance(value, dict):
        return value
    parts = [str(p).strip() for p in value] if isinstance(value, (list, tuple)) else [p.strip() for p in str(value).split(">")]
    parent = _match_option(f, f.options, parts[0], sig) if f.options else Option(id=parts[0], label=parts[0])
    out: dict[str, Any] = {"id": parent.id}
    if len(parts) > 1 and parts[1]:
        child = _match_option(f, parent.children, parts[1], sig) if parent.children else Option(id=parts[1], label=parts[1])
        out["child"] = {"id": child.id}
    return out


def _datetime(value: Any) -> str:
    if isinstance(value, _dt.datetime):
        parsed = value
    else:
        try:
            parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.000")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _coerce(f: FieldSig, value: Any, sig: RequestTypeSig, cloud: bool) -> Any:
    kind = f.kind
    if kind == "attachment":
        raise JiraError(f"{f.name}: attachments can't be set here — raise the request first, then use add-attachment")
    if kind in ("str", "text"):
        return value if isinstance(value, str) else json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    if kind == "number":
        try:
            return float(value) if not isinstance(value, (int, float)) else value
        except (TypeError, ValueError):
            raise JiraError(f"{f.name}: {value!r} is not a number") from None
    if kind == "date":
        return value.isoformat() if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime) else str(value)[:10]
    if kind == "datetime":
        return _datetime(value)
    if kind == "select":
        return _select(f, value, sig)
    if kind == "multiselect":
        return [_select(f, v, sig) for v in split_list(value)]
    if kind == "cascade":
        return _cascade(f, value, sig)
    if kind == "user":
        return _user(value, cloud)
    if kind == "multiuser":
        return [_user(v, cloud) for v in split_list(value)]
    if kind == "labels":
        return [str(v) for v in split_list(value)]
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, (list, tuple)) and not value)


def build_field_values(
    sig: RequestTypeSig,
    values: Mapping[str, Any],
    *,
    cloud: bool = False,
    require_all: bool = True,
) -> dict[str, Any]:
    """Resolve friendly keys and coerce values into a ``{fieldId: wire value}`` dict.

    Keys may be a field's slug ``name``, its portal ``label`` or its ``field_id``.
    Empty values (``None``, ``""``, ``[]``) are dropped. With ``require_all``
    (create), a missing required field is an error; updates pass ``False``.
    A key shaped like ``customfield_12345`` that isn't in the request type is
    passed through unchanged (an escape hatch for fields hidden from the portal).
    """
    index: dict[str, FieldSig] = {}
    for f in sig.fields:
        for alias in (f.name, f.label.casefold(), f.field_id.casefold()):
            index.setdefault(alias, f)
    out: dict[str, Any] = {}
    for key, value in values.items():
        if _empty(value):
            continue
        hit = index.get(key) or index.get(key.casefold()) or index.get(slugify(key))
        if hit is None:
            if re.fullmatch(r"customfield_\d+", key):
                out[key] = value
                continue
            names = [x.name for x in sig.fields] + [x.label for x in sig.fields]
            raise JiraError(f"Unknown field {key!r} for request type {sig.name!r}{suggest(key, names)}\n{sig.summary()}")
        out[hit.field_id] = _coerce(hit, value, sig, cloud)
    if require_all:
        missing = [f.name for f in sig.fields if f.required and f.kind != "attachment" and f.field_id not in out]
        if missing:
            raise JiraError(f"Missing required field(s) for {sig.name!r}: {', '.join(missing)}\n{sig.summary()}")
    return out


# ── one-call create / update ────────────────────────────────────────────────


def _parse_values(values: Mapping[str, Any] | str | None) -> Mapping[str, Any]:
    if values is None or values == "":
        return {}
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError as exc:
            raise JiraError(f"--values is not valid JSON: {exc}") from exc
    if not isinstance(values, Mapping):
        raise JiraError("--values must be a JSON object of field: value")
    return values


@translate_errors
def create_request(
    config: JiraConfig,
    project: str,
    request_type: str,
    values: Mapping[str, Any] | str,
    *,
    on_behalf_of: str | None = None,
    signatures: Sequence[RequestTypeSig] | None = None,
) -> dict[str, Any]:
    """Raise a request by project + request type **name**, with friendly field keys.

    ``values`` is a mapping (or JSON object string) keyed by field name, portal
    label or field id. Resolution + validation happen locally; the request is
    raised with a single POST. Without ``signatures`` the lookup costs three
    cheap GETs (desks, request types, that one type's fields); pass ``signatures``
    (e.g. from a codegen manifest) to skip it entirely.
    """
    if signatures is not None:
        sig = find_request_type(signatures, request_type, project=project)
    else:
        sd = build_servicedesk(config)
        desk = match_service_desk(list_service_desks(config), project)
        sig = _signature(sd, config, desk, _pick_raw(_raw_request_types(sd, config, desk), request_type, desk))
    return create_request_from_signature(config, sig, values, on_behalf_of=on_behalf_of)


@translate_errors
def create_request_from_signature(
    config: JiraConfig,
    sig: RequestTypeSig,
    values: Mapping[str, Any] | str,
    *,
    on_behalf_of: str | None = None,
) -> dict[str, Any]:
    """Raise a request for an already-resolved :class:`RequestTypeSig` (one POST)."""
    body: dict[str, Any] = {
        "serviceDeskId": sig.service_desk_id,
        "requestTypeId": sig.request_type_id,
        "requestFieldValues": build_field_values(sig, _parse_values(values), cloud=config.cloud),
    }
    if on_behalf_of:
        body["raiseOnBehalfOf"] = on_behalf_of
    sd = build_servicedesk(config)
    made = sd.post(f"{SD_API}/request", data=body, headers=sd.experimental_headers) or {}
    links = made.get("_links") or {}
    return {
        "issueKey": made.get("issueKey"),
        "issueId": made.get("issueId"),
        "project": sig.project_key,
        "requestType": sig.name,
        "web": links.get("web"),
    }


@translate_errors
def update_request(
    config: JiraConfig,
    issue_key: str,
    values: Mapping[str, Any] | str,
    *,
    signatures: Sequence[RequestTypeSig] | None = None,
) -> dict[str, Any]:
    """Edit a request's fields by friendly name, in one call.

    Looks up the request's type, resolves ``values`` against that type's
    signature (from ``signatures`` when it's there, otherwise live), and applies
    them with a single issue PUT. Only the given fields change.
    """
    parsed = _parse_values(values)
    if not parsed:
        raise JiraError("update-request needs at least one field in --values")
    sd = build_servicedesk(config)
    req = sd.get(f"{SD_API}/request/{issue_key}", headers=sd.experimental_headers) or {}
    rt_id = str(req.get("requestTypeId") or "")
    desk_id = str(req.get("serviceDeskId") or "")
    if not rt_id:
        raise JiraError(f"{issue_key} is not a service desk request (no request type)")
    sig = next((s for s in signatures or () if s.request_type_id == rt_id and s.service_desk_id == desk_id), None)
    if sig is None:
        desk = match_service_desk(list_service_desks(config), desk_id)
        sig = _signature(sd, config, desk, _pick_raw(_raw_request_types(sd, config, desk), rt_id, desk))
    fields = build_field_values(sig, parsed, cloud=config.cloud, require_all=False)
    build_client(config).put(f"rest/api/2/issue/{issue_key}", data={"fields": fields})
    return {"issueKey": issue_key, "requestType": sig.name, "updated": sorted(fields)}
