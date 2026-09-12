"""
test_search_rank.py — BM25(F) ranking for search-mode runnable discovery.
"""

from __future__ import annotations

from runspec_console.search_rank import bm25f_scores, tokenize


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
