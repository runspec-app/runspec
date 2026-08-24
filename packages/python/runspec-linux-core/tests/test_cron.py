"""Cron helpers: parsing, validation, the read-modify-write, and run_as escalation.

The subprocess layer is stubbed with an in-memory fake crontab so no real
``crontab`` runs; the tests assert the parsed records, the schedule guards, the
rewritten table piped to ``crontab -``, and the escalated argv (the
``sudo -n -u`` / ``su`` prefix) for another user.
"""

from __future__ import annotations

import subprocess

import pytest

from runspec_linux_core import cron
from runspec_linux_core.errors import CommandError, ToolNotFoundError


class FakeCrontab:
    """A one-user in-memory crontab driving the stubbed ``subprocess.run``.

    Recognises the three crontab invocations by the tokens present in argv (so it
    works for both the plain ``["crontab", "-l"]`` form and the
    ``su user -c "crontab -l"`` form): ``-l`` reads, ``-r`` removes, bare ``-``
    installs the table piped on stdin. Records every argv for escalation asserts.
    """

    def __init__(self, lines: list[str] | None = None, exists: bool = True) -> None:
        self.lines = lines or []
        self.exists = exists
        self.calls: list[list[str]] = []

    def run(self, argv, capture_output=None, text=None, input=None):  # noqa: A002
        self.calls.append(argv)
        joined = " ".join(argv)
        if "crontab -l" in joined:
            if not self.exists:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no crontab for bob")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(self.lines) + ("\n" if self.lines else ""), stderr="")
        if "crontab -r" in joined:
            self.lines = []
            self.exists = False
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        # bare "crontab -" write from stdin
        self.lines = (input or "").splitlines()
        self.exists = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeCrontab) -> None:
    monkeypatch.setattr(cron, "_crontab_available", lambda: True)
    monkeypatch.setattr(cron.subprocess, "run", fake.run)
    monkeypatch.setattr(cron, "current_user", lambda: "tester")


# ── validate_schedule ──────────────────────────────────────────────────────


@pytest.mark.parametrize("good", ["0 3 * * *", "*/5 * * * 1-5", "@reboot", "@Daily", "15 0 1 * *"])
def test_validate_schedule_ok(good: str) -> None:
    # returns normalised (nicknames lowercased, whitespace collapsed)
    assert cron.validate_schedule(good)


@pytest.mark.parametrize("bad", ["", "0 3 * *", "0 3 * * * extra", "@nope", "0 3 * * ;rm", "0 3 * * *\n0 0 * * *"])
def test_validate_schedule_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        cron.validate_schedule(bad)


# ── list_cron ──────────────────────────────────────────────────────────────


def test_list_cron_parses_jobs_env_and_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(
        [
            "SHELL=/bin/bash",
            "# nightly backup",
            "0 3 * * * /opt/app/backup.sh",
            "",
            "@reboot /opt/app/warmup.sh",
        ]
    )
    _install(monkeypatch, fake)
    out = cron.list_cron()
    assert out["exists"] is True
    assert out["count"] == 2
    assert out["env"] == [{"name": "SHELL", "value": "/bin/bash"}]
    first, second = out["jobs"]
    assert first == {
        "index": 0,
        "schedule": "0 3 * * *",
        "command": "/opt/app/backup.sh",
        "comment": "nightly backup",
        "special": False,
        "raw": "0 3 * * * /opt/app/backup.sh",
    }
    assert second["index"] == 1
    assert second["schedule"] == "@reboot"
    assert second["special"] is True
    assert second["comment"] is None


def test_list_cron_no_crontab_is_empty_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(exists=False)
    _install(monkeypatch, fake)
    out = cron.list_cron(run_as="bob")
    assert out == {"user": "bob", "run_as": "bob", "exists": False, "jobs": [], "env": [], "count": 0}


def test_tool_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "_crontab_available", lambda: False)
    with pytest.raises(ToolNotFoundError):
        cron.list_cron()


# ── add_cron ───────────────────────────────────────────────────────────────


def test_add_cron_appends_and_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 1 * * * /existing"])
    _install(monkeypatch, fake)
    out = cron.add_cron("0 3 * * *", "/opt/app/backup.sh", comment="nightly backup")
    assert out["added"] is True
    assert out["count"] == 2
    assert fake.lines == ["0 1 * * * /existing", "# nightly backup", "0 3 * * * /opt/app/backup.sh"]


