"""
test_bridge_evals.py — the EvalsMixin wiring (``bridge_evals.py``).

The scoring core is exercised in test_evals; this pins the *bridge* half: that
run_evals builds the console's real toolset, drives the configured adapter,
scores with the tool-mode-aware injected callables, persists a report, and that
fixture/report CRUD round-trips. The model is a scripted ReplayAdapter (no
network) injected via _get_adapter, and reports/fixtures are redirected to a
tmp dir so nothing touches the real app dir.
"""

from __future__ import annotations

import pytest

from runspec_console import config as config_mod
from runspec_console import configmerge, gitsync
from runspec_console.adapters.replay import ReplayAdapter
from runspec_console.bridge import Bridge


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    b = Bridge()
    # Redirect the machine-local stores to the tmp dir.
    monkeypatch.setattr(config_mod, "eval_reports_dir", lambda: tmp_path / "reports")
    monkeypatch.setattr(
        config_mod, "evals_path", lambda: tmp_path / "runspec_evals.toml"
    )
    # A fixed dedup toolset + a couple of visible runnables, so the injections
    # (dedup: group__name → last segment; host stripped) are exercised.
    monkeypatch.setattr(b, "_tool_mode", lambda: "dedup")
    monkeypatch.setattr(
        b,
        "_visible_runnables",
        lambda surface: [
            {"name": "restart-service", "host": "web-01"},
            {"name": "disk-usage", "host": "web-01"},
        ],
    )
    monkeypatch.setattr(
        b, "_runnables_to_tools", lambda *a, **k: [{"name": "ops__restart-service"}]
    )
    monkeypatch.setattr(b, "get_config", lambda: {"llm": {"provider": "replay"}})
    return b


def _fixture_toml() -> str:
    return """
[[eval]]
id = "restart"
prompt = "restart nginx on web-01"
expect_tool = "restart-service"
expect_host = "web-01"
expect_args = { service = "nginx" }

[[eval]]
id = "ambiguous"
prompt = "delete it"
expect_no_run = true
"""


def _script():
    # dedup tool name is "ops__restart-service"; host is a top-level arg.
    return {
        "restart": [
            {
                "tool_calls": [
                    {
                        "name": "ops__restart-service",
                        "input": {"service": "nginx", "host": "web-01"},
                    }
                ]
            }
        ],
        "ambiguous": [{"text": "Which host did you mean?", "tool_calls": []}],
    }


def test_run_evals_scores_against_the_real_toolset(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(_script())
    )
    report = bridge.run_evals(fixture_toml=_fixture_toml())
    assert report["ok"] is True
    assert report["tool_mode"] == "dedup"
    assert report["summary"]["total"] == 2
    assert report["summary"]["passed"] == 2  # dedup target resolves, host stripped
    cases = {c["case_id"]: c for c in report["cases"]}
    assert cases["restart"]["target"] == "restart-service"
    assert cases["ambiguous"]["passed"] is True


def test_run_evals_flags_a_wrong_tool(bridge, monkeypatch):
    script = {
        "restart": [
            {"tool_calls": [{"name": "ops__stop-service", "input": {"host": "web-01"}}]}
        ],
        "ambiguous": [{"text": "which?", "tool_calls": []}],
    }
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(script))
    report = bridge.run_evals(fixture_toml=_fixture_toml())
    cases = {c["case_id"]: c for c in report["cases"]}
    assert cases["restart"]["passed"] is False
    assert cases["restart"]["category"] == "wrong_tool"
    assert report["summary"]["by_category"] == {"wrong_tool": 1}


def test_search_mode_resolves_run_runnable_id_not_the_wrapper(bridge, monkeypatch):
    # Regression: in search mode the model calls run_runnable(id=…), and the eval
    # must resolve the target to that id — not the wrapper name "run_runnable"
    # (which made every search-mode case read as wrong_tool).
    monkeypatch.setattr(bridge, "_tool_mode", lambda: "search")
    script = {
        "restart": [
            {
                "tool_calls": [
                    {
                        "name": "run_runnable",
                        "input": {
                            "id": "restart-service",
                            "host": "web-01",
                            "args": {"service": "nginx"},
                        },
                    }
                ]
            }
        ],
        "ambiguous": [{"text": "Which host?", "tool_calls": []}],
    }
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(script))
    report = bridge.run_evals(fixture_toml=_fixture_toml())
    cases = {c["case_id"]: c for c in report["cases"]}
    assert cases["restart"]["target"] == "restart-service"
    assert cases["restart"]["passed"] is True  # tool + args + host all match
    assert report["summary"]["passed"] == 2


