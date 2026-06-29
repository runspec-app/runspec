"""
test_sync_pull.py — the Config Sync pull orchestration (Bridge.sync_pull) and the
[config_sync] config helpers.

Covers: read_sync/write_sync round-trips (incl. preserve) + preserve other
sections; a non-destructive pull (collections merge by identity, mappings
deep-overlay, preserve keeps machine-local keys); the local [config_sync] is
re-injected + stamped across the config.toml merge; invalid TOML aborts the whole
pull; existing files are backed up; the app hot-reloads after a pull.

The git layer is mocked — gitsync.fetch_files returns canned bytes — so these
tests need no network or git.
"""

from __future__ import annotations

import tomllib

import runspec_console.config as cfg
import runspec_console.gitsync as gitsync
from runspec_console.bridge import Bridge
from runspec_console.credentials import load_credentials, save_credentials
from runspec_console.filters_store import load_filters, save_filters
from runspec_console.hosts import load_hosts, save_hosts


# ── [config_sync] config helpers ────────────────────────────────────────────────


def _cfg_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")


def test_read_write_sync_round_trips(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_sync(
        {
            "url": "https://example.com/cfg.git",
            "ref": "v1.0.0",
            "credential_id": "config-repo",
            "files": ["config.toml", "runspec_hosts.toml"],
            "preserve": ["config.toml:mailbox.folder"],
        }
    )
    got = cfg.read_sync()
    assert got["url"] == "https://example.com/cfg.git"
    assert got["ref"] == "v1.0.0"
    assert got["files"] == ["config.toml", "runspec_hosts.toml"]
    assert got["preserve"] == ["config.toml:mailbox.folder"]


def test_backups_keep_round_trips(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    # The default is omitted from the file to keep it clean, but reads back as 5.
    cfg.write_sync({"url": "https://x/y.git", "ref": "v1", "backups_keep": 5})
    assert "backups_keep" not in cfg.read_config().get("config_sync", {})
    assert cfg.backups_keep_value(cfg.read_sync()) == 5
    # A non-default value is persisted and coerced on read.
    cfg.write_sync({"url": "https://x/y.git", "ref": "v1", "backups_keep": 0})
    assert cfg.read_config()["config_sync"]["backups_keep"] == 0
    assert cfg.backups_keep_value(cfg.read_sync()) == 0


def test_write_sync_preserves_other_sections(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config(
        {"ssh": {"identityFile": "~/key"}, "llm": {"provider": "anthropic"}}
    )
    cfg.write_sync({"url": "https://example.com/cfg.git", "ref": "v1"})
    back = cfg.read_config()
    assert back["ssh"]["identityFile"] == "~/key"
    assert back["llm"]["provider"] == "anthropic"
    assert back["config_sync"]["url"] == "https://example.com/cfg.git"


# ── sync_pull ────────────────────────────────────────────────────────────────────


class _SyncBridge:
    """Borrows the real pull machinery; stubs the app-lifecycle reactions."""

    sync_pull = Bridge.sync_pull
    _apply_config_layers = Bridge._apply_config_layers
    _atomic_write_bytes = Bridge._atomic_write_bytes
    _merge_synced_file = Bridge._merge_synced_file
    _merge_synced_triggers = Bridge._merge_synced_triggers
    _prune_backups = Bridge._prune_backups

    def __init__(self):
        self._credentials: list = []
        self.reloaded: list[str] = []
        self.triggers_restarted = False

    def get_config(self):
        return cfg.read_config()

    def _sync_auth(self, url, cred_id):
        return gitsync.GitAuth()

    def _reload_hosts(self):
        self.reloaded.append("hosts")

    def _reload_filters(self):
        self.reloaded.append("filters")

    def _reload_credentials(self):
        self._credentials = load_credentials(cfg.credentials_path())

    def _after_config_change(self, old, new):
        self.reloaded.append("config")

    def restart_triggers(self):
        self.triggers_restarted = True


def _wire(monkeypatch, tmp_path, files: dict[str, bytes], *, commit="a1b2c3d4" * 5):
    """Point all paths at tmp_path; make gitsync.fetch_layers return one layer."""
    _wire_layers(monkeypatch, tmp_path, {"": files}, commit=commit)


def _wire_layers(
    monkeypatch,
    tmp_path,
    by_layer: dict[str, dict[str, bytes]],
    *,
    commit="a1b2c3d4" * 5,
):
    """Like ``_wire`` but maps each layer subpath → its files, so a pull folds
    them. ``fetch_layers`` is mocked to return exactly the requested subpaths."""
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "runspec_console.config.credentials_path",
        lambda: tmp_path / "runspec_credentials.toml",
    )
    monkeypatch.setattr(
        gitsync,
        "fetch_layers",
        lambda url, ref, names, auth, subpaths: gitsync.LayeredFetchResult(
            ref=ref,
            commit=commit,
            layers={sp: by_layer.get(sp, {}) for sp in subpaths},
        ),
    )


def _set_sync(files=None, preserve=None, layers=None, backups_keep=None):
    sync = {
        "url": "https://example.com/cfg.git",
        "ref": "v1.0.0",
        "credential_id": "config-repo",
        "files": files or ["config.toml", "runspec_hosts.toml"],
        "preserve": preserve or [],
        "layers": layers or [],
    }
    if backups_keep is not None:
        sync["backups_keep"] = backups_keep
    cfg.write_sync(sync)


def _read_toml(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def test_pull_writes_files_and_preserves_sync_settings(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {
            "config.toml": b'[llm]\nprovider = "anthropic"\n',
            "runspec_hosts.toml": b'[[host]]\nname = "web1"\n',
        },
    )
    _set_sync()

    res = _SyncBridge().sync_pull()

    assert res["ok"] is True
    assert set(res["written"]) == {"config.toml", "runspec_hosts.toml"}
    assert res["commit"] == "a1b2c3d4" * 5
    back = cfg.read_config()
    assert back["llm"]["provider"] == "anthropic"  # repo config landed
    hosts = {h["name"] for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts == {"web1"}
    # the local [config_sync] survived the config.toml merge, now stamped
    assert back["config_sync"]["url"] == "https://example.com/cfg.git"
    assert back["config_sync"]["last_ref"] == "v1.0.0"
    assert back["config_sync"]["last_commit"] == "a1b2c3d4" * 5
    assert back["config_sync"]["last_synced"]


def test_config_deep_overlay_keeps_local_only_and_preserves_default(
    monkeypatch, tmp_path
):
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": b'[llm]\nprovider = "openai"\n[mailbox]\nfolder = "Inbox"\n'},
    )
    # local has an api_key (local-only) and a personal mailbox folder
    cfg.write_config(
        {
            "llm": {"provider": "anthropic", "api_key": "sk-local"},
            "mailbox": {"folder": "Tickets/Mine"},
        }
    )
    _set_sync(files=["config.toml"])  # default preserve covers mailbox.folder

    assert _SyncBridge().sync_pull()["ok"] is True

    back = cfg.read_config()
    assert back["llm"]["provider"] == "openai"  # repo wins on conflict
    assert back["llm"]["api_key"] == "sk-local"  # local-only key survives
    assert back["mailbox"]["folder"] == "Tickets/Mine"  # default-preserved


def test_hosts_merge_keeps_local_only_and_preserves_room_user(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"runspec_hosts.toml": b'[[host]]\nname = "web1"\nssh = "new@web1"\n'},
    )
    save_hosts(
        tmp_path / "runspec_hosts.toml",
        [
            {"name": "web1", "ssh": "old@web1", "room_user": "alice"},
            {"name": "my-laptop", "ssh": "me@laptop"},
        ],
    )
    _set_sync(files=["runspec_hosts.toml"])  # default preserve covers *.room_user

    assert _SyncBridge().sync_pull()["ok"] is True

    hosts = {h["name"]: h for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts["web1"]["ssh"] == "new@web1"  # matched host updated
    assert hosts["web1"]["room_user"] == "alice"  # room_user preserved
    assert "my-laptop" in hosts  # local-only host kept


def test_filters_merged_by_name(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"runspec_filters.toml": b'[[filter]]\nname = "shared"\npipeline = "new"\n'},
    )
    save_filters(
        tmp_path / "runspec_filters.toml",
        [{"name": "shared", "pipeline": "old"}, {"name": "mine", "pipeline": "local"}],
    )
    _set_sync(files=["runspec_filters.toml"])

    assert _SyncBridge().sync_pull()["ok"] is True

    filters = {
        f["name"]: f["pipeline"]
        for f in load_filters(tmp_path / "runspec_filters.toml")
    }
    assert filters == {"shared": "new", "mine": "local"}


def test_triggers_merged_by_name_keeps_local_and_rota_untouched(monkeypatch, tmp_path):
    # The repo file carries triggers AND a [rota] — the rota must be ignored.
    _wire(
        monkeypatch,
        tmp_path,
        {
            "runspec_triggers.toml": b"[rota]\nenabled = true\n[rota.mon]\nstart = '09:00'\n"
            b'[triggers.shared]\nkind = "agent"\n',
        },
    )
    cfg.write_triggers(
        [{"name": "shared", "kind": "rule"}, {"name": "mine", "kind": "agent"}]
    )
    cfg.write_rota({"enabled": False, "mon": {"start": "07:30", "end": "16:00"}})
    _set_sync(files=["runspec_triggers.toml"])

    assert _SyncBridge().sync_pull()["ok"] is True

    names = {t["name"] for t in cfg.read_triggers()}
    assert names == {"shared", "mine"}  # repo's "shared" updated, local "mine" kept
    # rota is per-person — the repo's [rota] is ignored, local rota unchanged
    rota = cfg.read_rota()
    assert rota["enabled"] is False
    assert rota["mon"] == {"start": "07:30", "end": "16:00"}


def test_pull_hot_reloads_and_restarts_triggers(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"config.toml": b"[console]\ntheme = 'dark'\n"})
    _set_sync(files=["config.toml"])

    b = _SyncBridge()
    b.sync_pull()

    assert {"hosts", "filters", "config"} <= set(b.reloaded)
    assert b.triggers_restarted is True


def test_invalid_toml_aborts_whole_pull(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {
            "config.toml": b"a = = broken",
            "runspec_hosts.toml": b'[[host]]\nname = "web1"\n',
        },
    )
    _set_sync()

    res = _SyncBridge().sync_pull()

    assert res["ok"] is False
    assert "not valid TOML" in res["error"]
    assert not (tmp_path / "runspec_hosts.toml").exists()  # nothing written


def test_existing_file_is_backed_up(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"runspec_hosts.toml": b'[[host]]\nname = "web1"\n'})
    _set_sync(files=["runspec_hosts.toml"])
    save_hosts(tmp_path / "runspec_hosts.toml", [{"name": "old"}])

    res = _SyncBridge().sync_pull()

    assert res["ok"] is True
    backups = list(tmp_path.glob("runspec_hosts.toml.bak-*"))
    assert len(backups) == 1
    assert _read_toml(backups[0])["host"][0]["name"] == "old"


