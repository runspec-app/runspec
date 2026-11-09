"""Schedules that installed runnables declare in their runspec.toml
(``[<runnable>.meta.scheduler.<name>]``), the venv's policy for them, and the
venv's own zone."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from runspec_scheduler import cli, cronsync, daemon, discovery, packages, render
from runspec_scheduler.config import (
    ScheduleEntry,
    effective_config,
    load_settings,
    raw_schedules,
    remove_schedule,
    set_backend,
    set_enabled,
    set_settings,
    write_schedules,
)
from runspec_scheduler.timing import When, next_fire_time

HOST = "web-01"


# ── the words ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("at", "hm"), [("02:30", (2, 30)), ("2:05", (2, 5)), ("midnight", (0, 0)), ("Noon", (12, 0)), ("23:59", (23, 59))])
def test_parse_at(at, hm):
    assert packages.parse_at(at) == hm


@pytest.mark.parametrize("bad", ["25:00", "02:60", "2pm", "", "0230"])
def test_parse_at_refuses(bad):
    with pytest.raises(ValueError, match="'at'"):
        packages.parse_at(bad)


@pytest.mark.parametrize(
    ("on", "fields"),
    [
        (None, ("*", "*")),
        ("daily", ("*", "*")),
        ("weekdays", ("*", "mon-fri")),
        ("mon-fri", ("*", "mon-fri")),
        ("Weekends", ("*", "sat,sun")),
        ("sun", ("*", "sun")),
        ("monday, wednesday", ("*", "mon,wed")),
        ("tue-thu", ("*", "tue-thu")),
        ("1", ("1", "*")),
        ("1,15", ("1,15", "*")),
        ("last", ("last", "*")),
    ],
)
def test_parse_on(on, fields):
    assert packages.parse_on(on) == fields


@pytest.mark.parametrize("bad", ["fortnightly", "32", "0", "mon-funday"])
def test_parse_on_refuses(bad):
    with pytest.raises(ValueError, match="'on'"):
        packages.parse_on(bad)


def test_words_to_cron():
    assert packages.words_to_cron(2, 30, ("*", "mon-fri")) == "30 2 * * mon-fri"
    assert packages.words_to_cron(2, 30, ("*", "*"), offset=17 * 60) == "47 2 * * *"
    # A spread never pushes a fire into the next day.
    assert packages.words_to_cron(23, 50, ("*", "fri"), offset=30 * 60) == "59 23 * * fri"


def test_spread_offset_is_stable_and_inside_the_window():
    a = packages.spread_offset(1800, HOST, "x.nightly")
    assert a == packages.spread_offset(1800, HOST, "x.nightly")
    assert 0 <= a < 1800 and a % 60 == 0
    offsets = {packages.spread_offset(1800, f"host-{i}", "x.nightly") for i in range(70)}
    assert len(offsets) > 10  # the fleet spreads out
    assert packages.spread_offset(0, HOST, "x") == 0


# ── one table ─────────────────────────────────────────────────────────────────


def test_parse_table_at_on():
    s = packages.parse_table("cleanup-tmp", "nightly", {"at": "02:30", "on": "weekdays", "args": {"older-than": "7d"}, "description": "Old files"}, host=HOST)
    assert s.id == "cleanup-tmp.nightly"
    assert s.base == {"id": "cleanup-tmp.nightly", "runnable": "cleanup-tmp", "when": {"cron": "30 2 * * mon-fri", "timezone": "local"}, "args": {"older-than": "7d"}}
    assert s.words == "02:30 weekdays"
    assert s.description == "Old files"


def test_parse_table_spread_bakes_this_hosts_minute():
    s = packages.parse_table("r", "n", {"at": "02:00", "spread": "30m"}, host=HOST)
    offset = packages.spread_offset(1800, HOST, "r.n") // 60
    assert s.base["when"]["cron"] == f"{offset} 2 * * *"
    assert f"this host 02:{offset:02d}" in s.words


def test_parse_table_every_cron_and_extras():
    s = packages.parse_table("r", "poll", {"every": "5m", "timezone": "UTC", "command": "check now", "timeout": "1h", "catch_up": True}, host=HOST, run_as="svc")
    assert s.base["when"] == {"every": "5m", "timezone": "UTC"}
    assert s.base["command"] == ["check", "now"]
    assert s.base["timeout"] == 3600 and s.base["catch_up"] is True
    assert s.base["run_as"] == "svc"
    assert packages.parse_table("r", "d", {"cron": "@daily"}, host=HOST).base["when"]["cron"] == "@daily"


@pytest.mark.parametrize(
    ("table", "match"),
    [
        ({"at": "02:00", "every": "5m"}, "exactly one"),
        ({}, "exactly one"),
        ({"on": "mon", "cron": "0 2 * * *"}, "'on' goes with 'at'"),
        ({"cron": "0 2 * * *", "spread": "10m"}, "'spread' works with 'at'"),
        ({"at": "02:00", "spread": "13h"}, "at most 12h"),
        ({"at": "02:00", "timezone": "Mars/Base"}, "isn't a zone"),
        ({"at": "02:00", "when": "soon"}, "unknown key"),
        ({"at": "02:00", "args": "x"}, "'args' must be a table"),
        ({"cron": "@reboot"}, "isn't a time"),
        ({"every": "soon"}, "every"),
    ],
)
def test_parse_table_refuses(table, match):
    with pytest.raises(ValueError, match=match):
        packages.parse_table("r", "n", table, host=HOST)


def test_parse_table_refuses_a_bad_name():
    with pytest.raises(ValueError, match="schedule name"):
        packages.parse_table("r", "has space", {"at": "02:00"}, host=HOST)


def test_schedules_in_flags_a_schedule_without_a_name():
    tables, problem = packages.schedules_in({"r": {"meta": {"scheduler": {"at": "02:00", "nightly": {"at": "03:00"}}}}}, "r")
    assert list(tables) == ["nightly"]
    assert "[r.meta.scheduler.<name>]" in problem
    assert packages.schedules_in({"r": {}}, "r") == ({}, "")


# ── reading the installed packages ────────────────────────────────────────────


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Two runnables from one package, one with schedules (and a broken one)."""
    toml = tmp_path / "pkg" / "runspec.toml"
    toml.parent.mkdir()
    toml.write_text(
        textwrap.dedent("""\
        [cleanup-tmp]
        description = "Clean /tmp"
        run_as = "svc"
        [cleanup-tmp.args]
        older-than = {default = "7d"}
        [cleanup-tmp.meta.scheduler.nightly]
        description = "Old files out of /tmp"
        at = "02:30"
        [cleanup-tmp.meta.scheduler.broken]
        at = "02:30"
        every = "5m"

        [report]
        description = "Report"
        [report.meta.scheduler.london]
        at = "07:00"
        on = "weekdays"
        timezone = "Europe/London"
        [report.meta.scheduler.poll]
        every = "15m"
        """)
    )
    records = {
        "cleanup-tmp": {"runnable": "cleanup-tmp", "source": str(toml), "spec": {"run_as": "svc", "args": {"older-than": {}}}},
        "report": {"runnable": "report", "source": str(toml), "spec": {}},
    }
    monkeypatch.setattr(discovery, "discover", lambda: dict(records))
    return records


