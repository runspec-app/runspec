"""wiki_pages — reading the team wiki's OKF Markdown pages."""

from __future__ import annotations

from runspec_console import wiki_pages as wp

PAGE = """---
type: Runnable
title: http-check
description: Probe an HTTP(S) endpoint.
status: stable
verified:
  - by: human:ana
    at: 2026-10-28
---

# http-check

The first thing to run when a service looks down from outside.

## Works with

- **then run [tail-log](/runnables/tail-log.md)** after a 502, before
  restarting anything.
- after `restart-service`, to confirm the app came back.

## Watch out for

- Internal URLs go through the proxy unless you pass `--proxy direct`.
"""


def test_entity_for_maps_folders_and_skips_indexes():
    assert wp.entity_for("runnables/http-check.md") == "runnable:http-check"
    assert wp.entity_for("terms/bau.md") == "term:bau"
    assert wp.entity_for("runnables/index.md") == ""
    assert wp.entity_for("runspec/apps/console.md") == ""  # generated docs
    assert wp.entity_for("runnables/deep/x.md") == ""
    assert wp.entity_for("README.md") == ""


def test_parse_page_reads_frontmatter_sections_and_bullets():
    page = wp.parse_page("runnables/http-check.md", PAGE)
    assert page["entity"] == "runnable:http-check"
    assert page["type"] == "Runnable" and page["status"] == "stable"
    assert page["title"] == "http-check" and page["verified_by"] == "human:ana"
    heads = [s["heading"] for s in page["sections"]]
    assert heads == ["", "Works with", "Watch out for"]
    works = wp.section_bullets(page, "works with")
    assert len(works) == 2 and works[0].endswith("before restarting anything.")
    assert wp.notes(page) == [
        "then run tail-log after a 502, before restarting anything.",
        "after restart-service, to confirm the app came back.",
        "Internal URLs go through the proxy unless you pass --proxy direct.",
    ]
    assert "proxy" in wp.search_text(page)
    assert "error" not in page


def test_parse_page_flags_a_page_without_frontmatter():
    page = wp.parse_page("runnables/x.md", "# x\n\n## Works with\n\n- y\n")
    assert page["error"] == "no frontmatter" and wp.notes(page) == ["y"]


def test_cache_round_trip_replaces_and_counts(tmp_path):
    files = {
        "runnables/http-check.md": PAGE.encode(),
        "runnables/index.md": b"# Runnables",
        "terms/bau.md": b"---\ntype: Term\n---\n\n# BAU\n",
        "facts/jo.toml": b"[[fact]]\n",
    }
    got = wp.write_cache(tmp_path, files)
    assert got == {"pages": 2, "added": 2, "removed": 0}
    pages = wp.read_cache(tmp_path)
    assert [p["entity"] for p in pages] == ["runnable:http-check", "term:bau"]
    got = wp.write_cache(tmp_path, {"terms/bau.md": files["terms/bau.md"]})
    assert got == {"pages": 1, "added": 0, "removed": 1}
    assert wp.write_cache(tmp_path, {}) == {"pages": 0, "added": 0, "removed": 1}
    assert wp.read_cache(tmp_path) == []