def test_no_op_pull_leaves_no_backup(monkeypatch, tmp_path):
    # A re-pull that changes nothing must not leave a backup behind — the bulk of
    # the "backups pile up" problem was these identical-content copies.
    _wire(monkeypatch, tmp_path, {"runspec_hosts.toml": b'[[host]]\nname = "web1"\n'})
    _set_sync(files=["runspec_hosts.toml"])
    save_hosts(tmp_path / "runspec_hosts.toml", [{"name": "web1"}])

    # First pull settles the file into its merged form (already matches here).
    _SyncBridge().sync_pull()
    for b in tmp_path.glob("runspec_hosts.toml.bak-*"):
        b.unlink()

    # Second, identical pull: the merged result equals what's on disk → no backup.
    res = _SyncBridge().sync_pull()

    assert res["ok"] is True
    assert list(tmp_path.glob("runspec_hosts.toml.bak-*")) == []
    assert res["backups"] == []


def test_old_backups_pruned_to_keep(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"runspec_hosts.toml": b'[[host]]\nname = "web1"\n'})
    _set_sync(files=["runspec_hosts.toml"], backups_keep=2)
    save_hosts(tmp_path / "runspec_hosts.toml", [{"name": "old"}])
    # Five stale backups already sitting in the config dir (timestamps sort lexically).
    for i in range(1, 6):
        (tmp_path / f"runspec_hosts.toml.bak-20200101-00000{i}").write_text("stale")

    res = _SyncBridge().sync_pull()

    assert res["ok"] is True
    # This pull changed the file, so it adds one fresh backup; with keep=2 only the
    # two most recent survive (the new one + the newest stale), the rest are pruned.
    remaining = sorted(p.name for p in tmp_path.glob("runspec_hosts.toml.bak-*"))
    assert len(remaining) == 2
    assert "runspec_hosts.toml.bak-20200101-000005" in remaining
    assert len(res["pruned"]) == 4


