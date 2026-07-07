"""
test_workspaces.py — unit tests for the per-session workspace sandbox.

The workspace module holds pure helpers behind the ``workspace_*`` agent tools.
Every function operates against a filesystem path, so these tests exercise the
jail (path-escape rejection), the spill helper (envelope shape + on-disk file),
the read/head/tail windowing, the grep hit cap, ``workspace_write``'s refusal
to touch ``tool-results/``, and the orphan sweep.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from runspec_console import workspaces


# ── jail ──────────────────────────────────────────────────────────────────────


def test_jail_accepts_relative_inside(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    target = workspaces.jail(tmp_path, "notes")
    assert target == (tmp_path / "notes").resolve()


def test_jail_accepts_empty(tmp_path: Path) -> None:
    assert workspaces.jail(tmp_path, "") == tmp_path.resolve()
    assert workspaces.jail(tmp_path, ".") == tmp_path.resolve()


def test_jail_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        workspaces.jail(tmp_path, "../etc/passwd")


def test_jail_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        workspaces.jail(tmp_path, "/etc/passwd")


def test_jail_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escape.txt"
    outside.write_text("secret")
    link = tmp_path / "link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(ValueError):
        workspaces.jail(tmp_path, "link")


# ── spill_output ──────────────────────────────────────────────────────────────


def test_spill_output_writes_file_and_returns_envelope(tmp_path: Path) -> None:
    payload = "line-1\nline-2\n" + ("x" * 500_000)
    envelope, rel_path = workspaces.spill_output(
        tmp_path, "linux__list-processes", "abc123deadbe", payload, head_bytes=64
    )

    # Envelope names the path and includes a head preview + size.
    assert rel_path.startswith("tool-results/")
    assert rel_path.endswith(".txt")  # not JSON, so .txt
    # Tool name is preserved verbatim in the filename (underscores are safe).
    assert rel_path.split("/")[-1].startswith("linux__list-processes-abc123deadbe")
    assert rel_path in envelope
    assert "line-1" in envelope  # head preview present
    assert "workspace_read" in envelope

    # File on disk carries the full payload uncapped.
    on_disk = (tmp_path / rel_path).read_text(encoding="utf-8")
    assert on_disk == payload


def test_spill_output_detects_json(tmp_path: Path) -> None:
    payload = json.dumps({"processes": [{"pid": i} for i in range(1000)]})
    _envelope, rel_path = workspaces.spill_output(tmp_path, "ps-tool", "run01", payload)
    assert rel_path.endswith(".json")


def test_spill_output_sanitises_tool_name(tmp_path: Path) -> None:
    # Colons/slashes in a tool name (e.g. an MCP-style "server:tool") must not
    # escape the tool-results/ dir.
    _envelope, rel_path = workspaces.spill_output(
        tmp_path, "mcp:some/tool", "run02", "hello"
    )
    filename = rel_path.split("/")[-1]
    assert "/" not in filename
    assert ":" not in filename


# ── list_dir ──────────────────────────────────────────────────────────────────


def test_list_dir_empty_root_returns_empty_list(tmp_path: Path) -> None:
    # A fresh workspace has never been written to — ls should not raise.
    assert workspaces.list_dir(tmp_path, "") == []


def test_list_dir_returns_rows_with_size_and_mtime(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("hi")
    (tmp_path / "b.txt").write_text("bye")
    rows = workspaces.list_dir(tmp_path, "")
    kinds = {(r["name"], r["type"]) for r in rows}
    assert kinds == {("notes", "dir"), ("b.txt", "file")}
    file_row = next(r for r in rows if r["name"] == "b.txt")
    assert file_row["size"] == 3
    assert isinstance(file_row["modified"], int) and file_row["modified"] > 0


def test_list_dir_rejects_file_target(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    with pytest.raises(NotADirectoryError):
        workspaces.list_dir(tmp_path, "a.txt")


# ── read_file ─────────────────────────────────────────────────────────────────


def test_read_file_windows_by_offset_and_limit(tmp_path: Path) -> None:
    lines = [f"line-{i}" for i in range(50)]
    (tmp_path / "log.txt").write_text("\n".join(lines))
    result = workspaces.read_file(tmp_path, "log.txt", offset=10, limit=5)
    assert result["offset"] == 10
    assert result["limit"] == 5
    assert result["total_lines"] == 50
    assert result["truncated"] is True
    assert result["content"].splitlines() == lines[10:15]


def test_read_file_untruncated_flag(tmp_path: Path) -> None:
    (tmp_path / "short.txt").write_text("only\nfour\nsmall\nlines")
    result = workspaces.read_file(tmp_path, "short.txt", offset=0, limit=200)
    assert result["truncated"] is False
    assert result["total_lines"] == 4


def test_read_file_rejects_binary(tmp_path: Path) -> None:
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02data")
    with pytest.raises(ValueError):
        workspaces.read_file(tmp_path, "b.bin")


def test_read_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        workspaces.read_file(tmp_path, "nope.txt")


# ── head / tail ───────────────────────────────────────────────────────────────


def test_head_and_tail(tmp_path: Path) -> None:
    lines = [f"line-{i}" for i in range(100)]
    (tmp_path / "log.txt").write_text("\n".join(lines))
    head_result = workspaces.head(tmp_path, "log.txt", lines=3)
    assert head_result["content"].splitlines() == ["line-0", "line-1", "line-2"]
    tail_result = workspaces.tail(tmp_path, "log.txt", lines=3)
    assert tail_result["content"].splitlines() == [
        "line-97",
        "line-98",
        "line-99",
    ]


# ── grep ──────────────────────────────────────────────────────────────────────


def test_grep_returns_matches_with_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "app.log").write_text(
        "\n".join(
            [
                "INFO started",
                "ERROR db timeout",
                "INFO recovered",
                "ERROR queue full",
            ]
        )
    )
    hits = workspaces.grep(tmp_path, r"ERROR")
    assert len(hits) == 2
    assert hits[0] == {"path": "app.log", "line": 2, "text": "ERROR db timeout"}
    assert hits[1]["line"] == 4


def test_grep_respects_max_results_cap(tmp_path: Path) -> None:
    (tmp_path / "log.txt").write_text("\n".join(["MATCH"] * 500))
    hits = workspaces.grep(tmp_path, r"MATCH", max_results=10)
    assert len(hits) == 10


def test_grep_context_lines(tmp_path: Path) -> None:
    (tmp_path / "log.txt").write_text("before-2\nbefore-1\nHIT here\nafter-1\nafter-2")
    hits = workspaces.grep(tmp_path, r"HIT", context=1)
    assert hits[0]["before"] == ["before-1"]
    assert hits[0]["after"] == ["after-1"]


def test_grep_skips_binary_file(tmp_path: Path) -> None:
    (tmp_path / "text.txt").write_text("HIT")
    (tmp_path / "bin.bin").write_bytes(b"\x00\x00HIT\x00")
    hits = workspaces.grep(tmp_path, r"HIT")
    assert [h["path"] for h in hits] == ["text.txt"]


# ── write_file ────────────────────────────────────────────────────────────────


def test_write_file_creates_notes(tmp_path: Path) -> None:
    result = workspaces.write_file(tmp_path, "notes/summary.md", "# hello")
    assert (tmp_path / "notes" / "summary.md").read_text() == "# hello"
    assert result["created"] is True
    assert result["path"] == "notes/summary.md"


def test_write_file_refuses_tool_results(tmp_path: Path) -> None:
    # The system owns tool-results/ — the model must not be able to overwrite
    # a spilled runnable output with something arbitrary.
    with pytest.raises(PermissionError):
        workspaces.write_file(tmp_path, "tool-results/faked.txt", "not the real output")


def test_write_file_refuses_empty_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        workspaces.write_file(tmp_path, "", "content")


def test_write_file_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        workspaces.write_file(tmp_path, "../evil.txt", "no")


def test_write_file_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        workspaces.write_file(tmp_path, "/etc/passwd", "no")


# ── sweep_orphans ─────────────────────────────────────────────────────────────


def test_sweep_drops_chat_ws_when_conv_gone(tmp_path: Path) -> None:
    (tmp_path / "aliveconv").mkdir()
    (tmp_path / "aliveconv" / "marker").write_text("x")
    (tmp_path / "deadconv").mkdir()
    (tmp_path / "deadconv" / "marker").write_text("x")

    removed = workspaces.sweep_orphans(
        tmp_path, alive_chat_ids={"aliveconv"}, headless_retention_days=7
    )
    assert removed == 1
    assert (tmp_path / "aliveconv").exists()
    assert not (tmp_path / "deadconv").exists()


def test_sweep_keeps_recent_headless(tmp_path: Path) -> None:
    (tmp_path / "trigger-recent").mkdir()
    (tmp_path / "trigger-recent" / "marker").write_text("x")
    removed = workspaces.sweep_orphans(
        tmp_path, alive_chat_ids=set(), headless_retention_days=7
    )
    assert removed == 0
    assert (tmp_path / "trigger-recent").exists()


def test_sweep_drops_old_headless(tmp_path: Path) -> None:
    old = tmp_path / "schedule-old"
    old.mkdir()
    (old / "marker").write_text("x")
    # Push mtime 30 days back.
    ancient = time.time() - 30 * 86400
    import os

    os.utime(old, (ancient, ancient))

    removed = workspaces.sweep_orphans(
        tmp_path, alive_chat_ids=set(), headless_retention_days=7
    )
    assert removed == 1
    assert not old.exists()


def test_sweep_missing_root_is_zero(tmp_path: Path) -> None:
    assert (
        workspaces.sweep_orphans(
            tmp_path / "does-not-exist",
            alive_chat_ids=set(),
            headless_retention_days=7,
        )
        == 0
    )


# ── safe_id ───────────────────────────────────────────────────────────────────


def test_safe_id_keeps_alphanumeric(tmp_path: Path) -> None:
    assert workspaces._safe_id("abc_DEF-123") == "abc_DEF-123"


def test_safe_id_strips_dangerous_chars() -> None:
    assert workspaces._safe_id("../evil/path") == "evilpath"
    assert workspaces._safe_id("a:b") == "ab"
