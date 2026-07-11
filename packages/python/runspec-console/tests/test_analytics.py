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

import gzip
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runspec_console.bridge_utils import (
    _aggregate_analytics,
    _log_globs,
    _parse_log,
    _parse_log_text,
    _percentile,
    _remote_log_script,
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
    trigger: str = "",
    runbook: str = "",
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
        "trigger": trigger,
        "runbook": runbook,
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
        # The subcommand path surfaces on the lean record so a History rerun can
        # reconstruct the original subcommand invocation.
        self.assertEqual(rec["commandPath"], ["sub"])

    def test_command_path_defaults_empty(self) -> None:
        rec = _parse_log_text("backup", _summary_line(run_id="r1"), "local")[0]
        self.assertEqual(rec["commandPath"], [])

    def test_trigger_provenance_surfaced(self) -> None:
        # A scheduled run that is also an LLM turn: both axes are independent and
        # both surface (lean record + rich extra).
        text = _summary_line(run_id="r1", agent=True, trigger="schedule")
        rec = _parse_log_text("backup", text, "local", include_extra=True)[0]
        self.assertEqual(rec["trigger"], "schedule")
        self.assertEqual(rec["initiatedBy"], "llm")
        self.assertEqual(rec["extra"]["trigger"], "schedule")

    def test_trigger_defaults_empty_for_manual_run(self) -> None:
        rec = _parse_log_text("backup", _summary_line(run_id="r1"), "local")[0]
        self.assertEqual(rec["trigger"], "")

    def test_runbook_provenance_surfaced(self) -> None:
        # The saved runbook that drove the run surfaces on the lean record and in extra.
        text = _summary_line(run_id="r1", runbook="fetch-and-process-log")
        rec = _parse_log_text("backup", text, "local", include_extra=True)[0]
        self.assertEqual(rec["runbook"], "fetch-and-process-log")
        self.assertEqual(rec["extra"]["runbook"], "fetch-and-process-log")

    def test_runbook_defaults_empty_for_manual_run(self) -> None:
        rec = _parse_log_text("backup", _summary_line(run_id="r1"), "local")[0]
        self.assertEqual(rec["runbook"], "")


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
    runbook="",
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
        "runbook": runbook,
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

    def test_runbook_dimension_and_bucket(self) -> None:
        # Runs driven by a runbook split into their own bucket and the runbook name
        # surfaces in the dimensions so the Analytics tab can filter by it.
        records = [
            _rec("2026-06-01T01:00:00Z", runbook="fetch-log"),
            _rec("2026-06-01T02:00:00Z", runbook="fetch-log"),
            _rec("2026-06-01T03:00:00Z"),  # manual — no runbook
        ]
        out = _aggregate_analytics(records, "2026-06-01", "2026-06-01")
        self.assertEqual(out["dimensions"]["runbooks"], ["fetch-log"])
        runbook_bucket = next(b for b in out["buckets"] if b["runbook"] == "fetch-log")
        self.assertEqual(runbook_bucket["total"], 2)

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
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(
            __import__("runspec_console.bridge", fromlist=["Bridge"]).Bridge,
            "_start_refresh_watcher",
        ),
    ):
        from runspec_console.bridge import Bridge

        b = Bridge()
    # Exercise the legacy connect-per-call path these tests mock via
    # executor.ssh_run; the pooled path is covered by test_ssh_pool.py.
    b._ssh_pool = None
    return b


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
            # The record lives only in the venv logs dir; the `logs status` query
            # and the $HOME/logs scan return nothing (so the same record isn't
            # double-counted now that the scan covers several dirs).
            script = a[0] if a else ""
            if ssh == "u@h1":
                return (0, framed, "") if "/opt/v/logs/*.log" in script else (0, "", "")
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


# ── 4. Per-invocation files (store="per-run") ────────────────────────────────


