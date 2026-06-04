"""
test_logs_management.py — the Logs tab's bridge surface.

Covers the four bridge methods (logs_status / logs_view / logs_prune /
logs_compact) plus the _run_logs dispatcher that drives `runspec logs --json`
against each venv: argv construction, JSON parsing, per-venv group tagging,
policy guards, and the local-subprocess vs remote-SSH split.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from runspec_console.bridge import Bridge


def _make_bridge():
    with (
        patch("runspec_console.bridge.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        return Bridge()


def _local_entry():
    return {"name": "ws", "runspec_paths": ["/opt/venvs/ops-tools/bin/runspec"]}


def _remote_entry():
    return {
        "name": "prod-1",
        "ssh": "u@h1",
        "runspec_paths": ["/opt/venvs/deploy-core/bin/runspec"],
    }


class TestVenvResolution(unittest.TestCase):
    def test_venvs_lists_group_and_path(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        self.assertEqual(
            b._venvs("prod-1"), [("deploy-core", "/opt/venvs/deploy-core/bin/runspec")]
        )

    def test_venv_path_resolves_group(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        self.assertEqual(
            b._venv_path("prod-1", "deploy-core"), "/opt/venvs/deploy-core/bin/runspec"
        )
        self.assertEqual(b._venv_path("prod-1", "nope"), "")


class TestLogsStatus(unittest.TestCase):
    def test_parses_and_tags_group(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        payload = {
            "dirs": ["/opt/venvs/ops-tools/logs"],
            "runnables": [
                {
                    "runnable": "backup",
                    "per_run_files": 3,
                    "archives": 1,
                    "total_bytes": 900,
                    "oldest": "2026-01-01T00:00:00Z",
                    "newest": "2026-06-01T00:00:00Z",
                }
            ],
            "total_bytes": 900,
            "total_files": 4,
        }
        with patch.object(
            b, "_run_logs", return_value=(0, json.dumps(payload), "")
        ) as m:
            out = b.logs_status("ws")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["ok"])
        self.assertEqual(out[0]["group"], "ops-tools")
        self.assertEqual(out[0]["total_files"], 4)
        # argv: logs status --json (no runnable filter)
        self.assertEqual(m.call_args.args[2], ["logs", "status", "--json"])

    def test_runnable_filter_in_argv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs", return_value=(0, "{}", "")) as m:
            b.logs_status("ws", runnable="backup")
        self.assertEqual(m.call_args.args[2], ["logs", "status", "backup", "--json"])

    def test_error_surfaced_per_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs", return_value=(1, "", "boom")):
            out = b.logs_status("ws")
        self.assertFalse(out[0]["ok"])
        self.assertEqual(out[0]["error"], "boom")
        self.assertEqual(out[0]["group"], "ops-tools")


class TestLogsPrune(unittest.TestCase):
    def test_requires_a_policy(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs") as m:
            out = b.logs_prune("ws", "ops-tools")
        self.assertFalse(out["ok"])
        m.assert_not_called()  # never shells out without a policy

    def test_builds_argv_and_parses_result(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        result = {"dry_run": True, "count": 2, "freed_bytes": 200, "deleted": []}
        with patch.object(
            b, "_run_logs", return_value=(0, json.dumps(result), "")
        ) as m:
            out = b.logs_prune("ws", "ops-tools", older_than="90d", dry_run=True)
        argv = m.call_args.args[2]
        self.assertEqual(
            argv, ["logs", "prune", "--older-than", "90d", "--dry-run", "--json"]
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["group"], "ops-tools")
        self.assertEqual(out["count"], 2)

    def test_apply_omits_dry_run_flag(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs", return_value=(0, "{}", "")) as m:
            b.logs_prune(
                "ws", "ops-tools", runnable="backup", max_files=50, dry_run=False
            )
        argv = m.call_args.args[2]
        self.assertEqual(
            argv, ["logs", "prune", "backup", "--max-files", "50", "--json"]
        )

    def test_unknown_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        out = b.logs_prune("ws", "missing", older_than="7d")
        self.assertFalse(out["ok"])


class TestLogsCompact(unittest.TestCase):
    def test_requires_older_than(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs") as m:
            out = b.logs_compact("ws", "ops-tools")
        self.assertFalse(out["ok"])
        m.assert_not_called()

    def test_builds_argv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(
            b, "_run_logs", return_value=(0, '{"compacted": 1, "archives": []}', "")
        ) as m:
            out = b.logs_compact(
                "ws", "ops-tools", older_than="7d", gzip=True, dry_run=False
            )
        argv = m.call_args.args[2]
        self.assertEqual(
            argv, ["logs", "compact", "--older-than", "7d", "--gzip", "--json"]
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["compacted"], 1)


class TestLogsView(unittest.TestCase):
    def test_parses_json_lines(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        lines = "\n".join(
            json.dumps({"ts": f"2026-06-0{i}", "message": f"m{i}"}) for i in (1, 2)
        )
        with patch.object(b, "_run_logs", return_value=(0, lines + "\n", "")) as m:
            out = b.logs_view("ws", "ops-tools", "backup", since="1h")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["records"]), 2)
        argv = m.call_args.args[2]
        self.assertEqual(argv, ["logs", "backup", "--json", "--since", "1h"])

    def test_unknown_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        out = b.logs_view("ws", "missing", "backup")
        self.assertFalse(out["ok"])
        self.assertEqual(out["records"], [])


class TestRunLogsDispatch(unittest.TestCase):
    def test_remote_quotes_command_string(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        with (
            patch("runspec_console.executor.ssh_run", return_value=(0, "{}", "")) as m,
            patch.object(b, "get_config", return_value={}),
        ):
            code, out, _ = b._run_logs(
                "prod-1",
                "/opt/venvs/deploy-core/bin/runspec",
                ["logs", "status", "--json"],
                timeout=30,
            )
        self.assertEqual(code, 0)
        # command is the path + args joined, shell-quoted
        self.assertEqual(
            m.call_args.args[1], "/opt/venvs/deploy-core/bin/runspec logs status --json"
        )
        self.assertEqual(m.call_args.args[0], "u@h1")

    def test_local_uses_subprocess(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]

        class _Proc:
            returncode = 0
            stdout = "{}"
            stderr = ""

        with patch("subprocess.run", return_value=_Proc()) as m:
            code, out, _ = b._run_logs(
                "ws",
                "/opt/venvs/ops-tools/bin/runspec",
                ["logs", "status", "--json"],
                timeout=30,
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            m.call_args.args[0],
            ["/opt/venvs/ops-tools/bin/runspec", "logs", "status", "--json"],
        )


if __name__ == "__main__":
    unittest.main()
