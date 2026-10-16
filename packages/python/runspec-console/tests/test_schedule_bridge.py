"""
test_schedule_bridge.py — the Schedules-tab bridge seam (get/update/run-now).

Covers the bits the Schedules UI added on top of create/delete:

  - get_schedules attaches a display-only ``nextRun`` (and never persists it),
  - update_schedule replaces an entry in place keeping its id,
  - run_schedule_now routes a runnable-action schedule to a direct run and an
    event-action schedule to an engine.feed() emit, ignoring ``enabled``.

The methods are bound onto a bare stub (à la test_email_bridge) so we dodge
``Bridge.__init__`` side effects; only the collaborators each method actually
touches are supplied.
"""

from __future__ import annotations

import threading
import types

import pytest

from runspec_console.bridge import Bridge
from runspec_console.bridge_turn import TurnResult
from tests.session_stub import install_sessions
from runspec_console.bridge_utils import _write_schedules


def _stub(tmp_path, monkeypatch, *, engine=None):
    monkeypatch.setattr(
        "runspec_console.config.hosts_path", lambda: tmp_path / "runspec_hosts.toml"
    )
    ns = types.SimpleNamespace()
    for name in (
        "_read_schedules",
        "_backup_corrupt_schedules",
        "get_schedules",
        "_schedule_next_run",
        "create_schedule",
        "update_schedule",
        "delete_schedule",
        "run_schedule_now",
        "_invoke_scheduled",
        "_invoke_fanout",
        "_schedule_title",
        "_invoke_scheduled_agent",
        "_pick_connected_host",
        "_run_scheduled",
        "_run_scheduled_agent",
        "_scheduled_agent_message",
        "_record_schedule_fire",
        "_on_schedule_event",
        "_on_schedule_card",
        "_sync_card_session",
        "get_schedule_board",
        "get_schedule_events",
    ):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns.restart_triggers = lambda: None
    ns._trigger_engine = engine
    ns._lock = threading.Lock()
    install_sessions(ns)  # every schedule card opens a session
    ns._schedule_cards = {}
    ns._schedule_events = []
    ns.dispatched = []
    ns._dispatch = lambda ev, payload: ns.dispatched.append((ev, payload))
    ns.runs = []
    ns.invoke_runnable = lambda host, runnable, args, **kw: (
        ns.runs.append((host, runnable, args, kw)) or "inv-xyz"
    )
    return ns


def _seed(tmp_path, schedules):
    _write_schedules(tmp_path / "runspec_schedules.toml", schedules)


# ── get_schedules: computed, non-persisted nextRun ────────────────────────────


def test_get_schedules_attaches_next_run(tmp_path, monkeypatch):
    pytest.importorskip("apscheduler")
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "h",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
            }
        ],
    )
    out = ns.get_schedules()
    assert out[0]["nextRun"]  # ISO string present for an enabled schedule
    # and it is display-only — the raw read never carries it
    assert "nextRun" not in ns._read_schedules()[0]


def test_get_schedules_disabled_has_no_next_run(tmp_path, monkeypatch):
    pytest.importorskip("apscheduler")
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "h",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
                "enabled": False,
            }
        ],
    )
    assert "nextRun" not in ns.get_schedules()[0]


# ── update_schedule: in-place replace, transient-key stripped ─────────────────


def test_update_schedule_replaces_in_place(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {"id": "a", "action": "event", "event": "e1", "when": {"every": "5m"}},
            {"id": "b", "action": "event", "event": "e2", "when": {"every": "5m"}},
        ],
    )
    ns.update_schedule(
        "a",
        {
            "id": "a",
            "action": "event",
            "event": "e1-renamed",
            "when": {"every": "10m"},
            "nextRun": "2026-01-01T00:00:00",  # must not be persisted
        },
    )
    raw = ns._read_schedules()
    assert [s["id"] for s in raw] == ["a", "b"]  # order + count preserved
    assert raw[0]["event"] == "e1-renamed"
    assert raw[0]["when"]["every"] == "10m"
    assert "nextRun" not in raw[0]  # transient key stripped on write


