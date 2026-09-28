"""plan_invocation: a schedule's {name: value} args → argv + env, faithful to the
runnable's spec (flags, default-true flags, lists, positionals, rest) and refusing
what the command line can't express (incl. secrets)."""

from __future__ import annotations

import pytest

from runspec_scheduler.discovery import check_invocation
from runspec_scheduler.invocation import InvocationError, env_key, plan_invocation

# loader-shaped (not yet inferred): `default = false` is a flag only after inference
SPEC = {
    "args": {
        "target": {"position": 1, "description": "t"},
        "verbose": {"default": False},
        "dry-run": {"default": True},
        "tags": {"type": "str", "multiple": True, "required": False},
        "level": {"options": ["info", "warn"], "default": "info"},
        "count": {"default": 3},
        "api-token": {"type": "password", "required": False},
        "extra": {"type": "rest"},
    },
    "commands": {},
}


def plan(args):
    return plan_invocation("deploy-app", SPEC, (), args)


def test_flags_true_false_and_default_true_off():
    p = plan({"verbose": True})
    assert p.argv == ["--verbose"] and p.env == {}
    assert plan({"verbose": False}).argv == []  # already the default → nothing
    p = plan({"dry_run": False})  # default true → argv can't; env channel can
    assert p.argv == [] and p.env == {"RUNSPEC_DEPLOY_APP_ARG_DRY_RUN": "0"}
    assert plan({"dry-run": True}).argv == ["--dry-run"]


def test_no_negated_flags_ever():
    for value in (True, False):
        for name in ("verbose", "dry-run"):
            assert not any(t.startswith("--no-") for t in plan({name: value}).argv)


def test_list_repeats_the_flag():
    assert plan({"tags": ["a", "b"]}).argv == ["--tags", "a", "--tags", "b"]


def test_positional_is_passed_by_name():
    assert plan({"target": "prod"}).argv == ["--target", "prod"]


def test_scalars():
    assert plan({"count": 5, "level": "warn"}).argv == ["--count", "5", "--level", "warn"]


def test_rest_goes_after_a_separator():
    assert plan({"target": "prod", "extra": ["-x", "1"]}).argv == ["--target", "prod", "--", "-x", "1"]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"api_token": "s3cret"}, "password"),
        ({"count": [1, 2]}, "takes one value"),
        ({"target": {"a": 1}}, "table/object"),
        ({"verbose": "yes"}, "flag"),
        ({"tags": [["nested"]]}, "plain values"),
    ],
)
def test_refused(args, needle):
    with pytest.raises(InvocationError, match=needle):
        plan(args)
    # …and the same reason surfaces at `add` time
    problem = check_invocation(SPEC, (), args)
    assert problem and needle in problem


def test_env_key():
    assert env_key("deploy-app", "dry-run") == "RUNSPEC_DEPLOY_APP_ARG_DRY_RUN"


def test_spec_less_fallback_never_negates():
    p = plan_invocation("x", {}, (), {"a": True, "b": False, "c": ["1", "2"], "d": 4})
    assert p.argv == ["--a", "--c", "1", "--c", "2", "--d", "4"]
