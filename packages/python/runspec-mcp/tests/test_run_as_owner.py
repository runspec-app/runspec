"""run_as for a discovered item — the "@owner" default."""

from __future__ import annotations

from runspec_mcp.catalog import _item_run_as


def _item(config=None, owner="svc", user="alice"):
    return {"config_run_as": config, "env_owner": owner, "user": user}


def test_default_is_the_venv_owner():
    assert _item_run_as(_item(), {"run_as": None}) == "svc"


def test_no_escalation_to_the_login_account_or_root():
    assert _item_run_as(_item(owner="alice"), {}) == ""
    assert _item_run_as(_item(owner=""), {}) == ""


def test_older_core_without_owner_never_escalates():
    assert _item_run_as({}, {}) == ""


def test_config_then_runnable():
    assert _item_run_as(_item(config=""), {}) == ""
    assert _item_run_as(_item(config="ops"), {}) == "ops"
    assert _item_run_as(_item(config=""), {"run_as": "root"}) == "root"
    assert _item_run_as(_item(), {"run_as": {"default": "@owner"}}) == "svc"
