"""Per-trigger / per-schedule model choice, the [llm] utility_model for
background summaries, and the Haiku 4.x thinking default.

A trigger/schedule ``model`` swaps only the model id on a one-off adapter (same
provider, key and settings; never cached as the shared chat adapter). Unset, the
calls are exactly as before — the runner doesn't even pass ``model``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from runspec_console.adapters.base import (
    anthropic_thinking_base,
    capabilities_from_limits,
)
from runspec_console.bridge import Bridge
from runspec_console.sources.schedule import parse_schedule_entry
from runspec_console.trigger_runner import make_handler
from runspec_console.triggers import Event, Trigger

from tests.test_trigger_runner import FakeOps, _card

LOAD = "runspec_console.adapters.base.load_adapter"


class _Stub:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self._adapter = None
        self._cfg = cfg

    def get_config(self) -> dict[str, Any]:
        return self._cfg

    def _ensure_plugins_imported(self, cfg: dict[str, Any]) -> None:
        return None

    _get_adapter = Bridge._get_adapter
    _utility_adapter = Bridge._utility_adapter
    _apply_capabilities = staticmethod(Bridge._apply_capabilities)


def _cfg(**llm: Any) -> dict[str, Any]:
    return {
        "llm": {
            "provider": "anthropic",
            "base_url": "https://x",
            "model": "claude-sonnet-5",
            **llm,
        }
    }


def test_model_override_swaps_the_model_and_is_not_cached():
    stub = _Stub(_cfg())
    with patch(LOAD, return_value="ADAPTER") as load:
        stub._get_adapter(system_override="S", model_override="claude-haiku-4-5")
    assert load.call_args.kwargs["model"] == "claude-haiku-4-5"
    assert load.call_args.kwargs["system"] == "S"
    assert stub._adapter is None


def test_model_override_alone_is_one_off():
    stub = _Stub(_cfg())
    with patch(LOAD, return_value="ADAPTER"):
        stub._get_adapter(model_override="claude-haiku-4-5")
    assert stub._adapter is None  # the chat adapter cache is untouched


def test_utility_model_used_for_background_calls():
    with patch(LOAD, return_value="ADAPTER") as load:
        _Stub(_cfg(utility_model="claude-haiku-4-5"))._utility_adapter()
    assert load.call_args.kwargs["model"] == "claude-haiku-4-5"
    assert "utility_model" not in load.call_args.kwargs


def test_utility_adapter_falls_back_to_the_chat_model():
    stub = _Stub(_cfg())
    with patch(LOAD, return_value="ADAPTER") as load:
        stub._utility_adapter()
    assert load.call_args.kwargs["model"] == "claude-sonnet-5"
    assert stub._adapter == "ADAPTER"  # the ordinary cached chat adapter


def test_trigger_parses_and_saves_model():
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "model": " claude-haiku-4-5 ",
        }
    )
    assert t.model == "claude-haiku-4-5"
    saved = Bridge._clean_trigger(
        {"source": "outlook", "event": "new_mail", "model": "claude-haiku-4-5"}, "t"
    )
    assert saved["model"] == "claude-haiku-4-5"


class _ModelOps(FakeOps):
    def run_agent_turn(self, *args: Any, model: str = "", **kw: Any) -> str:
        self.model_seen = model
        return super().run_agent_turn(*args, **kw)

    def run_pre_turn(
        self, chat_id: str, instructions: str, event_text: str, model: str = ""
    ) -> dict[str, Any]:
        self.pre_model_seen = model
        return super().run_pre_turn(chat_id, instructions, event_text)


def test_agent_trigger_passes_its_model():
    ops = _ModelOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "rules": "x",
            "model": "claude-haiku-4-5",
        }
    )
    card, _ = _card()
    make_handler(ops, base_system="B")(
        card, t, Event.new("outlook", "new_mail", {"subject": "s"})
    )
    assert ops.model_seen == "claude-haiku-4-5"


def test_rule_pre_turn_passes_its_model():
    ops = _ModelOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "mode": "rule",
            "run": "local__echo",
            "pre_turn": "extract",
            "model": "claude-haiku-4-5",
        }
    )
    card, _ = _card()
    make_handler(ops, base_system="B")(
        card, t, Event.new("outlook", "new_mail", {"subject": "s"})
    )
    assert ops.pre_model_seen == "claude-haiku-4-5"


def test_trigger_without_model_does_not_pass_one():
    # FakeOps.run_agent_turn has no `model` param: an unset model must not send it.
    ops = FakeOps()
    t = Trigger.from_config(
        {"name": "t", "source": "outlook", "event": "new_mail", "rules": "x"}
    )
    card, _ = _card()
    make_handler(ops, base_system="B")(
        card, t, Event.new("outlook", "new_mail", {"subject": "s"})
    )
    assert len(ops.agent_calls) == 1


def test_schedule_parses_model():
    job = parse_schedule_entry(
        {
            "id": "s",
            "action": "prompt",
            "prompt": "check disks",
            "when": {"every": "1h"},
            "model": "claude-haiku-4-5",
        }
    )
    assert job.model == "claude-haiku-4-5"
    assert (
        parse_schedule_entry(
            {"id": "s", "action": "prompt", "prompt": "p", "when": {"every": "1h"}}
        ).model
        == ""
    )


def test_haiku_4_gets_no_adaptive_thinking():
    for model in ("claude-haiku-4-5", "anthropic.claude-haiku-4-5"):
        caps = capabilities_from_limits(model, {}, base=anthropic_thinking_base(model))
        assert caps.adaptive_thinking is False
    # An operator entry can still switch it back on.
    caps = capabilities_from_limits(
        "claude-haiku-4-5",
        {"haiku": {"adaptive_thinking": True}},
        base=anthropic_thinking_base("claude-haiku-4-5"),
    )
    assert caps.adaptive_thinking is True


def test_haiku_effort_is_dropped_by_capabilities():
    from runspec_console.adapters.anthropic import AnthropicAdapter

    with patch("runspec_console.adapters.anthropic.anthropic.AsyncAnthropic"):
        adapter = AnthropicAdapter(
            api_key="k", model="claude-haiku-4-5", effort="medium"
        )
    adapter._model_info_cache = None  # no Models API lookup
    Bridge._apply_capabilities(adapter)
    assert adapter._effort == ""


def test_runbook_schedule_uses_the_runbook_model_unless_the_schedule_sets_one(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from runspec_console import memory

    memory.save_runbook("fix", title="Fix", steps="x", model="claude-opus-5-5")
    base = {"id": "s", "action": "runbook", "runbook": "fix", "when": {"every": "1h"}}
    assert (
        Bridge._scheduled_agent_model(parse_schedule_entry(base)) == "claude-opus-5-5"
    )
    own = parse_schedule_entry({**base, "model": "claude-haiku-4-5"})
    assert Bridge._scheduled_agent_model(own) == "claude-haiku-4-5"
    prompt = parse_schedule_entry(
        {"id": "p", "action": "prompt", "prompt": "x", "when": {"every": "1h"}}
    )
    assert Bridge._scheduled_agent_model(prompt) == ""
