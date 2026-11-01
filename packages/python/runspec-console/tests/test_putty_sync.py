"""PuTTY session sync — one saved session per console host ([putty] sync)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runspec_console import putty_sync as ps

# ── pure helpers ─────────────────────────────────────────────────────────


def test_read_defaults_off():
    assert ps.read_putty_sync({}) == {
        "sync": False,
        "profiles": [],
        "keepalive": 30,
        "prefix": "",
    }


def test_read_values_and_clamps():
    cfg = {"putty": {"sync": True, "profiles": [" prod ", ""], "keepalive": 99999}}
    got = ps.read_putty_sync(cfg)
    assert got["sync"] is True
    assert got["profiles"] == ["prod"]
    assert got["keepalive"] == 3600
    assert ps.read_putty_sync({"putty": {"keepalive": "x"}})["keepalive"] == 30


@pytest.mark.parametrize(
    "name,key",
    [
        ("prod/web-01", "prod/web-01"),
        ("Default Settings", "Default%20Settings"),
        ("a*b?c%d\\e", "a%2Ab%3Fc%25d%5Ce"),
        (".hidden", "%2Ehidden"),
        ("a.b", "a.b"),
        ("café", "caf%C3%A9"),
    ],
)
def test_munge_matches_putty_and_round_trips(name, key):
    assert ps.munge(name) == key
    assert ps.unmunge(key) == name


def test_session_name_uses_group_and_prefix():
    assert ps.session_name({"name": "web-01", "group": "prod"}) == "prod/web-01"
    assert ps.session_name({"name": "web-01"}) == "web-01"
    assert (
        ps.session_name({"name": "web-01", "group": "prod"}, "rs/") == "rs/prod/web-01"
    )


def test_select_hosts_skips_local_and_filters_profiles():
    hosts = [
        {"name": "local"},
        {"name": "b", "ssh": "b", "profiles": ["ops"]},
        {"name": "a", "ssh": "a", "group": "prod", "profiles": ["Web"]},
        {"name": "c", "ssh": ""},
    ]
    assert [h["name"] for h in ps.select_hosts(hosts)] == ["b", "a"]
    assert [h["name"] for h in ps.select_hosts(hosts, ["web"])] == ["a"]


def test_host_values_defaults_to_direct_and_keepalive():
    v = ps.host_values(
        hostname="h",
        port=None,
        user="ops",
        key_file="k.ppk",
        keepalive=30,
        proxy=None,
        look={"FontHeight": 12},
        owner="runspec-console",
    )
    assert v["HostName"] == "h" and v["PortNumber"] == 22 and v["Protocol"] == "ssh"
    assert v["UserName"] == "ops" and v["PublicKeyFile"] == "k.ppk"
    assert v["PingIntervalSecs"] == 30 and v["TCPKeepalives"] == 1
    assert v["ProxyMethod"] == 0  # a removed proxy/jump doesn't linger
    assert v["FontHeight"] == 12
    assert v[ps.MARKER] == "runspec-console"


def test_keepalive_off():
    assert ps.keepalive_values(0) == {
        "PingInterval": 0,
        "PingIntervalSecs": 0,
        "TCPKeepalives": 0,
    }


def test_plan_writes_ours_deletes_stale_and_keeps_theirs():
    existing = {
        "prod/web-01": "runspec-console",  # ours, still wanted
        "prod/old": "runspec-console",  # ours, gone
        "web-02": None,  # the operator's own session
        "other": "acme-console",  # a different console's
    }
    got = ps.plan(
        {"prod/web-01": {}, "web-02": {}, "new": {}}, existing, "runspec-console"
    )
    assert got == {
        "write": ["prod/web-01", "new"],
        "delete": ["prod/old"],
        "conflict": ["web-02"],
    }


# ── bridge: values per host + registry writes ───────────────────────────


class FakeStore:
    def __init__(self, existing=None):
        self.sessions: dict[str, dict] = {
            k: ({ps.MARKER: v} if v else {}) for k, v in (existing or {}).items()
        }

    def list(self):
        return {k: v.get(ps.MARKER) for k, v in self.sessions.items()}

    def write(self, name, values):
        self.sessions[name] = dict(values)

    def delete(self, name):
        del self.sessions[name]


def _bridge(hosts):
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch("runspec_console.config.hosts_path", return_value=MagicMock()),
        patch("runspec_console.bridge.Bridge._start_refresh_watcher"),
    ):
        from runspec_console.bridge import Bridge

        b = Bridge()
    b._hosts = hosts
    return b


def _sync(b, cfg, store):
    with (
        patch.object(b, "get_config", return_value=cfg),
        patch("runspec_console.putty_sync.RegistrySessions", return_value=store),
        patch("runspec_console.config.app_slug", return_value="runspec-console"),
        patch(
            "runspec_console.bridge_utils._find_putty_exe",
            side_effect=lambda n="putty.exe": Path(f"C:/PuTTY/{n}"),
        ),
        patch(
            "runspec_console.bridge_utils._ensure_putty_proxy_session",
            side_effect=lambda proxy, name, look=None: name,
        ),
        patch("pathlib.Path.exists", return_value=True),
    ):
        return b.putty_sync_now()


def test_sync_writes_one_session_per_host():
    b = _bridge(
        [
            {"name": "web-01", "ssh": "ops@10.0.0.1:2222", "group": "prod"},
            {"name": "db", "ssh": "10.0.0.2", "identityFile": "C:/k/db_key"},
        ]
    )
    store = FakeStore({"prod/old": "runspec-console", "mine": None})
    cfg = {
        "ssh": {"user": "jason", "identityFile": "C:/k/id"},
        "terminal": {"group_themes": {"prod": "red-alert"}},
    }
    res = _sync(b, cfg, store)
    assert res["ok"] and res["removed"] == ["prod/old"]
    assert {w["session"] for w in res["written"]} == {"prod/web-01", "db"}
    web = store.sessions["prod/web-01"]
    assert web["HostName"] == "10.0.0.1" and web["PortNumber"] == 2222
    assert web["UserName"] == "ops"  # the host's own user wins
    assert web["PublicKeyFile"] == str(Path("C:/k/id.ppk"))  # .ppk preferred
    assert web["UseSystemColours"] == 0  # the prod group's theme
    db = store.sessions["db"]
    assert db["UserName"] == "jason"  # falls back to [ssh] user
    assert db["PublicKeyFile"] == str(Path("C:/k/db_key.ppk"))  # host's own key
    assert "mine" in store.sessions  # the operator's session is untouched


def test_sync_jump_host_uses_local_proxy_command():
    b = _bridge(
        [
            {"name": "bastion", "ssh": "jump@bastion.corp"},
            {"name": "behind", "ssh": "svc@10.0.0.5", "jump": "bastion"},
        ]
    )
    store = FakeStore()
    _sync(b, {"ssh": {"identityFile": "C:/k/id"}}, store)
    v = store.sessions["behind"]
    assert v["ProxyMethod"] == 5 and v["ProxyDNS"] == 2
    cmd = v["ProxyTelnetCommand"]
    assert "plink.exe" in cmd and "-nc %host:%port bastion.corp" in cmd
    assert "-l jump" in cmd
    assert store.sessions["bastion"]["ProxyMethod"] == 0


def test_sync_http_proxy_and_conflict_and_missing_key():
    b = _bridge(
        [
            {"name": "a", "ssh": "a"},
            {"name": "taken", "ssh": "t"},
        ]
    )
    store = FakeStore({"taken": None})
    res = _sync(b, {"ssh": {"proxy": "http://px:3128"}}, store)
    assert store.sessions["a"]["ProxyMethod"] == 3
    assert store.sessions["a"]["ProxyHost"] == "px"
    assert store.sessions["a"]["PublicKeyFile"] == ""
    assert [s["host"] for s in res["skipped"]] == ["taken"]
    assert ps.MARKER not in store.sessions["taken"]


def test_sync_profiles_scope_prunes_out_of_scope_hosts():
    b = _bridge(
        [
            {"name": "a", "ssh": "a", "profiles": ["web"]},
            {"name": "b", "ssh": "b", "profiles": ["db"]},
        ]
    )
    store = FakeStore({"b": "runspec-console"})
    res = _sync(b, {"putty": {"profiles": ["web"]}}, store)
    assert [w["session"] for w in res["written"]] == ["a"]
    assert res["removed"] == ["b"]


def test_remove_deletes_only_ours():
    b = _bridge([])
    store = FakeStore({"x": "runspec-console", "y": None, "z": "acme"})
    with (
        patch("runspec_console.putty_sync.RegistrySessions", return_value=store),
        patch("runspec_console.config.app_slug", return_value="runspec-console"),
    ):
        res = b.putty_sync_remove()
    assert res["removed"] == ["x"]
    assert set(store.sessions) == {"y", "z"}


def test_unsupported_off_windows():
    b = _bridge([])
    with (
        patch.object(b, "get_config", return_value={}),
        patch("runspec_console.putty_sync.RegistrySessions", side_effect=ImportError),
    ):
        res = b.putty_sync_now()
    assert res["supported"] is False and res["ok"] is False


def test_auto_sync_is_a_noop_off_windows_or_when_off():
    b = _bridge([{"name": "a", "ssh": "a"}])
    with (
        patch.object(b, "_putty_sync_run") as run,
        patch.object(b, "get_config", return_value={"putty": {"sync": True}}),
    ):
        with patch.object(sys, "platform", "linux"):
            b._putty_sync_auto()
        run.assert_not_called()
    with (
        patch.object(b, "_putty_sync_run") as run,
        patch.object(b, "get_config", return_value={"putty": {"sync": False}}),
        patch.object(sys, "platform", "win32"),
    ):
        b._putty_sync_auto()
    run.assert_not_called()


def test_auto_sync_runs_in_background_when_on():
    b = _bridge([])
    import threading

    done = threading.Event()
    with (
        patch.object(b, "_putty_sync_run", side_effect=lambda: done.set()),
        patch.object(b, "get_config", return_value={"putty": {"sync": True}}),
        patch.object(sys, "platform", "win32"),
    ):
        b._putty_sync_auto()
        assert done.wait(5)
