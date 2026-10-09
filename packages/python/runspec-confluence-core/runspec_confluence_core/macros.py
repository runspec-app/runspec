"""macros.py — read Confluence macros out of a page's storage format.

Every macro on a Confluence page, built-in or add-on, sits in the page's
**storage** XHTML as ``<ac:structured-macro ac:name="…">`` with
``<ac:parameter>`` children and an optional ``<ac:plain-text-body>`` (CDATA) or
``<ac:rich-text-body>`` (nested XHTML). That holds on Cloud and Server/Data
Center alike, so one parser covers:

- **Built-ins** — ``code`` / ``noformat``, the ``info`` / ``note`` / ``tip`` /
  ``warning`` / ``panel`` panels, ``expand``, ``status``, ``toc``, ``jira`` …
- **Text diagrams** — PlantUML and Mermaid apps keep the diagram source inline
  in the plain-text body. Vendors name their macros differently, so these are
  matched by substring (``plantuml`` / ``mermaid``).
- **Attachment diagrams** — draw.io (``drawio``, ``inc-drawio`` for a diagram
  embedded from another page, ``drawio-sketch``) and Gliffy (``gliffy``) keep
  only a pointer in the macro; the diagram itself is a **page attachment** (the
  source file named after the diagram, plus a ``.png`` preview).

:func:`list_macros` inventories a storage body; :func:`storage_to_html` rewrites
the macros into plain HTML so :func:`summarise.html_to_markdown` renders them
readably instead of gluing parameter values into the text.

Best-effort throughout: vendor apps change parameter names between versions, so
anything unrecognised is reported as a generic macro rather than raising.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup, Tag

# Attachment-backed diagram macros → diagram kind.
DIAGRAM_MACROS = {
    "drawio": "drawio",
    "inc-drawio": "drawio",
    "drawio-sketch": "drawio",
    "gliffy": "gliffy",
}

CODE_MACROS = {"code", "noformat"}

PANEL_MACROS = {"info": "Info", "note": "Note", "tip": "Tip", "warning": "Warning", "panel": None}


def _text_diagram_kind(name: str) -> str | None:
    """``plantuml`` / ``mermaid`` for a text-diagram macro from any vendor."""
    if "plantuml" in name:
        return "plantuml"
    if "mermaid" in name:
        return "mermaid"
    return None


def macro_kind(name: str) -> str:
    """Classify a macro name: ``diagram`` / ``text-diagram`` / ``code`` / ``panel``
    / ``expand`` / ``macro`` (anything else)."""
    name = name.lower()
    if name in DIAGRAM_MACROS:
        return "diagram"
    if _text_diagram_kind(name):
        return "text-diagram"
    if name in CODE_MACROS:
        return "code"
    if name in PANEL_MACROS:
        return "panel"
    if name == "expand":
        return "expand"
    return "macro"


def _soup(storage: str) -> BeautifulSoup:
    # html.parser keeps the ``ac:`` / ``ri:`` prefixes in tag and attribute
    # names and reads CDATA bodies; an XML parser would reject storage format
    # (undeclared namespaces, HTML entities like &nbsp;).
    return BeautifulSoup(storage, "html.parser")


def _own_children(macro: Tag, tag: str) -> list[Tag]:
    """Direct children named ``tag`` (not ones belonging to a nested macro)."""
    return [c for c in macro.children if isinstance(c, Tag) and c.name == tag]


def _params(macro: Tag) -> dict[str, str]:
    out: dict[str, str] = {}
    for param in _own_children(macro, "ac:parameter"):
        key = param.get("ac:name")
        if isinstance(key, str):
            out[key] = param.get_text()
    return out


def _plain_body(macro: Tag) -> str | None:
    bodies = _own_children(macro, "ac:plain-text-body")
    return bodies[0].get_text() if bodies else None


def _rich_body(macro: Tag) -> Tag | None:
    bodies = _own_children(macro, "ac:rich-text-body")
    return bodies[0] if bodies else None


def _macro_name(macro: Tag) -> str:
    name = macro.get("ac:name")
    return name.lower() if isinstance(name, str) else ""


def diagram_ref(name: str, params: dict[str, str]) -> dict[str, Any] | None:
    """Where an attachment diagram lives: ``{kind, name, revision?, page_id?,
    attachments, preview}``. ``attachments`` lists the filenames the source may be
    stored under (vendors and versions differ); ``page_id`` is set when the macro
    embeds a diagram from another page (``inc-drawio``)."""
    kind = DIAGRAM_MACROS.get(name)
    if kind == "drawio":
        diagram = params.get("diagramName") or params.get("diagramDisplayName")
        if not diagram:
            return None
        ref: dict[str, Any] = {
            "kind": "drawio",
            "name": diagram,
            "attachments": [diagram, f"{diagram}.drawio", f"{diagram}.xml"],
            "preview": f"{diagram}.png",
        }
        if params.get("revision"):
            ref["revision"] = params["revision"]
        if name == "inc-drawio" and params.get("pageId"):
            ref["page_id"] = params["pageId"]
        return ref
    if kind == "gliffy":
        diagram = params.get("name") or params.get("displayName")
        if not diagram:
            return None
        ref = {
            "kind": "gliffy",
            "name": diagram,
            "attachments": [diagram, f"{diagram}.gliffy"],
            "preview": f"{diagram}.png",
        }
        if params.get("pagePin"):
            ref["revision"] = params["pagePin"]
        return ref
    return None


def list_macros(storage: str | None) -> list[dict[str, Any]]:
    """Inventory the macros in a storage-format body, in document order.

    Each entry is ``{name, kind, params}`` plus ``body`` (plain-text bodies —
    code, PlantUML, Mermaid), ``title`` (expand / panels), ``diagram`` (draw.io /
    Gliffy attachment pointer, see :func:`diagram_ref`), and ``depth`` when the
    macro is nested inside another macro's rich body."""
    if not storage:
        return []
    out: list[dict[str, Any]] = []
    for macro in _soup(storage).find_all("ac:structured-macro"):
        name = _macro_name(macro)
        params = _params(macro)
        entry: dict[str, Any] = {"name": name, "kind": macro_kind(name), "params": params}
        body = _plain_body(macro)
        if body is not None:
            entry["body"] = body
        if params.get("title"):
            entry["title"] = params["title"]
        ref = diagram_ref(name, params)
        if ref:
            entry["diagram"] = ref
        text_kind = _text_diagram_kind(name)
        if text_kind:
            entry["language"] = text_kind
        depth = len(macro.find_parents("ac:structured-macro"))
        if depth:
            entry["depth"] = depth
        out.append(entry)
    return out


