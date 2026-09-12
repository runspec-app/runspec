"""
test_improve.py — the pure Improve analysis core (``improve.py``).

Improve mines saved conversations + run-history records into an effectiveness
report. The core is pure (stdlib only), so these tests build conversation/history
fixtures by hand and assert the metrics, incident detection, scores, prompt, and
rendering — no disk, no model, no bridge. The detectors match the exact console
marker strings (bounce ``Error:``/``✗``, ``paused after N tool steps``, ``↻ Trying
to recover``, ``⚠ Error:``), so a fixture mirrors what the console actually writes.
"""

from __future__ import annotations


from runspec_console import improve


def _conv(cid, turns):
    return {"id": cid, "turns": turns}


def test_classify_bounce_buckets():
    assert (
        improve.classify_bounce("Error: unknown argument(s) for x: foo")
        == "unknown_arg"
    )
    assert (
        improve.classify_bounce("Error: missing required argument: node")
        == "missing_required"
    )
    assert (
        improve.classify_bounce(
            "Error: 'us' is not a valid choice; must be one of a, b"
        )
        == "bad_choice"
    )
    assert (
        improve.classify_bounce("Error: count out of range (must be between 1 and 10)")
        == "out_of_range"
    )
    assert improve.classify_bounce("Error: unknown command 'frob'") == "bad_command"
    assert improve.classify_bounce("Error: something else") == "other"


def test_search_returned_nothing_detects_empty_json_and_substring():
    assert improve._search_returned_nothing('{"results": [], "runbooks": []}')
    assert not improve._search_returned_nothing(
        '{"results": [{"id": "a"}], "runbooks": []}'
    )
    # Non-JSON output falls back to a substring check.
    assert improve._search_returned_nothing("no matching runnables found")


def test_run_attempt_and_runnable_identity():
    assert improve._is_run_attempt("run_runnable")
    assert improve._is_run_attempt("grp__drain")
    assert not improve._is_run_attempt("search_runnables")
    assert improve._runnable_of("run_runnable", {"id": "grp__drain"}) == "grp__drain"
    assert improve._runnable_of("grp__drain", {}) == "drain"


def test_build_report_counts_search_funnel_and_bounces():
    convs = [
        _conv(
            "c1",
            [
                {
                    "user": "drain it",
                    "assistant": "ok",
                    "tools": [
                        {
                            "name": "search_runnables",
                            "input": {"query": "drain"},
                            "output": '{"results": [{"id": "grp__drain"}], "runbooks": []}',
                        },
                        {
                            "name": "run_runnable",
                            "input": {"id": "grp__drain"},
                            "output": "Error: unknown argument(s) for drain: nodex. Valid arguments: node",
                        },
                        {
                            "name": "run_runnable",
                            "input": {"id": "grp__drain"},
                            "output": "drained web01",
                        },
                    ],
                },
            ],
        ),
    ]
    rep = improve.build_report(convs, [], window_days=30)
    m = rep["metrics"]
    assert m["searches"] == 1 and m["empty_searches"] == 0
    assert m["runs"] == 2 and m["runs_after_search"] == 2
    assert m["run_attempts"] == 2 and m["arg_bounces"] == 1
    assert rep["bounce_kinds"] == {"unknown_arg": 1}
    assert rep["scores"]["arg_bounce_rate_pct"] == 50.0
    assert rep["scores"]["search_before_run_pct"] == 100.0
    # one arg_bounce incident flagged, carrying the runnable + kind
    bounces = [i for i in rep["incidents"] if i["category"] == "arg_bounce"]
    assert bounces and bounces[0]["runnable"] == "grp__drain"
    assert bounces[0]["bounce_kind"] == "unknown_arg"


def test_build_report_flags_empty_search():
    convs = [
        _conv(
            "c1",
            [
                {
                    "user": "x",
                    "assistant": "",
                    "tools": [
                        {
                            "name": "search_runnables",
                            "input": {"query": "zzz"},
                            "output": '{"results": [], "runbooks": []}',
                        }
                    ],
                }
            ],
        )
    ]
    rep = improve.build_report(convs, [], window_days=7)
    assert rep["metrics"]["empty_searches"] == 1
    assert any(i["category"] == "empty_search" for i in rep["incidents"])
    assert rep["scores"]["empty_search_rate_pct"] == 100.0


