"""diagrams.py — read draw.io and Gliffy diagrams on a Confluence page.

Both apps store a diagram as **page attachments**: the source file (draw.io
mxfile XML, or Gliffy JSON) named after the diagram, and a ``<name>.png``
preview. The page's macro only points at them (see :mod:`macros`). So reading a
diagram is: fetch the page's storage body → find the diagram macro → fetch the
source attachment → decode it into a text outline (nodes with their labels,
edges as ``source → target``), which is what an agent can reason about.

Decoding is stdlib only:

- **draw.io** — an ``<mxfile>`` of ``<diagram>`` pages, each holding either a
  plain ``<mxGraphModel>`` or the compressed form (``base64`` → raw deflate →
  URL-encoded XML). Cells are ``<mxCell vertex="1">`` / ``<mxCell edge="1"
  source=… target=…>``, sometimes wrapped in ``<UserObject>`` / ``<object>``
  which carry the id and label. Labels may be HTML; they are reduced to text.
- **Gliffy** — undocumented JSON; read best-effort (shape text and lines with
  their end constraints).

No rendering: the ``.png`` preview attachment already is the rendered diagram,
and :func:`download_diagram` saves it alongside the source.
"""

from __future__ import annotations

import base64
import json
import os
import re
import xml.etree.ElementTree as ET
import zlib
from html import unescape
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig
from runspec_confluence_core.errors import ConfluenceError
from runspec_confluence_core.macros import diagrams, list_macros

# ── decoding ────────────────────────────────────────────────────────────────


def _label_text(value: str | None) -> str:
    """A draw.io label (plain or HTML) as one line of text."""
    if not value:
        return ""
    if "<" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _inflate_diagram(text: str) -> str:
    """Decode a compressed draw.io ``<diagram>`` payload to mxGraphModel XML."""
    raw = base64.b64decode(text.strip())
    return unquote(zlib.decompress(raw, -15).decode("utf-8"))


def _style_shape(style: str | None) -> str | None:
    """The shape a draw.io style names (``shape=cylinder3`` → ``cylinder3``, or a
    leading bare token such as ``ellipse`` / ``rhombus`` / ``swimlane``)."""
    if not style:
        return None
    parts = [p for p in style.split(";") if p]
    for part in parts:
        if part.startswith("shape="):
            return part.split("=", 1)[1]
    if parts and "=" not in parts[0]:
        return parts[0]
    return None


def _graph_models(root: ET.Element) -> list[tuple[str, ET.Element]]:
    """``(page name, mxGraphModel)`` for every page in an mxfile (or a bare model)."""
    if root.tag == "mxGraphModel":
        return [("Page-1", root)]
    pages: list[tuple[str, ET.Element]] = []
    for index, diagram in enumerate(root.iter("diagram"), start=1):
        name = diagram.get("name") or f"Page-{index}"
        model = diagram.find("mxGraphModel")
        if model is None and (diagram.text or "").strip():
            try:
                model = ET.fromstring(_inflate_diagram(diagram.text or ""))
            except (ValueError, zlib.error, ET.ParseError) as exc:
                raise ConfluenceError(f"Could not decode draw.io page {name!r}: {exc}") from exc
        if model is not None:
            pages.append((name, model))
    return pages


def _cells(model: ET.Element) -> list[dict[str, Any]]:
    """Flatten a model's cells, folding ``UserObject``/``object`` wrappers into
    their inner ``mxCell`` (the wrapper carries the id and label)."""
    root = model.find("root")
    if root is None:
        return []
    cells: list[dict[str, Any]] = []
    for el in root:
        inner = el if el.tag == "mxCell" else el.find("mxCell")
        if inner is None:
            continue
        cell = inner
        attrs: dict[str, Any] = dict(cell.attrib)
        if cell is not el:
            attrs.update(id=el.get("id", ""), value=el.get("label", ""))
        attrs["vertex"] = cell.get("vertex", "")
        attrs["edge"] = cell.get("edge", "")
        cells.append(attrs)
    return cells


def _ref(labels: dict[str, str], cell_id: str | None) -> str:
    """A cell's label, else its id (``?`` for a dangling connector end)."""
    if not cell_id:
        return "?"
    return labels.get(cell_id) or cell_id


