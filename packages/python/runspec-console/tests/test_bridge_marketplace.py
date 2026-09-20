"""
test_bridge_marketplace.py — the Marketplace tab's bridge seam.

Binds the real MarketplaceMixin methods onto a light stub (tmp app dir, a fake
SSH runner, a canned index fetch) and checks: the effective repo list (declared
+ pip-derived, secrets never surfaced), config validation + round-trip, the
catalogue cache / refresh / stale fallback, the venv targets, the installed
probe (real for the local venv, canned over "SSH"), and an install op — plan
validation, the exact remote command (sudo + env-carried index), events, the
discovery kick.
"""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time

import pytest

from runspec_console import config as config_mod
from runspec_console import hosts as hosts_mod
from runspec_console import marketplace as mp
from runspec_console.bridge import Bridge


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    # Keep the test hermetic: no pip.conf / PIP_* from the host machine.
    monkeypatch.setattr(mp, "pip_config_files", lambda **kw: [])
    for k in (
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_CONFIG_FILE",
        "PIP_TRUSTED_HOST",
        "PIP_CERT",
    ):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


class _StubBridge:
    def __init__(self) -> None:
        self._hosts: list[dict] = []
        self._connected_cache: dict[str, bool] = {"web-01": True, "db-01": False}
        self._lock = threading.Lock()
        self._marketplace_lock = threading.Lock()
        self._marketplace_ops: dict[str, dict] = {}
        self._marketplace_installed = None
        self._credentials: list[dict] = []
        self.dispatched: list[tuple[str, dict]] = []
        self.ssh_calls: list[tuple[str, str]] = []
        self.refreshed = 0
        self.ssh_reply: tuple[int, str, str] = (0, "", "")

    def get_config(self) -> dict:
        return {"ssh": {}}

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append((event, detail))

    def _kick_refresh(self, *, force: bool = False) -> None:
        self.refreshed += 1

    def _fanout_workers(self) -> int:
        return 4

    def _ssh_run(self, ssh, command, *, identity_file=None, timeout=15):
        self.ssh_calls.append((ssh, command))
        return self.ssh_reply

    def mcp_credential_options(self):
        return [{"id": "nexus", "label": "Nexus", "kind": "userpass"}]

    def _mcp_credential(self, cred_id):
        if cred_id == "nexus":
            return {
                "id": "nexus",
                "kind": "userpass",
                "username": "bob",
                "secret": "pw",
            }
        return None

    _reload_hosts = Bridge._reload_hosts
    _local_host_entry = Bridge._local_host_entry
    _all_host_entries = Bridge._all_host_entries
    _active_profile_name = Bridge._active_profile_name
    _active_host_entries = Bridge._active_host_entries

    get_marketplace_config = Bridge.get_marketplace_config
    save_marketplace_config = Bridge.save_marketplace_config
    marketplace_catalog = Bridge.marketplace_catalog
    marketplace_targets = Bridge.marketplace_targets
    marketplace_installed = Bridge.marketplace_installed
    marketplace_install = Bridge.marketplace_install
    marketplace_uninstall = Bridge.marketplace_uninstall
    marketplace_op = Bridge.marketplace_op
    marketplace_cancel = Bridge.marketplace_cancel
    marketplace_clear_cache = Bridge.marketplace_clear_cache
    _marketplace_agent_tools = Bridge._marketplace_agent_tools
    _run_marketplace_tool = Bridge._run_marketplace_tool
    _marketplace_credential_options = Bridge._marketplace_credential_options
    _marketplace_repos = Bridge._marketplace_repos
    _marketplace_auth = Bridge._marketplace_auth
    _marketplace_client = Bridge._marketplace_client
    _marketplace_progress = Bridge._marketplace_progress
    _read_catalog = Bridge._read_catalog
    _marketplace_targets = Bridge._marketplace_targets
    _target_host_entry = Bridge._target_host_entry
    _run_on_target = Bridge._run_on_target
    _probe_targets = Bridge._probe_targets
    _start_pip_op = Bridge._start_pip_op
    _run_pip_op = Bridge._run_pip_op