def test_build_report_detects_stop_recovery_and_durable_error_markers():
    convs = [
        _conv(
            "c1",
            [
                {
                    "user": "a",
                    "assistant": 'working \n\n_(paused after 25 tool steps — say "continue" to keep going)_',
                    "tools": [],
                },
                {
                    "user": "b",
                    "assistant": "⚠ boom\n↻ Trying to recover (1/1)…",
                    "tools": [],
                },
                {"user": "c", "assistant": "⚠ Error: the model gave up", "tools": []},
            ],
        )
    ]
    rep = improve.build_report(convs, [], window_days=7)
    m = rep["metrics"]
    assert m["stops"] == 1 and m["recoveries"] == 1 and m["durable_errors"] == 1
    cats = {i["category"] for i in rep["incidents"]}
    assert {"stop", "recovery", "durable_error"} <= cats


def test_build_report_counts_autonomy_decisions():
    convs = [
        _conv(
            "c1",
            [
                {
                    "user": "x",
                    "assistant": "",
                    "tools": [
                        {
                            "name": "grp__wipe",
                            "input": {},
                            "output": "✗ declined",
                            "operator_decision": "denied",
                        },
                        {
                            "name": "grp__restart",
                            "input": {},
                            "output": "ok",
                            "operator_decision": "edited",
                        },
                    ],
                }
            ],
        )
    ]
    rep = improve.build_report(convs, [], window_days=7)
    assert rep["metrics"]["autonomy_denied"] == 1
    assert rep["metrics"]["autonomy_edited"] == 1
    assert rep["autonomy_by_tool"]["grp__wipe"]["denied"] == 1


def test_build_report_uses_run_history_failures():
    hist = [
        {
            "runnable": "drain",
            "exitCode": 1,
            "ts": "2026-09-01T00:00:00+00:00",
            "runId": "r1",
            "host": "web01",
            "exception": {"type": "ValueError", "message": "boom"},
        },
        {"runnable": "drain", "exitCode": 0, "ts": "2026-09-01T00:00:00+00:00"},
        {
            "runnable": "drain",
            "exitCode": 130,
            "ts": "2026-09-01T00:00:00+00:00",
        },  # cancelled, not a failure
    ]
    rep = improve.build_report([], hist, window_days=30)
    assert rep["metrics"]["history_runs"] == 3
    assert rep["metrics"]["history_failures"] == 1
    assert rep["scores"]["history_failure_rate_pct"] == 33.3
    fail = [i for i in rep["incidents"] if i["category"] == "run_failure"]
    assert fail and fail[0]["run_id"] == "r1" and "ValueError" in fail[0]["evidence"]


def test_comment_makes_it_a_problem_report_and_is_carried():
    rep = improve.build_report([], [], comment="agent picks wrong host", window_days=7)
    assert rep["kind"] == "problem_report"
    assert rep["comment"] == "agent picks wrong host"
    prompt = improve.build_synthesis_prompt(rep)
    assert "agent picks wrong host" in prompt


def test_since_iso_filters_old_turns_and_records():
    convs = [
        _conv(
            "c1",
            [
                {
                    "user": "old",
                    "assistant": 'x _(paused after 5 tool steps — say "continue" to keep going)_',
                    "tools": [],
                    "ts": "2026-01-01T00:00:00+00:00",
                },
                {
                    "user": "new",
                    "assistant": 'y _(paused after 5 tool steps — say "continue" to keep going)_',
                    "tools": [],
                    "ts": "2026-09-01T00:00:00+00:00",
                },
            ],
        )
    ]
    hist = [{"runnable": "a", "exitCode": 1, "ts": "2026-01-01T00:00:00+00:00"}]
    rep = improve.build_report(
        convs, hist, since_iso="2026-06-01T00:00:00+00:00", window_days=90
    )
    assert rep["metrics"]["turns"] == 1  # only the new turn
    assert rep["metrics"]["stops"] == 1
    assert rep["metrics"]["history_failures"] == 0  # old record excluded


def test_operator_run_turns_are_ignored():
    convs = [
        _conv(
            "c1",
            [
                {
                    "kind": "operator_run",
                    "runnable": "x",
                    "output": "⚠ Error: not a chat turn",
                },
                {"user": "real", "assistant": "ok", "tools": []},
            ],
        )
    ]
    rep = improve.build_report(convs, [], window_days=7)
    assert rep["metrics"]["turns"] == 1
    assert rep["metrics"]["durable_errors"] == 0  # the operator_run text is not scanned


def test_incidents_are_prioritised_and_capped():
    turns = []
    # many low-priority empty searches + one durable error
    for i in range(50):
        turns.append(
            {
                "user": str(i),
                "assistant": "",
                "tools": [
                    {
                        "name": "search_runnables",
                        "input": {"query": "q"},
                        "output": '{"results": [], "runbooks": []}',
                    }
                ],
            }
        )
    turns.append({"user": "boom", "assistant": "⚠ Error: fatal", "tools": []})
    rep = improve.build_report(
        [_conv("c1", turns)], [], window_days=30, max_incidents=5
    )
    assert rep["incident_shown"] == 5
    assert rep["incident_total"] > 5
    # the durable error (highest weight) survives the cap
    assert rep["incidents"][0]["category"] == "durable_error"


