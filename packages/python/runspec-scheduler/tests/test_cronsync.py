"""Tests for cronsync — rendering schedules into the OS crontab (the cron backend).

Covers the pure rendering (command line + block), the declarative reconcile
(strip our marked blocks, preserve the operator's lines, append current,
idempotent), and the sync orchestration (filtering interval/disabled/uninstalled,
grouping per run_as account, cross-account orphan cleanup, dry-run).
"""

from __future__ import annotations

from pathlib import Path

from runspec_scheduler import cronsync
from runspec_scheduler.config import ScheduleEntry, SchedulerSettings
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
        cmd = cronsync.render_command("/venv/bin/runspec", "backup", {"target": "prod"}, "", home="/home/svc", sched_id="nightly")
        # runspec exec, labelled + locked, recorded under trigger=schedule; output
        # redirected only so cron doesn't mail it (exec captured it in the log).
        assert cmd.startswith("env RUNSPEC_AGENT=1 /venv/bin/runspec exec --label sched:nightly --trigger schedule:cron ")
        assert "--lock /home/svc/.cache/runspec-scheduler/nightly.lock" in cmd
        # every fire names the venv's schedule-only env file (missing = nothing added)
        assert "--env-file /venv/runspec_schedules.env" in cmd
        assert "backup -- --target prod" in cmd
        assert cmd.endswith(">/dev/null 2>&1")
        # no bare flock / mkdir any more — exec takes the lock (and creates its dir)
        assert "flock" not in cmd and "mkdir" not in cmd

    def test_flags_and_env_file(self):
        # verbose=true → --verbose; dry-run=false on a default-true flag → the env
        # channel (argv has no --no-<flag>, which runspec rejects).
        spec = {"args": {"verbose": {"default": False}, "dry-run": {"default": True}}, "commands": {}}
        cmd = cronsync.render_command(
            "/venv/bin/runspec",
            "backup",
            {"verbose": True, "dry-run": False},
            "/home/svc/.config/runspec-scheduler/env/n.env",
            home="/home/svc",
            sched_id="n",
            spec=spec,
        )
        assert "backup -- --verbose >/dev/null" in cmd
        assert "--no-" not in cmd
        assert "RUNSPEC_BACKUP_ARG_DRY_RUN=0" in cmd
        # the credential file rides RUNSPEC_ENV_FILE (never the secret itself).
        assert "RUNSPEC_ENV_FILE=/home/svc/.config/runspec-scheduler/env/n.env" in cmd

    def test_no_env_file_when_absent(self):
        cmd = cronsync.render_command("/venv/bin/runspec", "backup", {}, "", home="/home/svc", sched_id="n")
        assert "RUNSPEC_ENV_FILE" not in cmd
        assert cmd.endswith("backup -- >/dev/null 2>&1")

    def test_quotes_values_with_spaces(self):
        cmd = cronsync.render_command("/venv/bin/runspec", "backup", {"msg": "hello world"}, "", home="/home/svc", sched_id="n")
        assert "'hello world'" in cmd


class TestRenderBlock:
    def test_marker_and_job(self):
        block = cronsync.render_block("nightly", "0 2 * * *", "env RUNSPEC_AGENT=1 /v/bin/runspec exec x >/dev/null 2>&1")
        assert block[0] == "# runspec:sched:nightly"
        assert block[1].startswith("0 2 * * *  ")