def _write_hosts(hosts):
    hosts_mod.save_hosts(config_mod.hosts_path(), hosts)


def _write_marketplace(section):
    config_mod.write_marketplace(section)


class _CannedFetch:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, *, headers=None, max_bytes=0, auth=None):
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        return 404, "", b""


def _nexus_routes():
    return {
        "https://nexus.corp/simple/": (
            200,
            "text/html",
            b'<a href="runspec-acme/">runspec-acme</a><a href="runspec-acme-core/">runspec-acme-core</a>',
        ),
        "https://nexus.corp/simple/runspec-acme/": (
            200,
            "text/html",
            b'<a href="../../p/runspec_acme-1.2.0-py3-none-any.whl" data-core-metadata="true">runspec_acme-1.2.0-py3-none-any.whl</a>',
        ),
        "https://nexus.corp/p/runspec_acme-1.2.0-py3-none-any.whl.metadata": (
            200,
            "text/plain",
            b"Metadata-Version: 2.1\nName: runspec-acme\nVersion: 1.2.0\nSummary: Acme runnables\nKeywords: acme,ops\nProject-URL: Documentation, https://docs.acme/runspec\nRequires-Dist: runspec>=0.50\n",
        ),
        "https://nexus.corp/simple/runspec-acme-core/": (
            200,
            "text/html",
            b'<a href="../../p/runspec_acme_core-1.0-py3-none-any.whl" data-core-metadata="true">runspec_acme_core-1.0-py3-none-any.whl</a>',
        ),
        "https://nexus.corp/p/runspec_acme_core-1.0-py3-none-any.whl.metadata": (
            200,
            "text/plain",
            b"Metadata-Version: 2.1\nName: runspec-acme-core\nVersion: 1.0\n",
        ),
    }


# --------------------------------------------------------------------------- #


def test_config_lists_declared_and_pip_repos_without_secrets(app, monkeypatch):
    monkeypatch.setattr(
        mp,
        "read_pip_config",
        lambda **kw: {
            "index_url": "https://bob:pw@nexus.corp/simple/",
            "extra_index_urls": ["https://pypi.org/simple"],
            "trusted_hosts": [],
            "cert": None,
            "sources": ["/etc/pip.conf"],
        },
    )
    _write_marketplace(
        {
            "repos": [
                {
                    "name": "Nexus",
                    "url": "https://nexus.corp/simple",
                    "credential": "nexus",
                    "packages": ["acme-tools"],
                }
            ]
        }
    )
    b = _StubBridge()
    cfg = b.get_marketplace_config()
    assert cfg["enabled"] and cfg["auto_pip"]
    assert [r["name"] for r in cfg["repos"]] == ["Nexus", "PyPI"]
    nexus = cfg["repos"][0]
    assert (
        nexus["source"] == "config"
        and nexus["pip_role"] == "index"
        and nexus["credential"] == "nexus"
    )
    assert "pw" not in json.dumps(cfg)
    assert cfg["pip_sources"] == ["/etc/pip.conf"]
    assert cfg["credential_options"][0]["id"] == "nexus"
    assert [r["name"] for r in cfg["declared_repos"]] == ["Nexus"]


def test_save_config_validates_and_round_trips(app):
    b = _StubBridge()
    with pytest.raises(ValueError):
        b.save_marketplace_config({"repos": [{"name": "x", "url": "ftp://nope"}]})
    with pytest.raises(ValueError):
        b.save_marketplace_config(
            {
                "repos": [
                    {"name": "a", "url": "https://x/simple"},
                    {"name": "b", "url": "https://x/simple/"},
                ]
            }
        )
    out = b.save_marketplace_config(
        {
            "auto_pip": False,
            "cache_ttl": 60,
            "include_prerelease": True,
            "repos": [
                {
                    "name": "Nexus",
                    "url": "https://nexus.corp/simple",
                    "scan": False,
                    "packages": ["runspec-acme"],
                    "pip_arg": "extra-index-url",
                    "tags": ["internal"],
                }
            ],
        }
    )
    assert (
        out["auto_pip"] is False
        and out["cache_ttl"] == 60
        and out["include_prerelease"] is True
    )
    assert [r["name"] for r in out["repos"]] == ["Nexus"]  # auto_pip off → no PyPI row
    raw = config_mod.read_config()["marketplace"]
    assert raw == {
        "auto_pip": False,
        "include_prerelease": True,
        "cache_ttl": 60.0,
        "repos": [
            {
                "name": "Nexus",
                "url": "https://nexus.corp/simple",
                "enabled": True,
                "scan": False,
                "packages": ["runspec-acme"],
                "pip_arg": "extra-index-url",
                "tags": ["internal"],
            }
        ],
    }
    # All-defaults → the section disappears.
    b.save_marketplace_config({"auto_pip": True, "repos": []})
    assert "marketplace" not in config_mod.read_config()


