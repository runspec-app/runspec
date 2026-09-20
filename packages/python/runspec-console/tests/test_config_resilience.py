"""
test_config_resilience.py — the config Python⇄TypeScript boundary, tested for
real (no mocks) against the class of bug that bricked the app.

Background: adding a jump host with an empty "Add path" row wrote invalid TOML
(``runspec_paths = [None]``); ``runspec_hosts.toml`` is read on the launch path
(``Bridge.__init__ -> _reload_hosts``), so the file crashed the app on every
start until it was cleared by hand. ~3000 tests missed it because the frontend
mocked the bridge (the real serializer never ran) and the Python tests never fed
the ``[null]`` shape the editor emits through ``save -> write -> reload``.

The rules this file enforces (see docs/design/config-resilience via the handoff):

1. **Startup resilience.** Every config file read at/near launch must survive a
   corrupt/garbage file — quarantine-and-continue, never raise. A launch-path
   read that raises bricks the app.
2. **Round-trip.** Any config the UI writes must survive the *real*
   serializer/loader for the awkward shapes the TS editor actually emits:
   ``null``/empty elements, quotes, backslashes (Windows paths), unicode.
3. **One audited serializer.** ``tomlsafe.dump_scalar``/``basic_string`` are the
   single escaping implementation; a ``None`` never becomes a Python ``repr``.
"""

from __future__ import annotations

import pytest

from runspec_console import config as config_mod
from runspec_console import credentials as cred_mod
from runspec_console import filters_store, groups, hosts, profiles, repos, self_service
from runspec_console import tomlsafe
from runspec_console.bridge import Bridge