@pytest.mark.packages
def test_scan_reads_meta_scheduler(installed):
    found = packages.scan(HOST)
    assert sorted(s.id for s in found.schedules) == ["cleanup-tmp.nightly", "report.london", "report.poll"]
    nightly = found.by_id()["cleanup-tmp.nightly"]
    assert nightly.base["run_as"] == "svc"
    assert nightly.description == "Old files out of /tmp"
    assert [e["id"] for e in found.errors] == ["cleanup-tmp.broken"]
    assert found.signature == packages.scan(HOST).signature


@pytest.mark.packages
def test_scan_signature_changes_with_a_package(installed, tmp_path):
    before = packages.scan(HOST).signature
    toml = Path(installed["report"]["source"])
    toml.write_text(toml.read_text().replace('at = "07:00"', 'at = "08:00"'))
    assert packages.scan(HOST).signature != before


def _scan(*tables: tuple[str, str, dict]) -> packages.Scan:
    return packages.Scan(schedules=tuple(packages.parse_table(r, n, t, host=HOST) for r, n, t in tables))


NIGHTLY = ("cleanup-tmp", "nightly", {"at": "02:30", "description": "Old files"})
POLL = ("report", "poll", {"every": "15m"})


# ── the policy and the venv's changes ─────────────────────────────────────────


