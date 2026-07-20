"""
test_transfer_adhoc.py — ad-hoc endpoints in start_transfer.

A transfer's remote side may be a configured fleet host (a name string) or an
ad-hoc host not in the fleet, given as a ``{"kind": "adhoc", ...}`` descriptor
reached through a configured jump — the same shape the Relay dialog uses. This
lets a download pull from an ad-hoc host and an upload push to one. The ad-hoc
secret is resolved from the keyring at connect time and never left on the
descriptor / in_flight / logs. The keyring and the connection opener are faked.
"""

from __future__ import annotations

import threading

import pytest

import runspec_console.credentials as cred_mod
from runspec_console.bridge import Bridge
from runspec_console.credentials import KEYRING_SERVICE


class _FakeKR:
    def __init__(self, secrets):
        self._secrets = secrets

    def get_password(self, service, name):
        assert service == KEYRING_SERVICE
        return self._secrets.get(name)


@pytest.fixture
def kr(monkeypatch):
    monkeypatch.setattr(cred_mod, "_keyring", lambda: _FakeKR({"svc": "s3cret"}))


CRED = {"id": "svc", "label": "svc", "username": "svc_user"}


# ── _normalise_transfer_side (pure) ─────────────────────────────────────────────


class _NormBridge:
    _normalise_transfer_side = Bridge._normalise_transfer_side
    _resolve_adhoc_credential = Bridge._resolve_adhoc_credential
    _credential_entry = Bridge._credential_entry

    def __init__(self, creds):
        self._credentials = creds

    def _reload_credentials(self):
        pass


def test_normalise_local_side():
    b = _NormBridge([])
    assert b._normalise_transfer_side("local") == ("local", None, None)


def test_normalise_configured_host():
    b = _NormBridge([])
    label, desc, pw = b._normalise_transfer_side("prod-1")
    assert label == "prod-1"
    assert desc == {"kind": "host", "host": "prod-1"}
    assert pw is None


def test_normalise_adhoc_resolves_credential(kr):
    b = _NormBridge([CRED])
    label, desc, pw = b._normalise_transfer_side(
        {"kind": "adhoc", "host": "box", "jump": "jumpx", "credential_id": "svc"}
    )
    assert label == "box"
    assert desc["kind"] == "adhoc"
    assert desc["host"] == "box"
    assert desc["jump"] == "jumpx"
    assert desc["user"] == "svc_user"  # filled from the credential
    assert "credential_id" not in desc and "password" not in desc  # never on desc
    assert pw == "s3cret"  # secret pulled from the keyring, returned out-of-band


def test_normalise_adhoc_port_coerced():
    b = _NormBridge([])
    _, desc, _ = b._normalise_transfer_side(
        {"kind": "adhoc", "host": "box", "jump": "j", "port": "2222"}
    )
    assert desc["port"] == 2222


def test_normalise_adhoc_missing_host_raises():
    b = _NormBridge([])
    with pytest.raises(ValueError, match="ad-hoc host"):
        b._normalise_transfer_side({"kind": "adhoc", "host": "", "jump": "j"})


def test_normalise_adhoc_jumpless_allowed():
    # A jump is optional — an empty jump means connect to the target directly.
    b = _NormBridge([])
    label, desc, _ = b._normalise_transfer_side(
        {"kind": "adhoc", "host": "box", "jump": ""}
    )
    assert label == "box"
    assert desc["kind"] == "adhoc" and desc["host"] == "box"
    assert desc["jump"] == ""


# ── start_transfer routes the ad-hoc side through _open_source_conn ──────────────


class _XferBridge:
    start_transfer = Bridge.start_transfer
    _normalise_transfer_side = Bridge._normalise_transfer_side
    _resolve_adhoc_credential = Bridge._resolve_adhoc_credential
    _credential_entry = Bridge._credential_entry
    _emit_transfer_end = Bridge._emit_transfer_end
    _await_transfer = Bridge._await_transfer

    def __init__(self, creds):
        self._credentials = creds
        self._lock = threading.Lock()
        self._in_flight: dict = {}
        self._recent_transfer_results: dict = {}
        self.captured: dict = {}

    def _reload_credentials(self):
        pass

    def _host_entry(self, h):
        return {"name": h, "ssh": f"deploy@{h}"}

    def get_config(self):
        return {"ssh": {}}

    def _current_user(self):
        return "tester"

    def _dispatch(self, *a, **k):
        pass

    def _open_source_conn(self, source, password, cfg):
        self.captured["source"] = dict(source)
        self.captured["password"] = password
        raise RuntimeError("stop")  # abort after capturing the resolved endpoint


def _run_and_wait(b, *args):
    res = b.start_transfer(*args)
    assert "id" in res, res
    b._await_transfer(res["id"], timeout=2.0)
    return res


