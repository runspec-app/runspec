"""Smoke tests: each runnable parses args, calls its core helper, prints JSON.

The runnables are exercised end-to-end via their installed console scripts (the
same path `runspec serve` invokes), so this also proves the runspec.toml ships in
the package and resolves.
"""

import json
import shutil
import subprocess

import pytest


def _run(script: str, *args: str) -> dict:
    exe = shutil.which(script)
    if not exe:
        pytest.skip(f"{script} console script not installed")
    proc = subprocess.run([exe, *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_helpers_reexported() -> None:
    from runspec_logops_core import summarize_log as core_summarize

    from runspec_logops import build_bundle, map_trace_to_sources, summarize_log

    assert summarize_log is core_summarize
    assert callable(map_trace_to_sources) and callable(build_bundle)


def test_summarize_log_runnable(tmp_path) -> None:
    log = tmp_path / "app.log"
    log.write_text("\n".join(["2026-06-18T08:00:00Z ERROR NullPointerException: boom\n\tat com.acme.Svc.go(Svc.java:9)"] * 5 + ["2026-06-18T08:01:00Z INFO ok"]) + "\n")
    out = _run("summarize-log", "--file", str(log), "-n", "5")
    assert out["file"] == str(log)
    assert out["top"][0]["count"] == 5
    assert out["top"][0]["signature"].startswith("NullPointerException@")


def test_map_trace_runnable(tmp_path) -> None:
    (tmp_path / "app.py").write_text("\n".join(f"line {i}" for i in range(1, 30)) + "\n")
    trace = 'File "app.py", line 10, in handler'
    out = _run("map-trace", "--repo", str(tmp_path), "--trace", trace, "--context", "2")
    assert out["files_matched"] == 1
    assert out["frames"][0]["file"] == "app.py"


def test_bundle_digest_runnable(tmp_path) -> None:
    log = tmp_path / "app.log"
    log.write_text("2026-06-18T08:00:00Z ERROR ValueError: bad\n")
    dest = tmp_path / "out"
    out = _run("bundle-digest", "--file", str(log), "--destination", str(dest))
    assert out["destination"].endswith(".zip")
    assert "digest.json" in out["contents"]
