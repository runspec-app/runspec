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

# A stand-in runspec-scheduler: `use` and `install-service` answer like 0.6.0;
# FAIL_USE / FAIL_KEEPER (files next to it) make those steps fail.
_FAKE_SCHEDULER = r"""#!/bin/sh
d=$(dirname "$0")
case "$1" in
  use) [ -e "$d/FAIL_USE" ] && { echo "use broke" >&2; exit 1; }
       echo '{"ok": true, "backend": "daemon"}' ;;
  install-service)
       [ -e "$d/FAIL_KEEPER" ] && { echo "keeper broke" >&2; exit 1; }
       echo "$*" >> "$d/keeper.calls"
       echo '{"ok": true, "manager": "cron", "reason": "no systemd", "warnings": ["the daemon runs as root"]}' ;;
  daemon) while :; do sleep 1; done ;;
esac
"""


def _fake_scheduler(path) -> None:
    with open(path, "w") as fh:
        fh.write(_FAKE_SCHEDULER)
    subprocess.run(["chmod", "755", str(path)], check=True)


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
            bindir = argv[0].rsplit("/", 1)[0]
            if "runspec" in argv:  # the install step: stand in bin/runspec
                with open(f"{bindir}/runspec", "w") as fh:
                    fh.write(_FAKE_RUNSPEC)
                subprocess.run(["chmod", "755", f"{bindir}/runspec"], check=True)
            if any(a.startswith("runspec-scheduler") for a in argv):
                _fake_scheduler(f"{bindir}/runspec-scheduler")
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
    venv_update_preview = Bridge.venv_update_preview
    venv_update_start = Bridge.venv_update_start
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
    _deprov_stop_scheduler = Bridge._deprov_stop_scheduler
    _prov_pip_env = Bridge._prov_pip_env
    _venv_inspect = Bridge._venv_inspect
    _upd_plan = Bridge._upd_plan
    _upd_run = Bridge._upd_run


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
    # The half-built venv is gone, so the same provision can be retried.
    assert not (tmp_path / "venvs" / "ops").exists()
    assert "so you can provision again" in r["summary"]
    assert b.audit[-1]["extra"]["cleanup"] == "cleaned"


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_failed_provision_into_root_owned_parent_cleans_up_as_root(app, tmp_path):
    _hosts()
    b = _ReadOnlyParentBridge("sudo")
    b.fail_on = "runspec runspec-gttlab"
    dest = tmp_path / "venvs" / "ops"
    payload = {
        "hosts": ["web-01"],
        "dest": str(dest),
        "python": sys.executable,
        "packages": ["runspec-gttlab"],
    }
    r = _wait(b, b.provision_start(payload)["op"])["results"][0]
    assert not r["ok"] and r["step"] == "install"
    assert not dest.exists() and dest.parent.is_dir()
    root_cmds = [c for c in b.commands if c.startswith("sudo -n -u root ")]
    assert len(root_cmds) == 2 and root_cmds[1].endswith(f"{dest} all")
    # Nothing left in the way: the retry with the right name goes through.
    b.fail_on = ""
    payload["packages"] = ["runspec-gitlab"]
    r = _wait(b, b.provision_start(payload)["op"])["results"][0]
    assert r["ok"], r


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_failed_provision_keeps_a_kept_logs_folder(app, tmp_path):
    _hosts()
    b = _StubBridge()
    b.fail_on = "runspec runspec-linux"
    dest = tmp_path / "venvs" / "ops"
    (dest / "logs").mkdir(parents=True)
    (dest / "logs" / "runs.db").write_text("history")
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(dest),
            "python": sys.executable,
            "packages": ["runspec-linux"],
        }
    )
    r = _wait(b, out["op"])["results"][0]
    assert not r["ok"]
    assert [p.name for p in dest.iterdir()] == ["logs"]
    assert (dest / "logs" / "runs.db").read_text() == "history"


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_preflight_failure_touches_nothing(app, tmp_path):
    _hosts()
    b = _StubBridge()
    dest = tmp_path / "venvs" / "ops"
    dest.mkdir(parents=True)
    (dest / "mine.txt").write_text("not yours")
    out = b.provision_start(
        {"hosts": ["web-01"], "dest": str(dest), "python": sys.executable}
    )
    r = _wait(b, out["op"])["results"][0]
    assert not r["ok"] and r["step"] == "preflight"
    assert (dest / "mine.txt").read_text() == "not yours"
    assert b.audit[-1]["extra"]["cleanup"] == "none"


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


