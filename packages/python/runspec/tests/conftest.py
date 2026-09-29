"""Suite-wide fixtures.

The venv owner's first run auto-creates ``logs/inbox`` and records that in the
audit log (docs/design/venv-logs-inbox.md §4). Most tests read the first audit
record of a fresh temp venv, so auto-provisioning is off unless a test is
marked ``@pytest.mark.inbox`` (the inbox tests, which exercise it directly).
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "inbox: leave the logs-inbox auto-provisioning on")


@pytest.fixture(autouse=True)
def _no_auto_inbox(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("inbox"):
        return
    from runspec import inbox

    real = inbox.ensure_inbox

    def check_only(log_dir, owner=None, *, force=False):  # type: ignore[no-untyped-def]
        # An explicit request (`logs share --inbox`) still creates; the automatic
        # check-and-set on a run only reports.
        return real(log_dir, owner, force=True) if force else inbox.state(log_dir, owner)

    monkeypatch.setattr(inbox, "ensure_inbox", check_only)
