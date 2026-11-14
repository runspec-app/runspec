"""hooktemplate.py — read a webhook body, pick out fields, fill in args.

Pure functions, no I/O. A hook says which values it wants from a body
(``fields = { alertname = "alerts.0.labels.alertname" }``), which rule a
delivery matches (``match = { status = "^firing$" }``) and what a run gets
(``args = { host = "{{ instance | regex('^([^:]+)', 1) }}" }``).

Deliberately small: a dotted path, a fixed set of filters, regex matching. No
expressions, no code, no Jinja — a body comes from another system and must
never be able to run anything here. A sender too awkward for this gets a
transform runnable instead (see :mod:`runspec_scheduler.hookrun`).

Bodies: JSON, XML (``<!DOCTYPE``/``<!ENTITY`` refused), form-encoded, or plain
text (only the built-in ``body`` field).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable
from typing import Any


class TemplateError(ValueError):
    """A path, template or filter that can't be used (the message says why)."""


# ── bodies ────────────────────────────────────────────────────────────────────


def body_kind(content_type: str, body: bytes) -> str:
    """``json`` / ``xml`` / ``form`` / ``text`` from the Content-Type (sniffed
    when it says nothing useful)."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.endswith("json") or ct.endswith("+json"):
        return "json"
    if ct.endswith("xml") or ct.endswith("+xml"):
        return "xml"
    if ct == "application/x-www-form-urlencoded":
        return "form"
    head = body.lstrip()[:1]
    if head in (b"{", b"["):
        return "json"
    if head == b"<":
        return "xml"
    return "text"


def parse_body(body: bytes, content_type: str = "") -> Any:
    """The body as data: a dict/list for JSON, a dict for XML and forms, else
    ``None`` (only ``body`` is usable then). Raises :class:`TemplateError` when
    the body says it's JSON/XML but isn't."""
    kind = body_kind(content_type, body)
    text = body.decode("utf-8", errors="replace")
    if kind == "json":
        try:
            return json.loads(text)
        except ValueError as exc:
            raise TemplateError(f"the body isn't valid JSON: {exc}") from None
    if kind == "xml":
        return xml_to_data(text)
    if kind == "form":
        out: dict[str, Any] = {}
        for key, value in urllib.parse.parse_qsl(text, keep_blank_values=True):
            if key in out:
                prev = out[key]
                out[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                out[key] = value
        return out
    return None


def xml_to_data(text: str) -> dict[str, Any]:
    """XML as nested dicts: ``{root: {child: …, "@attr": …, "#text": …}}``.

    A repeated child becomes a list; an element with only text becomes that
    text. Document types and entities are refused (nothing is fetched or
    expanded)."""
    import xml.etree.ElementTree as ET

    upper = text[:4096].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise TemplateError("XML bodies with a DOCTYPE or ENTITY aren't accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TemplateError(f"the body isn't valid XML: {exc}") from None

    def tag(el: Any) -> str:
        t = str(el.tag)
        return t.split("}", 1)[1] if t.startswith("{") else t

    def conv(el: Any) -> Any:
        children = list(el)
        text_value = (el.text or "").strip()
        if not children and not el.attrib:
            return text_value
        node: dict[str, Any] = {f"@{k}": v for k, v in el.attrib.items()}
        for child in children:
            key = tag(child)
            value = conv(child)
            if key in node:
                prev = node[key]
                node[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                node[key] = value
        if text_value:
            node["#text"] = text_value
        return node

    return {tag(root): conv(root)}


# ── paths ─────────────────────────────────────────────────────────────────────

_MISSING = object()


def get_path(data: Any, path: str, default: Any = _MISSING) -> Any:
    """Follow a dotted path: ``alerts.0.labels.alertname``. A number indexes a
    list (``-1`` = the last item). A missing step returns ``default`` (raises
    :class:`KeyError` when none is given)."""
    node = data
    for step in [p for p in str(path).split(".") if p != ""]:
        if isinstance(node, dict):
            if step in node:
                node = node[step]
                continue
        elif isinstance(node, list) and re.fullmatch(r"-?\d+", step):
            i = int(step)
            if -len(node) <= i < len(node):
                node = node[i]
                continue
        if default is _MISSING:
            raise KeyError(path)
        return default
    return node


def check_path(path: str) -> str:
    """Why ``path`` can't be a field path, or ""."""
    p = str(path).strip()
    if not p:
        return "a field path is empty"
    if p.startswith("$"):
        p = p[1:].lstrip(".")
    if any(not part for part in p.split(".")) and p:
        return f"field path {path!r} has an empty step"
    return ""


