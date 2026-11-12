"""wiki_check — what in the team wiki needs a look."""

from __future__ import annotations

from datetime import date

from runspec_console import wiki_check as wc
from runspec_console import wiki_pages as wp

CRON = """---
type: Runnable
stale_after: 2026-01-01
---

# cron

## Works with

- remove lines with `remove --line 3`, after `list --all`
- run `--debug` when a schedule doesn't fire
- always check the crontab of the account that runs the job first

## Watch out for

- `clear` removes every line, including ones the console didn't add.
"""

DF = """---
type: Runnable
status: deprecated
---

# df

## Watch out for

- always check the crontab of the account that runs the job first
"""

RECORDS = [
    {
        "name": "cron",
        "args": [{"name": "run_as"}],
        "commands": {
            "list": {"name": "list", "args": [{"name": "all"}]},
            "remove": {
                "name": "remove",
                "args": [{"name": "match"}, {"name": "index"}],
            },
        },
    },
    {"name": "df", "args": [{"name": "path"}]},
    {"name": "tail-log", "args": []},
    {"name": "http-check", "args": [{"name": "url"}]},
]


def _pages():
    return [
        wp.parse_page("runnables/cron.md", CRON),
        wp.parse_page("runnables/df.md", DF),
    ]


def test_known_flags_cover_verbs():
    flags = wc.known_flags(RECORDS)
    assert flags["cron"] == {"run-as", "all", "match", "index"}
    assert wc.page_flags(_pages()[0]) == ["line", "all", "debug"]


def test_check_finds_clash_stale_duplicate_and_gap():
    got = wc.check(_pages(), RECORDS, today=date(2026, 11, 12))
    assert got["summary"] == {"clash": 1, "stale": 2, "duplicate": 1, "gap": 2}
    kinds = {f["kind"]: f for f in got["findings"]}
    assert kinds["clash"]["flags"] == ["line"]  # --all is a verb arg, --debug is common
    assert "--line" in kinds["clash"]["message"]
    stale = [f["message"] for f in got["findings"] if f["kind"] == "stale"]
    assert any("2026-01-01" in m for m in stale) and any(
        "deprecated" in m for m in stale
    )
    assert kinds["duplicate"]["paths"] == ["runnables/cron.md", "runnables/df.md"]
    assert kinds["gap"]["names"] == ["http-check", "tail-log"]


def test_check_is_quiet_for_a_tidy_wiki():
    page = wp.parse_page(
        "runnables/df.md",
        "---\ntype: Runnable\n---\n\n# df\n\n## Works with\n\n- `--path /var`\n",
    )
    got = wc.check([page], [{"name": "df", "args": [{"name": "path"}]}])
    assert got == {
        "summary": {"clash": 0, "stale": 0, "duplicate": 0, "gap": 0},
        "findings": [],
    }


def test_pages_for_tools_not_installed_are_not_checked_for_clashes():
    got = wc.check(
        _pages(), [{"name": "http-check", "args": []}], today=date(2026, 1, 1)
    )
    assert got["summary"]["clash"] == 0