def _exc_line(
    run_id: str,
    *,
    exc_type: str = "ValueError",
    message: str = "bad tag",
    module: str = "release",
    frames: list | None = None,
    ts: str | None = None,
) -> str:
    """A runspec.exception record (runspec >=0.26) carrying structured frames."""
    ts = ts or datetime.now(timezone.utc).isoformat()
    return json.dumps(
        {
            "ts": ts,
            "level": "CRITICAL",
            "logger": "runspec.exception",
            "message": "uncaught exception",
            "exc": "Traceback (most recent call last):\n  ...",
            "exc_structured": {
                "type": exc_type,
                "message": message,
                "module": module,
                "frames": frames
                or [
                    {
                        "file": "/abs/deploy.py",
                        "line": 48,
                        "func": "main",
                        "code": "main()",
                    },
                    {
                        "file": "/abs/release.py",
                        "line": 212,
                        "func": "go",
                        "code": "raise ValueError(...)",
                    },
                ],
            },
            "extra": {"run_id": run_id},
        }
    )


class TestRecordAttribution(unittest.TestCase):
    """Runnable name comes from extra.runnable, not the (per-invocation) filename."""

    def test_runnable_from_record_not_filename(self) -> None:
        # `name` simulates a per-invocation filename stem (polluted) — record wins.
        text = "\n".join(
            [
                _info_line("r1", "starting"),
                _summary_line(run_id="r1", runnable="backup"),
            ]
        )
        rec = _parse_log_text("backup.20260603T142201Z.r1", text, "local")[0]
        self.assertEqual(rec["runnable"], "backup")

    def test_falls_back_to_name_when_record_lacks_runnable(self) -> None:
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": "INFO",
                "logger": "runspec.runsummary",
                "message": "run completed",
                "extra": {"event": "run_summary", "run_id": "r1", "exit_code": 0},
            }
        )
        rec = _parse_log_text("legacy-name", line, "local")[0]
        self.assertEqual(rec["runnable"], "legacy-name")


class TestPrintCaptureSurfaced(unittest.TestCase):
    """A runnable's captured stdout (runspec.print records) shows in History.

    runspec-node 0.25.0 tees console.log into the audit log as `runspec.print`
    records — the same shape Python already emits — so History surfaces a Node
    runnable's output identically. This guards that the parser keeps including
    those records as log lines (not dropping them by logger name).
    """

    def test_print_record_appears_in_log_lines(self) -> None:
        printed = json.dumps(
            {
                "ts": "2026-06-04T10:00:00Z",
                "level": "INFO",
                "logger": "runspec.print",
                "message": "Hello from the node runnable",
                "extra": {"run_id": "r1"},
            }
        )
        text = "\n".join([printed, _summary_line(run_id="r1", runnable="greet")])
        rec = _parse_log_text("greet", text, "local")[0]
        messages = [line["message"] for line in rec["logLines"]]
        self.assertIn("Hello from the node runnable", messages)


class TestExcStructuredLifting(unittest.TestCase):
    def test_exception_frames_surface_on_record(self) -> None:
        text = "\n".join(
            [
                _exc_line("r1"),
                _summary_line(run_id="r1", runnable="deploy", exit_code=1),
            ]
        )
        rec = _parse_log_text("deploy", text, "local")[0]
        self.assertIn("exception", rec)
        self.assertEqual(rec["exception"]["type"], "ValueError")
        self.assertEqual(rec["exception"]["module"], "release")
        self.assertEqual(len(rec["exception"]["frames"]), 2)

    def test_exception_not_duplicated_as_log_line(self) -> None:
        text = "\n".join(
            [
                _exc_line("r1"),
                _summary_line(run_id="r1", runnable="deploy", exit_code=1),
            ]
        )
        rec = _parse_log_text("deploy", text, "local")[0]
        self.assertNotIn(
            "uncaught exception", [ln["message"] for ln in rec["logLines"]]
        )