class TestPlanCrontab:
    def test_strips_ours_preserves_foreign_appends_current(self):
        existing = [
            "# my own job",
            "30 4 * * * /usr/local/bin/thing",
            "# runspec:sched:old",
            "0 1 * * * /v/bin/runspec exec --label sched:old --lock /home/s/.cache/runspec-scheduler/old.lock r",
        ]
        block = cronsync.render_block("new", "0 2 * * *", "new-command")
        out = cronsync.plan_crontab(existing, [block])
        assert "# my own job" in out and "30 4 * * * /usr/local/bin/thing" in out  # foreign kept
        assert "# runspec:sched:old" not in out and not any("sched:old" in ln for ln in out)  # ours stripped
        assert "# runspec:sched:new" in out and "0 2 * * *  new-command" in out  # current appended

    def test_idempotent(self):
        block = cronsync.render_block("a", "* * * * *", "/v/bin/runspec exec --label sched:a --lock /home/s/.cache/runspec-scheduler/a.lock r")
        once = cronsync.plan_crontab(["# keep", "* * * * * foreign"], [block])
        twice = cronsync.plan_crontab(once, [block])
        assert once == twice

    def test_empty_blocks_strips_all_ours(self):
        existing = ["# runspec:sched:a", "* * * * * /v/bin/runspec exec --label sched:a --lock /home/s/.cache/runspec-scheduler/a.lock r", "# foreign", "* * * * * foreign-cmd"]
        out = cronsync.plan_crontab(existing, [])
        assert out == ["# foreign", "* * * * * foreign-cmd"]

    def test_marker_without_its_job_keeps_the_next_line(self):
        # A marker left on its own must not take the operator's next line with it.
        existing = ["# runspec:sched:old", "0 1 * * * /important/backup", "# runspec:sched:x", "", "keep"]
        assert cronsync.plan_crontab(existing, []) == ["0 1 * * * /important/backup", "", "keep"]

    def test_strips_current_and_legacy_job_lines(self):
        current = cronsync.render_block("a", "* * * * *", "env X=1 /v/bin/runspec exec --label sched:a --trigger schedule:cron --lock /home/s/.cache/runspec-scheduler/a.lock r >/dev/null 2>&1")
        legacy = ["# runspec:sched:b", "* * * * *  flock -n /home/s/.cache/runspec-scheduler/b.lock /v/bin/r >/dev/null 2>&1"]
        assert cronsync.plan_crontab(["mine", *current, *legacy], []) == ["mine"]

    def test_is_our_job(self):
        assert cronsync.is_our_job("0 * * * *  runspec exec --label sched:a --lock x", "a")
        assert not cronsync.is_our_job("0 * * * *  runspec exec --label sched:ab --lock x", "a")
        assert not cronsync.is_our_job("# /.cache/runspec-scheduler/a.lock", "a")
        assert not cronsync.is_our_job("", "a")

    def test_large_crontab_round_trips(self):
        own = [f"*/5 * * * * /usr/bin/job{i} 'a b' >/dev/null 2>&1" if i % 3 else f"# note {i}" for i in range(5000)]
        block = cronsync.render_block("a", "* * * * *", "/v/bin/runspec exec --label sched:a --lock /home/s/.cache/runspec-scheduler/a.lock r")
        once = cronsync.plan_crontab(own, [block])
        assert once[:5000] == own and cronsync.plan_crontab(once, [block]) == once
        assert cronsync.plan_crontab(once, []) == own

    def test_keeps_another_venvs_blocks(self):
        # Two venvs owned by one account share its crontab: this venv's sync
        # (or `use daemon`) removes only its own blocks.
        other = ["# runspec:sched:backup", "0 2 * * * env RUNSPEC_AGENT=1 /opt/venvs/b/bin/runspec exec --label sched:backup --lock /home/s/.cache/runspec-scheduler/backup.lock r >/dev/null 2>&1"]
        legacy_other = ["# runspec:sched:old", "* * * * *  flock -n /home/s/.cache/runspec-scheduler/old.lock /opt/venvs/b/bin/r >/dev/null 2>&1"]
        ours = cronsync.render_block("a", "* * * * *", "/v/bin/runspec exec --label sched:a --lock /home/s/.cache/runspec-scheduler/a.lock r")
        existing = ["mine", *other, *ours, *legacy_other]
        assert cronsync.plan_crontab(existing, [], bin_dir="/v/bin") == ["mine", *other, *legacy_other]
        assert cronsync.plan_crontab(existing, [], bin_dir="/opt/venvs/b/bin") == ["mine", *ours]
        once = cronsync.plan_crontab(existing, [ours], bin_dir="/v/bin")
        assert once == ["mine", *other, *legacy_other, *ours]
        assert cronsync.plan_crontab(once, [ours], bin_dir="/v/bin") == once

    def test_job_in_venv_needs_the_whole_folder(self):
        assert cronsync.job_in_venv("x /opt/venvs/ops/bin/runspec exec", "/opt/venvs/ops/bin")
        assert not cronsync.job_in_venv("x /opt/venvs/ops2/bin/runspec exec", "/opt/venvs/ops/bin")
        assert cronsync.job_in_venv("x '/opt/my venv/bin/runspec' exec", "/opt/my venv/bin/")

    def test_marker_at_eof_without_job(self):
        # A trailing marker with no following job line is dropped cleanly.
        out = cronsync.plan_crontab(["keep", "# runspec:sched:x"], [])
        assert out == ["keep"]