def test_catalog_reads_caches_refreshes_and_falls_back_stale(app, monkeypatch):
    fetch = _CannedFetch(_nexus_routes())
    monkeypatch.setattr(mp, "default_fetch", lambda **kw: fetch)
    _write_marketplace(
        {
            "auto_pip": False,
            "repos": [
                {
                    "name": "Nexus",
                    "url": "https://nexus.corp/simple",
                    "credential": "nexus",
                }
            ],
        }
    )
    b = _StubBridge()

    cat = b.marketplace_catalog()
    assert cat["enabled"] and [e["name"] for e in cat["entries"]] == ["runspec-acme"]
    e = cat["entries"][0]
    assert (
        e["version"] == "1.2.0"
        and e["summary"] == "Acme runnables"
        and e["repos"] == ["nexus"]
    )
    assert e["links"]["documentation"] == "https://docs.acme/runspec" and e[
        "keywords"
    ] == ["acme", "ops"]
    assert cat["repos"][0]["count"] == 1 and cat["repos"][0]["error"] is None
    assert cat["errors"] == []
    n = len(fetch.calls)
    phases = [d for ev, d in b.dispatched if ev == "runspec:marketplace_progress"]
    assert [p["status"] for p in phases] == ["reading", "done"]

    # Second read is served from the cache — no fetch.
    b.marketplace_catalog()
    assert len(fetch.calls) == n
    # Refresh re-reads.
    b.marketplace_catalog(refresh=True)
    assert len(fetch.calls) > n

    # Index down → error reported, last entries served stale.
    fetch.routes = {}
    cat3 = b.marketplace_catalog(refresh=True)
    assert cat3["repos"][0]["error"] and cat3["repos"][0]["stale"] is True
    assert [e["name"] for e in cat3["entries"]] == ["runspec-acme"]

    b.marketplace_clear_cache()
    assert not config_mod.marketplace_cache_path().exists()


def test_catalog_hides_curated_packages(app, monkeypatch):
    fetch = _CannedFetch(_nexus_routes())
    monkeypatch.setattr(mp, "default_fetch", lambda **kw: fetch)
    repos = [
        {"name": "Nexus", "url": "https://nexus.corp/simple", "credential": "nexus"}
    ]
    _write_marketplace({"auto_pip": False, "hide": ["runspec-acme"], "repos": repos})
    b = _StubBridge()

    cat = b.marketplace_catalog()
    assert [e["name"] for e in cat["entries"]] == []  # hidden by name
    assert cat["repos"][0]["count"] == 0  # the count reflects visible packages
    assert b.get_marketplace_config()["hide"] == ["runspec-acme"]
    n = len(fetch.calls)

    # hide is a read-time filter over the cached catalogue: change the list and
    # the package reappears without a re-fetch.
    _write_marketplace({"auto_pip": False, "hide": ["runspec-nope"], "repos": repos})
    cat2 = b.marketplace_catalog()
    assert [e["name"] for e in cat2["entries"]] == ["runspec-acme"]
    assert len(fetch.calls) == n  # served from cache, no new fetch

    # a glob hides it too.
    _write_marketplace({"auto_pip": False, "hide": ["runspec-*"], "repos": repos})
    assert b.marketplace_catalog()["entries"] == []


