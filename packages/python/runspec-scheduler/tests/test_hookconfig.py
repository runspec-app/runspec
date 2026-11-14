"""Webhook settings, hook tables, the hooks.d layer and section-preserving writes."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from runspec_scheduler import config as cfgmod
from runspec_scheduler import hookconfig as hc

TOKEN = "s3cret-token"
TOKEN_SHA = hc.sha256(TOKEN)


def hook(**over: object) -> dict[str, object]:
    raw: dict[str, object] = {"name": "alerts", "token_sha256": TOKEN_SHA}
    raw.update(over)
    return raw


# ── [hooks] / [smtp] ──────────────────────────────────────────────────────────


def test_settings_defaults_are_off() -> None:
    s = hc.parse_hooks_settings(None)
    assert not s.enabled and s.bind == "127.0.0.1" and s.max_body == 256 * 1024 and s.keep == 7 * 86400
    assert "off" in s.listen_problem()


def test_settings_parse() -> None:
    table = {"enabled": True, "port": 9443, "bind": "0.0.0.0", "cert": "/c", "key": "/k", "max_body": "1m"}
    s = hc.parse_hooks_settings({**table, "base_path": "runspec/ops/", "trusted_proxies": ["10.0.0.0/8"], "keep": "2d"})
    assert s.tls and s.max_body == 1024 * 1024 and s.base_path == "/runspec/ops" and s.keep == 2 * 86400
    assert s.listen_problem() == ""
    assert s.url("web-01", "alerts") == "https://web-01:9443/runspec/ops/hook/alerts"
    assert s.to_dict()["trusted_proxies"] == ["10.0.0.0/8"]


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"cert": "/c"}, "cert and key go together"),
        ({"port": 70000}, "port"),
        ({"base_path": "/a b"}, "base_path"),
        ({"trusted_proxies": ["nope"]}, "isn't an address"),
        ({"max_body": "lots"}, "max_body"),
        ("x", "must be a table"),
    ],
)
def test_settings_bad(table: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        hc.parse_hooks_settings(table)


def test_plain_http_only_on_loopback() -> None:
    assert "needs a certificate" in hc.parse_hooks_settings({"enabled": True, "port": 1, "bind": "0.0.0.0"}).listen_problem()
    assert hc.parse_hooks_settings({"enabled": True, "port": 1, "bind": "::1"}).listen_problem() == ""
    assert hc.parse_hooks_settings({"enabled": True, "port": 1, "bind": "localhost"}).listen_problem() == ""
    assert "port" in hc.parse_hooks_settings({"enabled": True}).listen_problem()


def test_smtp() -> None:
    assert not hc.parse_smtp(None).configured
    s = hc.parse_smtp({"relay": "mail", "from": "a@x", "to": ["b@x"], "starttls": True})
    assert s.configured and s.to == ("b@x",) and s.to_dict()["from"] == "a@x"


# ── [[hook]] ──────────────────────────────────────────────────────────────────


def test_parse_hook_full() -> None:
    h = hc.parse_hook(
        hook(
            fields={"alertname": "labels.alertname", "group": "$.groupKey"},
            split="alerts",
            allow=["10.0.0.0/8"],
            rule=[
                {"name": "skip-resolved", "match": {"status": "^resolved$"}, "skip": True},
                {"match": {"alertname": "Disk"}, "run": "demo", "command": "fix now", "args": {"host": "{{ alertname | lower }}"}},
            ],
            run="notify",
            dedup="{{ alertname }}",
        )
    )
    assert [r.name for r in h.rules] == ["skip-resolved", "rule-2", "default"]
    assert [r.action for r in h.rules] == ["skip", "run", "run"]
    assert h.rules[1].command == ("fix", "now")
    assert h.allows("10.1.2.3") and not h.allows("192.168.1.1") and not h.allows("nonsense")
    assert h.check_token(TOKEN) and not h.check_token("wrong") and not h.check_token("")
    summary = h.summary()
    assert summary["auth"] == ["token"] and TOKEN_SHA not in str(summary)


def test_basic_auth() -> None:
    h = hc.parse_hook({"name": "b", "username": "svc", "password_hash": hc.hash_password("pw", iterations=1000)})
    assert h.check_basic("svc", "pw") and not h.check_basic("svc", "no") and not h.check_basic("x", "pw")
    assert h.check_basic("svc", "pw")  # second time from the in-memory match
    assert not h.check_basic("svc", "") and not h.check_token("pw")


def test_password_hash_is_salted_and_slow() -> None:
    a, b = hc.hash_password("pw", iterations=1000), hc.hash_password("pw", iterations=1000)
    assert a != b and a.startswith("pbkdf2_sha256$1000$")
    assert hc.verify_password("pw", a) and hc.verify_password("pw", b) and not hc.verify_password("pw2", a)
    assert not hc.verify_password("pw", "pbkdf2_sha256$1000$!!!!!!!!$" + "A" * 44)
    assert not hc.verify_password("pw", hc.sha256("pw"))
    assert hc.PBKDF2_ITERATIONS >= 600_000
    assert hc.hash_password("pw").startswith(f"pbkdf2_sha256${hc.PBKDF2_ITERATIONS}$")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"name": "Bad Name", "token_sha256": TOKEN_SHA}, "must be lowercase"),
        ({"name": "a"}, "needs a token"),
        ({"name": "a", "token_sha256": "plaintext"}, "never the token itself"),
        ({"name": "a", "username": "u", "password_hash": "plaintext"}, "never the password itself"),
        ({"name": "a", "username": "u", "password_hash": TOKEN_SHA}, "never the password itself"),
        ({"name": "a", "username": "u"}, "needs both username and password_hash"),
        (hook(color="red"), "unknown key"),
        (hook(fields={"x": "a..b"}), "empty step"),
        (hook(rule=[{"match": {"x": "("}}]), "match.x"),
        (hook(rule=[{"run": "x", "skip": True}]), "skips or runs"),
        (hook(rule=[{"args": {"a": "b"}}]), "need run"),
        (hook(rule=[{"run": "x", "args": {"a": "{{ a | eval }}"}}]), "unknown filter"),
        (hook(rule=[{"run": "x", "when": "always"}]), "unknown key"),
        (hook(dedup="{{ x"), "dedup"),
        (hook(token_header="X Bad"), "token_header"),
        (hook(rate_per_minute=0), "rate_per_minute"),
        (hook(allow=["999.1.1.1"]), "isn't an address"),
    ],
)
def test_parse_hook_bad(raw: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        hc.parse_hook(raw)


def test_parse_size() -> None:
    assert hc.parse_size("64k", "x") == 64 * 1024
    assert hc.parse_size(10, "x") == 10
    with pytest.raises(ValueError):
        hc.parse_size("-1", "x")


# ── layers ────────────────────────────────────────────────────────────────────


def write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    path.chmod(mode)
    return path


def test_deployed_wins_and_errors_are_reported(tmp_path: Path) -> None:
    cfg = write(
        tmp_path / "runspec_scheduler.toml",
        f"""
        [[hook]]
        name = "alerts"
        token_sha256 = "{TOKEN_SHA}"
        description = "console copy"

        [[hook]]
        name = "console-only"
        token_sha256 = "{TOKEN_SHA}"

        [[hook]]
        name = "broken"
        """,
    )
    write(
        tmp_path / "hooks.d" / "ops.toml",
        f"""
        [[hook]]
        name = "alerts"
        token_sha256 = "{TOKEN_SHA}"
        description = "deployed"
        """,
    )
    write(tmp_path / "hooks.d" / "loose.toml", f'[[hook]]\nname = "loose"\ntoken_sha256 = "{TOKEN_SHA}"\n', mode=0o666)
    loaded = hc.load_hook_config(cfg)
    assert sorted(loaded.hooks) == ["alerts", "console-only"]
    assert loaded.hooks["alerts"].description == "deployed" and loaded.hooks["alerts"].source == "deployed"
    listing = {(r["source"], r["name"]): r for r in loaded.listing()}
    assert listing[("console", "alerts")]["overridden"] is True
    errors = {e["hook"] or Path(e["file"]).name: e["error"] for e in loaded.errors}
    assert "needs a token" in errors["broken"]
    assert "group or others can write it" in errors["loose.toml"]


def test_signature_changes_with_hooks_d(tmp_path: Path) -> None:
    cfg = write(tmp_path / "runspec_scheduler.toml", "")
    before = hc.signature(cfg)
    write(tmp_path / "hooks.d" / "a.toml", "")
    assert hc.signature(cfg) != before


def test_check_file(tmp_path: Path) -> None:
    good = write(tmp_path / "good.toml", f'[[hook]]\nname = "a"\ntoken_sha256 = "{TOKEN_SHA}"\n')
    assert hc.check_file(good) == []
    bad = write(tmp_path / "bad.toml", f'[hooks]\ncert = "/c"\n\n[[hook]]\nname = "a"\ntoken_sha256 = "{TOKEN_SHA}"\n\n[[hook]]\nname = "a"\ntoken_sha256 = "{TOKEN_SHA}"\n')
    problems = hc.check_file(bad)
    assert any("cert and key" in p for p in problems) and any("more than once" in p for p in problems)
    assert hc.check_file(write(tmp_path / "broken.toml", "[[hook]\n"))


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
def test_deployed_file_problem_not_regular(tmp_path: Path) -> None:
    (tmp_path / "d.toml").mkdir()
    assert hc.deployed_file_problem(tmp_path / "d.toml") == "not a regular file"


# ── writing the console layer ─────────────────────────────────────────────────

SCHEDULES = """\
[scheduler]
backend = "cron"

