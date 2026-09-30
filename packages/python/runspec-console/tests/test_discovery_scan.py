"""
test_discovery_scan.py — discovery's site-packages scan (fast path) and its
``runspec local`` fallback.

The scan must yield exactly what ``runspec local --format json`` would: that
command is runspec's ``load_raw`` per package printed as-is, so the console
normalises the fetched TOML with the same loader functions and applies the same
filters (entry point present in bin/, dedupe, core/self excluded). Fallback to
the CLI whenever the scan can't be trusted (unusable reply, no runspec.toml,
editable install).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from runspec.loader import load_raw
from runspec_console import discovery
from runspec_console.discovery import (
    _SCAN_END,
    _normalise_toml_text,
    _parse_scan_identity,
    _parse_scan_output,
    _runnables_from_tomls,
    _scan_command,
    local_identity,
    owner_account,
    discover_local,
    discover_remote,
)

TOML_A = """
[config]
autonomy-default = "autonomous"

[hello]
description = "Say hello"

[hello.args]
name = "world"
count = 2
loud = false

[secret-tool]
description = "No entry point installed"
"""

TOML_B = """
[deploy]
description = "Deploy"
autonomy = "confirm"

[deploy.args]
target = { type = "str" }
"""

CORE_TOML = """
[runspec]
description = "the core control plane"
"""


def _reply(
    bin_names: list[str],
    files: list[tuple[str, str]],
    editable: list[str] | None = None,
) -> str:
    lines = ["=====BIN", *bin_names, "=====EDITABLE", *(editable or [])]
    for path, text in files:
        lines.append(f"=====FILE {path}")
        lines.extend(text.splitlines())
    lines.append(_SCAN_END)
    return "\n".join(lines) + "\n"


SP = "/opt/venvs/ops/lib/python3.11/site-packages"


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_scan_command_quotes_path_and_carries_markers():
    cmd = _scan_command("/opt/venvs/ops tools/bin/runspec")
    assert "'/opt/venvs/ops tools/bin/runspec'" in cmd
    for m in ("=====BIN", "=====EDITABLE", "=====FILE", "=====END"):
        assert m in cmd
    assert "runspec.toml" in cmd and "direct_url.json" in cmd


def test_parse_scan_output_requires_end_marker():
    assert _parse_scan_output("[]") is None
    assert _parse_scan_output("=====BIN\nhello\n=====EDITABLE\n") is None  # truncated


def test_parse_scan_output_sections():
    out = _reply(
        ["hello", "runspec", ""],
        [(f"{SP}/a/runspec.toml", TOML_A), (f"{SP}/b/runspec.toml", TOML_B)],
    )
    parsed = _parse_scan_output(out)
    assert parsed is not None
    bin_names, editable, files = parsed
    assert bin_names == {"hello", "runspec"}
    assert editable is False
    assert [p for p, _ in files] == [f"{SP}/a/runspec.toml", f"{SP}/b/runspec.toml"]
    assert "[hello.args]" in files[0][1] and "[deploy]" in files[1][1]


def test_parse_scan_output_flags_editable():
    out = _reply(["x"], [], editable=[f"{SP}/mypkg-1.0.dist-info/direct_url.json"])
    assert _parse_scan_output(out) == ({"x"}, True, [])


def test_normalise_toml_text_matches_load_raw(tmp_path: Path):
    """The scan's normalisation is byte-identical to what `runspec local` emits."""
    f = tmp_path / "runspec.toml"
    f.write_text(TOML_A, encoding="utf-8")
    assert _normalise_toml_text(TOML_A) == load_raw(f)


def test_normalise_toml_text_rejects_non_runspec_toml():
    assert _normalise_toml_text("not = toml = at all") is None
    # A reserved arg-name prefix makes the loader raise — skipped, not fatal.
    assert _normalise_toml_text("[x]\n[x.args]\nrunspec_bad = 1\n") is None


def test_runnables_from_tomls_filters_like_the_cli():
    files = [
        (f"{SP}/a/runspec.toml", TOML_A),
        (f"{SP}/runspec/runspec.toml", CORE_TOML),  # core package — never listed
        (f"{SP}/a/runspec.toml", TOML_A),  # duplicate path — deduped
    ]
    out = _runnables_from_tomls(files, {"hello", "runspec"}, "prod-1", "ops")
    names = [r["name"] for r in out]
    assert names == ["hello"]  # secret-tool has no entry point; runspec excluded
    hello = out[0]
    assert hello["host"] == "prod-1" and hello["group"] == "ops"
    assert hello["autonomy"] == "autonomous"  # [config] autonomy-default applied
    args = {a["name"]: a for a in hello["args"]}
    assert args["name"]["default"] == "world" and args["count"]["default"] == 2
    assert args["loud"]["required"] is False


