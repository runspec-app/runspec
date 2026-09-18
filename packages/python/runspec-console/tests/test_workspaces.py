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


def test_jail_treats_backslash_as_separator(tmp_path: Path) -> None:
    # A Windows-minded agent's `notes\a.txt` means a nested path in every tool,
    # not only write_file (audit #10). A leading separator is still absolute.
    (tmp_path / "notes").mkdir()
    assert workspaces.jail(tmp_path, "notes\\a.txt") == (tmp_path / "notes" / "a.txt")
    with pytest.raises(ValueError):
        workspaces.jail(tmp_path, "\\etc\\passwd")


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


def test_spill_output_json_object_shows_shape_not_byte_prefix(tmp_path: Path) -> None:
    # A minified JSON object: the envelope must carry a structural shape summary
    # (naming the fields) instead of a mangled raw byte prefix.
    payload = json.dumps({"status": "ok", "count": 3, "host": "web01"})
    envelope, rel_path = workspaces.spill_output(tmp_path, "probe", "r1", payload)
    assert rel_path.endswith(".json")
    assert "JSON object" in envelope
    assert "status: str" in envelope and "count: int" in envelope
    # The raw file is still the verbatim record.
    assert (tmp_path / rel_path).read_text() == payload


def test_spill_output_json_object_points_at_json_get_with_largest_field(
    tmp_path: Path,
) -> None:
    # A minified object is one line, so the line tools can't page it. The envelope
    # must name workspace_json_get and the dominant string field as the pointer —
    # so the model's next call is one autonomous read, not a confirm-gated
    # run_python_sandboxed doing json.load for the same field.
    payload = json.dumps({"status": "ok", "body": "x" * 50_000, "note": "short"})
    envelope, rel_path = workspaces.spill_output(tmp_path, "probe", "r1", payload)
    assert f"workspace_json_get(path={rel_path!r}, pointer='body')" in envelope
    assert "49 KB" in envelope  # the field's size, so the model knows it's the big one
    assert "Prefer that over run_python_sandboxed" in envelope


def test_spill_output_json_object_dotted_key_uses_json_pointer(tmp_path: Path) -> None:
    # A key containing '.' can't be addressed as a dotted path — the hint spells
    # it as a JSON Pointer so the suggested call works verbatim.
    payload = json.dumps({"a.b": "y" * 100, "c": "z"})
    envelope, rel_path = workspaces.spill_output(tmp_path, "probe", "r1", payload)
    assert f"workspace_json_get(path={rel_path!r}, pointer='/a.b')" in envelope
    got = workspaces.json_get(tmp_path, rel_path, "/a.b")
    assert "y" * 100 in json.dumps(got)


def test_spill_output_json_object_without_string_field_still_names_json_get(
    tmp_path: Path,
) -> None:
    payload = json.dumps({"count": 3, "items": list(range(10))})
    envelope, rel_path = workspaces.spill_output(tmp_path, "probe", "r1", payload)
    assert "workspace_json_get" in envelope
    assert "pointer='<dotted.path>'" in envelope


def test_spill_output_json_array_has_no_object_hint(tmp_path: Path) -> None:
    # An array gets the .jsonl companion + line-tool advice, not the object hint.
    payload = json.dumps([{"a": 1}, {"a": 2}])
    envelope, _ = workspaces.spill_output(tmp_path, "probe", "r1", payload)
    assert "This is a JSON object" not in envelope
    assert ".jsonl" in envelope


def test_spill_output_text_has_no_object_hint(tmp_path: Path) -> None:
    envelope, _ = workspaces.spill_output(tmp_path, "probe", "r1", "plain\nlines\n")
    assert "workspace_json_get" not in envelope


def test_spill_output_json_array_writes_jsonl_companion(tmp_path: Path) -> None:
    # A top-level JSON array gets a line-per-record .jsonl companion so the line
    # tools work on it, plus an array shape summary listing the record fields.
    rows = [{"pid": i, "name": f"proc{i}", "cpu": 0.1 * i} for i in range(5)]
    payload = json.dumps(rows)
    envelope, rel_path = workspaces.spill_output(tmp_path, "ps", "r2", payload)

    assert "JSON array" in envelope and "array[5] of" in envelope
    assert "pid: int" in envelope and "name: str" in envelope
    # Companion written and referenced.
    jsonl = (
        tmp_path
        / "tool-results"
        / (rel_path.split("/")[-1].rsplit(".", 1)[0] + ".jsonl")
    )
    assert jsonl.exists()
    lines = jsonl.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0]) == rows[0]
    assert jsonl.name in envelope
    # The line tools now work record-by-record on the companion.
    hits = workspaces.grep(tmp_path, r"proc3", glob="*.jsonl")
    assert len(hits) == 1 and json.loads(hits[0]["text"])["pid"] == 3


