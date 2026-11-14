"""Webhook bodies, field paths, templates and rule matching (pure functions)."""

from __future__ import annotations

import re

import pytest

from runspec_scheduler import hooktemplate as tpl

# ── bodies ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content_type", "body", "kind"),
    [
        ("application/json", b"{}", "json"),
        ("application/vnd.api+json; charset=utf-8", b"{}", "json"),
        ("text/xml", b"<a/>", "xml"),
        ("application/atom+xml", b"<a/>", "xml"),
        ("application/x-www-form-urlencoded", b"a=1", "form"),
        ("", b'  [{"a":1}]', "json"),
        ("application/octet-stream", b"<x/>", "xml"),
        ("text/plain", b"hello", "text"),
    ],
)
def test_body_kind(content_type: str, body: bytes, kind: str) -> None:
    assert tpl.body_kind(content_type, body) == kind


def test_parse_json_and_bad_json() -> None:
    assert tpl.parse_body(b'{"a": [1, 2]}', "application/json") == {"a": [1, 2]}
    with pytest.raises(tpl.TemplateError, match="isn't valid JSON"):
        tpl.parse_body(b"{nope", "application/json")


def test_parse_form_repeats_become_lists() -> None:
    assert tpl.parse_body(b"a=1&b=&a=2&a=3", "application/x-www-form-urlencoded") == {"a": ["1", "2", "3"], "b": ""}


def test_parse_text_is_none() -> None:
    assert tpl.parse_body(b"just words", "text/plain") is None


def test_xml_to_data() -> None:
    body = b'<alert xmlns="urn:x" id="7"><host>web-01</host><tag>a</tag><tag>b</tag><note sev="hi">disk</note></alert>'
    data = tpl.parse_body(body, "application/xml")
    assert data == {"alert": {"@id": "7", "host": "web-01", "tag": ["a", "b"], "note": {"@sev": "hi", "#text": "disk"}}}
    assert tpl.get_path(data, "alert.tag.1") == "b"


@pytest.mark.parametrize("bad", [b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', b'<!ENTITY a "b"><x/>'])
def test_xml_refuses_doctype_and_entities(bad: bytes) -> None:
    with pytest.raises(tpl.TemplateError, match="DOCTYPE or ENTITY"):
        tpl.parse_body(bad, "text/xml")


def test_xml_bad() -> None:
    with pytest.raises(tpl.TemplateError, match="isn't valid XML"):
        tpl.parse_body(b"<a><b></a>", "text/xml")


# ── paths ─────────────────────────────────────────────────────────────────────

DATA = {"alerts": [{"labels": {"alertname": "DiskFull"}}, {"labels": {"alertname": "Load"}}], "status": "firing"}


def test_get_path() -> None:
    assert tpl.get_path(DATA, "alerts.0.labels.alertname") == "DiskFull"
    assert tpl.get_path(DATA, "alerts.-1.labels.alertname") == "Load"
    assert tpl.get_path(DATA, "alerts.5", "gone") == "gone"
    assert tpl.get_path(DATA, "status.x", None) is None
    with pytest.raises(KeyError):
        tpl.get_path(DATA, "nope")


@pytest.mark.parametrize(("path", "ok"), [("a.b", True), ("$.a", True), ("$", True), ("", False), ("a..b", False), ("a.", False)])
def test_check_path(path: str, ok: bool) -> None:
    assert (tpl.check_path(path) == "") is ok


def test_extract_fields_item_and_root() -> None:
    item = DATA["alerts"][1]
    out = tpl.extract_fields({"name": "labels.alertname", "status": "$.status", "missing": "labels.none", "whole": "$"}, item, DATA)
    assert out == {"name": "Load", "status": "firing", "whole": DATA}


# ── templates ─────────────────────────────────────────────────────────────────

FIELDS = {"instance": "web-01:9100", "name": " DiskFull ", "hosts": "a, b ,c", "n": 3, "items": ["x", "y"], "labels": {"team": "ops"}}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{ instance }}", "web-01:9100"),
        ("{{ n }}", 3),  # a lone template keeps its type
        ("n={{ n }}", "n=3"),
        ("{{ instance | regex('^([^:]+)', 1) }}", "web-01"),
        ("{{ name | trim | lower }}", "diskfull"),
        ("{{ name | trim | upper }}", "DISKFULL"),
        ("{{ instance | replace(':9100') }}", "web-01"),
        ("{{ missing | default('none') }}", "none"),
        ("{{ hosts | split(',') }}", ["a", "b", "c"]),
        ("{{ hosts | split | first }}", "a"),
        ("{{ items | last }}", "y"),
        ("{{ items | join(' ') }}", "x y"),
        ("{{ labels | json }}", '{"team":"ops"}'),
        ("{{ labels.team }}", "ops"),
        ("{{ instance }} and {{ n }}", "web-01:9100 and 3"),
        ("plain", "plain"),
        (5, 5),
    ],
)
def test_render(template: object, expected: object) -> None:
    assert tpl.render(template, FIELDS) == expected


def test_render_nested_lists_and_dicts() -> None:
    assert tpl.render({"a": ["{{ n }}", "x{{ n }}"]}, FIELDS) == {"a": [3, "x3"]}


def test_render_regex_no_match_is_none() -> None:
    assert tpl.render("{{ instance | regex('^zzz') }}", FIELDS) is None


@pytest.mark.parametrize(
    ("template", "problem"),
    [
        ("{{ x | nope }}", "unknown filter"),
        ("{{ x", "unbalanced"),
        ("{{ 'x' }}", "must start with a field name"),
        ("{{ x | regex(y) }}", "quoted text or numbers"),
        ("{{ x | lower( }}", "missing ')'"),
        ("{{ x lower }}", "expected '| filter'"),
        ("{{ x | __import__ }}", "unknown filter"),
    ],
)
def test_check_template(template: str, problem: str) -> None:
    assert problem in tpl.check_template(template)


def test_check_template_ok() -> None:
    assert tpl.check_template({"a": ["{{ x | regex('(a)', 1) | default('z') }}"]}) == ""


def test_bad_regex_in_filter() -> None:
    with pytest.raises(tpl.TemplateError, match="regex"):
        tpl.render("{{ instance | regex('(') }}", FIELDS)


# ── matching ──────────────────────────────────────────────────────────────────


def test_match_rule_named_groups_and_paths() -> None:
    patterns = {"alertname": re.compile(r"^(?P<kind>Disk)"), "labels.team": re.compile("^ops$")}
    assert tpl.match_rule(patterns, {"alertname": "DiskFull", "labels": {"team": "ops"}}) == {"kind": "Disk"}
    assert tpl.match_rule(patterns, {"alertname": "Load", "labels": {"team": "ops"}}) is None
    assert tpl.match_rule(patterns, {"alertname": "DiskFull"}) is None


def test_match_rule_empty_matches_everything() -> None:
    assert tpl.match_rule({}, {}) == {}
