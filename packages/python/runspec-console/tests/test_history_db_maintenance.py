"""
test_history_db_maintenance.py — the Logs tab's runs-index (runspec_runs.db)
maintenance surface: history_db_stats / history_db_maintain drive core's
`runspec history stats|prune|reap|reindex --scope … --json` over the venv's own
binary, with old-runspec + permission errors named rather than swallowed.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from runspec_console.bridge import Bridge


def _make_bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    b._hosts = [{"name": "ws", "runspec_paths": ["/opt/venvs/ops-tools/bin/runspec"]}]
    return b


class TestHistoryDbStats(unittest.TestCase):
    def test_returns_first_dir_snapshot(self) -> None:
        b = _make_bridge()
        snap = {
            "path": "/opt/venvs/ops-tools/logs/runspec_runs.db",
            "exists": True,
            "rows": 7,
        }
        with patch.object(
            b,
            "_run_logs",
            return_value=(0, json.dumps({"verb": "stats", "dirs": [snap]}), ""),
        ) as m:
            out = b.history_db_stats("ws", "ops-tools")
        self.assertEqual(
            m.call_args.args[2], ["history", "stats", "--scope", "venv", "--json"]
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["rows"], 7)
        self.assertEqual(out["group"], "ops-tools")

    def test_home_scope(self) -> None:
        b = _make_bridge()
        with patch.object(
            b,
            "_run_logs",
            return_value=(0, json.dumps({"verb": "stats", "dirs": []}), ""),
        ) as m:
            out = b.history_db_stats("ws", "~/logs")
        self.assertEqual(
            m.call_args.args[2], ["history", "stats", "--scope", "home", "--json"]
        )
        self.assertTrue(out["ok"])
        self.assertFalse(out["exists"])

    def test_old_runspec_hints_upgrade(self) -> None:
        b = _make_bridge()
        stdout = (
            "✗  Unknown history verb: stats (list / running / prune / reindex / reap)\n"
        )
        with patch.object(b, "_run_logs", return_value=(1, stdout, "")):
            out = b.history_db_stats("ws", "ops-tools")
        self.assertFalse(out["ok"])
        self.assertIn("0.62.0", out["error"])

    def test_unknown_venv(self) -> None:
        self.assertFalse(_make_bridge().history_db_stats("ws", "nope")["ok"])


class TestHistoryDbMaintain(unittest.TestCase):
    def test_prune_requires_a_policy(self) -> None:
        b = _make_bridge()
        with patch.object(b, "_run_logs") as m:
            out = b.history_db_maintain("ws", "ops-tools", "prune")
        self.assertFalse(out["ok"])
        m.assert_not_called()

    def test_rejects_unknown_action(self) -> None:
        b = _make_bridge()
        with patch.object(b, "_run_logs") as m:
            out = b.history_db_maintain("ws", "ops-tools", "drop")
        self.assertFalse(out["ok"])
        m.assert_not_called()

    def test_prune_argv_and_cache_bust(self) -> None:
        b = _make_bridge()
        result = {
            "verb": "prune",
            "count": 3,
            "dirs": [{"dir": "x", "count": 3}],
            "ok": True,
        }
        with (
            patch.object(b, "_run_logs", return_value=(0, json.dumps(result), "")) as m,
            patch.object(b, "_invalidate_remote_history") as inv,
        ):
            out = b.history_db_maintain(
                "ws",
                "ops-tools",
                "prune",
                older_than="30d",
                max_rows=1000,
                run_as="svc",
            )
        self.assertEqual(
            m.call_args.args[2],
            [
                "history",
                "prune",
                "--older-than",
                "30d",
                "--max-rows",
                "1000",
                "--scope",
                "venv",
                "--json",
            ],
        )
        self.assertEqual(m.call_args.kwargs["run_as"], "svc")
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 3)
        inv.assert_called_once_with("ws")

    def test_reap_and_reindex_argv(self) -> None:
        b = _make_bridge()
        for action in ("reap", "reindex"):
            with patch.object(
                b,
                "_run_logs",
                return_value=(
                    0,
                    json.dumps({"verb": action, "count": 0, "ok": True}),
                    "",
                ),
            ) as m:
                self.assertTrue(b.history_db_maintain("ws", "ops-tools", action)["ok"])
            self.assertEqual(
                m.call_args.args[2], ["history", action, "--scope", "venv", "--json"]
            )

    def test_unwritable_dir_error_suggests_run_as(self) -> None:
        b = _make_bridge()
        result = {
            "verb": "reap",
            "count": 0,
            "dirs": [
                {"dir": "x", "count": 0, "error": "permission denied: x (owned by svc)"}
            ],
            "ok": False,
        }
        with patch.object(b, "_run_logs", return_value=(1, json.dumps(result), "")):
            out = b.history_db_maintain("ws", "ops-tools", "reap")
        self.assertFalse(out["ok"])
        self.assertIn("owned by svc", out["error"])
        self.assertIn("Run as", out["error"])

    def test_legacy_text_success_is_reported(self) -> None:
        # runspec 0.57–0.60: prune/reap/reindex exist but print text, not JSON.
        b = _make_bridge()
        with patch.object(
            b,
            "_run_logs",
            return_value=(0, "✓  reaped 2 orphaned 'running' row(s) → crashed\n", ""),
        ):
            out = b.history_db_maintain("ws", "ops-tools", "reap")
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 2)


if __name__ == "__main__":
    unittest.main()
