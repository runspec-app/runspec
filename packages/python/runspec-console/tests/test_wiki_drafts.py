"""wiki_drafts — page edits kept on this console until they're proposed."""

from __future__ import annotations

import pytest

from runspec_console import wiki_drafts as wd
from runspec_console import wiki_pages as wp

PAGE = """---
type: Runnable
title: cron
---

# cron

## Works with

- run `list` first

## Watch out for

"""


def test_page_paths_and_new_pages():
    assert wd.page_path_for("runnable:http-check") == "runnables/http-check.md"
    assert wd.page_path_for("term:Change Window") == "terms/change-window.md"
    assert wd.page_path_for("group:prod") == ""
    page = wd.new_page("runnable:df", "Disk usage.")
    parsed = wp.parse_page("runnables/df.md", page)
    assert parsed["type"] == "Runnable" and parsed["status"] == "draft"
    assert [s["heading"] for s in parsed["sections"]] == list(wd.RUNNABLE_SECTIONS)


def test_new_runnable_page_starts_with_its_description():
    page = wd.new_page("runnable:backup", "Back up a directory.")
    assert "## What it's for\n\nBack up a directory.\n" in page
    assert wd.text_hash(page) == wd.text_hash(page + "\n\n")


def test_target_for_picks_page_and_section():
    pair = {"kind": "pairing", "about": ["term:BAU", "runnable:cron"]}
    assert wd.target_for(pair) == ("runnable:cron", "Works with")
    note = {"kind": "note", "about": ["runnable:cron"]}
    assert wd.target_for(note) == ("runnable:cron", "Watch out for")
    assert wd.target_for({"kind": "glossary", "about": ["term:BAU"]}) == (
        "term:BAU",
        "Notes",
    )
    assert wd.target_for({"kind": "note", "about": ["file:/etc/x"]}) is None


def test_add_bullet_appends_to_the_section_and_skips_repeats():
    out = wd.add_bullet(PAGE, "Works with", "then `remove --match`")
    assert "- run `list` first\n- then `remove --match`\n" in out
    out = wd.add_bullet(out, "Watch out for", "clear removes every line")
    assert "## Watch out for\n\n- clear removes every line\n" in out
    assert wd.add_bullet(out, "Works with", "Run list first") == out  # already said
    out = wd.add_bullet(out, "Notes", "owned by ops")
    assert out.rstrip().endswith("## Notes\n\n- owned by ops")


def test_draft_store_round_trip(tmp_path):
    wp.write_cache(tmp_path, {"runnables/cron.md": PAGE.encode()})
    text = wd.current_text(tmp_path, "runnables/cron.md")
    assert text == PAGE
    assert wd.save_draft(
        tmp_path, "runnables/cron.md", wd.add_bullet(text, "Works with", "x")
    )
    assert wd.save_draft(tmp_path, "terms/bau.md", wd.new_page("term:BAU"))
    drafts = {d["path"]: d for d in wd.list_drafts(tmp_path)}
    assert (
        drafts["runnables/cron.md"]["added"] == 1
        and not drafts["runnables/cron.md"]["new"]
    )
    assert "+- x" in drafts["runnables/cron.md"]["diff"]
    assert drafts["terms/bau.md"]["new"]
    # Saving the team's text back drops the draft.
    assert wd.save_draft(tmp_path, "runnables/cron.md", PAGE) is False
    assert wd.draft_paths(tmp_path) == ["terms/bau.md"]
    # A pull that brings in the merged page drops the draft too.
    wp.write_cache(
        tmp_path,
        {
            "runnables/cron.md": PAGE.encode(),
            "terms/bau.md": wd.new_page("term:BAU").encode(),
        },
    )
    assert wd.drop_landed(tmp_path) == ["terms/bau.md"]
    with pytest.raises(ValueError):
        wd.save_draft(tmp_path, "../evil.md", "x")


def test_merge_request_urls():
    gl = wd.merge_request_url("git@gitlab.corp:ops/wiki.git", "contrib/jo", "main")
    assert gl == (
        "https://gitlab.corp/ops/wiki/-/merge_requests/new"
        "?merge_request%5Bsource_branch%5D=contrib%2Fjo&merge_request%5Btarget_branch%5D=main"
    )
    gh = wd.merge_request_url("https://github.com/acme/wiki.git", "contrib/jo", "main")
    assert gh == "https://github.com/acme/wiki/compare/main...contrib/jo?expand=1"
    assert wd.merge_request_url("/srv/wiki.git", "a", "b") == ""
