"""Storage-format macro parsing and macro-aware Markdown rendering."""

from runspec_confluence_core import html_to_markdown, list_macros, macro_kind, storage_to_html

DRAWIO = '<ac:structured-macro ac:name="drawio" ac:schema-version="1"><ac:parameter ac:name="diagramName">arch</ac:parameter><ac:parameter ac:name="revision">3</ac:parameter></ac:structured-macro>'
CODE = '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter><ac:plain-text-body><![CDATA[if a < b:\n    print(1)]]></ac:plain-text-body></ac:structured-macro>'
EXPAND_WITH_NOTE = (
    '<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">More</ac:parameter>'
    "<ac:rich-text-body><p>hidden</p>"
    '<ac:structured-macro ac:name="note"><ac:rich-text-body><p>careful &amp; slow</p></ac:rich-text-body></ac:structured-macro>'
    "</ac:rich-text-body></ac:structured-macro>"
)


def test_macro_kind() -> None:
    assert macro_kind("drawio") == "diagram"
    assert macro_kind("inc-drawio") == "diagram"
    assert macro_kind("gliffy") == "diagram"
    assert macro_kind("plantumlcloud") == "text-diagram"
    assert macro_kind("mermaid-cloud") == "text-diagram"
    assert macro_kind("code") == "code"
    assert macro_kind("warning") == "panel"
    assert macro_kind("expand") == "expand"
    assert macro_kind("toc") == "macro"


def test_list_macros_drawio_pointer() -> None:
    (macro,) = list_macros("<p>x</p>" + DRAWIO)
    assert macro["kind"] == "diagram"
    assert macro["diagram"] == {
        "kind": "drawio",
        "name": "arch",
        "attachments": ["arch", "arch.drawio", "arch.xml"],
        "preview": "arch.png",
        "revision": "3",
    }


def test_list_macros_inc_drawio_points_at_other_page() -> None:
    storage = '<ac:structured-macro ac:name="inc-drawio"><ac:parameter ac:name="diagramName">net</ac:parameter><ac:parameter ac:name="pageId">777</ac:parameter></ac:structured-macro>'
    (macro,) = list_macros(storage)
    assert macro["diagram"]["page_id"] == "777"


def test_list_macros_gliffy_pointer() -> None:
    storage = '<ac:structured-macro ac:name="gliffy"><ac:parameter ac:name="name">flow</ac:parameter><ac:parameter ac:name="pagePin">2</ac:parameter></ac:structured-macro>'
    (macro,) = list_macros(storage)
    assert macro["diagram"]["kind"] == "gliffy"
    assert macro["diagram"]["attachments"] == ["flow", "flow.gliffy"]
    assert macro["diagram"]["revision"] == "2"


def test_list_macros_bodies_and_nesting() -> None:
    plantuml = '<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[A -> B]]></ac:plain-text-body></ac:structured-macro>'
    macros = list_macros(CODE + EXPAND_WITH_NOTE + plantuml)
    assert [m["name"] for m in macros] == ["code", "expand", "note", "plantuml"]
    assert macros[0]["body"] == "if a < b:\n    print(1)"
    assert macros[1]["title"] == "More"
    assert macros[2]["depth"] == 1
    assert macros[3]["language"] == "plantuml"
    assert macros[3]["body"] == "A -> B"


def test_list_macros_empty() -> None:
    assert list_macros(None) == []
    assert list_macros("<p>plain</p>") == []


def test_storage_to_html_leaves_plain_html_alone() -> None:
    assert storage_to_html("<p>plain</p>") == "<p>plain</p>"


def test_markdown_renders_macros_instead_of_gluing_params() -> None:
    md = html_to_markdown("<p>Intro</p>" + DRAWIO + CODE + EXPAND_WITH_NOTE)
    assert "arch3" not in md
    assert '[draw.io diagram "arch" rev 3]' in md
    assert "```python\nif a < b:\n    print(1)\n```" in md
    assert "**More**" in md
    assert "> **Note:**" in md
    assert "careful & slow" in md


def test_markdown_text_diagrams_fenced_with_language() -> None:
    mermaid = '<ac:structured-macro ac:name="mermaid-cloud"><ac:plain-text-body><![CDATA[graph TD; A-->B]]></ac:plain-text-body></ac:structured-macro>'
    assert "```mermaid\ngraph TD; A-->B\n```" in html_to_markdown(mermaid)


def test_markdown_links_images_and_unknown_macros() -> None:
    storage = '<p>See <ac:link><ri:page ri:content-title="Runbook"/></ac:link> and <ac:image><ri:attachment ri:filename="x.png"/></ac:image></p><ac:structured-macro ac:name="toc"/>'
    md = html_to_markdown(storage)
    assert "See Runbook and [image: x.png]" in md
    assert "[toc macro]" in md


def test_markdown_panel_with_title() -> None:
    storage = '<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Heads up</ac:parameter><ac:rich-text-body><p>body</p></ac:rich-text-body></ac:structured-macro>'
    assert "> **Info: Heads up**" in html_to_markdown(storage)