def test_update_schedule_keeps_id_stable(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [{"id": "a", "action": "event", "event": "e", "when": {"every": "5m"}}],
    )
    # Even if the payload carries a different id, the addressed id wins.
    ns.update_schedule(
        "a", {"id": "WRONG", "action": "event", "event": "e", "when": {"every": "5m"}}
    )
    assert [s["id"] for s in ns._read_schedules()] == ["a"]


# ── _write_schedules: valid TOML for prompts with backslashes/quotes/newlines ──


def test_write_schedules_escapes_prompt_with_backslash(tmp_path, monkeypatch):
    """A prompt-action schedule whose natural-language prompt carries a Windows
    path (``C:\\Users``), a quote, and a newline must round-trip. The pre-fix
    hand-rolled serialiser wrote the ``\\U`` unescaped → "Invalid hex value",
    which corrupted the file and blocked every later schedule create."""
    ns = _stub(tmp_path, monkeypatch)
    prompt = 'Review C:\\Users\\jason and check the "status"\nsecond line'
    _seed(
        tmp_path,
        [
            {
                "id": "remind",
                "action": "prompt",
                "when": {"cron": "0 9 * * 1", "timezone": "America/New_York"},
                "prompt": prompt,
                "args": {"path": "C:\\logs\\app.log"},
            }
        ],
    )
    raw = ns._read_schedules()  # re-parses via tomllib — would raise if invalid
    assert raw[0]["prompt"] == prompt
    assert raw[0]["args"]["path"] == "C:\\logs\\app.log"


def test_read_schedules_recovers_from_corrupt_file(tmp_path, monkeypatch):
    """An already-corrupt file (from the pre-fix serialiser) is backed up and
    read as empty, so the Schedules tab and create tool recover instead of
    failing forever."""
    ns = _stub(tmp_path, monkeypatch)
    path = tmp_path / "runspec_schedules.toml"
    # An unescaped Windows path — exactly what the old serialiser produced.
    path.write_text(
        '[[schedule]]\nid = "x"\nprompt = "C:\\Users\\jason"\n', encoding="utf-8"
    )
    assert ns._read_schedules() == []
    # The raw file is preserved beside it for manual salvage, not deleted.
    backups = list(tmp_path.glob("runspec_schedules.toml.corrupt-*"))
    assert len(backups) == 1
    assert not path.exists()


# ── run_schedule_now: route by action, ignore enabled ─────────────────────────


def test_run_now_runnable_invokes_directly(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "prod-1",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
                "args": {"db": "main"},
                "enabled": False,  # ignored — running now is the authorization
            }
        ],
    )
    res = ns.run_schedule_now("r1")
    assert res == {
        "action": "runnable",
        "inv_id": "inv-xyz",
        "host": "prod-1",
        "runnable": "backup",
    }
    host, runnable, args, kw = ns.runs[0]
    assert (host, runnable, args) == ("prod-1", "backup", {"db": "main"})
    assert kw["operator"] == "Scheduled Task" and kw["trigger"] == "schedule"
    # The scheduled path pre-allocates the inv_id and wires a progress callback so
    # it can build the run card from the invoke lifecycle.
    assert "inv_id" in kw and callable(kw["on_progress"])
    # A run card was emitted to the Schedules inbox, and the fire recorded.
    board = ns.get_schedule_board()
    assert len(board) == 1 and board[0]["source"] == "schedule"
    assert (
        board[0]["column"] == "running" and board[0]["summary"] == "/backup on prod-1"
    )
    assert board[0]["card_id"] == kw["inv_id"]
    # No credentials configured → the invoke gets credential_ids=None (preserving
    # the scope-based auto-apply the scheduled path had before this feature).
    assert kw["credential_ids"] is None
    events = ns.get_schedule_events()
    assert len(events) == 1 and events[0]["matched"] == ["r1"]