def _entries(path, scan):
    return {e.id: e for e in effective_config(path, scan).entries}


def test_offer_lists_them_switched_off(tmp_path):
    cfg = tmp_path / "c.toml"
    entries = _entries(cfg, _scan(NIGHTLY))
    e = entries["cleanup-tmp.nightly"]
    assert e.origin == "package" and e.offered and not e.enabled
    assert e.description == "Old files"
    assert e.to_dict()["package"]["words"] == "02:30 daily"


def test_auto_turns_them_on(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [], scheduler={"package_schedules": "auto"})
    e = _entries(cfg, _scan(NIGHTLY))["cleanup-tmp.nightly"]
    assert e.enabled and not e.offered


def test_off_ignores_them(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [{"id": "cleanup-tmp.nightly", "enabled": True}], scheduler={"package_schedules": "off"})
    eff = effective_config(cfg, _scan(NIGHTLY))
    assert eff.entries == []
    assert eff.orphans == [{"id": "cleanup-tmp.nightly", "reason": "package schedules are off in this venv"}]


def test_enable_writes_only_enabled_so_the_package_keeps_its_time(tmp_path):
    cfg = tmp_path / "c.toml"
    assert set_enabled(cfg, "cleanup-tmp.nightly", True, package_ids={"cleanup-tmp.nightly"})
    assert raw_schedules(cfg) == [{"id": "cleanup-tmp.nightly", "enabled": True}]
    e = _entries(cfg, _scan(NIGHTLY))["cleanup-tmp.nightly"]
    assert e.enabled and not e.offered and e.when.cron == "30 2 * * *"
    # The package moves its time: the venv follows.
    moved = ("cleanup-tmp", "nightly", {"at": "03:00"})
    assert _entries(cfg, _scan(moved))["cleanup-tmp.nightly"].when.cron == "0 3 * * *"
    assert not set_enabled(cfg, "nope", True, package_ids={"cleanup-tmp.nightly"})


def test_disable_hides_an_offered_one(tmp_path):
    cfg = tmp_path / "c.toml"
    set_enabled(cfg, "cleanup-tmp.nightly", False, package_ids={"cleanup-tmp.nightly"})
    e = _entries(cfg, _scan(NIGHTLY))["cleanup-tmp.nightly"]
    assert not e.enabled and not e.offered


def test_a_changed_time_stays_and_shows_the_packages(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [{"id": "cleanup-tmp.nightly", "enabled": True, "when": {"cron": "15 4 * * *"}}])
    e = _entries(cfg, _scan(("cleanup-tmp", "nightly", {"at": "03:00"})))["cleanup-tmp.nightly"]
    assert e.when.cron == "15 4 * * *"
    assert e.package["overridden"] == ["when"]
    assert render.rec_when(e.to_dict()) == "15 4 * * * (package says 03:00 daily)"


def test_remove_puts_it_back(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [{"id": "cleanup-tmp.nightly", "enabled": True, "when": {"cron": "15 4 * * *"}}])
    assert remove_schedule(cfg, "cleanup-tmp.nightly")
    assert _entries(cfg, _scan(NIGHTLY))["cleanup-tmp.nightly"].offered


def test_an_uninstalled_package_leaves_an_orphan_not_an_error(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [{"id": "gone.nightly", "enabled": True}, ScheduleEntry(id="own", when=When(cron="0 1 * * *"), runnable="a")])
    eff = effective_config(cfg, packages.Scan())
    assert [e.id for e in eff.entries] == ["own"]
    assert eff.orphans[0]["id"] == "gone.nightly"


def test_writers_keep_the_small_entries(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [{"id": "cleanup-tmp.nightly", "enabled": True}])
    set_backend(cfg, "daemon")
    set_settings(cfg, {"timeout": "1h"})
    assert raw_schedules(cfg) == [{"id": "cleanup-tmp.nightly", "enabled": True}]


# ── zones ─────────────────────────────────────────────────────────────────────


