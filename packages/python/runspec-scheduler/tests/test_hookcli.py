"""The ``hooks`` verbs (called as functions, no daemon)."""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from runspec_scheduler import hookcli
from runspec_scheduler import hookconfig as hc
from runspec_scheduler import spool as sp


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "runspec_scheduler.toml"
    path.write_text('[scheduler]\nbackend = "cron"\n\n[hooks]\nenabled = true\nport = 18990\n')
    return path


HOOK_TOML = """\
[[hook]]
name = "alerts"
fields = { alertname = "labels.alertname" }

[[hook.rule]]
name = "note"
match = { alertname = "." }
"""


def test_save_with_new_token_list_and_remove(config: Path) -> None:
    out = hookcli.save(config, HOOK_TOML, new_token=True, new_password=False)
    token = out["secrets"]["token"]
    assert out["ok"] and not out["replaced"] and out["url"].endswith(":18990/hook/alerts")
    text = config.read_text()
    assert token not in text and hc.sha256(token) in text
    # saving again without a new token keeps the stored hash
    again = hookcli.save(config, HOOK_TOML.replace('"note"', '"tell"'), new_token=False, new_password=False)
    assert again["replaced"] and "secrets" not in again
    assert tomllib.loads(config.read_text())["hook"][0]["token_sha256"] == hc.sha256(token)
    listing = hookcli.hooks_status(config)
    assert [h["name"] for h in listing["hooks"]] == ["alerts"] and listing["pending"] == []
    assert "alerts" in hookcli.render_list(listing)
    assert hookcli.remove(config, "alerts")["ok"]
    assert not hookcli.remove(config, "alerts")["ok"]


def test_save_json_and_basic_password(config: Path) -> None:
    out = hookcli.save(config, json.dumps({"name": "svc", "username": "sender"}), new_token=False, new_password=True)
    assert out["secrets"]["password"] and "basic" in out["hook"]["auth"]


def test_save_refuses_secretless_and_multiple(config: Path) -> None:
    with pytest.raises(ValueError, match="needs a token"):
        hookcli.save(config, HOOK_TOML, new_token=False, new_password=False)
    with pytest.raises(ValueError, match="one \\[\\[hook\\]\\] at a time"):
        hookcli.save(config, HOOK_TOML + "\n[[hook]]\nname = 'b'\n", new_token=True, new_password=False)


def test_remove_deployed_says_where(config: Path) -> None:
    folder = config.parent / "hooks.d"
    folder.mkdir()
    (folder / "ops.toml").write_text(f'[[hook]]\nname = "ops"\ntoken_sha256 = "{hc.sha256("x")}"\n')
    (folder / "ops.toml").chmod(0o644)
    out = hookcli.remove(config, "ops")
    assert not out["ok"] and "deployed from" in out["error"]


def test_settings(config: Path) -> None:
    out = hookcli.settings(config, ["max_body=64k", 'trusted_proxies=["10.0.0.0/8"]', "port="], ["relay=mail.example", 'to=["ops@example"]', "from=rs@example"])
    data = tomllib.loads(config.read_text())
    assert data["hooks"] == {"enabled": True, "max_body": "64k", "trusted_proxies": ["10.0.0.0/8"]}
    assert data["smtp"]["to"] == ["ops@example"] and out["smtp_settings"]["relay"] == "mail.example"
    assert data["scheduler"] == {"backend": "cron"}
    with pytest.raises(ValueError, match="key=value"):
        hookcli.settings(config, ["enabled"], [])


def test_token() -> None:
    out = hookcli.token()
    assert hc.sha256(out["token"]) == out["token_sha256"]