def test_search_marketplace_agent_tool(app, monkeypatch):
    fetch = _CannedFetch(_nexus_routes())
    monkeypatch.setattr(mp, "default_fetch", lambda **kw: fetch)
    _write_marketplace(
        {
            "auto_pip": False,
            "repos": [
                {
                    "name": "Nexus",
                    "url": "https://nexus.corp/simple",
                    "credential": "nexus",
                }
            ],
        }
    )
    b = _StubBridge()

    # Whole catalogue (no query), read-only discovery envelope.
    res = json.loads(b._run_marketplace_tool({}))
    assert res["enabled"] and res["count"] == 1 and res["total"] == 1
    e = res["results"][0]
    assert e["name"] == "runspec-acme" and e["version"] == "1.2.0"
    assert e["summary"] == "Acme runnables" and e["docs"] == "https://docs.acme/runspec"
    assert e["repos"] == ["nexus"] and isinstance(e["verified"], bool)
    assert e["installed"] is False and "operator action" in res["note"]
    n = len(fetch.calls)

    # Query matches name/summary/keywords; a miss returns nothing (served from cache).
    assert json.loads(b._run_marketplace_tool({"query": "acme"}))["count"] == 1
    assert json.loads(b._run_marketplace_tool({"query": "ops"}))["count"] == 1
    assert json.loads(b._run_marketplace_tool({"query": "zzz"}))["count"] == 0
    assert len(fetch.calls) == n  # no re-fetch — reads the cache

    # An in-memory probe (no fresh SSH) marks the package installed.
    b._marketplace_installed = {"targets": [{"dists": {"runspec-acme": "1.1.0"}}]}
    assert json.loads(b._run_marketplace_tool({}))["results"][0]["installed"] is True


def test_search_marketplace_honours_hide_and_disabled(app, monkeypatch):
    fetch = _CannedFetch(_nexus_routes())
    monkeypatch.setattr(mp, "default_fetch", lambda **kw: fetch)
    repos = [
        {"name": "Nexus", "url": "https://nexus.corp/simple", "credential": "nexus"}
    ]
    _write_marketplace({"auto_pip": False, "hide": ["runspec-acme"], "repos": repos})
    b = _StubBridge()
    # A curated-out package is never recommended (hide flows through the catalogue).
    assert json.loads(b._run_marketplace_tool({}))["count"] == 0

    # Disabled: the tool is dropped from the schema and refuses if called.
    _write_marketplace({"enabled": False})
    b2 = _StubBridge()
    assert b2._marketplace_agent_tools() == []
    assert b2._run_marketplace_tool({}).startswith("Error:")


def test_search_marketplace_tool_schema_when_enabled(app):
    b = _StubBridge()
    (tool,) = b._marketplace_agent_tools()
    assert tool["name"] == "search_marketplace"
    props = tool["input_schema"]["properties"]
    assert set(props) == {"query", "limit"} and not tool["input_schema"].get("required")


def test_catalog_disabled(app):
    _write_marketplace({"enabled": False})
    b = _StubBridge()
    assert b.marketplace_catalog() == {
        "enabled": False,
        "entries": [],
        "repos": [],
        "errors": [],
    }
    with pytest.raises(ValueError):
        b.marketplace_install({"package": "x", "targets": [{"id": "local::x"}]})


def test_targets_list_local_and_remote_venvs(app):
    _write_hosts(
        [
            {
                "name": "web-01",
                "ssh": "ops@web-01",
                "runspec_paths": [
                    "/opt/venvs/ops/bin/runspec",
                    "/opt/venvs/web/bin/runspec",
                ],
            },
            {
                "name": "db-01",
                "ssh": "ops@db-01",
                "runspec_paths": ["/opt/venvs/db/bin/runspec"],
            },
        ]
    )
    b = _StubBridge()
    targets = b.marketplace_targets()
    remote = [t for t in targets if not t["local"]]
    assert [
        (t["host"], t["venv"], t["path"], t["python"], t["connected"]) for t in remote
    ] == [
        ("web-01", "ops", "/opt/venvs/ops", "/opt/venvs/ops/bin/python", True),
        ("web-01", "web", "/opt/venvs/web", "/opt/venvs/web/bin/python", True),
        ("db-01", "db", "/opt/venvs/db", "/opt/venvs/db/bin/python", False),
    ]
    assert remote[0]["id"] == "web-01::/opt/venvs/ops"
    local = [t for t in targets if t["local"]]
    assert all(t["host"] == "local" and t["connected"] for t in local)