# ── the scheduler daemon (runspec-scheduler 0.6.0) ─────────────────────────────


def test_plan_adds_the_scheduler_package(app):
    _hosts()
    b = _StubBridge()
    base = {"hosts": ["web-01"], "dest": "/opt/venvs/ops", "python": "3.12"}
    with pytest.raises(ValueError, match="scheduler keeper"):
        b._prov_plan({**base, "scheduler": "systemd"})
    plan = b._prov_plan({**base, "scheduler": "supervisor"})
    assert plan["scheduler"] == "supervisor"
    assert plan["packages"] == ["runspec", "runspec-scheduler[supervisor]>=0.6.0"]
    pinned = b._prov_plan(
        {**base, "scheduler": "auto", "packages": ["runspec-scheduler==0.6.1"]}
    )
    assert pinned["packages"] == ["runspec", "runspec-scheduler==0.6.1"]
    assert b._prov_plan(base)["scheduler"] == ""


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_provision_sets_up_the_scheduler_daemon(app, tmp_path):
    _hosts()
    b = _StubBridge()
    dest = tmp_path / "venvs" / "ops"
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(dest),
            "python": sys.executable,
            "scheduler": "auto",
        }
    )
    rec = _wait(b, out["op"])
    [res] = rec["results"]
    assert res["ok"], res
    assert res["scheduler"] == {"manager": "cron", "reason": "no systemd"}
    assert "the daemon runs as root" in res["warnings"]
    joined = "\n".join(b.commands)
    assert f"{dest}/bin/runspec-scheduler use daemon" in joined
    assert (dest / "bin" / "keeper.calls").read_text().strip() == (
        "install-service --manager auto --write"
    )
    started = next(
        a for a in b.audit if a["extra"]["event"] == "venv.provision.started"
    )
    assert started["extra"]["scheduler"] == "auto"


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_provision_skips_the_keeper_when_use_fails(app, tmp_path, monkeypatch):
    _hosts()
    b = _StubBridge()
    b.fail_on = "runspec-scheduler use daemon"
    dest = tmp_path / "venvs" / "ops"
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(dest),
            "python": sys.executable,
            "scheduler": "cron",
        }
    )
    rec = _wait(b, out["op"])
    [res] = rec["results"]
    assert res["ok"], res  # the venv works; the scheduler is a warning
    assert any("use daemon" in w for w in res["warnings"])
    assert not any("install-service" in c for c in b.commands)


def _venv_with_scheduler(tmp_path):
    venv = tmp_path / "venvs" / "ops"
    assert (
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)]
        ).returncode
        == 0
    )
    (venv / "bin" / "runspec").write_text(_FAKE_RUNSPEC)
    _fake_scheduler(venv / "bin" / "runspec-scheduler")
    return venv


