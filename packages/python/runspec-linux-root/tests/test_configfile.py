"""``config-file``: the allowlisted root config writer (runspec-linux-root).

Run in-process against a temp "venv" (``sys.prefix``). The suite isn't root, so
the root-ownership check on the allowlist is stubbed; the core's own tests
cover it."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from runspec_linux_core import rootfiles as rf

import runspec_linux_root.configfile as configfile

TOKEN = "glc_s3cr3t-token-value"


@pytest.fixture
def venv(tmp_path, monkeypatch):
    v = tmp_path / "venv"
    v.mkdir()
    etc = tmp_path / "etc" / "alloy"
    etc.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(v))
    monkeypatch.setattr(rf, "_check_trusted", lambda path, owner_uid: None)
    monkeypatch.setattr("runspec.parser.verify_run_as_identity", lambda user: None)
    audits: list[tuple] = []
    monkeypatch.setattr(configfile.rs, "audit", lambda *a, **kw: audits.append((a, kw)))
    rf.allow(v / rf.ALLOWLIST_NAME, f"{etc}/*")
    return {"venv": v, "etc": etc, "audits": audits}


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["config-file", *argv])
    code = 0
    try:
        configfile.main()
    except SystemExit as exc:
        code = exc.code or 0
    return code, json.loads(capsys.readouterr().out)


def test_write_fills_secret_masks_output_and_audits(venv, monkeypatch, capsys):
    monkeypatch.setenv("RUNSPEC_SECRET_GRAFANA_CLOUD", TOKEN)
    f = venv["etc"] / "config.alloy"
    code, out = _run(
        monkeypatch,
        capsys,
        ["write", str(f), "--content", 'password = "{{secret:grafana-cloud}}"\n'],
    )
    assert code == 0, out
    assert f.read_text() == f'password = "{TOKEN}"\n'
    assert stat.S_IMODE(f.stat().st_mode) == 0o600  # new file holding a secret
    assert TOKEN not in json.dumps(out) and "{{secret:grafana-cloud}}" in out["diff"]
    (event, outcome), kw = venv["audits"][-1]
    assert (event, outcome) == ("root_file.written", "succeeded")
    assert kw["detail"]["credentials"] == ["grafana-cloud"] and TOKEN not in json.dumps(kw)


def test_edit_masks_secret_in_diff(venv, monkeypatch, capsys):
    monkeypatch.setenv("RUNSPEC_SECRET_GRAFANA_CLOUD", TOKEN)
    f = venv["etc"] / "config.alloy"
    f.write_text('password = "old"\n')
    code, out = _run(
        monkeypatch,
        capsys,
        ["edit", str(f), "--old-text", '"old"', "--new-text", '"{{secret:grafana-cloud}}"'],
    )
    assert code == 0, out
    assert TOKEN in f.read_text() and TOKEN not in json.dumps(out)
    assert Path(out["backup"]).read_text() == 'password = "old"\n'


def test_missing_secret_writes_nothing(venv, monkeypatch, capsys):
    monkeypatch.delenv("RUNSPEC_SECRET_GRAFANA_CLOUD", raising=False)
    f = venv["etc"] / "config.alloy"
    code, out = _run(monkeypatch, capsys, ["write", str(f), "--content", "{{secret:grafana-cloud}}"])
    assert code == 1 and out["kind"] == "SecretNotAvailableError"
    assert not f.exists()
    assert venv["audits"][-1][0] == ("root_file.written", "failed")


def test_outside_allowlist_is_a_json_error(venv, monkeypatch, capsys, tmp_path):
    code, out = _run(monkeypatch, capsys, ["write", str(tmp_path / "x.conf"), "--content", "x"])
    assert code == 1 and "isn't in the allowlist" in out["error"]
    assert "config-file allow" in out["error"]


def test_edit_pairs_must_match(venv, monkeypatch, capsys):
    f = venv["etc"] / "config.alloy"
    f.write_text("a\n")
    code, out = _run(monkeypatch, capsys, ["edit", str(f), "--old-text", "a", "--old-text", "b", "--new-text", "c"])
    assert code == 1 and "one --new-text for each --old-text" in out["error"]


def test_allowed_allow_disallow(venv, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["allowed"])
    assert out["paths"] == [f"{venv['etc']}/*"]
    code, out = _run(monkeypatch, capsys, ["allow", "/etc/systemd/system/alloy.service.d/*"])
    assert code == 0 and out["changed"]
    assert venv["audits"][-1][0] == ("root_file.allow", "succeeded")
    code, out = _run(monkeypatch, capsys, ["disallow", "/etc/systemd/system/alloy.service.d/*"])
    assert out["paths"] == [f"{venv['etc']}/*"]


def test_allow_and_disallow_several(venv, monkeypatch, capsys):
    a, b = "/etc/systemd/system/alloy.service.d/*", "/etc/default/alloy"
    code, out = _run(monkeypatch, capsys, ["allow", f"{a},{b}"])
    assert code == 0 and out["added"] == [a, b] and out["paths"][-2:] == [a, b]
    assert venv["audits"][-1][1]["detail"]["patterns"] == [a, b]
    code, out = _run(monkeypatch, capsys, ["allow", "--pattern", "/etc/x/*", "--pattern", "relative/*"])
    assert code == 1 and "absolute" in out["error"]
    assert "/etc/x/*" not in _run(monkeypatch, capsys, ["allowed"])[1]["paths"]
    code, out = _run(monkeypatch, capsys, ["disallow", f"{a}, {b} /nope/*"])
    assert out["removed"] == [a, b] and out["not_found"] == ["/nope/*"]
    assert out["paths"] == [f"{venv['etc']}/*"]


def test_read(venv, monkeypatch, capsys):
    f = venv["etc"] / "config.alloy"
    f.write_text("hello\n")
    os.chmod(f, 0o640)
    code, out = _run(monkeypatch, capsys, ["read", str(f)])
    assert code == 0 and out["content"] == "hello\n" and out["mode"] == "0640"