def test_subcommand_is_captured_and_scored(bridge, monkeypatch):
    # A runnable with subcommands rides its chosen verb in a top-level `command`
    # field (never inside `args`). The eval must resolve + grade it, and drop it
    # from the arg-subset check.
    fixture = """
[[eval]]
id = "cron-add"
prompt = "add a nightly backup cron on web-01"
expect_tool = "cron"
expect_command = "add"
expect_host = "web-01"
expect_args = { schedule = "0 2 * * *" }
"""
    script = {
        "cron-add": [
            {
                "tool_calls": [
                    {
                        "name": "ops__cron",
                        "input": {
                            "command": "add",
                            "schedule": "0 2 * * *",
                            "host": "web-01",
                        },
                    }
                ]
            }
        ],
    }
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(script))
    report = bridge.run_evals(fixture_toml=fixture)
    c = report["cases"][0]
    assert c["target"] == "cron"
    assert c["command"] == "add"  # captured + surfaced on the result
    assert c["passed"] is True  # tool + subcommand + host + args all match


def test_wrong_subcommand_is_flagged(bridge, monkeypatch):
    fixture = """
[[eval]]
id = "cron-add"
prompt = "add a nightly backup cron on web-01"
expect_tool = "cron"
expect_command = "add"
"""
    script = {
        "cron-add": [
            {"tool_calls": [{"name": "ops__cron", "input": {"command": "remove"}}]}
        ],
    }
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(script))
    report = bridge.run_evals(fixture_toml=fixture)
    c = report["cases"][0]
    assert c["passed"] is False
    assert c["category"] == "wrong_command"
    assert c["command"] == "remove"


def test_autonomy_is_proven_without_executing(bridge, monkeypatch):
    # The eval never runs the call — but it resolves, via the SAME code the live
    # gate uses (_effective_tool_autonomy), the level the call WOULD gate at, so a
    # case can assert "a confirm would be raised" deterministically.
    fixture = """
[[eval]]
id = "restart"
prompt = "restart nginx on web-01"
expect_tool = "restart-service"
expect_host = "web-01"
expect_args = { service = "nginx" }
expect_autonomy = "confirm"
"""
    seen: dict[str, object] = {}

    def fake_autonomy(name, ti):
        seen["name"] = name
        seen["input"] = ti
        return "confirm"

    monkeypatch.setattr(bridge, "_effective_tool_autonomy", fake_autonomy)
    monkeypatch.setattr(
        bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(_script())
    )
    report = bridge.run_evals(fixture_toml=fixture)
    c = report["cases"][0]
    assert c["autonomy"] == "confirm"
    assert c["passed"] is True
    # The gate was asked about the model's REAL captured call, not the wrapper.
    assert seen["name"] == "ops__restart-service"
    assert seen["input"]["service"] == "nginx"


def test_manual_autonomy_expectation_flags_a_mismatch(bridge, monkeypatch):
    # expect_autonomy = "manual" proves the agent would be refused; if the call
    # actually resolves to "confirm", that's a wrong_autonomy failure.
    fixture = """
[[eval]]
id = "restart"
prompt = "restart nginx on web-01"
expect_tool = "restart-service"
expect_host = "web-01"
expect_args = { service = "nginx" }
expect_autonomy = "manual"
"""
    monkeypatch.setattr(bridge, "_effective_tool_autonomy", lambda name, ti: "confirm")
    monkeypatch.setattr(
        bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(_script())
    )
    report = bridge.run_evals(fixture_toml=fixture)
    c = report["cases"][0]
    assert c["passed"] is False
    assert c["category"] == "wrong_autonomy"
    assert c["autonomy"] == "confirm"


