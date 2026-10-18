"""Mock playback (RUNSPEC_MOCK): parse() answers from recorded responses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import runspec
from runspec import mock

SPEC = """
[tickets]
description = "Ticket tool"
autonomy = "autonomous"
require-command = true

[tickets.args]
env = {type = "choice", options = ["dev", "prod"], default = "dev"}
token = {type = "password", required = true, description = "API token"}

[tickets.commands.get]
description = "Get a ticket"
[tickets.commands.get.args]
key = {type = "str", required = true}
verbose = {type = "flag", default = false}
limit = {type = "int", default = 10}

[tickets.commands.comment]
description = "Comment on a ticket"
autonomy = "confirm"
[tickets.commands.comment.args]
key = {type = "str", required = true}
body = {type = "str", required = true}

[plain]
description = "No subcommands"
[plain.args]
name = {type = "str", default = "x"}
"""


@pytest.fixture
def spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    for var in (mock.MOCK_ENV, mock.MOCK_DIR_ENV, mock.MOCK_RECORD_ENV, "RUNSPEC_TICKETS_ARG_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    path = pkg / "runspec.toml"
    path.write_text(SPEC, encoding="utf-8")
    return path


def _write(path: Path, entries: list[dict], runnable: str = "tickets") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runspec_mock": 1, "runnable": runnable, "entries": entries}), encoding="utf-8")
    return path


def _package_file(spec: Path, entries: list[dict], runnable: str = "tickets") -> Path:
    return _write(spec.parent / "mocks" / f"{runnable}.json", entries, runnable)


def _play(spec: Path, argv: list[str], capsys: pytest.CaptureFixture[str], runnable: str = "tickets") -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        runspec.parse(runnable, argv=argv, config_path=spec)
    out, err = capsys.readouterr()
    return int(exc.value.code or 0), out, err


ENTRIES = [
    {"command": ["get"], "args": {"key": "T-1"}, "stdout": {"key": "T-1", "status": "Open"}},
    {"command": ["get"], "args": {"key": "T-1", "verbose": True}, "stdout": {"key": "T-1", "status": "Open", "history": []}},
    {"command": ["get"], "args": {}, "exit_code": 1, "stdout": {"error": "not found"}},
    {"command": ["comment"], "args": {"key": "T-1"}, "stdout": "commented\n", "stderr": "note: mock\n"},
]


# ── mode ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "mode"),
    [("1", "best"), ("true", "best"), ("ON", "best"), ("strict", "strict"), ("", ""), ("0", ""), ("nope", "")],
)
def test_mock_mode(raw: str, mode: str) -> None:
    assert mock.mock_mode({mock.MOCK_ENV: raw}) == mode


# ── playback through parse() ──────────────────────────────────────────────────


def test_plays_the_matching_response_without_running(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert code == 0
    assert json.loads(out) == {"key": "T-1", "status": "Open"}


def test_most_specific_entry_wins(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    _, out, _ = _play(spec, ["get", "--key", "T-1", "--verbose"], capsys)
    assert "history" in json.loads(out)


def test_empty_args_entry_is_the_commands_fallback(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get", "--key", "T-9"], capsys)
    assert code == 1
    assert json.loads(out) == {"error": "not found"}


def test_string_stdout_and_stderr_are_written_verbatim(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    _, out, err = _play(spec, ["comment", "--key", "T-1", "--body", "anything at all"], capsys)
    assert out == "commented\n"
    assert err == "note: mock\n"


def test_strict_needs_the_exact_args(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "strict")
    code, out, _ = _play(spec, ["comment", "--key", "T-1", "--body", "hi"], capsys)
    assert code == mock.EXIT_NO_MOCK
    assert json.loads(out)["kind"] == "MockNotFound"
    code, out, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert code == 0 and json.loads(out)["status"] == "Open"


def test_no_match_never_runs_and_lists_what_is_recorded(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, [ENTRIES[0]])
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get", "--key", "T-2"], capsys)
    assert code == mock.EXIT_NO_MOCK == 69
    payload = json.loads(out)
    assert payload == {
        "error": "no mock response recorded for these arguments",
        "kind": "MockNotFound",
        "runnable": "tickets",
        "command": ["get"],
        "args": {"key": "T-2"},
        "recorded": [{"key": "T-1"}],
    }


def test_no_mock_file_at_all_is_a_miss(spec: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert code == mock.EXIT_NO_MOCK
    assert json.loads(out)["recorded"] == []


def test_validation_still_runs(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get"], capsys)  # --key missing
    assert code == 1
    assert "--key" in out
    code, out, _ = _play(spec, ["--env", "staging", "get", "--key", "T-1"], capsys)
    assert code == 1


def test_required_password_is_not_needed(spec: Path, capsys, monkeypatch) -> None:
    # `token` is a required password: with no value a real run fails; a mock run doesn't.
    _package_file(spec, ENTRIES)
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, _, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert code == 0


def test_password_and_defaults_are_not_part_of_the_key(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, [{"command": ["get"], "args": {"key": "T-1"}, "stdout": "ok"}])
    monkeypatch.setenv(mock.MOCK_ENV, "strict")
    monkeypatch.setenv("RUNSPEC_TICKETS_ARG_TOKEN", "s3cret")
    code, out, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert (code, out) == (0, "ok")


def test_values_compare_by_string_form(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, [{"command": ["get"], "args": {"key": "T-1", "limit": "5"}, "stdout": "five"}])
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    _, out, _ = _play(spec, ["get", "--key", "T-1", "--limit", "5"], capsys)
    assert out == "five"


def test_env_supplied_args_take_part(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, [{"command": ["get"], "args": {"key": "T-1", "env": "prod"}, "stdout": "prod"}, {"command": ["get"], "args": {"key": "T-1"}, "stdout": "any"}])
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    monkeypatch.setenv("RUNSPEC_TICKETS_ARG_ENV", "prod")
    _, out, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert out == "prod"


def test_layers_mock_dir_then_venv_then_package(spec: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    entry = {"command": ["get"], "args": {"key": "T-1"}}
    _package_file(spec, [{**entry, "stdout": "package"}])
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    assert _play(spec, ["get", "--key", "T-1"], capsys)[1] == "package"
    _write(Path(sys.prefix) / "runspec_mocks" / "tickets.json", [{**entry, "stdout": "venv"}])
    assert _play(spec, ["get", "--key", "T-1"], capsys)[1] == "venv"
    _write(tmp_path / "extra" / "tickets.json", [{**entry, "stdout": "dir"}])
    monkeypatch.setenv(mock.MOCK_DIR_ENV, str(tmp_path / "extra"))
    assert _play(spec, ["get", "--key", "T-1"], capsys)[1] == "dir"


def test_runnable_without_subcommands(spec: Path, capsys, monkeypatch) -> None:
    _package_file(spec, [{"args": {"name": "y"}, "stdout": "hello y"}], runnable="plain")
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    assert _play(spec, ["--name", "y"], capsys, runnable="plain")[1] == "hello y"


def test_malformed_file_refuses_without_running(spec: Path, capsys, monkeypatch) -> None:
    (spec.parent / "mocks").mkdir()
    (spec.parent / "mocks" / "tickets.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get", "--key", "T-1"], capsys)
    assert code == mock.EXIT_NO_MOCK
    assert json.loads(out)["kind"] == "MockError"


def test_help_is_not_mocked(spec: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    code, out, _ = _play(spec, ["get", "--help"], capsys)
    assert code == 0 and "--key" in out


def test_off_returns_normally(spec: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUNSPEC_TICKETS_ARG_TOKEN", "t")
    parsed = runspec.parse("tickets", argv=["get", "--key", "T-1"], config_path=spec)
    assert parsed.key.value == "T-1"


def test_load_spec_is_never_mocked(spec: Path, monkeypatch) -> None:
    _package_file(spec, ENTRIES, runnable="plain")
    monkeypatch.setenv(mock.MOCK_ENV, "1")
    assert runspec.load_spec("plain", config_path=spec).name.value == "x"


# ── recording handoff ─────────────────────────────────────────────────────────


def test_record_handoff_writes_the_cli_key_and_runs_normally(spec: Path, tmp_path: Path, monkeypatch) -> None:
    key_file = tmp_path / "key.json"
    monkeypatch.setenv(mock.MOCK_RECORD_ENV, str(key_file))
    monkeypatch.setenv("RUNSPEC_TICKETS_ARG_TOKEN", "t")
    monkeypatch.setenv("RUNSPEC_TICKETS_ARG_ENV", "prod")  # env-supplied: not recorded
    parsed = runspec.parse("tickets", argv=["get", "--key", "T-1", "--verbose"], config_path=spec)
    assert parsed.key.value == "T-1"
    assert json.loads(key_file.read_text()) == {"command": ["get"], "args": {"key": "T-1", "verbose": True}}
    import os

    assert mock.MOCK_RECORD_ENV not in os.environ


def test_build_entry_stores_json_as_json_and_redacts() -> None:
    entry = mock.build_entry({"command": ["get"], "args": {"key": "T-1"}}, 0, '{"owner": "alice@acme.com"}\n', "", redact=[r"[a-z]+@acme\.com"], note="n")
    assert entry["stdout"] == {"owner": mock.REDACTED}
    assert "stderr" not in entry
    assert entry["source"] == "record" and entry["note"] == "n"
    text = mock.build_entry({}, 2, "plain text\n", "boom\n")
    assert text["stdout"] == "plain text\n" and text["stderr"] == "boom\n" and text["exit_code"] == 2


def test_save_entry_adds_then_replaces(tmp_path: Path) -> None:
    path = tmp_path / "mocks" / "tickets.json"
    assert mock.save_entry(path, "tickets", {"command": ["get"], "args": {"key": "T-1"}, "exit_code": 0, "stdout": "a"}) == "added"
    assert mock.save_entry(path, "tickets", {"command": ["get"], "args": {"key": "T-1"}, "exit_code": 0, "stdout": "b"}) == "replaced"
    assert mock.save_entry(path, "tickets", {"command": ["get"], "args": {"key": "T-2"}, "exit_code": 0, "stdout": "c"}) == "added"
    data = mock.load_file(path)
    assert [e["stdout"] for e in data["entries"]] == ["b", "c"]
    assert data["runspec_mock"] == 1 and data["runnable"] == "tickets"


def test_resolve_destination(spec: Path, tmp_path: Path) -> None:
    assert mock.resolve_destination("tickets", spec) == spec.parent / "mocks" / "tickets.json"
    assert mock.resolve_destination("tickets", spec, "venv") == Path(sys.prefix) / "runspec_mocks" / "tickets.json"
    assert mock.resolve_destination("tickets", spec, str(tmp_path / "d")) == tmp_path / "d" / "tickets.json"
    installed = tmp_path / "lib" / "site-packages" / "pkg" / "runspec.toml"
    assert mock.resolve_destination("tickets", installed) == Path(sys.prefix) / "runspec_mocks" / "tickets.json"


# ── check ─────────────────────────────────────────────────────────────────────


def test_check_accepts_valid_entries(spec: Path) -> None:
    path = _package_file(spec, ENTRIES)
    assert mock.check_entries("tickets", spec, mock.load_candidates("tickets", spec)) == []
    assert path.is_file()


def test_check_reports_drift_and_secrets(spec: Path) -> None:
    _package_file(
        spec,
        [
            {"command": ["delete"], "args": {}},
            {"command": ["get"], "args": {"nope": 1}},
            {"command": ["get"], "args": {"limit": "lots"}},
            {"command": ["get"], "args": {"token": "x"}},
            {"command": [], "args": {}},
            {"command": ["get"], "args": {"key": "T-1"}, "stdout": {"auth": "Bearer abcdefghijklmnopqrstuvwxyz0123"}},
        ],
    )
    problems = mock.check_entries("tickets", spec, mock.load_candidates("tickets", spec))
    joined = "\n".join(problems)
    assert len(problems) == 6, joined
    assert "no such subcommand" in problems[0]
    assert "unknown arg 'nope'" in problems[1]
    assert "lots" in problems[2]
    assert "password arg 'token'" in problems[3]
    assert "needs a subcommand" in problems[4]
    assert "credential" in problems[5]


def test_load_file_rejects_bad_shapes(tmp_path: Path) -> None:
    bad = tmp_path / "x.json"
    bad.write_text(json.dumps({"runspec_mock": 2, "entries": []}))
    with pytest.raises(mock.MockError, match="version"):
        mock.load_file(bad)
    bad.write_text(json.dumps({"runspec_mock": 1, "entries": [{"command": "get"}]}))
    with pytest.raises(mock.MockError, match="command"):
        mock.load_file(bad)


# ── runspec mock (CLI) ────────────────────────────────────────────────────────


@pytest.fixture
def installed(spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`tickets` installed as a tiny script that prints what it was asked for."""
    from runspec import cli, execute

    script = tmp_path / "tickets_main.py"
    script.write_text(
        f"import json, runspec\na = runspec.parse('tickets', config_path={str(spec)!r})\nprint(json.dumps({{'key': a.key.value, 'live': True}}))\n",
        encoding="utf-8",
    )
    binary = [sys.executable, str(script)]
    monkeypatch.setattr(cli, "_discover_installed", lambda: [{"runnable": "tickets", "source": str(spec), "spec": {}}])
    monkeypatch.setattr(execute, "find_script", lambda name, scripts_dir=None: binary if name == "tickets" else None)
    monkeypatch.setenv("RUNSPEC_TICKETS_ARG_TOKEN", "t")
    return binary


