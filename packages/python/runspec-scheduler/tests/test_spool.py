"""The webhook spool: deliveries, the event stream, first-wins updates, mutes,
followers and pruning."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from runspec_scheduler import spool as sp


@pytest.fixture
def spool(tmp_path: Path) -> sp.Spool:
    s = sp.Spool(tmp_path / "hooks" / "spool.sqlite")
    yield s
    s.close()


def add(spool: sp.Spool, hook: str = "alerts", body: bytes = b'{"a": 1}', **kw: object) -> int:
    return spool.add_delivery(hook, source="10.0.0.1", content_type="application/json", headers={"user-agent": "t"}, body=body, **kw)  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_owner_only(spool: sp.Spool) -> None:
    assert stat.S_IMODE(spool.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(spool.path.stat().st_mode) == 0o600


def test_spool_path_for(tmp_path: Path) -> None:
    assert sp.spool_path_for(tmp_path / "runspec_scheduler.toml") == tmp_path / "hooks" / "spool.sqlite"


def test_add_get_queued(spool: sp.Spool) -> None:
    a = add(spool)
    b = add(spool, body=b"x" * 10)
    assert [r["id"] for r in spool.queued()] == [a, b]
    row = spool.get(a)
    assert row and row["state"] == sp.QUEUED and row["headers"] == {"user-agent": "t"} and row["size"] == 8
    assert spool.get(999) is None


def test_update_records_events_and_first_wins(spool: sp.Spool) -> None:
    d = add(spool)
    assert spool.update(d, kind="delivery", with_body=True, state=sp.PENDING, args={"x": [1, 2]})
    events = spool.events_since(0)
    assert events[-1]["kind"] == "delivery" and events[-1]["body"] == '{"a": 1}' and events[-1]["args"] == {"x": [1, 2]}
    # two deciders race: only the first conditional update changes the row
    won = spool.update(d, kind="approved", when=f"state = '{sp.PENDING}'", state=sp.APPROVED, decided_by="ann")
    lost = spool.update(d, kind="denied", when=f"state = '{sp.PENDING}'", state=sp.DENIED, decided_by="bob")
    assert (won, lost) == (True, False)
    row = spool.get(d)
    assert row and row["state"] == sp.APPROVED and row["decided_by"] == "ann"
    assert [e["kind"] for e in spool.events_since(0)] == ["delivery", "approved"]
    assert spool.update(d) is False  # nothing to change


def test_concurrent_writers(spool: sp.Spool) -> None:
    ids: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(20):
            i = add(spool)
            spool.update(i, state=sp.EVENT)
            with lock:
                ids.append(i)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 80 and spool.count(sp.EVENT) == 80


def test_body_cut_in_events_not_in_delivery(spool: sp.Spool) -> None:
    big = b"x" * (sp.EVENT_BODY_BYTES + 100)
    d = add(spool, body=big)
    spool.update(d, kind="delivery", with_body=True, state=sp.EVENT)
    event = spool.events_since(0)[-1]
    assert len(event["body"].encode()) <= sp.EVENT_BODY_BYTES + 100
    assert len(event["body"]) < len(big)
    row = spool.get(d)
    assert row and len(row["body"]) == len(big)


def test_dedup_and_bump(spool: sp.Spool) -> None:
    first = add(spool)
    spool.update(first, state=sp.PENDING, dedup_key="k")
    second = add(spool)
    found = spool.find_dedup("alerts", "k", time.time() - 60, second)
    assert found and found["id"] == first
    assert spool.find_dedup("alerts", "k", time.time() + 60, second) is None
    assert spool.find_dedup("other", "k", 0, second) is None
    spool.bump(first)
    row = spool.get(first)
    assert row and row["count"] == 2
    assert spool.events_since(0)[-1]["kind"] == "repeat"


def test_cursor_helpers(spool: sp.Spool) -> None:
    assert spool.last_event_id() == 0
    t0 = time.time()
    d = add(spool)
    spool.update(d, state=sp.EVENT)
    spool.update(d, state=sp.SKIPPED)
    assert spool.last_event_id() == 2
    assert spool.first_event_after(t0 - 1) == 0
    assert spool.first_event_after(time.time() + 10) == 2


def test_mutes_and_watching(spool: sp.Spool) -> None:
    now = time.time()
    spool.follower_seen("t1", "ann", 1)
    spool.follower_seen("t2", "bob", 2)
    assert spool.watching("alerts") == ["ann", "bob"]
    spool.mute("ann", "alerts", now + 600, "busy")
    spool.mute("bob", "*", now + 600)
    assert spool.is_muted("ann", "alerts") and not spool.is_muted("ann", "other")
    assert spool.watching("alerts") == []
    assert spool.watching("other") == ["ann"]
    assert {m["who"] for m in spool.mutes()} == {"ann", "bob"}
    assert spool.unmute("ann", "alerts") and not spool.unmute("ann", "alerts")
    assert spool.watching("alerts") == ["ann"]
    # "*" is the all-hooks mute itself, not a wildcard; None is all of them
    spool.mute("ann", "alerts", now + 600)
    spool.mute("ann", "*", now + 600)
    assert spool.unmute("ann", "*") and spool.is_muted("ann", "alerts")
    assert spool.unmute("ann", None) and not spool.is_muted("ann", "alerts")
    spool.follower_gone("t1")
    assert spool.watching("alerts") == []
    kinds = [e["kind"] for e in spool.events_since(0)]
    assert kinds == ["mute", "mute", "unmute", "mute", "mute", "unmute", "unmute"]


def test_stale_followers_dont_count(spool: sp.Spool) -> None:
    spool.follower_seen("t1", "ann", 1)
    assert spool.followers(time.time() + sp.FOLLOWER_STALE_SECONDS + 5) == []


def test_prune_keeps_unsettled(spool: sp.Spool) -> None:
    old = time.time() - 10 * 86400
    settled = add(spool, received_ts=old)
    spool.update(settled, state=sp.RAN)
    pending = add(spool, received_ts=old)
    spool.update(pending, state=sp.PENDING)
    fresh = add(spool)
    spool.update(fresh, state=sp.RAN)
    spool.mute("ann", "alerts", time.time() - 1)
    assert spool.prune(7 * 86400) == 1
    assert spool.get(settled) is None and spool.get(pending) and spool.get(fresh)
    assert spool.mutes() == []


def test_snapshot_never_has_headers(spool: sp.Spool) -> None:
    d = add(spool)
    row = spool.get(d)
    assert row
    snap = sp.snapshot(row, body=True)
    assert "headers" not in snap and snap["body"] == '{"a": 1}'
