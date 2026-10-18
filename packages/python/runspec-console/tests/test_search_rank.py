"""
test_search_rank.py — BM25(F) ranking for search-mode runnable discovery.
"""

from __future__ import annotations

from runspec_console.search_rank import bm25f_scores, runnable_doc, tokenize


def test_tokenize_lowercases_and_splits():
    assert tokenize("Restart-Service NOW") == ["restart", "service", "now"]
    assert tokenize("") == []


DOCS = [
    {
        "name": "restart-service",
        "group": "systemd",
        "description": "Restart a systemd service by name.",
        "args": ["name"],
    },
    {
        "name": "list-files",
        "group": "filesystem",
        "description": "List files in a directory.",
        "args": ["path"],
    },
    {
        "name": "disk-usage",
        "group": "filesystem",
        "description": "Report disk usage and free space across mounts.",
        "args": ["path", "human"],
    },
]


def test_empty_query_scores_all_zero():
    assert bm25f_scores("", DOCS) == [0.0, 0.0, 0.0]


def test_name_match_ranks_first():
    scores = bm25f_scores("restart service", DOCS)
    # the restart-service runnable (name hit) must outrank the others
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]


def test_group_match_beats_unrelated():
    scores = bm25f_scores("filesystem", DOCS)
    # both filesystem-group runnables score; the systemd one does not
    assert scores[0] == 0.0
    assert scores[1] > 0.0 and scores[2] > 0.0


def test_arg_name_contributes():
    scores = bm25f_scores("human", DOCS)
    # only disk-usage has a 'human' arg
    assert scores[2] > 0.0
    assert scores[0] == 0.0 and scores[1] == 0.0


def test_rarer_term_scores_higher_than_common_term():
    # Same field (description), same tf=1 in the target doc, but one term is in
    # every doc and the other in just one → the rarer term's higher idf wins.
    docs = [
        {"name": "x", "group": "", "description": "common rare", "args": []},
        {"name": "y", "group": "", "description": "common", "args": []},
        {"name": "z", "group": "", "description": "common", "args": []},
    ]
    rare = bm25f_scores("rare", docs)  # only doc0
    common = bm25f_scores("common", docs)  # all docs
    assert rare[0] > common[0] > 0.0


def test_length_normalisation_does_not_let_long_desc_dominate():
    docs = [
        {"name": "a", "group": "g", "description": "disk", "args": []},
        {
            "name": "b",
            "group": "g",
            "description": "disk " + "padding " * 50,
            "args": [],
        },
    ]
    scores = bm25f_scores("disk", docs)
    # the short, focused doc should not be beaten merely by the padded one
    assert scores[0] >= scores[1]


def test_deterministic():
    assert bm25f_scores("restart", DOCS) == bm25f_scores("restart", DOCS)


# ── the whole runspec.toml is searchable ──────────────────────────────────────


def _cron() -> dict:
    return {
        "name": "cron",
        "group": "runspec-linux",
        "description": "Manage a user's crontab.",
        "args": [
            {
                "name": "become-method",
                "type": "choice",
                "description": "How to escalate.",
                "options": ["sudo", "su"],
            }
        ],
        "rawSpec": {
            "examples": [{"cmd": "cron add", "description": "Run a nightly backup"}]
        },
        "commands": {
            "add": {
                "description": "Add a scheduled job",
                "args": [
                    {
                        "name": "become-method",
                        "description": "How to escalate.",
                        "inherited": True,
                    },
                    {"name": "schedule", "description": "Five cron fields"},
                ],
                "commands": {
                    "deep": {"description": "A nested verb about quokkas", "args": []}
                },
            }
        },
    }


def test_runnable_doc_collects_every_field():
    doc = runnable_doc(_cron(), groups=["runspec-linux", "other"], wiki=["Use UTC."])
    assert doc["name"] == "cron"
    assert doc["group"] == "runspec-linux other"
    assert doc["args"] == ["become-method"]
    assert doc["arg_text"] == ["How to escalate.", "sudo", "su"]
    assert doc["commands"] == ["add", "deep"]  # nested verbs too
    # A command's own args are indexed; inherited globals are not repeated.
    assert doc["command_text"] == [
        "Add a scheduled job",
        "schedule",
        "Five cron fields",
        "A nested verb about quokkas",
    ]
    assert doc["examples"] == ["Run a nightly backup"]
    assert doc["wiki"] == ["Use UTC."]


def _corpus(**extra_cron) -> list[dict]:
    cron = _cron()
    cron.update(extra_cron)
    others = [
        {"name": "backup-files", "description": "Archive a directory", "args": []},
        {"name": "ping-host", "description": "Ping a host", "args": []},
    ]
    return [runnable_doc(cron, wiki=["Jobs run in UTC on the batch servers"])] + [
        runnable_doc(o) for o in others
    ]


def test_subcommand_description_finds_a_multi_verb_runnable():
    # "scheduled" appears only in the add verb's description.
    scores = bm25f_scores("add a scheduled job", _corpus())
    assert scores[0] > 0 and scores[0] == max(scores)


def test_choice_options_examples_and_wiki_are_searchable():
    docs = _corpus()
    assert bm25f_scores("sudo", docs)[0] > 0  # a choice option
    assert bm25f_scores("nightly", docs)[0] > 0  # an example description
    assert bm25f_scores("batch servers", docs)[0] > 0  # a wiki fact
    assert bm25f_scores("quokkas", docs)[0] > 0  # a nested verb's description


def test_bulky_command_text_does_not_drown_a_name_match():
    # A many-verb runnable whose command text mentions "ping" in passing must
    # not outrank the runnable actually named ping-host: each field is length-
    # normalised against its own average, so the bulk of a fifty-verb runnable
    # can't swamp another runnable's name hit.
    bulky = {
        "name": "netkit",
        "description": "Network toolkit",
        "args": [],
        "commands": {
            f"verb{i}": {
                "description": ("ping first, then " if i < 2 else "")
                + "probe the endpoint and report "
                + "filler " * 20
            }
            for i in range(50)
        },
    }
    small = [
        {"name": f"tool{i}", "description": f"Does task {i}", "args": []}
        for i in range(5)
    ]
    docs = [runnable_doc(r) for r in (bulky, {"name": "ping-host"}, *small)]
    scores = bm25f_scores("ping", docs)
    assert scores[1] > scores[0] > 0
