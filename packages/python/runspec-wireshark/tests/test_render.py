"""Terminal text rendering."""

from runspec_wireshark.render import human_text


def test_generic_header_tables_and_footer() -> None:
    payload = {
        "file": "a.pcapng",
        "count": 1,
        "total": 3,
        "offset": 0,
        "truncated": True,
        "next_offset": 1,
        "packets": [{"no": 1, "src": "10.0.0.1:5000", "dst": "10.0.0.2:80", "info": "x" * 100}],
    }
    text = human_text("packets", payload)
    assert text is not None
    assert text.splitlines()[0] == "File  a.pcapng"
    assert "NO  SRC" in text and "…" in text
    assert text.endswith("Showing 1 of 3 — next page: --offset 1")


def test_empty_list_says_so() -> None:
    assert "No connections." in (human_text("tls", {"file": "a", "connections": []}) or "")


def test_follow_text() -> None:
    payload = {"protocol": "tcp", "stream": 2, "a": "c:1", "b": "s:80", "chunks": [{"from": "c:1", "bytes": 5, "text": "GET /\r\n"}], "truncated": True, "bytes_total": 99}
    text = human_text("follow", payload) or ""
    assert text.startswith("tcp stream 2: c:1 ⇄ s:80") and "── c:1 (5 bytes)\nGET /" in text and "raise --max-bytes" in text


def test_packet_and_stats_text() -> None:
    text = human_text("packet", {"no": 1, "layers": [{"title": "TCP", "lines": ["Port: 80"], "more": 2}], "bytes": ["0000  00"]}) or ""
    assert text == "TCP\n    Port: 80\n    … 2 more lines\n\n0000  00"
    assert human_text("stats", {"lines": ["a", "b"], "truncated": False}) == "a\nb"


def test_non_dict_is_json() -> None:
    assert human_text("packet", ["x"]) is None