def test_run_now_runnable_threads_credential_ids(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "jira-nightly",
                "action": "runnable",
                "host": "prod-1",
                "runnable": "jsm-raise",
                "when": {"cron": "0 2 * * *"},
                "args": {"summary": "nightly"},
                "credential_ids": ["cred-jira"],
            }
        ],
    )
    ns.run_schedule_now("jira-nightly")
    _, runnable, _, kw = ns.runs[0]
    assert runnable == "jsm-raise"
    # An explicit credential selection is threaded to invoke_runnable, which
    # injects the secret from the keychain at fire time.
    assert kw["credential_ids"] == ["cred-jira"]


def test_run_now_runnable_card_finalizes_on_exit(tmp_path, monkeypatch):
    """The progress callback finalizes the card to done/failed from the exit code."""
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "prod-1",
                "runnable": "backup",
                "when": {"cron": "0 2 * * *"},
                "args": {},
            }
        ],
    )
    ns.run_schedule_now("r1")
    _, _, _, kw = ns.runs[0]
    on_progress = kw["on_progress"]
    on_progress("line", {"line": "working…", "stream": "stdout"})
    on_progress("done", {"exit_code": 0, "duration_ms": 5})
    card = ns.get_schedule_board()[0]
    assert card["column"] == "done"
    assert card["detail"]["exit_code"] == 0 and "working" in card["detail"]["result"]
    # A non-zero exit lands as failed with an error.
    on_progress("done", {"exit_code": 2, "duration_ms": 5})
    card = ns.get_schedule_board()[0]
    assert card["column"] == "failed" and card["detail"]["error"]


def test_run_now_event_feeds_engine(tmp_path, monkeypatch):
    fed = []
    engine = types.SimpleNamespace(feed=fed.append)
    ns = _stub(tmp_path, monkeypatch, engine=engine)
    _seed(
        tmp_path,
        [
            {
                "id": "e1",
                "action": "event",
                "event": "nightly",
                "when": {"every": "5m"},
                "fields": {"scope": "prod"},
            }
        ],
    )
    res = ns.run_schedule_now("e1")
    assert res == {"action": "event", "event": "nightly"}
    assert len(fed) == 1
    ev = fed[0]
    assert ev.source == "schedule" and ev.type == "nightly"
    assert ev.payload["scope"] == "prod" and ev.payload["schedule"] == "e1"
    # Store isolation: an event-action fire makes NO schedule run card (it routes
    # through the engine), but it IS recorded in the Schedules event log.
    assert ns.get_schedule_board() == []
    assert len(ns.get_schedule_events()) == 1


def test_run_now_event_without_engine_raises(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch, engine=None)
    _seed(
        tmp_path,
        [{"id": "e1", "action": "event", "event": "n", "when": {"every": "5m"}}],
    )
    with pytest.raises(RuntimeError):
        ns.run_schedule_now("e1")