def test_deprovision_stops_the_scheduler_first(app, tmp_path):
    venv = _venv_with_scheduler(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    prev = b.deprovision_preview({"targets": [{"host": "web-01", "runspec_path": rp}]})
    assert (
        prev["rows"][0]["scheduler"] is True and prev["rows"][0]["scheduler_pid"] == 0
    )
    calls = venv / "bin" / "keeper.calls"
    out = b.deprovision_start(
        {"targets": [{"host": "web-01", "runspec_path": rp}], "confirm": "ops"}
    )
    rec = _wait(b, out["op"])
    assert rec["results"][0]["ok"], rec["results"]
    stop_at = next(
        i for i, c in enumerate(b.commands) if "install-service --remove --write" in c
    )
    rm_at = next(i for i, c in enumerate(b.commands) if "refused:" in c)
    assert stop_at < rm_at
    assert not calls.exists()  # gone with the venv (keep_logs keeps logs/ only)


def test_deprovision_keeps_a_venv_whose_daemon_wont_stop(app, tmp_path):
    venv = _venv_with_scheduler(tmp_path)
    (venv / "bin" / "FAIL_KEEPER").touch()
    daemon = subprocess.Popen([str(venv / "bin" / "runspec-scheduler"), "daemon"])
    try:
        (venv / "runspec_scheduler.lock").write_text(f"{daemon.pid}\n")
        rp = f"{venv}/bin/runspec"
        _hosts([rp])
        b = _StubBridge()
        prev = b.deprovision_preview(
            {"targets": [{"host": "web-01", "runspec_path": rp}]}
        )
        assert prev["rows"][0]["scheduler_pid"] == daemon.pid
        out = b.deprovision_start(
            {"targets": [{"host": "web-01", "runspec_path": rp}], "confirm": "ops"}
        )
        rec = _wait(b, out["op"])
        [res] = rec["results"]
        assert not res["ok"] and f"still running (pid {daemon.pid})" in res["summary"]
        assert (venv / "pyvenv.cfg").exists()
        assert not any("refused:" in c for c in b.commands)  # remove never ran

        # With the daemon gone, an old scheduler that can't --remove is no reason to keep it.
        daemon.kill()
        daemon.wait()
        out = b.deprovision_start(
            {"targets": [{"host": "web-01", "runspec_path": rp}], "confirm": "ops"}
        )
        rec = _wait(b, out["op"])
        assert rec["results"][0]["ok"], rec["results"]
    finally:
        if daemon.poll() is None:
            daemon.kill()


# ── the log retention policy (runspec 0.80.0) ─────────────────────────────────


def test_plan_validates_the_retention_policy(app):
    _hosts()
    b = _StubBridge()
    base = {"hosts": ["web-01"], "dest": "/opt/venvs/ops", "python": "3.12"}
    assert b._prov_plan(base)["retention"] == {}
    plan = b._prov_plan(
        {**base, "retention": {"compact_after": "7D", "delete_after": " 90d "}}
    )
    assert plan["retention"] == {"compact_after": "7d", "delete_after": "90d"}
    assert b._prov_plan({**base, "retention": {"delete_after": "30d"}})[
        "retention"
    ] == {"delete_after": "30d"}
    with pytest.raises(ValueError, match="shorter than"):
        b._prov_plan(
            {**base, "retention": {"compact_after": "90d", "delete_after": "7d"}}
        )
    with pytest.raises(ValueError, match="Not a duration"):
        b._prov_plan({**base, "retention": {"delete_after": "forever"}})


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_provision_sets_the_retention_policy(app, tmp_path):
    _hosts()
    b = _StubBridge()
    dest = tmp_path / "venvs" / "ops"
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(dest),
            "python": sys.executable,
            "retention": {"compact_after": "7d", "delete_after": "90d"},
        }
    )
    rec = _wait(b, out["op"])
    [res] = rec["results"]
    assert res["ok"] and not res["warnings"], res
    joined = "\n".join(b.commands)
    assert "logs retention --compact-after 7d --delete-after 90d" in joined
    # After the inbox, before verify.
    order = [
        next(i for i, c in enumerate(b.commands) if needle in c)
        for needle in ("share --inbox", "logs retention", "--version")
    ]
    assert order == sorted(order)
    steps = [
        d.get("step")
        for e, d in b.dispatched
        if e == "runspec:provision_progress" and d.get("phase") == "step"
    ]
    assert "retention" in steps


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_retention_failure_is_a_warning(app, tmp_path):
    _hosts()
    b = _StubBridge()
    b.fail_on = "logs retention"
    dest = tmp_path / "venvs" / "ops"
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(dest),
            "python": sys.executable,
            "retention": {"compact_after": "7d", "delete_after": "90d"},
        }
    )
    rec = _wait(b, out["op"])
    [res] = rec["results"]
    assert res["ok"], res
    [w] = [w for w in res["warnings"] if "retention" in w]
    assert "0.80.0" in w
    assert "runspec logs retention --compact-after 7d --delete-after 90d" in w


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_no_retention_step_without_a_policy(app, tmp_path):
    _hosts()
    b = _StubBridge()
    out = b.provision_start(
        {
            "hosts": ["web-01"],
            "dest": str(tmp_path / "venvs" / "ops"),
            "python": sys.executable,
        }
    )
    _wait(b, out["op"])
    assert not any("logs retention" in c for c in b.commands)


# ── Update an existing venv ──────────────────────────────────────────────────


def _step_cmds(b) -> str:
    """The commands an update ran on the host, without the read-only inspect
    script (which mentions every file it looks at)."""
    return "\n".join(c for c in b.commands if not c.startswith("sh -c "))