def test_search_mode_strips_group_host_prefix_from_run_runnable_id(bridge, monkeypatch):
    # Regression: search_runnables returns each row's id as the *dedup tool name*
    # (`group__name` / `host__name`), so the model calls run_runnable(id=
    # "local__restart-service"). The eval must resolve that to the bare runnable
    # name — otherwise it never matches expect_tool ("restart-service").
    monkeypatch.setattr(bridge, "_tool_mode", lambda: "search")
    script = {
        "restart": [
            {
                "tool_calls": [
                    {
                        "name": "run_runnable",
                        "input": {
                            "id": "local__restart-service",
                            "host": "web-01",
                            "args": {"service": "nginx"},
                        },
                    }
                ]
            }
        ],
        "ambiguous": [{"text": "Which host?", "tool_calls": []}],
    }
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(script))
    report = bridge.run_evals(fixture_toml=_fixture_toml())
    cases = {c["case_id"]: c for c in report["cases"]}
    assert cases["restart"]["target"] == "restart-service"  # prefix stripped
    assert cases["restart"]["passed"] is True
    assert report["summary"]["passed"] == 2


def test_run_evals_persists_a_report_that_lists_and_loads(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(_script())
    )
    report = bridge.run_evals(fixture_toml=_fixture_toml())
    listed = bridge.list_eval_reports()
    assert [r["id"] for r in listed] == [report["id"]]
    loaded = bridge.load_eval_report(report["id"])
    assert loaded["summary"]["passed"] == 2
    assert bridge.delete_eval_report(report["id"]) is True
    assert bridge.list_eval_reports() == []


def test_run_evals_needs_a_provider(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: None)
    out = bridge.run_evals(fixture_toml=_fixture_toml())
    assert out["ok"] is False and "provider" in out["reason"]


def test_run_evals_rejects_bad_toml(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter({}))
    out = bridge.run_evals(fixture_toml="this is = = not toml")
    assert out["ok"] is False and "TOML" in out["reason"]


def test_validate_option_buckets_a_bounce(bridge, monkeypatch):
    # The model omits a required arg; a stubbed preflight flags it, and the
    # verdict buckets via improve.classify_bounce.
    script = {
        "restart": [
            {
                "tool_calls": [
                    {"name": "ops__restart-service", "input": {"host": "web-01"}}
                ]
            }
        ],
        "ambiguous": [{"text": "which?", "tool_calls": []}],
    }
    monkeypatch.setattr(bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(script))
    monkeypatch.setattr(
        bridge,
        "_preflight_validate",
        lambda cached, path, args: (
            None
            if "service" in args
            else "Error: missing required argument(s): service."
        ),
    )
    report = bridge.run_evals(fixture_toml=_fixture_toml(), validate=True)
    cases = {c["case_id"]: c for c in report["cases"]}
    # expect_args in the fixture already requires `service`, so wrong_args fires
    # first here; assert the run completes and flags the restart case.
    assert cases["restart"]["passed"] is False


# ── fixture CRUD ──────────────────────────────────────────────────────────────
def test_fixture_crud_round_trip(bridge):
    res = bridge.save_eval_fixture("linux", _fixture_toml())
    assert res == {"ok": True, "cases": 2}
    assert bridge.list_eval_fixtures() == [{"id": "linux", "cases": 2}]
    assert "[[eval]]" in bridge.get_eval_fixture_text("linux")
    assert len(bridge.get_eval_fixture("linux")["eval"]) == 2
    assert bridge.delete_eval_fixture("linux") is True
    assert bridge.list_eval_fixtures() == []


def test_save_fixture_rejects_bad_toml(bridge):
    res = bridge.save_eval_fixture("bad", "nope = = =")
    assert res["ok"] is False and "TOML" in res["reason"]


def test_run_evals_by_stored_fixture_id(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_get_adapter", lambda *a, **k: ReplayAdapter(_script())
    )
    bridge.save_eval_fixture("linux", _fixture_toml())
    report = bridge.run_evals(fixture_id="linux")
    assert report["ok"] is True and report["summary"]["total"] == 2


