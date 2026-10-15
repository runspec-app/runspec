"""test_bridge_provision.py — the provision / deprovision bridge seam.

Binds the real ProvisionMixin methods onto a light stub whose "SSH" runner
executes the command line in a local shell, so the step scripts really run
against a temp dir. ``pip`` is faked (no network): it drops a stand-in
``bin/runspec`` that the inbox + verify steps then really execute. Checks the
plan validation, preflight rows, an end-to-end provision (hosts file updated,
events, audit, discovery kick), a failure that leaves the host file alone, and
a deprovision with its typed confirmation + kept logs.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time

import pytest

from runspec_console import config as config_mod
from runspec_console import hosts as hosts_mod
from runspec_console import marketplace as mp
from runspec_console.bridge import Bridge

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")

_FAKE_RUNSPEC = '#!/bin/sh\necho "runspec $*"\n'


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path / "app")
    (tmp_path / "app").mkdir()
    monkeypatch.setattr(mp, "pip_config_files", lambda **kw: [])
    for k in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CONFIG_FILE"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


class _StubBridge:
    def __init__(self) -> None:
        self._hosts: list[dict] = []
        self._connected_cache: dict[str, bool] = {
            "web-01": True,
            "web-02": True,
            "db-01": False,
        }
        self._lock = threading.Lock()
        self._runnables_cache: list[dict] = []
        self.dispatched: list[tuple[str, dict]] = []
        self.audit: list[dict] = []
        self.commands: list[str] = []
        self.stdin: list[bytes | None] = []
        self.refreshed = 0
        self.fail_on: str = ""
        # Each "host" is its own machine: a path under ``host_root`` is
        # rewritten to ``host_root/<host>/…`` so two hosts never share a disk.
        self.host_root: str = ""

    def get_config(self) -> dict:
        return {"ssh": {}}

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append((event, detail))

    def _kick_refresh(self, *, force: bool = False) -> None:
        self.refreshed += 1

    def _fanout_workers(self) -> int:
        return 4

    def _current_user(self) -> str:
        return "operator"

    def _append_tool_audit(self, record: dict) -> None:
        self.audit.append(record)

    def _marketplace_repos(self, cfg, *, resolve_auth):
        return []

    def _ssh_run(self, ssh, command, *, identity_file=None, timeout=15, stdin=None):
        self.commands.append(command)
        if self.host_root:
            host = ssh.split("@")[-1]
            command = command.replace(self.host_root, f"{self.host_root}/{host}")
        self.stdin.append(stdin)
        if self.fail_on and self.fail_on in command:
            return 1, "", f"boom: {self.fail_on}"
        argv = shlex.split(command)
        if "-m" in argv and "pip" in argv:
            if "runspec" in argv:  # the install step: stand in bin/runspec
                py = argv[0]
                bindir = py.rsplit("/", 1)[0]
                with open(f"{bindir}/runspec", "w") as fh:
                    fh.write(_FAKE_RUNSPEC)
                subprocess.run(["chmod", "755", f"{bindir}/runspec"], check=True)
            return 0, "Successfully installed", ""
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120
        )
        return r.returncode, r.stdout, r.stderr

    _reload_hosts = Bridge._reload_hosts
    _local_host_entry = Bridge._local_host_entry
    _all_host_entries = Bridge._all_host_entries

    provision_options = Bridge.provision_options
    provision_interpreters = Bridge.provision_interpreters
    provision_preflight = Bridge.provision_preflight
    provision_start = Bridge.provision_start
    provision_op = Bridge.provision_op
    provision_cancel = Bridge.provision_cancel
    deprovision_preview = Bridge.deprovision_preview
    deprovision_start = Bridge.deprovision_start
    _prov_state = Bridge._prov_state
    _prov_remote_hosts = Bridge._prov_remote_hosts
    _prov_host = Bridge._prov_host
    _prov_run = Bridge._prov_run
    _prov_login = Bridge._prov_login
    _prov_fanout = Bridge._prov_fanout
    _prov_plan = Bridge._prov_plan
    _prov_preflight_one = Bridge._prov_preflight_one
    _prov_new_op = Bridge._prov_new_op
    _prov_progress = Bridge._prov_progress
    _prov_audit = Bridge._prov_audit
    _prov_run_provision = Bridge._prov_run_provision
    _deprov_targets = Bridge._deprov_targets
    _deprov_inspect_one = Bridge._deprov_inspect_one
    _deprov_references = Bridge._deprov_references
    _prov_run_deprovision = Bridge._prov_run_deprovision
    _prov_edit_hosts = Bridge._prov_edit_hosts
    _prov_finish = Bridge._prov_finish
    _prov_operator = Bridge._prov_operator
    _prov_host_audit = Bridge._prov_host_audit


def _hosts(extra_paths=None):
    hosts_mod.save_hosts(
        config_mod.hosts_path(),
        [
            {
                "name": "web-01",
                "ssh": "deploy@web-01",
                "group": "Web",
                "profiles": ["prod"],
                "runspec_paths": list(extra_paths or ["/opt/venvs/old/bin/runspec"]),
                "role": "primary",
            },
            {
                "name": "web-02",
                "ssh": "deploy@web-02",
                "group": "Web",
                "runspec_paths": list(extra_paths or ["/opt/venvs/old/bin/runspec"]),
            },
            {
                "name": "db-01",
                "ssh": "deploy@db-01",
                "group": "DB",
                "runspec_paths": ["/opt/venvs/old/bin/runspec"],
            },
        ],
    )


def _wait(b, op, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        rec = b.provision_op(op)
        if rec and rec["status"] != "running":
            return rec
        time.sleep(0.05)
    raise AssertionError("op did not finish")


def test_options_lists_hosts_groups_profiles(app):
    _hosts()
    b = _StubBridge()
    opts = b.provision_options()
    names = [h["name"] for h in opts["hosts"]]
    assert names == ["web-01", "web-02", "db-01"]
    assert opts["groups"] == ["Web", "DB"]
    assert opts["profiles"] == ["prod"]
    assert [h["connected"] for h in opts["hosts"]] == [True, True, False]
    assert opts["min_python"] == "3.10"


def test_interpreters_probe_runs_on_host(app):
    _hosts()
    b = _StubBridge()
    res = b.provision_interpreters({"host": "web-01", "extra_paths": [sys.executable]})
    assert res["ok"]
    assert res["pythons"][0]["path"] == sys.executable
    with pytest.raises(ValueError):
        b.provision_interpreters({"host": "nope"})


def test_plan_validation(app):
    _hosts()
    b = _StubBridge()
    base = {"hosts": ["web-01"], "dest": "/opt/venvs/ops", "python": "3.12"}
    with pytest.raises(ValueError, match="host"):
        b._prov_plan({**base, "hosts": []})
    with pytest.raises(ValueError, match="system directory"):
        b._prov_plan({**base, "dest": "/opt"})
    with pytest.raises(ValueError, match="interpreter"):
        b._prov_plan({**base, "python": ""})
    with pytest.raises(ValueError, match="user name"):
        b._prov_plan({**base, "run_as": "a b"})
    with pytest.raises(ValueError, match="package spec"):
        b._prov_plan({**base, "packages": ["--index-url=http://evil"]})
    with pytest.raises(ValueError, match="configured"):
        b._prov_plan({**base, "hosts": ["ghost"]})
    plan = b._prov_plan({**base, "packages": ["runspec-linux"]})
    assert plan["packages"] == ["runspec", "runspec-linux"]


def test_preflight_rows(app, tmp_path):
    _hosts()
    b = _StubBridge()
    dest = str(tmp_path / "venvs" / "ops")
    res = b.provision_preflight(
        {"hosts": ["web-01", "db-01"], "dest": dest, "python": sys.executable}
    )
    rows = {r["host"]: r for r in res["rows"]}
    assert rows["db-01"]["blockers"] == ["Host is not connected."]
    web = rows["web-01"]
    assert web["python"] == sys.executable
    assert web["dest_state"] == "absent"
    assert web["ok"] is (sys.version_info >= (3, 10))
    assert not (tmp_path / "venvs").exists()  # preflight never changes anything

    res = b.provision_preflight({"hosts": ["web-01"], "dest": dest, "python": "2.7"})
    assert "No Python 2.7 found" in res["rows"][0]["blockers"][0]


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_provision_end_to_end_two_hosts(app, tmp_path):
    _hosts()
    b = _StubBridge()
    root = tmp_path / "venvs"
    b.host_root = str(root)
    out = b.provision_start(
        {
            "hosts": ["web-01", "web-02"],
            "dest": str(root / "ops"),
            "python": sys.executable,
            "packages": ["runspec-linux"],
        }
    )
    rec = _wait(b, out["op"])
    assert rec["status"] == "finished"
    assert all(r["ok"] for r in rec["results"]), rec["results"]
    assert (root / "web-01" / "ops" / "pyvenv.cfg").exists()
    assert (root / "web-02" / "ops" / "pyvenv.cfg").exists()
    # Every step ran, in order, and the inbox command was issued on the new venv.
    joined = "\n".join(b.commands)
    assert "logs share --inbox" in joined
    assert (
        "pip install --disable-pip-version-check --no-input runspec runspec-linux"
        in joined
    )
    # The new venv's own ledger records how it was made, as its owner.
    assert (
        f"runspec audit record venv.provision --outcome succeeded "
        f"--target path={root}/ops --detail operator=operator --detail python="
    ) in joined
    # The hosts file gained the path; unmanaged keys survived.
    hosts = {h["name"]: h for h in hosts_mod.load_hosts(config_mod.hosts_path())}
    want = f"{root}/ops/bin/runspec"
    assert hosts["web-01"]["runspec_paths"] == ["/opt/venvs/old/bin/runspec", want]
    assert hosts["web-01"]["role"] == "primary"
    assert hosts["db-01"]["runspec_paths"] == ["/opt/venvs/old/bin/runspec"]
    events = [e for e, _ in b.dispatched]
    assert "runspec:provision_done" in events and "runspec:hosts_updated" in events
    assert b.refreshed == 1
    kinds = [a["extra"]["event"] for a in b.audit]
    assert kinds.count("venv.provision.started") == 2
    assert kinds.count("venv.provision.succeeded") == 2
    assert all(a["extra"]["user"] == "operator" for a in b.audit)


class _ReadOnlyParentBridge(_StubBridge):
    """The run-as account can't write the parent, and the SSH login answers the
    root check with ``root_via``; ``sudo -n -u root`` is run without sudo here."""

    def __init__(self, root_via: str) -> None:
        super().__init__()
        self.root_via = root_via

    def _ssh_run(self, ssh, command, *, identity_file=None, timeout=15, stdin=None):
        if command == "id -un":  # an SSH login that isn't root
            return 0, "operator\n", ""
        if "sudo -n true" in command:
            self.commands.append(command)
            return 0, self.root_via + "\n", ""
        if command.startswith("sudo -n -u root "):
            self.commands.append(command)
            return super()._ssh_run(
                ssh, command[len("sudo -n -u root ") :], stdin=stdin
            )
        code, out, err = super()._ssh_run(
            ssh, command, identity_file=identity_file, timeout=timeout, stdin=stdin
        )
        # The preflight runs for real; pretend the parent isn't writable.
        return code, out.replace("writable\tyes", "writable\tno"), err


def test_preflight_unwritable_parent_without_sudo_blocks(app, tmp_path):
    _hosts()
    b = _ReadOnlyParentBridge("no")
    res = b.provision_preflight(
        {
            "hosts": ["web-01"],
            "dest": str(tmp_path / "venvs" / "ops"),
            "python": sys.executable,
        }
    )
    row = res["rows"][0]
    assert not row["ok"] and not row["handoff"]
    assert "can't sudo" in row["blockers"][0]


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_provision_unwritable_parent_hands_folder_over(app, tmp_path):
    _hosts()
    b = _ReadOnlyParentBridge("sudo")
    dest = tmp_path / "venvs" / "ops"
    payload = {"hosts": ["web-01"], "dest": str(dest), "python": sys.executable}
    row = b.provision_preflight(payload)["rows"][0]
    assert row["ok"] and row["handoff"] and row["handoff_via"] == "sudo"
    assert "as root" in row["warnings"][0]
    assert not dest.exists()  # the preflight changes nothing

    rec = _wait(b, b.provision_start(payload)["op"])
    r = rec["results"][0]
    assert r["ok"], r
    assert (dest / "pyvenv.cfg").exists()
    root_cmds = [c for c in b.commands if c.startswith("sudo -n -u root ")]
    assert len(root_cmds) == 1 and root_cmds[0].endswith(f"{dest} {row['user']}")
    assert "created " + str(dest) in "\n".join(r["lines"])
    started = next(
        a for a in b.audit if a["extra"]["event"] == "venv.provision.started"
    )
    assert started["extra"]["folder_by_root"] is True


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_provision_failure_leaves_hosts_file(app, tmp_path):
    _hosts()
    b = _StubBridge()
    b.fail_on = "runspec runspec-linux"
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(tmp_path / "venvs" / "ops"),
            "python": sys.executable,
            "packages": ["runspec-linux"],
        }
    )
    rec = _wait(b, out["op"])
    r = rec["results"][0]
    assert not r["ok"] and r["step"] == "install"
    hosts = {h["name"]: h for h in hosts_mod.load_hosts(config_mod.hosts_path())}
    assert hosts["web-01"]["runspec_paths"] == ["/opt/venvs/old/bin/runspec"]
    assert b.refreshed == 0
    assert [a["extra"]["event"] for a in b.audit][-1] == "venv.provision.failed"


def test_provision_index_rides_stdin_not_argv(app, tmp_path, monkeypatch):
    _hosts()
    b = _StubBridge()
    repo = mp.Repo(name="nexus", url="https://user:pw@nexus.example/simple")
    b._marketplace_repos = lambda cfg, *, resolve_auth: [repo]  # type: ignore[method-assign]
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(tmp_path / "venvs" / "ops"),
            "python": sys.executable,
            "repo": repo.id,
            "run_as": "",
        }
    )
    _wait(b, out["op"])
    assert all("pw@" not in c for c in b.commands)
    with pytest.raises(ValueError, match="Unknown package index"):
        b.provision_start(
            {
                "hosts": ["web-01"],
                "dest": str(tmp_path / "x" / "y"),
                "python": "3.12",
                "repo": "nope",
            }
        )


def test_deprovision_guards_and_keep_logs(app, tmp_path):
    venv = tmp_path / "venvs" / "ops"
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    assert (
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)]
        ).returncode
        == 0
    )
    (venv / "bin" / "runspec").write_text(_FAKE_RUNSPEC)
    (venv / "logs").mkdir()
    (venv / "logs" / "run.log").write_text("history\n")
    b._runnables_cache = [{"host": "web-01", "group": "ops", "name": "disk-usage"}]
    config_mod.triggers_path().write_text(
        '[triggers.full]\nrun = "web-01__disk-usage"\n'
    )

    # Only a path the host already lists can be targeted.
    with pytest.raises(ValueError, match="not one of"):
        b.deprovision_preview(
            {"targets": [{"host": "web-01", "runspec_path": "/etc/bin/runspec"}]}
        )

    prev = b.deprovision_preview({"targets": [{"host": "web-01", "runspec_path": rp}]})
    row = prev["rows"][0]
    assert row["ok"] and row["kind"] == "venv"
    assert row["runnables"] == ["disk-usage"]
    assert row["references"] == ["triggers"]
    assert prev["names"] == ["ops"]

    with pytest.raises(ValueError, match="Type the venv name"):
        b.deprovision_start(
            {"targets": [{"host": "web-01", "runspec_path": rp}], "confirm": "op"}
        )
    assert venv.exists()

    out = b.deprovision_start(
        {
            "targets": [{"host": "web-01", "runspec_path": rp}],
            "confirm": "ops",
            "keep_logs": True,
        }
    )
    rec = _wait(b, out["op"])
    assert rec["results"][0]["ok"], rec["results"]
    assert sorted(p.name for p in venv.iterdir()) == ["logs"]
    assert (venv / "logs" / "run.log").read_text() == "history\n"
    hosts = {h["name"]: h for h in hosts_mod.load_hosts(config_mod.hosts_path())}
    assert "runspec_paths" not in hosts["web-01"]
    assert hosts["web-02"]["runspec_paths"] == [rp]
    # The host recorded the deprovision before the delete ran.
    audit_at = next(
        i for i, c in enumerate(b.commands) if "audit record venv.deprovision" in c
    )
    rm_at = next(i for i, c in enumerate(b.commands) if "refused:" in c)
    assert audit_at < rm_at
    assert "--outcome confirmed" in b.commands[audit_at]
    assert "--detail keep_logs=true" in b.commands[audit_at]
    events = [a["extra"]["event"] for a in b.audit]
    assert events == [
        "venv.deprovision.confirmed",
        "venv.deprovision.succeeded",
        "venv.logs.retained",
    ]

    # web-02 lists the same path, but it's no longer a venv: refused, untouched.
    out = b.deprovision_start(
        {
            "targets": [{"host": "web-02", "runspec_path": rp}],
            "confirm": "ops",
            "keep_logs": False,
        }
    )
    rec = _wait(b, out["op"])
    assert not rec["results"][0]["ok"]
    assert (venv / "logs" / "run.log").exists()


def test_deprovision_falls_back_to_syslog_without_runspec_audit(app, tmp_path):
    venv = tmp_path / "venvs" / "old"
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    assert (
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)]
        ).returncode
        == 0
    )
    (venv / "bin" / "runspec").write_text(_FAKE_RUNSPEC)
    b.fail_on = "audit record"  # a runspec older than 0.71.0
    out = b.deprovision_start(
        {
            "targets": [{"host": "web-01", "runspec_path": rp}],
            "confirm": "old",
            "keep_logs": False,
        }
    )
    rec = _wait(b, out["op"])
    assert rec["results"][0]["ok"], rec["results"]
    logger_cmds = [c for c in b.commands if c.startswith("logger -t runspec")]
    assert len(logger_cmds) == 1
    assert "venv.deprovision" in logger_cmds[0] and "confirmed" in logger_cmds[0]
    assert not venv.exists()


class _RunAsBridge(_StubBridge):
    """Provision as a service account: ``sudo -n -u svc`` runs without sudo here,
    and the SSH login reads as ``deploy``."""

    def _ssh_run(self, ssh, command, *, identity_file=None, timeout=15, stdin=None):
        if command == "id -un":
            self.commands.append(command)
            return 0, "deploy\n", ""
        if command.startswith("sudo -n -u svc "):
            self.commands.append(command)
            command = command[len("sudo -n -u svc ") :]
        return super()._ssh_run(
            ssh, command, identity_file=identity_file, timeout=timeout, stdin=stdin
        )


def test_provision_as_another_account_grants_the_login_an_acl(app, tmp_path):
    _hosts()
    b = _RunAsBridge()
    root = tmp_path / "venvs"
    b.host_root = str(root)
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(root / "ops"),
            "python": sys.executable,
            "run_as": "svc",
        }
    )
    rec = _wait(b, out["op"])
    assert rec["status"] == "finished", rec
    inbox = [c for c in b.commands if "logs share --inbox" in c]
    assert inbox and "--acl-user deploy" in inbox[-1]


def test_provision_as_the_login_adds_no_acl(app, tmp_path):
    _hosts()
    b = _StubBridge()
    root = tmp_path / "venvs"
    b.host_root = str(root)
    out = b.provision_start(
        {"hosts": ["web-01"], "dest": str(root / "ops"), "python": sys.executable}
    )
    rec = _wait(b, out["op"])
    assert rec["status"] == "finished", rec
    assert "id -un" not in b.commands
    assert not any("--acl-user" in c for c in b.commands)


class _SudoRefusedBridge(_RunAsBridge):
    """The login's sudoers rules don't let it run ``sh`` as svc: sudo prints the
    refused command — the whole check script — and exits 1."""

    def _ssh_run(self, ssh, command, *, identity_file=None, timeout=15, stdin=None):
        if command.startswith("sudo -n -u svc "):
            self.commands.append(command)
            return (
                1,
                "",
                (
                    "Sorry, user deploy is not allowed to execute '/bin/sh -c "
                    + command[len("sudo -n -u svc sh -c ") :]
                    + "' as svc on web-01."
                ),
            )
        return super()._ssh_run(
            ssh, command, identity_file=identity_file, timeout=timeout, stdin=stdin
        )


def test_check_host_sudo_refusal_is_a_sentence_and_logged(app, tmp_path, caplog):
    _hosts()
    b = _SudoRefusedBridge()
    with caplog.at_level("WARNING", logger="runspec_console.bridge_const"):
        res = b.provision_preflight(
            {
                "hosts": ["web-01"],
                "dest": str(tmp_path / "venvs" / "ops"),
                "python": sys.executable,
                "run_as": "svc",
            }
        )
    row = res["rows"][0]
    assert not row["ok"]
    assert row["blockers"][0].startswith("sudo refused"), row["blockers"]
    assert "out writable" not in row["blockers"][0]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "check failed on web-01" in logged and "not allowed to execute" in logged


def test_run_as_the_login_itself_skips_sudo(app, tmp_path):
    _hosts()
    b = _RunAsBridge()  # the login reads as "deploy"
    res = b.provision_preflight(
        {
            "hosts": ["web-01"],
            "dest": str(tmp_path / "venvs" / "ops"),
            "python": sys.executable,
            "run_as": "deploy",
        }
    )
    assert res["rows"][0]["ok"], res["rows"][0]
    assert not any(c.startswith("sudo ") for c in b.commands)