def _old_venv(tmp_path, name="ops", version="0.79.0"):
    """A venv made by an earlier provision: its own runspec, run history in
    logs/, no inbox, no log cleanup, no scheduler."""
    venv = tmp_path / "venvs" / name
    assert (
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)]
        ).returncode
        == 0
    )
    (venv / "bin" / "runspec").write_text(
        f'#!/bin/sh\n[ "$1" = --version ] && echo "runspec {version}" && exit 0\necho "runspec $*"\n'
    )
    subprocess.run(["chmod", "755", str(venv / "bin" / "runspec")], check=True)
    (venv / "logs").mkdir()
    (venv / "logs" / "run.log").write_text("history\n")
    (venv / "installed.marker").write_text("keep me\n")
    return venv


def test_update_preview_reports_the_venv_state(app, tmp_path):
    venv = _old_venv(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    with pytest.raises(ValueError, match="not one of"):
        b.venv_update_preview(
            {"targets": [{"host": "web-01", "runspec_path": "/etc/bin/runspec"}]}
        )
    res = b.venv_update_preview(
        {
            "targets": [
                {"host": "web-01", "runspec_path": rp},
                {"host": "db-01", "runspec_path": "/opt/venvs/old/bin/runspec"},
            ]
        }
    )
    rows = {r["host"]: r for r in res["rows"]}
    web = rows["web-01"]
    assert web["ok"] and web["kind"] == "venv"
    assert web["runspec_version"] == "0.79.0"
    assert web["inbox"] is False and web["retention"] == {}
    assert web["scheduler"] is False and web["backend"] == ""
    assert rows["db-01"]["blockers"] == ["Host is not connected."]

    (venv / "logs" / "inbox").mkdir()
    (venv / "runspec_logs_retention.toml").write_text(
        '# set by runspec logs retention\ncompact_after = "7d"\ndelete_after = "90d"\n'
    )
    _fake_scheduler(venv / "bin" / "runspec-scheduler")
    (venv / "runspec_scheduler.toml").write_text('[scheduler]\nbackend = "daemon"\n')
    [web] = b.venv_update_preview(
        {"targets": [{"host": "web-01", "runspec_path": rp}]}
    )["rows"]
    assert web["inbox"] is True
    assert web["retention"] == {"compact_after": "7d", "delete_after": "90d"}
    assert web["scheduler"] is True and web["backend"] == "daemon"


def test_update_plan_validation(app, tmp_path):
    venv = _old_venv(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    t = {"targets": [{"host": "web-01", "runspec_path": rp}]}
    with pytest.raises(ValueError, match="at least one thing"):
        b._upd_plan(t)
    with pytest.raises(ValueError, match="scheduler keeper"):
        b._upd_plan({**t, "scheduler": "systemd"})
    with pytest.raises(ValueError, match="shorter than"):
        b._upd_plan({**t, "retention": {"compact_after": "9d", "delete_after": "1d"}})
    with pytest.raises(ValueError, match="package spec"):
        b._upd_plan({**t, "packages": ["--index-url=http://evil"]})
    plan = b._upd_plan({**t, "upgrade_runspec": True, "scheduler": "supervisor"})
    assert plan["specs"] == ["runspec", "runspec-scheduler[supervisor]>=0.6.0"]
    assert b._upd_plan({**t, "scheduler": "off"})["specs"] == []
    assert b._upd_plan({**t, "inbox": True})["inbox"] is True


@pytest.mark.skipif(sys.version_info < (3, 10), reason="runspec needs 3.10+")
def test_update_applies_the_new_options_in_place(app, tmp_path):
    venv = _old_venv(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    hosts_before = config_mod.hosts_path().read_text()
    b = _StubBridge()
    out = b.venv_update_start(
        {
            "targets": [{"host": "web-01", "runspec_path": rp}],
            "upgrade_runspec": True,
            "inbox": True,
            "retention": {"compact_after": "7d", "delete_after": "90d"},
            "scheduler": "auto",
        }
    )
    rec = _wait(b, out["op"])
    assert rec["kind"] == "update"
    [res] = rec["results"]
    assert res["ok"], res
    assert res["scheduler"] == {"manager": "cron", "reason": "no systemd"}
    joined = _step_cmds(b)
    assert "pip install --disable-pip-version-check --no-input -U runspec " in joined
    assert "runspec-scheduler[supervisor]>=0.6.0" in joined
    assert f"{venv}/bin/runspec logs share --inbox" in joined
    assert (
        f"{venv}/bin/runspec logs retention --compact-after 7d --delete-after 90d"
        in joined
    )
    assert f"{venv}/bin/runspec-scheduler use daemon" in joined
    assert "install-service --manager auto --write" in joined
    assert "audit record venv.update --outcome succeeded" in joined
    assert " -m venv " not in joined  # never recreated
    # The venv, its installs and its history are all still there.
    assert (venv / "installed.marker").read_text() == "keep me\n"
    assert (venv / "logs" / "run.log").read_text() == "history\n"
    # The host already lists the venv: the hosts file is untouched.
    assert config_mod.hosts_path().read_text() == hosts_before
    events = [a["extra"]["event"] for a in b.audit]
    assert events == ["venv.update.started", "venv.update.succeeded"]
    assert b.audit[-1]["extra"]["applied"] == [
        "pip",
        "install",
        "inbox",
        "retention",
        "scheduler",
        "keeper",
    ]
    steps = [
        d["step"]
        for e, d in b.dispatched
        if e == "runspec:provision_progress" and d["phase"] == "step"
    ]
    assert steps == [
        "inspect",
        "pip",
        "install",
        "inbox",
        "retention",
        "verify",
        "scheduler",
        "keeper",
    ]
    assert b.refreshed == 1


def test_update_only_runs_the_chosen_steps(app, tmp_path):
    venv = _old_venv(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    out = b.venv_update_start(
        {
            "targets": [{"host": "web-01", "runspec_path": rp}],
            "retention": {"compact_after": "14d"},
        }
    )
    [res] = _wait(b, out["op"])["results"]
    assert res["ok"], res
    assert res["runspec_version"] == "0.79.0"
    joined = _step_cmds(b)
    assert "pip" not in joined
    assert "logs share" not in joined
    assert "runspec-scheduler" not in joined
    assert "logs retention --compact-after 14d" in joined


def test_update_install_failure_stops_that_host(app, tmp_path):
    venv = _old_venv(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    b.fail_on = "-U runspec"
    out = b.venv_update_start(
        {
            "targets": [{"host": "web-01", "runspec_path": rp}],
            "upgrade_runspec": True,
            "inbox": True,
        }
    )
    [res] = _wait(b, out["op"])["results"]
    assert not res["ok"]
    assert res["step"] == "install"
    assert "install failed" in res["summary"]
    assert "logs share" not in _step_cmds(b)
    assert "audit record venv.update --outcome failed" in _step_cmds(b)
    assert [a["extra"]["event"] for a in b.audit][-1] == "venv.update.failed"
    assert venv.exists() and (venv / "logs" / "run.log").exists()
    assert b.refreshed == 0


def test_update_moves_schedules_back_to_crontab(app, tmp_path):
    venv = _old_venv(tmp_path)
    rp = f"{venv}/bin/runspec"
    _hosts([rp])
    b = _StubBridge()
    # No scheduler in the venv: nothing to stop, and that's said.
    out = b.venv_update_start(
        {"targets": [{"host": "web-01", "runspec_path": rp}], "scheduler": "off"}
    )
    [res] = _wait(b, out["op"])["results"]
    assert res["ok"], res
    assert any("no daemon to stop" in w for w in res["warnings"])
    assert "runspec-scheduler" not in _step_cmds(b)

    _fake_scheduler(venv / "bin" / "runspec-scheduler")
    b = _StubBridge()
    out = b.venv_update_start(
        {"targets": [{"host": "web-01", "runspec_path": rp}], "scheduler": "off"}
    )
    [res] = _wait(b, out["op"])["results"]
    assert res["ok"], res
    cmds = [c for c in _step_cmds(b).splitlines() if "runspec-scheduler" in c]
    assert cmds[0].endswith("runspec-scheduler use cron")
    assert cmds[1].endswith("install-service --remove --write")
    assert res["scheduler"]["manager"] == "crontab"

    # `use cron` failing leaves the daemon (and its keeper) alone.
    (venv / "bin" / "FAIL_USE").write_text("")
    b = _StubBridge()
    out = b.venv_update_start(
        {"targets": [{"host": "web-01", "runspec_path": rp}], "scheduler": "off"}
    )
    [res] = _wait(b, out["op"])["results"]
    assert res["ok"]
    assert not any("install-service" in c for c in b.commands)
    assert any("left running" in w for w in res["warnings"])
