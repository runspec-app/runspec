"""
test_executor_ssh.py — HTTP-CONNECT proxy + ~/.ssh/config handling in executor.

`_http_connect_sock` is tested against a local fake proxy (no network). The
`_make_ssh_client` tests patch paramiko.SSHClient to capture connect kwargs and
use real paramiko.SSHConfig parsing.
"""

from __future__ import annotations

import socket
import threading
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from runspec_console import executor
from runspec_console.executor import _http_connect_sock, _make_ssh_client


# ── fake HTTP CONNECT proxy ───────────────────────────────────────────────────


def _serve_once(status_line: bytes) -> tuple[int, dict]:
    """Start a one-shot fake proxy on localhost. Returns (port, captured)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    captured: dict = {}

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            captured["request"] = conn.recv(4096)
            conn.sendall(status_line + b"\r\n\r\n")
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port, captured


def test_http_connect_sock_success():
    port, captured = _serve_once(b"HTTP/1.1 200 Connection established")
    sock = _http_connect_sock(f"http://127.0.0.1:{port}", "target.host", 22)
    try:
        assert captured["request"].startswith(b"CONNECT target.host:22 HTTP/1.1")
    finally:
        sock.close()


def test_http_connect_sock_non_200_raises():
    port, _ = _serve_once(b"HTTP/1.1 403 Forbidden")
    with pytest.raises(OSError, match="CONNECT"):
        _http_connect_sock(f"127.0.0.1:{port}", "target.host", 22)  # scheme optional


def test_http_connect_sock_bad_url():
    with pytest.raises(ValueError, match="Invalid proxy"):
        _http_connect_sock("http://", "h", 22)


# ── _make_ssh_client wiring ───────────────────────────────────────────────────


def test_proxy_socket_passed_to_paramiko(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(executor, "_http_connect_sock", lambda *a, **k: sentinel)
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {"proxy": "http://proxy.corp:8080"})
    kwargs = MockClient.return_value.connect.call_args.kwargs
    assert kwargs["sock"] is sentinel
    assert kwargs["hostname"] == "prod-1"
    assert kwargs["username"] == "deploy"


def test_no_proxy_no_sock():
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {})
    kwargs = MockClient.return_value.connect.call_args.kwargs
    assert "sock" not in kwargs
    assert kwargs["hostname"] == "prod-1"


def test_connect_uses_default_banner_and_auth_timeouts():
    """The connect phase carries explicit banner/auth timeouts (not paramiko's
    tight 15s defaults) — the mitigation for 'Error reading SSH protocol banner'.
    """
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {})
    kwargs = MockClient.return_value.connect.call_args.kwargs
    assert kwargs["timeout"] == executor.DEFAULT_CONNECT_TIMEOUT
    assert kwargs["banner_timeout"] == executor.DEFAULT_BANNER_TIMEOUT
    assert kwargs["auth_timeout"] == executor.DEFAULT_AUTH_TIMEOUT


def test_connect_timeouts_overridable_via_ssh_config():
    cfg = {"connect_timeout": 5, "banner_timeout": 60, "auth_timeout": 45}
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, cfg)
    kwargs = MockClient.return_value.connect.call_args.kwargs
    assert kwargs["timeout"] == 5.0
    assert kwargs["banner_timeout"] == 60.0
    assert kwargs["auth_timeout"] == 45.0


# ── host-key verification ─────────────────────────────────────────────────────


def _policy_of(MockClient) -> object:
    return MockClient.return_value.set_missing_host_key_policy.call_args.args[0]


def test_default_host_key_checking_is_accept_new(monkeypatch, tmp_path):
    """Default mode loads known hosts and installs a TOFU policy — NOT the blind
    AutoAddPolicy/RejectPolicy. accept-new is a bespoke MissingHostKeyPolicy."""
    monkeypatch.setenv("APPDATA", str(tmp_path))  # app-managed known_hosts → tmp
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {})
    MockClient.return_value.load_system_host_keys.assert_called_once()
    policy = _policy_of(MockClient)
    assert isinstance(policy, paramiko.MissingHostKeyPolicy)
    assert not isinstance(policy, (paramiko.AutoAddPolicy, paramiko.RejectPolicy))


def test_host_key_checking_yes_is_strict_reject():
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {"host_key_checking": "yes"})
    assert isinstance(_policy_of(MockClient), paramiko.RejectPolicy)


def test_no_blind_autoadd_policy_is_ever_installed(monkeypatch, tmp_path):
    """There is no 'accept anything' mode — even a 'no'/'off' value must not
    install paramiko's AutoAddPolicy (the MITM hole / CWE-295 this change closes).
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    for value in ("no", "off", "false", "anything-else"):
        with patch("paramiko.SSHClient") as MockClient:
            _make_ssh_client("deploy@prod-1", None, {"host_key_checking": value})
        assert not isinstance(_policy_of(MockClient), paramiko.AutoAddPolicy)