def test_spill_output_non_json_keeps_head_preview(tmp_path: Path) -> None:
    # A plain log keeps the raw head-slice preview (shape summary is JSON-only)
    # and gets no companion.
    payload = "ERROR boom\n" + ("noise line\n" * 5000)
    envelope, rel_path = workspaces.spill_output(
        tmp_path, "log", "r3", payload, head_bytes=64
    )
    assert rel_path.endswith(".txt")
    assert "ERROR boom" in envelope
    assert "JSON" not in envelope
    assert not list((tmp_path / "tool-results").glob("*.jsonl"))


# ── JSON shape helpers ─────────────────────────────────────────────────────────


def test_json_shape_object_lists_keys_with_types() -> None:
    shape = workspaces._json_shape({"a": 1, "b": "x", "c": True, "d": None})
    assert shape == "{a: int, b: str, c: bool, d: null}"


def test_json_shape_array_of_records_unions_fields() -> None:
    value = [{"pid": 1, "name": "a"}, {"pid": 2, "name": "b", "extra": 9}]
    # length + the union of sampled record fields (first-seen type)
    assert (
        workspaces._json_shape(value) == "array[2] of {pid: int, name: str, extra: int}"
    )


def test_json_shape_empty_and_scalar_array() -> None:
    assert workspaces._json_shape([]) == "array[0]"
    assert workspaces._json_shape([1, 2, 3]) == "array[3] of int"


def test_json_shape_depth_is_bounded() -> None:
    # Beyond the depth cap, nested structure collapses to a summary (no runaway).
    deep = {"lvl1": {"lvl2": {"lvl3": {"lvl4": 1}}}}
    shape = workspaces._json_shape(deep)
    assert "lvl3" not in shape  # depth-capped before reaching lvl3's contents
    assert "object(" in shape


def test_parse_json_bounded_rejects_non_json_and_oversize() -> None:
    assert workspaces._parse_json_bounded("not json") == (None, False)
    assert workspaces._parse_json_bounded("") == (None, False)
    assert workspaces._parse_json_bounded('{"a": 1}') == ({"a": 1}, True)
    # A plain scalar/log line is not object/array-rooted → not treated as JSON.
    assert workspaces._parse_json_bounded("42") == (None, False)


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


def test_list_dir_sort_by_modified_newest_first(tmp_path: Path) -> None:
    import os

    for i, nm in enumerate(["old.json", "mid.json", "new.json"]):
        p = tmp_path / nm
        p.write_text("x")
        os.utime(p, (1_000 + i * 100, 1_000 + i * 100))  # old < mid < new
    names = [r["name"] for r in workspaces.list_dir(tmp_path, "", sort="modified")]
    assert names == ["new.json", "mid.json", "old.json"]
    # default is still alphabetical
    alpha = [r["name"] for r in workspaces.list_dir(tmp_path, "")]
    assert alpha == ["mid.json", "new.json", "old.json"]


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


def test_read_file_pages_past_the_first_512kb(tmp_path: Path) -> None:
    # A window deep in a multi-MB file must be reachable, and total_lines must be
    # the real count — not just what fit in the first read chunk (audit #2/#3).
    lines = [f"line-{i:07d} " + ("x" * 200) for i in range(30_000)]  # ~6 MB
    (tmp_path / "big.log").write_text("\n".join(lines))
    result = workspaces.read_file(tmp_path, "big.log", offset=25_000, limit=3)
    assert result["content"].splitlines() == lines[25_000:25_003]
    assert result["total_lines"] == 30_000
    assert result["total_lines_exact"] is True
    assert result["truncated"] is True  # more lines after the window


