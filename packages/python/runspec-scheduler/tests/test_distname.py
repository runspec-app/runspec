"""The distribution name a rebranded copy runs under (distname.py)."""

from __future__ import annotations

import pytest

from runspec_scheduler import daemon, distname, keeper


@pytest.fixture(autouse=True)
def _fresh():
    distname.dist_name.cache_clear()
    yield
    distname.dist_name.cache_clear()


def _installed(monkeypatch, names):
    import importlib.metadata as md

    monkeypatch.setattr(md, "packages_distributions", lambda: {"runspec_scheduler": names})


def test_upstream_name(monkeypatch):
    _installed(monkeypatch, ["runspec-scheduler"])
    assert distname.dist_name() == "runspec-scheduler"


def test_rebranded_name_wins(monkeypatch):
    _installed(monkeypatch, ["runspec_scheduler", "mycorp-scheduler"])
    assert distname.dist_name() == "mycorp-scheduler"
    assert distname.pip_spec("supervisor") == "'mycorp-scheduler[supervisor]'"


def test_unknown_falls_back(monkeypatch):
    _installed(monkeypatch, [])
    assert distname.dist_name() == "runspec-scheduler"


def test_version_is_read_under_the_rebranded_name(monkeypatch):
    import importlib.metadata as md

    _installed(monkeypatch, ["mycorp-scheduler"])
    asked = []
    monkeypatch.setattr(md, "version", lambda name: asked.append(name) or "0.10.2")
    assert daemon.installed_version() == "0.10.2" and asked == ["mycorp-scheduler"]


def test_keeper_hint_names_the_rebranded_package(monkeypatch):
    _installed(monkeypatch, ["mycorp-scheduler"])
    _, reason = keeper.choose(keeper.Facts(user="svc", venv_owner="svc", linger=False, systemd_version=252, user_bus=True, supervisord=None, crontab=True))
    assert "pip install 'mycorp-scheduler[supervisor]'" in reason
