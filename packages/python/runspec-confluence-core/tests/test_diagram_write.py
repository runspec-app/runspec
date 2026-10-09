"""Diagram creation: macro / Kroki image modes and page edits."""

import pytest

from runspec_confluence_core import (
    ConfluenceConfig,
    ConfluenceError,
    add_diagram,
    diagram_filename,
    image_storage,
    list_macros,
    macro_storage,
    resolve_mode,
)
from runspec_confluence_core import diagram_write as dw

BASE = {"url": "https://wiki.example.com", "cloud": False, "personal_token": "p"}
MERMAID = "graph TD\n  A-->B"


class FakeResp:
    def __init__(self, status: int, content: bytes = b"\x89PNG", text: str = ""):
        self.status_code = status
        self.content = content
        self.text = text


@pytest.fixture
def kroki(monkeypatch):
    calls: list[dict] = []

    def post(url, **kw):
        calls.append({"url": url, **kw})
        return FakeResp(200)

    monkeypatch.setattr(dw.requests, "post", post)
    return calls


@pytest.fixture
def uploads(monkeypatch):
    seen: list[tuple] = []

    def upload(config, page_id, path, **kw):
        with open(path, "rb") as handle:
            seen.append((page_id, kw["name"], handle.read(), kw["content_type"]))
        return {"id": "att"}

    monkeypatch.setattr(dw, "upload_attachment", upload)
    return seen


def _page(storage: str = "<p>Intro</p>") -> dict:
    return {"id": "9", "title": "Arch", "version": {"number": 4}, "body": {"storage": {"value": storage}}}


def test_resolve_mode() -> None:
    with pytest.raises(ConfluenceError, match="render_diagram"):
        resolve_mode(ConfluenceConfig(**BASE), None)
    assert resolve_mode(ConfluenceConfig(**BASE, kroki_url="https://k"), "auto") == "image"
    assert resolve_mode(ConfluenceConfig(**BASE, kroki_url="https://k", diagram_mode="macro"), None) == "macro"
    assert resolve_mode(ConfluenceConfig(**BASE), "image") == "image"
    with pytest.raises(ConfluenceError):
        resolve_mode(ConfluenceConfig(**BASE), "svg")


def test_diagram_filename() -> None:
    assert diagram_filename("Order flow / v2") == "order-flow-v2.png"
    with pytest.raises(ConfluenceError):
        diagram_filename("  //  ")


def test_macro_storage_escapes_cdata_end() -> None:
    xml = macro_storage("plantuml", "a ]]> b")
    (macro,) = list_macros(xml)
    assert macro["name"] == "plantuml"
    assert macro["body"] == "a ]]> b"


def test_image_storage_round_trips_source() -> None:
    xml = image_storage("flow.png", "mermaid", MERMAID, caption="Flow")
    names = [m["name"] for m in list_macros(xml)]
    assert names == ["expand", "code"]
    assert list_macros(xml)[1]["body"] == MERMAID
    assert 'ri:filename="flow.png"' in xml


def test_add_diagram_macro_mode(install) -> None:
    cfg = ConfluenceConfig(**BASE, mermaid_macro="mermaid-macro", diagram_mode="macro")
    client, _ = install(returns={"get_page_by_id": _page()})
    out = add_diagram(cfg, "9", "mermaid", MERMAID, "flow")
    assert out == {"page_id": "9", "mode": "macro", "kind": "mermaid", "name": "flow", "macro": "mermaid-macro", "updated_body": True}
    args, kwargs = client.last("update_page")
    assert args == ("9", "Arch")
    assert kwargs["representation"] == "storage"
    assert kwargs["body"].startswith('<p>Intro</p><ac:structured-macro ac:name="mermaid-macro"')


def test_add_diagram_macro_mode_needs_mermaid_macro_name(install) -> None:
    install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="CONFLUENCE_MERMAID_MACRO"):
        add_diagram(ConfluenceConfig(**BASE, diagram_mode="macro"), "9", "mermaid", MERMAID, "flow")


def test_add_diagram_macro_mode_rejects_graphviz(install) -> None:
    install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="mode=image"):
        add_diagram(ConfluenceConfig(**BASE), "9", "graphviz", "digraph{a->b}", "g", mode="macro")


def test_add_diagram_macro_mode_validates_with_kroki(install, monkeypatch) -> None:
    monkeypatch.setattr(dw.requests, "post", lambda url, **kw: FakeResp(400, text="Syntax error in graph"))
    client, _ = install(returns={"get_page_by_id": _page()})
    cfg = ConfluenceConfig(**BASE, kroki_url="https://kroki", diagram_mode="macro")
    with pytest.raises(ConfluenceError, match="Syntax error in graph"):
        add_diagram(cfg, "9", "plantuml", "@startuml\nbad", "p")
    assert not [c for c in client.calls if c[0] == "update_page"]


