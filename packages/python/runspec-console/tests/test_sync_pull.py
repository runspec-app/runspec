"""
test_sync_pull.py — the Config Sync pull orchestration (Bridge.sync_pull) and the
[config_sync] config helpers.

Covers: read_sync/write_sync round-trips and preserve other sections; a pull
re-injects the local [config_sync] and stamps last_* across a config.toml
overwrite; invalid TOML in a fetched file aborts the whole pull (nothing
written); existing files are backed up; runspec_credentials.toml is merged by id
(a local-only entry survives); and the app hot-reloads after a successful pull.

The git layer is mocked — gitsync.fetch_files is replaced with canned bytes — so
these tests need no network or git.
"""

from __future__ import annotations

import runspec_console.bridge as bridge_mod
import runspec_console.config as cfg
import runspec_console.gitsync as gitsync
from runspec_console.bridge import Bridge
from runspec_console.credentials import load_credentials, save_credentials


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
        }
    )
    got = cfg.read_sync()
    assert got["url"] == "https://example.com/cfg.git"
    assert got["ref"] == "v1.0.0"
    assert got["files"] == ["config.toml", "runspec_hosts.toml"]


def test_write_sync_preserves_other_sections(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config(
        {"ssh": {"identityFile": "C:/keys/id"}, "llm": {"provider": "anthropic"}}
    )
    cfg.write_sync({"url": "https://example.com/cfg.git", "ref": "v1"})
    back = cfg.read_config()
    assert back["ssh"]["identityFile"] == "C:/keys/id"
    assert back["llm"]["provider"] == "anthropic"
    assert back["config_sync"]["url"] == "https://example.com/cfg.git"


# ── sync_pull ────────────────────────────────────────────────────────────────────


class _SyncBridge:
    """Borrows the real pull machinery; stubs the app-lifecycle reactions."""

    sync_pull = Bridge.sync_pull
    _atomic_write_bytes = Bridge._atomic_write_bytes
    _merge_synced_credentials = Bridge._merge_synced_credentials

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
    """Point all paths at tmp_path and make gitsync.fetch_files return `files`."""
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(bridge_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        bridge_mod, "credentials_path", lambda: tmp_path / "runspec_credentials.toml"
    )
    monkeypatch.setattr(
        gitsync,
        "fetch_files",
        lambda url, ref, names, auth, subpath="": gitsync.FetchResult(
            ref=ref, commit=commit, files=files
        ),
    )


def _set_sync(files=None):
    cfg.write_sync(
        {
            "url": "https://example.com/cfg.git",
            "ref": "v1.0.0",
            "credential_id": "config-repo",
            "files": files or ["config.toml", "runspec_hosts.toml"],
        }
    )


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
    # the repo's config landed…
    back = cfg.read_config()
    assert back["llm"]["provider"] == "anthropic"
    assert (tmp_path / "runspec_hosts.toml").read_text() == '[[host]]\nname = "web1"\n'
    # …and the local [config_sync] survived the overwrite, now stamped with last_*
    assert back["config_sync"]["url"] == "https://example.com/cfg.git"
    assert back["config_sync"]["last_ref"] == "v1.0.0"
    assert back["config_sync"]["last_commit"] == "a1b2c3d4" * 5
    assert back["config_sync"]["last_synced"]


def test_pull_hot_reloads_and_restarts_triggers(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"config.toml": b"a = 1\n"})
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
    # nothing written — the good file must not have landed either
    assert not (tmp_path / "runspec_hosts.toml").exists()


def test_existing_file_is_backed_up(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {"runspec_hosts.toml": b'[[host]]\nname = "new"\n'})
    _set_sync(files=["runspec_hosts.toml"])
    (tmp_path / "runspec_hosts.toml").write_text('[[host]]\nname = "old"\n')

    res = _SyncBridge().sync_pull()

    assert res["ok"] is True
    backups = list(tmp_path.glob("runspec_hosts.toml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == '[[host]]\nname = "old"\n'
    assert (tmp_path / "runspec_hosts.toml").read_text() == '[[host]]\nname = "new"\n'


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
    # local store: a shared id + a local-only id (e.g. the git-auth credential)
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
    # no [config_sync] written at all
    res = _SyncBridge().sync_pull()
    assert res["ok"] is False
    assert "URL" in res["error"]