def _cli(args: list[str], capsys) -> tuple[int, str, str]:
    from runspec import cli

    code = 0
    try:
        cli.cmd_mock(args)
    except SystemExit as e:
        code = int(e.code or 0)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_record_list_play_check(spec: Path, installed: list[str], capsys) -> None:
    code, out, err = _cli(["record", "tickets", "--", "get", "--key", "T-7"], capsys)
    assert code == 0, err
    assert "added tickets get" in out
    data = mock.load_file(spec.parent / "mocks" / "tickets.json")
    assert data["entries"][0]["args"] == {"key": "T-7"}
    assert data["entries"][0]["stdout"] == {"key": "T-7", "live": True}

    code, out, _ = _cli(["list", "--json"], capsys)
    rows = json.loads(out)
    assert [(r["command"], r["args"], r["layer"]) for r in rows] == [(["get"], {"key": "T-7"}, "package")]

    code, out, _ = _cli(["check"], capsys)
    assert code == 0 and "1 mock response" in out


def test_cli_record_refuses_when_args_are_rejected(spec: Path, installed: list[str], capsys) -> None:
    code, _, err = _cli(["record", "tickets", "--", "get"], capsys)  # --key missing
    assert code == 1
    assert "nothing recorded" in err
    assert not (spec.parent / "mocks" / "tickets.json").exists()


def test_cli_check_fails_on_drift(spec: Path, installed: list[str], capsys) -> None:
    _package_file(spec, [{"command": ["get"], "args": {"gone": 1}}])
    code, out, _ = _cli(["check"], capsys)
    assert code == 1 and "unknown arg 'gone'" in out


def test_cli_needs_a_runnable_for_record(capsys) -> None:
    code, _, err = _cli(["record"], capsys)
    assert code == 2 and "needs a runnable" in err