def decode_drawio(data: bytes | str) -> dict[str, Any]:
    """Decode a draw.io file into ``{pages: [{name, nodes, edges}], summary}``.

    ``nodes`` are ``{id, label, shape?, in?}`` (``in`` = the containing group or
    swimlane's label); ``edges`` are ``{source, target, label?}`` using node labels
    (falling back to ids). ``summary`` is the same as plain lines."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ConfluenceError(f"Not a draw.io file (XML parse failed: {exc})") from exc
    if root.tag not in ("mxfile", "mxGraphModel"):
        raise ConfluenceError(f"Not a draw.io file (root element <{root.tag}>)")

    pages: list[dict[str, Any]] = []
    summary: list[str] = []
    for name, model in _graph_models(root):
        cells = _cells(model)
        labels = {c["id"]: _label_text(c.get("value")) for c in cells}
        vertex_ids = {c["id"] for c in cells if c["vertex"] == "1"}

        edge_ids = {c["id"] for c in cells if c["edge"] == "1"}
        nodes: list[dict[str, Any]] = []
        edges: dict[str, dict[str, Any]] = {}
        edge_labels: dict[str, str] = {}
        for cell in cells:
            parent = cell.get("parent")
            if cell["vertex"] == "1" and parent and parent in edge_ids:
                # A text vertex parented to an edge is that edge's label.
                if labels[cell["id"]]:
                    edge_labels.setdefault(parent, labels[cell["id"]])
            elif cell["vertex"] == "1":
                node: dict[str, Any] = {"id": cell["id"], "label": labels[cell["id"]]}
                shape = _style_shape(cell.get("style"))
                if shape:
                    node["shape"] = shape
                if parent in vertex_ids:
                    node["in"] = _ref(labels, parent)
                nodes.append(node)
            elif cell["edge"] == "1":
                edges[cell["id"]] = {"source": _ref(labels, cell.get("source")), "target": _ref(labels, cell.get("target"))}
                if labels.get(cell["id"]):
                    edge_labels[cell["id"]] = labels[cell["id"]]
        for edge_id, label in edge_labels.items():
            edges[edge_id]["label"] = label
        pages.append({"name": name, "nodes": nodes, "edges": list(edges.values())})

        summary.append(f"{name}: {len(nodes)} shapes, {len(edges)} connectors")
        for node in nodes:
            if node["label"]:
                where = f" (in {node['in']})" if node.get("in") else ""
                summary.append(f"  - {node['label']}{where}")
        for edge in edges.values():
            label = f" [{edge['label']}]" if edge.get("label") else ""
            summary.append(f"  {edge['source']} -> {edge['target']}{label}")
    return {"format": "drawio", "pages": pages, "summary": summary}


def _gliffy_text(obj: dict[str, Any]) -> str:
    parts: list[str] = []
    text = ((obj.get("graphic") or {}).get("Text") or {}).get("html")
    if text:
        parts.append(_label_text(text))
    for child in obj.get("children") or []:
        if isinstance(child, dict):
            parts.append(_gliffy_text(child))
    return " ".join(p for p in parts if p)


def decode_gliffy(data: bytes | str) -> dict[str, Any]:
    """Best-effort Gliffy JSON → ``{pages: [{name, nodes, edges}], summary}``
    (same shape as :func:`decode_drawio`). Gliffy's format is undocumented, so
    shapes without text or lines without end constraints may be missing."""
    try:
        doc = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise ConfluenceError(f"Not a Gliffy file (JSON parse failed: {exc})") from exc
    objects = ((doc.get("stage") or {}).get("objects")) if isinstance(doc, dict) else None
    if not isinstance(objects, list):
        raise ConfluenceError("Not a Gliffy file (no stage.objects)")

    labels: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        graphic = obj.get("graphic") or {}
        oid = str(obj.get("id", ""))
        if graphic.get("type") == "Line":
            lines.append(obj)
            continue
        label = _gliffy_text(obj)
        labels[oid] = label
        node: dict[str, Any] = {"id": oid, "label": label}
        if obj.get("uid"):
            node["shape"] = str(obj["uid"]).rsplit(".", 1)[-1]
        nodes.append(node)

    def end(obj: dict[str, Any], key: str, inner: str) -> str:
        node_id = (((obj.get("constraints") or {}).get(key) or {}).get(inner) or {}).get("nodeId")
        return (labels.get(str(node_id)) or str(node_id)) if node_id is not None else "?"

    edges = []
    for line in lines:
        edge: dict[str, Any] = {
            "source": end(line, "startConstraint", "StartPositionConstraint"),
            "target": end(line, "endConstraint", "EndPositionConstraint"),
        }
        text = _gliffy_text(line)
        if text:
            edge["label"] = text
        edges.append(edge)

    summary = [f"Gliffy: {len(nodes)} shapes, {len(edges)} lines"]
    summary += [f"  - {n['label']}" for n in nodes if n["label"]]
    summary += [f"  {e['source']} -> {e['target']}" + (f" [{e['label']}]" if e.get("label") else "") for e in edges]
    return {"format": "gliffy", "pages": [{"name": "Gliffy", "nodes": nodes, "edges": edges}], "summary": summary}


# ── Confluence operations ───────────────────────────────────────────────────


def _storage(client: Any, page_id: str) -> tuple[str, str]:
    page = client.get_page_by_id(page_id, expand="body.storage")
    if not isinstance(page, dict):
        raise ConfluenceError(f"Page {page_id} not found")
    body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    return body, str(page.get("title") or "")


@translate_errors
def page_macros(config: ConfluenceConfig, page_id: str) -> dict[str, Any]:
    """List the macros on a page (see :func:`macros.list_macros`), with a count
    per macro name."""
    storage, title = _storage(build_client(config), page_id)
    found = list_macros(storage)
    counts: dict[str, int] = {}
    for macro in found:
        counts[macro["name"]] = counts.get(macro["name"], 0) + 1
    return {"page_id": page_id, "title": title, "counts": counts, "macros": found}


def _pick(refs: list[dict[str, Any]], name: str | None, page_id: str) -> dict[str, Any]:
    if not refs:
        raise ConfluenceError(f"No draw.io or Gliffy diagrams on page {page_id}")
    if name is None:
        if len(refs) == 1:
            return refs[0]
        names = ", ".join(r["name"] for r in refs)
        raise ConfluenceError(f"Page {page_id} has {len(refs)} diagrams; pass a name: {names}")
    for ref in refs:
        if ref["name"] == name:
            return ref
    for ref in refs:
        if ref["name"].lower() == name.lower():
            return ref
    names = ", ".join(r["name"] for r in refs)
    raise ConfluenceError(f"No diagram named {name!r} on page {page_id}; found: {names}")


def _attachment(client: Any, page_id: str, filenames: list[str]) -> dict[str, Any] | None:
    """The first attachment on ``page_id`` matching one of ``filenames``."""
    for filename in filenames:
        resp = client.get_attachments_from_content(page_id, filename=filename)
        for att in (resp or {}).get("results") or []:
            if att.get("title") == filename:
                return att
    return None


def _download(client: Any, attachment: dict[str, Any]) -> bytes:
    link = ((attachment.get("_links") or {}).get("download")) or ""
    if not link:
        raise ConfluenceError(f"Attachment {attachment.get('title')!r} has no download link")
    data = client.get(link.lstrip("/"), not_json_response=True)
    if isinstance(data, str):
        return data.encode("utf-8")
    if not isinstance(data, bytes):
        raise ConfluenceError(f"Unexpected download response for {attachment.get('title')!r}")
    return data


def _resolve(client: Any, page_id: str, name: str | None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """``(diagram ref, page id holding the attachments, source attachment)``."""
    storage, _ = _storage(client, page_id)
    ref = _pick(diagrams(storage), name, page_id)
    holder = str(ref.get("page_id") or page_id)
    source = _attachment(client, holder, ref["attachments"])
    if source is None:
        tried = ", ".join(ref["attachments"])
        raise ConfluenceError(f"Diagram {ref['name']!r}: no source attachment on page {holder} (tried {tried})")
    return ref, holder, source


def _decode(kind: str, data: bytes) -> dict[str, Any]:
    return decode_drawio(data) if kind == "drawio" else decode_gliffy(data)


@translate_errors
def get_diagram(config: ConfluenceConfig, page_id: str, *, name: str | None = None, include_source: bool = False) -> dict[str, Any]:
    """Read a draw.io or Gliffy diagram on a page as a text outline.

    ``name`` picks the diagram when the page has several (case-insensitive
    fallback). Returns ``{page_id, diagram, attachment, pages, summary}``;
    ``include_source`` adds the decoded source text as ``source``."""
    client = build_client(config)
    ref, holder, source = _resolve(client, page_id, name)
    data = _download(client, source)
    outline = _decode(ref["kind"], data)
    result: dict[str, Any] = {
        "page_id": page_id,
        "diagram": ref,
        "attachment": {"page_id": holder, "title": source.get("title"), "version": (source.get("version") or {}).get("number")},
        **outline,
    }
    if include_source:
        result["source"] = data.decode("utf-8", errors="replace")
    return result


def _safe_filename(title: str) -> str:
    base = title.replace("\\", "/").rsplit("/", 1)[-1].strip() or "diagram"
    return base if base not in (".", "..") else "diagram"


@translate_errors
def download_diagram(config: ConfluenceConfig, page_id: str, dest: str, *, name: str | None = None) -> dict[str, Any]:
    """Save a diagram's source file and its ``.png`` preview into ``dest``.

    The draw.io source is written with a ``.drawio`` extension (opens in draw.io
    desktop / diagrams.net); Gliffy keeps ``.gliffy``. The preview is skipped
    (``preview: null``) when the page has none."""
    client = build_client(config)
    ref, holder, source = _resolve(client, page_id, name)
    os.makedirs(dest, exist_ok=True)
    ext = ".drawio" if ref["kind"] == "drawio" else ".gliffy"
    stem = _safe_filename(str(source.get("title") or ref["name"]))
    if stem.endswith((".drawio", ".gliffy", ".xml")):
        stem = stem.rsplit(".", 1)[0]
    source_path = os.path.join(dest, stem + ext)
    with open(source_path, "wb") as handle:
        handle.write(_download(client, source))
    preview_path = None
    preview = _attachment(client, holder, [ref["preview"]])
    if preview is not None:
        preview_path = os.path.join(dest, stem + ".png")
        with open(preview_path, "wb") as handle:
            handle.write(_download(client, preview))
    return {"page_id": page_id, "diagram": ref, "source": source_path, "preview": preview_path}
