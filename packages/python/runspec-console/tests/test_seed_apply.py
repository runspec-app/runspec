"""
test_seed_apply.py — the package config-seed apply orchestration
(Bridge.seed_apply), built on the same _apply_config_layers the git pull uses.

Covers: first apply merges + stamps; an unchanged stamp is a no-op (no writes,
no backups); a bumped version re-asserts; the air-gapped path (no [config_sync])
still produces a baseline; a seed entry re-asserts over a local edit unless
preserved; a seed that carries [config_sync] keeps the machine-local last_*
run-state; malformed bundled TOML aborts the whole apply.

configseed.discover is mocked — canned bytes + stamp — so no real distribution
or entry point is needed.
"""

from __future__ import annotations

import tomllib

import runspec_console.config as cfg
import runspec_console.configseed as configseed
from runspec_console.bridge import Bridge
from runspec_console.credentials import load_credentials, save_credentials
from runspec_console.hosts import load_hosts, save_hosts


class _SeedBridge:
    """Borrows the real seed/apply machinery; stubs the app-lifecycle reactions."""

    seed_apply = Bridge.seed_apply
    _run_seed_apply = Bridge._run_seed_apply
    seed_status = Bridge.seed_status
    _seed_overrides = Bridge._seed_overrides
    _seed_declared_preserve = Bridge._seed_declared_preserve
    _local_native_table = Bridge._local_native_table
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

    def _dispatch(self, event, detail):
        pass

    def _reload_hosts(self):
        self.reloaded.append("hosts")

    def _reload_filters(self):
        self.reloaded.append("filters")

    def _reload_credentials(self):
        self.reloaded.append("credentials")

    def _after_config_change(self, old, new):
        self.reloaded.append("config")

    def restart_triggers(self):
        self.triggers_restarted = True


def _wire(monkeypatch, tmp_path, fetched_layers, stamp):
    """Point all config paths at tmp_path and mock configseed.discover."""
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "runspec_console.config.credentials_path",
        lambda: tmp_path / "runspec_credentials.toml",
    )
    monkeypatch.setattr(configseed, "discover", lambda: (fetched_layers, stamp))


def _read_toml(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# ── apply / idempotency ──────────────────────────────────────────────────────────


def test_no_seed_bundled_is_a_clean_noop(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, {}, {})
    res = _SeedBridge().seed_apply()
    assert res == {"ok": True, "applied": False, "reason": "no seed bundled"}


def test_first_apply_merges_and_stamps(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {
            "config.toml": [b'[llm]\nprovider = "mycorp"\n'],
            "runspec_hosts.toml": [b'[[host]]\nname = "web1"\n'],
        },
        {"mycorp-console": "1.4.0"},
    )
    res = _SeedBridge().seed_apply()

    assert res["ok"] is True and res["applied"] is True
    assert set(res["written"]) == {"config.toml", "runspec_hosts.toml"}
    assert cfg.read_config()["llm"]["provider"] == "mycorp"
    hosts = {h["name"] for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts == {"web1"}
    # stamp recorded → next boot is a no-op
    assert cfg.read_seed_state()["applied"] == {"mycorp-console": "1.4.0"}


def test_unchanged_stamp_is_noop_no_backups(monkeypatch, tmp_path):
    seed = {"config.toml": [b'[llm]\nprovider = "mycorp"\n']}
    _wire(monkeypatch, tmp_path, seed, {"mycorp-console": "1.4.0"})
    _SeedBridge().seed_apply()  # first apply
    backups_before = list(tmp_path.glob("*.bak-*"))

    res = _SeedBridge().seed_apply()  # second apply, same stamp
    assert res["applied"] is False and res["reason"] == "unchanged"
    # nothing new written
    assert list(tmp_path.glob("*.bak-*")) == backups_before


def test_bumped_version_reapplies(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\nprovider = "mycorp"\nmodel = "a"\n']},
        {"mycorp-console": "1.4.0"},
    )
    _SeedBridge().seed_apply()

    # ship a new version with a changed value
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\nprovider = "mycorp"\nmodel = "b"\n']},
        {"mycorp-console": "1.5.0"},
    )
    res = _SeedBridge().seed_apply()
    assert res["applied"] is True
    assert cfg.read_config()["llm"]["model"] == "b"
    assert cfg.read_seed_state()["applied"] == {"mycorp-console": "1.5.0"}


def test_air_gapped_baseline_without_config_sync(monkeypatch, tmp_path):
    # A seed with no [config_sync] still configures a working baseline.
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\nprovider = "mycorp"\n[console]\ndocs_url = "x"\n']},
        {"mycorp-console": "1.0.0"},
    )
    res = _SeedBridge().seed_apply()
    assert res["applied"] is True
    cfg_data = cfg.read_config()
    assert cfg_data["llm"]["provider"] == "mycorp"
    assert cfg_data["console"]["docs_url"] == "x"
    assert "config_sync" not in cfg_data