class TestSync:
    def _wire(self, monkeypatch, entries, *, installed=("backup",)):
        monkeypatch.setattr(cronsync, "load_config", lambda _p: (SchedulerSettings(), entries))
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
        old_tab = {"olduser": ["# mine", "# runspec:sched:gone", "0 1 * * * /v/bin/runspec exec --label sched:gone --lock /home/s/.cache/runspec-scheduler/gone.lock r"]}
        monkeypatch.setattr(cronsync, "_read_crontab", lambda u, m, f: list(old_tab.get(u, [])))
        cronsync.sync(tmp_path / "cfg.toml")
        assert "olduser" in writes  # reconciled despite having no current schedules
        assert all(not line.startswith("# runspec:sched:") for line in writes["olduser"])
        # state now reflects only the currently-managed accounts.
        assert cronsync.read_managed_users(tmp_path / "cfg.toml") == [""]

    def test_sync_and_remove_all_leave_another_venvs_schedules(self, tmp_path, monkeypatch):
        writes = self._wire(monkeypatch, [_entry(id="a", runnable="backup", when=When(cron="0 2 * * *"))])
        other = [
            "# runspec:sched:nightly",
            "0 1 * * * env RUNSPEC_AGENT=1 /opt/venvs/b/bin/runspec exec --label sched:nightly --lock /home/self/.cache/runspec-scheduler/nightly.lock r >/dev/null 2>&1",
        ]
        tab = {"": ["# mine", *other]}
        monkeypatch.setattr(cronsync, "_read_crontab", lambda u, m, f: list(tab.get(u, [])))
        cronsync.sync(tmp_path / "cfg.toml")
        assert writes[""][:3] == ["# mine", *other]  # the other venv's block is untouched
        assert writes[""][3] == "# runspec:sched:a"
        tab[""] = writes.pop("")
        res = cronsync.remove_all(tmp_path / "cfg.toml")  # this venv moves to the daemon
        assert writes[""] == ["# mine", *other]
        assert [r["id"] for r in res.installed] == ["a"]  # reports only what it removed

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


def test_missing_crontab_binary_is_an_error_result_not_a_traceback(tmp_path, monkeypatch):
    import subprocess

    from runspec_scheduler.config import write_schedules

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "crontab")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(cronsync, "resolve_target", lambda name: _target())
    write_schedules(tmp_path / "cfg.toml", [_entry()])
    res = cronsync.sync(tmp_path / "cfg.toml", dry_run=True)
    assert res.errors and "crontab is not installed" in res.errors[0]["error"]
    assert not res.installed


class TestReplaceCrontab:
    def test_unchanged_is_not_written_or_backed_up(self, monkeypatch, _backups_in_tmp):
        writes: list[list[str]] = []
        monkeypatch.setattr(cronsync, "_write_crontab", lambda lines, *a: writes.append(lines))
        assert cronsync.replace_crontab(["a"], ["a"], "svc", "sudo", None) is False
        assert writes == [] and list(_backups_in_tmp.iterdir()) == []

    def test_backs_up_before_writing_and_keeps_ten(self, monkeypatch, _backups_in_tmp):
        writes: list[list[str]] = []
        monkeypatch.setattr(cronsync, "_write_crontab", lambda lines, *a: writes.append(lines))
        for i in range(12):
            (_backups_in_tmp / f"crontab-svc-2000010{i:02d}T000000Z-1.txt").write_text("old\n")
        assert cronsync.replace_crontab(["mine", "x"], ["mine"], "svc", "sudo", None) is True
        assert writes == [["mine"]]
        kept = sorted(p.name for p in _backups_in_tmp.glob("crontab-svc-*.txt"))
        assert len(kept) == 10
        newest = _backups_in_tmp / kept[-1]
        assert newest.read_text() == "mine\nx\n"
        assert newest.stat().st_mode & 0o777 == 0o600

    def test_backup_failure_does_not_block_the_write(self, monkeypatch, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("")
        monkeypatch.setattr(cronsync, "_backup_dir", lambda: blocker / "sub")
        writes: list[list[str]] = []
        monkeypatch.setattr(cronsync, "_write_crontab", lambda lines, *a: writes.append(lines))
        assert cronsync.replace_crontab(["a"], ["b"], "", "sudo", None) is True
        assert writes == [["b"]]