def test_backups_keep_zero_keeps_all(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"runspec_hosts.toml": b'[[host]]\nname = "web1"\n'})
    _set_sync(files=["runspec_hosts.toml"], backups_keep=0)
    save_hosts(tmp_path / "runspec_hosts.toml", [{"name": "old"}])
    for i in range(1, 4):
        (tmp_path / f"runspec_hosts.toml.bak-20200101-00000{i}").write_text("stale")

    res = _SyncBridge().sync_pull()

    assert res["ok"] is True
    # 0 = keep all: the 3 stale + 1 fresh backup all remain, nothing pruned.
    assert len(list(tmp_path.glob("runspec_hosts.toml.bak-*"))) == 4
    assert res["pruned"] == []


def test_credentials_merged_by_id_local_only_survives(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {
            "runspec_credentials.toml": b'[[credential]]\nid = "shared"\nlabel = "Shared (from repo)"\n'
            b'\n[[credential]]\nid = "from-repo"\nlabel = "New"\n',
        },
    )
    _set_sync(files=["runspec_credentials.toml"])
    save_credentials(
        cfg.credentials_path(),
        [
            {"id": "shared", "label": "Shared (local)"},
            {"id": "config-repo", "label": "Git auth (local only)"},
        ],
    )

    res = _SyncBridge().sync_pull()
    assert res["ok"] is True

    merged = {c["id"]: c.get("label") for c in load_credentials(cfg.credentials_path())}
    assert merged["shared"] == "Shared (from repo)"  # repo wins per id
    assert merged["from-repo"] == "New"  # new entry added
    assert merged["config-repo"] == "Git auth (local only)"  # local-only survives


