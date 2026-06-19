"""Skill params are runspec args — parsed/validated by the same core machinery."""

from runspec_console import skillargs


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
    by_name = {d["name"]: d for d in skillargs.parse_skill_args(_table())}
    # options → choice; numeric default → int; required inferred from no default.
    assert by_name["filter"]["type"] == "choice"
    assert by_name["filter"]["options"] == ["errors-only", "since-midnight"]
    assert by_name["count"]["type"] == "int"
    assert by_name["app"]["required"] is True
    # The console `ui` hint passes through untouched (→ host dropdown in the form).
    assert by_name["host"]["ui"] == "host"


def test_parse_empty_table():
    assert skillargs.parse_skill_args(None) == []
    assert skillargs.parse_skill_args({}) == []


def test_validate_ok():
    assert (
        skillargs.validate_skill_values(
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
    errs = skillargs.validate_skill_values(_table(), {"filter": "errors-only"})
    joined = "\n".join(errs)
    assert "--app" in joined and "--host" in joined and "--logpath" in joined


def test_validate_bad_choice():
    errs = skillargs.validate_skill_values(
        _table(), {"app": "x", "host": "h", "logpath": "/p", "filter": "nope"}
    )
    assert any("filter" in e for e in errs)


def test_validate_out_of_range():
    errs = skillargs.validate_skill_values(
        _table(), {"app": "x", "host": "h", "logpath": "/p", "count": 99}
    )
    assert any("range" in e.lower() for e in errs)


def test_validate_skips_path_coercion():
    """A skill `path` is usually a *remote* path — it must not be resolved against
    the local console filesystem, so any non-empty string passes."""
    assert (
        skillargs.validate_skill_values(
            _table(),
            {"app": "x", "host": "h", "logpath": "/srv/prod/web/logs/today.log"},
        )
        == []
    )
