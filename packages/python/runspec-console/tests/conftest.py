"""Shared test setup: keep every test out of the real console profile.

``config.app_dir()`` resolves to ``%APPDATA%/runspec-console`` (``~/AppData/
Roaming/…`` elsewhere). Without this, a test that reads config the stub didn't
patch — saved runs, the active host profile, ``[workspace]`` — sees the
developer's real profile and fails only on their machine, and tests that write
(history cache, workspaces) write into it. Each test gets a fresh, empty app-data
root instead; a test that sets ``APPDATA`` itself still wins.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path_factory.mktemp("appdata")))
    monkeypatch.delenv("RUNSPEC_CONSOLE_DIR", raising=False)
