"""Config read/write/round-trip tests."""

from __future__ import annotations

import pytest

from runspec_scheduler.config import (
    ScheduleEntry,
    config_path,
    load_schedules,
    parse_entry,
    read_config,
    remove_schedule,
    upsert_schedule,
    write_schedules,
)
from runspec_scheduler.timing import When


def _entry(**kw):
    kw.setdefault("id", "s1")
    kw.setdefault("when", When(every="15m"))
    kw.setdefault("runnable", "backup")
    return ScheduleEntry(**kw)


def test_round_trip(tmp_path):
    path = tmp_path / "runspec_scheduler.toml"
    entries = [
        _entry(id="a", when=When(cron="0 2 * * *", timezone="UTC"), args={"level": "warn", "n": 3, "flag": True}),
        _entry(id="b", when=When(every="15m"), run_as="svc", become_method="sudo", env_file="/home/svc/x.env"),
    ]
    write_schedules(path, entries)
    loaded = load_schedules(path)
    assert [e.id for e in loaded] == ["a", "b"]
    a = loaded[0]
    assert a.when.cron == "0 2 * * *" and a.when.timezone == "UTC"
    assert a.args == {"level": "warn", "n": 3, "flag": True}
    b = loaded[1]
    assert b.run_as == "svc" and b.env_file == "/home/svc/x.env" and b.when.every == "15m"


def test_upsert_replaces_by_id(tmp_path):
    path = tmp_path / "c.toml"
    upsert_schedule(path, _entry(id="x", runnable="one"))
    upsert_schedule(path, _entry(id="x", runnable="two"))
    loaded = load_schedules(path)
    assert len(loaded) == 1 and loaded[0].runnable == "two"


def test_remove(tmp_path):
    path = tmp_path / "c.toml"
    upsert_schedule(path, _entry(id="x"))
    upsert_schedule(path, _entry(id="y"))
    assert remove_schedule(path, "x") is True
    assert [e.id for e in load_schedules(path)] == ["y"]
    assert remove_schedule(path, "nope") is False


def test_missing_file_is_empty(tmp_path):
    assert load_schedules(tmp_path / "absent.toml") == []
    assert read_config(tmp_path / "absent.toml") == {}


def test_parse_entry_validation():
    with pytest.raises(ValueError):
        parse_entry({"runnable": "x", "when": {"every": "15m"}})  # no id
    with pytest.raises(ValueError):
        parse_entry({"id": "a", "when": {"every": "15m"}})  # no runnable
    with pytest.raises(ValueError):
        parse_entry({"id": "a", "runnable": "x", "when": {}})  # no timing


def test_config_path_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNSPEC_SCHEDULER_CONFIG", raising=False)
    explicit = tmp_path / "explicit.toml"
    assert config_path(str(explicit)) == explicit
    monkeypatch.setenv("RUNSPEC_SCHEDULER_CONFIG", str(tmp_path / "envcfg.toml"))
    assert config_path(None) == tmp_path / "envcfg.toml"
    assert config_path(str(explicit)) == explicit  # explicit wins over env


def test_toml_string_escaping(tmp_path):
    path = tmp_path / "c.toml"
    write_schedules(path, [_entry(id='has"quote', args={"msg": 'a "b" c'})])
    loaded = load_schedules(path)
    assert loaded[0].id == 'has"quote'
    assert loaded[0].args["msg"] == 'a "b" c'