class TestLogGlobs(unittest.TestCase):
    def test_runnable_specific_patterns(self) -> None:
        self.assertEqual(
            _log_globs("backup"), ["backup.*.log", "backup.log", "backup.*.log.gz"]
        )

    def test_all_patterns(self) -> None:
        self.assertEqual(_log_globs(None), ["*.log", "*.log.gz"])


class TestRemoteLogScript(unittest.TestCase):
    def test_includes_both_patterns_and_gz_handling(self) -> None:
        s = _remote_log_script("/v/logs", "backup")
        self.assertIn("/v/logs/backup.*.log", s)
        self.assertIn("/v/logs/backup.log", s)
        self.assertIn("gzip -dc", s)
        self.assertIn("RUNSPEC_LOG", s)


class TestParseGzip(unittest.TestCase):
    def test_reads_gzip_archive(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "backup.archive.20260603.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.write(_summary_line(run_id="r1", runnable="backup") + "\n")
            recs = _parse_log(p, "local")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["runnable"], "backup")


class TestPerInvocationDiscovery(unittest.TestCase):
    def _venv(self, td: str, files: dict[str, str]) -> dict:
        venv = Path(td) / "ops-tools"
        (venv / "bin").mkdir(parents=True)
        (venv / "logs").mkdir()
        for fname, content in files.items():
            (venv / "logs" / fname).write_text(content, encoding="utf-8")
        return {
            "name": "h",
            "runspec_paths": [str(venv / "bin" / "runspec")],
            "ssh": None,
        }

    def test_collects_per_invocation_files(self) -> None:
        b = _make_bridge()
        with TemporaryDirectory() as td:
            entry = self._venv(
                td,
                {
                    "backup.20260603T142201Z.r1.log": _summary_line(
                        run_id="r1", runnable="backup"
                    ),
                    "backup.20260603T150000Z.r2.log": _summary_line(
                        run_id="r2", runnable="backup"
                    ),
                },
            )
            recs = b._collect_host_records(entry, None)
        self.assertEqual(len(recs), 2)
        self.assertTrue(all(r["runnable"] == "backup" for r in recs))

    def test_mixed_single_and_per_run(self) -> None:
        b = _make_bridge()
        with TemporaryDirectory() as td:
            entry = self._venv(
                td,
                {
                    "backup.log": _summary_line(run_id="r0", runnable="backup"),
                    "backup.20260603T142201Z.r1.log": _summary_line(
                        run_id="r1", runnable="backup"
                    ),
                },
            )
            recs = b._collect_host_records(entry, None)
        self.assertEqual(len(recs), 2)
        self.assertEqual({r["runnable"] for r in recs}, {"backup"})

    def test_runnable_filter_glob_boundary(self) -> None:
        # filtering 'foo' must not pick up 'foobar' files
        b = _make_bridge()
        with TemporaryDirectory() as td:
            entry = self._venv(
                td,
                {
                    "foo.20260603T142201Z.r1.log": _summary_line(
                        run_id="r1", runnable="foo"
                    ),
                    "foobar.20260603T142201Z.r2.log": _summary_line(
                        run_id="r2", runnable="foobar"
                    ),
                },
            )
            recs = b._collect_host_records(entry, "foo")
        self.assertEqual([r["runnable"] for r in recs], ["foo"])

    def test_get_history_attributes_per_invocation(self) -> None:
        b = _make_bridge()
        with TemporaryDirectory() as td:
            entry = self._venv(
                td,
                {
                    "backup.20260603T142201Z.r1.log": "\n".join(
                        [
                            _info_line("r1", "hi"),
                            _summary_line(run_id="r1", runnable="backup"),
                        ]
                    )
                },
            )
            b._hosts = [entry]
            recs = b.get_history("h", "backup")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["runnable"], "backup")


