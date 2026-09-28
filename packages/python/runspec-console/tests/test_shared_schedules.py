"""
test_shared_schedules.py — the read-only Shared (host cron) schedules bridge:
`shared_schedules(host)` runs each venv's `runspec-scheduler status` and reports
it per venv, treating a venv without the scheduler as "not available" (not an
error) and naming the upgrade for a pre-0.3.0 scheduler.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from runspec_console.bridge import Bridge


def _make_bridge(entry):
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    b._hosts = [entry]
    return b


REMOTE = {
    "name": "prod-1",
    "ssh": "u@h1",
    "runspec_paths": ["/opt/venvs/ops/bin/runspec", "/opt/venvs/web/bin/runspec"],
}
STATUS = {
    "ok": True,
    "venv": "ops",
    "backend": "cron",
    "schedules": [{"id": "nightly", "runs": []}],
    "stale": [],
    "errors": [],
}


class TestSharedSchedules(unittest.TestCase):
    def test_runs_the_sibling_scheduler_binary_per_venv(self) -> None:
        b = _make_bridge(REMOTE)
        calls = []

        def fake(host, binary, argv, timeout, run_as=""):
            calls.append((binary, argv))
            if "/ops/" in binary:
                return 0, "noise line\n" + json.dumps(STATUS), ""
            return 127, "", "sh: 1: /opt/venvs/web/bin/runspec-scheduler: not found"

        with patch.object(b, "_run_logs", side_effect=fake):
            out = b.shared_schedules("prod-1")
        self.assertEqual(
            sorted(calls),
            [
                ("/opt/venvs/ops/bin/runspec-scheduler", ["status"]),
                ("/opt/venvs/web/bin/runspec-scheduler", ["status"]),
            ],
        )
        by = {r["group"]: r for r in out}
        self.assertTrue(by["ops"]["ok"] and by["ops"]["available"])
        self.assertEqual(by["ops"]["status"]["schedules"][0]["id"], "nightly")
        self.assertEqual(by["web"], {"group": "web", "available": False, "ok": True})

    def test_old_scheduler_hints_upgrade(self) -> None:
        b = _make_bridge({**REMOTE, "runspec_paths": ["/opt/venvs/ops/bin/runspec"]})
        stdout = "✗  Unknown command for 'runspec-scheduler': 'status'\n"
        with patch.object(b, "_run_logs", return_value=(1, stdout, "")):
            [r] = b.shared_schedules("prod-1")
        self.assertFalse(r["ok"])
        self.assertIn("0.3.0", r["error"])

    def test_local_host_uses_local_sibling(self) -> None:
        b = _make_bridge(
            {"name": "ws", "runspec_paths": ["/opt/venvs/ops/bin/runspec"]}
        )
        with patch.object(
            b,
            "_run_logs",
            return_value=(
                -1,
                "",
                "runspec not found at /opt/venvs/ops/bin/runspec-scheduler",
            ),
        ) as m:
            [r] = b.shared_schedules("ws")
        self.assertTrue(m.call_args.args[1].endswith("runspec-scheduler"))
        self.assertFalse(r["available"])

    def test_unknown_host(self) -> None:
        self.assertEqual(_make_bridge(REMOTE).shared_schedules("nope"), [])


if __name__ == "__main__":
    unittest.main()
