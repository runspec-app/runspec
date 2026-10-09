"""diagram_write.py — add a diagram to a page from diagram text.

The agent writes the diagram as text (Mermaid, PlantUML, Graphviz …) and it goes
onto the page one of three ways:

- **a PNG the caller already rendered** (``image=``) — e.g. by runspec-console's
  ``render_diagram`` tool. Attached and shown exactly like the Kroki image
  below; needs nothing set up on the host, so it is the default path.

- **macro** — inserted as a Mermaid / PlantUML app's macro with the text as its
  body. Confluence draws it live and people can edit the text later, but only if
  that app is installed. Macro names differ by vendor, so they come from the
  config (``mermaid_macro`` has no default; ``plantuml_macro`` defaults to
  ``plantuml``).
- **image** — rendered to PNG by a `Kroki <https://kroki.io>`_ server, uploaded
  as a page attachment and shown with ``ac:image``. Works on any Confluence with
  no app. The source text rides along in a collapsed "source" expand under the
  image, so the diagram can be read back and re-rendered. Re-adding a diagram
  with the same name uploads a new attachment version and leaves the page body
  alone apart from refreshing that source block (the image already points at
  it).

Kroki and the macro are **operator opt-in**: with neither ``CONFLUENCE_KROKI_URL``
nor ``CONFLUENCE_DIAGRAM_MODE`` set, a call without ``image`` refuses (before
touching the page) and says to render the PNG first. The agent never picks a
renderer — the runnable doesn't expose ``mode`` — so a host without one is never
offered a path that would fail.

draw.io is deliberately not a target: its macro shows a PNG preview that only
draw.io itself can render.
"""

from __future__ import annotations

import os
import re
import tempfile
from html import escape
from typing import Any

import requests

from runspec_confluence_core.attachments import upload_attachment
from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig
from runspec_confluence_core.errors import ConfluenceError

# Diagram languages Kroki renders to PNG (a subset of what it supports — the
# ones whose PNG output is reliable).
IMAGE_KINDS = ("mermaid", "plantuml", "c4plantuml", "graphviz", "d2", "ditaa", "erd", "nomnoml", "bpmn", "structurizr")
# Languages a Confluence app can draw from a macro.
MACRO_KINDS = ("mermaid", "plantuml", "c4plantuml")
MODES = ("macro", "image")

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def diagram_filename(name: str) -> str:
    """The PNG attachment name for a diagram (``Order flow`` → ``order-flow.png``)."""
    stem = _NAME_RE.sub("-", name.strip()).strip("-.").lower()
    if not stem:
        raise ConfluenceError("Diagram name must contain letters or digits")
    return f"{stem}.png"


NO_RENDERER = (
    "No diagram renderer is set up for Confluence on this host — render the diagram to a PNG first "
    "(e.g. runspec-console's render_diagram tool) and pass it as the image, or have the operator set "
    "CONFLUENCE_KROKI_URL or CONFLUENCE_DIAGRAM_MODE=macro"
)


def resolve_mode(config: ConfluenceConfig, mode: str | None) -> str:
    """``macro`` / ``image`` — explicit ``mode``, else the config's
    ``diagram_mode``, else ``image`` when a Kroki server is configured. Raises
    :data:`NO_RENDERER` when nothing is set up (both renderers are opt-in)."""
    chosen = (mode or "auto").lower()
    if chosen == "auto":
        chosen = config.diagram_mode or ("image" if config.kroki_url else "")
        if not chosen:
            raise ConfluenceError(NO_RENDERER)
    if chosen not in MODES:
        raise ConfluenceError(f"Unknown diagram mode {chosen!r} — expected macro or image")
    return chosen


def macro_name(config: ConfluenceConfig, kind: str) -> str:
    """The Confluence macro that draws ``kind``, from the config."""
    if kind == "mermaid":
        if not config.mermaid_macro:
            raise ConfluenceError(
                "No Mermaid macro configured — set CONFLUENCE_MERMAID_MACRO to your Mermaid app's macro name (see list-macros on a page that has one), or use mode=image with CONFLUENCE_KROKI_URL"
            )
        return config.mermaid_macro
    if kind in ("plantuml", "c4plantuml"):
        return config.plantuml_macro
    raise ConfluenceError(f"{kind} can't be inserted as a macro — use mode=image (supported: {', '.join(IMAGE_KINDS)})")


def _cdata(text: str) -> str:
    # "]]>" can't appear inside CDATA; split it across two sections.
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def macro_storage(name: str, source: str) -> str:
    """Storage XHTML for a text-diagram macro with ``source`` as its body."""
    return f'<ac:structured-macro ac:name="{escape(name)}" ac:schema-version="1"><ac:plain-text-body>{_cdata(source)}</ac:plain-text-body></ac:structured-macro>'


def image_storage(filename: str, kind: str, source: str, *, caption: str | None = None) -> str:
    """Storage XHTML for a rendered diagram: the image, an optional caption, and
    the source in a collapsed expand (as a code block tagged with ``kind``)."""
    alt = escape(caption or filename)
    parts = [f'<p><ac:image ac:alt="{alt}"><ri:attachment ri:filename="{escape(filename)}" /></ac:image></p>']
    if caption:
        parts.append(f"<p><em>{escape(caption)}</em></p>")
    parts.append(
        '<ac:structured-macro ac:name="expand" ac:schema-version="1">'
        f'<ac:parameter ac:name="title">Diagram source ({escape(filename)})</ac:parameter>'
        "<ac:rich-text-body>"
        '<ac:structured-macro ac:name="code" ac:schema-version="1">'
        f'<ac:parameter ac:name="language">{escape(kind)}</ac:parameter>'
        f"<ac:plain-text-body>{_cdata(source)}</ac:plain-text-body>"
        "</ac:structured-macro></ac:rich-text-body></ac:structured-macro>"
    )
    return "".join(parts)