class TestRemoteHistoryDirs(unittest.TestCase):
    """Remote History asks the remote runspec for its real log dirs (via `logs
    status --json`) instead of guessing {rp}/../../logs — that guess is wrong for
    pipx / ~/.local / system installs, where the Logs tab still finds the run
    (it uses the runnable's real sys.prefix) but History missed it."""

    def test_remote_history_uses_dirs_from_logs_status(self) -> None:
        b = _make_bridge()
        entry = {"name": "prod", "ssh": "u@h", "runspec_paths": ["/opt/v/bin/runspec"]}
        b._hosts = [entry]
        captured: dict[str, str] = {}
        framed = "\x00RUNSPEC_LOG:backup\n" + _summary_line(
            run_id="r1", runnable="backup"
        )
        # A non-standard install whose real dirs are nowhere near {rp}/../../logs.
        status = json.dumps({"dirs": ["/weird/pipx/logs", "/home/u/logs"]})

        def fake_ssh_run(ssh, command, **kw):
            if "logs status --json" in command:
                return (0, status, "")
            captured["script"] = command  # the cat scan
            return (0, framed, "")

        with patch.object(b, "_ssh_run", side_effect=fake_ssh_run):
            recs = b.get_history("prod", "backup")
        # The scan targets the dirs the remote reported, not the path guess.
        self.assertIn("/weird/pipx/logs", captured["script"])
        self.assertIn("/home/u/logs", captured["script"])
        self.assertEqual(len(recs), 1)

    def test_remote_history_falls_back_when_status_unavailable(self) -> None:
        # Old runspec without `logs status --json` → fall back to the path guess
        # plus $HOME/logs so History still works.
        b = _make_bridge()
        entry = {"name": "prod", "ssh": "u@h", "runspec_paths": ["/opt/v/bin/runspec"]}
        b._hosts = [entry]
        captured: dict[str, str] = {}
        framed = "\x00RUNSPEC_LOG:backup\n" + _summary_line(
            run_id="r1", runnable="backup"
        )

        def fake_ssh_run(ssh, command, **kw):
            if "logs status --json" in command:
                return (2, "", "no such command")  # unsupported
            captured["script"] = command
            return (0, framed, "")

        with patch.object(b, "_ssh_run", side_effect=fake_ssh_run):
            recs = b.get_history("prod", "backup")
        self.assertIn("/opt/v/logs", captured["script"])
        self.assertIn("$HOME/logs", captured["script"])
        self.assertEqual(len(recs), 1)


class TestHomeLogsFallback(unittest.TestCase):
    """A runnable whose venv logs dir isn't writable logs to ~/logs instead — but
    the venv dir is still *created* (empty) on the way to that fallback. History
    and analytics must scan ~/logs too, or those runs (e.g. triggered runs on a
    managed machine) are invisible even though the Logs tab shows them."""

    def _entry(self, td: str, name: str = "local") -> dict:
        venv = Path(td) / "ops-tools"
        (venv / "bin").mkdir(parents=True)
        (venv / "logs").mkdir()  # exists but empty — writes went to ~/logs
        return {
            "name": name,
            "runspec_paths": [str(venv / "bin" / "runspec")],
            "ssh": None,
        }

    def test_collect_local_reads_home_logs(self) -> None:
        b = _make_bridge()
        with TemporaryDirectory() as td, TemporaryDirectory() as home:
            entry = self._entry(td)
            home_logs = Path(home) / "logs"
            home_logs.mkdir()
            (home_logs / "backup.log").write_text(
                _summary_line(run_id="r1", runnable="backup"), encoding="utf-8"
            )
            with patch("pathlib.Path.home", return_value=Path(home)):
                recs = b._collect_host_records(entry, None)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["runnable"], "backup")
        # ~/logs records tagged with the primary venv's group (single-venv case).
        self.assertEqual(recs[0]["group"], "ops-tools")

    def test_get_history_reads_home_logs(self) -> None:
        b = _make_bridge()
        with TemporaryDirectory() as td, TemporaryDirectory() as home:
            entry = self._entry(td, name="h")
            b._hosts = [entry]
            home_logs = Path(home) / "logs"
            home_logs.mkdir()
            (home_logs / "backup.log").write_text(
                "\n".join(
                    [
                        _info_line("r1", "hi"),
                        _summary_line(run_id="r1", runnable="backup"),
                    ]
                ),
                encoding="utf-8",
            )
            with patch("pathlib.Path.home", return_value=Path(home)):
                recs = b.get_history("h", "backup")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["runnable"], "backup")


