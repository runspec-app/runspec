"""The agent and the operator see which account a runnable runs as.

A shared venv's tools run as its owner (``run_as`` → ``@owner``), not the
account the console logs in as. Search rows carry ``runs_as`` per host so the
agent doesn't assume "the current user", and the confirm card names the account.
"""

from __future__ import annotations

from runspec_console.bridge import Bridge
from runspec_console.bridge_invoke import InvokeMixin
from tests.test_tool_search import _Bridge, _rn


class _OwnerBridge(_Bridge):
    _become_params = InvokeMixin._become_params


def _ident(owner, login="alice", run_as=None):
    return {
        "rawSpec": {"run_as": run_as},
        "venvOwner": owner,
        "loginUser": login,
    }


def test_search_row_names_the_owner_account_per_host():
    b = _OwnerBridge(
        [
            _rn("deb2", "cron", **_ident("svc-ops")),
            _rn("deb3", "cron", **_ident("alice")),
            _rn("deb4", "cron", **_ident("svc-ops", run_as="root")),
        ]
    )
    [row] = b._search_runnables("cron")["results"]
    # deb3's venv is the login account's own → no escalation → left out.
    assert row["runs_as"] == {"deb2": "svc-ops", "deb4": "root"}


def test_search_row_omits_runs_as_when_everything_runs_as_login():
    b = _OwnerBridge([_rn("deb3", "cron", **_ident("alice"))])
    [row] = b._search_runnables("cron")["results"]
    assert "runs_as" not in row


def test_search_row_tolerates_a_become_lookup_failure():
    class _Boom(_Bridge):
        def _become_params(self, cached, host):
            raise RuntimeError("bad record")

    [row] = _Boom([_rn("deb2", "cron")])._search_runnables("cron")["results"]
    assert "runs_as" not in row


def test_confirm_runs_as_for_search_mode_run_runnable():
    b = _OwnerBridge([_rn("deb2", "cron", **_ident("svc-ops"))])
    assert (
        b._confirm_runs_as("run_runnable", {"id": "cron", "host": "deb2"}) == "svc-ops"
    )


def test_confirm_runs_as_is_empty_for_login_account_and_unknown_targets():
    b = _OwnerBridge([_rn("deb3", "cron", **_ident("alice"))])
    assert b._confirm_runs_as("run_runnable", {"id": "cron", "host": "deb3"}) == ""
    assert b._confirm_runs_as("run_runnable", {"id": "nope", "host": "deb3"}) == ""
    assert b._confirm_runs_as("console_status", {}) == ""


def test_confirm_runs_as_for_a_direct_dedup_tool():
    b = _OwnerBridge([_rn("deb2", "cron", **_ident("svc-ops"))], mode="dedup")
    assert "cron" in {t["name"] for t in b._runnables_to_tools()}
    assert b._confirm_runs_as("cron", {"host": "deb2"}) == "svc-ops"


def test_bridge_exposes_both_helpers():
    assert callable(Bridge._runs_as_by_host)
    assert callable(Bridge._confirm_runs_as)
