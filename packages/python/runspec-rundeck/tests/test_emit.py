"""Unit tests for the emit/discovery shell-outs and the pure visibility helpers."""

from __future__ import annotations

import json

import pytest

from runspec_rundeck import emit


def test_owning_runnable_longest_match():
    names = {"foo", "foo_bar", "greeter"}
    assert emit.owning_runnable("greeter", names) == "greeter"
    assert emit.owning_runnable("foo_x", names) == "foo"
    assert emit.owning_runnable("foo_bar_baz", names) == "foo_bar"  # longest wins
    assert emit.owning_runnable("foobar_x", names) is None  # underscore boundary
    assert emit.owning_runnable("unknown", names) is None


def test_filter_visible_drops_hidden():
    jobs = [{"name": "greeter"}, {"name": "secret_helper_run"}]
    names = {"greeter", "secret_helper"}
    hidden = {"secret_helper"}
    kept, dropped = emit.filter_visible(jobs, names, hidden)
    assert [j["name"] for j in kept] == ["greeter"]
    assert dropped == ["secret_helper_run"]


def test_filter_visible_include_hidden_keeps_all():
    jobs = [{"name": "greeter"}, {"name": "secret_helper"}]
    kept, dropped = emit.filter_visible(jobs, {"greeter", "secret_helper"}, {"secret_helper"}, include_hidden=True)
    assert len(kept) == 2
    assert dropped == []


def test_filter_visible_no_hidden_is_noop():
    jobs = [{"name": "a"}, {"name": "b"}]
    kept, dropped = emit.filter_visible(jobs, {"a", "b"}, set())
    assert kept == jobs
    assert dropped == []


def test_emit_jobs_builds_argv_and_parses(monkeypatch):
    seen = {}

    def fake_run(argv):
        seen["argv"] = argv
        return json.dumps([{"name": "greeter", "uuid": "u1"}])

    monkeypatch.setattr(emit, "_run", fake_run)
    jobs = emit.emit_jobs("ops", group="team", node_filter="tags: web", venv="/opt/v", runnable="greeter", bin_path="runspec")
    assert jobs == [{"name": "greeter", "uuid": "u1"}]
    assert seen["argv"][:5] == ["runspec", "emit", "--rundeck", "--stdout", "--project"]
    assert "ops" in seen["argv"]
    assert "--group" in seen["argv"] and "team" in seen["argv"]
    assert "--node-filter" in seen["argv"] and "tags: web" in seen["argv"]
    assert "--venv" in seen["argv"] and "/opt/v" in seen["argv"]
    assert "--runnable" in seen["argv"] and "greeter" in seen["argv"]


def test_emit_jobs_rejects_non_array(monkeypatch):
    monkeypatch.setattr(emit, "_run", lambda argv: json.dumps({"not": "a list"}))
    with pytest.raises(emit.EmitError, match="array"):
        emit.emit_jobs("ops", bin_path="runspec")


def test_emit_jobs_empty_output(monkeypatch):
    monkeypatch.setattr(emit, "_run", lambda argv: "  \n")
    assert emit.emit_jobs("ops", bin_path="runspec") == []


def test_discover_visibility_parses_array(monkeypatch):
    # Real `runspec local --format json` shape: an array of discovery records.
    payload = [
        {"runnable": "greeter", "spec": {"name": "greeter", "discoverable": None}},
        {"runnable": "secret", "spec": {"name": "secret", "discoverable": False}},
    ]
    monkeypatch.setattr(emit, "_run", lambda argv: json.dumps(payload))
    names, hidden = emit.discover_visibility(bin_path="runspec")
    assert names == {"greeter", "secret"}
    assert hidden == {"secret"}


def test_discover_visibility_dict_fallback(monkeypatch):
    payload = {"greeter": {"name": "greeter"}, "secret": {"name": "secret", "x-discoverable": False}}
    monkeypatch.setattr(emit, "_run", lambda argv: json.dumps(payload))
    names, hidden = emit.discover_visibility(bin_path="runspec")
    assert names == {"greeter", "secret"}
    assert hidden == {"secret"}


def test_discover_visibility_best_effort_on_failure(monkeypatch):
    def boom(argv):
        raise emit.EmitError("no runspec")

    monkeypatch.setattr(emit, "_run", boom)
    assert emit.discover_visibility(bin_path="runspec") == (set(), set())


def test_load_job_files_array_and_single(tmp_path):
    (tmp_path / "arr.json").write_text(json.dumps([{"name": "a"}, {"name": "b"}]))
    (tmp_path / "one.json").write_text(json.dumps({"name": "c"}))
    jobs = emit.load_job_files([str(tmp_path / "arr.json"), str(tmp_path / "one.json")])
    assert [j["name"] for j in jobs] == ["a", "b", "c"]


def test_load_job_files_directory_globs(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"name": "a"}))
    (tmp_path / "b.json").write_text(json.dumps({"name": "b"}))
    (tmp_path / "ignore.txt").write_text("nope")
    jobs = emit.load_job_files([str(tmp_path)])
    assert sorted(j["name"] for j in jobs) == ["a", "b"]


def test_load_job_files_missing_raises():
    with pytest.raises(emit.EmitError, match="No such file"):
        emit.load_job_files(["/does/not/exist.json"])
