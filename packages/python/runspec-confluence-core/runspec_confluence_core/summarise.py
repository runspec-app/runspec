"""summarise.py — read-side result shaping: Confluence HTML → Markdown and
trimmed "simplified" dicts.

This is the **read** counterpart to :mod:`runspec_confluence_core.mdconvert`, and
the two are deliberately **not inverses**:

- ``mdconvert.markdown_to_storage`` — Markdown → Confluence storage XHTML, via the
  ``markdown`` library. Used on **writes** (``create_page`` / ``update_page`` with
  ``--content-format markdown``).
- ``summarise.html_to_markdown`` — Confluence HTML (storage/view body) → Markdown,
  via the ``markdownify`` library. Used on **reads** to render a page body back to
  readable Markdown.

``simplify_*`` reduce a raw ``atlassian-python-api`` response to a small, stable
dict — the read equivalent of mcp-atlassian's model ``to_simplified_dict()``,
here as plain dict→dict functions (no Pydantic). Every field is best-effort: a key
appears only when present in the source, so a partial API response never raises.
"""

from __future__ import annotations

from typing import Any

from markdownify import markdownify as _md


def html_to_markdown(html: str | None) -> str:
    """Convert Confluence HTML (storage or rendered ``view`` body) to Markdown
    (best-effort, via ``markdownify``). Confluence-specific macros are not
    interpreted.

    Overrides two markdownify defaults that read poorly for documents: headings
    render as **ATX** (``# Heading``) rather than Setext (Setext can't express h3+,
    so any real document mixes both styles), and bullets are a stable ``-`` rather
    than the default ``*+-`` that cycles by nesting depth (which makes nested lists
    harder to read)."""
    if not html:
        return ""
    return _md(html, heading_style="ATX", bullets="-").strip()


def _dig(raw: Any, *keys: str) -> Any:
    """Walk nested dicts by ``keys``, returning ``None`` if any hop is missing."""
    for key in keys:
        if not isinstance(raw, dict):
            return None
        raw = raw.get(key)
    return raw


def _link_url(raw: dict[str, Any]) -> str | None:
    """Compose an absolute URL from a resource's ``_links`` (base + webui)."""
    links = raw.get("_links") or {}
    webui = links.get("webui") or links.get("self")
    if not webui:
        return None
    base = links.get("base")
    if base and webui.startswith("/"):
        return f"{base}{webui}"
    return webui


def _person(raw: dict[str, Any] | None) -> str | None:
    """A person's display name from a Confluence user object."""
    if not isinstance(raw, dict):
        return None
    return raw.get("displayName") or raw.get("publicName") or raw.get("username")


def _body(raw: dict[str, Any]) -> dict[str, str] | None:
    """Pluck a page body as ``{value, format}`` (prefers ``storage``, then ``view``)."""
    body = raw.get("body") or {}
    for fmt in ("storage", "view", "editor"):
        section = body.get(fmt)
        if isinstance(section, dict) and section.get("value"):
            return {"value": section["value"], "format": fmt}
    return None


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    """Set ``key`` only when ``value`` is meaningful (not None / empty)."""
    if value:
        out[key] = value


