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
    # The login user owns every venv unless a test says otherwise (no SSH stat).
    b.venv_owner = lambda host, group: {"ok": True, "owner": "u", "self": "u"}
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

    def test_service_account_venv_runs_as_its_owner(self) -> None:
        b = _make_bridge({**REMOTE, "runspec_paths": ["/opt/venvs/ops/bin/runspec"]})
        b.venv_owner = lambda host, group: {"ok": True, "owner": "svc", "self": "u"}
        with (
            patch.object(b, "_run_logs") as direct,
            patch.object(
                b, "_ssh_run", return_value=(0, json.dumps(STATUS), "")
            ) as ssh,
        ):
            [r] = b.shared_schedules("prod-1")
        direct.assert_not_called()
        self.assertTrue(r["ok"])
        command = ssh.call_args.args[1]
        # Same shape as a runnable run: sudo to the owner, the venv's python
        # prelude, the env frame on stdin — and a not-found guard first.
        self.assertIn("[ ! -e /opt/venvs/ops/bin/runspec-scheduler ]", command)
        self.assertIn("sudo -n -u svc /opt/venvs/ops/bin/python -c", command)
        self.assertTrue(command.endswith("/opt/venvs/ops/bin/runspec-scheduler status"))
        self.assertIn(b"RUNSPEC_LABEL=", ssh.call_args.kwargs["stdin"])

    def test_owner_venv_without_scheduler_is_not_available(self) -> None:
        b = _make_bridge({**REMOTE, "runspec_paths": ["/opt/venvs/ops/bin/runspec"]})
        b.venv_owner = lambda host, group: {"ok": True, "owner": "svc", "self": "u"}
        miss = (127, "", "/opt/venvs/ops/bin/runspec-scheduler: not found")
        with patch.object(b, "_ssh_run", return_value=miss):
            [r] = b.shared_schedules("prod-1")
        self.assertEqual(r, {"group": "ops", "available": False, "ok": True})

    def test_refused_sudo_names_the_owner(self) -> None:
        b = _make_bridge({**REMOTE, "runspec_paths": ["/opt/venvs/ops/bin/runspec"]})
        b.venv_owner = lambda host, group: {"ok": True, "owner": "svc", "self": "u"}
        refused = (1, "", "sudo: a password is required")
        with patch.object(b, "_ssh_run", return_value=refused):
            [r] = b.shared_schedules("prod-1")
        self.assertFalse(r["ok"])
        self.assertIn("'svc'", r["error"])
        self.assertIn("passwordless sudo", r["error"])

    def test_root_owned_venv_runs_as_the_login_user(self) -> None:
        b = _make_bridge({**REMOTE, "runspec_paths": ["/opt/venvs/ops/bin/runspec"]})
        b.venv_owner = lambda host, group: {"ok": True, "owner": "root", "self": "u"}
        with patch.object(
            b, "_run_logs", return_value=(0, json.dumps(STATUS), "")
        ) as direct:
            [r] = b.shared_schedules("prod-1")
        direct.assert_called_once()
        self.assertTrue(r["ok"])

    def test_unknown_host(self) -> None:
        self.assertEqual(_make_bridge(REMOTE).shared_schedules("nope"), [])


if __name__ == "__main__":
    unittest.main()
