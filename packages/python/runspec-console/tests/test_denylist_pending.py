"""Kill-switch blocks queued for disconnected hosts (denylist_pending.py)."""

from __future__ import annotations

from pathlib import Path

from runspec_console import denylist_pending as dp


def test_add_read_remove(tmp_path: Path) -> None:
    assert dp.read_pending(tmp_path) == []
    dp.add_pending(tmp_path, ["bastion", "uat-db-01"], "incident 42")
    got = dp.read_pending(tmp_path)
    assert [e["host"] for e in got] == ["bastion", "uat-db-01"]
    assert got[0]["reason"] == "incident 42" and got[0]["since"] > 0

    # Re-queueing keeps the first time and takes the newer reason; no duplicate.
    first = got[0]["since"]
    dp.add_pending(tmp_path, ["bastion", ""], "incident 43")
    got = dp.read_pending(tmp_path)
    assert [e["host"] for e in got] == ["bastion", "uat-db-01"]
    assert got[0]["since"] == first and got[0]["reason"] == "incident 43"

    dp.remove_pending(tmp_path, "bastion")
    assert [e["host"] for e in dp.read_pending(tmp_path)] == ["uat-db-01"]
    dp.remove_pending(tmp_path)
    assert dp.read_pending(tmp_path) == []


def test_unreadable_file_is_empty(tmp_path: Path) -> None:
    (tmp_path / dp.FILE_NAME).write_text("not json", encoding="utf-8")
    assert dp.read_pending(tmp_path) == []
    (tmp_path / dp.FILE_NAME).write_text(
        '[{"nohost": 1}, {"host": "a"}]', encoding="utf-8"
    )
    assert [e["host"] for e in dp.read_pending(tmp_path)] == ["a"]