def test_unrecognised_mode_falls_back_to_accept_new(monkeypatch, tmp_path):
    """A typo must not silently disable verification — unknown → safe TOFU."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {"host_key_checking": "lenient?"})
    policy = _policy_of(MockClient)
    assert not isinstance(policy, (paramiko.AutoAddPolicy, paramiko.RejectPolicy))


def test_known_hosts_override_loaded_when_present(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("", encoding="utf-8")
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("deploy@prod-1", None, {"known_hosts": str(kh)})
    MockClient.return_value.load_host_keys.assert_called_once_with(str(kh))


def test_accept_new_policy_records_and_persists_unknown_key(tmp_path):
    """The accept-new policy adds the key and writes it to the app-managed file,
    creating the parent dir lazily on first use."""
    kh = tmp_path / "nested" / "known_hosts"  # parent does not exist yet
    policy = executor._accept_new_policy(kh)
    client = MagicMock()
    key = MagicMock()
    key.get_name.return_value = "ssh-ed25519"

    policy.missing_host_key(client, "prod-1", key)

    client._host_keys.add.assert_called_once_with("prod-1", "ssh-ed25519", key)
    client.save_host_keys.assert_called_once_with(str(kh))
    assert kh.parent.is_dir()


def test_accept_new_policy_survives_unwritable_known_hosts(tmp_path):
    """A failed persist must not break the connection — trust holds in-memory."""
    kh = tmp_path / "known_hosts"
    policy = executor._accept_new_policy(kh)
    client = MagicMock()
    client.save_host_keys.side_effect = OSError("read-only filesystem")
    key = MagicMock()
    key.get_name.return_value = "ssh-rsa"

    policy.missing_host_key(client, "prod-1", key)  # must not raise

    client._host_keys.add.assert_called_once()


def test_ssh_config_applies_hostname_user_port_identity(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text(
        "Host myalias\n"
        "    HostName real.internal\n"
        "    User deploy\n"
        "    Port 2222\n"
        "    IdentityFile ~/.ssh/id_ed25519\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client("myalias", None, {"use_ssh_config": True})
    kwargs = MockClient.return_value.connect.call_args.kwargs
    assert kwargs["hostname"] == "real.internal"
    assert kwargs["username"] == "deploy"
    assert kwargs["port"] == 2222
    assert kwargs["key_filename"].endswith("id_ed25519")


def test_explicit_proxy_beats_ssh_config_proxycommand(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text(
        "Host prod-1\n    ProxyCommand connect -H other:9999 %h %p\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    sentinel = object()
    captured = {}

    def fake_connect_sock(proxy, host, port, **k):
        captured["proxy"] = proxy
        return sentinel

    monkeypatch.setattr(executor, "_http_connect_sock", fake_connect_sock)
    with patch("paramiko.SSHClient") as MockClient:
        _make_ssh_client(
            "deploy@prod-1",
            None,
            {"use_ssh_config": True, "proxy": "http://proxy.corp:8080"},
        )
    kwargs = MockClient.return_value.connect.call_args.kwargs
    # The explicit HTTP proxy is used, not the ssh_config ProxyCommand.
    assert kwargs["sock"] is sentinel
    assert captured["proxy"] == "http://proxy.corp:8080"


def test_ssh_run_routes_through_proxy(monkeypatch):
    """ssh_run is what the key-rotation push/verify use — confirm a configured
    proxy reaches the connection there too."""
    sentinel = object()
    monkeypatch.setattr(executor, "_http_connect_sock", lambda *a, **k: sentinel)
    with patch("paramiko.SSHClient") as MockClient:
        inst = MockClient.return_value
        out = MagicMock()
        out.read.return_value = b"ok"
        out.channel.recv_exit_status.return_value = 0
        err = MagicMock()
        err.read.return_value = b""
        inst.exec_command.return_value = (MagicMock(), out, err)
        code, sout, _ = executor.ssh_run(
            "deploy@prod-1",
            "echo ok",
            global_ssh_config={"proxy": "http://proxy.corp:8080"},
        )
    assert inst.connect.call_args.kwargs["sock"] is sentinel
    assert code == 0 and sout == "ok"
