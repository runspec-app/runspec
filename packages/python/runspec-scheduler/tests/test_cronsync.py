"""Tests for cronsync — rendering schedules into the OS crontab (the cron backend).

Covers the pure rendering (command line + block), the declarative reconcile
(strip our marked blocks, preserve the operator's lines, append current,
idempotent), and the sync orchestration (filtering interval/disabled/uninstalled,
grouping per run_as account, cross-account orphan cleanup, dry-run).
"""

from __future__ import annotations

from pathlib import Path

from runspec_scheduler import cronsync
from runspec_scheduler.config import ScheduleEntry
from runspec_scheduler.discovery import Target
from runspec_scheduler.timing import When


def _entry(**over) -> ScheduleEntry:
    base = dict(
        id="nightly",
        when=When(cron="0 2 * * *"),
        runnable="backup",
        args={},
        enabled=True,
        run_as="",
        become_method="sudo",
        env_file="",
    )
    base.update(over)
    return ScheduleEntry(**base)


def _target(**over) -> Target:
    base = dict(
        runnable="backup",
        source="/venv/lib/backup/runspec.toml",
        binary=Path("/venv/bin/backup"),
        run_as="",
        become_method="sudo",
        become_flags=None,
    )
    base.update(over)
    return Target(**base)


class TestRenderCommand:
    def test_shape(self):
        cmd = cronsync.render_command("/venv/bin/backup", "/venv/lib/backup/runspec.toml", {"target": "prod"}, "", home="/home/svc", sched_id="nightly")
        # mkdir -p the lock dir, then flock -n the run, output discarded.
        assert cmd.startswith("mkdir -p /home/svc/.cache/runspec-scheduler && ")
        assert "flock -n /home/svc/.cache/runspec-scheduler/nightly.lock" in cmd
        assert cmd.endswith(">/dev/null 2>&1")
        # provenance via env(1), then the runnable binary + its args.
        assert "env RUNSPEC_AGENT=1 RUNSPEC_TRIGGER=schedule RUNSPEC_CONFIG=/venv/lib/backup/runspec.toml" in cmd
        assert "/venv/bin/backup --target prod" in cmd

    def test_flag_negation_and_env_file(self):
        cmd = cronsync.render_command("/venv/bin/backup", "", {"verbose": True, "dry-run": False}, "/home/svc/.config/runspec-scheduler/env/n.env", home="/home/svc", sched_id="n")
        assert "--verbose" in cmd and "--no-dry-run" in cmd
        # the credential file rides RUNSPEC_ENV_FILE (never the secret itself).
        assert "RUNSPEC_ENV_FILE=/home/svc/.config/runspec-scheduler/env/n.env" in cmd

    def test_no_env_file_when_absent(self):
        cmd = cronsync.render_command("/venv/bin/backup", "", {}, "", home="/home/svc", sched_id="n")
        assert "RUNSPEC_ENV_FILE" not in cmd

    def test_quotes_values_with_spaces(self):
        cmd = cronsync.render_command("/venv/bin/backup", "", {"msg": "hello world"}, "", home="/home/svc", sched_id="n")
        assert "'hello world'" in cmd


class TestRenderBlock:
    def test_marker_and_job(self):
        block = cronsync.render_block("nightly", "0 2 * * *", "mkdir -p x && flock -n y env z >/dev/null 2>&1")
        assert block[0] == "# runspec:sched:nightly"
        assert block[1].startswith("0 2 * * *  ")


class TestPlanCrontab:
    def test_strips_ours_preserves_foreign_appends_current(self):
        existing = [
            "# my own job",
            "30 4 * * * /usr/local/bin/thing",
            "# runspec:sched:old",
            "0 1 * * * old-command",
        ]
        block = cronsync.render_block("new", "0 2 * * *", "new-command")
        out = cronsync.plan_crontab(existing, [block])
        assert "# my own job" in out and "30 4 * * * /usr/local/bin/thing" in out  # foreign kept
        assert "# runspec:sched:old" not in out and "0 1 * * * old-command" not in out  # ours stripped
        assert "# runspec:sched:new" in out and "0 2 * * *  new-command" in out  # current appended

    def test_idempotent(self):
        block = cronsync.render_block("a", "* * * * *", "cmd-a")
        once = cronsync.plan_crontab(["# keep", "* * * * * foreign"], [block])
        twice = cronsync.plan_crontab(once, [block])
        assert once == twice

    def test_empty_blocks_strips_all_ours(self):
        existing = ["# runspec:sched:a", "* * * * * cmd-a", "# foreign", "* * * * * foreign-cmd"]
        out = cronsync.plan_crontab(existing, [])
        assert out == ["# foreign", "* * * * * foreign-cmd"]

    def test_marker_at_eof_without_job(self):
        # A trailing marker with no following job line is dropped cleanly.
        out = cronsync.plan_crontab(["keep", "# runspec:sched:x"], [])
        assert out == ["keep"]