def simplify_page(raw: dict[str, Any], *, body_as_markdown: bool = False) -> dict[str, Any]:
    """Trim a page response to id/title/type/space/version/author/url/body.

    ``body_as_markdown`` opts into converting the body HTML to Markdown (via
    :func:`html_to_markdown`) — the body is then ``{"value": <markdown>, "format":
    "markdown"}`` — instead of embedding the raw storage/view HTML verbatim."""
    out: dict[str, Any] = {}
    _put(out, "id", raw.get("id"))
    _put(out, "type", raw.get("type"))
    _put(out, "status", raw.get("status"))
    _put(out, "title", raw.get("title"))
    _put(out, "url", _link_url(raw))
    space = raw.get("space") or {}
    if space:
        _put(out, "space", {k: space[k] for k in ("key", "name") if space.get(k)})
    version = raw.get("version") or {}
    if version:
        vout: dict[str, Any] = {}
        _put(vout, "number", version.get("number"))
        _put(vout, "when", version.get("when"))
        _put(vout, "by", _person(version.get("by")))
        _put(out, "version", vout)
    history = raw.get("history") or {}
    _put(out, "created", history.get("createdDate"))
    _put(out, "author", _person(history.get("createdBy")))
    ancestors = raw.get("ancestors") or []
    if ancestors:
        out["ancestors"] = [{"id": a.get("id"), "title": a.get("title")} for a in ancestors]
    body = _body(raw)
    if body and body_as_markdown:
        body = {"value": html_to_markdown(body["value"]), "format": "markdown"}
    _put(out, "body", body)
    return out


def simplify_comment(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a comment response to id/title/body/version/author/created/url.

    The version's ``by``/``number`` describe the last edit, so the original
    author and creation date are read from ``history`` (present when the fetch
    expands it — e.g. ``get_comments(..., expand="body.view,history")``)."""
    out: dict[str, Any] = {}
    _put(out, "id", raw.get("id"))
    _put(out, "title", raw.get("title"))
    _put(out, "url", _link_url(raw))
    body = _body(raw)
    if body:
        _put(out, "body", body.get("value"))
    version = raw.get("version") or {}
    if version:
        _put(out, "version", version.get("number"))
        _put(out, "by", _person(version.get("by")))
    history = raw.get("history") or {}
    _put(out, "created", history.get("createdDate"))
    _put(out, "author", _person(history.get("createdBy")))
    return out


def simplify_space(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a space response to id/key/name/type/status/description/url."""
    out: dict[str, Any] = {}
    _put(out, "id", raw.get("id"))
    _put(out, "key", raw.get("key"))
    _put(out, "name", raw.get("name"))
    _put(out, "type", raw.get("type"))
    _put(out, "status", raw.get("status"))
    _put(out, "description", _dig(raw, "description", "plain", "value"))
    _put(out, "url", _link_url(raw))
    return out


def simplify_user(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a user response to identity fields, reading **both** Cloud and
    Server/Data Center shapes.

    Cloud returns ``accountId``/``accountType`` and a top-level ``email``;
    Server/DC returns ``type``/``username``/``userKey``, nests the address at
    ``details.business.email``, and carries an avatar at ``profilePicture.path``.
    """
    out: dict[str, Any] = {}
    _put(out, "accountId", raw.get("accountId"))
    _put(out, "accountType", raw.get("accountType"))
    _put(out, "type", raw.get("type"))
    _put(out, "userKey", raw.get("userKey"))
    _put(out, "username", raw.get("username") or raw.get("name"))
    _put(out, "displayName", _person(raw))
    _put(out, "email", raw.get("email") or raw.get("emailAddress") or _dig(raw, "details", "business", "email"))
    _put(out, "avatar", _dig(raw, "profilePicture", "path"))
    return out


def simplify_attachment(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim an attachment response to id/title/mediaType/fileSize/version/created/url."""
    out: dict[str, Any] = {}
    _put(out, "id", raw.get("id"))
    _put(out, "title", raw.get("title"))
    metadata = raw.get("metadata") or {}
    _put(out, "mediaType", metadata.get("mediaType"))
    extensions = raw.get("extensions") or {}
    # fileSize has lived under both extensions and metadata across API versions.
    _put(out, "fileSize", extensions.get("fileSize") or metadata.get("fileSize"))
    version = raw.get("version") or {}
    if version:
        _put(out, "version", version.get("number"))
    history = raw.get("history") or {}
    _put(out, "created", history.get("createdDate"))
    _put(out, "created_by", _person(history.get("createdBy")))
    links = raw.get("_links") or {}
    _put(out, "download", links.get("download"))
    _put(out, "url", _link_url(raw))
    return out