def test_venv_zone_is_local(tmp_path):
    cfg = tmp_path / "c.toml"
    write_schedules(
        cfg,
        [ScheduleEntry(id="blank", when=When(cron="0 2 * * *"), runnable="a"), ScheduleEntry(id="own", when=When(cron="0 2 * * *", timezone="UTC"), runnable="a")],
        scheduler={"timezone": "Australia/Sydney", "package_schedules": "auto"},
    )
    entries = _entries(cfg, _scan(NIGHTLY))
    assert entries["cleanup-tmp.nightly"].fire_when.timezone == "Australia/Sydney"
    assert entries["blank"].fire_when.timezone == "Australia/Sydney"
    assert entries["own"].fire_when.timezone == "UTC"
    # what's written stays as it was: changing the venv zone moves them all
    assert entries["blank"].to_dict()["when"] == {"cron": "0 2 * * *"}
    nxt = next_fire_time(entries["cleanup-tmp.nightly"].fire_when)
    assert nxt is not None and (nxt.hour, nxt.minute) == (2, 30) and str(nxt.tzinfo) == "Australia/Sydney"


def test_no_venv_zone_means_the_hosts(tmp_path):
    e = _entries(tmp_path / "c.toml", _scan(NIGHTLY))["cleanup-tmp.nightly"]
    assert e.fire_when.timezone == ""
    assert ScheduleEntry(id="x", when=When(cron="0 2 * * *", timezone="local"), runnable="a").fire_when.timezone == ""


@pytest.mark.parametrize(
    ("changes", "match"),
    [({"timezone": "Mars/Base"}, "isn't a zone"), ({"timezone": "local"}, "zone name"), ({"package_schedules": "sometimes"}, "package_schedules")],
)
def test_settings_refuse(tmp_path, changes, match):
    with pytest.raises(ValueError, match=match):
        set_settings(tmp_path / "c.toml", changes)


def test_settings_set_and_clear(tmp_path):
    cfg = tmp_path / "c.toml"
    s = set_settings(cfg, {"timezone": "Europe/London", "package_schedules": "AUTO"})
    assert (s.timezone, s.package_schedules) == ("Europe/London", "auto")
    assert set_settings(cfg, {"timezone": "host"}).timezone == ""
    assert load_settings(cfg).package_schedules == "auto"


def test_cron_needs_the_daemon_for_another_zone_and_for_last(tmp_path, monkeypatch):
    monkeypatch.setattr("runspec_scheduler.timing.host_zone_name", lambda: "Etc/UTC")
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [], scheduler={"timezone": "Australia/Sydney", "package_schedules": "auto"})
    e = _entries(cfg, _scan(NIGHTLY))["cleanup-tmp.nightly"]
    ok, reason = cronsync._installable(e, "cron")
    assert not ok and "this venv's zone (Australia/Sydney)" in reason and "use daemon" in reason
    assert cronsync._installable(e, "daemon") == (True, "")
    write_schedules(cfg, [], scheduler={"package_schedules": "auto"})
    last = _entries(cfg, _scan(("r", "eom", {"at": "23:00", "on": "last"})))["r.eom"]
    ok, reason = cronsync._installable(last, "cron")
    assert not ok and "last day" in reason
    nxt = next_fire_time(last.fire_when)
    assert nxt is not None and (nxt + __import__("datetime").timedelta(days=1)).day == 1


# ── the daemon notices a package being installed ──────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon")
def test_daemon_reloads_when_packages_change(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [], scheduler={"backend": "daemon", "package_schedules": "auto"})
    current = {"scan": packages.Scan()}
    monkeypatch.setattr(packages, "scan", lambda hostname=None: current["scan"])
    d = daemon.Daemon(cfg)
    try:
        d.load(initial=True)
        assert d._scheduler.get_jobs() == []
        current["scan"] = _scan(POLL)
        d._check_packages()
        assert [j.id for j in d._scheduler.get_jobs()] == ["report.poll"]
        current["scan"] = packages.Scan()
        d._check_packages()
        assert d._scheduler.get_jobs() == []
    finally:
        d._shutdown_scheduler()


# ── the verbs and their text ──────────────────────────────────────────────────