# ── the registry of collection loaders read at/near launch ──────────────────────
# (name, loader, path-helper on config, the array-of-tables key). Every one of
# these is now hardened to quarantine-and-continue rather than raise.
_LOADERS = [
    ("hosts", hosts.load_hosts, config_mod.hosts_path, "host"),
    ("filters", filters_store.load_filters, config_mod.filters_path, "filter"),
    (
        "credentials",
        cred_mod.load_credentials,
        config_mod.credentials_path,
        "credential",
    ),
    ("groups", groups.load_groups, config_mod.groups_path, "group"),
    ("profiles", profiles.load_profiles, config_mod.profiles_path, "profile"),
    ("repos", repos.load_repos, config_mod.repos_path, "repo"),
    (
        "self_service",
        self_service.load_self_service,
        config_mod.self_service_path,
        "self_service",
    ),
]


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Point every ``config.*_path()`` helper at an isolated app dir."""
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


# ── Part B.2 — startup-resilience sweep ─────────────────────────────────────────


@pytest.mark.parametrize(
    "name,loader,path_fn,_key", _LOADERS, ids=[r[0] for r in _LOADERS]
)
def test_missing_file_returns_empty(app, name, loader, path_fn, _key):
    assert loader(path_fn()) == []


@pytest.mark.parametrize(
    "name,loader,path_fn,_key", _LOADERS, ids=[r[0] for r in _LOADERS]
)
def test_corrupt_file_is_quarantined_not_raised(app, name, loader, path_fn, _key):
    path = path_fn()
    # A truncated / hand-broken file — the kind a bad serializer or hand-edit
    # leaves behind. A launch-path read of this must not raise.
    path.write_text("[[oops\nnot = valid toml = = =\n", encoding="utf-8")
    result = loader(path)
    assert result == []  # degrade to no-config, don't crash
    backups = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert len(backups) == 1  # the operator's data was preserved, not deleted
    assert not path.exists()  # moved aside so the next save writes a clean file


@pytest.mark.parametrize(
    "name,loader,path_fn,key", _LOADERS, ids=[r[0] for r in _LOADERS]
)
def test_wrong_shape_section_returns_empty(app, name, loader, path_fn, key):
    # A syntactically valid file whose section is the wrong type (a scalar, not an
    # array of tables) must not throw downstream — treat it as no entries.
    path = path_fn()
    path.write_text(f'{key} = "not a list"\n', encoding="utf-8")
    assert loader(path) == []


def test_bridge_launch_reloads_survive_corrupt_files(app):
    """The three collection files read directly in ``Bridge.__init__`` — hosts,
    filters, credentials — must not crash the launch path when corrupt."""
    for pth in (
        config_mod.hosts_path(),
        config_mod.filters_path(),
        config_mod.credentials_path(),
    ):
        pth.write_text("this is = = not toml\n", encoding="utf-8")

    class _LaunchStub:
        _reload_hosts = Bridge._reload_hosts
        _reload_filters = Bridge._reload_filters
        _reload_credentials = Bridge._reload_credentials

    stub = _LaunchStub()
    stub._reload_hosts()
    stub._reload_filters()
    stub._reload_credentials()
    assert stub._hosts == []
    assert stub._filters == []
    assert stub._credentials == []
    # All three quarantined; app would start with no configured entries.
    for pth in (
        config_mod.hosts_path(),
        config_mod.filters_path(),
        config_mod.credentials_path(),
    ):
        assert not pth.exists()
        assert list(pth.parent.glob(f"{pth.name}.corrupt-*"))


# ── the shared audited serializer (Part B.3) ────────────────────────────────────


def test_dump_scalar_never_emits_python_repr():
    # The exact bug: a None / a list with a blank element must not serialise to a
    # Python repr (`None` / `[None]`) that corrupts the file.
    assert tomlsafe.dump_scalar(None) == '""'
    assert tomlsafe.dump_scalar([None]) == '[""]'
    assert tomlsafe.dump_scalar(["a", None]) == '["a", ""]'


def test_dump_scalar_types():
    assert tomlsafe.dump_scalar(True) == "true"
    assert tomlsafe.dump_scalar(False) == "false"
    assert tomlsafe.dump_scalar(42) == "42"
    assert tomlsafe.dump_scalar(3.5) == "3.5"
    assert tomlsafe.dump_scalar("x") == '"x"'
    assert tomlsafe.dump_scalar(["a", "b"]) == '["a", "b"]'


def test_basic_string_escapes():
    assert tomlsafe.basic_string('a"b') == '"a\\"b"'
    assert tomlsafe.basic_string("C:\\dir") == '"C:\\\\dir"'
    assert tomlsafe.basic_string("line1\nline2") == '"line1\\nline2"'
    assert tomlsafe.basic_string("tab\there") == '"tab\\there"'
    # A raw control char is emitted as \uXXXX so the file always re-parses.
    assert tomlsafe.basic_string("\x01") == '"\\u0001"'
    # Unicode passes through unescaped (valid in a TOML basic string).
    assert tomlsafe.basic_string("café — 世界") == '"café — 世界"'


def test_quarantine_collision_gets_distinct_sidecars(tmp_path):
    path = tmp_path / "x.toml"
    path.write_text("bad", encoding="utf-8")
    tomlsafe.quarantine_corrupt(path, ValueError("1"))
    path.write_text("bad again", encoding="utf-8")
    tomlsafe.quarantine_corrupt(path, ValueError("2"))
    assert len(list(tmp_path.glob("x.toml.corrupt-*"))) == 2


def test_quarantine_never_raises_when_unmovable(tmp_path):
    # A path that cannot be moved aside (it does not exist) must be swallowed.
    tomlsafe.quarantine_corrupt(tmp_path / "gone.toml", ValueError("boom"))


# ── Part B.1 — round-trip the awkward shapes through the real writers ───────────

_AWKWARD = 'quote " backslash \\ path C:\\Users\\x unicode café 世界'


def test_filters_round_trip_awkward(app):
    path = config_mod.filters_path()
    entry = {"name": "f1", "pipeline": _AWKWARD, "description": _AWKWARD}
    filters_store.save_filters(path, [entry])
    assert filters_store.load_filters(path) == [entry]


def test_credentials_round_trip_awkward(app):
    path = config_mod.credentials_path()
    entry = {
        "id": "c1",
        "label": _AWKWARD,
        "kind": "userpass",
        "username": _AWKWARD,
        "scope_hosts": ["web-01", _AWKWARD],
    }
    cred_mod.save_credentials(path, [entry])
    assert cred_mod.load_credentials(path) == [entry]


def test_groups_round_trip_awkward(app):
    path = config_mod.groups_path()
    entry = {"name": _AWKWARD, "description": _AWKWARD}
    groups.save_groups(path, [entry])
    assert groups.load_groups(path) == groups.normalise_groups([entry])


def test_profiles_round_trip_awkward(app):
    path = config_mod.profiles_path()
    entry = {"name": _AWKWARD, "description": _AWKWARD}
    profiles.save_profiles(path, [entry])
    assert profiles.load_profiles(path) == profiles.normalise_profiles([entry])


def test_self_service_round_trip_awkward(app):
    path = config_mod.self_service_path()
    entry = {"name": _AWKWARD, "description": _AWKWARD}
    self_service.save_self_service(path, [entry])
    assert self_service.load_self_service(path) == self_service.normalise_self_service(
        [entry]
    )


def test_repos_round_trip_awkward(app):
    path = config_mod.repos_path()
    entry = {
        "name": "r1",
        "url": "https://example.com/x.git",
        "ref": "main",
        "credential_id": "c1",
        "description": _AWKWARD,
    }
    repos.save_repos(path, [entry])
    assert repos.load_repos(path) == repos.normalise_repos([entry])


def test_hosts_round_trip_null_path_row(app):
    # The exact incident shape: a null path element must never become `[None]`.
    path = config_mod.hosts_path()
    hosts.save_hosts(path, [{"name": "h1", "runspec_paths": [None]}])
    # File re-parses (no crash) and the null was written as an empty string, not
    # a Python repr that would wedge every later read.
    loaded = hosts.load_hosts(path)
    assert loaded == [{"name": "h1", "runspec_paths": [""]}]