def test_add_cron_idempotent_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 3 * * * /opt/app/backup.sh"])
    _install(monkeypatch, fake)
    out = cron.add_cron("0 3 * * *", "/opt/app/backup.sh")
    assert out["added"] is False
    assert out["count"] == 1
    # no write happened (only reads)
    assert all("crontab -l" in " ".join(c) for c in fake.calls)


def test_add_cron_allow_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 3 * * * /opt/app/backup.sh"])
    _install(monkeypatch, fake)
    out = cron.add_cron("0 3 * * *", "/opt/app/backup.sh", allow_duplicate=True)
    assert out["added"] is True
    assert fake.lines.count("0 3 * * * /opt/app/backup.sh") == 2


def test_add_cron_rejects_newline_in_command(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab([])
    _install(monkeypatch, fake)
    with pytest.raises(ValueError, match="command must not contain a newline"):
        cron.add_cron("0 3 * * *", "/bin/true\n0 0 * * * /bin/evil")


# ── remove_cron ────────────────────────────────────────────────────────────


def test_remove_cron_by_match_drops_job_and_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(
        [
            "# nightly backup",
            "0 3 * * * /opt/app/backup.sh",
            "0 1 * * * /keep.sh",
        ]
    )
    _install(monkeypatch, fake)
    out = cron.remove_cron(match="backup.sh")
    assert out["removed"] == [{"schedule": "0 3 * * *", "command": "/opt/app/backup.sh"}]
    assert out["count"] == 1
    assert fake.lines == ["0 1 * * * /keep.sh"]


def test_remove_cron_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 1 * * * /a.sh", "0 2 * * * /b.sh"])
    _install(monkeypatch, fake)
    out = cron.remove_cron(index=1)
    assert out["removed"] == [{"schedule": "0 2 * * *", "command": "/b.sh"}]
    assert fake.lines == ["0 1 * * * /a.sh"]


def test_remove_cron_match_nothing_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 1 * * * /a.sh"])
    _install(monkeypatch, fake)
    out = cron.remove_cron(match="nomatch")
    assert out["removed"] == []
    assert out["count"] == 1
    # nothing written — only reads
    assert all("crontab -l" in " ".join(c) for c in fake.calls)


def test_remove_cron_bad_index_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 1 * * * /a.sh"])
    _install(monkeypatch, fake)
    with pytest.raises(ValueError, match="no job at index 5"):
        cron.remove_cron(index=5)


def test_remove_cron_requires_exactly_one_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab([])
    _install(monkeypatch, fake)
    with pytest.raises(ValueError, match="exactly one of index or match"):
        cron.remove_cron()
    with pytest.raises(ValueError, match="exactly one of index or match"):
        cron.remove_cron(match="x", index=0)


# ── clear_cron ─────────────────────────────────────────────────────────────


def test_clear_cron_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 1 * * * /a.sh"])
    _install(monkeypatch, fake)
    out = cron.clear_cron()
    assert out == {"user": "tester", "run_as": "", "cleared": True, "existed": True}
    assert any("crontab -r" in " ".join(c) for c in fake.calls)


def test_clear_cron_noop_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(exists=False)
    _install(monkeypatch, fake)
    out = cron.clear_cron()
    assert out == {"user": "tester", "run_as": "", "cleared": False, "existed": False}
    assert not any("crontab -r" in " ".join(c) for c in fake.calls)


# ── run_as escalation ──────────────────────────────────────────────────────


def test_list_cron_run_as_escalates_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab(["0 1 * * * /a.sh"])
    _install(monkeypatch, fake)
    cron.list_cron(run_as="deploy")
    assert fake.calls[0] == ["sudo", "-n", "-u", "deploy", "crontab", "-l"]


def test_add_cron_run_as_write_escalates_su(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCrontab([])
    _install(monkeypatch, fake)
    cron.add_cron("@daily", "/x.sh", run_as="svc", become_method="su")
    # read then write, both under su -c
    assert fake.calls[0] == ["su", "svc", "-c", "crontab -l"]
    write_call = next(c for c in fake.calls if c[:3] == ["su", "svc", "-c"] and c[3] == "crontab -")
    assert write_call == ["su", "svc", "-c", "crontab -"]


def test_run_as_passwordless_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, capture_output=None, text=None, input=None):  # noqa: A002
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="sudo: a password is required")

    monkeypatch.setattr(cron, "_crontab_available", lambda: True)
    monkeypatch.setattr(cron.subprocess, "run", fake_run)
    with pytest.raises(CommandError, match="passwordless sudo is not configured for run-as 'svc'"):
        cron.list_cron(run_as="svc")
