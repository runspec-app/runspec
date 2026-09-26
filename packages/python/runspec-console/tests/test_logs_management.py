"""
test_logs_management.py — the Logs tab's bridge surface.

Covers logs_status (now a single directory listing per host — local scandir or
one remote `find` — classified in-process via runspec.logs, with a separate
~/logs "home" entry), and logs_view / logs_prune / logs_compact argv (now
`--scope`-routed so a per-venv op touches only that venv's dir), plus the
_run_logs local-subprocess vs remote-SSH split.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runspec_console.bridge import Bridge


def _make_bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    # Exercise the legacy connect-per-call path these tests mock via
    # executor.ssh_run; the pooled path is covered by test_ssh_pool.py.
    b._ssh_pool = None
    return b


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

    def test_scope_for_routes_venv_and_home(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        self.assertEqual(
            b._scope_for("prod-1", "deploy-core"),
            ("/opt/venvs/deploy-core/bin/runspec", "venv"),
        )
        # the home sentinel routes to the primary venv's binary + --scope home
        self.assertEqual(
            b._scope_for("prod-1", "~/logs"),
            ("/opt/venvs/deploy-core/bin/runspec", "home"),
        )


class TestLogsStatusLocal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.venv_logs = root / "venvs" / "ops-tools" / "logs"
        self.venv_logs.mkdir(parents=True)
        (root / "venvs" / "ops-tools" / "bin").mkdir(parents=True)
        self.rp = str(root / "venvs" / "ops-tools" / "bin" / "runspec")
        self.nohome = root / "nohome"  # a home with no logs dir
        self.b = _make_bridge()
        self.b._hosts = [{"name": "ws", "runspec_paths": [self.rp]}]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _write(d: Path, name: str) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("{}\n", encoding="utf-8")

    def test_groups_by_runnable_and_excludes_single_mode(self) -> None:
        self._write(self.venv_logs, "backup.20260101T100000Z.aaaaaaaa.log")
        self._write(self.venv_logs, "backup.20260201T100000Z.bbbbbbbb.log")
        self._write(self.venv_logs, "backup.archive.20260101.log")
        self._write(self.venv_logs, "deploy.20260101T100000Z.cccccccc.log")
        self._write(self.venv_logs, "backup.log")  # single-mode active — never counted
        with patch.object(Path, "home", return_value=self.nohome):
            out = self.b.logs_status("ws")
        self.assertEqual([e["group"] for e in out], ["ops-tools"])  # no home entry
        entry = out[0]
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["dirs"], [str(self.venv_logs)])
        rns = {r["runnable"]: r for r in entry["runnables"]}
        self.assertEqual(rns["backup"]["per_run_files"], 2)
        self.assertEqual(rns["backup"]["archives"], 1)
        self.assertEqual(rns["deploy"]["per_run_files"], 1)
        self.assertEqual(
            entry["total_files"], 4
        )  # 4 managed files; single-mode excluded

    def test_home_entry_only_when_home_has_logs(self) -> None:
        self._write(self.venv_logs, "backup.20260101T100000Z.aaaaaaaa.log")
        home = Path(self.tmp.name) / "home"
        self._write(home / "logs", "other.20260101T100000Z.dddddddd.log")
        with patch.object(Path, "home", return_value=home):
            out = self.b.logs_status("ws")
        self.assertEqual([e["group"] for e in out], ["ops-tools", "~/logs"])
        home_entry = out[1]
        self.assertEqual(home_entry["dirs"], ["~/logs"])
        self.assertEqual([r["runnable"] for r in home_entry["runnables"]], ["other"])

    def test_runnable_filter(self) -> None:
        self._write(self.venv_logs, "backup.20260101T100000Z.aaaaaaaa.log")
        self._write(self.venv_logs, "deploy.20260101T100000Z.cccccccc.log")
        with patch.object(Path, "home", return_value=self.nohome):
            out = self.b.logs_status("ws", runnable="backup")
        self.assertEqual([r["runnable"] for r in out[0]["runnables"]], ["backup"])


class TestLogsStatusRemote(unittest.TestCase):
    def test_one_find_per_host_parsed_into_scopes(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        framed = (
            "\x00RSSCOPE:deploy-core\n"
            "backup.20260101T100000Z.aaaaaaaa.log\t100\t1700000000.0\n"
            "backup.archive.20260101.log\t50\t1700000100.0\n"
            "\x00RSSCOPE:~/logs\n"
            "other.20260201T100000Z.bbbbbbbb.log\t20\t1700000200.0\n"
        )
        with patch.object(b, "_ssh_run", return_value=(0, framed, "")) as m:
            out = b.logs_status("prod-1")
        self.assertEqual([e["group"] for e in out], ["deploy-core", "~/logs"])
        deploy = out[0]["runnables"][0]
        self.assertEqual(
            (deploy["runnable"], deploy["per_run_files"], deploy["archives"]),
            ("backup", 1, 1),
        )
        self.assertEqual(out[1]["runnables"][0]["runnable"], "other")
        # ONE ssh round-trip for the whole host (not one per venv), scoping both dirs
        self.assertEqual(m.call_count, 1)
        script = m.call_args.args[1]
        self.assertIn("/opt/venvs/deploy-core/logs", script)
        self.assertIn("$HOME/logs", script)


class TestLogsPrune(unittest.TestCase):
    def test_requires_a_policy(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs") as m:
            out = b.logs_prune("ws", "ops-tools")
        self.assertFalse(out["ok"])
        m.assert_not_called()  # never shells out without a policy

    def test_builds_argv_with_venv_scope(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        result = {"dry_run": True, "count": 2, "freed_bytes": 200, "deleted": []}
        with patch.object(
            b, "_run_logs", return_value=(0, json.dumps(result), "")
        ) as m:
            out = b.logs_prune("ws", "ops-tools", older_than="90d", dry_run=True)
        self.assertEqual(
            m.call_args.args[2],
            [
                "logs",
                "prune",
                "--older-than",
                "90d",
                "--dry-run",
                "--scope",
                "venv",
                "--json",
            ],
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 2)

    def test_apply_omits_dry_run_flag(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs", return_value=(0, "{}", "")) as m:
            b.logs_prune(
                "ws", "ops-tools", runnable="backup", max_files=50, dry_run=False
            )
        self.assertEqual(
            m.call_args.args[2],
            [
                "logs",
                "prune",
                "backup",
                "--max-files",
                "50",
                "--scope",
                "venv",
                "--json",
            ],
        )

    def test_home_scope_prunes_home_via_primary_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs", return_value=(0, "{}", "")) as m:
            b.logs_prune("ws", "~/logs", older_than="90d", dry_run=False)
        # routed through the primary venv's binary, scoped to ~/logs
        self.assertEqual(m.call_args.args[1], "/opt/venvs/ops-tools/bin/runspec")
        self.assertEqual(
            m.call_args.args[2],
            ["logs", "prune", "--older-than", "90d", "--scope", "home", "--json"],
        )

    def test_unknown_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        out = b.logs_prune("ws", "missing", older_than="7d")
        self.assertFalse(out["ok"])

    def test_scope_arg_error_hints_upgrade(self) -> None:
        # An old remote runspec fails --scope safely; the error points at the fix.
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(
            b, "_run_logs", return_value=(2, "", "unknown argument: --scope")
        ):
            out = b.logs_prune("ws", "ops-tools", older_than="7d")
        self.assertFalse(out["ok"])
        self.assertIn("0.56", out["error"])

    def test_stdout_scope_error_is_surfaced(self) -> None:
        # The user's real case: runspec prints arg errors to STDOUT (exit 1), not
        # stderr, so the old `code != 0 and not stdout` guard never fired and the
        # console showed a generic "unparseable result output". Surface the real
        # message (+ the upgrade hint), never "unparseable".
        b = _make_bridge()
        b._hosts = [_local_entry()]
        stdout = "✗  Unknown argument: --scope\n   Valid options: --dry-run, --json\n"
        with patch.object(b, "_run_logs", return_value=(1, stdout, "")):
            out = b.logs_prune("ws", "ops-tools", older_than="7d")
        self.assertFalse(out["ok"])
        self.assertIn("Unknown argument: --scope", out["error"])
        self.assertIn("0.56", out["error"])
        self.assertNotIn("unparseable", out["error"].lower())

    def test_result_line_found_past_leading_noise(self) -> None:
        # A stray non-JSON line before the result (e.g. a config routing an INFO
        # line to stdout) must not mask a valid JSON result.
        b = _make_bridge()
        b._hosts = [_local_entry()]
        stdout = (
            "INFO starting\n"
            + json.dumps({"dry_run": True, "count": 3, "freed_bytes": 9, "deleted": []})
            + "\n"
        )
        with patch.object(b, "_run_logs", return_value=(0, stdout, "")):
            out = b.logs_prune("ws", "ops-tools", older_than="7d", dry_run=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 3)

    def test_run_as_threaded_to_run_logs(self) -> None:
        # Prune-as-owner passes run_as down to _run_logs (which prepends sudo -n -u).
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs", return_value=(0, "{}", "")) as m:
            b.logs_prune("ws", "ops-tools", older_than="7d", run_as="svc")
        self.assertEqual(m.call_args.kwargs.get("run_as"), "svc")


class TestLogsCompact(unittest.TestCase):
    def test_requires_older_than(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(b, "_run_logs") as m:
            out = b.logs_compact("ws", "ops-tools")
        self.assertFalse(out["ok"])
        m.assert_not_called()

    def test_builds_argv_with_scope(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        with patch.object(
            b, "_run_logs", return_value=(0, '{"compacted": 1, "archives": []}', "")
        ) as m:
            out = b.logs_compact(
                "ws", "ops-tools", older_than="7d", gzip=True, dry_run=False
            )
        self.assertEqual(
            m.call_args.args[2],
            [
                "logs",
                "compact",
                "--older-than",
                "7d",
                "--gzip",
                "--scope",
                "venv",
                "--json",
            ],
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["compacted"], 1)


class TestLogsView(unittest.TestCase):
    def test_parses_json_lines_with_scope(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        lines = "\n".join(
            json.dumps({"ts": f"2026-06-0{i}", "message": f"m{i}"}) for i in (1, 2)
        )
        with patch.object(b, "_run_logs", return_value=(0, lines + "\n", "")) as m:
            out = b.logs_view("ws", "ops-tools", "backup", since="1h")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["records"]), 2)
        self.assertEqual(
            m.call_args.args[2],
            ["logs", "backup", "--json", "--scope", "venv", "--since", "1h"],
        )

    def test_unknown_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        out = b.logs_view("ws", "missing", "backup")
        self.assertFalse(out["ok"])
        self.assertEqual(out["records"], [])

    def test_retries_unscoped_when_scope_rejected(self) -> None:
        # An old venv rejects --scope (a ✗ error on stdout, exit 1). View is
        # read-only, so it retries WITHOUT --scope rather than showing an empty
        # pane (the "View isn't working on any logs" report).
        b = _make_bridge()
        b._hosts = [_local_entry()]
        rec = json.dumps({"ts": "2026-06-01", "message": "hi"})
        results = [
            (1, "✗  Unknown argument: --scope\n", ""),  # scoped attempt fails
            (0, rec + "\n", ""),  # unscoped retry succeeds
        ]
        with patch.object(b, "_run_logs", side_effect=results) as m:
            out = b.logs_view("ws", "ops-tools", "backup")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(m.call_count, 2)
        self.assertNotIn(
            "--scope", m.call_args_list[1].args[2]
        )  # retry dropped --scope

    def test_view_surfaces_error_when_both_attempts_fail(self) -> None:
        b = _make_bridge()
        b._hosts = [_local_entry()]
        fail = (1, "✗  boom on stdout\n", "")
        with patch.object(b, "_run_logs", side_effect=[fail, fail]):
            out = b.logs_view("ws", "ops-tools", "backup")
        self.assertFalse(out["ok"])
        self.assertIn("boom on stdout", out["error"])
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
        self.assertEqual(
            m.call_args.args[1], "/opt/venvs/deploy-core/bin/runspec logs status --json"
        )
        self.assertEqual(m.call_args.args[0], "u@h1")

    def test_run_as_prepends_sudo_remote(self) -> None:
        # Prune-as-owner: `sudo -n -u <owner> <venv>/bin/runspec …` (a venv binary,
        # so the scoped sudoers grant covers it).
        b = _make_bridge()
        b._hosts = [_remote_entry()]  # ssh = u@h1 → effective user "u"
        with (
            patch("runspec_console.executor.ssh_run", return_value=(0, "{}", "")) as m,
            patch.object(b, "get_config", return_value={}),
        ):
            b._run_logs(
                "prod-1",
                "/opt/venvs/deploy-core/bin/runspec",
                ["logs", "prune", "--json"],
                timeout=30,
                run_as="svc",
            )
        self.assertEqual(
            m.call_args.args[1],
            "sudo -n -u svc /opt/venvs/deploy-core/bin/runspec logs prune --json",
        )

    def test_run_as_self_no_sudo(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]  # effective user "u"
        with (
            patch("runspec_console.executor.ssh_run", return_value=(0, "{}", "")) as m,
            patch.object(b, "get_config", return_value={}),
        ):
            b._run_logs(
                "prod-1",
                "/opt/venvs/deploy-core/bin/runspec",
                ["logs", "status", "--json"],
                timeout=30,
                run_as="u",
            )
        self.assertNotIn("sudo", m.call_args.args[1])

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


class TestShareLogsCmd(unittest.TestCase):
    """The escalation command builder for 'Make logs shared' (bridge_utils)."""

    def test_share_logs_cmd_shape(self) -> None:
        from runspec_console import bridge_utils

        cmd = bridge_utils.share_logs_cmd(
            "/opt/venvs/deploy-core/bin/share-venv-logs",
            "/opt/venvs/deploy-core",
            "consoles",
        )
        # Runs the venv binary (grant-covered) as the owner, else root; probes with
        # --help (harmless) exactly like the denylist probes with `deny list`.
        self.assertIn('sudo -n -u "$o" "$sv" --help', cmd)
        self.assertIn(
            'exec sudo -n -u "$o" "$sv" --venv /opt/venvs/deploy-core --group consoles',
            cmd,
        )
        self.assertIn('elif sudo -n "$sv" --help', cmd)
        self.assertIn(
            bridge_utils.DENYLIST_AS_MARKER, cmd
        )  # =====AS marker for split_denylist_as
        self.assertIn("exit 77", cmd)

    def test_share_logs_cmd_omits_group_when_blank(self) -> None:
        from runspec_console import bridge_utils

        cmd = bridge_utils.share_logs_cmd("/v/bin/share-venv-logs", "/v", "")
        self.assertIn("--venv /v", cmd)
        self.assertNotIn("--group", cmd)


class TestShareVenvLogs(unittest.TestCase):
    """The share_venv_logs bridge method (Make logs shared button)."""

    def test_remote_direct_chmod_no_runnable(self) -> None:
        # The common case — a venv the operator owns. Runs chmod/chgrp directly:
        # no runspec-linux / share-venv-logs in the venv, no sudo.
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        with patch.object(b, "_ssh_run", return_value=(0, "", "")) as m:
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles")
        self.assertTrue(res["ok"])
        self.assertEqual(res["as_user"], "")  # direct run, no escalation
        cmd = m.call_args.args[1]
        self.assertIn("mkdir -p /opt/venvs/deploy-core/logs", cmd)
        self.assertIn("chgrp consoles /opt/venvs/deploy-core/logs", cmd)
        self.assertIn("chmod 2775 /opt/venvs/deploy-core/logs", cmd)
        self.assertNotIn("share-venv-logs", cmd)  # no runnable dependency

    def test_remote_omits_chgrp_when_no_group(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        with patch.object(b, "_ssh_run", return_value=(0, "", "")) as m:
            res = b.share_venv_logs("prod-1", "deploy-core")
        self.assertTrue(res["ok"])
        cmd = m.call_args.args[1]
        self.assertIn("chmod 2775", cmd)
        self.assertNotIn("chgrp", cmd)

    def test_permission_denied_without_runnable_gives_hint(self) -> None:
        # A venv owned by another user with no share-venv-logs to escalate through:
        # direct chmod is refused and nothing can escalate → an actionable message,
        # never an opaque failure (and never an unscoped/unsafe fallback).
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        results = [
            (
                1,
                "",
                "chmod: changing permissions of '/opt/venvs/deploy-core/logs': Operation not permitted",
            ),
            (1, "", ""),  # [ -x .../share-venv-logs ] → not present
        ]
        with patch.object(b, "_ssh_run", side_effect=results):
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles")
        self.assertFalse(res["ok"])
        self.assertIn("create-venv --group", res["error"])

    def test_remote_escalates_via_runnable_when_present(self) -> None:
        # Direct chmod refused, but the venv *has* share-venv-logs → escalate through
        # it (a venv binary, covered by the scoped sudoers grant), stripping the
        # =====AS marker so the UI can show who it ran as.
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        results = [
            (
                1,
                "",
                "chmod: changing permissions of '/opt/venvs/deploy-core/logs': Operation not permitted",
            ),
            (0, "", ""),  # [ -x .../share-venv-logs ] → present
            (0, "=====AS svc\n" + json.dumps({"shared": True}), ""),  # escalation
            (0, "", ""),  # post-share `[ -w logs ]` writability probe → writable
        ]
        with patch.object(b, "_ssh_run", side_effect=results) as m:
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles")
        self.assertTrue(res["ok"])
        self.assertEqual(res["as_user"], "svc")
        self.assertEqual(res["warning"], "")  # the console user can write → no warning
        self.assertEqual(m.call_count, 4)  # chmod-probe, escalation, writability probe
        # The runnable escalation is the third call (sudo -u owner probe).
        self.assertIn('sudo -n -u "$o" "$sv" --help', m.call_args_list[2].args[1])

    def test_unknown_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        res = b.share_venv_logs("prod-1", "nope")
        self.assertFalse(res["ok"])
        self.assertIn("no venv", res["error"])

    def test_local_uses_subprocess_shell(self) -> None:
        import shlex

        b = _make_bridge()
        b._hosts = [_local_entry()]

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        with patch("subprocess.run", return_value=_Proc()) as m:
            res = b.share_venv_logs("ws", "ops-tools", "consoles")
        self.assertTrue(res["ok"])
        argv = m.call_args.args[0]
        self.assertEqual(
            argv[:2], ["sh", "-c"]
        )  # direct chmod via a shell, no runnable
        # Build the expected fragment the way the code does (shlex.quote + native
        # Path) so it doesn't assume POSIX separators on a Windows runner.
        logdir = shlex.quote(
            str(Path("/opt/venvs/ops-tools/bin/runspec").parent.parent / "logs")
        )
        self.assertIn(f"chmod 2775 {logdir}", argv[2])
        self.assertIn(f"chgrp consoles {logdir}", argv[2])
        self.assertNotIn("share-venv-logs", argv[2])

    def test_run_as_escalates_and_repairs_index(self) -> None:
        # Pre-filled owner (Run as) → the whole repair runs `sudo -n -u <owner>
        # sh -c`, working wherever the operator has passwordless sudo (not only where
        # the venv ships share-venv-logs), and it also repairs the existing
        # runspec_runs.db so a group member's writes to the index succeed after.
        b = _make_bridge()
        b._hosts = [_remote_entry()]  # ssh = u@h1 → effective user "u"
        with patch.object(b, "_ssh_run", return_value=(0, "", "")) as m:
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles", run_as="svc")
        self.assertTrue(res["ok"])
        self.assertEqual(res["as_user"], "svc")
        # Two calls: the escalated chmod, then the writability probe (as the login
        # user). No runnable dance.
        self.assertEqual(m.call_count, 2)
        cmd = m.call_args_list[0].args[1]  # the escalated chmod
        self.assertIn("sudo -n -u svc sh -c", cmd)
        self.assertIn("chmod 2775", cmd)
        self.assertIn("runspec_runs.db", cmd)  # existing-index perms repair
        self.assertEqual(res["warning"], "")  # probe returned writable

    def test_run_as_self_does_not_escalate(self) -> None:
        # Running as the SSH login user needs no sudo (a grant to run as yourself
        # you likely don't have would fail spuriously).
        b = _make_bridge()
        b._hosts = [_remote_entry()]  # ssh = u@h1 → effective user "u"
        with patch.object(b, "_ssh_run", return_value=(0, "", "")) as m:
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles", run_as="u")
        self.assertTrue(res["ok"])
        self.assertNotIn("sudo", m.call_args.args[1])

    def test_run_as_sudo_refused_hints(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        with patch.object(
            b, "_ssh_run", return_value=(1, "", "sudo: a password is required")
        ):
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles", run_as="svc")
        self.assertFalse(res["ok"])
        self.assertEqual(res["as_user"], "svc")
        self.assertIn("sudo -n -u svc", res["error"])

    def test_warns_when_console_user_cannot_write_after_escalated_share(self) -> None:
        # The chmod succeeds (as the owner), but the console's own user isn't in the
        # dir's group, so its runs' logs would still fall back to ~/logs. The share
        # succeeds *with a warning* naming the fix (the "shared but still ~/logs"
        # surprise, surfaced at share time).
        b = _make_bridge()
        b._hosts = [_remote_entry()]  # ssh = u@h1 → effective user "u"
        results = [
            (0, "", ""),  # escalated chmod succeeds
            (1, "", ""),  # `[ -w logs ]` as user "u" → NOT writable
        ]
        with patch.object(b, "_ssh_run", side_effect=results):
            res = b.share_venv_logs("prod-1", "deploy-core", "consoles", run_as="svc")
        self.assertTrue(res["ok"])  # the share itself applied
        self.assertIn("~/logs", res["warning"])
        self.assertIn("consoles", res["warning"])  # names the group to join
        self.assertIn("u", res["warning"])  # names the console user


class TestVenvOwner(unittest.TestCase):
    def test_remote_stats_owner(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]  # ssh = u@h1
        with patch.object(b, "_ssh_run", return_value=(0, "svc\n", "")) as m:
            res = b.venv_owner("prod-1", "deploy-core")
        self.assertEqual(res, {"ok": True, "owner": "svc", "self": "u"})
        self.assertIn("stat -c %U", m.call_args.args[1])

    def test_home_scope_has_no_owner(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        res = b.venv_owner("prod-1", "~/logs")
        self.assertEqual(res["owner"], "")
        self.assertTrue(res["ok"])

    def test_unknown_venv(self) -> None:
        b = _make_bridge()
        b._hosts = [_remote_entry()]
        res = b.venv_owner("prod-1", "missing")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
