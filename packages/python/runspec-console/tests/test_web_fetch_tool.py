"""
test_web_fetch_tool.py — the read-only ``fetch_url`` built-in agent tool and its
pure ``webfetch`` core.

``fetch_url`` GETs an http(s) page and hands the agent its readable text (HTML
stripped to headings/lists/paragraphs) so it can read an online doc — a GitLab
Pages / wiki page — and turn it into a runbook. A GET has no side effects, so
it's autonomous, and it's a hot built-in (inline even in search mode). The pure
converter and fetch live in ``webfetch``; the Bridge glue (schema, config
gating, dispatch) is bound onto a light stub, the pattern used across the suite.
"""

from __future__ import annotations

import json

from runspec_console import webfetch
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import (
    _BUILTIN_TOOL_NAMES,
    _HOT_BUILTIN_NAMES,
    _WEB_TOOL_NAMES,
)


# ── pure: html_to_text ──────────────────────────────────────────────────────────


def test_html_to_text_extracts_headings_lists_and_title():
    html = (
        "<html><head><title>Deploy Guide</title>"
        "<style>.x{color:red}</style></head><body>"
        "<h1>Deploy</h1><p>First run <code>make</code>.</p>"
        "<ul><li>step one</li><li>step two</li></ul>"
        "<script>evil()</script></body></html>"
    )
    text, title = webfetch.html_to_text(html)
    assert title == "Deploy Guide"
    assert "# Deploy" in text
    assert "- step one" in text and "- step two" in text
    # script/style content never leaks into the readable text.
    assert "evil()" not in text and "color:red" not in text


def test_html_to_text_preserves_pre_and_collapses_whitespace():
    html = "<p>a    b\n   c</p><pre>line1\n  line2</pre>"
    text, _ = webfetch.html_to_text(html)
    assert "a b c" in text  # intra-paragraph whitespace collapsed
    assert "line1\n  line2" in text  # <pre> kept verbatim


def test_html_to_text_survives_malformed_markup():
    # Unbalanced tags must not raise and must clamp skip/pre depth at 0.
    text, _ = webfetch.html_to_text("<div><p>hi</pre></script></div><h2>x")
    assert "hi" in text and "## x" in text.splitlines()


# ── pure: fetch_url (opener injected) ───────────────────────────────────────────


class _FakeHeaders:
    def __init__(self, ct):
        self._ct = ct

    def get(self, key, default=""):
        return self._ct if key.lower() == "content-type" else default


class _FakeResp:
    def __init__(self, body, ct="text/html; charset=utf-8", status=200, url=None):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.status = status
        self.headers = _FakeHeaders(ct)
        self._url = url

    def getcode(self):
        return self.status

    def geturl(self):
        return self._url or "https://example.com/final"

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, resp):
        self._resp = resp
        self.calls: list = []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, timeout))
        return self._resp


def _patch_opener(monkeypatch, resp):
    opener = _FakeOpener(resp)
    monkeypatch.setattr(webfetch, "_build_opener", lambda ca, verify: opener)
    return opener


def test_fetch_url_renders_html_to_text(monkeypatch):
    _patch_opener(
        monkeypatch, _FakeResp("<html><title>T</title><body><h1>Hi</h1></body></html>")
    )
    res = webfetch.fetch_url("https://team.gitlab.io/docs")
    assert res["rendered"] == "html-text"
    assert res["title"] == "T" and "# Hi" in res["text"]
    assert res["status"] == 200 and res["final_url"] == "https://example.com/final"
    assert res["truncated"] is False


def test_fetch_url_rejects_non_http_scheme():
    for bad in ("file:///etc/passwd", "ftp://host/x", "javascript:alert(1)", "nope"):
        try:
            webfetch.fetch_url(bad)
        except webfetch.WebFetchError as e:
            assert "http" in str(e).lower()
        else:  # pragma: no cover
            raise AssertionError(f"expected rejection for {bad}")


def test_fetch_url_marks_truncation(monkeypatch):
    _patch_opener(monkeypatch, _FakeResp("x" * 5000, ct="text/plain"))
    res = webfetch.fetch_url("https://h/x", max_bytes=1000)
    assert res["truncated"] is True and res["bytes"] == 1000


