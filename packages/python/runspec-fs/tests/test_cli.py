"""Wrapper logic: edit-pairing, kwargs unpacking, the run-as worker path (mocked
sudo), the worker end-to-end, and a live in-process run of a couple of verbs."""

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from runspec.testing import ParseHarness
from runspec_fs_core import BecomeError, build_become

from runspec_fs.cli import _pair_edits, _run_as_worker, _verb_kwargs

SPEC = Path(__file__).parent.parent / "runspec_fs" / "runspec.toml"


def _harness() -> ParseHarness:
    return ParseHarness(script_name="filesystem", config_path=SPEC)


# ── edit pairing ────────────────────────────────────────────────────────────


def test_pair_edits_ok() -> None:
    assert _pair_edits(["a", "b"], ["x", "y"]) == [["a", "x"], ["b", "y"]]


def test_pair_edits_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="count mismatch"):
        _pair_edits(["a"], ["x", "y"])


# ── kwargs unpacking (real parsed spec, no mocks) ───────────────────────────


def test_verb_kwargs_read_text_file() -> None:
    h = _harness()
    spec = h.expect_ok(["read-text-file", "--path", "README.md", "--head", "3"])
    kw = _verb_kwargs(spec)
    assert kw["path"].endswith("README.md") and kw["head"] == 3 and kw["tail"] is None
    h.close()


def test_verb_kwargs_edit_file_pairs_and_is_json_safe() -> None:
    h = _harness()
    spec = h.expect_ok(["edit-file", "--path", "a.txt", "--old-text", "foo", "--new-text", "bar"])
    kw = _verb_kwargs(spec)
    assert kw["edits"] == [["foo", "bar"]] and kw["dry_run"] is False
    json.dumps(kw)  # must round-trip for the run-as worker
    h.close()


def test_verb_kwargs_search_exclude_defaults_to_none() -> None:
    h = _harness()
    spec = h.expect_ok(["search-files", "--path", ".", "--pattern", "x"])
    assert _verb_kwargs(spec)["exclude"] is None
    h.close()


def test_verb_kwargs_directory_size() -> None:
    h = _harness()
    spec = h.expect_ok(["directory-size", "--path", "sub", "--limit", "5", "--depth", "3"])
    kw = _verb_kwargs(spec)
    assert kw["path"].endswith("sub") and kw["limit"] == 5 and kw["depth"] == 3
    assert kw["cross_mounts"] is False  # one filesystem by default, like du -x
    h.close()


def test_verb_kwargs_directory_size_cross_mounts() -> None:
    h = _harness()
    spec = h.expect_ok(["directory-size", "--path", "sub", "--cross-mounts"])
    assert _verb_kwargs(spec)["cross_mounts"] is True
    h.close()


# ── run-as worker path (mock the sudo subprocess) ───────────────────────────


def test_run_as_worker_builds_become_command_and_ships_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(argv, input=None, capture_output=False, text=False):
        captured["argv"] = argv
        captured["input"] = input
        return types.SimpleNamespace(returncode=0, stdout='{"ok": 1}\n', stderr="")

    monkeypatch.setattr("runspec_fs.cli.subprocess.run", fake_run)
    monkeypatch.setattr("runspec_fs.cli.escalation_supported", lambda: True)

    out = _run_as_worker("svc", "sudo", "list-directory", {"path": "/tmp"})

    assert out == '{"ok": 1}\n'
    assert captured["argv"] == build_become([sys.executable, "-m", "runspec_fs._worker"], "svc", "sudo")
    assert json.loads(captured["input"]) == {"verb": "list-directory", "kwargs": {"path": "/tmp"}}


def test_run_as_worker_passwordless_hint(monkeypatch) -> None:
    def fake_run(argv, input=None, capture_output=False, text=False):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="sudo: a password is required")

    monkeypatch.setattr("runspec_fs.cli.subprocess.run", fake_run)
    monkeypatch.setattr("runspec_fs.cli.escalation_supported", lambda: True)

    with pytest.raises(BecomeError, match="passwordless sudo is not configured"):
        _run_as_worker("svc", "sudo", "list-directory", {"path": "/tmp"})


def test_run_as_worker_refuses_non_posix(monkeypatch) -> None:
    monkeypatch.setattr("runspec_fs.cli.escalation_supported", lambda: False)
    with pytest.raises(BecomeError, match="POSIX"):
        _run_as_worker("svc", "sudo", "list-directory", {"path": "/tmp"})