class TestRemoteLogScriptLimit(unittest.TestCase):
    def test_limit_uses_ls_t_and_head(self) -> None:
        s = _remote_log_script("/v/logs", "backup", limit=250)
        self.assertIn("ls -1t", s)
        self.assertIn("head -n 250", s)
        self.assertIn("/v/logs/backup.*.log", s)
        self.assertIn("gzip -dc", s)
        self.assertIn("RUNSPEC_LOG", s)
        # bounded form uses `while read`, not the unbounded `for` loop
        self.assertIn("while", s)
        self.assertNotIn("for f in", s)

    def test_no_limit_keeps_unbounded_for_loop(self) -> None:
        # Analytics (_collect_host_records) relies on the unbounded default.
        s = _remote_log_script("/v/logs", "backup")
        self.assertIn("for f in", s)
        self.assertNotIn("ls -1t", s)


def _per_run(td: str, files: dict[str, tuple[str, float]]) -> dict:
    """Venv fixture whose per-run files each carry (content, mtime-epoch)."""
    import os

    venv = Path(td) / "ops-tools"
    (venv / "bin").mkdir(parents=True)
    (venv / "logs").mkdir()
    for fname, (content, mtime) in files.items():
        p = venv / "logs" / fname
        p.write_text(content, encoding="utf-8")
        os.utime(p, (mtime, mtime))
    return {
        "name": "h",
        "runspec_paths": [str(venv / "bin" / "runspec")],
        "ssh": None,
    }