def test_fixtures_are_stored_as_config_sync_suites(bridge, tmp_path):
    # Saved suites land in one runspec_evals.toml as [[suite]] entries, and two
    # different suites coexist in the single synced file.
    bridge.save_eval_fixture("linux", _fixture_toml())
    bridge.save_eval_fixture("web", _fixture_toml())
    text = (tmp_path / "runspec_evals.toml").read_text()
    assert "[[suite]]" in text
    import tomllib

    data = tomllib.loads(text)
    assert sorted(s["name"] for s in data["suite"]) == ["linux", "web"]
    assert all("eval" in s for s in data["suite"])


def test_evals_file_is_registered_for_config_sync():
    # The store rides Config Sync, merged by suite name (a repo suite replaces the
    # local one of that name; local-only suites survive).
    assert "runspec_evals.toml" in gitsync.KNOWN_FILES
    assert configmerge.COLLECTION_FILES["runspec_evals.toml"] == ("suite", "name")
    assert config_mod.evals_path().name == "runspec_evals.toml"


# ── scheduling an eval sweep (action="eval") ──────────────────────────────────
def test_schedule_action_eval_parses_and_dispatches():
    from runspec_console.sources.schedule import ScheduleSource, parse_schedule_entry

    raw = {
        "id": "nightly",
        "action": "eval",
        "when": {"cron": "0 6 * * *"},
        "eval_fixture": "linux",
        "eval_samples": 3,
        "eval_validate": True,
    }
    job = parse_schedule_entry(raw)
    assert job.action == "eval" and job.eval_fixture == "linux"
    assert job.eval_samples == 3 and job.eval_validate is True

    fired = {}
    src = ScheduleSource(
        [raw], run_eval=lambda j: fired.setdefault("id", j.eval_fixture)
    )
    src._fire("nightly")
    assert fired == {"id": "linux"}


def test_schedule_action_eval_requires_a_fixture():
    from runspec_console.sources.schedule import parse_schedule_entry

    with pytest.raises(ValueError, match="eval_fixture"):
        parse_schedule_entry(
            {"id": "x", "action": "eval", "when": {"cron": "0 6 * * *"}}
        )


def test_create_schedule_action_tool_exposes_eval(bridge):
    # The agent's create_schedule tool offers action='eval' with an eval_fixture
    # enum drawn from the stored suites.
    bridge.save_eval_fixture("linux", _fixture_toml())
    tools = {t["name"]: t for t in bridge._action_tools()}
    sched = tools["create_schedule"]["input_schema"]["properties"]
    assert "eval" in sched["action"]["enum"]
    assert sched["eval_fixture"]["enum"] == ["linux"]
    assert "eval_samples" in sched and "eval_validate" in sched


def test_create_schedule_action_tool_persists_an_eval_schedule(bridge, monkeypatch):
    bridge.save_eval_fixture("linux", _fixture_toml())
    saved = {}
    monkeypatch.setattr(bridge, "create_schedule", lambda data: saved.update(data))
    out = bridge._action_create_schedule(
        {
            "id": "nightly-evals",
            "action": "eval",
            "when": {"cron": "0 6 * * *"},
            "eval_fixture": "linux",
            "eval_samples": 3,
            "eval_validate": True,
        }
    )
    assert out.startswith("✓")
    assert saved["action"] == "eval" and saved["eval_fixture"] == "linux"
    assert saved["eval_samples"] == 3 and saved["eval_validate"] is True


def test_run_scheduled_eval_drives_run_evals(bridge, monkeypatch):
    from types import SimpleNamespace

    calls = {}

    def fake_run_evals(
        fixture_toml="", fixture_id="", samples=1, validate=False, telemetry=False
    ):
        calls.update(
            fixture_id=fixture_id,
            samples=samples,
            validate=validate,
            telemetry=telemetry,
        )
        return {"ok": True, "summary": {"total": 2, "passed": 2, "pass_rate": 1.0}}

    monkeypatch.setattr(bridge, "run_evals", fake_run_evals)
    job = SimpleNamespace(
        eval_fixture="linux", eval_samples=5, eval_validate=True, eval_telemetry=False
    )
    out = bridge._run_scheduled_eval(job)
    assert out["ok"] is True and out["summary"]["passed"] == 2
    assert calls == {
        "fixture_id": "linux",
        "samples": 5,
        "validate": True,
        "telemetry": False,
    }
