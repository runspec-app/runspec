"""Shared fixtures: crontab backups go to a temp folder, never the test venv."""

from __future__ import annotations

from pathlib import Path

import pytest

from runspec_scheduler import cronsync


@pytest.fixture(autouse=True)
def _backups_in_tmp(tmp_path_factory, monkeypatch):
    folder = tmp_path_factory.mktemp("crontab-backups")
    monkeypatch.setattr(cronsync, "_backup_dir", lambda: folder)
    return folder


@pytest.fixture(autouse=True)
def _venv_bin(monkeypatch):
    """This venv's scripts folder is ``/v/bin`` in tests, so the crontab lines the
    tests write (``/v/bin/runspec exec …``) count as this venv's."""
    monkeypatch.setattr(cronsync, "scripts_dir", lambda: Path("/v/bin"))
