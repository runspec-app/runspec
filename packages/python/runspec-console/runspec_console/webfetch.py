"""webfetch.py — fetch an http(s) URL and render it as readable text.

Pure, stdlib-only logic behind the ``fetch_url`` built-in agent tool
(bridge_agent_tools). Given a URL it performs a GET (following redirects),
bounds the download, and — when the response is HTML — strips it to clean,
readable, lightly-marked-up text (headings as ``#``, list items as ``-``,
``<pre>`` preserved) so the model reads the *content* of a page (e.g. a GitLab
Pages doc it should turn into a runbook) rather than a wall of markup.

No third-party dependencies: ``urllib`` for the request and ``html.parser`` for
the HTML→text conversion. Keeping it dependency-free matches the console's lean
runtime footprint and means the capability works on a fresh, air-gapped-ish
install with nothing extra to ``pip install``.

The request goes through the console-wide ``[http]`` proxy (see
:mod:`runspec_console.netproxy`; by default the machine's ``HTTPS_PROXY`` /
``NO_PROXY`` environment) and, optionally, a private-CA bundle / a
TLS-verify escape hatch for corporate TLS-intercepting proxies — the same knobs
Config Sync already exposes for git-over-HTTPS.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any

from . import netproxy

# Default User-Agent — some sites (and WAFs) reject the bare ``Python-urllib``
# default, so present a plain, honest browser-ish string.
_DEFAULT_UA = "Mozilla/5.0 (compatible; runspec-console/agent; +https://runspec.dev)"

# A GET is bounded so a runaway page (or a mistargeted binary) can't blow the
# download or the model context. The tool caller can lower/raise it.
_DEFAULT_MAX_BYTES = 2_000_000
_DEFAULT_TIMEOUT_S = 20.0

# Elements whose *content* is never part of the readable text.
_SKIP_CONTENT = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "iframe"}
)

# Block-level elements that force a line break around their text.
_BLOCK = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "ul",
        "ol",
        "table",
        "tr",
        "form",
        "figure",
        "figcaption",
        "blockquote",
        "hr",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "pre",
        "nav",
        "dl",
        "dt",
        "dd",
        "details",
        "summary",
    }
)

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class WebFetchError(Exception):
    """A fetch failed — bad URL, network error, or non-2xx HTTP status. Carries a
    human-readable message the tool hands straight back to the model."""


class _HtmlToText(HTMLParser):
    """Minimal, forgiving HTML→text renderer.

    Not a full Markdown converter — it keeps just enough structure (headings,
    lists, paragraph breaks, verbatim ``<pre>``) to read a documentation page
    cleanly, and drops the rest (scripts, styles, attributes, tags). Robust to
    malformed markup by construction: ``HTMLParser`` never raises on bad input,
    and unbalanced skip/pre tags are tracked by depth counters that clamp at 0.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0  # inside <script>/<style>/… — swallow text
        self._pre_depth = 0  # inside <pre> — keep whitespace verbatim
        self._title_parts: list[str] = []
        self._in_title = False
        self.title = ""

    # ── tags ──────────────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            return
        # <title> lives inside <head> (a skip element) — let it through the skip
        # guard so the page title is still captured.
        if self._skip_depth and tag != "title":
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "pre":
            self._pre_depth += 1
        if tag in _HEADINGS:
            self._newline()
            self._parts.append("#" * _HEADINGS[tag] + " ")
        elif tag == "li":
            self._newline()
            self._parts.append("- ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag in _BLOCK:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())
            return
        if self._skip_depth:
            return
        if tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            self._newline()
        elif tag in _HEADINGS or tag in _BLOCK:
            self._newline()

    # ── text ──────────────────────────────────────────────────────────────
    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        if self._pre_depth:
            self._parts.append(data)
            return
        # Collapse intra-run whitespace; block handlers own the line breaks.
        collapsed = " ".join(data.split())
        if not collapsed:
            # Preserve a single separating space if the raw data had any.
            if data and not (self._parts and self._parts[-1].endswith((" ", "\n"))):
                self._parts.append(" ")
            return
        if (
            data[:1].isspace()
            and self._parts
            and not self._parts[-1].endswith((" ", "\n"))
        ):
            self._parts.append(" ")
        self._parts.append(collapsed)
        if data[-1:].isspace():
            self._parts.append(" ")

    def _newline(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse 3+ blank lines to a single blank line, trim trailing spaces.
        lines = [ln.rstrip() for ln in raw.splitlines()]
        out: list[str] = []
        blanks = 0
        for ln in lines:
            if ln:
                blanks = 0
                out.append(ln)
            else:
                blanks += 1
                if blanks <= 1:
                    out.append("")
        return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Render an HTML document to ``(text, title)``. Never raises on bad markup."""
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is forgiving, but guard anyway — a partial render beats none.
        pass
    return parser.text(), parser.title


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            cs = part.split("=", 1)[1].strip().strip('"').strip("'")
            return cs or None
    return None


def _is_texty(content_type: str) -> bool:
    ct = content_type.lower()
    return (
        ct.startswith("text/")
        or "html" in ct
        or "xml" in ct
        or "json" in ct
        or "javascript" in ct
        or "+text" in ct
    )


def _build_opener(
    ca_bundle: str | None,
    tls_verify: bool,
    proxy_settings: netproxy.ProxySettings | None = None,
) -> urllib.request.OpenerDirector:
    """An opener routed through the console-wide ``[http]`` proxy
    (``proxy_settings`` overrides it, e.g. to test unsaved settings) and,
    optionally, a custom CA bundle or a verification-disabled context for
    TLS-intercepting corporate proxies. Mirrors the knobs Config Sync exposes
    for git-over-HTTPS."""
    handlers: list[urllib.request.BaseHandler] = [
        netproxy.urllib_handler(proxy_settings)
    ]
    ctx: ssl.SSLContext | None = None
    if not tls_verify:
        ctx = ssl._create_unverified_context()
    elif ca_bundle:
        ctx = ssl.create_default_context(cafile=ca_bundle)
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def fetch_url(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_S,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    as_text: bool = True,
    ca_bundle: str | None = None,
    tls_verify: bool = True,
    user_agent: str = _DEFAULT_UA,
    proxy_settings: netproxy.ProxySettings | None = None,
) -> dict[str, Any]:
    """GET ``url`` and return a structured, readable result.

    Follows redirects, bounds the download to ``max_bytes``, and — when the
    response is HTML and ``as_text`` — strips it to clean text. Returns a dict:
    ``{url, final_url, status, content_type, title, text, bytes, truncated,
    encoding, rendered}``. ``rendered`` is ``"html-text"`` when HTML was
    converted, ``"text"`` for other textual bodies, ``"raw-html"`` when
    ``as_text`` is False.

    Raises ``WebFetchError`` for a non-http(s) scheme, a network failure, or a
    non-2xx HTTP status.
    """
    url = (url or "").strip()
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise WebFetchError(
            f"Only http/https URLs are supported (got {scheme or 'no'} scheme). "
            "Pass a full URL like https://example.gitlab.io/docs/page.html."
        )
    max_bytes = max(1, int(max_bytes))
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        method="GET",
    )
    opener = _build_opener(ca_bundle, tls_verify, proxy_settings)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 0
            final_url = resp.geturl() or url
            content_type = resp.headers.get("Content-Type", "") or ""
            # Read one byte past the cap so truncation is detectable.
            body = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:  # non-2xx
        raise WebFetchError(f"HTTP {e.code} {e.reason} fetching {url}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise WebFetchError(f"Could not fetch {url}: {reason}") from e
    except ssl.SSLError as e:
        raise WebFetchError(
            f"TLS error fetching {url}: {e}. If this is an internal host behind "
            "a TLS-intercepting proxy, set a ca_bundle or tls_verify=false."
        ) from e
    except (TimeoutError, OSError) as e:
        raise WebFetchError(f"Could not fetch {url}: {e}") from e

    truncated = len(body) > max_bytes
    if truncated:
        body = body[:max_bytes]

    encoding = _charset(content_type) or "utf-8"
    result: dict[str, Any] = {
        "url": url,
        "final_url": final_url,
        "status": int(status),
        "content_type": content_type,
        "bytes": len(body),
        "truncated": truncated,
        "encoding": encoding,
        "title": "",
    }

    if not _is_texty(content_type) and content_type:
        result["rendered"] = "binary"
        result["text"] = ""
        result["note"] = (
            f"Response is {content_type} — not a text/HTML page, so no text was "
            "extracted. Use a file-transfer tool if you need the raw bytes."
        )
        return result

    try:
        decoded = body.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        decoded = body.decode("utf-8", errors="replace")

    looks_html = "html" in content_type.lower() or (
        not content_type and "<html" in decoded[:2048].lower()
    )
    if as_text and looks_html:
        text, title = html_to_text(decoded)
        result["text"] = text
        result["title"] = title or ""
        result["rendered"] = "html-text"
    elif as_text and looks_html is False and content_type == "":
        # No content-type and no <html marker — treat as plain text.
        result["text"] = decoded
        result["rendered"] = "text"
    elif not as_text:
        result["text"] = decoded
        result["rendered"] = "raw-html" if looks_html else "text"
    else:
        result["text"] = decoded
        result["rendered"] = "text"
    return result