def test_runnables_from_tomls_accepts_windows_exe_entry_points():
    out = _runnables_from_tomls(
        [(f"{SP}/a/runspec.toml", TOML_A)], {"hello.exe"}, "h", "g"
    )
    assert [r["name"] for r in out] == ["hello"]


def test_runnables_from_tomls_excludes_the_console_itself():
    toml = '[runspec-console]\ndescription = "me"\n'
    assert (
        _runnables_from_tomls(
            [(f"{SP}/c/runspec.toml", toml)], {"runspec-console"}, "h", "g"
        )
        == []
    )


# ── discover_remote: scan first, CLI fallback ────────────────────────────────


class _Runner:
    """Records commands; answers the scan with ``scan_reply`` and the CLI with
    ``cli_reply`` (a JSON string) — each may be ``(code, out)``."""

    def __init__(
        self, scan_reply: tuple[int, str], cli_reply: tuple[int, str] = (0, "[]")
    ):
        self.scan_reply = scan_reply
        self.cli_reply = cli_reply
        self.commands: list[str] = []

    def __call__(
        self,
        target: str,
        command: str,
        *,
        identity_file: Any = None,
        global_ssh_config: Any = None,
        timeout: int = 15,
    ):
        self.commands.append(command)
        if "local --format json" in command:
            return (*self.cli_reply, "")
        return (*self.scan_reply, "")


def test_remote_scan_path_never_runs_the_cli():
    r = _Runner(
        (
            0,
            _reply(
                ["hello", "deploy"],
                [(f"{SP}/a/runspec.toml", TOML_A), (f"{SP}/b/runspec.toml", TOML_B)],
            ),
        )
    )
    out = discover_remote("u@prod-1", "/opt/venvs/ops/bin/runspec", "prod-1", runner=r)
    assert sorted(x["name"] for x in out) == ["deploy", "hello"]
    assert len(r.commands) == 1 and "local --format json" not in r.commands[0]
    assert {x["group"] for x in out} == {"ops"}


def test_remote_falls_back_when_reply_is_not_a_scan():
    r = _Runner(
        (0, "[]"),
        cli_reply=(
            0,
            '[{"runnable": "cli-tool", "spec": {"description": "from cli", "args": {}}, "config_autonomy": "confirm"}]',
        ),
    )
    out = discover_remote("u@h", "/v/bin/runspec", "h", runner=r)
    assert [x["name"] for x in out] == ["cli-tool"]
    assert len(r.commands) == 2 and "local --format json" in r.commands[1]


def test_remote_falls_back_on_non_zero_exit():
    r = _Runner((127, ""), cli_reply=(0, "[]"))
    assert discover_remote("u@h", "/v/bin/runspec", "h", runner=r) == []
    assert len(r.commands) == 2


def test_remote_falls_back_when_no_toml_found():
    r = _Runner((0, _reply(["hello"], [])), cli_reply=(0, "[]"))
    discover_remote("u@h", "/v/bin/runspec", "h", runner=r)
    assert len(r.commands) == 2


def test_remote_falls_back_on_editable_install():
    r = _Runner(
        (
            0,
            _reply(
                ["hello"],
                [(f"{SP}/a/runspec.toml", TOML_A)],
                editable=[f"{SP}/dev-0.1.dist-info/direct_url.json"],
            ),
        ),
        cli_reply=(
            0,
            '[{"runnable": "hello", "spec": {"description": "editable", "args": {}}, "config_autonomy": "confirm"}]',
        ),
    )
    out = discover_remote("u@h", "/v/bin/runspec", "h", runner=r)
    assert [x["description"] for x in out] == ["editable"]  # the CLI's answer won
    assert len(r.commands) == 2


def test_remote_cli_failure_after_fallback_is_empty():
    r = _Runner((0, "[]"), cli_reply=(1, ""))
    assert discover_remote("u@h", "/v/bin/runspec", "h", runner=r) == []


# ── discover_local: in-process scan, CLI fallback ────────────────────────────


def _venv(root: Path, *, windows: bool = False) -> str:
    """Lay down a fake venv with one runspec package + entry point. Returns the
    runspec binary path."""
    sp = root / ("Lib/site-packages" if windows else "lib/python3.11/site-packages")
    (sp / "mypkg").mkdir(parents=True)
    (sp / "mypkg" / "runspec.toml").write_text(TOML_A, encoding="utf-8")
    (sp / "runspec").mkdir()
    (sp / "runspec" / "runspec.toml").write_text(CORE_TOML, encoding="utf-8")
    b = root / ("Scripts" if windows else "bin")
    b.mkdir()
    for name in ("hello", "runspec"):
        (b / (f"{name}.exe" if windows else name)).write_text("#!/bin/sh\n")
    return str(b / ("runspec.exe" if windows else "runspec"))


def test_local_scan_needs_no_subprocess(tmp_path: Path):
    rp = _venv(tmp_path)
    with patch.object(
        subprocess, "run", side_effect=AssertionError("CLI must not run")
    ):
        out = discover_local(rp, "local")
    assert [r["name"] for r in out] == ["hello"]
    assert out[0]["group"] == tmp_path.name and out[0]["autonomy"] == "autonomous"


