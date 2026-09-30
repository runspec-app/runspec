"""Shared fixtures.

``build_become`` skips escalating to the account the process already runs as.
Tests assert the escalated command shape for targets like ``root`` or ``svc``,
so they'd flip when the suite itself runs as that account (CI's root jobs, a
container). Pin "not the current user" by default; ``test_become`` exercises the
real check.
"""

import pytest


@pytest.fixture(autouse=True)
def _not_current_user(request, monkeypatch):
    if "real_current_user" in request.keywords:
        return
    monkeypatch.setattr("runspec_linux_core.become.is_current_user", lambda name: False)
