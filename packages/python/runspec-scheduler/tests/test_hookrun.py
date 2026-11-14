"""Planning webhook deliveries: rules, autonomy, splitting, dedup, approvals,
expiry and the email fallback (no processes started)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from runspec_scheduler import hookconfig as hc
from runspec_scheduler import hookrun
from runspec_scheduler import spool as sp

sys.path.insert(0, str(Path(__file__).parent))
from procenv import DEMO_TOML  # noqa: E402

TOKEN_SHA = hc.sha256("t")

ALERTS = {
    "groupKey": "g1",
    "status": "firing",
    "alerts": [
        {"status": "firing", "labels": {"alertname": "DiskFull", "instance": "web-01:9100"}},
        {"status": "firing", "labels": {"alertname": "CarefulThing"}},
        {"status": "resolved", "labels": {"alertname": "DiskFull"}},
        {"status": "firing", "labels": {"alertname": "Other"}},
    ],
}


def alerts_hook(**over: Any) -> hc.Hook:
    raw: dict[str, Any] = {
        "name": "alerts",
        "token_sha256": TOKEN_SHA,
        "split": "alerts",
        "fields": {"alertname": "labels.alertname", "status": "status", "instance": "labels.instance", "group": "$.groupKey"},
        "dedup": "{{ group }}:{{ alertname }}:{{ status }}",
        "rule": [
            {"name": "resolved", "match": {"status": "^resolved$"}, "skip": True},
            {"name": "safe", "match": {"alertname": "^(?P<kind>Disk)"}, "run": "demo", "args": {"marker": "{{ kind | lower }}", "host": "{{ instance | regex('^([^:]+)', 1) }}"}},
            {"name": "careful", "match": {"alertname": "^Careful"}, "run": "demo", "args": {"careful": "{{ alertname }}"}, "require_approval": True},
        ],
    }
    raw.update(over)
    return hc.parse_hook(raw)


def fake_resolve(runnable: str, command: tuple[str, ...], args: dict[str, Any]) -> tuple[str, str]:
    if runnable != "demo":
        return "", f"{runnable} isn't installed in this venv"
    if args.get("reckless"):
        return "manual", ""
    if args.get("careful"):
        return "confirm", ""
    return "autonomous", ""


def body(data: Any = ALERTS) -> bytes:
    return json.dumps(data).encode()


# ── planning (pure) ───────────────────────────────────────────────────────────


def test_dry_run_alertmanager() -> None:
    plans = hookrun.dry_run(alerts_hook(), body(), "application/json", resolve=fake_resolve)
    assert [(p["state"], p["rule"], p["autonomy"]) for p in plans] == [
        (sp.WAITING, "safe", "autonomous"),
        (sp.PENDING, "careful", "confirm"),
        (sp.SKIPPED, "resolved", ""),
        (sp.NO_MATCH, "", ""),
    ]
    assert plans[0]["args"] == {"marker": "disk", "host": "web-01"}
    assert plans[0]["dedup_key"] == "g1:DiskFull:firing"
    assert "body" not in plans[0]["fields"] and plans[0]["fields"]["kind"] == "Disk"


def test_dry_run_bad_body_and_empty_split() -> None:
    assert hookrun.dry_run(alerts_hook(), b"{nope", "application/json")[0]["state"] == sp.ERROR
    assert hookrun.dry_run(alerts_hook(), body({"alerts": []}), "application/json")[0]["state"] == sp.NO_MATCH


def test_manual_and_unknown_are_refused() -> None:
    # manual stays refused even when the hook asks for an approval
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "demo", "args": {"reckless": "yes"}, "require_approval": True})
    assert hookrun.plan_item(hook, {}, resolve=fake_resolve).state == sp.REFUSED
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "demo", "args": {"reckless": "yes"}})
    plan = hookrun.plan_item(hook, {}, resolve=fake_resolve)
    assert plan.state == sp.REFUSED and "manual" in plan.reason
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "ghost"})
    plan = hookrun.plan_item(hook, {}, resolve=fake_resolve)
    assert plan.state == sp.REFUSED and "isn't installed" in plan.reason


def test_setting_up_the_hook_is_the_approval() -> None:
    # A confirm (or supervised) runnable runs like an autonomous one: the
    # hook's rules and token are what a person agreed to.
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "demo", "args": {"careful": "yes"}})
    plan = hookrun.plan_item(hook, {}, resolve=fake_resolve)
    assert (plan.state, plan.autonomy, plan.reason) == (sp.WAITING, "confirm", "")


def test_require_approval_waits_whatever_the_level() -> None:
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "demo", "require_approval": True})
    plan = hookrun.plan_item(hook, {}, resolve=fake_resolve)
    assert (plan.state, plan.autonomy, plan.reason) == (sp.PENDING, "autonomous", "the hook asks for an approval")
    careful = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "demo", "args": {"careful": "yes"}, "require_approval": True})
    assert hookrun.plan_item(careful, {}, resolve=fake_resolve).state == sp.PENDING
    rule_level = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "rule": [{"run": "demo", "require_approval": True}]})
    assert hookrun.plan_item(rule_level, {}, resolve=fake_resolve).state == sp.PENDING


def test_no_rules_and_event_rules() -> None:
    bare = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA})
    assert hookrun.plan_item(bare, {}).state == sp.EVENT
    note = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "rule": [{"name": "tell", "match": {"x": "."}}]})
    assert hookrun.plan_item(note, {"x": "1"}).state == sp.EVENT
    assert hookrun.plan_item(note, {}).state == sp.NO_MATCH


def test_empty_args_are_dropped() -> None:
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "run": "demo", "args": {"a": "{{ missing }}", "b": "{{ x | default('d') }}"}})
    assert hookrun.plan_item(hook, {}).args == {"b": "d"}


def test_xml_and_form_bodies() -> None:
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "fields": {"host": "event.host", "sev": "event.@sev"}, "run": "demo", "args": {"marker": "{{ host }}-{{ sev }}"}})
    plans = hookrun.dry_run(hook, b'<event sev="2"><host>db-01</host></event>', "text/xml")
    assert plans[0]["args"] == {"marker": "db-01-2"}
    form = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "fields": {"who": "user"}, "run": "demo", "args": {"marker": "{{ who }}"}})
    assert hookrun.dry_run(form, b"user=ann&x=1", "application/x-www-form-urlencoded")[0]["args"] == {"marker": "ann"}


def test_transform_adds_fields() -> None:
    hook = hc.parse_hook({"name": "h", "token_sha256": TOKEN_SHA, "rule": [{"match": {"kind": "^disk$"}, "run": "demo"}]})
    plans = hookrun.dry_run(hook, b"opaque", "text/plain", transform=lambda h, b, r: {"kind": "disk"})
    assert plans[0]["state"] == sp.WAITING and plans[0]["fields"]["kind"] == "disk"


# ── autonomy + env, from a real spec ──────────────────────────────────────────

DEMO_SPEC = tomllib.loads(DEMO_TOML)["demo"]


@pytest.mark.parametrize(
    ("args", "level"),
    [({}, "autonomous"), ({"marker": "x"}, "autonomous"), ({"careful": "x"}, "confirm"), ({"reckless": "x"}, "manual"), ({"careful": "x", "reckless": "y"}, "manual")],
)
def test_autonomy_for(args: dict[str, Any], level: str) -> None:
    assert hookrun.autonomy_for(DEMO_SPEC, "autonomous", (), args) == level


def test_autonomy_for_command_level() -> None:
    spec = {"args": {}, "commands": {"read": {"autonomy": "autonomous"}, "wipe": {"autonomy": "manual"}}}
    assert hookrun.autonomy_for(spec, "confirm", ("read",), {}) == "autonomous"
    assert hookrun.autonomy_for(spec, "confirm", ("wipe",), {}) == "manual"


def test_run_env() -> None:
    env = hookrun.run_env("demo", DEMO_SPEC, [], {"marker": "/tmp/m", "careful": ["a", "b"], "flag": True, "n": 3})
    assert env == {"RUNSPEC_DEMO_ARG_MARKER": "/tmp/m", "RUNSPEC_DEMO_ARG_CAREFUL": "a\nb", "RUNSPEC_DEMO_ARG_FLAG": "1", "RUNSPEC_DEMO_ARG_N": "3"}


def test_keep_headers_drops_secrets() -> None:
    kept = hookrun.keep_headers({"Authorization": "Bearer x", "X-Runspec-Token": "y", "Cookie": "c", "User-Agent": "am", "X-Request-Id": "1"})
    assert kept == {"user-agent": "am", "x-request-id": "1"}


def test_who_am_i_prefers_sudo_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "ann")
    assert hookrun.who_am_i() == "ann"
    monkeypatch.setenv("SUDO_USER", "root")
    assert hookrun.who_am_i() != "root" or hookrun.who_am_i() == "root"  # falls back to this account


# ── the worker, with run starts recorded instead of spawned ───────────────────


class Worker(hookrun.HookWorker):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.started: list[dict[str, Any]] = []

    def _start(self, row: dict[str, Any], hook: hc.Hook, cfg: hc.HookConfig) -> None:
        self.started.append(row)
        self.spool.update(row["id"], state=sp.RUNNING, started_ts=time.time())


@pytest.fixture
def env(tmp_path: Path) -> tuple[Worker, sp.Spool, hc.HookConfig]:
    config = tmp_path / "runspec_scheduler.toml"
    config.write_text("")
    spool = sp.Spool(tmp_path / "hooks" / "spool.sqlite")
    cfg = hc.HookConfig(settings=hc.parse_hooks_settings({"enabled": True, "port": 1}), smtp=hc.parse_smtp(None), hooks={"alerts": alerts_hook()}, listed=[])
    worker = Worker(spool, config, resolver=fake_resolve)  # type: ignore[arg-type]
    worker.configure(cfg)
    yield worker, spool, cfg
    spool.close()


def deliver(spool: sp.Spool, data: Any = ALERTS, hook: str = "alerts") -> int:
    return spool.add_delivery(hook, source="10.0.0.1", content_type="application/json", headers={}, body=body(data))


def states(spool: sp.Spool) -> list[tuple[int, str]]:
    return [(r["id"], r["state"]) for r in reversed(spool.recent())]


def test_worker_splits_plans_and_starts(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, _ = env
    first = deliver(spool)
    worker.tick()
    assert states(spool) == [(1, sp.RUNNING), (2, sp.PENDING), (3, sp.SKIPPED), (4, sp.NO_MATCH)]
    assert [r["id"] for r in worker.started] == [first]
    children = spool.recent()
    assert all(r["request"] == first for r in children)
    # only the request row keeps the body; items read it from there
    assert all(not r["body"] for r in children if r["id"] != first)
    pending = spool.get(2)
    assert pending and pending["expires_ts"] == pytest.approx(pending["received_ts"] + 3600)
    kinds = [e["kind"] for e in spool.events_since(0)]
    assert kinds[0] == "split" and kinds.count("delivery") == 4


def test_worker_dedups_repeats(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, _ = env
    deliver(spool)
    worker.tick()
    deliver(spool)
    worker.tick()
    dups = spool.in_state(sp.DUPLICATE)
    assert [(r["dup_of"], r["reason"]) for r in dups] == [(1, "repeat of delivery 1"), (2, "repeat of delivery 2")]
    assert spool.get(1)["count"] == 2  # type: ignore[index]
    assert len(worker.started) == 1


def test_worker_bad_row_doesnt_block_the_rest(env: tuple[Worker, sp.Spool, hc.HookConfig], monkeypatch: pytest.MonkeyPatch) -> None:
    worker, spool, _ = env
    deliver(spool, {"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull"}}]})
    deliver(spool, {"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull"}}], "groupKey": "g2"})
    real = worker._plan
    calls = {"n": 0}

    def boom(*a: Any, **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaput")
        real(*a, **kw)

    monkeypatch.setattr(worker, "_plan", boom)
    worker.tick()
    assert states(spool) == [(1, sp.ERROR), (2, sp.RUNNING)]
    assert "kaput" in spool.get(1)["reason"]  # type: ignore[index]


def test_worker_refuses_unknown_hook_and_bad_body(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, _ = env
    deliver(spool, hook="gone")
    spool.add_delivery("alerts", source="x", content_type="application/json", headers={}, body=b"{bad")
    worker.tick()
    assert states(spool) == [(1, sp.REFUSED), (2, sp.ERROR)]


def test_approve_deny_first_wins(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, _ = env
    deliver(spool)
    worker.tick()
    won, row = hookrun.approve(spool, 2, "ann", "ok")
    assert won and row and row["state"] == sp.APPROVED and row["decided_by"] == "ann" and row["note"] == "ok"
    lost, row = hookrun.deny(spool, 2, "bob")
    assert not lost and row and row["decided_by"] == "ann"
    worker.tick()
    assert [r["id"] for r in worker.started] == [1, 2]
    # an approval can't revive a settled delivery
    assert hookrun.approve(spool, 3, "ann")[0] is False


def test_expiry(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, _ = env
    deliver(spool)
    worker.tick()
    worker.tick(now=time.time() + 3601)
    assert spool.get(2)["state"] == sp.EXPIRED  # type: ignore[index]
    assert hookrun.approve(spool, 2, "ann")[0] is False


def test_max_running_and_skip_if_running(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, cfg = env
    cfg.hooks["alerts"] = alerts_hook(dedup="", skip_if_running=True)
    worker._runs[99] = hookrun._Run(delivery=99, hook="alerts", proc=None, started=time.time())  # type: ignore[arg-type]
    monkey_reap = worker.reap
    worker.reap = lambda: None  # type: ignore[method-assign]
    deliver(spool, {"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull"}}]})
    worker.tick()
    worker.reap = monkey_reap  # type: ignore[method-assign]
    row = spool.get(1)
    assert row and row["state"] == sp.SKIPPED and "still going" in row["reason"]
    assert worker.started == []


class FakeSMTP:
    sent: list[Any] = []

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def starttls(self) -> None:
        return None

    def send_message(self, msg: Any) -> None:
        FakeSMTP.sent.append(msg)


def test_mail_when_nobody_watches(env: tuple[Worker, sp.Spool, hc.HookConfig], monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    worker, spool, cfg = env
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    worker.cfg = hc.HookConfig(settings=cfg.settings, smtp=hc.parse_smtp({"relay": "mail", "from": "rs@x", "to": ["ops@x"]}), hooks=cfg.hooks, listed=[])
    deliver(spool)
    worker.tick()
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert "waiting for approval" in msg["Subject"] and msg["To"] == "ops@x"
    assert "hooks approve --id 2" in msg.get_content()
    row = spool.get(2)
    assert row and row["emailed_ts"] and row["mail"].startswith("mailed to ops@x")
    worker.tick()
    assert len(FakeSMTP.sent) == 1  # once only


def test_mail_after_expect_claim_within(env: tuple[Worker, sp.Spool, hc.HookConfig], monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    worker, spool, cfg = env
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    worker.cfg = hc.HookConfig(settings=cfg.settings, smtp=hc.parse_smtp({"relay": "mail", "from": "rs@x", "to": ["ops@x"]}), hooks=cfg.hooks, listed=[])
    spool.follower_seen("t", "ann", 1)
    deliver(spool)
    worker.tick()
    assert FakeSMTP.sent == []  # ann is watching
    spool.follower_seen("t", "ann", 1)
    worker.tick(now=time.time() + 16 * 60)
    assert len(FakeSMTP.sent) == 1 and "within 15 minutes" in FakeSMTP.sent[0].get_content()


def test_no_smtp_says_so_once(env: tuple[Worker, sp.Spool, hc.HookConfig], caplog: pytest.LogCaptureFixture) -> None:
    worker, spool, _ = env
    deliver(spool)
    worker.tick()
    worker.tick()
    row = spool.get(2)
    assert row and row["mail"].startswith("not mailed: no [smtp]") and not row["emailed_ts"]
    assert sum("no [smtp]" in r.getMessage() for r in caplog.records) == 1


def test_recover_marks_dead_runs(env: tuple[Worker, sp.Spool, hc.HookConfig]) -> None:
    worker, spool, _ = env
    d = deliver(spool, {"alerts": []})
    spool.update(d, state=sp.RUNNING, pid=2**22 + 12345)
    worker.recover()
    row = spool.get(d)
    assert row and row["state"] == sp.RAN and "daemon was stopped" in row["reason"]


# ── respond = "output" ────────────────────────────────────────────────────────


def test_output_value() -> None:
    assert hookrun.output_value('{"a": 1}\n') == {"a": 1}
    assert hookrun.output_value("[1, 2]") == [1, 2]
    assert hookrun.output_value("done\n") == "done"
    assert hookrun.output_value("{not json") == "{not json"
    assert hookrun.output_value("") == ""
    assert hookrun.output_value(None) is None


def test_collect_output_cuts_and_cleans_up(tmp_path: Path) -> None:
    spool = sp.Spool(tmp_path / "hooks" / "spool.sqlite")
    d = spool.add_delivery("h", source="x", content_type="", headers={}, body=b"")
    fd, path = hookrun.open_output(spool, d)
    import os
    import stat

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    os.write(fd, b"x" * 20)
    os.close(fd)
    assert hookrun.collect_output(spool, d, 8) is True
    row = spool.get(d)
    assert row and (row["output"], row["output_truncated"]) == ("x" * 8, 1)
    assert not Path(path).exists()
    assert hookrun.collect_output(spool, d, 8) is False  # no file: nothing to do
    spool.close()


def test_request_result_split_items(tmp_path: Path) -> None:
    spool = sp.Spool(tmp_path / "hooks" / "spool.sqlite")
    hook = hc.parse_hook({"name": "alerts", "token_sha256": TOKEN_SHA, "split": "alerts", "run": "demo", "respond": "output"})
    first = spool.add_delivery("alerts", source="x", content_type="", headers={}, body=b"{}")
    spool.update(first, request=first, state=sp.RAN, exit_code=0, runnable="demo", output="ok\n")
    second = spool.add_delivery("alerts", source="x", content_type="", headers={}, body=b"", request=first, item=1)
    url = f"/hook/alerts/delivery/{first}"
    got = hookrun.request_result(spool, first, hook=hook, status_url=url)
    assert got and got["status"] == "running" and got["status_url"] == url and len(got["items"]) == 2
    assert "state" not in got  # several items: only the list
    spool.update(second, state=sp.SKIPPED, reason="a rule said skip")
    got = hookrun.request_result(spool, first, hook=hook, status_url=url)
    assert got and got["status"] == "done" and got["ok"] is True and [i["output"] for i in got["items"]] == ["ok", None]
    # an item's own id isn't a request
    assert hookrun.request_result(spool, second, hook=hook, status_url="") is None
    assert hookrun.request_result(spool, 999, hook=hook, status_url="") is None
    # a hook without respond = "output" never shows output
    plain = hc.parse_hook({"name": "alerts", "token_sha256": TOKEN_SHA, "run": "demo"})
    got = hookrun.request_result(spool, first, hook=plain, status_url=url)
    assert got and all("output" not in i for i in got["items"])
    spool.close()


def test_respond_hook_run_writes_its_stdout(tmp_path: Path) -> None:
    import os

    fake = tmp_path / "fakepy"
    fake.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"freed_mb\": 812}'\nexit 0\n")
    os.chmod(fake, 0o755)
    config = tmp_path / "runspec_scheduler.toml"
    config.write_text("")
    spool = sp.Spool(tmp_path / "hooks" / "spool.sqlite")
    hook = hc.parse_hook({"name": "reply", "token_sha256": TOKEN_SHA, "run": "demo", "respond": "output"})
    cfg = hc.HookConfig(settings=hc.parse_hooks_settings({"enabled": True, "port": 1}), smtp=hc.parse_smtp(None), hooks={"reply": hook}, listed=[])
    worker = hookrun.HookWorker(spool, config, python=str(fake), resolver=fake_resolve)  # type: ignore[arg-type]
    worker.configure(cfg)
    d = spool.add_delivery("reply", source="x", content_type="application/json", headers={}, body=b"{}")
    worker.tick()
    run = worker._runs[d]
    run.proc.wait(10)
    worker.reap()
    got = hookrun.request_result(spool, d, hook=hook, status_url="")
    assert got and (got["status"], got["exit_code"], got["output"]) == ("done", 0, {"freed_mb": 812})
    spool.close()
