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


# ── read-lenient validation + type aliases (agent read/edit ergonomics) ──────────


def test_validate_values_require_false_skips_missing_required():
    """Reading/consulting a runbook must not demand values for its required args (an
    arg with no default is inferred required), but a supplied bad value still bounces."""
    table = {
        "app": {"type": "str", "required": True},
        "host": {"type": "str", "required": True, "ui": "host"},
        "filter": {"options": ["errors-only", "since-midnight"]},
    }
    # Nothing supplied → no errors under require=False.
    assert runbookargs.validate_runbook_values(table, {}, require=False) == []
    # A supplied out-of-set choice is still rejected.
    errs = runbookargs.validate_runbook_values(table, {"filter": "nope"}, require=False)
    assert any("filter" in e for e in errs)
    # Strict (default) still flags the missing required args.
    assert runbookargs.validate_runbook_values(table, {}) != []


def test_normalize_arg_types_maps_json_spellings():
    norm = runbookargs.normalize_arg_types(
        {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "c": {"type": "number"},
            "d": {"type": "boolean"},
            "e": {"type": "choice", "options": ["x"]},  # already valid, untouched
        }
    )
    assert {k: v["type"] for k, v in norm.items()} == {
        "a": "str",
        "b": "int",
        "c": "float",
        "d": "bool",
        "e": "choice",
    }
    # And the normalised table now passes shape validation.
    assert runbookargs.validate_runbook_arg_table(norm) == []


def test_describe_runbook_params_summarises_interface():
    out = runbookargs.describe_runbook_params(
        {
            "host": {"type": "choice", "options": ["web-01", "web-02"]},
            "count": {"default": 5},
        }
    )
    assert "host (choice, one of: web-01|web-02, required)" in out
    assert "count" in out and "optional" in out
    # No params → empty string (caller omits the section).
    assert runbookargs.describe_runbook_params({}) == ""
