"""
test_savedtransfers.py — the saved-transfer registry and its pure helpers
(parse / templates + dates / wildcard expansion / selection / dest names).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from runspec_console import configmerge, gitsync
from runspec_console import savedtransfers as st

NOW = datetime(2026, 10, 29, 14, 30, 5)


def _entry(**kw):
    data = {"id": "logs", "host": "web-01", "paths": ["/var/log/app.log"]}
    data.update(kw)
    return st.parse_saved_transfer(data)


# ── parse ──────────────────────────────────────────────────────────────────────


def test_parse_minimal_defaults():
    e = _entry()
    assert e.host == "web-01"
    assert e.max_files == st.DEFAULT_MAX_FILES
    assert e.autonomy == "confirm"
    assert e.source_summary() == "web-01"
    assert e.dest_summary() == "chat workspace"


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"id": "", "host": "h", "paths": ["/x"]}, "missing an 'id'"),
        ({"id": "a", "paths": ["/x"]}, "exactly one source"),
        ({"id": "a", "host": "h", "group": "g", "paths": ["/x"]}, "exactly one"),
        ({"id": "a", "host": "local", "paths": ["/x"]}, "needs a remote destination"),
        (
            {"id": "a", "host": "h", "to_host": "g", "to_group": "w", "paths": ["/x"]},
            "more than one destination",
        ),
        (
            {"id": "a", "host": "h", "to_host": "local", "paths": ["/x"]},
            "onto this machine",
        ),
        (
            {
                "id": "a",
                "group": "w",
                "to_host": "g",
                "paths": ["/x"],
                "dest_dir": "/i",
            },
            "group",
        ),
        ({"id": "a", "host": "local", "to_host": "g", "paths": ["x"]}, "dest_dir"),
        (
            {
                "id": "a",
                "host": "local",
                "to_host": "g",
                "to_jump": "j",
                "paths": ["x"],
            },
            "to_adhoc",
        ),
        ({"id": "a", "host": "h", "paths": []}, "no 'paths'"),
        ({"id": "a", "host": "h", "jump": "j", "paths": ["/x"]}, "only apply"),
        ({"id": "a", "host": "h", "paths": ["/x"], "autonomy": "yolo"}, "autonomy"),
        ({"id": "a", "host": "h", "paths": ["/{{nope}}"]}, "no param named 'nope'"),
        ({"id": "a", "host": "h", "paths": ["/x"], "subdir": "/abs"}, "relative"),
        ({"id": "a", "host": "h", "paths": ["/x"], "max_files": 5000}, "at most"),
        ({"id": "a", "host": "h", "paths": ["/x"], "latest": -1}, "negative"),
    ],
)
def test_parse_rejects(data, msg):
    with pytest.raises(ValueError, match=msg):
        st.parse_saved_transfer(data)


def test_parse_adhoc_and_params():
    e = st.parse_saved_transfer(
        {
            "id": "db",
            "adhoc": "db7.corp",
            "jump": "bastion",
            "user": "svc",
            "credential_id": "db7",
            "paths": ["/var/log/{{svc}}-{{day:%Y%m%d}}.log"],
            "params": {"svc": {"required": True}, "day": {"default": "today"}},
        }
    )
    assert e.source_summary() == "db7.corp (ad-hoc via bastion)"
    row = st.entry_to_row(e)
    assert row["adhoc"] == "db7.corp" and row["credential_id"] == "db7"
    assert "max_files" not in row and "autonomy" not in row
    assert st.parse_saved_transfer(row) == e


# ── values + templates ────────────────────────────────────────────────────────


def test_resolve_values_defaults_and_validation():
    e = _entry(
        paths=["/l/{{svc}}-{{day}}"],
        params={"svc": {"options": ["api", "web"]}, "day": {"default": "yesterday"}},
    )
    assert st.resolve_values(e, {"svc": "api"}) == {"svc": "api", "day": "yesterday"}
    with pytest.raises(ValueError):
        st.resolve_values(e, {"svc": "db"})
    with pytest.raises(ValueError, match="no param"):
        st.resolve_values(e, {"svc": "api", "other": 1})


@pytest.mark.parametrize(
    "value, expected",
    [
        ("today", "2026-10-29"),
        ("yesterday", "2026-10-28"),
        ("-3d", "2026-10-26"),
        ("+1d", "2026-10-30"),
        ("-1w", "2026-10-22"),
        ("2026-01-05", "2026-01-05"),
        ("2026-01-05T23:10:00Z", "2026-01-05"),
    ],
)
def test_parse_when(value, expected):
    assert st.parse_when(value, NOW).strftime("%Y-%m-%d") == expected


def test_parse_when_rejects_junk():
    with pytest.raises(ValueError, match="isn't a date"):
        st.parse_when("someday", NOW)


def test_render_params_builtins_and_formats():
    vals = {"day": "-1d", "svc": "api"}
    assert (
        st.render("/l/{{svc}}/app-{{day:%Y%m%d}}*.log", vals, now=NOW)
        == "/l/api/app-20261028*.log"
    )
    assert st.render("{{today}}_{{host}}", {}, host="web-01", now=NOW) == (
        "2026-10-29_web-01"
    )
    assert st.render("{{yesterday:%d}}", {}, now=NOW) == "28"
    with pytest.raises(ValueError, match="svc: 'api' isn't a date"):
        st.render("{{svc:%Y}}", vals, now=NOW)


def test_render_escapes_param_values_in_globs():
    out = st.render("/l/{{name}}*.log", {"name": "a*b[1]"}, escape_glob=True)
    assert out == "/l/a[*]b[[]1]*.log"


def test_safe_subdir():
    assert st.safe_subdir("logs//2026/") == "logs/2026"
    for bad in ("../up", "a/../../b", "/abs", "C:\\x"):
        with pytest.raises(ValueError):
            st.safe_subdir(bad)


# ── wildcard expansion ────────────────────────────────────────────────────────

TREE = {
    "/var/log/app": [
        ("app-20261028.log", False, 10, 100),
        ("app-20261028.1.log", False, 20, 200),
        ("app-20261029.log", False, 30, 300),
        (".hidden.log", False, 1, 1),
        ("archive", True, 0, 0),
    ],
    "/var/log/app/archive": [("app-20261001.log", False, 5, 5)],
    "/srv": [("svc1", True, 0, 0), ("svc2", True, 0, 0), ("readme", False, 1, 1)],
    "/srv/svc1": [("out.log", False, 7, 70)],
    "/srv/svc2": [("out.log", False, 8, 80)],
}


def _listdir(d):
    if d not in TREE:
        raise OSError("nope")
    return TREE[d]


def _stat(path):
    parent, name = path.rsplit("/", 1)
    for n, is_dir, size, mtime in TREE.get(parent or "/", []):
        if n == name:
            return is_dir, size, mtime
    return None


def _paths(files):
    return sorted(f.path for f in files)


def test_expand_basename_glob_skips_dirs_and_dotfiles():
    files = st.expand_pattern("/var/log/app/app-20261028*.log", _listdir, _stat)
    assert _paths(files) == [
        "/var/log/app/app-20261028.1.log",
        "/var/log/app/app-20261028.log",
    ]
    assert _paths(st.expand_pattern("/var/log/app/*", _listdir, _stat)) == [
        "/var/log/app/app-20261028.1.log",
        "/var/log/app/app-20261028.log",
        "/var/log/app/app-20261029.log",
    ]
    assert len(st.expand_pattern("/var/log/app/.*", _listdir, _stat)) == 1


def test_expand_directory_glob():
    assert _paths(st.expand_pattern("/srv/*/out.log", _listdir, _stat)) == [
        "/srv/svc1/out.log",
        "/srv/svc2/out.log",
    ]


def test_expand_literal_path_exists_or_not():
    assert _paths(st.expand_pattern("/srv/svc1/out.log", _listdir, _stat)) == [
        "/srv/svc1/out.log"
    ]
    assert st.expand_pattern("/srv/svc1/missing.log", _listdir, _stat) == []
    assert st.expand_pattern("/srv/svc1", _listdir, _stat) == []  # a directory


def test_expand_escaped_value_matches_literally():
    TREE["/odd"] = [("a*b.log", False, 1, 1), ("axb.log", False, 1, 1)]
    try:
        pat = st.render("/odd/{{n}}.log", {"n": "a*b"}, escape_glob=True)
        assert _paths(st.expand_pattern(pat, _listdir, _stat)) == ["/odd/a*b.log"]
    finally:
        del TREE["/odd"]


def test_select_latest_dedupe_and_cap():
    a = st.expand_pattern("/var/log/app/app-*.log", _listdir, _stat)
    b = st.expand_pattern("/var/log/app/app-20261029.log", _listdir, _stat)
    newest = st.select_files([a, b], latest=1, max_files=10)
    assert [f.path for f in newest] == ["/var/log/app/app-20261029.log"]
    every = st.select_files([a, b], latest=0, max_files=10)
    assert len(every) == 3
    with pytest.raises(ValueError, match="more than max_files"):
        st.select_files([a], latest=0, max_files=2, entry_id="logs")


def test_dest_names_flat_unless_basenames_collide():
    flat = st.expand_pattern("/var/log/app/app-*.log", _listdir, _stat)
    assert sorted(st.dest_names(flat)) == [
        "app-20261028.1.log",
        "app-20261028.log",
        "app-20261029.log",
    ]
    clash = st.expand_pattern("/srv/*/out.log", _listdir, _stat)
    assert sorted(st.dest_names(clash)) == ["svc1/out.log", "svc2/out.log"]


def test_local_join_stays_inside(tmp_path):
    assert st.local_join(str(tmp_path), "a/b.log").endswith("b.log")
    with pytest.raises(ValueError):
        st.local_join(str(tmp_path), "../escape.log")


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_round_trip_and_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "runspec_console.config.saved_transfers_path",
        lambda: tmp_path / "runspec_saved_transfers.toml",
    )
    assert st.load_saved_transfers() == []
    st.save_saved_transfers([st.entry_to_row(_entry())])
    assert st.load_saved_transfer("logs")["host"] == "web-01"
    assert st.load_saved_transfer("nope") is None
    assert "runspec_saved_transfers.toml" in gitsync.KNOWN_FILES
    assert configmerge.COLLECTION_FILES["runspec_saved_transfers.toml"] == (
        "saved_transfer",
        "id",
    )


# ── push / relay ───────────────────────────────────────────────────────────────


def test_directions_and_summaries():
    pull = _entry()
    assert pull.direction == "pull"
    push = _entry(host="local", paths=["out/*.csv"], to_group="web", dest_dir="/in")
    assert push.direction == "push"
    assert push.source_summary() == "this computer"
    assert push.target_summary() == "group web"
    assert push.dest_summary() == "group web:/in"
    relay = _entry(to_adhoc="10.0.0.9", to_user="ops", dest_dir="/drop", subdir="x")
    assert relay.direction == "relay"
    assert relay.dest_summary().endswith(":/drop/x")
    row = st.entry_to_row(relay)
    assert row["to_adhoc"] == "10.0.0.9" and row["to_user"] == "ops"
    assert st.parse_saved_transfer(row) == relay


def test_is_absolute():
    assert st.is_absolute("/srv/x")
    assert st.is_absolute("C:/logs/a")
    assert st.is_absolute("C:\\logs\\a")
    assert not st.is_absolute("out/a.csv")