def test_pull_without_url_errors(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"config.toml": b"a = 1\n"})
    res = _SyncBridge().sync_pull()
    assert res["ok"] is False
    assert "URL" in res["error"]


# ── layered pull (shared base + team overlay) ────────────────────────────────────


def test_layers_config_folds_base_then_team(monkeypatch, tmp_path):
    _wire_layers(
        monkeypatch,
        tmp_path,
        {
            "_base": {
                "config.toml": b'[llm]\nprovider = "anthropic"\nbase_only = "kept"\n'
            },
            "teams/x": {"config.toml": b'[llm]\nprovider = "openai"\n'},
        },
    )
    cfg.write_config(
        {
            "llm": {"provider": "local", "api_key": "sk-local"},
            "mailbox": {"folder": "Tickets/Mine"},
        }
    )
    _set_sync(files=["config.toml"], layers=["_base", "teams/x"])

    assert _SyncBridge().sync_pull()["ok"] is True

    back = cfg.read_config()
    assert back["llm"]["provider"] == "openai"  # team layer wins
    assert back["llm"]["base_only"] == "kept"  # base-only key survives the fold
    assert back["llm"]["api_key"] == "sk-local"  # local-only key survives
    assert back["mailbox"]["folder"] == "Tickets/Mine"  # default-preserved