def diagrams(storage: str | None) -> list[dict[str, Any]]:
    """Just the attachment-diagram pointers (draw.io / Gliffy) in a storage body."""
    return [m["diagram"] for m in list_macros(storage) if "diagram" in m]


# ── storage → plain HTML (for Markdown rendering) ───────────────────────────


def _new(soup: BeautifulSoup, tag: str, text: str | None = None, **attrs: str) -> Tag:
    el = soup.new_tag(tag, attrs=attrs)
    if text is not None:
        el.string = text
    return el


def _replacement(soup: BeautifulSoup, macro: Tag) -> list[Any]:
    """The plain-HTML nodes standing in for one macro."""
    name = _macro_name(macro)
    params = _params(macro)
    kind = macro_kind(name)
    rich = _rich_body(macro)

    if kind in ("code", "text-diagram"):
        language = _text_diagram_kind(name) or params.get("language", "")
        pre = _new(soup, "pre", **({"data-language": language} if language else {}))
        pre.append(_new(soup, "code", _plain_body(macro) or ""))
        nodes: list[Any] = []
        if params.get("title"):
            nodes.append(_new(soup, "p", params["title"]))
        return [*nodes, pre]

    if kind == "diagram":
        ref = diagram_ref(name, params)
        label = "draw.io" if DIAGRAM_MACROS[name] == "drawio" else "Gliffy"
        if ref:
            text = f'[{label} diagram "{ref["name"]}"' + (f" rev {ref['revision']}" if ref.get("revision") else "")
            text += f" from page {ref['page_id']}]" if ref.get("page_id") else "]"
        else:
            text = f"[{label} diagram]"
        return [_new(soup, "p", text)]

    if kind == "panel":
        quote = _new(soup, "blockquote")
        heading = PANEL_MACROS[name] or params.get("title")
        title = params.get("title") if PANEL_MACROS[name] else None
        if heading:
            lead = _new(soup, "p")
            lead.append(_new(soup, "strong", f"{heading}: {title}" if title else f"{heading}:"))
            quote.append(lead)
        if rich is not None:
            quote.extend(list(rich.contents))
        return [quote]

    if kind == "expand":
        lead = _new(soup, "p")
        lead.append(_new(soup, "strong", params.get("title") or "Expand"))
        return [lead, *(list(rich.contents) if rich is not None else [])]

    if name == "status":
        return [_new(soup, "code", f"[{params.get('title') or params.get('colour') or 'status'}]")]

    if name == "jira" and params.get("key"):
        return [_new(soup, "span", params["key"])]

    # Anything else: keep the rich body if it has one (excerpt, section, column,
    # details …), else leave a named placeholder (toc, children, include …).
    if rich is not None:
        return list(rich.contents)
    return [_new(soup, "p", f"[{name or 'unknown'} macro]")]


def _replace_links_and_images(soup: BeautifulSoup) -> None:
    for image in soup.find_all("ac:image"):
        target = image.find(["ri:attachment", "ri:url"])
        src = ""
        if isinstance(target, Tag):
            src = str(target.get("ri:filename") or target.get("ri:value") or "")
        alt = str(image.get("ac:alt") or src or "image")
        image.replace_with(_new(soup, "span", f"[image: {alt}]"))
    for link in soup.find_all("ac:link"):
        body = link.find(["ac:plain-text-link-body", "ac:link-body"])
        target = link.find(["ri:page", "ri:attachment", "ri:user", "ri:space"])
        text = body.get_text() if isinstance(body, Tag) else ""
        if not text and isinstance(target, Tag):
            text = str(
                target.get("ri:content-title")
                or target.get("ri:filename")
                or target.get("ri:space-key")
                or "@" + str(target.get("ri:username") or target.get("ri:userkey") or target.get("ri:account-id") or "user")
            )
        link.replace_with(_new(soup, "span", text))
    for emoticon in soup.find_all("ac:emoticon"):
        emoticon.decompose()


def storage_to_html(storage: str | None) -> str:
    """Rewrite Confluence storage XHTML into plain HTML a generic HTML→Markdown
    converter renders readably: code and PlantUML/Mermaid become ``<pre>`` blocks
    (``data-language`` carries the language), panels become block quotes led by
    their type, expand becomes a bold title over its body, draw.io/Gliffy become
    a ``[draw.io diagram "name" rev N]`` placeholder, and ``ac:image``/``ac:link``
    become their alt text / link text. Input without any ``ac:`` markup is
    returned unchanged."""
    if not storage or "<ac:" not in storage:
        return storage or ""
    soup = _soup(storage)
    # Innermost first: a macro's rich body may itself hold macros.
    for macro in reversed(soup.find_all("ac:structured-macro")):
        macro.replace_with(*_replacement(soup, macro))
    _replace_links_and_images(soup)
    return str(soup)
