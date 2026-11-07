"""test_provision.py — the pure venv provisioning module.

Validation and guards, the interpreter probe / preflight / inspect parsers, and
the step scripts themselves run for real under ``sh`` against a temp dir (the
same scripts the bridge sends over SSH), including the deprovision guards.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from runspec_console import provision as prov

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")


def _sh(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


# ── validation ──


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/opt/venvs/ops", "/opt/venvs/ops"),
        ("/opt/venvs/ops/", "/opt/venvs/ops"),
        ("/srv/tools/../tools/ops-1.2", "/srv/tools/ops-1.2"),
        ("/home/svc/.venvs/ops", "/home/svc/.venvs/ops"),
    ],
)
def test_normalize_dest_accepts(path, expected):
    assert prov.normalize_dest(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative/venv",
        "/",
        "/opt",
        "/usr/local",
        "/opt/venvs",
        "/home/jason",
        "/Users/jason",
        "/venv",
        "C:\\venvs\\ops",
        "/opt/venvs/bad name",
        "/opt/venvs/.hidden",
    ],
)
def test_normalize_dest_refuses(path):
    with pytest.raises(prov.ProvisionError):
        prov.normalize_dest(path)


def test_validate_user_and_become():
    assert prov.validate_user("") == ""
    assert prov.validate_user(" svc-ops ") == "svc-ops"
    with pytest.raises(prov.ProvisionError):
        prov.validate_user("svc; rm -rf /")
    assert prov.validate_become("SU") == "su"
    with pytest.raises(prov.ProvisionError):
        prov.validate_become("doas")


def test_root_from_runspec_path():
    assert prov.root_from_runspec_path("/opt/venvs/ops/bin/runspec") == "/opt/venvs/ops"
    assert prov.root_from_runspec_path("/opt/venvs/ops/bin") == "/opt/venvs/ops"
    with pytest.raises(prov.ProvisionError):
        prov.root_from_runspec_path("runspec")
    with pytest.raises(prov.ProvisionError):
        prov.root_from_runspec_path("C:\\venvs\\ops\\Scripts\\runspec.exe")


def test_versions():
    assert prov.version_ok_for_runspec("3.12.4")
    assert prov.version_ok_for_runspec("3.10.0")
    assert not prov.version_ok_for_runspec("3.9.18")
    assert not prov.version_ok_for_runspec("garbage")
    assert prov.matches_minor("3.12.4", "3.12")
    assert not prov.matches_minor("3.12.4", "3.1")
    assert prov.matches_minor("3.12.4", "3.12.4")


def test_install_specs():
    assert prov.install_specs([]) == ["runspec"]
    assert prov.install_specs(["runspec-linux", "runspec", "runspec-linux"]) == [
        "runspec",
        "runspec-linux",
    ]
    assert prov.install_specs(["runspec==0.70.0", "runspec-fs"]) == [
        "runspec==0.70.0",
        "runspec-fs",
    ]


# ── parsers ──


def test_parse_probe_dedupes_on_resolved_path():
    out = (
        "noise\n"
        "RSPY\t/opt/py312/bin/python3.12\t/opt/py312/bin/python3.12\t3.12.4\n"
        "RSPY\t/usr/bin/python3\t/usr/bin/python3.9\t3.9.18\n"
        "RSPY\t/usr/bin/python3.9\t/usr/bin/python3.9\t3.9.18\n"
        "RSPY\t/usr/bin/python3.11\t/usr/bin/python3.11\tbroken\n"
        "RSID\tdeploy\tLinux\n"
    )
    res = prov.parse_probe(out)
    assert res.user == "deploy" and res.os == "Linux"
    assert [i.path for i in res.pythons] == [
        "/opt/py312/bin/python3.12",
        "/usr/bin/python3",
    ]
    assert [i.runspec_ok for i in res.pythons] == [True, False]
    assert (
        prov.pick_interpreter(res.pythons, "3.12").path == "/opt/py312/bin/python3.12"
    )
    assert (
        prov.pick_interpreter(res.pythons, "/usr/bin/python3.9").path
        == "/usr/bin/python3"
    )
    assert prov.pick_interpreter(res.pythons, "3.13") is None


def test_preflight_verdict():
    good = {
        "user": "svc",
        "python_version": "3.12.4",
        "venv": "ok",
        "dest": "absent",
        "ancestor": "/opt/venvs",
        "writable": "yes",
    }
    assert prov.preflight_verdict(good) == ([], [])
    blockers, _ = prov.preflight_verdict({**good, "python_version": "3.9.18"})
    assert any("too old" in b for b in blockers)
    blockers, _ = prov.preflight_verdict({**good, "venv": "missing"})
    assert any("python3.12-venv" in b for b in blockers)
    blockers, _ = prov.preflight_verdict({**good, "dest": "notempty", "writable": "no"})
    assert len(blockers) == 2
    blockers, warnings = prov.preflight_verdict({**good, "dest": "logs_only"})
    assert not blockers and warnings
    blockers, _ = prov.preflight_verdict({})
    assert blockers and "account" in blockers[0]


def test_preflight_verdict_root_handoff():
    facts = {
        "user": "svc-ops",
        "python_version": "3.12.4",
        "venv": "ok",
        "dest": "absent",
        "ancestor": "/opt/venvs",
        "writable": "no",
    }
    assert prov.needs_handoff(facts)
    # Not asked / no passwordless sudo: still a blocker, with a hint for the latter.
    blockers, _ = prov.preflight_verdict(facts)
    assert blockers == ["/opt/venvs isn't writable by svc-ops."]
    blockers, _ = prov.preflight_verdict(facts, root_via="no")
    assert "can't sudo" in blockers[0]
    # sudo / root login: a warning, the folder is made by root.
    for via in ("sudo", "root"):
        blockers, warnings = prov.preflight_verdict(facts, root_via=via)
        assert not blockers
        assert "as root" in warnings[0]
    # An existing folder with contents is never chowned.
    for dest in ("logs_only", "notempty"):
        assert not prov.needs_handoff({**facts, "dest": dest})
    assert prov.needs_handoff({**facts, "dest": "empty"})
    assert not prov.needs_handoff({**facts, "writable": "yes"})


def test_root_check_and_handoff_argv():
    assert prov.parse_root_check("noise\nsudo\n") == "sudo"
    assert prov.parse_root_check("") == "no"
    assert prov.handoff_argv("/usr/bin/python3", "/opt/venvs/x", "svc-ops")[-2:] == [
        "/opt/venvs/x",
        "svc-ops",
    ]
    with pytest.raises(prov.ProvisionError):
        prov.handoff_argv("/usr/bin/python3", "/opt/venvs/x", "")
    with pytest.raises(prov.ProvisionError):
        prov.handoff_argv("/usr/bin/python3", "/opt/venvs/x", "bad;name")


def test_audit_argv():
    argv = prov.audit_record_argv(
        "/opt/venvs/ops",
        "venv.provision",
        "succeeded",
        target={"path": "/opt/venvs/ops"},
        detail={"packages": ["runspec", "runspec-linux"], "root": True, "empty": ""},
    )
    assert argv == [
        "/opt/venvs/ops/bin/runspec",
        "audit",
        "record",
        "venv.provision",
        "--outcome",
        "succeeded",
        "--target",
        "path=/opt/venvs/ops",
        "--detail",
        "packages=runspec runspec-linux",
        "--detail",
        "root=true",
    ]
    log = prov.audit_logger_argv(
        "venv.deprovision", "confirmed", target={"path": "/x"}, detail={"a": ""}
    )
    assert log[:5] == ["logger", "-t", "runspec", "-p", "auth.notice"]
    assert log[5].startswith("runspec-audit {")
    assert '"a"' not in log[5] and '"path": "/x"' in log[5]


def test_host_entry_edits():
    e = {"name": "web", "runspec_path": "/opt/venvs/a/bin/runspec"}
    assert prov.add_runspec_path(e, "/opt/venvs/b/bin/runspec")
    assert e["runspec_paths"] == [
        "/opt/venvs/a/bin/runspec",
        "/opt/venvs/b/bin/runspec",
    ]
    assert "runspec_path" not in e
    assert not prov.add_runspec_path(e, "/opt/venvs/b/bin/runspec")
    assert prov.remove_runspec_path(e, "/opt/venvs/a")
    assert e["runspec_paths"] == ["/opt/venvs/b/bin/runspec"]
    assert not prov.remove_runspec_path(e, "/opt/venvs/zzz")
    assert prov.remove_runspec_path(e, "/opt/venvs/b")
    assert "runspec_paths" not in e


def test_audit_record_shape():
    rec = prov.audit_record(
        "venv.provision.failed",
        "2026-10-02T00:00:00Z",
        host="web",
        path="/opt/venvs/ops",
        error="x",
        run_as="",
    )
    assert rec["level"] == "WARNING"
    assert rec["extra"]["event"] == "venv.provision.failed"
    assert "run_as" not in rec["extra"]
    assert rec["message"] == "venv.provision.failed web:/opt/venvs/ops"


# ── the scripts, run for real ──


@posix_only
def test_probe_script_finds_this_interpreter():
    res = prov.parse_probe(_sh(prov.probe_argv([sys.executable])).stdout)
    assert res.user
    assert res.pythons and res.pythons[0].path == sys.executable


@posix_only
def test_preflight_create_inspect_remove_roundtrip(tmp_path):
    dest = str(tmp_path / "ops")
    facts = prov.parse_preflight(_sh(prov.preflight_argv(sys.executable, dest)).stdout)
    assert facts["dest"] == "absent"
    assert facts["writable"] == "yes"
    assert facts["venv"] == "ok"
    assert prov.preflight_verdict(facts)[0] == [] or not prov.version_ok_for_runspec(
        facts["python_version"]
    )

    created = _sh(prov.create_argv(sys.executable, dest))
    assert created.returncode == 0, created.stdout + created.stderr
    assert (tmp_path / "ops" / "pyvenv.cfg").exists()
    assert (tmp_path / "ops" / "logs").is_dir()
    # A real venv has no bin/runspec until install; stand one in.
    runspec = tmp_path / "ops" / "bin" / "runspec"
    runspec.write_text("#!/bin/sh\necho runspec 0.0\n")
    runspec.chmod(0o755)
    (tmp_path / "ops" / "logs" / "run.log").write_text("x\n")

    facts = prov.parse_preflight(_sh(prov.preflight_argv(sys.executable, dest)).stdout)
    assert facts["dest"] == "notempty"

    info = prov.parse_inspect(_sh(prov.inspect_argv(dest)).stdout)
    assert info["kind"] == "venv"
    assert info["owner"]

    removed = _sh(prov.remove_argv(sys.executable, dest, keep_logs=True))
    assert removed.returncode == 0, removed.stdout
    assert sorted(p.name for p in (tmp_path / "ops").iterdir()) == ["logs"]
    assert (tmp_path / "ops" / "logs" / "run.log").exists()

    # Only logs/ left: preflight sees it, and a new venv builds around it.
    facts = prov.parse_preflight(_sh(prov.preflight_argv(sys.executable, dest)).stdout)
    assert facts["dest"] == "logs_only"
    assert _sh(prov.create_argv(sys.executable, dest)).returncode == 0
    assert (tmp_path / "ops" / "logs" / "run.log").exists()
    runspec.write_text("#!/bin/sh\necho runspec 0.0\n")
    runspec.chmod(0o755)

    removed = _sh(prov.remove_argv(sys.executable, dest, keep_logs=False))
    assert removed.returncode == 0, removed.stdout
    assert not (tmp_path / "ops").exists()


@posix_only
def test_remove_script_refuses_non_venvs(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "keep.txt").write_text("precious")
    r = _sh(prov.remove_argv(sys.executable, str(plain), keep_logs=False))
    assert r.returncode == 64 and "not a venv" in r.stdout
    assert (plain / "keep.txt").exists()

    # A symlink to a real venv is refused, never followed.
    venv = tmp_path / "real"
    assert _sh(prov.create_argv(sys.executable, str(venv))).returncode == 0
    (venv / "bin" / "runspec").write_text("")
    link = tmp_path / "link"
    link.symlink_to(venv)
    r = _sh(prov.remove_argv(sys.executable, str(link), keep_logs=False))
    assert r.returncode == 64 and "symlink" in r.stdout
    assert venv.exists()


def test_remove_argv_refuses_protected():
    for bad in ("/", "/opt", "/home/jason", "relative"):
        with pytest.raises(prov.ProvisionError):
            prov.remove_argv("/usr/bin/python3", bad, keep_logs=False)


@posix_only
def test_handoff_script_makes_and_refuses(tmp_path):
    import getpass

    me = getpass.getuser()
    dest = tmp_path / "parent" / "venv"
    r = _sh(prov.handoff_argv(sys.executable, str(dest), me))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dest.is_dir() and oct(dest.stat().st_mode & 0o777) == "0o755"
    # Idempotent on an empty dir, refuses anything else.
    assert _sh(prov.handoff_argv(sys.executable, str(dest), me)).returncode == 0
    (dest / "f").write_text("x")
    r = _sh(prov.handoff_argv(sys.executable, str(dest), me))
    assert r.returncode == 64 and "not empty" in r.stdout
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "parent")
    r = _sh(prov.handoff_argv(sys.executable, str(link), me))
    assert r.returncode == 64 and "symlink" in r.stdout
    r = _sh(
        prov.handoff_argv(sys.executable, str(tmp_path / "other"), "no-such-user-xyz")
    )
    assert r.returncode == 64 and "no such user" in r.stdout
    assert not (tmp_path / "other").exists()
    r = _sh(["sh", "-c", prov.ROOT_CHECK_SCRIPT, "sh"])
    assert prov.parse_root_check(r.stdout) in ("root", "sudo", "no")


@posix_only
def test_cleanup_script_undoes_only_what_provision_made(tmp_path):
    # Absent before: the whole folder goes, the parent mkdir -p made stays.
    dest = tmp_path / "venvs" / "ops"
    assert _sh(prov.create_argv(sys.executable, str(dest))).returncode == 0
    r = _sh(prov.cleanup_argv(sys.executable, str(dest), "absent"))
    assert r.returncode == 0 and "removed" in r.stdout
    assert not dest.exists() and dest.parent.is_dir()
    # Nothing there any more: a no-op, not an error.
    assert _sh(prov.cleanup_argv(sys.executable, str(dest), "absent")).returncode == 0

    # Empty before: the folder stays, emptied.
    dest.mkdir()
    assert _sh(prov.create_argv(sys.executable, str(dest))).returncode == 0
    assert _sh(prov.cleanup_argv(sys.executable, str(dest), "empty")).returncode == 0
    assert dest.is_dir() and not list(dest.iterdir())

    # Only a kept logs/ before: logs/ and its history survive.
    (dest / "logs").mkdir()
    (dest / "logs" / "runs.db").write_text("history")
    assert _sh(prov.create_argv(sys.executable, str(dest))).returncode == 0
    assert (
        _sh(prov.cleanup_argv(sys.executable, str(dest), "logs_only")).returncode == 0
    )
    assert [p.name for p in dest.iterdir()] == ["logs"]
    assert (dest / "logs" / "runs.db").read_text() == "history"

    # A symlink is refused, never followed.
    link = tmp_path / "link"
    link.symlink_to(dest)
    r = _sh(prov.cleanup_argv(sys.executable, str(link), "absent"))
    assert r.returncode == 64 and "symlink" in r.stdout
    assert (dest / "logs").exists()


def test_cleanup_argv_refuses_states_it_did_not_make():
    for before in ("notempty", "symlink", "file", ""):
        with pytest.raises(prov.ProvisionError):
            prov.cleanup_argv("/usr/bin/python3", "/opt/venvs/ops", before)
    for bad in ("/", "/opt", "/home/jason"):
        with pytest.raises(prov.ProvisionError):
            prov.cleanup_argv("/usr/bin/python3", bad, "absent")


@posix_only
def test_create_script_never_chmods_through_symlinks(tmp_path):
    # A stand-in "python -m venv --symlinks" whose bin/python points outside the
    # venv, like the real one does at the system interpreter.
    target = tmp_path / "system-python"
    target.write_text("x")
    target.chmod(0o700)
    fake_py = tmp_path / "fakepy"
    fake_py.write_text(
        '#!/bin/sh\nmkdir -p "$4/bin"\nln -s "%s" "$4/bin/python"\n' % target
    )
    fake_py.chmod(0o755)
    dest = tmp_path / "v"
    argv = prov.create_argv(str(fake_py), str(dest))
    argv[0] = sys.executable  # the script runs on Python; fake_py only makes the venv
    r = _sh(argv)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (target.stat().st_mode & 0o777) == 0o700
    assert ((dest / "bin").stat().st_mode & 0o777) == 0o755


# ── failure text ──

# What sudo prints when a sudoers rule doesn't cover the command: the refused
# command, multi-line script and all.
_SUDO_REFUSED = (
    "Sorry, user deploy is not allowed to execute '/usr/bin/python3.12 -I -S -c "
    + prov.PREFLIGHT_SCRIPT
    + " /opt/venvs/ops' as svc on web-01."
)


def test_failure_text_explains_a_sudo_refusal_without_the_script():
    msg = prov.failure_text(_SUDO_REFUSED, 1, run_as="svc")
    assert msg.startswith("sudo refused") and "svc" in msg
    assert "/usr/bin/python3.12" in msg
    assert "import" not in msg and "out(" not in msg
    assert "run that command as svc" in prov.failure_text(
        "user deploy is not allowed to execute", 1, run_as="svc"
    )


def test_escalated_steps_never_run_a_shell():
    """Every step that runs as another account runs Python, not ``sh -c`` — a
    sudoers rule that refuses shells must still cover provisioning."""
    py = "/usr/bin/python3.12"
    argvs = [
        prov.preflight_argv(py, "/opt/venvs/ops"),
        prov.create_argv(py, "/opt/venvs/ops"),
        prov.handoff_argv(py, "/opt/venvs/ops", "svc"),
        prov.cleanup_argv(py, "/opt/venvs/ops", "absent"),
        prov.remove_argv("/opt/venvs/ops/bin/python", "/opt/venvs/ops", keep_logs=True),
    ]
    for argv in argvs:
        assert argv[0].endswith(("python3.12", "/bin/python"))
        assert argv[1:4] == ["-I", "-S", "-c"]
    with pytest.raises(prov.ProvisionError):
        prov.remove_argv("", "/opt/venvs/ops", keep_logs=True)


@posix_only
def test_scripts_are_python36_syntax():
    """A too-old interpreter must still run the preflight to say it is too old,
    so the escalated scripts avoid syntax newer than 3.6 (no f-strings, walrus)."""
    import ast

    for script in (
        prov.PREFLIGHT_SCRIPT,
        prov.CREATE_SCRIPT,
        prov.HANDOFF_SCRIPT,
        prov.CLEANUP_SCRIPT,
        prov.REMOVE_SCRIPT,
    ):
        ast.parse(script, feature_version=(3, 6))
        assert 'f"' not in script and "f'" not in script


@pytest.mark.parametrize(
    "text,expect",
    [
        ("sudo: a password is required", "needs a password"),
        ("deploy is not in the sudoers file.", "can't use sudo"),
        ("Sorry, user deploy may not run sudo on web-01.", "can't use sudo"),
    ],
)
def test_failure_text_sudo_variants(text, expect):
    msg = prov.failure_text(text, 1, run_as="svc")
    assert expect in msg


def test_failure_text_drops_echoed_script_lines():
    text = "\n".join(
        [*prov.PREFLIGHT_SCRIPT.strip().splitlines()[-3:], "boom: no such thing"]
    )
    assert prov.failure_text(text, 2) == "boom: no such thing"
    assert prov.failure_text(prov.PREFLIGHT_SCRIPT, 2) == "exit 2"
    assert prov.failure_text("", 3) == "exit 3"


# ── Update an existing venv ──────────────────────────────────────────────────


def test_update_install_specs():
    assert prov.update_install_specs([], upgrade_runspec=False, scheduler="") == []
    assert prov.update_install_specs(
        ["runspec-linux", "runspec"], upgrade_runspec=True, scheduler=""
    ) == ["runspec", "runspec-linux"]
    assert prov.update_install_specs([], upgrade_runspec=False, scheduler="cron") == [
        "runspec-scheduler>=0.6.0"
    ]
    # A pinned scheduler wins; "off" installs nothing.
    assert prov.update_install_specs(
        ["runspec-scheduler==0.6.1"], upgrade_runspec=False, scheduler="auto"
    ) == ["runspec-scheduler==0.6.1"]
    assert prov.update_install_specs([], upgrade_runspec=False, scheduler="off") == []


def test_update_steps_order_and_flags():
    root = "/opt/venvs/ops"
    steps = prov.update_steps(
        root,
        specs=["runspec"],
        inbox=True,
        retention={"delete_after": "30d"},
        scheduler="auto",
        has_scheduler=False,
        acl_user="deploy",
    )
    assert [s.name for s in steps] == [
        "pip",
        "install",
        "inbox",
        "retention",
        "verify",
        "scheduler",
        "keeper",
    ]
    by = {s.name: s for s in steps}
    assert by["install"].pip and by["install"].required
    assert by["install"].argv[:3] == [f"{root}/bin/python", "-m", "pip"]
    assert "-U" in by["install"].argv
    assert by["inbox"].argv[-2:] == ["--acl-user", "deploy"]
    assert by["retention"].argv[-2:] == ["--delete-after", "30d"]
    assert by["verify"].required and not by["inbox"].required
    # Only verify when nothing else is asked for; "off" needs a scheduler.
    bare = prov.update_steps(
        root, specs=[], inbox=False, retention={}, scheduler="off", has_scheduler=False
    )
    assert [s.name for s in bare] == ["verify"]
    off = prov.update_steps(
        root, specs=[], inbox=False, retention={}, scheduler="off", has_scheduler=True
    )
    assert [s.argv[1:] for s in off[1:]] == [
        ["use", "cron"],
        ["install-service", "--remove", "--write"],
    ]


def test_update_restarts_a_running_daemon_after_upgrades():
    root = "/opt/venvs/ops"

    def names(**kw):
        base = {
            "specs": ["runspec"],
            "inbox": False,
            "retention": {},
            "scheduler": "",
            "has_scheduler": True,
            "daemon_running": True,
        }
        return [s.name for s in prov.update_steps(root, **{**base, **kw})]

    steps = prov.update_steps(
        root,
        specs=["runspec"],
        inbox=False,
        retention={},
        scheduler="",
        has_scheduler=True,
        daemon_running=True,
    )
    assert [s.name for s in steps] == ["pip", "install", "verify", "restart"]
    assert steps[-1].argv == [
        f"{root}/bin/runspec-scheduler",
        "restart",
        "--wait",
        "15",
    ]
    assert not steps[-1].required
    # after the keeper when moving to the daemon too
    assert names(scheduler="auto")[-3:] == ["scheduler", "keeper", "restart"]
    # nothing upgraded, no daemon running, or the daemon is being stopped: no restart
    assert "restart" not in names(specs=[])
    assert "restart" not in names(daemon_running=False)
    assert "restart" not in names(scheduler="off")


def test_update_scheduler_and_version_helpers():
    assert prov.validate_update_scheduler("off") == "off"
    assert prov.validate_update_scheduler("") == ""
    with pytest.raises(prov.ProvisionError):
        prov.validate_update_scheduler("systemd")
    assert prov.runspec_version("runspec 0.81.0") == "0.81.0"
    assert prov.runspec_version("runspec (version unknown)") == ""
    assert prov.version_at_least("0.80.0", "0.80.0")
    assert prov.version_at_least("runspec 0.81.2", "0.80.0")
    assert not prov.version_at_least("0.79.9", "0.80.0")
    assert not prov.version_at_least("", "0.80.0")
    assert prov.parse_retention_fact("compact_after=7d delete_after=90d junk ") == {
        "compact_after": "7d",
        "delete_after": "90d",
    }


def test_schedule_counts():
    def status(**kw):
        return "warning: something\n" + json.dumps({"schedules": [], **kw}) + "\n"

    assert prov.scheduler_status_argv("/opt/v/") == [
        "/opt/v/bin/runspec-scheduler",
        "status",
    ]
    assert prov.schedule_counts("") is None
    assert prov.schedule_counts('{"ok": false}') is None
    rows = [
        {
            "id": "a",
            "enabled": True,
            "when": {"cron": "0 2 * * *"},
            "installed": True,
            "line": "exec",
        },
        {"id": "b", "enabled": True, "when": {"every": "15m"}, "installed": False},
        {
            "id": "c",
            "enabled": False,
            "when": {"cron": "* * * * *"},
            "installed": False,
        },
    ]
    assert prov.schedule_counts(
        status(backend="cron", schedules=rows, stale=[{"id": "x"}])
    ) == {
        "total": 3,
        "disabled": 1,
        "cron": 1,
        "every": 1,
        "in_crontab": 1,
        "leftover": 1,
    }
    # A crontab that couldn't be read: the count is unknown, not 0.
    unread = [{**rows[0], "installed": None}]
    assert (
        prov.schedule_counts(status(backend="cron", schedules=unread))["in_crontab"]
        is None
    )
    # On the daemon nothing is in the crontab by design; leftover lines still count.
    daemon = prov.schedule_counts(
        status(backend="daemon", schedules=rows, stale=[{"id": "a"}])
    )
    assert daemon["in_crontab"] == 0 and daemon["leftover"] == 1


def test_parse_cron_clear():
    assert prov.parse_cron_clear(
        'noise\n{"ok": true, "backend": "daemon", "crontab": {"ok": true, "errors": []}}'
    ) == {"ok": True, "error": ""}
    bad = '{"ok": false, "error": "could not remove", "crontab": {"errors": [{"user": "svc", "error": "sudo: a password is required"}]}}'
    assert prov.parse_cron_clear(bad) == {
        "ok": False,
        "error": "svc: sudo: a password is required",
    }
    assert prov.parse_cron_clear("usage: runspec-scheduler ...") == {
        "ok": False,
        "error": "",
    }
