"""draw.io / Gliffy decoding and the page-level diagram ops."""

import base64
import json
import urllib.parse
import zlib

import pytest

from runspec_confluence_core import (
    ConfluenceConfig,
    ConfluenceError,
    decode_drawio,
    decode_gliffy,
    download_diagram,
    get_diagram,
    page_macros,
)

CFG = ConfluenceConfig(url="https://acme.atlassian.net/wiki", cloud=True, personal_token="p")

MODEL = (
    '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
    '<mxCell id="g" value="VPC" style="swimlane;" vertex="1" parent="1"/>'
    '<mxCell id="a" value="&lt;b&gt;Web&lt;/b&gt; tier" style="rounded=1;" vertex="1" parent="g"/>'
    '<UserObject id="b" label="DB"><mxCell style="shape=cylinder3;" vertex="1" parent="1"/></UserObject>'
    '<mxCell id="e" edge="1" source="a" target="b" parent="1"/>'
    '<mxCell id="el" value="SQL" style="edgeLabel;" vertex="1" parent="e"/>'
    '<mxCell id="d" edge="1" source="b" parent="1"/>'
    "</root></mxGraphModel>"
)


def _compressed(model: str) -> str:
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = deflate.compress(urllib.parse.quote(model).encode()) + deflate.flush()
    return f'<mxfile><diagram name="Overview">{base64.b64encode(raw).decode()}</diagram></mxfile>'


EXPECTED_NODES = [
    {"id": "g", "label": "VPC", "shape": "swimlane"},
    {"id": "a", "label": "Web tier", "in": "VPC"},
    {"id": "b", "label": "DB", "shape": "cylinder3"},
]


@pytest.mark.parametrize("source", [_compressed(MODEL), f'<mxfile><diagram name="Overview">{MODEL}</diagram></mxfile>'])
def test_decode_drawio_compressed_and_plain(source: str) -> None:
    out = decode_drawio(source.encode())
    (page,) = out["pages"]
    assert page["name"] == "Overview"
    assert page["nodes"] == EXPECTED_NODES
    assert page["edges"] == [{"source": "Web tier", "target": "DB", "label": "SQL"}, {"source": "DB", "target": "?"}]
    assert "  Web tier -> DB [SQL]" in out["summary"]
    assert "  - Web tier (in VPC)" in out["summary"]


def test_decode_drawio_bare_model() -> None:
    assert decode_drawio(MODEL)["pages"][0]["name"] == "Page-1"


@pytest.mark.parametrize("bad", ["not xml", "<html/>", "<mxfile><diagram>!!notbase64</diagram></mxfile>"])
def test_decode_drawio_rejects_garbage(bad: str) -> None:
    with pytest.raises(ConfluenceError):
        decode_drawio(bad)


GLIFFY = {
    "stage": {
        "objects": [
            {"id": 1, "uid": "com.gliffy.shape.basic.basic_v1.default.rectangle", "graphic": {"type": "Shape"}, "children": [{"id": 2, "graphic": {"type": "Text", "Text": {"html": "<p>API</p>"}}}]},
            {"id": 3, "uid": "com.gliffy.shape.basic.basic_v1.default.ellipse", "graphic": {"type": "Shape"}, "children": [{"id": 4, "graphic": {"type": "Text", "Text": {"html": "<p>Queue</p>"}}}]},
            {
                "id": 5,
                "graphic": {"type": "Line"},
                "constraints": {"startConstraint": {"StartPositionConstraint": {"nodeId": 1}}, "endConstraint": {"EndPositionConstraint": {"nodeId": 3}}},
            },
        ]
    }
}


def test_decode_gliffy() -> None:
    out = decode_gliffy(json.dumps(GLIFFY))
    (page,) = out["pages"]
    assert [n["label"] for n in page["nodes"]] == ["API", "Queue"]
    assert page["nodes"][0]["shape"] == "rectangle"
    assert page["edges"] == [{"source": "API", "target": "Queue"}]