class TestGetHistoryBounded(unittest.TestCase):
    """The History read must be bounded (newest-N files only) and cached across
    re-polls, or the 8s interval re-reads/re-parses every log file on disk."""

    def _spy_parse(self):
        """Patch bridge_reporting._parse_log with a call-counting passthrough."""
        import runspec_console.bridge_reporting as br

        real = br._parse_log
        calls: list[str] = []

        def wrapped(path, host, include_extra=False):
            calls.append(str(path))
            return real(path, host, include_extra=include_extra)

        return patch.object(br, "_parse_log", side_effect=wrapped), calls

    def test_reads_only_newest_files_with_early_stop(self) -> None:
        b = _make_bridge()
        base = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
        files: dict[str, tuple[str, float]] = {}
        for i in range(250):
            dt = base - timedelta(minutes=i)  # i=0 newest
            ts = dt.isoformat()
            rid = f"{i:08x}"
            fname = f"backup.{dt.strftime('%Y%m%dT%H%M%SZ')}.{rid}.log"
            files[fname] = (
                _summary_line(run_id=rid, runnable="backup", ts=ts),
                dt.timestamp(),
            )
        with TemporaryDirectory() as td:
            entry = _per_run(td, files)
            b._hosts = [entry]
            spy, calls = self._spy_parse()
            with spy:
                recs = b.get_history("h", "backup")
        self.assertEqual(len(recs), 200)  # capped at newest 200
        # newest row is i=0, oldest kept is i=199
        self.assertEqual(recs[0]["runId"], f"{0:08x}")
        self.assertEqual(recs[-1]["runId"], f"{199:08x}")
        # early stop: far fewer than all 250 files parsed
        self.assertLess(len(calls), 210)

    def test_parse_cache_reuses_unchanged_files(self) -> None:
        b = _make_bridge()
        base = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
        files: dict[str, tuple[str, float]] = {}
        for i in range(5):
            dt = base - timedelta(minutes=i)
            rid = f"{i:08x}"
            fname = f"backup.{dt.strftime('%Y%m%dT%H%M%SZ')}.{rid}.log"
            files[fname] = (
                _summary_line(run_id=rid, runnable="backup", ts=dt.isoformat()),
                dt.timestamp(),
            )
        with TemporaryDirectory() as td:
            entry = _per_run(td, files)
            b._hosts = [entry]
            spy1, calls1 = self._spy_parse()
            with spy1:
                b.get_history("h", "backup")
            self.assertEqual(len(calls1), 5)  # all parsed the first time
            spy2, calls2 = self._spy_parse()
            with spy2:
                recs = b.get_history("h", "backup")
            self.assertEqual(len(calls2), 0)  # all cache hits
            self.assertEqual(len(recs), 5)

    def test_recent_mtime_archive_does_not_displace_fresh(self) -> None:
        """A compacted archive with a recent *mtime* but old *contents* must not
        crowd fresh runs out of the newest-N, and shouldn't even be read when the
        fresh per-run files already fill the cap."""
        b = _make_bridge()
        base = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
        files: dict[str, tuple[str, float]] = {}
        for i in range(200):
            dt = base - timedelta(minutes=i)
            rid = f"{i:08x}"
            fname = f"backup.{dt.strftime('%Y%m%dT%H%M%SZ')}.{rid}.log"
            files[fname] = (
                _summary_line(run_id=rid, runnable="backup", ts=dt.isoformat()),
                dt.timestamp(),
            )
        # Archive: old content (year 2000) but the newest mtime of all.
        files["backup.archive.20000101.log"] = (
            _summary_line(
                run_id="deadbeef", runnable="backup", ts="2000-01-01T00:00:00+00:00"
            ),
            (base + timedelta(hours=1)).timestamp(),
        )
        with TemporaryDirectory() as td:
            entry = _per_run(td, files)
            b._hosts = [entry]
            spy, calls = self._spy_parse()
            with spy:
                recs = b.get_history("h", "backup")
        self.assertEqual(len(recs), 200)
        self.assertNotIn("deadbeef", {r["runId"] for r in recs})
        # fresh files fill the cap, so the archive is never read
        self.assertFalse(any("archive" in c for c in calls))

    def test_single_mode_reparsed_on_growth(self) -> None:
        import os

        b = _make_bridge()
        base = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as td:
            venv = Path(td) / "ops-tools"
            (venv / "bin").mkdir(parents=True)
            (venv / "logs").mkdir()
            logf = venv / "logs" / "backup.log"
            logf.write_text(
                _summary_line(run_id="aaaaaaaa", runnable="backup", ts=base.isoformat())
                + "\n",
                encoding="utf-8",
            )
            os.utime(logf, (base.timestamp(), base.timestamp()))
            entry = {
                "name": "h",
                "runspec_paths": [str(venv / "bin" / "runspec")],
                "ssh": None,
            }
            b._hosts = [entry]
            recs1 = b.get_history("h", "backup")
            self.assertEqual(len(recs1), 1)
            # Append a second run and bump mtime — the single growing file must be
            # re-parsed (cache keyed on size+mtime), not served stale from cache.
            later = base + timedelta(minutes=5)
            with logf.open("a", encoding="utf-8") as fh:
                fh.write(
                    _summary_line(
                        run_id="bbbbbbbb", runnable="backup", ts=later.isoformat()
                    )
                    + "\n"
                )
            os.utime(logf, (later.timestamp(), later.timestamp()))
            recs2 = b.get_history("h", "backup")
        self.assertEqual(len(recs2), 2)
        self.assertEqual({r["runId"] for r in recs2}, {"aaaaaaaa", "bbbbbbbb"})


if __name__ == "__main__":
    unittest.main()
