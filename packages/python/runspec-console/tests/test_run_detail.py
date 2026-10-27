"""
test_run_detail.py — the History row-expand read (get_run_detail).

With the row's runs-index ``log_file`` the read opens just that run's file
(locally) or streams just that file over one SSH exec (remotely), instead of
scanning the host's newest ~250 log files. Without it, or when the file doesn't
hold the run, it falls back to the old full scan.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runspec_console import bridge_utils
from runspec_console.bridge import Bridge

RUN = "0b1c2d3e-0000-4000-8000-000000000001"
NAME = f"backup.20261027T100000Z.{RUN}.log"


def _make_bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    return b


def _log_text(run_id: str = RUN, message: str = "hello") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "ts": "2026-10-27T10:00:00",
                    "level": "INFO",
                    "message": message,
                    "extra": {"run_id": run_id, "runnable": "backup"},
                }
            ),
            json.dumps(
                {
                    "ts": "2026-10-27T10:00:01",
                    "level": "INFO",
                    "message": "done",
                    "extra": {
                        "event": "run_summary",
                        "run_id": run_id,
                        "runnable": "backup",
                        "exit_code": 0,
                    },
                }
            ),
        ]
    )


class TestSafeBasename(unittest.TestCase):
    def test_plain_names_pass(self) -> None:
        self.assertEqual(bridge_utils.safe_log_basename(NAME), NAME)
        self.assertEqual(
            bridge_utils.safe_log_basename("backup.2026-10.log.gz"),
            "backup.2026-10.log.gz",
        )

    def test_paths_and_shell_text_refused(self) -> None:
        for bad in (
            "",
            "../x.log",
            "a/b.log",
            ".hidden.log",
            "x.log; rm -rf /",
            "$(id).log",
        ):
            self.assertIsNone(bridge_utils.safe_log_basename(bad), bad)


class TestRemoteFilesScript(unittest.TestCase):
    def test_streams_only_named_files_and_keeps_home(self) -> None:
        s = bridge_utils._remote_files_script(
            ["/srv/venv/logs/inbox/a.log", "$HOME/logs/a.log", '/odd "dir"/$x/a.log']
        )
        self.assertIn('for f in "/srv/venv/logs/inbox/a.log" "$HOME/logs/a.log"', s)
        self.assertIn('"/odd \\"dir\\"/\\$x/a.log"', s)  # escaped, not expanded
        self.assertNotIn("ls -1t", s)  # no directory scan
        self.assertIn("RUNSPEC_NONCE", s)


class TestLocalRunDetail(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.venv = root / "ops-tools"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "logs").mkdir()
        self.home = root / "home"
        (self.home / "logs").mkdir(parents=True)
        self.b = _make_bridge()
        entry = {"name": "local", "runspec_paths": [str(self.venv / "bin" / "runspec")]}
        self.b._host_entry = lambda host: entry if host == "local" else None  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_only_the_named_file(self) -> None:
        (self.venv / "logs" / NAME).write_text(_log_text())
        with (
            patch("pathlib.Path.home", return_value=self.home),
            patch.object(Bridge, "get_history", side_effect=AssertionError("scanned")),
        ):
            res = self.b.get_run_detail("local", RUN, "ops-tools", NAME)
        self.assertEqual([ln["message"] for ln in res["logLines"]], ["hello"])

    def test_finds_file_in_home_logs(self) -> None:
        (self.home / "logs" / NAME).write_text(_log_text(message="from home"))
        with (
            patch("pathlib.Path.home", return_value=self.home),
            patch.object(Bridge, "get_history", side_effect=AssertionError("scanned")),
        ):
            res = self.b.get_run_detail("local", RUN, "ops-tools", NAME)
        self.assertEqual([ln["message"] for ln in res["logLines"]], ["from home"])

    def test_falls_back_to_scan_without_log_file(self) -> None:
        rows = [{"runId": RUN, "logLines": [{"message": "scanned"}]}]
        with patch.object(Bridge, "get_history", return_value=rows) as gh:
            res = self.b.get_run_detail("local", RUN, "ops-tools", "")
        gh.assert_called_once()
        self.assertEqual(res["logLines"], [{"message": "scanned"}])

    def test_falls_back_when_file_is_missing(self) -> None:
        rows = [{"runId": RUN, "logLines": [{"message": "scanned"}]}]
        with (
            patch("pathlib.Path.home", return_value=self.home),
            patch.object(Bridge, "get_history", return_value=rows) as gh,
        ):
            res = self.b.get_run_detail("local", RUN, "ops-tools", NAME)
        gh.assert_called_once()
        self.assertEqual(res["logLines"], [{"message": "scanned"}])


class TestRemoteRunDetail(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _make_bridge()
        entry = {
            "name": "prod-1",
            "ssh": "ops@prod-1",
            "runspec_paths": ["/srv/ops-tools/bin/runspec", "/srv/other/bin/runspec"],
        }
        self.b._host_entry = lambda host: entry if host == "prod-1" else None  # type: ignore[method-assign]

    def test_one_exec_for_just_the_run_file(self) -> None:
        calls: list[str] = []

        def fake_ssh(target, script, **kw):
            calls.append(script)
            out = (
                "\x00RUNSPEC_NONCE:abc\n"
                f"\x00RUNSPEC_LOG:abc:{NAME[:-4]}\n" + _log_text() + "\n"
            )
            return 0, out, ""

        with (
            patch.object(self.b, "_ssh_run", side_effect=fake_ssh),
            patch.object(Bridge, "get_history", side_effect=AssertionError("scanned")),
        ):
            res = self.b.get_run_detail("prod-1", RUN, "ops-tools", NAME)
        self.assertEqual([ln["message"] for ln in res["logLines"]], ["hello"])
        self.assertEqual(len(calls), 1)
        script = calls[0]
        self.assertIn(f'"/srv/ops-tools/logs/inbox/{NAME}"', script)
        self.assertIn(f'"/srv/ops-tools/logs/{NAME}"', script)
        self.assertIn(f'"$HOME/logs/{NAME}"', script)
        self.assertNotIn("/srv/other/", script)  # only the row's venv
        self.assertNotIn("ls -1t", script)

    def test_unsafe_log_file_never_reaches_the_shell(self) -> None:
        with (
            patch.object(self.b, "_ssh_run") as ssh,
            patch.object(Bridge, "get_history", return_value=[]),
        ):
            self.b.get_run_detail("prod-1", RUN, "ops-tools", "x.log; rm -rf ~")
        ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