[[schedule]]
id = "nightly"
runnable = "demo"
cron = "0 2 * * *"
"""


def test_save_hook_keeps_schedules_and_hashes_secrets(tmp_path: Path) -> None:
    cfg = write(tmp_path / "runspec_scheduler.toml", SCHEDULES)
    hc.save_hook(cfg, {"name": "alerts", "token": TOKEN, "fields": {"a": "b.c"}, "rule": [{"name": "r", "match": {"a": "^x$"}, "run": "demo", "args": {"m": "{{ a }}"}}]})
    text = cfg.read_text()
    assert TOKEN not in text and TOKEN_SHA in text
    data = tomllib.loads(text)
    assert data["schedule"][0]["id"] == "nightly" and data["scheduler"]["backend"] == "cron"
    assert data["hook"][0]["rule"][0]["args"] == {"m": "{{ a }}"}
    # replace in place, then remove
    hc.save_hook(cfg, {"name": "alerts", "token_sha256": TOKEN_SHA, "description": "v2"})
    data = tomllib.loads(cfg.read_text())
    assert len(data["hook"]) == 1 and data["hook"][0]["description"] == "v2"
    assert hc.remove_hook(cfg, "alerts") is True
    assert hc.remove_hook(cfg, "alerts") is False
    data = tomllib.loads(cfg.read_text())
    assert "hook" not in data and data["schedule"][0]["id"] == "nightly"


def test_save_hook_refuses_over_deployed(tmp_path: Path) -> None:
    cfg = write(tmp_path / "runspec_scheduler.toml", "")
    write(tmp_path / "hooks.d" / "ops.toml", f'[[hook]]\nname = "alerts"\ntoken_sha256 = "{TOKEN_SHA}"\n')
    with pytest.raises(ValueError, match="deployed from"):
        hc.save_hook(cfg, hook())


def test_save_hook_validates_first(tmp_path: Path) -> None:
    cfg = write(tmp_path / "runspec_scheduler.toml", SCHEDULES)
    with pytest.raises(ValueError):
        hc.save_hook(cfg, {"name": "alerts"})
    assert cfg.read_text() == SCHEDULES


def test_schedule_writes_keep_hooks(tmp_path: Path) -> None:
    cfg = write(tmp_path / "runspec_scheduler.toml", SCHEDULES)
    hc.set_settings(cfg, "hooks", {"enabled": True, "port": 9000})
    hc.save_hook(cfg, hook(rule=[{"run": "demo"}]))
    entries = cfgmod.raw_schedules(cfg)
    cfgmod.write_schedules(cfg, [*entries, {"id": "hourly", "runnable": "demo", "cron": "0 * * * *"}])
    data = tomllib.loads(cfg.read_text())
    assert [s["id"] for s in data["schedule"]] == ["nightly", "hourly"]
    assert data["hooks"] == {"enabled": True, "port": 9000}
    assert data["hook"][0]["rule"] == [{"run": "demo"}]


def test_set_settings(tmp_path: Path) -> None:
    cfg = write(tmp_path / "runspec_scheduler.toml", SCHEDULES)
    assert hc.set_settings(cfg, "smtp", {"relay": "mail", "from": "a@x"}) == {"relay": "mail", "from": "a@x"}
    hc.set_settings(cfg, "smtp", {"relay": None})
    assert tomllib.loads(cfg.read_text())["smtp"] == {"from": "a@x"}
    with pytest.raises(ValueError, match="not a \\[hooks\\] setting"):
        hc.set_settings(cfg, "hooks", {"colour": "red"})
    with pytest.raises(ValueError, match="cert and key"):
        hc.set_settings(cfg, "hooks", {"cert": "/c"})
    assert "cert" not in tomllib.loads(cfg.read_text()).get("hooks", {})


def test_split_sections_keeps_dotted_tables_with_their_root() -> None:
    text = '# top\n[[hook]]\nname = "a"\n\n[[hook.rule]]\nrun = "x"\n\n[smtp]\nrelay = "m"\n'
    preamble, sections = cfgmod.split_sections(text)
    assert preamble == "# top"
    assert [root for root, _ in sections] == ["hook", "smtp"]
    assert "[[hook.rule]]" in sections[0][1]


def test_rewrite_refuses_to_change_untouched_tables() -> None:
    with pytest.raises(ValueError, match="would change \\[smtp\\]"):
        cfgmod._check_kept('[smtp]\nrelay = "m"\n', '[smtp]\nrelay = "n"\n', {"hook"})
    cfgmod._check_kept('[smtp]\nrelay = "m"\n', '[smtp]\nrelay = "n"\n', {"smtp"})


def test_rule_after_another_table_still_reads_back(tmp_path: Path) -> None:
    cfg = write(tmp_path / "c.toml", '[[hook]]\nname = "a"\n\n[smtp]\nrelay = "m"\n\n[[hook.rule]]\nrun = "x"\n')
    cfgmod.rewrite_sections(cfg, drop={"smtp"}, add=[])
    assert tomllib.loads(cfg.read_text()) == {"hook": [{"name": "a", "rule": [{"run": "x"}]}]}


def test_with_secret() -> None:
    raw, token = hc.with_secret({"name": "a"})
    assert raw["token_sha256"] == hc.sha256(token) and len(token) > 30
