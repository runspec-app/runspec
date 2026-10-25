"""test_envfile.py — the pure .runspec_env helpers and the worker.

Parsing must match core (``runspec.env._parse_dotenv``) exactly, so the
console shows what a runnable reads; patching must keep every line it doesn't
touch; the worker's write is compare-and-swap and keeps the file's mode.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from runspec.env import _parse_dotenv
from runspec_console import _envfile_worker as worker
from runspec_console import envfile as ef

SAMPLE = """\
# deploy settings
JIRA_URL=https://jira.example.com
JIRA_PAT="abc 123"
  SPACED = ' padded '
EMPTY=
not a line
DUP=one
DUP=two
"""


def test_parse_matches_core(tmp_path):
    p = tmp_path / ".runspec_env"
    p.write_text(SAMPLE, encoding="utf-8")
    assert ef.parse(SAMPLE).values() == _parse_dotenv(p)


def test_parse_reports_comments_ignored_and_duplicates():
    parsed = ef.parse(SAMPLE)
    assert parsed.comments == 1
    assert parsed.ignored == [6]
    dup = next(e for e in parsed.entries if e.key == "DUP")
    assert (dup.value, dup.line, dup.duplicates) == ("two", 8, 1)


@pytest.mark.parametrize(
    "value",
    ["plain", "", " lead", "trail ", '"quoted"', "'single'", 'mix "a" b', "a=b#c", "ü"],
)
def test_format_line_round_trips_through_core(tmp_path, value):
    p = tmp_path / "e"
    p.write_text(ef.format_line("K", value) + "\n", encoding="utf-8")
    assert _parse_dotenv(p) == {"K": value}


def test_apply_changes_keeps_untouched_lines():
    out = ef.apply_changes(SAMPLE, {"JIRA_URL": "https://new", "NEW": "x"}, ["EMPTY"])
    lines = out.splitlines()
    assert lines[0] == "# deploy settings"
    assert lines[1] == "JIRA_URL=https://new"
    assert "EMPTY=" not in lines
    assert "not a line" in lines
    assert lines[-1] == "NEW=x"
    assert ef.parse(out).values()["JIRA_PAT"] == "abc 123"


def test_apply_changes_collapses_a_set_duplicate():
    out = ef.apply_changes(SAMPLE, {"DUP": "three"}, [])
    assert [ln for ln in out.splitlines() if ln.startswith("DUP")] == ["DUP=three"]


def test_apply_changes_empty_and_validation():
    assert ef.apply_changes("", {"A": "1"}, []) == "A=1\n"
    assert ef.apply_changes("A=1\n", {}, ["A"]) == ""
    with pytest.raises(ef.EnvFileError):
        ef.apply_changes("", {"1BAD": "x"}, [])
    with pytest.raises(ef.EnvFileError):
        ef.apply_changes("", {"A": "two\nlines"}, [])
    with pytest.raises(ef.EnvFileError):
        ef.apply_changes("", {"A": "x"}, ["A"])


def test_changed_keys():
    assert ef.changed_keys("A=1\nB=2\n", "A=1\nB=3\nC=4\n") == {
        "added": ["C"],
        "changed": ["B"],
        "removed": [],
    }


def test_secret_detection():
    for k in (
        "JIRA_PAT",
        "DB_PASSWORD",
        "API_KEY",
        "GITLAB_TOKEN",
        "SSH_KEY",
        "CLIENT_SECRET",
    ):
        assert ef.is_secret_name(k), k
    for k in ("JIRA_URL", "PATH_PREFIX", "KEYBOARD", "MONKEY_COUNT"):
        assert not ef.is_secret_name(k), k
    assert ef.world_readable("0644") and not ef.world_readable("0600")


RECORDS = [
    {
        "name": "jira",
        "args": [
            {"name": "jira-pat", "type": "password", "env": ["JIRA_PERSONAL_TOKEN"]},
            {"name": "env", "type": "str", "description": "instance"},
        ],
        "commands": {
            "get-issue": {
                "name": "get-issue",
                "args": [
                    {"name": "jira-pat", "type": "password", "inherited": True},
                    {"name": "issue-key", "type": "str"},
                ],
            }
        },
        "rawSpec": {},
    },
    {"name": "deploy", "args": [], "rawSpec": {"runspec_env": "/etc/deploy.env"}},
]


def test_readers_auto_alias_and_commands():
    r = ef.readers(RECORDS)
    pat = r["RUNSPEC_JIRA_ARG_JIRA_PAT"]
    assert len(pat) == 1 and pat[0]["password"] and pat[0]["via"] == "auto"
    assert r["JIRA_PERSONAL_TOKEN"][0]["via"] == "alias"
    assert r["RUNSPEC_JIRA_ARG_ISSUE_KEY"][0]["command"] == "get-issue"
    assert ef.overrides(RECORDS) == [{"runnable": "deploy", "file": "/etc/deploy.env"}]


def test_entry_rows_masks_secrets_and_suggests():
    rows = ef.entry_rows(
        "JIRA_PERSONAL_TOKEN=s3cret\nRUNSPEC_JIRA_ARG_ENV=prod\n", ef.readers(RECORDS)
    )
    by = {e["key"]: e for e in rows["entries"]}
    assert (
        by["JIRA_PERSONAL_TOKEN"]["masked"] and by["JIRA_PERSONAL_TOKEN"]["value"] == ""
    )
    assert by["RUNSPEC_JIRA_ARG_ENV"]["value"] == "prod"
    assert "s3cret" not in json.dumps(rows)
    sugg = {s["key"]: s for s in rows["suggestions"]}
    assert "JIRA_PERSONAL_TOKEN" not in sugg
    assert sugg["RUNSPEC_JIRA_ARG_JIRA_PAT"]["secret"]
    assert rows["has_secrets"]


def test_roots():
    assert ef.remote_root("/opt/venvs/ops/bin/runspec") == "/opt/venvs/ops"
    with pytest.raises(ef.EnvFileError):
        ef.remote_root("C:\\venvs\\x\\Scripts\\runspec.exe")
    with pytest.raises(ef.EnvFileError):
        ef.remote_root("bin/runspec")
    assert ef.local_root(str(Path("/v/x/bin/runspec"))) == str(Path("/v/x"))


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #


def test_worker_read_absent_then_write_and_conflict(tmp_path):
    r = worker.run("read", str(tmp_path))
    assert r["ok"] and not r["exists"] and r["sha"] == ef.ABSENT and r["writable"]
    w = worker.run("write", str(tmp_path), {"text": "A=1\n", "expect": ef.ABSENT})
    assert w["ok"]
    f = tmp_path / ".runspec_env"
    assert f.read_text() == "A=1\n"
    if os.name == "posix":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    stale = worker.run("write", str(tmp_path), {"text": "A=2\n", "expect": ef.ABSENT})
    assert stale.get("conflict") and f.read_text() == "A=1\n"
    r2 = worker.run("read", str(tmp_path))
    assert r2["text"] == "A=1\n" and r2["sha"] == w["sha"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_worker_keeps_existing_mode_and_refuses_symlink(tmp_path):
    f = tmp_path / ".runspec_env"
    f.write_text("A=1\n")
    f.chmod(0o640)
    sha = worker.run("read", str(tmp_path))["sha"]
    assert worker.run("write", str(tmp_path), {"text": "A=2\n", "expect": sha})["ok"]
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    f.unlink()
    target = tmp_path / "real.env"
    target.write_text("")
    f.symlink_to(target)
    r = worker.run("read", str(tmp_path))
    assert r["symlink"]
    out = worker.run("write", str(tmp_path), {"text": "B=1\n", "expect": r["sha"]})
    assert not out["ok"] and "symlink" in out["error"]


def test_worker_runs_as_a_script_with_stdin(tmp_path):
    src = Path(worker.__file__).read_text()
    payload = json.dumps({"text": "K=v\n", "expect": ef.ABSENT})
    p = subprocess.run(
        [sys.executable, "-c", src, "write", str(tmp_path)],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    line = [ln for ln in p.stdout.splitlines() if ln.startswith(worker.MARKER)][-1]
    assert json.loads(line[len(worker.MARKER) :])["ok"]
    assert (tmp_path / ".runspec_env").read_text() == "K=v\n"


def test_worker_missing_dir(tmp_path):
    r = worker.run("read", str(tmp_path / "nope"))
    assert not r["ok"] and "not a directory" in r["error"]