def test_synthesis_prompt_names_developers_and_asks_for_json():
    rep = improve.build_report(
        [],
        [],
        window_days=7,
        developer_emails=["a@x.com", "b@x.com"],
        product_name="Acme",
    )
    p = improve.build_synthesis_prompt(rep)
    assert "a@x.com, b@x.com" in p
    assert "Acme" in p
    assert '"recommendations"' in p


def test_parse_synthesis_tolerates_fences_and_prose():
    assert improve.parse_synthesis('```json\n{"summary": "s"}\n```') == {"summary": "s"}
    assert improve.parse_synthesis('sure! {"summary": "s"} done') == {"summary": "s"}
    assert improve.parse_synthesis("not json") == {}
    assert improve.parse_synthesis("") == {}


def test_learnings_snippet_from_recommendations_and_from_findings():
    rep = improve.build_report(
        [
            _conv(
                "c1",
                [
                    {
                        "user": "x",
                        "assistant": "",
                        "tools": [
                            {
                                "name": "grp__drain",
                                "input": {},
                                "output": "Error: unknown argument foo",
                            }
                        ],
                    }
                ],
            )
        ],
        [],
        window_days=7,
    )
    # deterministic (no synthesis) → from findings
    snippet = improve.build_learnings_snippet(rep)
    assert "[[learning]]" in snippet and "drain" in snippet
    # with synthesis → from recommendations
    synth = {
        "recommendations": [
            {
                "category": "runnable_args",
                "target": "drain",
                "why": "w",
                "fix": "f",
                "confidence": "high",
            }
        ]
    }
    snippet2 = improve.build_learnings_snippet(rep, synth)
    assert 'category = "runnable_args"' in snippet2 and '"f"' in snippet2


def test_render_markdown_and_mailto():
    rep = improve.build_report(
        [],
        [],
        comment="broken",
        window_days=7,
        developer_emails=["dev@x.com"],
        product_name="Acme",
    )
    md = improve.render_markdown(rep)
    assert "# Acme — Improve Problem report" in md
    assert "> broken" in md
    url = improve.mailto_url(rep, md)
    assert url.startswith("mailto:dev%40x.com?subject=")
    assert "body=" in url


def test_mailto_body_is_length_bounded():
    rep = improve.build_report([], [], window_days=7, developer_emails=["d@x.com"])
    url = improve.mailto_url(rep, "x" * 10000, max_body=100)
    # body is capped, so the URL can't carry the full 10k
    assert len(url) < 1000


# ── catalog description quality signal ────────────────────────────────────────


def test_catalog_description_issues_flags_short_empty_and_duplicate():
    runnables = [
        {"id": "svc__restart", "name": "restart", "description": "Restarts it."},
        {"id": "fs__ls", "name": "ls", "description": ""},
        {
            "id": "a__x",
            "name": "x",
            "description": "Does the thing in a perfectly clear, descriptive way.",
        },
        {
            "id": "b__y",
            "name": "y",
            "description": "Does the thing in a perfectly clear, descriptive way.",
        },
    ]
    issues = improve.catalog_description_issues(runnables)
    kinds = {(i["runnable"], i["kind"]) for i in issues}
    assert ("svc__restart", "short") in kinds  # < 40 chars
    assert ("fs__ls", "empty") in kinds
    # a__x and b__y share a first sentence → both flagged duplicate
    assert ("a__x", "duplicate") in kinds
    assert ("b__y", "duplicate") in kinds
    dup = next(
        i for i in issues if i["runnable"] == "a__x" and i["kind"] == "duplicate"
    )
    assert dup["shared_with"] == ["b__y"]


def test_catalog_description_issues_clean_set_is_empty():
    runnables = [
        {
            "id": "a",
            "name": "a",
            "description": "Restart a systemd service by its unit name. More detail here.",
        },
        {
            "id": "b",
            "name": "b",
            "description": "List files in a directory on the target host. Extra prose.",
        },
    ]
    assert improve.catalog_description_issues(runnables) == []


def test_build_report_includes_catalog_description_incidents():
    runnables = [{"id": "svc__x", "name": "x", "description": "Short."}]
    rep = improve.build_report([], [], runnables=runnables)
    assert rep["metrics"]["catalog_desc_issues"] == 1
    assert any(i["category"] == improve.CAT_CATALOG_DESC for i in rep["incidents"])
    md = improve.render_markdown(rep)
    assert "Catalog descriptions needing attention" in md
