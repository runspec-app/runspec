"""Shared fixtures: crontab backups go to a temp folder, never the test venv."""

from __future__ import annotations

import pytest

from runspec_scheduler import cronsync


@pytest.fixture(autouse=True)
def _backups_in_tmp(tmp_path_factory, monkeypatch):
    folder = tmp_path_factory.mktemp("crontab-backups")
    monkeypatch.setattr(cronsync, "_backup_dir", lambda: folder)
    return folder