class TestSync:
    def _wire(self, monkeypatch, entries, *, installed=("backup",)):
        monkeypatch.setattr(cronsync, "load_schedules", lambda _p: entries)
        monkeypatch.setattr(cronsync, "_home_of", lambda user: f"/home/{user}" if user else "/home/self")

        def resolve(name):
            if name not in installed:
                return None
            return _target(runnable=name, binary=Path(f"/venv/bin/{name}"))

        monkeypatch.setattr(cronsync, "resolve_target", resolve)
        writes: dict[str, list[str]] = {}
        monkeypatch.setattr(cronsync, "_read_crontab", lambda u, m, f: [])
        monkeypatch.setattr(cronsync, "_write_crontab", lambda lines, u, m, f: writes.__setitem__(u, lines))
        return writes

    def test_installs_enabled_cron_schedule(self, tmp_path, monkeypatch):
        writes = self._wire(monkeypatch, [_entry(id="a", runnable="backup", when=When(cron="0 2 * * *"))])
        res = cronsync.sync(tmp_path / "cfg.toml")
        assert res.to_dict()["ok"] is True
        assert [r["id"] for r in res.installed] == ["a"]
        assert "" in writes  # the invoking user's crontab was written
        assert any(line == "# runspec:sched:a" for line in writes[""])

    def test_skips_interval_disabled_and_uninstalled(self, tmp_path, monkeypatch):
        entries = [
            _entry(id="iv", runnable="backup", when=When(every="15m")),
            _entry(id="off", runnable="backup", when=When(cron="0 2 * * *"), enabled=False),
            _entry(id="gone", runnable="missing", when=When(cron="0 2 * * *")),
        ]
        self._wire(monkeypatch, entries)
        res = cronsync.sync(tmp_path / "cfg.toml")
        skipped = {s["id"]: s["reason"] for s in res.skipped}
        assert set(skipped) == {"iv", "off", "gone"}
        assert "every" in skipped["iv"] and skipped["off"] == "disabled" and "not installed" in skipped["gone"]
        assert res.installed == []

    def test_groups_by_run_as_account(self, tmp_path, monkeypatch):
        entries = [
            _entry(id="a", runnable="backup", when=When(cron="0 2 * * *"), run_as="svc"),
            _entry(id="b", runnable="backup", when=When(cron="0 3 * * *")),  # invoking user
        ]
        writes = self._wire(monkeypatch, entries)
        cronsync.sync(tmp_path / "cfg.toml")
        assert "svc" in writes and "" in writes  # a separate crontab per account
        assert any("# runspec:sched:a" in line for line in writes["svc"])
        assert any("# runspec:sched:b" in line for line in writes[""])

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        writes = self._wire(monkeypatch, [_entry(id="a", runnable="backup", when=When(cron="0 2 * * *"))])
        res = cronsync.sync(tmp_path / "cfg.toml", dry_run=True)
        assert res.dry_run is True and res.installed  # still reports what it would do
        assert writes == {}  # but touches no crontab
        assert not (tmp_path / cronsync._STATE_NAME).exists()  # and no state written

    def test_cleans_up_orphaned_account(self, tmp_path, monkeypatch):
        # 'olduser' was managed previously but has no schedules now → its crontab is
        # still reconciled (our lines stripped), not left to leak.
        cronsync.write_managed_users(tmp_path / "cfg.toml", ["olduser"])
        writes = self._wire(monkeypatch, [_entry(id="a", runnable="backup", when=When(cron="0 2 * * *"))])
        cronsync.sync(tmp_path / "cfg.toml")
        assert "olduser" in writes  # reconciled despite having no current schedules
        assert all(not line.startswith("# runspec:sched:") for line in writes["olduser"])
        # state now reflects only the currently-managed accounts.
        assert cronsync.read_managed_users(tmp_path / "cfg.toml") == [""]

    def test_crontab_error_is_reported_not_fatal(self, tmp_path, monkeypatch):
        entries = [
            _entry(id="a", runnable="backup", when=When(cron="0 2 * * *"), run_as="svc"),
            _entry(id="b", runnable="backup", when=When(cron="0 3 * * *")),
        ]
        self._wire(monkeypatch, entries)

        def flaky_write(lines, user, m, f):
            if user == "svc":
                raise RuntimeError("sudo: a password is required")

        monkeypatch.setattr(cronsync, "_write_crontab", flaky_write)
        res = cronsync.sync(tmp_path / "cfg.toml")
        assert res.to_dict()["ok"] is False
        assert [e["user"] for e in res.errors] == ["svc"]
        assert [r["id"] for r in res.installed] == ["b"]  # the good account still installed
