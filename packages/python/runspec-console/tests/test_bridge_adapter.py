"""
test_bridge_adapter.py — Bridge._get_adapter threading of the new LLM config keys.

Binds the real _get_adapter onto a light stub so we exercise the method without
Bridge.__init__'s filesystem side effects. load_adapter is patched to a sentinel.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from runspec_console.adapters import base
from runspec_console.bridge import Bridge

LOAD = "runspec_console.adapters.base.load_adapter"


class _StubBridge:
    def __init__(self, cfg: dict) -> None:
        self._adapter = None
        self._cfg = cfg

    def get_config(self) -> dict:
        return self._cfg

    _get_adapter = Bridge._get_adapter
    _ensure_plugins_imported = Bridge._ensure_plugins_imported
    list_providers = Bridge.list_providers


def test_static_key_adapter_is_cached():
    cfg = {
        "llm": {
            "provider": "anthropic",
            "api_key": "sk-static",
            "model": "claude-sonnet-4-6",
        }
    }
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        a = bridge._get_adapter()
        b = bridge._get_adapter()
    assert a is b == "ADAPTER"
    assert load.call_count == 1  # second call hit the cache
    assert bridge._adapter == "ADAPTER"


def test_api_key_command_threaded_and_not_cached():
    cfg = {
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "base_url": "https://proxy.example.com",
            "api_key_command": "token-helper",
            "api_key_ttl_ms": 3_600_000,
        }
    }
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
        bridge._get_adapter()
    # A fresh adapter each call — bridge-level cache is bypassed so the adapter's
    # own TTL governs refresh.
    assert load.call_count == 2
    assert bridge._adapter is None
    # New keys are forwarded to the adapter.
    _, kwargs = load.call_args
    assert kwargs["api_key_command"] == "token-helper"
    assert kwargs["api_key_ttl_ms"] == 3_600_000
    assert kwargs["base_url"] == "https://proxy.example.com"


def test_langserve_knobs_threaded():
    cfg = {
        "llm": {
            "provider": "langserve",
            "base_url": "https://gateway.corp/my-chain",
            "api_key_command": "vend-token",
            "api_key_ttl_ms": 300_000,
            "tools_in_config": True,
            "input_messages_key": "chat_history",
            "auth_header": "X-Api-Key",
            "max_tokens": 8192,
            "max_tokens_key": "maxTokens",
            "max_tokens_in": "config",
        }
    }
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert kwargs["base_url"] == "https://gateway.corp/my-chain"
    assert kwargs["tools_in_config"] is True
    assert kwargs["input_messages_key"] == "chat_history"
    assert kwargs["auth_header"] == "X-Api-Key"
    # The output-ceiling knobs reach the langserve adapter.
    assert kwargs["max_tokens"] == 8192
    assert kwargs["max_tokens_key"] == "maxTokens"
    assert kwargs["max_tokens_in"] == "config"
    # api_key_command path bypasses the bridge-level adapter cache.
    assert bridge._adapter is None


@pytest.mark.parametrize("provider", ["anthropic", "bedrock", "langserve"])
def test_max_tokens_threaded_for_supported_providers(provider):
    cfg = {"llm": {"provider": provider, "base_url": "https://x", "max_tokens": 12000}}
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert kwargs["max_tokens"] == 12000


def test_max_tokens_not_threaded_to_openai():
    # The OpenAI adapter's constructor has no max_tokens param — forwarding it
    # would raise TypeError, so the guard must skip it.
    cfg = {"llm": {"provider": "openai", "api_key": "k", "max_tokens": 12000}}
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert "max_tokens" not in kwargs


@pytest.mark.parametrize("provider", ["anthropic", "bedrock", "langserve"])
def test_effort_threaded_for_supported_providers(provider):
    # anthropic/bedrock map effort to Anthropic adaptive thinking; langserve passes
    # it through as a wire field. All three declare an `effort` constructor param.
    cfg = {"llm": {"provider": provider, "base_url": "https://x", "effort": "high"}}
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert kwargs["effort"] == "high"


def test_effort_not_threaded_to_openai():
    # The OpenAI adapter's constructor has no effort param; the guard must skip it
    # so the constructor never sees an unknown kwarg.
    cfg = {"llm": {"provider": "openai", "base_url": "https://x", "effort": "high"}}
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert "effort" not in kwargs


def test_langserve_effort_placement_knobs_threaded():
    # effort_key / effort_in describe where the pass-through effort string lands in
    # the /invoke body — forwarded only to the langserve adapter.
    cfg = {
        "llm": {
            "provider": "langserve",
            "base_url": "https://gateway.corp/chain",
            "effort": "high",
            "effort_key": "thinking_level",
            "effort_in": "config",
        }
    }
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert kwargs["effort"] == "high"
    assert kwargs["effort_key"] == "thinking_level"
    assert kwargs["effort_in"] == "config"


def test_effort_off_or_unset_not_threaded():
    # An empty or explicit "off" effort is not forwarded, so the adapter keeps its
    # no-thinking default (rather than being handed a redundant off kwarg).
    for effort in ("", "off"):
        cfg = {
            "llm": {"provider": "anthropic", "base_url": "https://x", "effort": effort}
        }
        bridge = _StubBridge(cfg)
        with patch(LOAD, return_value="ADAPTER") as load:
            bridge._get_adapter()
        _, kwargs = load.call_args
        assert "effort" not in kwargs


def test_effort_level_mapping():
    from runspec_console.adapters.base import effort_level

    assert effort_level("low") == "low"
    assert effort_level("medium") == "medium"
    assert effort_level("HIGH") == "high"  # case-insensitive, normalised
    assert effort_level(" xhigh ") == "xhigh"  # trimmed
    assert effort_level("max") == "max"
    assert effort_level("off") == ""
    assert effort_level("") == ""
    assert effort_level("bogus") == ""  # unknown fails safe to off


def test_apply_thinking_enables_adaptive_with_effort():
    from runspec_console.adapters.base import apply_thinking

    # Adaptive thinking: thinking={"type": "adaptive"} + output_config.effort;
    # max_tokens is left untouched (adaptive thinking counts toward it).
    kwargs = {"max_tokens": 8192}
    apply_thinking(kwargs, "high")
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["max_tokens"] == 8192  # unchanged — no budget to reserve

    # xhigh/max are passed straight through as the effort level.
    kwargs = {"max_tokens": 16000}
    apply_thinking(kwargs, "max")
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == "max"
    assert kwargs["max_tokens"] == 16000


def test_apply_thinking_preserves_existing_output_config():
    from runspec_console.adapters.base import apply_thinking

    # An existing output_config (e.g. a future format hint) is merged, not clobbered.
    kwargs = {"max_tokens": 8192, "output_config": {"format": {"type": "text"}}}
    apply_thinking(kwargs, "medium")
    assert kwargs["output_config"] == {
        "format": {"type": "text"},
        "effort": "medium",
    }


def test_apply_thinking_off_is_noop():
    from runspec_console.adapters.base import apply_thinking

    kwargs = {"max_tokens": 8192}
    apply_thinking(kwargs, "off")  # effort off
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs
    assert kwargs["max_tokens"] == 8192

    kwargs = {"max_tokens": 8192}
    apply_thinking(kwargs, "")  # unset
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_plugin_import_error_surfaces_cause_not_fake_extra():
    # A plugin provider whose dependency (e.g. httpx) is missing must not be told
    # to `pip install runspec-console[<provider>]` — there is no such extra.
    bridge = _StubBridge({"llm": {"provider": "acme-llm"}})
    with patch(LOAD, side_effect=ImportError("No module named 'httpx'")):
        with pytest.raises(ImportError) as exc:
            bridge._get_adapter()
    msg = str(exc.value)
    assert "runspec-console[acme-llm]" not in msg
    assert "httpx" in msg  # the real cause is preserved


def test_builtin_import_error_still_suggests_the_extra():
    bridge = _StubBridge({"llm": {"provider": "anthropic"}})
    with patch(LOAD, side_effect=ImportError("anthropic")):
        with pytest.raises(ImportError) as exc:
            bridge._get_adapter()
    assert "pip install runspec-console[anthropic]" in str(exc.value)


def test_no_provider_returns_none():
    bridge = _StubBridge({"llm": {}})
    assert bridge._get_adapter() is None


def test_plugin_declared_config_keys_are_forwarded(monkeypatch):
    # A plugin adapter that declares its own constructor parameters should
    # receive matching [llm] config keys — without the allowlist knowing them.
    def _make(api_key=None, tenant_id=None, region=None, **_extra):  # noqa: ANN001
        ...

    monkeypatch.setitem(base._REGISTRY, "mycorp", _make)
    cfg = {
        "llm": {
            "provider": "mycorp",
            "api_key": "sk-1",
            "tenant_id": "acme",
            "region": "eu",
            "not_declared": "dropped",
        }
    }
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert kwargs["tenant_id"] == "acme"
    assert kwargs["region"] == "eu"
    assert kwargs["api_key"] == "sk-1"
    # A config key the adapter does not declare is never forwarded.
    assert "not_declared" not in kwargs
    # `provider` selects the adapter; it is not threaded through as a kwarg.
    assert "provider" not in kwargs


def test_builtin_kwargs_factory_forwards_nothing_extra():
    # Built-in factories take **kwargs (no named params), so a stray [llm] key
    # is not signature-matched and stays out of the adapter kwargs.
    cfg = {"llm": {"provider": "anthropic", "api_key": "k", "tenant_id": "x"}}
    bridge = _StubBridge(cfg)
    with patch(LOAD, return_value="ADAPTER") as load:
        bridge._get_adapter()
    _, kwargs = load.call_args
    assert "tenant_id" not in kwargs