def test_local_scan_windows_layout(tmp_path: Path):
    rp = _venv(tmp_path, windows=True)
    with patch.object(
        subprocess, "run", side_effect=AssertionError("CLI must not run")
    ):
        out = discover_local(rp, "local")
    assert [r["name"] for r in out] == ["hello"]


def test_local_falls_back_on_editable_install(tmp_path: Path):
    rp = _venv(tmp_path)
    sp = tmp_path / "lib/python3.11/site-packages"
    (sp / "dev-0.1.dist-info").mkdir()
    (sp / "dev-0.1.dist-info" / "direct_url.json").write_text(
        '{"url": "file:///src", "dir_info": {"editable": true}}'
    )
    with patch.object(
        discovery, "_discover_local_cli", return_value=[{"name": "from-cli"}]
    ) as cli:
        out = discover_local(rp, "local")
    assert out == [{"name": "from-cli"}]
    cli.assert_called_once_with(rp, "local")


def test_local_falls_back_when_nothing_to_scan(tmp_path: Path):
    (tmp_path / "bin").mkdir()
    rp = str(tmp_path / "bin" / "runspec")
    with patch.object(discovery, "_discover_local_cli", return_value=[]) as cli:
        assert discover_local(rp, "local") == []
    cli.assert_called_once()


# ── the real shell command against a real layout ─────────────────────────────

_SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(not _SH, reason="needs sh")


@needs_sh
def test_scan_command_runs_under_sh_and_matches_local_scan(tmp_path: Path):
    rp = _venv(tmp_path)
    proc = subprocess.run(
        [_SH, "-c", _scan_command(rp)], capture_output=True, text=True, check=True
    )
    parsed = _parse_scan_output(proc.stdout)
    assert parsed is not None
    bin_names, editable, files = parsed
    assert bin_names == {"hello", "runspec"} and editable is False
    assert sorted(Path(p).parent.name for p, _ in files) == ["mypkg", "runspec"]
    owner, user = _parse_scan_identity(proc.stdout)
    assert (owner, user) == local_identity(rp)
    via_sh = _runnables_from_tomls(
        files, bin_names, "local", tmp_path.name, owner, user
    )
    assert via_sh == discover_local(rp, "local")


@needs_sh
def test_scan_command_reports_editable_under_sh(tmp_path: Path):
    rp = _venv(tmp_path)
    sp = tmp_path / "lib/python3.11/site-packages"
    (sp / "dev-0.1.dist-info").mkdir()
    (sp / "dev-0.1.dist-info" / "direct_url.json").write_text(
        '{"dir_info": {"editable": true}}'
    )
    proc = subprocess.run(
        [_SH, "-c", _scan_command(rp)], capture_output=True, text=True, check=True
    )
    parsed = _parse_scan_output(proc.stdout)
    assert parsed is not None and parsed[1] is True


@needs_sh
def test_scan_command_survives_spaces_in_paths(tmp_path: Path):
    root = tmp_path / "ops tools"
    root.mkdir()
    rp = _venv(root)
    proc = subprocess.run(
        [_SH, "-c", _scan_command(rp)], capture_output=True, text=True, check=True
    )
    parsed = _parse_scan_output(proc.stdout)
    assert parsed is not None
    assert any("ops tools" in p for p, _ in parsed[2])
    assert [
        r["name"] for r in _runnables_from_tomls(parsed[2], parsed[0], "l", "g")
    ] == ["hello"]


# ── "@owner" identity ─────────────────────────────────────────────────────────


def test_owner_account_rules():
    assert owner_account("1001", "svc") == "svc"
    assert owner_account("0", "root") == ""  # never escalate to root
    assert owner_account("4201", "UNKNOWN") == "4201"  # no passwd entry
    assert owner_account("", "") == ""


def test_parse_scan_identity():
    out = "=====OWNER\n1001 svc\n=====USER\nalice\n=====BIN\nhello\n=====END\n"
    assert _parse_scan_identity(out) == ("svc", "alice")
    # The identity lines don't leak into the bin section.
    assert _parse_scan_output(out)[0] == {"hello"}
    # An older reply (no preamble) ⇒ owner unknown.
    assert _parse_scan_identity("=====BIN\n=====END\n") == (None, "")


def test_scanned_runnables_carry_identity():
    files = [
        (
            "/v/lib/python3.12/site-packages/mypkg/runspec.toml",
            '[config]\nrun_as = ""\n\n[hello]\ndescription = "x"\n',
        )
    ]
    [r] = _runnables_from_tomls(files, {"hello"}, "prod-1", "ops", "svc", "alice")
    assert (r["configRunAs"], r["venvOwner"], r["loginUser"]) == ("", "svc", "alice")