def render_kroki(config: ConfluenceConfig, kind: str, source: str, *, fmt: str = "png", timeout: float = 30.0) -> bytes:
    """Render diagram ``source`` with the configured Kroki server. A syntax
    error comes back as a :class:`ConfluenceError` carrying Kroki's message, so
    the caller can fix the text before anything touches the page."""
    if not config.kroki_url:
        raise ConfluenceError("No Kroki server configured — set CONFLUENCE_KROKI_URL (e.g. a self-hosted https://kroki.example.com)")
    if kind not in IMAGE_KINDS:
        raise ConfluenceError(f"Kroki can't render {kind!r} here — supported: {', '.join(IMAGE_KINDS)}")
    url = f"{config.kroki_url}/{kind}/{fmt}"
    try:
        resp = requests.post(
            url,
            data=source.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=timeout,
            verify=config.resolve_verify(),
            proxies=config.proxies,
        )
    except requests.RequestException as exc:
        raise ConfluenceError(f"Kroki request to {config.kroki_url} failed: {exc}") from exc
    if resp.status_code >= 400:
        detail = resp.text.strip()[:500] or f"HTTP {resp.status_code}"
        raise ConfluenceError(f"Kroki could not render the {kind} diagram: {detail}")
    return resp.content


def _page(client: Any, page_id: str) -> tuple[str, str]:
    page = client.get_page_by_id(page_id, expand="body.storage,version")
    if not isinstance(page, dict):
        raise ConfluenceError(f"Page {page_id} not found")
    body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    return body, str(page.get("title") or "")


def _attachment_referenced(storage: str, filename: str) -> bool:
    return f'ri:filename="{escape(filename)}"' in storage


def _replace_source(storage: str, filename: str, source: str) -> str | None:
    """Swap the text in the "Diagram source (<filename>)" expand this module
    wrote; ``None`` when that block isn't on the page. Plain string surgery so
    the rest of the page's storage is left byte-for-byte as it was."""
    anchor = storage.find(f"Diagram source ({escape(filename)})</ac:parameter>")
    if anchor < 0:
        return None
    start = storage.find("<ac:plain-text-body>", anchor)
    end = storage.find("</ac:plain-text-body>", start)
    if start < 0 or end < 0:
        return None
    start += len("<ac:plain-text-body>")
    return storage[:start] + _cdata(source) + storage[end:]


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _read_png(image: str | os.PathLike[str]) -> bytes:
    path = os.fspath(image)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ConfluenceError(f"Can't read diagram image {path}: {exc.strerror or exc}") from exc
    if not data.startswith(_PNG_MAGIC):
        raise ConfluenceError(f"{path} is not a PNG image")
    return data


@translate_errors
def add_diagram(
    config: ConfluenceConfig,
    page_id: str,
    kind: str,
    source: str,
    name: str,
    *,
    image: str | os.PathLike[str] | None = None,
    mode: str | None = None,
    position: str = "append",
    caption: str | None = None,
) -> dict[str, Any]:
    """Add a diagram written as ``kind`` text (``mermaid``, ``plantuml`` …) to a page.

    ``image`` is a PNG of the diagram the caller already rendered; when given it
    is attached as-is (no renderer needed) and ``mode`` is ignored. Otherwise
    ``mode`` is ``macro`` / ``image`` / ``auto`` (see :func:`resolve_mode`).
    ``position`` is ``append`` or ``prepend``. In image mode a re-add with the
    same ``name`` uploads a new version of the PNG and refreshes its source block
    in place. In macro mode the text is validated with Kroki first when a server
    is configured. Returns
    ``{page_id, mode, kind, name, rendered_by?, macro?/attachment?, updated_body}``."""
    kind = kind.lower()
    if not source.strip():
        raise ConfluenceError("Diagram source is empty")
    if position not in ("append", "prepend"):
        raise ConfluenceError("position must be append or prepend")
    png = _read_png(image) if image is not None else None
    chosen = "image" if png is not None else resolve_mode(config, mode)
    client = build_client(config)
    storage, title = _page(client, page_id)
    result: dict[str, Any] = {"page_id": page_id, "mode": chosen, "kind": kind, "name": name}

    if chosen == "macro":
        macro = macro_name(config, kind)
        if config.kroki_url and kind in IMAGE_KINDS:
            render_kroki(config, kind, source)  # validate syntax before editing the page
        fragment = macro_storage(macro, source)
        result["macro"] = macro
    else:
        if png is None:
            png = render_kroki(config, kind, source)
            result["rendered_by"] = "kroki"
        else:
            result["rendered_by"] = "caller"
        filename = diagram_filename(name)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, filename)
            with open(path, "wb") as handle:
                handle.write(png)
            upload_attachment(config, page_id, path, name=filename, content_type="image/png", comment=f"Diagram {name} ({kind})")
        result["attachment"] = filename
        if _attachment_referenced(storage, filename):
            # The image already points at this attachment; just refresh the
            # source block so it matches the new version.
            refreshed = _replace_source(storage, filename, source)
            if refreshed is None or refreshed == storage:
                result["updated_body"] = False
                return result
            client.update_page(page_id, title, body=refreshed, representation="storage", version_comment=f"Update diagram {name}")
            result["updated_body"] = True
            return result
        fragment = image_storage(filename, kind, source, caption=caption)

    body = fragment + storage if position == "prepend" else storage + fragment
    client.update_page(page_id, title, body=body, representation="storage", version_comment=f"Add diagram {name}")
    result["updated_body"] = True
    return result