# ── re-assert semantics ──────────────────────────────────────────────────────────


def test_reasserts_over_local_edit(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"runspec_hosts.toml": [b'[[host]]\nname = "web1"\nrole = "seed"\n']},
        {"mycorp-console": "1.0.0"},
    )
    _SeedBridge().seed_apply()

    # operator edits the seed-shipped host in place
    save_hosts(tmp_path / "runspec_hosts.toml", [{"name": "web1", "role": "local"}])

    # re-assert (force) restores the seed value — no preserve for `role`
    res = _SeedBridge().seed_apply(force=True)
    assert res["applied"] is True
    hosts = {h["name"]: h for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts["web1"]["role"] == "seed"


def test_preserve_keeps_local_edit_over_reassert(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"runspec_hosts.toml": [b'[[host]]\nname = "web1"\nrole = "seed"\n']},
        {"mycorp-console": "1.0.0"},
    )
    _SeedBridge().seed_apply()

    # the operator pins the field locally and edits it
    cfg.write_config({"config_sync": {"preserve": ["runspec_hosts.toml:*.role"]}})
    save_hosts(tmp_path / "runspec_hosts.toml", [{"name": "web1", "role": "local"}])

    res = _SeedBridge().seed_apply(force=True)
    assert res["applied"] is True
    hosts = {h["name"]: h for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts["web1"]["role"] == "local"  # preserved


def test_seed_declared_preserve_protects_on_first_apply(monkeypatch, tmp_path):
    # The seed itself declares [config_sync].preserve; the operator has NOT added
    # it to local config. A seed-shipped credential omits the username, which the
    # operator set locally. Because the seed declares the preserve, the local
    # username survives the very re-assert that ships it — no per-machine setup.
    seed_config = b'[config_sync]\npreserve = ["runspec_credentials.toml:*.username"]\n'
    seed_creds = b'[[credential]]\nid = "jira"\nlabel = "Jira"\nkind = "userpass"\n'
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [seed_config], "runspec_credentials.toml": [seed_creds]},
        {"mycorp-console": "1.0.0"},
    )
    save_credentials(
        tmp_path / "runspec_credentials.toml",
        [{"id": "jira", "label": "Jira", "kind": "userpass", "username": "real-user"}],
    )

    res = _SeedBridge().seed_apply(force=True)
    assert res["applied"] is True
    creds = {
        c["id"]: c.get("username")
        for c in load_credentials(tmp_path / "runspec_credentials.toml")
    }
    assert creds["jira"] == "real-user"  # preserved by the seed's own declaration


def test_seed_declared_preserve_empty_without_config(monkeypatch, tmp_path):
    _wire(
        monkeypatch, tmp_path, {"runspec_hosts.toml": [b'[[host]]\nname = "a"\n']}, {}
    )
    assert (
        _SeedBridge()._seed_declared_preserve(
            {"runspec_hosts.toml": [b'[[host]]\nname = "a"\n']}
        )
        == []
    )


