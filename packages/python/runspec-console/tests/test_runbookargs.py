"""Runbook params are runspec args — parsed/validated by the same core machinery."""

from runspec_console import runbookargs


def _table():
    return {
        "app": {"type": "str", "required": True},
        "filter": {
            "options": ["errors-only", "since-midnight"],
            "default": "errors-only",
        },
        "count": {"default": 5, "range": [1, 10]},
        "host": {"type": "str", "required": True, "ui": "host"},
        "logpath": {"type": "path", "required": True},
    }


def test_parse_applies_inference_and_keeps_ui():
    by_name = {d["name"]: d for d in runbookargs.parse_runbook_args(_table())}
    # options → choice; numeric default → int; required inferred from no default.
    assert by_name["filter"]["type"] == "choice"
    assert by_name["filter"]["options"] == ["errors-only", "since-midnight"]
    assert by_name["count"]["type"] == "int"
    assert by_name["app"]["required"] is True
    # The console `ui` hint passes through untouched (→ host dropdown in the form).
    assert by_name["host"]["ui"] == "host"


def test_parse_empty_table():
    assert runbookargs.parse_runbook_args(None) == []
    assert runbookargs.parse_runbook_args({}) == []


def test_validate_ok():
    assert (
        runbookargs.validate_runbook_values(
            _table(),
            {
                "app": "web",
                "filter": "errors-only",
                "count": 5,
                "host": "h1",
                "logpath": "/var/log/app.log",
            },
        )
        == []
    )


def test_validate_required_missing():
    errs = runbookargs.validate_runbook_values(_table(), {"filter": "errors-only"})
    joined = "\n".join(errs)
    assert "--app" in joined and "--host" in joined and "--logpath" in joined


def test_validate_bad_choice():
    errs = runbookargs.validate_runbook_values(
        _table(), {"app": "x", "host": "h", "logpath": "/p", "filter": "nope"}
    )
    assert any("filter" in e for e in errs)


def test_validate_out_of_range():
    errs = runbookargs.validate_runbook_values(
        _table(), {"app": "x", "host": "h", "logpath": "/p", "count": 99}
    )
    assert any("range" in e.lower() for e in errs)


def test_validate_skips_path_coercion():
    """A runbook `path` is usually a *remote* path — it must not be resolved against
    the local console filesystem, so any non-empty string passes."""
    assert (
        runbookargs.validate_runbook_values(
            _table(),
            {"app": "x", "host": "h", "logpath": "/srv/prod/web/logs/today.log"},
        )
        == []
    )


# ── structured steps (the [[runbook.step]] array) ────────────────────────────────


def test_validate_steps_accepts_rows():
    assert (
        runbookargs.validate_runbook_steps([{"text": "a"}, {"text": "b", "title": "T"}])
        == []
    )


def test_validate_steps_accepts_legacy_string_and_none():
    # The legacy string form and an absent body are shape-valid here (emptiness is a
    # caller concern), so an old runbook never trips the new check.
    assert runbookargs.validate_runbook_steps("1. do a\n2. do b") == []
    assert runbookargs.validate_runbook_steps(None) == []


def test_validate_steps_requires_non_empty_text():
    errs = runbookargs.validate_runbook_steps([{"text": "  "}])
    assert any("text" in e for e in errs)


def test_validate_steps_row_must_be_a_table():
    errs = runbookargs.validate_runbook_steps(["just a string"])
    assert any("table" in e for e in errs)


def test_validate_steps_title_must_be_string():
    errs = runbookargs.validate_runbook_steps([{"text": "a", "title": 3}])
    assert any("title" in e for e in errs)


def test_validate_steps_rejects_unknown_key_cliff_guard():
    """A per-step machine-control key (condition/goto/…) would be branching /
    step-result piping — out of scope. It must be rejected, not silently stored."""
    errs = runbookargs.validate_runbook_steps([{"text": "a", "condition": "x > 1"}])
    assert any("condition" in e for e in errs)


def test_validate_steps_non_list_non_string_rejected():
    errs = runbookargs.validate_runbook_steps({"text": "a"})
    assert errs and "list" in errs[0]