def test_check(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text(f'[[hook]]\nname = "a"\ntoken_sha256 = "{hc.sha256("x")}"\n')
    assert hookcli.check(good) == {"ok": True, "file": str(good), "hooks": ["a"], "problems": []}
    bad = tmp_path / "bad.toml"
    bad.write_text('[[hook]]\nname = "a"\n')
    out = hookcli.check(bad)
    assert not out["ok"] and "needs a token" in hookcli.render_check(out)


def test_test_from_stdin_file_and_stored(config: Path, tmp_path: Path) -> None:
    hookcli.save(config, HOOK_TOML, new_token=True, new_password=False)
    body = json.dumps({"labels": {"alertname": "Disk"}}).encode()
    out = hookcli.test(config, "alerts", body=body, content_type="application/json", delivery=None, file=None)
    assert out["items"][0]["state"] == sp.EVENT and out["items"][0]["rule"] == "note"
    assert "shown on consoles only" in hookcli.render_test(out)
    # nothing received yet, no body
    out = hookcli.test(config, "alerts", body=None, content_type="", delivery=None, file=None)
    assert not out["ok"] and "hasn't received one yet" in out["error"]
    spool = sp.Spool(sp.spool_path_for(config))
    spool.add_delivery("alerts", source="x", content_type="application/json", headers={}, body=body)
    spool.close()
    out = hookcli.test(config, "alerts", body=None, content_type="", delivery=None, file=None)
    assert out["source"] == "delivery 1" and out["items"][0]["rule"] == "note"
    # a hooks.d file before it's deployed
    draft = tmp_path / "draft.toml"
    draft.write_text(
        textwrap.dedent(f"""
        [[hook]]
        name = "draft"
        token_sha256 = "{hc.sha256("x")}"
        fields = {{ alertname = "labels.alertname" }}

        [[hook.rule]]
        match = {{ alertname = "^Nope$" }}
        skip = true
        """)
    )
    out = hookcli.test(config, "", body=body, content_type="application/json", delivery=None, file=draft)
    assert out["hook"] == "draft" and out["items"][0]["state"] == sp.NO_MATCH
    assert not hookcli.test(config, "ghost", body=body, content_type="", delivery=None, file=None)["ok"]


def pending_delivery(config: Path) -> int:
    spool = sp.Spool(sp.spool_path_for(config))
    try:
        d = spool.add_delivery("alerts", source="x", content_type="", headers={}, body=b"{}")
        spool.update(d, state=sp.PENDING, runnable="demo")
        return d
    finally:
        spool.close()


def test_decide(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "ann")
    d = pending_delivery(config)
    out = hookcli.decide(config, d, "approve", "go")
    assert out["won"] and out["delivery"]["decided_by"] == "ann"
    assert "isn't running" in out["warning"]
    monkeypatch.setenv("SUDO_USER", "bob")
    lost = hookcli.decide(config, d, "deny", "")
    assert not lost["ok"] and "already approved by ann" in lost["error"]
    assert not hookcli.decide(config, 999, "approve", "")["ok"]
    assert "approved by ann" in hookcli.render_decision(out)


def test_mute_follow_once(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "ann")
    pending_delivery(config)
    hookcli.mute(config, "alerts", "30m", "lunch")
    with pytest.raises(ValueError):
        hookcli.mute(config, "alerts", "0s", "")
    buf = io.StringIO()
    assert hookcli.follow(config, since=0, replay="15m", who=None, once=True, out=buf) == 0
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    hello = lines[0]
    assert hello["kind"] == "hello" and hello["who"] == "ann"
    assert [p["id"] for p in hello["pending"]] == [1]
    assert hello["mutes"][0]["reason"] == "lunch"
    assert [e["kind"] for e in lines[1:]] == ["state", "mute"]
    # the follower is gone once it returns
    spool = sp.Spool(sp.spool_path_for(config))
    assert spool.followers() == []
    spool.close()
    assert hookcli.unmute(config, "alerts")["removed"] is True


def test_follow_replay_window(config: Path) -> None:
    pending_delivery(config)
    buf = io.StringIO()
    hookcli.follow(config, since=None, replay="15m", who="x", once=True, out=buf)
    assert len(buf.getvalue().splitlines()) == 2  # hello + the event inside the window


def test_deliveries_and_show(config: Path) -> None:
    assert hookcli.deliveries(config, hook=None, state=None, limit=10) == {"ok": True, "deliveries": []}
    d = pending_delivery(config)
    out = hookcli.deliveries(config, hook="alerts", state=sp.PENDING, limit=10)
    assert [r["id"] for r in out["deliveries"]] == [d]
    assert "pending" in hookcli.render_deliveries(out)
    shown = hookcli.show(config, d)
    assert shown["delivery"]["body"] == "{}" and shown["delivery"]["headers"] == {}
    assert not hookcli.show(config, 999)["ok"]