class _Spec:
    def __init__(self, **values):
        self._values = values

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return type("A", (), {"value": self._values.get(name)})()


def test_enable_verb_on_an_offered_package_schedule(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [], scheduler={"backend": "daemon"})
    monkeypatch.setattr(packages, "scan", lambda hostname=None: _scan(NIGHTLY))
    cli._cmd_enable(_Spec(config=str(cfg), id="cleanup-tmp.nightly"), True)
    out = capsys.readouterr().out
    assert '"origin": "package"' in out
    assert raw_schedules(cfg) == [{"id": "cleanup-tmp.nightly", "enabled": True}]
    with pytest.raises(SystemExit):
        cli._cmd_enable(_Spec(config=str(cfg), id="nope"), True)


def test_enable_verb_refuses_what_cron_cant_fire(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    write_schedules(cfg, [])
    monkeypatch.setattr(packages, "scan", lambda hostname=None: _scan(POLL))
    with pytest.raises(SystemExit):
        cli._cmd_enable(_Spec(config=str(cfg), id="report.poll"), True)
    assert "daemon backend" in capsys.readouterr().out
    assert raw_schedules(cfg) == []


def test_remove_verb_on_an_untouched_package_schedule(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    monkeypatch.setattr(packages, "scan", lambda hostname=None: _scan(NIGHTLY))
    with pytest.raises(SystemExit) as exc:
        cli._cmd_remove(_Spec(config=str(cfg), id="cleanup-tmp.nightly"))
    assert exc.value.code == 2
    payload = capsys.readouterr().out
    assert '"package": "cleanup-tmp"' in payload
    text = render.removed({"ok": False, "id": "cleanup-tmp.nightly", "package": "cleanup-tmp"})
    assert "disable --id cleanup-tmp.nightly" in text
    assert "back to what the cleanup-tmp package says" in render.removed({"ok": True, "id": "x", "package": "cleanup-tmp", "reset": True})


def test_status_text_lists_offered_ones(tmp_path):
    payload = {
        "venv": "ops",
        "backend": "daemon",
        "settings": {"backend": "daemon", "timeout": 3600, "misfire_grace": 300, "catch_up_window": 86400, "timezone": "Australia/Sydney", "package_schedules": "offer"},
        "host_zone": "Etc/UTC",
        "daemon": {"state": "running", "pid": 1},
        "packages": {
            "policy": "offer",
            "offered": 1,
            "errors": [{"runnable": "x", "id": "x.bad", "error": "needs exactly one of 'at'"}],
            "orphans": [{"id": "gone.n", "reason": "no installed runnable declares it"}],
        },
        "schedules": [
            {
                "id": "cleanup-tmp.nightly",
                "runnable": "cleanup-tmp",
                "enabled": False,
                "offered": True,
                "origin": "package",
                "description": "Old files",
                "when": {"cron": "30 2 * * *", "timezone": "local"},
                "package": {"words": "02:30 daily", "overridden": []},
                "runs": [],
            },
        ],
    }
    text = render.status(payload)
    assert "Australia/Sydney (this venv's; the host is on Etc/UTC)" in text
    assert "offer: offered, off until turned on (1 waiting)" in text
    assert "Package schedule x.bad can't be used" in text
    assert "gone.n changes a package schedule that isn't here" in text
    row = next(line for line in text.splitlines() if line.startswith("cleanup-tmp.nightly"))
    assert "02:30 daily" in row and row.rstrip().endswith("offered")
    assert "  cleanup-tmp.nightly  02:30 daily — Old files" in text
    assert "enable --id <id>" in text


def test_settings_and_toggled_text():
    values = {"backend": "cron", "timeout": 0, "misfire_grace": 300, "catch_up_window": 86400, "timezone": "", "package_schedules": "auto"}
    text = render.settings({"settings": values, "host_zone": "Europe/London", "changed": ["package_schedules"], "hint": "run runspec-scheduler sync to bring the crontab in line"})
    assert "Europe/London (the host's)" in text
    assert "auto: on as soon as they're installed" in text
    assert "Run runspec-scheduler sync" in text
    assert "Turned x on." in render.toggled({"id": "x", "enabled": True, "backend": "cron"})
