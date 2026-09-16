"""Local venv auto-discovery — the local mirror of a remote host's multi-venv
``runspec_paths`` list.

Covers the pure resolver (:func:`hosts.resolve_local_venvs`), the config reader
(:func:`config.read_local`), and the bridge editor methods
(``get_local_venv_config`` / ``save_local_venv_config``) plus the
``_local_host_entry`` merge that lights up per-venv discovery/execution.
"""

from __future__ import annotations

import os
import threading

import pytest

from runspec_console import config as config_mod
from runspec_console import hosts as hosts_mod
from runspec_console.bridge import Bridge


# ── resolve_local_venvs (pure) ────────────────────────────────────────────────


def _make_venv(root, *, windows: bool) -> str:
    """Create a fake venv under ``root`` and return its runspec binary path.

    Both layouts are creatable on any OS (``.exists()`` is platform-agnostic), so
    the Windows Scripts\\runspec.exe path is exercised on Linux CI too."""
    if windows:
        bindir = root / "Scripts"
        binary = bindir / "runspec.exe"
    else:
        bindir = root / "bin"
        binary = bindir / "runspec"
    bindir.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!stub\n", encoding="utf-8")
    return str(binary)


def test_empty_globs_returns_only_own(tmp_path):
    own = str(tmp_path / "self" / "bin" / "runspec")
    assert hosts_mod.resolve_local_venvs([], own_path=own) == [own]


def test_own_is_always_first_and_present_even_if_missing(tmp_path):
    # own need not exist on disk (matches _local_host_entry's fallback path).
    own = str(tmp_path / "self" / "bin" / "runspec")
    base = tmp_path / "venvs"
    _make_venv(base / "alpha", windows=False)
    out = hosts_mod.resolve_local_venvs([str(base / "*")], own_path=own)
    assert out[0] == own
    assert len(out) == 2


def test_posix_and_windows_layouts_are_discovered(tmp_path):
    base = tmp_path / "venvs"
    posix = _make_venv(base / "alpha", windows=False)
    win = _make_venv(base / "beta", windows=True)
    out = hosts_mod.resolve_local_venvs([str(base / "*")], own_path="")
    assert set(out) == {posix, win}


def test_non_venv_dirs_are_skipped(tmp_path):
    base = tmp_path / "venvs"
    good = _make_venv(base / "real", windows=False)
    (base / "not-a-venv").mkdir(parents=True)  # no bin/Scripts runspec
    (base / "loose-file").write_text("x", encoding="utf-8")  # not a dir
    out = hosts_mod.resolve_local_venvs([str(base / "*")], own_path="")
    assert out == [good]


def test_dedupe_by_realpath_keeps_own_first(tmp_path):
    base = tmp_path / "venvs"
    shared = _make_venv(base / "alpha", windows=False)
    # own points at the same venv a glob also matches → not listed twice.
    out = hosts_mod.resolve_local_venvs([str(base / "*")], own_path=shared)
    assert out == [shared]


def test_venv_name_tags_each_group(tmp_path):
    base = tmp_path / "venvs"
    a = _make_venv(base / "ops-tools", windows=False)
    b = _make_venv(base / "platform", windows=True)
    out = hosts_mod.resolve_local_venvs([str(base / "*")], own_path="")
    names = {hosts_mod.venv_name(p) for p in out}
    assert names == {"ops-tools", "platform"}
    assert a in out and b in out


# ── config.read_local ─────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


def test_read_local_default_empty(app):
    assert config_mod.read_local() == {"venv_globs": []}


def test_read_local_parses_and_cleans(app):
    config_mod.write_config({"local": {"venv_globs": ["C:/venvs/*", "  ", "x"]}})
    assert config_mod.read_local() == {"venv_globs": ["C:/venvs/*", "x"]}


def test_read_local_ignores_non_list(app):
    config_mod.write_config({"local": {"venv_globs": "not-a-list"}})
    assert config_mod.read_local() == {"venv_globs": []}


# ── bridge editor methods + _local_host_entry merge ───────────────────────────


class _StubBridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.dispatched: list[str] = []

    def _kick_refresh(self, *, force: bool = False) -> None:  # spawned by save
        pass

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append(event)

    _local_host_entry = Bridge._local_host_entry
    get_local_venv_config = Bridge.get_local_venv_config
    save_local_venv_config = Bridge.save_local_venv_config


def test_local_host_entry_merges_globbed_venvs(app):
    base = app / "venvs"
    v = _make_venv(base / "ops", windows=False)
    config_mod.write_config({"local": {"venv_globs": [str(base / "*")]}})
    entry = _StubBridge()._local_host_entry()
    assert entry["name"] == "local"
    assert entry["ssh"] is None
    paths = entry["runspec_paths"]
    assert v in paths
    assert len(paths) == 2  # own + the globbed venv


def test_get_local_venv_config_preview(app):
    base = app / "venvs"
    _make_venv(base / "ops", windows=False)
    config_mod.write_config({"local": {"venv_globs": [str(base / "*")]}})
    cfg = _StubBridge().get_local_venv_config()
    assert cfg["globs"] == [str(base / "*")]
    assert cfg["own_venv"] == cfg["resolved"][0]["path"]
    names = {r["venv_name"] for r in cfg["resolved"]}
    assert "ops" in names


def test_save_local_venv_config_round_trip_and_preserves(app):
    # An unrelated section must survive the [local] write.
    config_mod.write_config({"mailbox": {"folder": "Inbox"}})
    b = _StubBridge()
    b.save_local_venv_config(["C:/venvs/*"])
    assert config_mod.read_local() == {"venv_globs": ["C:/venvs/*"]}
    assert config_mod.read_config()["mailbox"] == {"folder": "Inbox"}
    assert "runspec:hosts_updated" in b.dispatched


def test_save_local_venv_config_empty_clears_section(app):
    config_mod.write_config(
        {"local": {"venv_globs": ["x"]}, "mailbox": {"folder": "X"}}
    )
    _StubBridge().save_local_venv_config([])
    cfg = config_mod.read_config()
    assert "local" not in cfg
    assert cfg["mailbox"] == {"folder": "X"}


def test_save_local_venv_config_rejects_bad_input(app):
    b = _StubBridge()
    for bad in ("not-a-list", [1, 2], ["", "  "], [None]):
        with pytest.raises(ValueError):
            b.save_local_venv_config(bad)  # type: ignore[arg-type]


def test_realpath_key_is_used(tmp_path):
    # A relative-vs-absolute spelling of the same file dedupes.
    v = _make_venv(tmp_path / "venvs" / "ops", windows=False)
    rel = os.path.relpath(v)
    out = hosts_mod.resolve_local_venvs([], own_path=v)
    out2 = hosts_mod.resolve_local_venvs([str(tmp_path / "venvs" / "*")], own_path=rel)
    assert out == [v]
    # rel + the glob (which yields the absolute path) resolve to one entry.
    assert len(out2) == 1
