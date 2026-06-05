"""
test_bridge_adapter.py — Bridge._get_adapter threading of the new LLM config keys.

Binds the real _get_adapter onto a light stub so we exercise the method without
Bridge.__init__'s filesystem side effects. load_adapter is patched to a sentinel.
"""

from __future__ import annotations

from unittest.mock import patch

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
    # api_key_command path bypasses the bridge-level adapter cache.
    assert bridge._adapter is None


def test_no_provider_returns_none():
    bridge = _StubBridge({"llm": {}})
    assert bridge._get_adapter() is None