def test_download_from_adhoc_resolves_and_strips_secret(kr):
    b = _XferBridge([CRED])
    _run_and_wait(
        b,
        {"kind": "adhoc", "host": "target", "jump": "jumpx", "credential_id": "svc"},
        ["/var/log/app.log"],
        "local",
        "/incoming",
    )
    src = b.captured["source"]
    assert src["kind"] == "adhoc" and src["host"] == "target" and src["jump"] == "jumpx"
    assert src["user"] == "svc_user"  # filled from the credential
    assert "credential_id" not in src and "password" not in src  # never on the desc
    assert b.captured["password"] == "s3cret"  # secret rides out-of-band
    # The in-flight record carries the target label, no secret.
    assert b._in_flight == {}  # popped on completion


def test_upload_to_adhoc_uses_dest_descriptor(kr):
    b = _XferBridge([CRED])
    _run_and_wait(
        b,
        "local",
        ["/etc/hosts"],
        {"kind": "adhoc", "host": "target", "jump": "jumpx"},
        "/incoming",
    )
    assert b.captured["source"]["kind"] == "adhoc"
    assert b.captured["source"]["host"] == "target"


def test_configured_host_still_routes_through_open_source_conn(kr):
    b = _XferBridge([CRED])
    _run_and_wait(b, "prod-1", ["/var/log/app.log"], "local", "/incoming")
    assert b.captured["source"] == {"kind": "host", "host": "prod-1"}
    assert b.captured["password"] is None


def test_two_remote_sides_rejected(kr):
    b = _XferBridge([CRED])
    res = b.start_transfer("prod-1", ["/x"], "prod-2", "/incoming")
    assert "error" in res and "local" in res["error"]


def test_start_transfer_rejects_path_like_rename(kr):
    b = _XferBridge([CRED])
    res = b.start_transfer("prod", ["/x"], "local", "/incoming", dest_name="../evil")
    assert "error" in res and "bare filename" in res["error"]
    assert b.captured == {}  # never opened a connection


def test_adhoc_jumpless_download_succeeds(kr):
    # No jump — start_transfer passes an empty jump straight to _open_source_conn,
    # which then connects to the target directly.
    b = _XferBridge([CRED])
    _run_and_wait(
        b,
        {"kind": "adhoc", "host": "target", "jump": ""},
        ["/x"],
        "local",
        "/incoming",
    )
    assert b.captured["source"]["kind"] == "adhoc"
    assert b.captured["source"]["host"] == "target"
    assert b.captured["source"]["jump"] == ""


def test_open_source_conn_jumpless_uses_proxy_aware_client(monkeypatch):
    # Jumpless must route through _make_ssh_client — the same builder every
    # configured host uses, which applies the [ssh] proxy / ~/.ssh/config routing.
    # A raw paramiko connect would bypass that routing (the reported timeout bug).
    # It must also touch no jump host or channel.
    import runspec_console.executor as executor_mod

    calls: dict = {}

    class _FakeTransport:
        def open_session(self):
            return object(), (lambda: None)

    class _FakeClient:
        def open_sftp(self):
            return "SFTP"

        def get_transport(self):
            return _FakeTransport()

        def close(self):
            pass

    def fake_make(ssh_target, identity_file, cfg, password=None):
        calls["ssh_target"] = ssh_target
        calls["password"] = password
        calls["cfg"] = cfg
        return _FakeClient()

    monkeypatch.setattr(executor_mod, "_make_ssh_client", fake_make)

    class _B:
        _open_source_conn = Bridge._open_source_conn

        def _host_entry(self, h):
            raise AssertionError("jumpless must not resolve a jump host entry")

        def _jump_channel(self, *a, **k):
            raise AssertionError("jumpless must not open a jump channel")

    b = _B()
    sftp, _sf, release = b._open_source_conn(
        {"kind": "adhoc", "host": "box", "jump": "", "user": "svc", "port": 2222},
        "pw",
        {"proxy": "http://p:8080"},
    )
    assert sftp == "SFTP"
    assert calls["ssh_target"] == "svc@box:2222"  # user + non-default port composed
    assert calls["password"] == "pw"
    assert calls["cfg"] == {"proxy": "http://p:8080"}  # routing config threaded in
    release()


def test_open_source_conn_jumpless_default_port_no_user(monkeypatch):
    import runspec_console.executor as executor_mod

    calls: dict = {}

    class _FakeClient:
        def open_sftp(self):
            return "SFTP"

        def get_transport(self):
            return None

        def close(self):
            pass

    def fake_make(ssh_target, identity_file, cfg, password=None):
        calls["ssh_target"] = ssh_target
        return _FakeClient()

    monkeypatch.setattr(executor_mod, "_make_ssh_client", fake_make)

    class _B:
        _open_source_conn = Bridge._open_source_conn

    b = _B()
    b._open_source_conn({"kind": "adhoc", "host": "box", "jump": ""}, None, {})
    assert calls["ssh_target"] == "box"  # no user, default port → bare hostname