def test_read_file_normalises_crlf(tmp_path: Path) -> None:
    (tmp_path / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    result = workspaces.read_file(tmp_path, "crlf.txt", offset=0, limit=10)
    assert result["content"] == "a\nb\nc"
    assert result["total_lines"] == 3


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


def test_tail_huge_single_line_file_is_not_empty(tmp_path: Path) -> None:
    # A minified JSON dump with no newline in the tail window used to come back
    # empty (the lone line was dropped as "partial"); now it returns the tail
    # fragment (audit #4).
    (tmp_path / "big.json").write_bytes(b"{" + b"a" * 1_000_000 + b"}")
    result = workspaces.tail(tmp_path, "big.json", lines=3)
    assert result["content"]  # non-empty
    assert result["truncated"] is True
    assert result["content"].endswith("}")


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


def test_grep_line_numbers_ignore_exotic_and_crlf(tmp_path: Path) -> None:
    # A form-feed (\x0c) inside a value must NOT split a line (audit #8), so the
    # match stays on the real newline-based line number; CRLF is normalised.
    (tmp_path / "app.log").write_bytes(b"one\r\ntwo\x0cstill-two\r\nERROR here\r\n")
    hits = workspaces.grep(tmp_path, r"ERROR")
    assert len(hits) == 1
    assert hits[0]["line"] == 3  # not inflated by the \x0c on line 2
    assert "\r" not in hits[0]["text"]


def test_split_lines_only_breaks_on_newline() -> None:
    # str.splitlines() would return 3 here; we only split on \n (+ strip \r).
    assert workspaces._split_lines("a\x0cb\x85c") == ["a\x0cb\x85c"]
    assert workspaces._split_lines("a\r\nb\r\n") == ["a", "b"]
    assert workspaces._split_lines("a\nb") == ["a", "b"]
    assert workspaces._split_lines("") == []


def test_grep_ignore_case_folds_case(tmp_path: Path) -> None:
    (tmp_path / "app.log").write_text("Error one\nERROR two\nerror three")
    assert len(workspaces.grep(tmp_path, r"error")) == 1  # case-sensitive default
    assert len(workspaces.grep(tmp_path, r"error", ignore_case=True)) == 3


def test_grep_fixed_matches_metacharacters_literally(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("price[0] literal here")
    # as a regex, [0] is a char class → does not match the literal text
    assert workspaces.grep(tmp_path, "price[0]") == []
    # fixed=True escapes the metacharacters and matches
    hit = workspaces.grep(tmp_path, "price[0]", fixed=True)
    assert hit and hit[0]["line"] == 1


def test_grep_glob_restricts_scanned_files(tmp_path: Path) -> None:
    (tmp_path / "tool-results").mkdir()
    (tmp_path / "tool-results" / "a.json").write_text("HIT json")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "b.md").write_text("HIT markdown")
    # bare-name and path globs both work; comma-separated list is allowed
    only_json = workspaces.grep(tmp_path, r"HIT", glob="*.json")
    assert [h["path"] for h in only_json] == ["tool-results/a.json"]
    scoped = workspaces.grep(tmp_path, r"HIT", glob="tool-results/*.json,*.md")
    assert {h["path"] for h in scoped} == {"tool-results/a.json", "notes/b.md"}


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


def test_write_file_refuses_tool_results_via_traversal(tmp_path: Path) -> None:
    # A `..` re-entry must not defeat the reserved-dir guard (audit #1): the
    # resolved target still lands in tool-results/, so it must be refused and no
    # file must be written there.
    (tmp_path / "tool-results").mkdir()
    (tmp_path / "tool-results" / "real.json").write_text("REAL")
    with pytest.raises(PermissionError):
        workspaces.write_file(tmp_path, "notes/../tool-results/real.json", "FAKE")
    assert (tmp_path / "tool-results" / "real.json").read_text() == "REAL"


def test_write_file_refuses_tool_results_case_insensitively(tmp_path: Path) -> None:
    # A case variant must not slip past on a case-insensitive filesystem (audit
    # #6). We assert the guard rejects the name regardless of the host FS.
    with pytest.raises(PermissionError):
        workspaces.write_file(tmp_path, "Tool-Results/x.json", "FAKE")


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


# ── workspace_grep dispatch (option pass-through + truncation flag) ─────────────


def test_workspace_grep_dispatch_options_and_truncation(monkeypatch, tmp_path) -> None:
    """The handler forwards ignore_case/fixed/glob to workspaces.grep and marks
    the payload 'truncated' once the row count reaches the effective cap — so the
    agent knows to narrow rather than assume it saw everything (parity with
    repo_grep)."""
    import threading

    from runspec_console.bridge import Bridge

    class _WsBridge:
        _run_workspace_tool = Bridge._run_workspace_tool
        _current_workspace_root = Bridge._current_workspace_root

        def __init__(self):
            self._lock = threading.Lock()
            self._conversation_id = "wsconv01"
            self._workspace_by_chat = {}

    monkeypatch.setattr(
        "runspec_console.config.workspace_dir", lambda sid: tmp_path / sid
    )
    seen: dict = {}

    def fake_grep(
        root, pattern, *, rel, max_results, context, ignore_case, fixed, glob
    ):
        seen.update(
            pattern=pattern,
            ignore_case=ignore_case,
            fixed=fixed,
            glob=glob,
            max_results=max_results,
        )
        return [
            {"path": "tool-results/x.json", "line": i, "text": "x"}
            for i in range(max_results)
        ]

    monkeypatch.setattr(workspaces, "grep", fake_grep)
    out = _WsBridge()._run_workspace_tool(
        "workspace_grep",
        {
            "pattern": "price[0]",
            "ignore_case": True,
            "fixed": True,
            "glob": "tool-results/*.json",
            "max_results": 3,
        },
    )
    data = json.loads(out)
    assert seen == {
        "pattern": "price[0]",
        "ignore_case": True,
        "fixed": True,
        "glob": "tool-results/*.json",
        "max_results": 3,
    }
    assert data["count"] == 3 and data["truncated"] is True and "Narrow" in data["hint"]
