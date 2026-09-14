"""
test_adapter_capabilities.py — ModelAdapter.capabilities() + the [llm.model_limits]
table reader + the console's clamp/effort reconciliation (Bridge._apply_capabilities).
"""

from __future__ import annotations

from typing import Any

from runspec_console.adapters.base import (
    ModelAdapter,
    ModelCapabilities,
    capabilities_from_limits,
)
from runspec_console.bridge_internals import InternalsMixin


# ── capabilities_from_limits (pure) ───────────────────────────────────────────


def test_limits_longest_substring_merges_broad_and_specific():
    limits = {
        "claude": {"context_window": 200000, "max_output_tokens": 8192},
        "claude-opus-4-8": {"max_output_tokens": 64000, "adaptive_thinking": False},
    }
    caps = capabilities_from_limits("anthropic.claude-opus-4-8", limits)
    assert caps.context_window == 200000  # from the broad base
    assert caps.max_output_tokens == 64000  # specific overrides
    assert caps.adaptive_thinking is False


def test_limits_no_match_returns_defaults():
    caps = capabilities_from_limits("gpt-4o", {"claude": {"context_window": 1}})
    assert caps == ModelCapabilities()
    assert caps.adaptive_thinking is True and caps.cache_markers is True


def test_limits_empty_key_is_catch_all():
    caps = capabilities_from_limits("whatever", {"": {"context_window": 123}})
    assert caps.context_window == 123


def test_limits_none_and_garbage_are_safe():
    assert capabilities_from_limits("m", None) == ModelCapabilities()
    assert capabilities_from_limits("m", {}) == ModelCapabilities()
    # non-dict entry ignored; bad numeric dropped
    caps = capabilities_from_limits("m", {"m": "nope", "m2": {"context_window": "xyz"}})
    assert caps.context_window is None


def test_bool_string_coercion():
    caps = capabilities_from_limits(
        "m", {"m": {"adaptive_thinking": "false", "cache_markers": "no"}}
    )
    assert caps.adaptive_thinking is False
    assert caps.cache_markers is False


# ── default ModelAdapter.capabilities() reads self._model_limits ──────────────


class _Tiny(ModelAdapter):
    def __init__(self, model: str, limits: dict[str, Any] | None) -> None:
        self.model = model
        self._model_limits = limits or {}
        self.max_tokens = 8192
        self._effort = "high"

    async def chat(self, messages, tools):  # pragma: no cover - unused
        raise NotImplementedError

    async def stream_chat(self, messages, tools):  # pragma: no cover - unused
        yield ""

    def make_tool_turn(self, response, results):  # pragma: no cover - unused
        return []


def test_default_capabilities_uses_stored_table():
    a = _Tiny("claude-x", {"claude-x": {"context_window": 42, "max_output_tokens": 7}})
    caps = a.capabilities()
    assert caps.context_window == 42
    assert caps.max_output_tokens == 7


# ── Bridge._apply_capabilities: clamp max_tokens + drop effort ────────────────


def test_apply_capabilities_clamps_max_tokens_down():
    a = _Tiny("claude-x", {"claude-x": {"max_output_tokens": 4096}})
    a.max_tokens = 100000
    InternalsMixin._apply_capabilities(a)
    assert a.max_tokens == 4096


def test_apply_capabilities_clamps_down_even_when_operator_set_it():
    # A ceiling above the model's real cap 400s, so it is clamped whoever chose it.
    a = _Tiny("claude-x", {"claude-x": {"max_output_tokens": 4096}})
    a.max_tokens = 64000
    InternalsMixin._apply_capabilities(a, explicit_max_tokens=True)
    assert a.max_tokens == 4096


def test_apply_capabilities_raises_default_max_tokens_to_model_cap():
    # No `[llm] max_tokens` set → the adapter carries its conservative 8192
    # default, which is a safe floor rather than a considered choice. Holding a
    # 64000-capable model there makes every long reply hit the ceiling and
    # auto-continue, re-sending the conversation each round for a reply the model
    # could have written in one.
    a = _Tiny("claude-x", {"claude-x": {"max_output_tokens": 64000}})
    a.max_tokens = 8192
    InternalsMixin._apply_capabilities(a, explicit_max_tokens=False)
    assert a.max_tokens == 64000


def test_apply_capabilities_never_raises_an_explicit_max_tokens():
    # A deliberately low ceiling is a budget decision — left alone.
    a = _Tiny("claude-x", {"claude-x": {"max_output_tokens": 64000}})
    a.max_tokens = 8192
    InternalsMixin._apply_capabilities(a, explicit_max_tokens=True)
    assert a.max_tokens == 8192


def test_apply_capabilities_defaults_to_treating_max_tokens_as_explicit():
    # The default is the conservative one: a caller that doesn't say gets the
    # old clamp-down-only behaviour.
    a = _Tiny("claude-x", {"claude-x": {"max_output_tokens": 64000}})
    a.max_tokens = 8192
    InternalsMixin._apply_capabilities(a)
    assert a.max_tokens == 8192


def test_apply_capabilities_without_a_known_cap_leaves_max_tokens_alone():
    a = _Tiny("claude-x", {})
    a.max_tokens = 8192
    InternalsMixin._apply_capabilities(a, explicit_max_tokens=False)
    assert a.max_tokens == 8192


def test_apply_capabilities_disables_effort_when_no_adaptive_thinking():
    a = _Tiny("claude-x", {"claude-x": {"adaptive_thinking": False}})
    InternalsMixin._apply_capabilities(a)
    assert a._effort == ""


def test_apply_capabilities_keeps_effort_when_supported():
    a = _Tiny("claude-x", {"claude-x": {"adaptive_thinking": True}})
    InternalsMixin._apply_capabilities(a)
    assert a._effort == "high"


def test_apply_capabilities_tolerates_missing_method():
    class _NoCaps:
        max_tokens = 999

    obj = _NoCaps()
    # A plugin adapter without capabilities() must not break construction.
    InternalsMixin._apply_capabilities(obj)
    assert obj.max_tokens == 999