def test_add_diagram_image_mode(install, kroki, uploads) -> None:
    cfg = ConfluenceConfig(**BASE, kroki_url="https://kroki.example.com", verify_ssl="/ca.pem")
    client, _ = install(returns={"get_page_by_id": _page()})
    out = add_diagram(cfg, "9", "mermaid", MERMAID, "Order flow", position="prepend", caption="Orders")
    assert out["mode"] == "image"
    assert out["rendered_by"] == "kroki"
    assert out["attachment"] == "order-flow.png"
    assert kroki[0]["url"] == "https://kroki.example.com/mermaid/png"
    assert kroki[0]["data"] == MERMAID.encode()
    assert kroki[0]["verify"] == "/ca.pem"
    assert uploads == [("9", "order-flow.png", b"\x89PNG", "image/png")]
    _, kwargs = client.last("update_page")
    assert kwargs["body"].startswith("<p><ac:image")
    assert kwargs["body"].endswith("<p>Intro</p>")


def test_add_diagram_image_readd_refreshes_source_only(install, kroki, uploads) -> None:
    cfg = ConfluenceConfig(**BASE, kroki_url="https://kroki")
    existing = "<p>Intro</p>" + image_storage("flow.png", "mermaid", "graph TD\n  A-->C")
    client, _ = install(returns={"get_page_by_id": _page(existing)})
    out = add_diagram(cfg, "9", "mermaid", MERMAID, "flow")
    assert out["updated_body"] is True
    _, kwargs = client.last("update_page")
    body = kwargs["body"]
    assert body.count("ac:image") == 2  # one open + one close tag: still a single image
    assert list_macros(body)[-1]["body"] == MERMAID
    assert len(uploads) == 1


def test_add_diagram_image_readd_same_source_leaves_page(install, kroki, uploads) -> None:
    cfg = ConfluenceConfig(**BASE, kroki_url="https://kroki")
    client, _ = install(returns={"get_page_by_id": _page(image_storage("flow.png", "mermaid", MERMAID))})
    assert add_diagram(cfg, "9", "mermaid", MERMAID, "flow")["updated_body"] is False
    assert not [c for c in client.calls if c[0] == "update_page"]


def test_add_diagram_image_needs_kroki(install) -> None:
    install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="CONFLUENCE_KROKI_URL"):
        add_diagram(ConfluenceConfig(**BASE), "9", "mermaid", MERMAID, "flow", mode="image")


def test_add_diagram_rejects_empty_and_bad_position(install) -> None:
    install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="empty"):
        add_diagram(ConfluenceConfig(**BASE), "9", "mermaid", "  ", "flow")
    with pytest.raises(ConfluenceError, match="position"):
        add_diagram(ConfluenceConfig(**BASE), "9", "mermaid", MERMAID, "flow", position="middle")


def test_kroki_network_error(install, monkeypatch) -> None:
    def boom(url, **kw):
        raise dw.requests.ConnectionError("refused")

    monkeypatch.setattr(dw.requests, "post", boom)
    install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="Kroki request"):
        add_diagram(ConfluenceConfig(**BASE, kroki_url="https://k"), "9", "graphviz", "digraph{a->b}", "g")


def test_add_diagram_no_renderer_refuses_before_touching_page(install) -> None:
    client, _ = install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="render_diagram"):
        add_diagram(ConfluenceConfig(**BASE), "9", "mermaid", MERMAID, "flow")
    assert client.calls == []


def test_add_diagram_with_caller_png(install, uploads, tmp_path, monkeypatch) -> None:
    png = tmp_path / "flow.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nDATA")

    def no_kroki(*a, **kw):  # pragma: no cover - must not be called
        raise AssertionError("Kroki called")

    monkeypatch.setattr(dw.requests, "post", no_kroki)
    # A configured macro mode doesn't override an image the caller supplied.
    cfg = ConfluenceConfig(**BASE, diagram_mode="macro", kroki_url="https://k")
    client, _ = install(returns={"get_page_by_id": _page()})
    out = add_diagram(cfg, "9", "mermaid", MERMAID, "Order flow", image=png, caption="Orders")
    assert out["mode"] == "image"
    assert out["rendered_by"] == "caller"
    assert out["attachment"] == "order-flow.png"
    assert uploads == [("9", "order-flow.png", b"\x89PNG\r\n\x1a\nDATA", "image/png")]
    _, kwargs = client.last("update_page")
    assert kwargs["body"].startswith("<p>Intro</p><p><ac:image")
    assert "Diagram source (order-flow.png)" in kwargs["body"]


def test_add_diagram_image_must_be_a_readable_png(install, tmp_path) -> None:
    client, _ = install(returns={"get_page_by_id": _page()})
    with pytest.raises(ConfluenceError, match="Can't read"):
        add_diagram(ConfluenceConfig(**BASE), "9", "mermaid", MERMAID, "flow", image=tmp_path / "missing.png")
    svg = tmp_path / "flow.svg"
    svg.write_text("<svg/>")
    with pytest.raises(ConfluenceError, match="not a PNG"):
        add_diagram(ConfluenceConfig(**BASE), "9", "mermaid", MERMAID, "flow", image=svg)
    assert client.calls == []
