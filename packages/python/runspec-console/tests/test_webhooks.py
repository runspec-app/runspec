"""Following hosts' webhooks: the pure state (webhooks.py), the follow stream
(sources/webhook.py) and the bridge mixin (bridge_webhooks.py) with fakes."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from runspec_console import webhooks as wh
from runspec_console.bridge_webhooks import WebhooksMixin
from runspec_console.sources import webhook as src


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=UTC).isoformat()


NOW = 1_800_000_000.0


def hello(**extra: Any) -> dict[str, Any]:
    msg = {
        "kind": "hello",
        "event": 10,
        "who": "jason@console",
        "settings": {"enabled": True},
        "hooks": [{"name": "alerts"}],
        "mutes": [],
        "pending": [],
    }
    msg.update(extra)
    return msg


def delivery(
    did: int,
    *,
    hook: str = "alerts",
    state: str = "pending",
    kind: str = "delivery",
    event: int = 11,
    received: float = NOW,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "event": event,
        "id": did,
        "hook": hook,
        "state": state,
        "received": iso(received),
        "fields": {"alertname": "Disk", "labels": {"instance": "web-01"}},
        "body": "{}",
    }


# ── pure state ───────────────────────────────────────────────────────────────


def test_parse_duration_and_follow_lists() -> None:
    assert (
        wh.parse_duration("15m") == 900
        and wh.parse_duration("") == 900
        and wh.parse_duration(30) == 30
    )
    with pytest.raises(wh.WebhookError):
        wh.parse_duration("soon")
    assert wh.followed_hooks({"hooks": ["alerts", "Bad Name", "alerts", "*"]}) == [
        "alerts",
        "*",
    ]
    assert (
        wh.followed_hooks({"hooks": "alerts"}) == ["alerts"]
        and wh.followed_hooks({}) == []
    )
    assert wh.validate_follow(["a", "*"]) == ["*"]
    with pytest.raises(wh.WebhookError):
        wh.validate_follow(["no spaces"])


def test_hello_keeps_only_followed_pending() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["alerts"])
    vs.hello(hello(pending=[delivery(1), delivery(2, hook="other")]))
    snap = vs.snapshot()
    assert (
        snap["connected"] and vs.cursor == 10 and [d["id"] for d in snap["open"]] == [1]
    )


def test_apply_tracks_state_and_fires_only_fresh_arrivals() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    vs.hello(hello())
    fresh = vs.apply(delivery(1), now=NOW + 5)
    assert fresh is not None and fresh["id"] == 1 and vs.cursor == 11
    # an old arrival (outside the replay window) is shown but fires nothing
    assert (
        vs.apply(delivery(2, event=12, received=NOW - 3600), now=NOW, replay_s=900)
        is None
        and vs.away == 1
    )
    # a state change keeps the body and settles into recent
    vs.apply(
        {
            "kind": "state",
            "event": 13,
            "id": 1,
            "hook": "alerts",
            "state": "approved",
            "decided_by": "sam",
        },
        now=NOW,
    )
    assert vs.open[1]["state"] == "approved" and vs.open[1]["body"] == "{}"
    vs.apply(
        {
            "kind": "state",
            "event": 14,
            "id": 1,
            "hook": "alerts",
            "state": "ran",
            "exit_code": 0,
        },
        now=NOW,
    )
    snap = vs.snapshot()
    assert (
        [d["id"] for d in snap["open"]] == [2]
        and snap["recent"][0]["id"] == 1
        and vs.cursor == 14
    )
    # pings only move the cursor
    assert vs.apply({"kind": "ping", "event": 20}) is None and vs.cursor == 20


def test_mutes_and_all_hooks_unmute() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    vs.hello(hello())
    until = iso(time.time() + 600)
    vs.apply(
        {
            "kind": "mute",
            "event": 11,
            "who": "me",
            "hook": "*",
            "until": until,
            "reason": "screen locked",
        }
    )
    vs.apply(
        {"kind": "mute", "event": 12, "who": "me", "hook": "alerts", "until": until}
    )
    vs.apply({"kind": "unmute", "event": 13, "who": "me", "hook": "*", "all": False})
    assert [m["hook"] for m in vs.snapshot()["mutes"]] == ["alerts"]
    vs.apply({"kind": "unmute", "event": 14, "who": "me", "hook": None, "all": True})
    assert vs.snapshot()["mutes"] == []


def test_dismissed_cards_are_hidden_but_counted() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    vs.hello(hello(pending=[delivery(1), delivery(2)]))
    vs.dismissed.add(1)
    snap = vs.snapshot()
    assert [d["id"] for d in snap["open"]] == [2] and snap["dismissed"] == 1


def test_trigger_payload_fields_win_and_meta_is_kept() -> None:
    d = delivery(7)
    d["fields"]["host"] = "from-the-body"
    payload = wh.trigger_payload("web-01", "ops", d)
    assert payload["alertname"] == "Disk" and payload["labels.instance"] == "web-01"
    assert (
        payload["host"] == "from-the-body"
        and payload["webhook"]["host"] == "web-01"
        and payload["hook"] == "alerts"
    )


def test_argv_builders() -> None:
    assert wh.follow_argv(None) == ["hooks", "follow", "--replay", "15m"]
    assert wh.follow_argv(42) == ["hooks", "follow", "--since", "42"]
    assert wh.decide_argv("approve", "5", " ok ") == [
        "hooks",
        "approve",
        "--id",
        "5",
        "--note",
        "ok",
    ]
    with pytest.raises(wh.WebhookError):
        wh.decide_argv("maybe", 5)
    assert wh.mute_argv("alerts", "2h", "lunch") == [
        "hooks",
        "mute",
        "--hook",
        "alerts",
        "--for",
        "7200s",
        "--reason",
        "lunch",
    ]
    with pytest.raises(wh.WebhookError):
        wh.mute_argv("alerts", "10s")
    assert wh.unmute_argv("") == ["hooks", "unmute", "--hook", "*"]
    assert wh.save_argv() == ["hooks", "save"]


def test_hook_table_reads_toml_or_a_table() -> None:
    assert wh.hook_table({"text": '[[hook]]\nname = "a"\n'}) == {"name": "a"}
    assert wh.hook_table({"text": 'name = "b"\n'}) == {"name": "b"}
    assert wh.hook_table({"hook": {"name": "c"}}) == {"name": "c"}
    with pytest.raises(wh.WebhookError, match="TOML"):
        wh.hook_table({"text": "name = "})
    with pytest.raises(wh.WebhookError, match="one"):
        wh.hook_table({"text": '[[hook]]\nname = "a"\n[[hook]]\nname = "b"\n'})


def test_new_secrets_are_made_here() -> None:
    hook, made = wh.new_secrets(
        {"name": "a", "username": "svc"}, token=True, password=True
    )
    assert (
        hook["token"] == made["token"]
        and hook["password"] == made["password"]
        and len(made["token"]) > 30
    )
    with pytest.raises(wh.WebhookError, match="username"):
        wh.new_secrets({"name": "a"}, token=False, password=True)


# ── the follow stream ────────────────────────────────────────────────────────


class FakeChannel:
    def __init__(
        self, chunks: list[bytes], *, stderr: bytes = b"", code: int | None = 0
    ) -> None:
        self.chunks = list(chunks)
        self.stderr = stderr
        self.code = code
        self.closed = False

    def settimeout(self, _t: float) -> None:
        pass

    def recv(self, _n: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, _n: int) -> bytes:
        out, self.stderr = self.stderr, b""
        return out

    def exit_status_ready(self) -> bool:
        return self.code is not None

    def recv_exit_status(self) -> int:
        return int(self.code or 0)

    def close(self) -> None:
        self.closed = True


def lines(*objs: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(o).encode() + b"\n" for o in objs)


def test_line_stream_splits_skips_junk_and_releases() -> None:
    released = []
    data = lines(hello()) + b"not json\n\n" + lines(delivery(1))
    stream = src.LineStream(
        FakeChannel([data[:7], data[7:]]), lambda: released.append(1)
    )
    assert (
        stream.recv()["kind"] == "hello"
        and stream.recv()["id"] == 1
        and stream.recv() is None
    )
    assert stream.failure() == ""
    stream.close()
    stream.close()
    assert released == [1]


def test_line_stream_failure_reasons() -> None:
    missing = src.LineStream(FakeChannel([], stderr=b"x: not found\n", code=127))
    assert (
        missing.recv() is None
        and "isn't installed" in missing.failure()
        and src.stuck(missing.failure())
    )
    sudo = src.LineStream(
        FakeChannel([], stderr=b"sudo: a password is required\n", code=1)
    )
    sudo.recv()
    assert "passwordless sudo" in sudo.failure()


def test_follower_run_once_feeds_state_and_arrivals() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    changes: list[int] = []
    arrivals: list[int] = []
    opened: list[int | None] = []

    def open_stream(cursor: int | None) -> src.LineStream:
        opened.append(cursor)
        return src.LineStream(
            FakeChannel(
                [
                    lines(
                        hello(),
                        delivery(1, received=time.time()),
                        {"kind": "ping", "event": 12},
                    )
                ]
            )
        )

    f = src.WebhookFollower(
        vs,
        open_stream,
        on_change=lambda s: changes.append(1),
        on_arrival=lambda s, d: arrivals.append(d["id"]),
    )
    assert f.run_once() == ""
    assert (
        opened == [None] and arrivals == [1] and len(changes) == 2 and vs.cursor == 12
    )
    assert f.run_once() == "" and opened == [None, 12]


def test_follower_reports_a_stream_without_hello() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    f = src.WebhookFollower(
        vs,
        lambda c: src.LineStream(FakeChannel([], stderr=b"boom\n", code=2)),
        on_change=lambda s: None,
        on_arrival=lambda s, d: None,
    )
    assert f.run_once() == "boom"


def test_follower_run_backs_off_and_stops() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    waits: list[float] = []

    def sleep(t: float) -> bool:
        waits.append(t)
        return len(waits) >= 3

    def broken(_c: int | None) -> src.LineStream:
        raise OSError("no route to host")

    f = src.WebhookFollower(
        vs, broken, on_change=lambda s: None, on_arrival=lambda s, d: None, sleep=sleep
    )
    f.run()
    assert (
        waits == [2.0, 4.0, 8.0] and vs.error == "no route to host" and not vs.connected
    )


def test_follower_shows_a_venv_without_webhooks_quietly(caplog: Any) -> None:
    vs = wh.VenvHooks("web-01", "ops-root", hooks=["*"])
    waits: list[float] = []

    def sleep(t: float) -> bool:
        waits.append(t)
        return len(waits) >= 2

    def missing(_c: int | None) -> src.LineStream:
        raise wh.WebhookError(
            "runspec-scheduler isn't installed in this venv (or is older than 0.11.0)"
        )

    f = src.WebhookFollower(
        vs, missing, on_change=lambda s: None, on_arrival=lambda s, d: None, sleep=sleep
    )
    with caplog.at_level("INFO"):
        f.run()
    snap = vs.snapshot()
    assert snap["error"] == "" and "isn't installed" in snap["unavailable"]
    assert waits == [src.BACKOFF_STUCK, src.BACKOFF_STUCK]
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert src.unavailable(src.ROOT_OWNED) and not src.unavailable("no route to host")
    vs.hello({"kind": "hello", "event": 0})
    assert vs.unavailable == "" and vs.connected


def test_root_owned_venv_is_not_followed() -> None:
    b = FakeBridge()
    b._host_entry = lambda host: b._hosts[0]  # type: ignore[attr-defined]
    b._scheduler_owner = lambda host, venv, remote: ("", "root")  # type: ignore[attr-defined]
    with pytest.raises(wh.WebhookError, match="root-owned"):
        b._webhook_command("web-01", "ops", "/srv/ops/bin/runspec", ["hooks", "follow"])


# ── the bridge mixin ─────────────────────────────────────────────────────────


class FakeBridge(WebhooksMixin):
    def __init__(self, hosts: list[dict[str, Any]] | None = None) -> None:
        self._lock = threading.Lock()
        self._ssh_pool = None
        self._hosts = hosts or [
            {
                "name": "web-01",
                "ssh": "web-01",
                "runspec_paths": ["/srv/ops/bin/runspec"],
                "hooks": ["*"],
            }
        ]
        self.calls: list[tuple[list[str], bytes]] = []
        self.answers: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[str] = []
        self.away = ""

    def get_config(self) -> dict[str, Any]:
        return {}

    def _dispatch(self, name: str, detail: dict[str, Any]) -> None:
        self.events.append((name, detail))

    def _host_sched_call(
        self,
        payload: dict[str, Any],
        argv: list[str],
        *,
        timeout: int = 60,
        data: bytes = b"",
    ) -> dict[str, Any]:
        self.calls.append((argv, data))
        return (
            self.answers.pop(0)
            if self.answers
            else {"ok": True, "error": "", "json": {"ok": True}}
        )

    def _host_sched_audit(
        self, event: str, payload: dict[str, Any], sid: str, **fields: Any
    ) -> None:
        self.audits.append(event)

    def _webhook_away_reason(self) -> str:
        return self.away


def test_targets_and_status() -> None:
    b = FakeBridge()
    assert b._webhook_targets() == [("web-01", "ops", "/srv/ops/bin/runspec", ["*"])]
    st = b.webhooks_status()
    assert st["configured"] and st["follows"] == {"web-01": ["*"]} and st["venvs"] == []
    assert not FakeBridge(
        [{"name": "x", "ssh": "x", "runspec_paths": ["/a/bin/runspec"]}]
    ).webhooks_status()["configured"]


def test_decide_mute_and_unmute_run_the_host_commands() -> None:
    b = FakeBridge()
    b.answers = [
        {
            "ok": True,
            "error": "",
            "json": {"ok": True, "delivery": {"id": 3, "state": "approved"}},
        }
    ]
    out = b.webhook_decide(
        {"host": "web-01", "venv": "ops", "verdict": "approve", "id": 3, "note": "fine"}
    )
    assert (
        out["ok"]
        and b.calls[-1][0] == ["hooks", "approve", "--id", "3", "--note", "fine"]
        and b.audits == ["webhook.approve"]
    )
    assert not b.webhook_decide(
        {"host": "web-01", "venv": "ops", "verdict": "nope", "id": 3}
    )["ok"]
    assert not b.webhook_decide({"venv": "ops", "verdict": "approve", "id": 3})["ok"]
    b.webhook_mute({"host": "web-01", "venv": "ops", "hook": "alerts", "for": "1h"})
    b.webhook_unmute({"host": "web-01", "venv": "ops", "hook": "alerts"})
    assert [c[0][1] for c in b.calls[-2:]] == ["mute", "unmute"]


def test_hook_save_makes_secrets_here_and_sends_them_on_stdin() -> None:
    b = FakeBridge()
    b.answers = [
        {
            "ok": True,
            "error": "",
            "json": {
                "ok": True,
                "hook": {"name": "alerts"},
                "url": "http://h/hook/alerts",
            },
        }
    ]
    out = b.webhook_hook_save(
        {"host": "web-01", "venv": "ops", "hook": {"name": "alerts"}, "new_token": True}
    )
    argv, data = b.calls[-1]
    sent = json.loads(data)
    assert (
        argv == ["hooks", "save"]
        and sent["token"] == out["secrets"]["token"]
        and out["url"].endswith("/alerts")
    )
    # a failed save shows no secret
    b.answers = [
        {"ok": False, "error": "bad hook", "json": {"ok": False, "error": "bad hook"}}
    ]
    out = b.webhook_hook_save(
        {"host": "web-01", "venv": "ops", "hook": {"name": "alerts"}, "new_token": True}
    )
    assert not out["ok"] and "secrets" not in out
    assert not b.webhook_hook_save({"host": "web-01", "venv": "ops", "hook": {}})["ok"]


def test_hook_test_sends_a_draft() -> None:
    b = FakeBridge()
    b.answers = [
        {
            "ok": True,
            "error": "",
            "json": {
                "ok": True,
                "source": "last delivery",
                "items": [{"state": "pending"}],
            },
        }
    ]
    out = b.webhook_hook_test(
        {"host": "web-01", "venv": "ops", "hook": {"name": "alerts"}, "body": "  "}
    )
    argv, data = b.calls[-1]
    assert (
        argv == ["hooks", "test", "--draft"]
        and json.loads(data)["body"] is None
        and out["items"] == [{"state": "pending"}]
    )


def test_dismiss_is_local() -> None:
    b = FakeBridge()
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    b._wh()["venvs"]["web-01/ops"] = vs
    assert (
        b.webhook_dismiss({"host": "web-01", "venv": "ops", "id": 4})["ok"]
        and 4 in vs.dismissed
        and b.calls == []
    )
    b.webhook_dismiss({"host": "web-01", "venv": "ops", "id": 4, "undo": True})
    assert 4 not in vs.dismissed


def test_presence_mutes_while_away_and_unmutes_on_return() -> None:
    b = FakeBridge()
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"], connected=True)
    b._wh()["venvs"]["web-01/ops"] = vs
    b.away = "screen locked"
    b._webhook_presence_tick(now=1000.0)
    assert (
        b.calls[-1][0]
        == [
            "hooks",
            "mute",
            "--hook",
            "*",
            "--for",
            "600s",
            "--reason",
            "screen locked",
        ]
        and vs.paused == "screen locked"
    )
    n = len(b.calls)
    b._webhook_presence_tick(now=1100.0)  # still away, not time to renew
    assert len(b.calls) == n
    b._webhook_presence_tick(now=1000.0 + 301)  # renewed
    assert len(b.calls) == n + 1
    b.away = ""
    b._webhook_presence_tick(now=1500.0)
    assert (
        b.calls[-1][0] == ["hooks", "unmute", "--hook", "*"]
        and vs.paused == ""
        and b._wh()["away"] == ""
    )


def test_arrival_feeds_the_trigger_engine() -> None:
    fed: list[Any] = []

    class Engine:
        def feed(self, event: Any) -> None:
            fed.append(event)

    b = FakeBridge()
    b._trigger_engine = Engine()  # type: ignore[attr-defined]
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    b._on_webhook_arrival(
        vs,
        wh.trigger_payload("web-01", "ops", delivery(5))
        | {"hook": "alerts", "id": 5, "fields": {"alertname": "Disk"}},
    )
    assert (
        fed
        and fed[0].source == "webhook"
        and fed[0].type == "alerts"
        and fed[0].payload["alertname"] == "Disk"
    )


# ── claim-gated webhook triggers (one console runs it) ──────────────────────


class _Inline:
    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        fn(*args, **kwargs)

    def shutdown(self, wait: bool = True, **_: Any) -> None:
        pass


def _claim_setup() -> tuple[Any, Any, list[str], list[Any]]:
    from runspec_console.bridge_triggers import TriggersMixin
    from runspec_console.triggers import Trigger, TriggerEngine

    class ClaimBridge(TriggersMixin, FakeBridge):
        pass

    ran: list[str] = []
    cards: list[Any] = []
    trig = Trigger.from_config(
        {
            "name": "deploys",
            "source": "webhook",
            "event": "deploy",
            "mode": "rule",
            "run": "web-01__deploy",
            "claim": True,
        }
    )
    engine = TriggerEngine(
        [trig],
        handler=lambda card, t, e: ran.append(card.id),
        on_card=cards.append,
        executor=_Inline(),  # type: ignore[arg-type]
        external_sources={"webhook"},
    )
    b = ClaimBridge()
    b._trigger_engine = engine  # type: ignore[attr-defined]
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    b._on_webhook_arrival(
        vs,
        delivery(7, hook="deploy", state="event") | {"fields": {"version": "1.2"}},
    )
    return b, vs, ran, cards


def test_claim_trigger_parks_until_this_console_wins() -> None:
    b, _vs, ran, cards = _claim_setup()
    engine = b._trigger_engine
    [card_id] = engine.parked_claim_ids()
    assert ran == [] and cards[-1].column == "awaiting_claim"
    assert cards[-1].detail["host"] == "web-01" and cards[-1].detail["delivery"] == 7
    b.answers = [
        {
            "ok": True,
            "error": "",
            "json": {
                "ok": True,
                "won": True,
                "delivery": {"id": 7, "claimed_by": "ann"},
            },
        }
    ]
    assert b.claim_card(card_id) == {"claimed": True, "owner": "ann"}
    assert b.calls[-1][0] == ["hooks", "claim", "--id", "7"]
    assert ran == [card_id] and engine.parked_claim_ids() == []
    assert "webhook.claim" in b.audits


def test_claim_lost_says_who_has_it() -> None:
    b, _vs, ran, cards = _claim_setup()
    [card_id] = b._trigger_engine.parked_claim_ids()
    b.answers = [
        {"ok": True, "error": "", "json": {"ok": True, "won": False, "owner": "bob"}}
    ]
    assert b.claim_card(card_id) == {"claimed": False, "owner": "bob"}
    assert ran == [] and cards[-1].column == "done"
    assert cards[-1].detail["claimed_by"] == "bob"


def test_claim_on_an_old_scheduler_explains_and_stays_parked() -> None:
    b, _vs, ran, _cards = _claim_setup()
    [card_id] = b._trigger_engine.parked_claim_ids()
    b.answers = [
        {
            "ok": False,
            "error": "✗  Unknown command for 'runspec-scheduler hooks': 'claim'",
            "json": None,
        }
    ]
    out = b.claim_card(card_id)
    assert not out["claimed"] and "0.13.0" in out["error"]
    assert b._trigger_engine.parked_claim_ids() == [card_id] and ran == []


def test_a_claim_in_the_follow_stream_resolves_other_consoles_cards() -> None:
    b, vs, ran, cards = _claim_setup()
    engine = b._trigger_engine
    [card_id] = engine.parked_claim_ids()
    # another venv's delivery 7 is a different request
    b._on_webhook_claim(wh.VenvHooks("web-02", "ops"), {"id": 7, "claimed_by": "bob"})
    assert engine.parked_claim_ids() == [card_id]
    # this console's own claim in flight is left alone
    b._wh_claiming().add(card_id)
    b._on_webhook_claim(vs, {"id": 7, "claimed_by": "ann"})
    assert engine.parked_claim_ids() == [card_id]
    b._wh_claiming().discard(card_id)
    b._on_webhook_claim(vs, {"id": 7, "claimed_by": "bob"})
    assert engine.parked_claim_ids() == [] and ran == []
    assert cards[-1].detail["note"] == "claimed by bob"


def test_follower_passes_claims_on() -> None:
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    claims: list[tuple[int, str]] = []

    def open_stream(cursor: int | None) -> src.LineStream:
        return src.LineStream(
            FakeChannel(
                [
                    lines(
                        hello(),
                        delivery(1, state="event") | {"claimed_by": "", "event": 12},
                        delivery(1, state="event", kind="claimed", event=13)
                        | {"claimed_by": "bob"},
                    )
                ]
            )
        )

    f = src.WebhookFollower(
        vs,
        open_stream,
        on_change=lambda s: None,
        on_arrival=lambda s, d: None,
        on_claim=lambda s, d: claims.append((d["id"], d["claimed_by"])),
    )
    assert f.run_once() == ""
    assert claims == [(1, "bob")] and vs.recent[0]["claimed_by"] == "bob"


def test_hook_names_for_the_triggers_editor() -> None:
    b = FakeBridge(
        [
            {
                "name": "web-01",
                "ssh": "web-01",
                "runspec_paths": ["/srv/ops/bin/runspec"],
                "hooks": ["alerts", "*"],
            }
        ]
    )
    vs = wh.VenvHooks("web-01", "ops", hooks=["*"])
    vs.hook_list = [{"name": "deploy"}, {"name": "alerts"}]
    b._wh()["venvs"][vs.key] = vs
    assert b._webhook_hook_names() == ["alerts", "deploy"]


def test_agent_sees_a_readable_webhook_request() -> None:
    from runspec_console.events import Event
    from runspec_console.trigger_runner import render_event

    payload = wh.trigger_payload(
        "web-01",
        "ops",
        delivery(9, hook="deploy", state="event")
        | {"fields": {"version": "1.2"}, "body": '{"version": "1.2"}'},
    )
    text = render_event(Event.new("webhook", "deploy", payload))
    assert text.startswith("A webhook request arrived on hook 'deploy' (web-01 ops)")
    assert "- delivery: 9" in text
    assert "nothing ran on the host" in text
    assert "- version: 1.2" in text and 'Body:\n{"version": "1.2"}' in text
    assert "webhook:" not in text and "- venv:" not in text