def extract_fields(paths: dict[str, str], item: Any, root: Any = None) -> dict[str, Any]:
    """``{name: value}`` for each named path. A path starting ``$.`` reads the
    whole body (``root``) rather than the current item (they're the same unless
    the hook splits the body). A missing value is left out."""
    root = item if root is None else root
    out: dict[str, Any] = {}
    for name, path in paths.items():
        p = str(path).strip()
        src = item
        if p.startswith("$"):
            src, p = root, p[1:].lstrip(".")
        value = get_path(src, p, None) if p else src
        if value is not None:
            out[name] = value
    return out


# ── templates ─────────────────────────────────────────────────────────────────

_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
_TOKEN_RE = re.compile(r"""\s*(?:("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(-?\d+)|([A-Za-z_][\w.\-]*)|(\|)|(\()|(\))|(,))""")


def _unquote(tok: str) -> str:
    body = tok[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _f_lower(v: Any) -> Any:
    return _as_text(v).lower()


def _f_upper(v: Any) -> Any:
    return _as_text(v).upper()


def _f_trim(v: Any) -> Any:
    return _as_text(v).strip()


def _f_replace(v: Any, old: str, new: str = "") -> Any:
    return _as_text(v).replace(str(old), str(new))


def _f_regex(v: Any, pattern: str, group: Any = 0) -> Any:
    try:
        m = re.search(str(pattern), _as_text(v))
    except re.error as exc:
        raise TemplateError(f"regex({pattern!r}): {exc}") from None
    if not m:
        return None
    try:
        return m.group(int(group) if str(group).lstrip("-").isdigit() else str(group))
    except IndexError:
        raise TemplateError(f"regex({pattern!r}): no group {group!r}") from None


def _f_default(v: Any, fallback: Any = "") -> Any:
    return fallback if v is None or v == "" or v == [] else v


def _f_split(v: Any, sep: str = ",") -> Any:
    if isinstance(v, list):
        return v
    text = _as_text(v)
    return [p.strip() for p in text.split(str(sep))] if text else []


def _f_first(v: Any) -> Any:
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _f_last(v: Any) -> Any:
    if isinstance(v, list):
        return v[-1] if v else None
    return v


def _f_join(v: Any, sep: str = ",") -> Any:
    if isinstance(v, list):
        return str(sep).join(_as_text(x) for x in v)
    return _as_text(v)


def _f_json(v: Any) -> Any:
    return json.dumps(v, separators=(",", ":"), sort_keys=True)


#: The filters a template may use — the whole list, on purpose.
FILTERS: dict[str, Callable[..., Any]] = {
    "lower": _f_lower,
    "upper": _f_upper,
    "trim": _f_trim,
    "replace": _f_replace,
    "regex": _f_regex,
    "default": _f_default,
    "split": _f_split,
    "first": _f_first,
    "last": _f_last,
    "join": _f_join,
    "json": _f_json,
}


def _tokens(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise TemplateError(f"can't read {expr.strip()!r} near {expr[pos:].strip()!r}")
        kinds = ("str", "int", "name", "pipe", "lparen", "rparen", "comma")
        for kind, val in zip(kinds, m.groups(), strict=True):
            if val is not None:
                out.append((kind, val))
                break
        pos = m.end()
    return out


def _parse_expr(expr: str) -> tuple[str, list[tuple[str, list[Any]]]]:
    """``field | filter(arg, …) | …`` → (field, [(filter, args), …])."""
    toks = _tokens(expr)
    if not toks or toks[0][0] != "name":
        raise TemplateError(f"{{{{{expr}}}}} must start with a field name")
    field_name = toks[0][1]
    chain: list[tuple[str, list[Any]]] = []
    i = 1
    while i < len(toks):
        if toks[i][0] != "pipe" or i + 1 >= len(toks) or toks[i + 1][0] != "name":
            raise TemplateError(f"{{{{{expr}}}}}: expected '| filter'")
        fname = toks[i + 1][1]
        if fname not in FILTERS:
            raise TemplateError(f"unknown filter {fname!r}; use one of: {', '.join(FILTERS)}")
        i += 2
        args: list[Any] = []
        if i < len(toks) and toks[i][0] == "lparen":
            i += 1
            while i < len(toks) and toks[i][0] != "rparen":
                kind, val = toks[i]
                if kind == "str":
                    args.append(_unquote(val))
                elif kind == "int":
                    args.append(int(val))
                elif kind == "comma":
                    pass
                else:
                    raise TemplateError(f"{fname}(): arguments are quoted text or numbers, not {val!r}")
                i += 1
            if i >= len(toks):
                raise TemplateError(f"{fname}(: missing ')'")
            i += 1
        chain.append((fname, args))
    return field_name, chain


def _eval(expr: str, fields: dict[str, Any]) -> Any:
    name, chain = _parse_expr(expr)
    value = fields.get(name) if name in fields else get_path(fields, name, None)
    for fname, args in chain:
        try:
            value = FILTERS[fname](value, *args)
        except TypeError:
            raise TemplateError(f"{fname}() takes different arguments") from None
    return value


def render(template: Any, fields: dict[str, Any]) -> Any:
    """Fill in ``template`` from ``fields``.

    A string that is one ``{{ … }}`` and nothing else keeps the value's own
    type (a list stays a list for a ``multiple`` arg); anything else becomes
    text. Lists and dicts are filled in item by item; other values are kept."""
    if isinstance(template, list):
        return [render(t, fields) for t in template]
    if isinstance(template, dict):
        return {k: render(v, fields) for k, v in template.items()}
    if not isinstance(template, str) or "{{" not in template:
        return template
    whole = _TEMPLATE_RE.fullmatch(template.strip()) if template.count("{{") == 1 else None
    if whole:
        return _eval(whole.group(1), fields)
    return _TEMPLATE_RE.sub(lambda m: _as_text(_eval(m.group(1), fields)), template)


def check_template(template: Any) -> str:
    """Why ``template`` can't be used, or "" (filters and syntax only)."""
    if isinstance(template, list):
        return next((p for p in (check_template(t) for t in template) if p), "")
    if isinstance(template, dict):
        return next((p for p in (check_template(t) for t in template.values()) if p), "")
    if not isinstance(template, str):
        return ""
    if template.count("{{") != template.count("}}"):
        return f"unbalanced {{{{ }}}} in {template!r}"
    for m in _TEMPLATE_RE.finditer(template):
        try:
            _parse_expr(m.group(1))
        except TemplateError as exc:
            return str(exc)
    return ""


# ── matching ──────────────────────────────────────────────────────────────────


def match_rule(patterns: dict[str, re.Pattern[str]], fields: dict[str, Any]) -> dict[str, Any] | None:
    """The named groups when every pattern matches its field (a search, so
    anchor with ^…$ for a whole value), else ``None``. A missing field doesn't
    match."""
    captured: dict[str, Any] = {}
    for name, pattern in patterns.items():
        if name not in fields:
            value = get_path(fields, name, None)
            if value is None:
                return None
        else:
            value = fields[name]
        m = pattern.search(_as_text(value))
        if not m:
            return None
        captured.update({k: v for k, v in m.groupdict().items() if v is not None})
    return captured
