"""Catalog flattening, namespacing, env collection, and change detection."""

from __future__ import annotations

import runspec_mcp.catalog as catalog_mod
from runspec_mcp.catalog import Catalog
from runspec_mcp.config import FleetConfig

# A discovered spec with a subcommand and an env-declaring arg.
_ITEMS = [
    {
        "runnable": "deploy",
        "config_autonomy": "confirm",
        "spec": {
            "description": "Deploy",
            "autonomy": "confirm",
            "args": {"user": {"type": "str", "env": "WINDOWS_USERNAME"}},
            "commands": {
                "restart": {"description": "restart", "autonomy": "supervised", "args": {}},
            },
        },
    },
    {
        "runnable": "greeter",
        "config_autonomy": "confirm",
        "spec": {"description": "hi", "autonomy": "autonomous", "args": {}},
    },
]


def _catalog(hosts, monkeypatch, items_by_host=None):
    cfg = FleetConfig(hosts=hosts)

    def fake_discover(host, runspec_path, ssh_cfg, pool, timeout=20):
        if items_by_host is not None:
            return items_by_host.get(str(host.get("name")), [])
        return _ITEMS

    monkeypatch.setattr(catalog_mod, "_discover_items", fake_discover)
    return Catalog(cfg, pool=None)


def test_single_host_bare_names(monkeypatch):
    cat = _catalog([{"name": "box", "runspec_paths": ["/v/env-a/bin/runspec"]}], monkeypatch)
    cat.refresh()
    names = {t.name for t in cat.tools()}
    # deploy has a subcommand → flattened leaf; greeter is bare.
    assert "deploy_restart" in names
    assert "greeter" in names


def test_multi_host_prefixed(monkeypatch):
    cat = _catalog(
        [
            {"name": "prod", "ssh": "u@prod", "runspec_paths": ["/v/env-a/bin/runspec"]},
            {"name": "dev", "ssh": "u@dev", "runspec_paths": ["/v/env-a/bin/runspec"]},
        ],
        monkeypatch,
    )
    cat.refresh()
    names = {t.name for t in cat.tools()}
    assert "prod__greeter" in names
    assert "dev__greeter" in names


def test_arg_env_names_collected(monkeypatch):
    cat = _catalog([{"name": "box", "runspec_paths": ["/v/env-a/bin/runspec"]}], monkeypatch)
    cat.refresh()
    deploy_restart = next(t for t in cat.tools() if t.name == "deploy_restart")
    # The inherited `user` arg's env is visible on the leaf for credential matching.
    assert "WINDOWS_USERNAME" in deploy_restart.arg_env_names


def test_autonomy_and_manual(monkeypatch):
    cat = _catalog([{"name": "box", "runspec_paths": ["/v/env-a/bin/runspec"]}], monkeypatch)
    cat.refresh()
    by = {t.name: t for t in cat.tools()}
    assert by["greeter"].autonomy == "autonomous"
    # child's own autonomy escalates the leaf (supervised) over the parent (confirm)
    assert by["deploy_restart"].autonomy in ("confirm", "supervised")


def test_change_detection(monkeypatch):
    items = {"box": list(_ITEMS)}

    def fake_discover(host, runspec_path, ssh_cfg, pool, timeout=20):
        return items.get(str(host.get("name")), [])

    monkeypatch.setattr(catalog_mod, "_discover_items", fake_discover)
    fired = {"n": 0}
    cat = Catalog(FleetConfig(hosts=[{"name": "box", "runspec_paths": ["/v/env-a/bin/runspec"]}]), pool=None, on_change=lambda: fired.__setitem__("n", fired["n"] + 1))

    assert cat.refresh() is True  # empty → populated
    assert cat.refresh() is False  # unchanged
    items["box"] = [_ITEMS[1]]  # drop deploy
    assert cat.refresh() is True  # changed
    assert fired["n"] == 2  # two real changes, not the no-op
