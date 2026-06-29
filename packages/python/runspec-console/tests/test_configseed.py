"""test_configseed.py — discovery of package-bundled config seed.

``configseed.discover()`` reads ``runspec_console.config_seed`` entry points and
collects each contributor's ``config_seed/`` data dir into the
``({name: [bytes]}, {dist: version})`` shape ``_apply_config_layers`` consumes.
We fake the entry points and the resource dirs so no real distribution is needed.
"""

from __future__ import annotations

from dataclasses import dataclass

import runspec_console.configseed as configseed


@dataclass
class _FakeDist:
    name: str
    version: str


@dataclass
class _FakeEP:
    module: str
    dist: _FakeDist


class _FakeTraversable:
    """Just enough of importlib.resources' Traversable for discover()."""

    def __init__(self, name: str, files: dict[str, bytes] | None):
        self._name = name
        self._files = files  # None ⇒ this path is not a directory

    @property
    def name(self) -> str:
        return self._name

    def __truediv__(self, child: str) -> "_FakeTraversable":
        # configseed does files(module) / "config_seed"
        return _FakeTraversable(child, self._files)

    def is_dir(self) -> bool:
        return self._files is not None

    def is_file(self) -> bool:
        return self._files is None

    def iterdir(self):
        for fname, data in (self._files or {}).items():
            yield _FakeFile(fname, data)


class _FakeFile:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def is_file(self) -> bool:
        return True

    def read_bytes(self) -> bytes:
        return self._data


def _wire(monkeypatch, eps, seed_dirs: dict[str, dict[str, bytes] | None]):
    """eps: list of _FakeEP. seed_dirs: module -> its config_seed/ files (or None
    to model a module with no config_seed dir)."""
    monkeypatch.setattr(
        configseed,
        "discover",
        configseed.discover,  # ensure we test the real function
    )
    import importlib.metadata as meta
    import importlib.resources as resources

    monkeypatch.setattr(
        meta,
        "entry_points",
        lambda group=None: eps if group == configseed.ENTRY_POINT_GROUP else [],
    )
    monkeypatch.setattr(
        resources,
        "files",
        lambda module: _FakeTraversable(module, seed_dirs.get(module)),
    )


def test_no_entry_points_is_empty(monkeypatch):
    _wire(monkeypatch, [], {})
    fetched, stamp = configseed.discover()
    assert fetched == {}
    assert stamp == {}


def test_single_contributor_collects_known_files_and_stamps(monkeypatch):
    eps = [_FakeEP("mycorp_console", _FakeDist("mycorp-console", "1.4.0"))]
    _wire(
        monkeypatch,
        eps,
        {
            "mycorp_console": {
                "config.toml": b'[llm]\nprovider = "mycorp"\n',
                "runspec_hosts.toml": b'[[host]]\nname = "web1"\n',
                "README.md": b"not a known file",  # ignored (not in KNOWN_FILES)
            }
        },
    )
    fetched, stamp = configseed.discover()
    assert set(fetched) == {"config.toml", "runspec_hosts.toml"}
    assert fetched["config.toml"] == [b'[llm]\nprovider = "mycorp"\n']
    assert stamp == {"mycorp-console": "1.4.0"}


def test_module_without_config_seed_dir_is_skipped(monkeypatch):
    eps = [_FakeEP("plainpkg", _FakeDist("plainpkg", "0.1.0"))]
    _wire(monkeypatch, eps, {"plainpkg": None})  # no config_seed/ dir
    fetched, stamp = configseed.discover()
    assert fetched == {}
    assert stamp == {}  # contributed nothing → not stamped


def test_multiple_contributors_fold_low_to_high_by_dist_name(monkeypatch):
    # zeta sorts after alpha → alpha is the low layer, zeta the high layer.
    eps = [
        _FakeEP("zeta_pkg", _FakeDist("zeta", "2.0.0")),
        _FakeEP("alpha_pkg", _FakeDist("alpha", "1.0.0")),
    ]
    _wire(
        monkeypatch,
        eps,
        {
            "alpha_pkg": {"config.toml": b"[a]\nx = 1\n"},
            "zeta_pkg": {"config.toml": b"[z]\ny = 2\n"},
        },
    )
    fetched, stamp = configseed.discover()
    # low→high: alpha first, zeta second
    assert fetched["config.toml"] == [b"[a]\nx = 1\n", b"[z]\ny = 2\n"]
    assert stamp == {"alpha": "1.0.0", "zeta": "2.0.0"}


def test_unreadable_contributor_is_skipped_not_fatal(monkeypatch):
    eps = [
        _FakeEP("good_pkg", _FakeDist("good", "1.0.0")),
        _FakeEP("bad_pkg", _FakeDist("bad", "1.0.0")),
    ]
    import importlib.metadata as meta
    import importlib.resources as resources

    monkeypatch.setattr(
        meta,
        "entry_points",
        lambda group=None: eps if group == configseed.ENTRY_POINT_GROUP else [],
    )

    def _files(module):
        if module == "bad_pkg":
            raise RuntimeError("resource boom")
        return _FakeTraversable(module, {"config.toml": b"[g]\nok = true\n"})

    monkeypatch.setattr(resources, "files", _files)

    fetched, stamp = configseed.discover()
    assert fetched == {"config.toml": [b"[g]\nok = true\n"]}
    assert stamp == {"good": "1.0.0"}  # bad_pkg skipped