def test_run_now_unknown_id_raises(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(tmp_path, [])
    with pytest.raises(ValueError):
        ns.run_schedule_now("nope")


# ── prompt / runbook schedules: agent-turn path ─────────────────────────────────


def test_run_now_prompt_routes_to_agent(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    # Capture the agent invoke (the real one spawns a thread + needs an adapter).
    captured = []
    ns._invoke_scheduled_agent = lambda job: captured.append(job) or "card-1"
    _seed(
        tmp_path,
        [
            {
                "id": "p1",
                "action": "prompt",
                "prompt": "Check disk usage and flag anything over 80%.",
                "when": {"cron": "0 8 * * *"},
                "enabled": False,  # ignored — running now is the authorization
            }
        ],
    )
    res = ns.run_schedule_now("p1")
    assert res["action"] == "prompt" and res["inv_id"] == "card-1"
    assert captured[0].prompt.startswith("Check disk usage")
    # The fire is recorded in the Schedules event log.
    events = ns.get_schedule_events()
    assert len(events) == 1 and events[0]["type"] == "prompt"


def test_run_now_runbook_routes_to_agent(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    captured = []
    ns._invoke_scheduled_agent = lambda job: captured.append(job) or "card-2"
    _seed(
        tmp_path,
        [
            {
                "id": "s1",
                "action": "runbook",
                "runbook": "build-report",
                "values": {"scope": "prod"},
                "when": {"every": "1h"},
            }
        ],
    )
    res = ns.run_schedule_now("s1")
    assert res["action"] == "runbook" and res["inv_id"] == "card-2"
    assert captured[0].runbook == "build-report"
    assert ns.get_schedule_events()[0]["type"] == "runbook"


def test_pick_connected_host_prefers_connected(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    ns._connected_cache = {"web01": False, "web02": True}
    ns._host_entry = lambda name: {"name": name, "ssh": "deploy@" + name}
    assert ns._pick_connected_host(["web01", "web02"]) == "web02"


def test_pick_connected_host_falls_back_to_first(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    ns._connected_cache = {"web01": False, "web02": False}
    ns._host_entry = lambda name: {"name": name, "ssh": "deploy@" + name}
    # None connected → fall back to the first candidate (still attempt it).
    assert ns._pick_connected_host(["web01", "web02"]) == "web01"


def test_run_now_hosts_list_runs_on_first_connected(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    ns._connected_cache = {"web01": False, "web02": True}
    ns._host_entry = lambda name: {"name": name, "ssh": "deploy@" + name}
    _seed(
        tmp_path,
        [
            {
                "id": "h1",
                "action": "runnable",
                "hosts": ["web01", "web02"],
                "runnable": "backup",
                "when": {"every": "5m"},
            }
        ],
    )
    res = ns.run_schedule_now("h1")
    assert res["action"] == "runnable" and res["host"] == "any:web01,web02"
    # The actual run targeted the first connected candidate.
    host, runnable, _, _ = ns.runs[0]
    assert (host, runnable) == ("web02", "backup")


def test_run_now_selected_hosts_fans_out_to_every_host(tmp_path, monkeypatch):
    """selected_hosts is fan-out (not failover): every host in the list runs."""
    ns = _stub(tmp_path, monkeypatch)
    ns._host_has_runnable = lambda host, runnable: True
    _seed(
        tmp_path,
        [
            {
                "id": "s1",
                "action": "runnable",
                "selected_hosts": ["web01", "web02", "web03"],
                "runnable": "backup",
                "when": {"every": "5m"},
                "args": {"db": "main"},
            }
        ],
    )
    res = ns.run_schedule_now("s1")
    assert res["action"] == "runnable"
    assert res["host"] == "selected:web01,web02,web03"
    # Every ticked host got its own invoke.
    hosts_run = [row[0] for row in ns.runs]
    assert hosts_run == ["web01", "web02", "web03"]
    # Each got the same args + Scheduled Task metadata.
    for host, runnable, args, kw in ns.runs:
        assert (runnable, args) == ("backup", {"db": "main"})
        assert kw["operator"] == "Scheduled Task" and kw["trigger"] == "schedule"


def test_run_now_selected_hosts_skips_hosts_without_runnable(tmp_path, monkeypatch):
    """A ticked host that doesn't carry the runnable is silently skipped."""
    ns = _stub(tmp_path, monkeypatch)
    ns._host_has_runnable = lambda host, runnable: host != "web02"
    _seed(
        tmp_path,
        [
            {
                "id": "s1",
                "action": "runnable",
                "selected_hosts": ["web01", "web02", "web03"],
                "runnable": "backup",
                "when": {"every": "5m"},
            }
        ],
    )
    ns.run_schedule_now("s1")
    hosts_run = [row[0] for row in ns.runs]
    assert hosts_run == ["web01", "web03"]


def test_scheduled_agent_message_prompt(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    job = types.SimpleNamespace(action="prompt", prompt="  Do the thing.  ")
    assert ns._scheduled_agent_message(job) == "Do the thing."


def test_scheduled_agent_message_runbook_renders_body(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "runspec_console.memory.load_runbook",
        lambda sid: {"id": sid, "title": "Build report", "steps": "Run {{scope}}"},
    )
    monkeypatch.setattr(
        "runspec_console.runbookargs.validate_runbook_values", lambda raw, vals: []
    )
    monkeypatch.setattr(
        "runspec_console.memory.render_runbook_body",
        lambda steps, vals: steps.replace("{{scope}}", vals.get("scope", "")),
    )
    job = types.SimpleNamespace(
        action="runbook", runbook="build-report", values={"scope": "prod"}, id="s1"
    )
    msg = ns._scheduled_agent_message(job)
    assert "Build report" in msg and "Run prod" in msg


def test_scheduled_agent_message_runbook_missing_is_empty(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    monkeypatch.setattr("runspec_console.memory.load_runbook", lambda sid: None)
    job = types.SimpleNamespace(action="runbook", runbook="nope", values={}, id="s1")
    assert ns._scheduled_agent_message(job) == ""


def _agent_turn_stub():
    """Bare bridge exposing just what `_trigger_run_agent_turn` touches, with the
    isolated turn stubbed to capture the workspace id registered for the run."""
    ns = types.SimpleNamespace()
    ns._trigger_run_agent_turn = types.MethodType(Bridge._trigger_run_agent_turn, ns)
    ns._now_context = lambda: "NOW"
    ns._workspace_guidance = lambda: ""
    # dedup mode → no catalogue index in these workspace tests.
    ns._tool_mode = lambda: "dedup"
    ns._defer_builtins_enabled = lambda: True
    ns._chat_index_text = lambda **k: ""
    ns._catalog_system_section = Bridge._catalog_system_section  # staticmethod
    ns.captured_system = {}

    def get_adapter(system_override=None):
        ns.captured_system["last"] = system_override
        return object()

    ns._get_adapter = get_adapter
    ns._lock = threading.Lock()
    ns._chat_cancels = {}
    install_sessions(ns)
    ns._runnables_to_tools = lambda *a, **k: []
    ns._filter_headless_tools = Bridge._filter_headless_tools  # staticmethod
    ns._set_run_origin = lambda *a, **k: None
    ns._clear_run_origin = lambda *a, **k: None
    ns.captured = {}

    async def fake_turn(spec):
        # The turn runs under the card's session — its workspace id is what
        # the sweep keys on.
        ns.captured[spec.chat_id] = spec.session.workspace_id
        return TurnResult(chat_id=spec.chat_id, summary="ok")

    ns._run_agent_turn = fake_turn
    return ns


def test_schedule_agent_turn_uses_schedule_workspace_prefix():
    # A schedule-sourced agent turn names its workspace `schedule-<id>` (audit
    # #9) so the sweep can tell it from a reactive `trigger-<id>` turn.
    ns = _agent_turn_stub()
    assert (
        ns._trigger_run_agent_turn(
            "card-9", "SYS", "go", draft=False, workspace_prefix="schedule"
        )
        == "ok"
    )
    assert ns.captured["card-9"] == "schedule-card-9"
    # A reactive turn keeps the default trigger- prefix.
    ns._trigger_run_agent_turn("card-10", "SYS", "go", draft=False)
    assert ns.captured["card-10"] == "trigger-card-10"


def test_headless_search_turn_folds_catalogue_into_system():
    # An unrestricted (no allowlist) search-mode headless turn composes the
    # catalogue index into the system prompt as a trailing section — not the
    # messages — so a gateway caching only the system block caches it.
    ns = _agent_turn_stub()
    ns._tool_mode = lambda: "search"
    ns._chat_index_text = lambda **k: "- restart-service — Restart a unit."
    ns._trigger_run_agent_turn("card-11", "SYS", "go", draft=False)
    system = ns.captured_system["last"]
    assert system.startswith("NOW\n\nSYS")  # base composed first
    assert "What you can reach right now, by category:" in system
    assert "- restart-service — Restart a unit." in system


def test_headless_allow_scoped_turn_gets_no_catalogue_index():
    # An allow-scoped turn falls back to dedup (full schemas inline), so the
    # catalogue index is not folded even in search mode.
    ns = _agent_turn_stub()
    ns._tool_mode = lambda: "search"
    ns._chat_index_text = lambda **k: "- restart-service — Restart a unit."
    ns._trigger_run_agent_turn(
        "card-12", "SYS", "go", draft=False, allowed_runnables=["restart-service"]
    )
    system = ns.captured_system["last"]
    assert "What you can reach right now, by category:" not in system
    assert system == "NOW\n\nSYS"


# ── run_schedule_now: saved_run action ───────────────────────────────────────


def test_run_now_saved_run_fires_preset(tmp_path, monkeypatch):
    from runspec_console import savedruns

    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path",
        lambda: tmp_path / "runspec_saved_runs.toml",
    )
    savedruns.save_saved_runs(
        [
            {
                "id": "restart-web",
                "runnable": "svc-restart",
                "host": "prod-1",
                "args": {"service": "nginx"},
                "params": {"reason": {"required": True}},
            }
        ]
    )
    ns = _stub(tmp_path, monkeypatch)
    for name in (
        "_run_scheduled_saved",
        "_invoke_scheduled_saved",
        "_saved_run_entry",
        "_saved_run_selected_hosts",
    ):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._host_entry = lambda h: {"name": h, "ssh": "user@" + h}
    _seed(
        tmp_path,
        [
            {
                "id": "s1",
                "action": "saved_run",
                "saved_run": "restart-web",
                "values": {"reason": "nightly"},
                "when": {"cron": "0 2 * * *"},
                "enabled": False,  # ignored — running now is the authorization
            }
        ],
    )
    res = ns.run_schedule_now("s1")
    assert res["action"] == "saved_run"
    assert res["inv_id"] == "inv-xyz"
    host, runnable, args, kw = ns.runs[0]
    assert (host, runnable) == ("prod-1", "svc-restart")
    assert args == {"service": "nginx", "reason": "nightly"}
    assert kw["operator"] == "Scheduled Task"
    assert kw["trigger"] == "schedule"
    # …and the fire is recorded in the Schedules event log.
    assert any("restart-web" in e.get("summary", "") for e in ns._schedule_events)


def test_run_now_saved_run_invalid_values_skips(tmp_path, monkeypatch):
    from runspec_console import savedruns

    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path",
        lambda: tmp_path / "runspec_saved_runs.toml",
    )
    savedruns.save_saved_runs(
        [
            {
                "id": "restart-web",
                "runnable": "svc-restart",
                "host": "prod-1",
                "params": {"reason": {"required": True}},
            }
        ]
    )
    ns = _stub(tmp_path, monkeypatch)
    for name in (
        "_run_scheduled_saved",
        "_invoke_scheduled_saved",
        "_saved_run_entry",
        "_saved_run_selected_hosts",
    ):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    _seed(
        tmp_path,
        [
            {
                "id": "s1",
                "action": "saved_run",
                "saved_run": "restart-web",
                "when": {"cron": "0 2 * * *"},
                # required param 'reason' missing from values
            }
        ],
    )
    res = ns.run_schedule_now("s1")
    assert res["inv_id"] == ""  # logged + skipped, nothing invoked
    assert ns.runs == []


# ── run_schedule_now: subcommand path threading ──────────────────────────────


def test_run_now_runnable_threads_command_path(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _seed(
        tmp_path,
        [
            {
                "id": "r1",
                "action": "runnable",
                "host": "prod-1",
                "runnable": "backup",
                "command": ["db", "restore"],
                "when": {"cron": "0 2 * * *"},
            }
        ],
    )
    res = ns.run_schedule_now("r1")
    assert res["action"] == "runnable"
    _host, _runnable, _args, kw = ns.runs[0]
    assert kw["command_path"] == ["db", "restore"]


def test_run_scheduled_timer_threads_command(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    # The timer path: ScheduleSource's RunFn callback signature includes the
    # command list as the trailing arg.
    ns._run_scheduled(
        "r1", "prod-1", "backup", {"db": "main"}, "", [], [], [], ["db", "restore"]
    )
    _host, _runnable, _args, kw = ns.runs[0]
    assert kw["command_path"] == ["db", "restore"]
