"""
test_analytics.py — Tests for the Analytics aggregation pipeline.

Covers three layers:
  1. The parser change: ``include_extra=True`` surfaces the rich run_summary
     fields, while the default output stays byte-identical to what the History
     tab consumes (no regression).
  2. The pure ``_aggregate_analytics`` fold: day bucketing, zero-filled gaps,
     success/failure split, percentiles, exception ranking, dimensions.
  3. ``Bridge.get_analytics`` end-to-end across a local host (real temp log
     files) and a remote host (patched ``ssh_run``), including the graceful
     ``partial``/``errors`` degradation when a host scan fails.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runspec_console.bridge import (
    _aggregate_analytics,
    _parse_log_text,
    _percentile,
)


def _summary_line(
    *,
    run_id: str,
    runnable: str = "backup",
    exit_code: int = 0,
    duration_ms: int = 800,
    ts: str | None = None,
    agent: bool = False,
    autonomy: str = "confirm",
    user: str = "alice",
    events: dict | None = None,
    exception: dict | None = None,
    command_path: list | None = None,
) -> str:
    ts = ts or datetime.now(timezone.utc).isoformat()
    extra = {
        "event": "run_summary",
        "run_id": run_id,
        "runnable": runnable,
        "command_path": command_path or [],
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "agent": agent,
        "autonomy": autonomy,
        "exception": exception,
        "events": events
        or {"DEBUG": 0, "INFO": 5, "WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        "user": user,
        "user_target": None,
        "invocation_args": {"keep": "7"},
        "args": {"keep": "7"},
        "arg_sources": {"keep": "cli"},
    }
    return json.dumps(
        {
            "ts": ts,
            "level": "INFO",
            "logger": "runspec.runsummary",
            "message": "run completed",
            "extra": extra,
        }
    )


def _info_line(run_id: str, message: str, ts: str | None = None) -> str:
    ts = ts or datetime.now(timezone.utc).isoformat()
    return json.dumps(
        {
            "ts": ts,
            "level": "INFO",
            "logger": "app",
            "message": message,
            "extra": {"run_id": run_id},
        }
    )


# ── 1. parser include_extra ──────────────────────────────────────────────────


class TestParserIncludeExtra(unittest.TestCase):
    def test_default_output_has_no_extra_key(self) -> None:
        text = "\n".join([_info_line("r1", "starting"), _summary_line(run_id="r1")])
        records = _parse_log_text("backup", text, "local")
        self.assertEqual(len(records), 1)
        self.assertNotIn("extra", records[0])

    def test_include_extra_is_additive_only(self) -> None:
        """include_extra=True must equal the lean record plus an 'extra' key."""
        text = "\n".join([_info_line("r1", "starting"), _summary_line(run_id="r1")])
        lean = _parse_log_text("backup", text, "local")[0]
        rich = _parse_log_text("backup", text, "local", include_extra=True)[0]
        rich_without_extra = {k: v for k, v in rich.items() if k != "extra"}
        self.assertEqual(lean, rich_without_extra)

    def test_extra_surfaces_rich_fields(self) -> None:
        text = _summary_line(
            run_id="r1",
            agent=True,
            autonomy="autonomous",
            command_path=["sub"],
            events={"DEBUG": 1, "INFO": 9, "WARNING": 2, "ERROR": 1, "CRITICAL": 0},
            exception={"type": "ValueError", "message": "bad input"},
        )
        rec = _parse_log_text("backup", text, "local", include_extra=True)[0]
        extra = rec["extra"]
        self.assertEqual(extra["autonomy"], "autonomous")
        self.assertTrue(extra["agent"])
        self.assertEqual(extra["command_path"], ["sub"])
        self.assertEqual(extra["events"]["WARNING"], 2)
        self.assertEqual(extra["exception"]["type"], "ValueError")
        # And the lean record still reflects the agent flag.
        self.assertEqual(rec["initiatedBy"], "llm")


# ── 2. percentile + aggregate fold ───────────────────────────────────────────


class TestPercentile(unittest.TestCase):
    def test_known_distribution(self) -> None:
        vals = [i * 100 for i in range(11)]  # 0,100,…,1000
        self.assertEqual(_percentile(vals, 50), 500)
        self.assertEqual(_percentile(vals, 95), 950)
        self.assertEqual(_percentile(vals, 0), 0)
        self.assertEqual(_percentile(vals, 100), 1000)

    def test_empty_and_single(self) -> None:
        self.assertEqual(_percentile([], 50), 0)
        self.assertEqual(_percentile([42], 95), 42)


def _rec(
    ts,
    *,
    ok=True,
    dur=800,
    host="local",
    group="ops",
    runnable="backup",
    operator="alice",
    initiated="user",
    autonomy="confirm",
    events=None,
    exception=None,
):
    r = {
        "ts": ts,
        "exitCode": 0 if ok else 1,
        "durationMs": dur,
        "host": host,
        "group": group,
        "runnable": runnable,
        "operator": operator,
        "initiatedBy": initiated,
        "extra": {"autonomy": autonomy, "events": events or {}, "exception": exception},
    }
    return r


class TestAggregate(unittest.TestCase):
    def test_buckets_daily_and_window(self) -> None:
        records = [
            _rec("2026-06-01T10:00:00Z", ok=True, dur=100),
            _rec("2026-06-01T11:00:00Z", ok=False, dur=300),
            _rec("2026-06-03T09:00:00Z", ok=True, dur=500, runnable="deploy"),
        ]
        out = _aggregate_analytics(records, "2026-06-01", "2026-06-03")
        self.assertEqual(out["totalRuns"], 3)
        self.assertEqual(out["successCount"], 2)
        self.assertEqual(out["failureCount"], 1)
        # Daily series zero-fills the gap day (06-02).
        self.assertEqual(
            [d["date"] for d in out["daily"]],
            ["2026-06-01", "2026-06-02", "2026-06-03"],
        )
        gap = next(d for d in out["daily"] if d["date"] == "2026-06-02")
        self.assertEqual(gap["total"], 0)
        day1 = next(d for d in out["daily"] if d["date"] == "2026-06-01")
        self.assertEqual((day1["success"], day1["failure"]), (1, 1))
        # The two 06-01 backup runs share a bucket key (same day/host/group/
        # runnable/operator/…) and merge; the 06-03 deploy is its own bucket.
        self.assertEqual(len(out["buckets"]), 2)
        backup_bucket = next(b for b in out["buckets"] if b["runnable"] == "backup")
        self.assertEqual(backup_bucket["total"], 2)
        self.assertEqual((backup_bucket["success"], backup_bucket["failure"]), (1, 1))

    def test_duration_stats_and_dimensions(self) -> None:
        records = [_rec(f"2026-06-01T0{i}:00:00Z", dur=i * 100) for i in range(0, 10)]
        out = _aggregate_analytics(records, "2026-06-01", "2026-06-01")
        self.assertEqual(out["durationMs"]["max"], 900)
        self.assertGreater(out["durationMs"]["p95"], out["durationMs"]["p50"])
        self.assertEqual(out["dimensions"]["runnables"], ["backup"])
        self.assertEqual(out["dimensions"]["initiatedBy"], ["user", "llm"])

    def test_exception_ranking(self) -> None:
        records = [
            _rec(
                "2026-06-01T01:00:00Z",
                ok=False,
                exception={"type": "HTTPError", "message": "401"},
                runnable="get-alerts",
            ),
            _rec(
                "2026-06-01T02:00:00Z",
                ok=False,
                exception={"type": "HTTPError", "message": "401"},
                runnable="get-alerts",
            ),
            _rec(
                "2026-06-01T03:00:00Z",
                ok=False,
                exception={"type": "TimeoutError", "message": "slow"},
                runnable="deploy",
            ),
        ]
        out = _aggregate_analytics(records, "2026-06-01", "2026-06-01")
        self.assertEqual(out["exceptions"][0]["count"], 2)
        self.assertEqual(out["exceptions"][0]["exception"], "HTTPError: 401")
        self.assertEqual(out["exceptions"][0]["lastTs"], "2026-06-01T02:00:00Z")

    def test_event_counts_accumulate_in_bucket(self) -> None:
        records = [
            _rec("2026-06-01T01:00:00Z", events={"WARNING": 2, "ERROR": 1}),
            _rec("2026-06-01T02:00:00Z", events={"WARNING": 3, "ERROR": 0}),
        ]
        out = _aggregate_analytics(records, "2026-06-01", "2026-06-01")
        # Same bucket key (same day/host/group/runnable/operator/…) → merged.
        self.assertEqual(len(out["buckets"]), 1)
        self.assertEqual(out["buckets"][0]["events"]["WARNING"], 5)
        self.assertEqual(out["buckets"][0]["events"]["ERROR"], 1)

    def test_partial_and_errors_passthrough(self) -> None:
        out = _aggregate_analytics(
            [],
            "2026-06-01",
            "2026-06-01",
            partial=True,
            errors=[{"host": "prod-2", "message": "boom"}],
        )
        self.assertTrue(out["partial"])
        self.assertEqual(out["errors"][0]["host"], "prod-2")


# ── 3. Bridge.get_analytics end-to-end ───────────────────────────────────────


def _make_bridge():
    with (
        patch("runspec_console.bridge.load_hosts", return_value=[]),
        patch.object(
            __import__("runspec_console.bridge", fromlist=["Bridge"]).Bridge,
            "_start_refresh_watcher",
        ),
    ):
        from runspec_console.bridge import Bridge

        return Bridge()


class TestCollectLocal(unittest.TestCase):
    def test_local_logs_tagged_with_group(self) -> None:
        b = _make_bridge()
        with TemporaryDirectory() as td:
            venv = Path(td) / "ops-tools"
            (venv / "bin").mkdir(parents=True)
            (venv / "logs").mkdir()
            bin_path = venv / "bin" / "runspec"
            now = datetime.now(timezone.utc).isoformat()
            (venv / "logs" / "backup.log").write_text(
                _summary_line(run_id="r1", runnable="backup", ts=now), encoding="utf-8"
            )
            entry = {"name": "local", "runspec_paths": [str(bin_path)]}
            recs = b._collect_host_records(entry, None)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["group"], "ops-tools")
        self.assertIn("extra", recs[0])  # rich fields retained


class TestGetAnalyticsRemote(unittest.TestCase):
    def test_remote_merge_and_partial(self) -> None:
        b = _make_bridge()
        now = datetime.now(timezone.utc).isoformat()
        good_entry = {
            "name": "prod-1",
            "ssh": "u@h1",
            "runspec_paths": ["/opt/v/bin/runspec"],
        }
        bad_entry = {
            "name": "prod-2",
            "ssh": "u@h2",
            "runspec_paths": ["/opt/v/bin/runspec"],
        }
        b._hosts = [good_entry, bad_entry]

        framed = "\x00RUNSPEC_LOG:backup\n" + _summary_line(
            run_id="r1", runnable="backup", ts=now
        )

        def fake_ssh_run(ssh, *a, **kw):
            if ssh == "u@h1":
                return (0, framed, "")
            return (255, "", "connection refused")

        with (
            patch("runspec_console.executor.ssh_run", side_effect=fake_ssh_run),
            patch.object(b, "get_config", return_value={}),
        ):
            out = b.get_analytics(["prod-1", "prod-2"], since_days=30)

        self.assertEqual(out["totalRuns"], 1)
        self.assertTrue(out["partial"])
        self.assertEqual(out["errors"][0]["host"], "prod-2")
        # Remote group derived from the venv path (parent.parent name).
        self.assertEqual(out["buckets"][0]["group"], "v")

    def test_window_filters_old_records(self) -> None:
        b = _make_bridge()
        old = "2000-01-01T00:00:00+00:00"
        entry = {
            "name": "prod-1",
            "ssh": "u@h1",
            "runspec_paths": ["/opt/v/bin/runspec"],
        }
        b._hosts = [entry]
        framed = "\x00RUNSPEC_LOG:backup\n" + _summary_line(run_id="r1", ts=old)
        with (
            patch("runspec_console.executor.ssh_run", return_value=(0, framed, "")),
            patch.object(b, "get_config", return_value={}),
        ):
            out = b.get_analytics(["prod-1"], since_days=30)
        self.assertEqual(out["totalRuns"], 0)


if __name__ == "__main__":
    unittest.main()