def test_layers_hosts_fold_by_identity(monkeypatch, tmp_path):
    _wire_layers(
        monkeypatch,
        tmp_path,
        {
            "_base": {
                "runspec_hosts.toml": b'[[host]]\nname = "web1"\nssh = "base@web1"\n'
                b'\n[[host]]\nname = "dbbase"\nssh = "base@db"\n'
            },
            "teams/x": {
                "runspec_hosts.toml": b'[[host]]\nname = "web1"\nssh = "team@web1"\n'
                b'\n[[host]]\nname = "web2"\nssh = "team@web2"\n'
            },
        },
    )
    save_hosts(
        tmp_path / "runspec_hosts.toml",
        [
            {"name": "web1", "ssh": "old@web1", "room_user": "alice"},
            {"name": "my-laptop", "ssh": "me@laptop"},
        ],
    )
    _set_sync(files=["runspec_hosts.toml"], layers=["_base", "teams/x"])

    assert _SyncBridge().sync_pull()["ok"] is True

    hosts = {h["name"]: h for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts["web1"]["ssh"] == "team@web1"  # team beats base beats local
    assert hosts["web1"]["room_user"] == "alice"  # preserved on the matched entry
    assert hosts["dbbase"]["ssh"] == "base@db"  # base-only entry kept
    assert hosts["web2"]["ssh"] == "team@web2"  # team-only entry added
    assert "my-laptop" in hosts  # local-only entry kept


def test_layers_broken_toml_in_any_layer_aborts(monkeypatch, tmp_path):
    _wire_layers(
        monkeypatch,
        tmp_path,
        {
            "_base": {"runspec_hosts.toml": b'[[host]]\nname = "web1"\n'},
            "teams/x": {"runspec_hosts.toml": b"a = = broken"},
        },
    )
    _set_sync(files=["runspec_hosts.toml"], layers=["_base", "teams/x"])

    res = _SyncBridge().sync_pull()
    assert res["ok"] is False
    assert "not valid TOML" in res["error"]
    assert not (tmp_path / "runspec_hosts.toml").exists()  # nothing written


# ── _sync_auth: SSH-key credentials ──────────────────────────────────────────────


class _AuthBridge:
    """Borrows the real _sync_auth + _credential_entry over a fixed config."""

    _sync_auth = Bridge._sync_auth
    _credential_entry = Bridge._credential_entry

    def __init__(self, creds, config):
        self._credentials = creds
        self._config = config

    def get_config(self):
        return self._config


def test_sync_auth_ssh_uses_credential_key_and_passphrase(monkeypatch, tmp_path):
    import runspec_console.credentials as cred_mod

    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")

    class _FakeKeyring:
        store = {("runspec-console", "deploy"): "pass1"}

        def get_password(self, service, name):
            return self.store.get((service, name))

    monkeypatch.setattr(cred_mod, "_keyring", lambda: _FakeKeyring())

    b = _AuthBridge(
        creds=[
            {
                "id": "deploy",
                "kind": "ssh-key",
                "username": "git",
                "private_key": "/keys/runspec_ed25519",
            }
        ],
        config={"ssh": {"identityFile": "/console/default_key"}},
    )
    auth = b._sync_auth("git@github.com:acme/cfg.git", "deploy")
    assert auth.scheme == "ssh"
    assert auth.username == "git"
    # the credential's own key + its keyring passphrase win over the console key
    assert auth.key_path == "/keys/runspec_ed25519"
    assert auth.passphrase == "pass1"


def test_sync_auth_ssh_falls_back_to_console_key(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    # No credential selected → use the console's configured identityFile, default user.
    b = _AuthBridge(creds=[], config={"ssh": {"identityFile": "/console/default_key"}})
    auth = b._sync_auth("git@github.com:acme/cfg.git", "")
    assert auth.key_path == "/console/default_key"
    assert auth.username == "git"
    assert auth.passphrase == ""
