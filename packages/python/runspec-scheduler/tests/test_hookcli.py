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
    out = hookcli.save(config, HOOK_TOML, new_token=True)
    token = out["secrets"]["token"]
    assert out["ok"] and not out["replaced"] and out["url"].endswith(":18990/hook/alerts")
    text = config.read_text()
    assert token not in text and hc.sha256(token) in text
    # saving again without a new token keeps the stored hash
    again = hookcli.save(config, HOOK_TOML.replace('"note"', '"tell"'), new_token=False)
    assert again["replaced"] and "secrets" not in again
    assert tomllib.loads(config.read_text())["hook"][0]["token_sha256"] == hc.sha256(token)
    listing = hookcli.hooks_status(config)
    assert [h["name"] for h in listing["hooks"]] == ["alerts"] and listing["pending"] == []
    assert "alerts" in hookcli.render_list(listing)
    assert hookcli.remove(config, "alerts")["ok"]
    assert not hookcli.remove(config, "alerts")["ok"]


def test_save_json_and_basic_password(config: Path) -> None:
    out = hookcli.save(config, json.dumps({"name": "svc", "username": "sender", "password": "chosen-password"}), new_token=False)
    # a given password is hashed and never echoed back
    assert "basic" in out["hook"]["auth"] and out["new_secret"] == ["password"] and "secrets" not in out
    assert "chosen-password" not in json.dumps(out) and "chosen-password" not in config.read_text()
    stored = tomllib.loads(config.read_text())["hook"][0]["password_hash"]
    assert hc.verify_password("chosen-password", stored)
    # saving again keeps the stored hash
    hookcli.save(config, json.dumps({"name": "svc", "username": "sender", "description": "x"}), new_token=False)
    assert tomllib.loads(config.read_text())["hook"][0]["password_hash"] == stored


def test_save_refuses_secretless_and_multiple(config: Path) -> None:
    with pytest.raises(ValueError, match="needs a token"):
        hookcli.save(config, HOOK_TOML, new_token=False)
    with pytest.raises(ValueError, match="one \\[\\[hook\\]\\] at a time"):
        hookcli.save(config, HOOK_TOML + "\n[[hook]]\nname = 'b'\n", new_token=True)


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


def test_token_and_basic_hash() -> None:
    out = hookcli.token()
    assert hc.sha256(out["token"]) == out["token_sha256"]
    basic = hookcli.basic_hash("a long chosen password\n")
    assert hc.verify_password("a long chosen password", basic["password_hash"]) and set(basic) == {"ok", "password_hash", "note"}
    assert not hookcli.basic_hash("short\n")["ok"]


def test_save_refuses_token_and_new_token(config: Path) -> None:
    with pytest.raises(ValueError, match="not both"):
        hookcli.save(config, json.dumps({"name": "x", "token": "t" * 40}), new_token=True)


def test_show_secret_skips_the_run_log(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A token goes to the real stdout, not to runspec's tee (which copies
    every line into the run log)."""
    import io
    import sys

    tee = io.StringIO()
    real = io.StringIO()
    monkeypatch.setattr(sys, "stdout", tee)
    monkeypatch.setattr(sys, "__stdout__", real)
    hookcli.show_secret("Token: abc")
    assert real.getvalue() == "Token: abc\n" and tee.getvalue() == ""


def test_check(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text(f'[[hook]]\nname = "a"\ntoken_sha256 = "{hc.sha256("x")}"\n')
    assert hookcli.check(good) == {"ok": True, "file": str(good), "hooks": ["a"], "problems": []}
    bad = tmp_path / "bad.toml"
    bad.write_text('[[hook]]\nname = "a"\n')
    out = hookcli.check(bad)
    assert not out["ok"] and "needs a token" in hookcli.render_check(out)


def test_test_from_stdin_file_and_stored(config: Path, tmp_path: Path) -> None:
    hookcli.save(config, HOOK_TOML, new_token=True)
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


def test_test_draft(config: Path) -> None:
    body = json.dumps({"labels": {"alertname": "Disk"}})
    draft = {"name": "new", "fields": {"alertname": "labels.alertname"}, "rule": [{"name": "disk", "match": {"alertname": "^Disk$"}}]}
    out = hookcli.test_draft(config, json.dumps({"hook": draft, "body": body, "content_type": "application/json"}))
    assert out["ok"] and out["hook"] == "new" and out["items"][0]["rule"] == "disk"
    # a plain token in a draft is never parsed or kept
    out = hookcli.test_draft(config, json.dumps({"hook": {**draft, "token": "secret"}, "body": body}))
    assert out["ok"]
    # no body yet: the last one received under that name
    spool = sp.Spool(sp.spool_path_for(config))
    spool.add_delivery("new", source="x", content_type="application/json", headers={}, body=body.encode())
    spool.close()
    out = hookcli.test_draft(config, json.dumps({"hook": draft}))
    assert out["source"] == "delivery 1"
    assert not hookcli.test_draft(config, "not json")["ok"]
    bad = hookcli.test_draft(config, json.dumps({"hook": {**draft, "rule": [{"match": {"x": "("}}]}, "body": body}))
    assert not bad["ok"] and "match.x" in bad["error"]


def test_listing_carries_the_table_as_toml(config: Path) -> None:
    hookcli.save(config, HOOK_TOML, new_token=True)
    row = hookcli.hooks_status(config)["hooks"][0]
    assert row["raw"]["name"] == "alerts" and row["raw"]["token_sha256"]
    assert row["toml"].startswith("[[hook]]") and 'name = "alerts"' in row["toml"] and "[[hook.rule]]" in row["toml"]


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


def test_claim_first_wins_and_reaches_followers(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = sp.Spool(sp.spool_path_for(config))
    try:
        d = spool.add_delivery("alerts", source="x", content_type="", headers={}, body=b"{}")
        spool.update(d, state=sp.EVENT)
        cursor = spool.last_event_id()
    finally:
        spool.close()
    monkeypatch.setenv("SUDO_USER", "ann")
    won = hookcli.claim(config, d, "on it")
    assert won["ok"] and won["won"] and won["delivery"]["claimed_by"] == "ann"
    assert won["delivery"]["claim_note"] == "on it" and won["delivery"]["state"] == sp.EVENT
    assert "Claimed delivery" in hookcli.render_claim(won)
    monkeypatch.setenv("SUDO_USER", "bob")
    lost = hookcli.claim(config, d, "")
    assert lost["ok"] and not lost["won"] and lost["owner"] == "ann"
    assert "already claimed by ann" in hookcli.render_claim(lost)
    assert not hookcli.claim(config, 999, "")["ok"]
    buf = io.StringIO()
    hookcli.follow(config, since=cursor, replay="15m", who="bob", once=True, out=buf)
    events = [json.loads(line) for line in buf.getvalue().splitlines()[1:]]
    assert [(e["kind"], e["id"], e["claimed_by"]) for e in events] == [("claimed", d, "ann")]


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
