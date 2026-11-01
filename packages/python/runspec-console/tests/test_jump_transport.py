"""
test_jump_transport.py — the system-ssh jump transport for legacy hosts.

A host entry with a ``jump`` key is reached over the system ``ssh`` binary
(``-J``), not paramiko, so ``~/.ssh/config`` legacy algorithms apply (the
CentOS 6 ssh-rsa/ssh-dss case that paramiko 4+/5 can't negotiate). Covers the
argv builder, the ``ssh_jump_run`` runner, the ``run_remote`` jump branch,
editor validation + TOML round-trip, and bridge routing for discovery and
connectivity.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from runspec_console import executor
from runspec_console.executor import (
    _system_ssh_argv,
    has_sshpass,
    jump_copy_id_command,
    run_remote,
    ssh_jump_copy_id,
    ssh_jump_run,
)
from runspec_console.hosts import load_hosts, save_hosts, validate_editor_hosts


# ── _system_ssh_argv ──────────────────────────────────────────────────────────


def test_system_ssh_argv_basic_jump():
    argv = _system_ssh_argv("svc@c6", "bastion")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    proxy = next(a for a in argv if a.startswith("ProxyCommand="))
    assert "-W %h:%p bastion" in proxy
    assert "-J" not in argv
    assert argv[-1] == "svc@c6"  # target last, user@host reassembled


def _proxy_command(argv):
    return next(a for a in argv if a.startswith("ProxyCommand="))[
        len("ProxyCommand=") :
    ]


def test_jump_hop_gets_key_user_and_batch_mode():
    # `ssh -J` passed none of these to the jump hop, so it could not log in.
    argv = _system_ssh_argv(
        "svc@c6",
        "bastion:2200",
        identity_file="/keys/id_ed25519",
        global_ssh_config={"user": "deploy", "connect_timeout": 7},
    )
    proxy = _proxy_command(argv)
    assert "BatchMode=yes" in proxy
    assert "ConnectTimeout=7" in proxy
    assert f"-i {Path('/keys/id_ed25519').as_posix()}" in proxy
    assert "-p 2200" in proxy
    assert "-l deploy" in proxy
    assert proxy.endswith("-W %h:%p bastion")


def test_jump_hop_keeps_explicit_user():
    argv = _system_ssh_argv("c6", "ops@bastion", global_ssh_config={"user": "deploy"})
    proxy = _proxy_command(argv)
    assert "-l ops" in proxy and "deploy" not in proxy


def test_jump_chain_keeps_dash_j():
    argv = _system_ssh_argv("c6", "a,b")
    assert argv[argv.index("-J") + 1] == "a,b"
    assert not any(a.startswith("ProxyCommand=") for a in argv)


def test_system_ssh_uses_console_known_hosts_and_accept_new(tmp_path):
    kh = tmp_path / "known hosts"
    argv = _system_ssh_argv("c6", "bastion", global_ssh_config={"known_hosts": kh})
    assert "StrictHostKeyChecking=accept-new" in argv
    assert f'UserKnownHostsFile="{kh.as_posix()}" ~/.ssh/known_hosts' in argv
    # Same trust store on the jump hop, quoted for the local command parser.
    assert "StrictHostKeyChecking=accept-new" in _proxy_command(argv)
    assert "known hosts" in _proxy_command(argv)


def test_known_hosts_without_space_is_not_quoted(tmp_path):
    kh = tmp_path / "known_hosts"
    argv = _system_ssh_argv("c6", "bastion", global_ssh_config={"known_hosts": kh})
    assert f"UserKnownHostsFile={kh.as_posix()} ~/.ssh/known_hosts" in argv
    # No inner quotes, so nothing in the hop's line needs a backslash escape.
    assert "\\" not in _proxy_command(argv)
    assert kh.as_posix() in _proxy_command(argv)


def test_jump_hop_on_windows_uses_forward_slashes():
    # OpenSSH dropped the backslashes in the ProxyCommand line, so the hop's
    # key became C:Usersme.sshid_ed25519 ("identity file not found").
    with (
        patch.object(executor.os, "name", "nt"),
        patch.object(
            executor.shutil,
            "which",
            return_value="C:\\Windows\\System32\\OpenSSH\\ssh.exe",
        ),
    ):
        argv = _system_ssh_argv(
            "svc@c6", "bastion", identity_file="C:\\Users\\me\\.ssh\\id_ed25519"
        )
    proxy = _proxy_command(argv)
    assert "\\" not in proxy
    assert "-i C:/Users/me/.ssh/id_ed25519" in proxy
    assert proxy.startswith("C:/Windows/System32/OpenSSH/ssh.exe ")


def test_system_ssh_strict_host_key_checking():
    argv = _system_ssh_argv(
        "c6", "bastion", global_ssh_config={"host_key_checking": "strict"}
    )
    assert "StrictHostKeyChecking=yes" in argv


def test_proxy_command_escapes_percent_in_paths():
    argv = _system_ssh_argv("c6", "bastion", identity_file="/k/100%key")
    proxy = _proxy_command(argv)
    assert "100%%key" in proxy and "%h:%p" in proxy


def test_system_ssh_argv_identity_port_and_timeout():
    argv = _system_ssh_argv(
        "user@host:2222",
        "jump",
        identity_file="/keys/id_rsa",
        global_ssh_config={"connect_timeout": 7},
    )
    assert argv[argv.index("-i") + 1].endswith("id_rsa")
    assert argv[argv.index("-p") + 1] == "2222"
    assert "ConnectTimeout=7" in argv
    # port carried via -p, not embedded in the target
    assert argv[-1] == "user@host"


def test_system_ssh_argv_global_user_and_key_fallback():
    argv = _system_ssh_argv(
        "host", "jump", global_ssh_config={"user": "deploy", "identityFile": "/g/k"}
    )
    assert argv[-1] == "deploy@host"
    assert argv[argv.index("-i") + 1].endswith("k")


def test_system_ssh_argv_bad_timeout_falls_back():
    argv = _system_ssh_argv("h", "j", global_ssh_config={"connect_timeout": "nope"})
    assert any(a.startswith("ConnectTimeout=") for a in argv)


# ── ssh_jump_run ──────────────────────────────────────────────────────────────


def test_ssh_jump_run_invokes_system_ssh_and_returns_triple():
    with patch.object(
        executor, "run_with_deadline", return_value=(0, "hello", "")
    ) as m:
        code, out, err = ssh_jump_run("svc@c6", "runspec local", jump="bastion")
    assert (code, out, err) == (0, "hello", "")
    argv = m.call_args.args[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "runspec local"  # command is the final argv element
    assert any(a.endswith(" bastion") for a in argv)


def test_ssh_jump_run_missing_ssh_binary():
    with patch.object(executor, "run_with_deadline", side_effect=FileNotFoundError()):
        code, _out, err = ssh_jump_run("h", "true", jump="j")
    assert code == -1 and "OpenSSH" in err


def test_ssh_jump_run_timeout():
    with patch.object(executor, "run_with_deadline", return_value=(None, "", "")):
        code, _out, err = ssh_jump_run("h", "true", jump="j", timeout=15)
    assert code == -1 and "timed out" in err


_KEX = "kex_exchange_identification: Connection closed by remote host"


def test_ssh_jump_run_retries_jump_throttle():
    results = [(255, "", _KEX), (255, "", _KEX), (0, "ok", "")]
    with (
        patch.object(executor, "run_with_deadline", side_effect=results) as m,
        patch.object(executor.time, "sleep"),
    ):
        code, out, _err = ssh_jump_run("h", "true", jump="j")
    assert (code, out) == (0, "ok") and m.call_count == 3


def test_ssh_jump_run_gives_up_after_retries():
    with (
        patch.object(executor, "run_with_deadline", return_value=(255, "", _KEX)) as m,
        patch.object(executor.time, "sleep"),
    ):
        code, _out, err = ssh_jump_run(
            "h", "true", jump="j", global_ssh_config={"jump_retries": 1}
        )
    assert code == 255 and "kex" in err and m.call_count == 2


def test_ssh_jump_run_does_not_retry_auth_or_command_failure():
    for result in [
        (255, "", "Permission denied (publickey)."),
        (1, "", "connection closed"),
    ]:
        with patch.object(executor, "run_with_deadline", return_value=result) as m:
            ssh_jump_run("h", "true", jump="j")
        assert m.call_count == 1


def test_ssh_jump_run_caps_concurrent_connections_per_jump():
    import threading
    import time as _time

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake(argv, timeout):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        _time.sleep(0.05)
        with lock:
            live["now"] -= 1
        return 0, "", ""

    cfg = {"jump_max_concurrent": 2}
    with patch.object(executor, "run_with_deadline", side_effect=fake):
        threads = [
            threading.Thread(
                target=ssh_jump_run,
                args=(f"h{i}", "true"),
                kwargs={"jump": "capjump", "global_ssh_config": cfg},
            )
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert live["peak"] == 2


def test_jump_hop_proxy_reads_ssh_config():
    out = "user ops\nproxycommand connect -H proxy.corp:8080 %h %p\nport 22\n"
    with patch.object(executor, "run_with_deadline", return_value=(0, out, "")) as m:
        found = executor.jump_hop_proxy("bastion")
    assert found == "proxycommand connect -H proxy.corp:8080 %h %p"
    assert m.call_args.args[0][1:] == ["-G", "bastion"]


def test_jump_hop_proxy_none():
    with patch.object(
        executor, "run_with_deadline", return_value=(0, "proxycommand none\n", "")
    ):
        assert executor.jump_hop_proxy("bastion") == ""


# ── run_remote jump branch ────────────────────────────────────────────────────


def test_run_remote_jump_streams_over_system_ssh_not_paramiko(monkeypatch):
    captured: dict = {}

    def fake_stream(cmd, on_line, on_done, **kw):
        captured["cmd"] = cmd
        on_done(0, 5)

    def boom(*a, **k):
        raise AssertionError("paramiko path must not be used for a jump host")

    monkeypatch.setattr(executor, "_stream", fake_stream)
    monkeypatch.setattr(executor, "_make_ssh_client", boom)

    done: dict = {}
    run_remote(
        "svc@c6",
        "/opt/bin/runspec",
        "disk-usage",
        {},
        [],
        on_line=lambda *_a: None,
        on_done=lambda code, _ms: done.update(code=code),
        jump="bastion",
    )
    assert done["code"] == 0
    cmd = captured["cmd"]
    assert cmd[0] == "ssh" and any(a.endswith(" bastion") for a in cmd)
    # The last element is the remote command string that invokes the runnable.
    assert "disk-usage" in cmd[-1]


# ── editor validation ─────────────────────────────────────────────────────────


def test_validate_accepts_jump_with_target():
    assert (
        validate_editor_hosts([{"name": "c6", "ssh": "svc@c6", "jump": "bastion"}])
        == []
    )
    # hostname (editor shape) also counts as a target
    assert validate_editor_hosts([{"name": "c6", "hostname": "c6", "jump": "b"}]) == []


def test_validate_rejects_jump_without_target():
    errs = validate_editor_hosts([{"name": "c6", "jump": "bastion"}])
    assert any("needs an ssh/hostname" in e for e in errs)


def test_validate_rejects_blank_jump_string():
    errs = validate_editor_hosts([{"name": "c6", "ssh": "svc@c6", "jump": "   "}])
    assert any("non-empty" in e for e in errs)


def test_validate_ignores_absent_jump():
    assert validate_editor_hosts([{"name": "c6", "ssh": "svc@c6"}]) == []


# ── TOML round-trip ───────────────────────────────────────────────────────────


def test_hosts_toml_round_trips_jump(tmp_path):
    p = tmp_path / "runspec_hosts.toml"
    save_hosts(
        p,
        [
            {
                "name": "c6",
                "ssh": "svc@c6",
                "jump": "bastion",
                "runspec_paths": ["runspec"],
            }
        ],
    )
    assert load_hosts(p)[0]["jump"] == "bastion"


# ── copy-key over a jump host ─────────────────────────────────────────────────


def test_has_sshpass_reflects_path_lookup():
    with patch.object(executor.shutil, "which", return_value="/usr/bin/sshpass"):
        assert has_sshpass() is True
    with patch.object(executor.shutil, "which", return_value=None):
        assert has_sshpass() is False


def test_jump_copy_id_command_builds_ssh_copy_id():
    cmd = jump_copy_id_command("svc@c6", "bastion", "/keys/id_ed25519")
    assert cmd.startswith("ssh-copy-id ")
    assert "ProxyJump=bastion" in cmd
    assert cmd.endswith("svc@c6")
    assert "id_ed25519" in cmd


def test_jump_copy_id_command_includes_nondefault_port():
    cmd = jump_copy_id_command("svc@c6:2222", "bastion")
    assert "-p 2222" in cmd


def test_ssh_jump_copy_id_uses_sshpass_password_auth_and_env():
    with patch.object(executor, "run_with_deadline", return_value=(0, "", "")) as m:
        code, _out, _err = ssh_jump_copy_id("svc@c6", "bastion", "append-key", "s3cret")
    assert code == 0
    argv = m.call_args.args[0]
    assert argv[:3] == ["sshpass", "-e", "ssh"]
    assert "PubkeyAuthentication=no" in argv
    assert argv[argv.index("-J") + 1] == "bastion"
    assert argv[-2:] == ["svc@c6", "append-key"]
    # Password rides the SSHPASS env var, never argv.
    assert m.call_args.kwargs["env"]["SSHPASS"] == "s3cret"
    assert "s3cret" not in argv


def test_ssh_jump_copy_id_missing_sshpass():
    with patch.object(executor, "run_with_deadline", side_effect=FileNotFoundError()):
        code, _out, err = ssh_jump_copy_id("h", "j", "cmd", "pw")
    assert code == -1 and "sshpass" in err


# ── bridge copy-key routing ───────────────────────────────────────────────────


def test_push_key_over_jump_uses_sshpass_when_available():
    b = _bridge()
    entry = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    with (
        patch.object(
            b,
            "_public_key_for",
            return_value={"ok": True, "public_key": "ssh-ed25519 AAAA test"},
        ),
        patch("runspec_console.executor.has_sshpass", return_value=True),
        patch(
            "runspec_console.executor.ssh_jump_copy_id", return_value=(0, "", "")
        ) as m,
    ):
        res = b._push_key_over_password(entry, "pw", {})
    assert res["ok"] is True
    # ssh_jump_copy_id(ssh_target, jump, remote_cmd, password, …) — jump is 2nd.
    assert m.call_args.args[1] == "bastion"


def test_push_key_over_jump_without_sshpass_returns_manual_command():
    b = _bridge()
    entry = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    with (
        patch.object(
            b,
            "_public_key_for",
            return_value={"ok": True, "public_key": "ssh-ed25519 AAAA test"},
        ),
        patch("runspec_console.executor.has_sshpass", return_value=False),
    ):
        res = b._push_key_over_password(entry, "pw", {})
    assert res["ok"] is False
    # Actionable fallback, not a paramiko error.
    assert "ssh-copy-id" in res["error"]
    assert "ProxyJump=bastion" in res["error"]


def test_push_key_non_jump_host_still_uses_paramiko():
    # A normal host must not be diverted to the jump path — it should reach the
    # paramiko client builder (which we stub to confirm it's exercised).
    b = _bridge()
    entry = {"name": "prod", "ssh": "deploy@prod"}
    with (
        patch.object(
            b,
            "_public_key_for",
            return_value={"ok": True, "public_key": "ssh-ed25519 AAAA test"},
        ),
        patch(
            "runspec_console.executor.ssh_jump_copy_id",
            side_effect=AssertionError("jump path must not run for a non-jump host"),
        ),
        patch("runspec_console.executor._make_ssh_client") as make_client,
    ):
        make_client.side_effect = RuntimeError("connect stubbed")
        res = b._push_key_over_password(entry, "pw", {})
    # Reached the paramiko builder (stubbed to raise) — proves no jump diversion.
    assert res["ok"] is False
    assert make_client.called


# ── bridge routing (discovery + connectivity) ─────────────────────────────────


def _bridge(pool=None):
    """A Bridge whose jump hosts use the system-ssh transport (no SSH pool),
    unless ``pool`` supplies a (fake) pool for the pooled-tunnel path."""
    from runspec_console.bridge import Bridge

    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = pool
    return b


def test_check_connected_routes_jump_host_through_system_ssh():
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch("runspec_console.executor.ssh_jump_run", return_value=(0, "", "")) as m,
    ):
        assert b._check_connected(host) is True
    assert m.call_args.kwargs["jump"] == "bastion"


def test_check_connected_jump_host_down_reports_false():
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch(
            "runspec_console.executor.ssh_jump_run", return_value=(255, "", "no route")
        ),
    ):
        assert b._check_connected(host) is False


def test_discover_one_routes_jump_host_through_system_ssh():
    b = _bridge()
    b._connected_cache["c6"] = True
    host = {
        "name": "c6",
        "ssh": "svc@c6",
        "jump": "bastion",
        "runspec_paths": ["/opt/bin/runspec"],
    }
    calls: dict = {}

    def fake_jump_run(
        target, command, *, jump, identity_file=None, global_ssh_config=None, timeout=15
    ):
        calls["jump"] = jump
        calls["command"] = command
        return (0, "[]", "")

    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch("runspec_console.executor.ssh_jump_run", side_effect=fake_jump_run),
    ):
        out = b._discover_one(host)
    assert out == []
    assert calls["jump"] == "bastion"
    assert "local --format json" in calls["command"]


def test_check_connected_resolves_fleet_host_name_as_jump():
    # The system ssh client doesn't know the console's host names.
    b = _bridge()
    b._hosts = [
        {"name": "bastion", "ssh": "ops@bastion.corp:2200"},
        {"name": "c6", "ssh": "svc@c6", "jump": "Bastion"},
    ]
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch("runspec_console.executor.ssh_jump_run", return_value=(0, "", "")) as m,
    ):
        assert b._check_connected(b._hosts[1]) is True
    assert m.call_args.kwargs["jump"] == "ops@bastion.corp:2200"


def test_check_connected_jump_failure_reaches_dev_tab_once():
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    events = []
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch.object(b, "_emit_ssh_log", side_effect=events.append),
        patch(
            "runspec_console.executor.ssh_jump_run",
            return_value=(255, "", "ops@bastion: Permission denied (publickey).\n"),
        ),
    ):
        assert b._check_connected(host) is False
        assert b._check_connected(host) is False  # same failure: not repeated
    assert len(events) == 1
    assert events[0]["logger"] == "ssh.jump"
    assert events[0]["level"] == "ERROR"
    assert "Permission denied" in events[0]["message"]
    assert events[0]["host"] == "c6"
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch.object(b, "_emit_ssh_log", side_effect=events.append),
        patch("runspec_console.executor.ssh_jump_run", return_value=(0, "", "")),
    ):
        assert b._check_connected(host) is True
    assert events[-1]["level"] == "INFO"  # recovery is reported too


def test_jump_edit_reports_the_next_attempt_even_if_it_fails_the_same():
    # Editing a jump host and getting no new Dev-tab row looked like the edit
    # had not been applied.
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    events = []
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch.object(b, "_emit_ssh_log", side_effect=events.append),
        patch(
            "runspec_console.executor.ssh_jump_run",
            return_value=(255, "", "Connection timed out"),
        ),
    ):
        b._check_connected(host)
        b._check_connected({**host, "jump": "bastion2"})
        b._check_connected({**host, "jump": "bastion2"})
    assert len(events) == 2
    assert "bastion2" in events[1]["message"]


def test_forced_refresh_clears_connect_backoff():
    # A profile switch or host save tries every host now; a host still in
    # connect-backoff used to be skipped silently until a restart.
    b = _bridge()
    cleared = []
    pool = type("P", (), {"clear_backoff": lambda self: cleared.append(1)})()
    with patch.object(b, "_ssh_pool", pool), patch.object(b, "_refresh_cycle"):
        b._kick_refresh()
        assert cleared == []
        b._kick_refresh(force=True)
    assert cleared == [1]


def test_jump_failure_names_a_proxy_from_ssh_config():
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    events = []
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch.object(b, "_emit_ssh_log", side_effect=events.append),
        patch(
            "runspec_console.executor.ssh_jump_run",
            return_value=(255, "", "FATAL: failed to begin relaying via HTTP."),
        ),
        patch(
            "runspec_console.executor.jump_hop_proxy",
            return_value="proxycommand connect -H proxy:8080 %h %p",
        ),
    ):
        b._check_connected(host)
    assert "proxycommand connect -H proxy:8080" in events[0]["message"]
    assert "ssh config" in events[0]["message"]


# ── pooled jump tunnel (0.369.0) ──────────────────────────────────────────────


class _FakePool:
    def __init__(self, connectable=True, error=""):
        self._ok = connectable
        self._error = error
        self.connect_calls = []
        self.sessions = []

    def connectable(self, ssh, *, identity_file=None, global_ssh_config=None):
        self.connect_calls.append(ssh)
        return self._ok

    def last_error(self, ssh, *, identity_file=None, global_ssh_config=None):
        return self._error

    def open_session(self, ssh, *, identity_file=None, global_ssh_config=None):
        self.sessions.append(ssh)
        return object(), lambda: None

    def clear_backoff(self):
        pass

    def close_all(self):
        pass


def test_jump_host_probes_over_the_pooled_tunnel():
    pool = _FakePool(connectable=True)
    b = _bridge(pool)
    host = {"name": "c7", "ssh": "svc@c7", "jump": "bastion"}
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch(
            "runspec_console.executor.ssh_jump_run",
            side_effect=AssertionError("system ssh must not run"),
        ),
    ):
        assert b._check_connected(host) is True
    assert pool.connect_calls == ["svc@c7"]
    assert b._jump_mode(host, {}) == "pool"


def test_jump_host_falls_back_to_system_ssh_and_remembers():
    pool = _FakePool(connectable=False, error="Incompatible ssh peer")
    b = _bridge(pool)
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    events = []
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch.object(b, "_emit_ssh_log", side_effect=events.append),
        patch("runspec_console.executor.ssh_jump_run", return_value=(0, "", "")) as m,
    ):
        assert b._check_connected(host) is True
        assert m.call_count == 1
        assert b._jump_mode(host, {}) == "ssh"
        # Next probe goes straight to system ssh.
        assert b._check_connected(host) is True
    assert pool.connect_calls == ["svc@c6"]
    fallback = [e for e in events if "using system ssh" in e["message"]]
    assert len(fallback) == 1 and "Incompatible ssh peer" in fallback[0]["message"]
    # A forced refresh (host save / profile switch / Refresh) tries the tunnel again.
    with patch.object(b, "_refresh_cycle"):
        b._kick_refresh(force=True)
    assert b._jump_mode(host, {}) == "pool"


def test_jump_host_down_both_ways_is_not_pinned():
    pool = _FakePool(connectable=False, error="timed out")
    b = _bridge(pool)
    host = {"name": "c7", "ssh": "svc@c7", "jump": "bastion"}
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch(
            "runspec_console.executor.ssh_jump_run", return_value=(255, "", "no route")
        ),
    ):
        assert b._check_connected(host) is False
    assert b._jump_mode(host, {}) == "pool"


def test_jump_mode_stays_on_system_ssh_for_chains_and_the_off_switch():
    b = _bridge(_FakePool())
    assert b._jump_mode({"name": "x", "jump": "a,b"}, {}) == "ssh"
    assert b._jump_mode({"name": "x", "jump": "a"}, {"jump_pool": False}) == "ssh"
    assert b._jump_mode({"name": "x"}, {}) == ""
    assert _bridge(None)._jump_mode({"name": "x", "jump": "a"}, {}) == "ssh"


def test_ssh_route_uses_the_jump_hosts_own_key():
    b = _bridge(_FakePool())
    b._hosts = [
        {"name": "bastion", "ssh": "ops@bastion.corp:2200", "identityFile": "~/k/b"},
        {"name": "c7", "ssh": "svc@c7", "jump": "Bastion", "identityFile": "~/k/t"},
        {"name": "c8", "ssh": "svc@c8", "jump": "edge-alias", "identityFile": "~/k/t"},
        {"name": "c9", "ssh": "svc@c9"},
    ]
    assert b._ssh_route("svc@c7", "~/k/t", {}) == ("ops@bastion.corp:2200", "~/k/b")
    # An alias jump is reached with the target's key, as the system-ssh hop is.
    assert b._ssh_route("svc@c8", "~/k/t", {}) == ("edge-alias", "~/k/t")
    assert b._ssh_route("svc@c9", None, {}) is None
    assert b._ssh_route("ops@bastion.corp:2200", "~/k/b", {}) is None
    assert b._ssh_route("svc@c7", "~/k/t", {"jump_pool": False}) is None


def test_remote_session_pools_runs_unless_system_ssh():
    pool = _FakePool()
    b = _bridge(pool)
    b._hosts = [{"name": "c7", "ssh": "svc@c7", "jump": "bastion"}]
    jump, factory = b._remote_session(b._hosts[0], {})
    assert jump == "" and factory is not None
    factory()
    assert pool.sessions == ["svc@c7"]
    b._pin_jump_ssh("c7")
    jump, factory = b._remote_session(b._hosts[0], {})
    assert jump == "bastion" and factory is None
    # A plain host gets a pooled factory (agent runs used to open a fresh
    # connection per run).
    jump, factory = b._remote_session({"name": "web", "ssh": "svc@web"}, {})
    assert jump == "" and factory is not None


def test_discover_one_pool_mode_jump_host_uses_the_pool():
    pool = _FakePool()
    calls = []

    def run(target, command, **kw):
        calls.append((target, command))
        return (0, "[]", "")

    pool.run = run
    b = _bridge(pool)
    b._connected_cache["c7"] = True
    host = {
        "name": "c7",
        "ssh": "svc@c7",
        "jump": "bastion",
        "runspec_paths": ["/opt/bin/runspec"],
    }
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch(
            "runspec_console.executor.ssh_jump_run",
            side_effect=AssertionError("system ssh must not run"),
        ),
    ):
        b._discover_one(host)
    assert calls and calls[0][0] == "svc@c7"