def test_installed_probe_local_real_and_remote_canned(app):
    _write_hosts(
        [
            {
                "name": "web-01",
                "ssh": "ops@web-01",
                "runspec_paths": ["/opt/venvs/ops/bin/runspec"],
            },
            {
                "name": "db-01",
                "ssh": "ops@db-01",
                "runspec_paths": ["/opt/venvs/db/bin/runspec"],
            },
        ]
    )
    b = _StubBridge()
    b.ssh_reply = (
        0,
        'RSMP{"prefix": "/opt/venvs/ops", "python": "3.12.1", "owner": "svc", "user": "ops", "dists": {"runspec": "0.50.0", "runspec-acme": "1.1.0", "boto3": "1.0"}}',
        "",
    )
    res = b.marketplace_installed()
    rows = {t["id"]: t for t in res["targets"]}
    web = rows["web-01::/opt/venvs/ops"]
    assert (
        web["ok"]
        and web["owner"] == "svc"
        and web["user"] == "ops"
        and web["python_version"] == "3.12.1"
    )
    assert web["dists"] == {
        "runspec": "0.50.0",
        "runspec-acme": "1.1.0",
    }  # boto3 filtered out
    assert (
        rows["db-01::/opt/venvs/db"]["ok"] is False
        and "not connected" in rows["db-01::/opt/venvs/db"]["error"]
    )
    # The remote probe ran the venv's python with the probe snippet.
    ssh, cmd = b.ssh_calls[0]
    assert ssh == "ops@web-01" and cmd.startswith("/opt/venvs/ops/bin/python -c ")
    # Cached until refresh.
    assert b.marketplace_installed() is res
    assert b.marketplace_installed(refresh=True) is not res
    # A local venv (this interpreter's) is probed for real when the console binary is found.
    local = [t for t in res["targets"] if t["local"]]
    for t in local:
        assert t["ok"], t["error"]
        assert "runspec" in t["dists"]


