"""Terminal text rendering — pure, no runspec."""

from runspec_grafana.render import human_text


def test_rows_become_a_table() -> None:
    text = human_text({"count": 1, "truncated": True, "dashboards": [{"uid": "a", "title": "API", "tags": ["prod", "eu"]}]})
    lines = text.splitlines()
    assert lines[0].startswith("Count") and "UID" in lines[2] and "TAGS" in lines[2]
    assert "prod, eu" in lines[3] and lines[-1] == "(more — raise --limit)"


def test_string_lists_one_per_line() -> None:
    text = human_text({"datasource": {"uid": "p", "name": "Prom"}, "count": 2, "truncated": False, "values": ["api", "node"]})
    assert text.splitlines()[-2:] == ["api", "node"]


def test_empty_list_says_so() -> None:
    assert human_text({"count": 0, "truncated": False, "silences": []}).endswith("No silences.")


def test_log_lines() -> None:
    payload = {"line_count": 1, "truncated": False, "streams": [{"labels": {"app": "api"}, "lines": 1}], "entries": [{"time": "T", "stream": 0, "line": "boom"}]}
    assert human_text(payload).splitlines()[0] == 'T {app="api"} boom'


def test_nested_row_lists_follow_the_table() -> None:
    text = human_text({"panel_count": 1, "panels": [{"id": 1, "title": "Rate", "queries": [{"ref_id": "A", "query": "up"}]}]})
    table, _, details = text.partition("Rate — queries:")
    assert "QUERIES" not in table and "Rate" in table
    assert "REF_ID" in details and "up" in details


def test_non_dict_falls_back_to_json() -> None:
    assert human_text([1, 2]) is None