def test_decode_gliffy_rejects_garbage() -> None:
    with pytest.raises(ConfluenceError):
        decode_gliffy("{}")


def _page(storage: str) -> dict:
    return {"id": "9", "title": "Arch", "body": {"storage": {"value": storage}}}


DRAWIO_MACRO = '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">arch</ac:parameter><ac:parameter ac:name="revision">2</ac:parameter></ac:structured-macro>'


def _wire(client, attachments: dict[str, dict], files: dict[str, bytes]) -> None:
    """Serve attachments by filename and their bytes by download link."""

    def get_attachments_from_content(page_id, filename=None, **_):
        att = attachments.get(filename or "")
        return {"results": [att] if att else []}

    def get(path, not_json_response=False, **_):
        assert not_json_response
        return files[path]

    client.get_attachments_from_content = get_attachments_from_content
    client.get = get


def _att(title: str) -> dict:
    return {"title": title, "version": {"number": 2}, "_links": {"download": f"/download/attachments/9/{title}"}}


def test_page_macros(install) -> None:
    client, _ = install(returns={"get_page_by_id": _page("<p>x</p>" + DRAWIO_MACRO + DRAWIO_MACRO)})
    out = page_macros(CFG, "9")
    assert out["counts"] == {"drawio": 2}
    assert out["title"] == "Arch"
    _, kwargs = client.last("get_page_by_id")
    assert kwargs["expand"] == "body.storage"


def test_get_diagram_falls_back_to_drawio_extension(install) -> None:
    client, _ = install(returns={"get_page_by_id": _page(DRAWIO_MACRO)})
    _wire(client, {"arch.drawio": _att("arch.drawio")}, {"download/attachments/9/arch.drawio": _compressed(MODEL).encode()})
    out = get_diagram(CFG, "9", include_source=True)
    assert out["diagram"]["name"] == "arch"
    assert out["attachment"] == {"page_id": "9", "title": "arch.drawio", "version": 2}
    assert out["pages"][0]["nodes"] == EXPECTED_NODES
    assert out["source"].startswith("<mxfile>")


def test_get_diagram_needs_name_when_several(install) -> None:
    other = DRAWIO_MACRO.replace(">arch<", ">net<")
    install(returns={"get_page_by_id": _page(DRAWIO_MACRO + other)})
    with pytest.raises(ConfluenceError, match="pass a name: arch, net"):
        get_diagram(CFG, "9")


def test_get_diagram_no_diagrams(install) -> None:
    install(returns={"get_page_by_id": _page("<p>none</p>")})
    with pytest.raises(ConfluenceError, match="No draw.io or Gliffy"):
        get_diagram(CFG, "9")


def test_get_diagram_missing_attachment(install) -> None:
    client, _ = install(returns={"get_page_by_id": _page(DRAWIO_MACRO)})
    _wire(client, {}, {})
    with pytest.raises(ConfluenceError, match="no source attachment"):
        get_diagram(CFG, "9", name="ARCH")


def test_download_diagram_writes_source_and_preview(install, tmp_path) -> None:
    client, _ = install(returns={"get_page_by_id": _page(DRAWIO_MACRO)})
    _wire(
        client,
        {"arch": _att("arch"), "arch.png": _att("arch.png")},
        {"download/attachments/9/arch": b"<mxfile/>", "download/attachments/9/arch.png": b"\x89PNG"},
    )
    out = download_diagram(CFG, "9", str(tmp_path / "out"))
    assert (tmp_path / "out" / "arch.drawio").read_bytes() == b"<mxfile/>"
    assert (tmp_path / "out" / "arch.png").read_bytes() == b"\x89PNG"
    assert out["preview"].endswith("arch.png")


def test_download_diagram_without_preview(install, tmp_path) -> None:
    client, _ = install(returns={"get_page_by_id": _page(DRAWIO_MACRO)})
    _wire(client, {"arch": _att("arch")}, {"download/attachments/9/arch": b"<mxfile/>"})
    assert download_diagram(CFG, "9", str(tmp_path))["preview"] is None