def test_seed_config_sync_keeps_local_last_state(monkeypatch, tmp_path):
    # the seed bootstraps a new pointer (no last_* — that's machine-local)
    _wire(
        monkeypatch,
        tmp_path,
        {
            "config.toml": [
                b'[config_sync]\nurl = "https://new.example/cfg.git"\nref = "v2"\n'
            ]
        },
        {"mycorp-console": "2.0.0"},
    )
    # local already has a git-pull run-state stamped (written under tmp_path)
    cfg.write_config(
        {
            "config_sync": {
                "url": "https://old.example/cfg.git",
                "ref": "v1",
                "last_ref": "v1",
                "last_commit": "deadbeef",
                "last_synced": "2026-01-01T00:00:00Z",
            }
        }
    )
    res = _SeedBridge().seed_apply()
    assert res["applied"] is True
    sync = cfg.read_config()["config_sync"]
    assert sync["url"] == "https://new.example/cfg.git"  # pointer updated
    assert sync["ref"] == "v2"
    assert sync["last_ref"] == "v1"  # machine-local run-state preserved
    assert sync["last_commit"] == "deadbeef"


# ── seed_status overrides (what a re-assert would silently revert) ────────────────


def test_seed_status_reports_local_overrides(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\nprovider = "mycorp"\napi_key_command = "get-key"\n']},
        {"mycorp-console": "1.0.0"},
    )
    # the operator set a different api_key_command locally (venv-specific path)
    cfg.write_config(
        {"llm": {"provider": "mycorp", "api_key_command": "C:/venv/Scripts/gk"}}
    )
    st = _SeedBridge().seed_status()

    over = {r["path"]: r for r in st["overrides"]}
    assert "llm.api_key_command" in over  # differs → surfaced
    assert "llm.provider" not in over  # matches → not surfaced
    row = over["llm.api_key_command"]
    assert row["file"] == "config.toml"
    assert row["seed"] == "get-key"
    assert row["local"] == "C:/venv/Scripts/gk"
    assert row["preserved"] is False


def test_seed_status_flags_preserved_override(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\napi_key_command = "get-key"\n']},
        {"mycorp-console": "1.0.0"},
    )
    cfg.write_config(
        {
            "llm": {"api_key_command": "mine"},
            "config_sync": {"preserve": ["config.toml:llm.api_key_command"]},
        }
    )
    st = _SeedBridge().seed_status()
    row = next(r for r in st["overrides"] if r["path"] == "llm.api_key_command")
    assert row["preserved"] is True  # a re-assert would NOT override it


def test_seed_status_no_overrides_when_local_matches(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\nprovider = "mycorp"\n']},
        {"mycorp-console": "1.0.0"},
    )
    cfg.write_config({"llm": {"provider": "mycorp"}})
    assert _SeedBridge().seed_status()["overrides"] == []


def test_malformed_seed_aborts(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b"this is = = not valid toml"]},
        {"mycorp-console": "1.0.0"},
    )
    cfg.write_config({"llm": {"provider": "existing"}})
    res = _SeedBridge().seed_apply()
    assert res["ok"] is False
    assert "not valid TOML" in res["error"]
    # nothing was written / stamped
    assert cfg.read_config()["llm"]["provider"] == "existing"
    assert cfg.read_seed_state() == {}


def test_malformed_seed_error_surfaces_in_status(monkeypatch, tmp_path):
    # A single malformed bundled file aborts the whole seed — that failure must
    # be visible (Settings → Config Sync panel reads seed_status), not silent.
    _wire(
        monkeypatch,
        tmp_path,
        {"runspec_repos.toml": [b'[[repo]]not-a-newline\nurl = ""\n']},
        {"jason-console": "1.0.0"},
    )
    b = _SeedBridge()
    b.seed_apply()
    st = b.seed_status()
    assert "not valid TOML" in st["error"]
    assert st["available"] is True  # discover found the file; it just won't parse


def test_seed_status_error_clears_after_a_clean_apply(monkeypatch, tmp_path):
    b = _SeedBridge()
    # First: a malformed seed records an error.
    _wire(monkeypatch, tmp_path, {"config.toml": [b"= bad"]}, {"jason-console": "1"})
    b.seed_apply()
    assert b.seed_status()["error"]
    # Then a good seed applies cleanly and the error clears.
    _wire(
        monkeypatch,
        tmp_path,
        {"config.toml": [b'[llm]\nprovider = "mycorp"\n']},
        {"jason-console": "2"},
    )
    b.seed_apply()
    assert b.seed_status()["error"] == ""