# ── worker + in-process, end to end (no sudo) ───────────────────────────────


def test_worker_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_text("x")
    payload = json.dumps({"verb": "list-directory", "kwargs": {"path": str(tmp_path)}})
    proc = subprocess.run(
        [sys.executable, "-m", "runspec_fs._worker"],
        input=payload,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert {e["name"] for e in out["entries"]} == {"w.txt"}


def _run_fs(argv: list[str], tmp_path: Path) -> dict:
    env = {**os.environ, "RUNSPEC_CONFIG": str(SPEC)}
    proc = subprocess.run(
        [sys.executable, "-c", "from runspec_fs.cli import main; main()", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_directory_size_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "tiny.bin").write_bytes(b"x")
    out = _run_fs(["directory-size", "--path", str(tmp_path)], tmp_path)
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["big"]["size"] == 100
    assert out["entries"][0]["name"] == "big"  # largest first
    assert out["total_size"] == 101


def test_run_as_current_user_skips_escalation(tmp_path: Path) -> None:
    # run-as = the user we already are → no sudo, runs in-process and still works.
    # Use the same helper cli.main() compares against, so the values always match.
    from runspec_fs_core import current_user

    (tmp_path / "hi.txt").write_text("hi")
    out = _run_fs(
        ["--run-as", current_user(), "list-directory", "--path", str(tmp_path)],
        tmp_path,
    )
    assert "hi.txt" in {e["name"] for e in out["entries"]}


# ── {{secret:<id>}} placeholders (end to end) ───────────────────────────────

SECRET = "glc_s3cr3t-token-value"


def _run_fs_raw(argv: list[str], tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "RUNSPEC_CONFIG": str(SPEC), **extra_env}
    return subprocess.run(
        [sys.executable, "-c", "from runspec_fs.cli import main; main()", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )


def test_write_file_fills_secret_and_never_prints_it(tmp_path: Path) -> None:
    cfg = tmp_path / "config.alloy"
    content = 'username = "{{secret:grafana-cloud.username}}"\npassword = "{{secret:grafana-cloud}}"\n'
    proc = _run_fs_raw(
        ["write-file", "--path", str(cfg), "--content", content],
        tmp_path,
        {"RUNSPEC_SECRET_GRAFANA_CLOUD": SECRET, "RUNSPEC_SECRET_GRAFANA_CLOUD__USERNAME": "123456"},
    )
    assert proc.returncode == 0, proc.stderr
    assert cfg.read_text() == f'username = "123456"\npassword = "{SECRET}"\n'
    assert SECRET not in proc.stdout and SECRET not in proc.stderr
    if os.name == "posix":
        assert json.loads(proc.stdout.strip().splitlines()[-1])["mode"] == "0600"


def test_edit_file_diff_is_masked(tmp_path: Path) -> None:
    cfg = tmp_path / "config.alloy"
    cfg.write_text('password = "CHANGE_ME"\n')
    proc = _run_fs_raw(
        ["edit-file", "--path", str(cfg), "--old-text", "CHANGE_ME", "--new-text", "{{secret:grafana-cloud}}"],
        tmp_path,
        {"RUNSPEC_SECRET_GRAFANA_CLOUD": SECRET},
    )
    assert proc.returncode == 0, proc.stderr
    assert cfg.read_text() == f'password = "{SECRET}"\n'
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "{{secret:grafana-cloud}}" in out["diff"]
    assert SECRET not in proc.stdout and SECRET not in proc.stderr


def test_missing_secret_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.alloy"
    proc = _run_fs_raw(["write-file", "--path", str(cfg), "--content", "{{secret:grafana-cloud}}"], tmp_path, {})
    assert proc.returncode == 1
    assert "RUNSPEC_SECRET_GRAFANA_CLOUD" in proc.stdout
    assert not cfg.exists()


def test_verb_kwargs_mode() -> None:
    h = _harness()
    spec = h.expect_ok(["write-file", "--path", "a", "--content", "x", "--mode", "0640"])
    assert _verb_kwargs(spec)["mode"] == 0o640
    h.expect_exit(["write-file", "--path", "a", "--content", "x", "--mode", "rw-r"], code=None, contains="mode")
    h.close()