def _wait_op(b, op, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = b.marketplace_op(op)
        if rec and rec["status"] != "running":
            return rec
        time.sleep(0.02)
    raise AssertionError("op did not finish")


def test_install_plan_validation(app):
    _write_hosts(
        [
            {
                "name": "db-01",
                "ssh": "ops@db-01",
                "runspec_paths": ["/opt/venvs/db/bin/runspec"],
            }
        ]
    )
    b = _StubBridge()
    with pytest.raises(ValueError):
        b.marketplace_install(
            {
                "package": "https://evil/x.whl",
                "targets": [{"id": "db-01::/opt/venvs/db"}],
            }
        )
    with pytest.raises(ValueError):
        b.marketplace_install({"package": "runspec-acme", "targets": []})
    with pytest.raises(ValueError):
        b.marketplace_install(
            {"package": "runspec-acme", "targets": [{"id": "nope::x"}]}
        )
    with pytest.raises(ValueError, match="not connected"):
        b.marketplace_install(
            {"package": "runspec-acme", "targets": [{"id": "db-01::/opt/venvs/db"}]}
        )
    _write_hosts(
        [
            {
                "name": "web-01",
                "ssh": "ops@web-01",
                "runspec_paths": ["/opt/venvs/ops/bin/runspec"],
            }
        ]
    )
    with pytest.raises(ValueError, match="user name"):
        b.marketplace_install(
            {
                "package": "runspec-acme",
                "targets": [{"id": "web-01::/opt/venvs/ops", "run_as": "bad user"}],
            }
        )
    with pytest.raises(ValueError, match="Unknown repo"):
        b.marketplace_install(
            {
                "package": "runspec-acme",
                "repo": "ghost",
                "targets": [{"id": "web-01::/opt/venvs/ops"}],
            }
        )


def test_install_remote_runs_pip_as_owner_with_index_env_and_events(app, monkeypatch):
    _write_hosts(
        [
            {
                "name": "web-01",
                "ssh": "ops@web-01",
                "runspec_paths": ["/opt/venvs/ops/bin/runspec"],
            }
        ]
    )
    _write_marketplace(
        {
            "auto_pip": False,
            "repos": [
                {
                    "name": "Nexus",
                    "url": "https://nexus.corp/simple",
                    "credential": "nexus",
                }
            ],
        }
    )
    b = _StubBridge()
    b.ssh_reply = (
        0,
        "Collecting runspec-acme\nSuccessfully installed runspec-acme-1.2.0",
        "",
    )

    out = b.marketplace_install(
        {
            "package": "runspec-acme",
            "repo": "nexus",
            "upgrade": True,
            "targets": [{"id": "web-01::/opt/venvs/ops", "run_as": "svc"}],
        }
    )
    rec = _wait_op(b, out["op"])
    assert rec["status"] == "finished"
    (r,) = rec["results"]
    assert r["ok"] and r["exit_code"] == 0 and "Successfully installed" in r["summary"]

    ssh, cmd = b.ssh_calls[-1]
    assert ssh == "ops@web-01"
    toks = shlex.split(cmd)
    assert toks[:5] == ["sudo", "-n", "-u", "svc", "env"]
    assert toks[5] == "PIP_INDEX_URL=https://bob:pw@nexus.corp/simple/"
    assert toks[6:10] == ["/opt/venvs/ops/bin/python", "-m", "pip", "install"]
    assert "-U" in toks and toks[-1] == "runspec-acme"

    events = [ev for ev, _ in b.dispatched]
    assert (
        "runspec:marketplace_progress" in events
        and events[-1] == "runspec:marketplace_done"
    )
    done = b.dispatched[-1][1]
    assert (
        done["action"] == "install"
        and done["results"][0]["ok"]
        and "lines" not in done["results"][0]
    )
    assert b.refreshed == 1  # new runnables → discovery kicked
    assert b._marketplace_installed is None


def test_uninstall_remote_without_run_as_and_failure_reported(app):
    _write_hosts(
        [
            {
                "name": "web-01",
                "ssh": "ops@web-01",
                "runspec_paths": ["/opt/venvs/ops/bin/runspec"],
            }
        ]
    )
    b = _StubBridge()
    b.ssh_reply = (1, "", "ERROR: Cannot uninstall runspec-acme")
    out = b.marketplace_uninstall(
        {"package": "runspec-acme", "targets": [{"id": "web-01::/opt/venvs/ops"}]}
    )
    rec = _wait_op(b, out["op"])
    (r,) = rec["results"]
    assert (
        r["ok"] is False and r["exit_code"] == 1 and "Cannot uninstall" in r["summary"]
    )
    ssh, cmd = b.ssh_calls[-1]
    assert (
        cmd
        == "/opt/venvs/ops/bin/python -m pip uninstall -y --disable-pip-version-check runspec-acme"
    )
    assert b.refreshed == 0  # nothing changed


def test_install_local_streams_lines(app, monkeypatch):
    b = _StubBridge()
    local = [t for t in b.marketplace_targets() if t["local"]]
    if not local:
        pytest.skip("no local runspec binary next to this interpreter")
    # Replace the pip invocation with a harmless python that prints two lines.
    real = mp.pip_install_argv

    def fake_argv(python, specs, **kw):
        return [
            sys.executable,
            "-c",
            "print('line one'); print('Successfully installed fake-1.0')",
        ]

    monkeypatch.setattr(mp, "pip_install_argv", fake_argv)
    try:
        out = b.marketplace_install(
            {"package": "fake", "targets": [{"id": local[0]["id"]}]}
        )
        rec = _wait_op(b, out["op"])
    finally:
        monkeypatch.setattr(mp, "pip_install_argv", real)
    (r,) = rec["results"]
    assert r["ok"] and r["lines"] == ["line one", "Successfully installed fake-1.0"]
    lines = [
        d["line"]
        for ev, d in b.dispatched
        if ev == "runspec:marketplace_progress" and d.get("phase") == "line"
    ]
    assert lines == ["line one", "Successfully installed fake-1.0"]
