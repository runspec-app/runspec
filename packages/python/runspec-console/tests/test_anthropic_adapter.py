"""
test_anthropic_adapter.py — base_url + api_key_command behaviour for AnthropicAdapter.

These tests patch anthropic.AsyncAnthropic so no network or real key is needed.
The adapter module imports cleanly without the Windows-only deps the rest of the
package pulls in, so they run on any platform.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from runspec_console.adapters.anthropic import AnthropicAdapter

CLIENT = "runspec_console.adapters.anthropic.anthropic.AsyncAnthropic"
RUN = "runspec_console.adapters.anthropic.subprocess.run"
MONOTONIC = "runspec_console.adapters.anthropic.time.monotonic"


class _Result:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── base_url ──────────────────────────────────────────────────────────────────


def test_base_url_forwarded_to_client():
    with patch(CLIENT) as client:
        AnthropicAdapter(api_key="sk-test", base_url="https://proxy.example.com")
    _, kwargs = client.call_args
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["base_url"] == "https://proxy.example.com"


def test_no_base_url_omits_kwarg():
    with patch(CLIENT) as client:
        AnthropicAdapter(api_key="sk-test")
    _, kwargs = client.call_args
    assert "base_url" not in kwargs


# ── api_key_command ───────────────────────────────────────────────────────────


def test_api_key_command_defers_client_then_uses_stdout():
    with patch(CLIENT) as client:
        adapter = AnthropicAdapter(api_key_command="echo secret-key")
        # No client (and no command run) until the first request.
        client.assert_not_called()
        assert adapter.client is None

        adapter._refresh_key_if_needed()
        client.assert_called_once()
        _, kwargs = client.call_args
        assert kwargs["api_key"] == "secret-key"
        assert adapter._cached_key == "secret-key"


def test_api_key_command_forwards_base_url():
    with patch(CLIENT) as client:
        adapter = AnthropicAdapter(api_key_command="echo k", base_url="https://proxy")
        adapter._refresh_key_if_needed()
    _, kwargs = client.call_args
    assert kwargs == {"api_key": "k", "base_url": "https://proxy"}


def test_static_key_refresh_is_noop():
    with patch(CLIENT) as client:
        adapter = AnthropicAdapter(api_key="sk-static")
        client.reset_mock()
        adapter._refresh_key_if_needed()
        client.assert_not_called()  # static key path never rebuilds


def test_empty_stdout_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(
        RUN, lambda *a, **k: _Result(stdout="  \n", stderr="boom", returncode=3)
    )
    with patch(CLIENT):
        adapter = AnthropicAdapter(api_key_command="token-helper")
        with pytest.raises(RuntimeError, match="no output"):
            adapter._refresh_key_if_needed()


# ── TTL caching ───────────────────────────────────────────────────────────────


def _counting_run(calls: dict[str, int]):
    def run(*a, **k):
        calls["n"] += 1
        return _Result(stdout=f"key-{calls['n']}")

    return run


def test_ttl_caches_within_window(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(RUN, _counting_run(calls))
    with patch(CLIENT):
        adapter = AnthropicAdapter(api_key_command="x", api_key_ttl_ms=60_000)
        adapter._refresh_key_if_needed()
        adapter._refresh_key_if_needed()  # well within the 60s TTL
    assert calls["n"] == 1
    assert adapter._cached_key == "key-1"


def test_ttl_zero_reruns_every_call(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(RUN, _counting_run(calls))
    with patch(CLIENT):
        adapter = AnthropicAdapter(api_key_command="x", api_key_ttl_ms=0)
        adapter._refresh_key_if_needed()
        adapter._refresh_key_if_needed()
    assert calls["n"] == 2
    assert adapter._cached_key == "key-2"


def test_ttl_expiry_reruns(monkeypatch):
    calls = {"n": 0}
    clock = {"t": 1000.0}
    monkeypatch.setattr(RUN, _counting_run(calls))
    monkeypatch.setattr(MONOTONIC, lambda: clock["t"])
    with patch(CLIENT):
        adapter = AnthropicAdapter(api_key_command="x", api_key_ttl_ms=1_000)  # 1s
        adapter._refresh_key_if_needed()  # t=1000 → key-1
        clock["t"] = 1000.5
        adapter._refresh_key_if_needed()  # within TTL → no re-run
        clock["t"] = 1002.0
        adapter._refresh_key_if_needed()  # past TTL → key-2
    assert calls["n"] == 2
    assert adapter._cached_key == "key-2"


# ── chat() invokes a refresh ──────────────────────────────────────────────────


def test_chat_triggers_refresh(monkeypatch):
    import asyncio

    calls = {"n": 0}
    monkeypatch.setattr(RUN, _counting_run(calls))

    class _FakeMessages:
        async def create(self, **kwargs):
            class _Block:
                type = "text"
                text = "hi"

            class _Resp:
                content = [_Block()]
                stop_reason = "end_turn"

            return _Resp()

    class _FakeClient:
        messages = _FakeMessages()

    with patch(CLIENT, return_value=_FakeClient()):
        adapter = AnthropicAdapter(api_key_command="x", api_key_ttl_ms=0)
        result = asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], []))
    assert calls["n"] == 1  # chat() called _refresh_key_if_needed
    assert result.text == "hi"