def test_fetch_url_flags_binary_content(monkeypatch):
    _patch_opener(monkeypatch, _FakeResp(b"\x89PNG", ct="image/png"))
    res = webfetch.fetch_url("https://h/logo.png")
    assert res["rendered"] == "binary" and res["text"] == "" and "note" in res


def test_fetch_url_raw_returns_html(monkeypatch):
    _patch_opener(monkeypatch, _FakeResp("<html><body><b>hi</b></body></html>"))
    res = webfetch.fetch_url("https://h/x", as_text=False)
    assert res["rendered"] == "raw-html" and "<b>hi</b>" in res["text"]


def test_fetch_url_wraps_http_error(monkeypatch):
    import urllib.error

    class _Boom(_FakeOpener):
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(webfetch, "_build_opener", lambda ca, verify: _Boom(None))
    try:
        webfetch.fetch_url("https://h/missing")
    except webfetch.WebFetchError as e:
        assert "404" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected WebFetchError")


# ── bridge: registration / autonomy / dispatch ──────────────────────────────────


class _Stub:
    _web_tools = Bridge._web_tools
    _diagram_tools = Bridge._diagram_tools
    _run_web_tool = Bridge._run_web_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy


def test_fetch_url_is_a_hot_readonly_builtin():
    assert "fetch_url" in _WEB_TOOL_NAMES
    assert _WEB_TOOL_NAMES <= _BUILTIN_TOOL_NAMES
    assert _WEB_TOOL_NAMES <= _HOT_BUILTIN_NAMES  # inline even in search mode


def test_fetch_url_is_autonomous():
    assert _Stub()._effective_tool_autonomy("fetch_url", {"url": "https://x"}) == (
        "autonomous"
    )


def test_dispatch_routes_to_web_handler(monkeypatch):
    monkeypatch.setattr(
        webfetch,
        "_build_opener",
        lambda ca, verify: _FakeOpener(_FakeResp("<h1>Y</h1>")),
    )
    out = Bridge._run_tool_sync(_Stub(), "fetch_url", {"url": "https://h/x"})
    assert "# Y" in json.loads(out)["text"]


# ── bridge: schema + config gating ──────────────────────────────────────────────


def test_schema_offered_when_enabled(monkeypatch):
    monkeypatch.setattr("runspec_console.config.read_web", lambda: {"enabled": True})
    (tool,) = _Stub()._web_tools()
    assert tool["name"] == "fetch_url"
    assert tool["input_schema"]["required"] == ["url"]
    assert "gitlab" in tool["description"].lower()


def test_schema_dropped_when_disabled(monkeypatch):
    monkeypatch.setattr("runspec_console.config.read_web", lambda: {"enabled": False})
    assert _Stub()._web_tools() == []


def _cfg(**over):
    base = {
        "enabled": True,
        "timeout": 20.0,
        "max_bytes": 2_000_000,
        "ca_bundle": None,
        "tls_verify": True,
    }
    base.update(over)
    return base


def test_run_web_tool_requires_url(monkeypatch):
    monkeypatch.setattr("runspec_console.config.read_web", lambda: _cfg())
    assert "Error" in _Stub()._run_web_tool({})


def test_run_web_tool_refuses_when_disabled(monkeypatch):
    monkeypatch.setattr("runspec_console.config.read_web", lambda: _cfg(enabled=False))
    out = _Stub()._run_web_tool({"url": "https://h/x"})
    assert "disabled" in out


def test_run_web_tool_returns_error_string_on_failure(monkeypatch):
    monkeypatch.setattr("runspec_console.config.read_web", lambda: _cfg())
    out = _Stub()._run_web_tool({"url": "file:///etc/passwd"})
    assert out.startswith("Error:") and "http" in out.lower()


def test_run_web_tool_clamps_max_bytes_to_config(monkeypatch):
    monkeypatch.setattr("runspec_console.config.read_web", lambda: _cfg(max_bytes=500))
    seen: dict = {}

    def _fake_fetch(url, **kw):
        seen.update(kw)
        return {"text": "ok", "truncated": False}

    monkeypatch.setattr(webfetch, "fetch_url", _fake_fetch)
    # A caller asking for more than the config cap is clamped down to it.
    _Stub()._run_web_tool({"url": "https://h/x", "max_bytes": 999999})
    assert seen["max_bytes"] == 500
    assert seen["as_text"] is True
