"""
test_plugin_adapters.py — the adapter registry + plugin discovery in base.py.

Covers all three resolution paths (dotted path, register_adapter, entry points),
available_providers(), and the unknown-provider error — without installing any
real plugin distribution.
"""

from __future__ import annotations

from typing import Any

import pytest

from runspec_console.adapters import base
from runspec_console.adapters.base import (
    ModelAdapter,
    available_providers,
    load_adapter,
    register_adapter,
)


class _Dummy(ModelAdapter):
    """Minimal concrete adapter — records the kwargs it was built with."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def chat(self, messages, tools):  # type: ignore[override]
        raise NotImplementedError

    async def stream_chat(self, messages, tools):  # type: ignore[override]
        yield ""

    def make_tool_turn(self, response, results):  # type: ignore[override]
        return []


def _make(**kwargs: Any) -> _Dummy:
    return _Dummy(**kwargs)


# ── built-ins are pre-registered ──────────────────────────────────────────────


def test_builtins_are_available():
    names = available_providers()
    for builtin in ("anthropic", "openai", "bedrock", "langserve"):
        assert builtin in names


# ── register_adapter (Option B) ───────────────────────────────────────────────


def test_register_and_load(monkeypatch):
    monkeypatch.setitem(base._REGISTRY, "mycorp", _make)
    adapter = load_adapter("mycorp", api_key="x", base_url="https://gw")
    assert isinstance(adapter, _Dummy)
    assert adapter.kwargs == {"api_key": "x", "base_url": "https://gw"}
    assert "mycorp" in available_providers()


def test_register_adapter_public_api(monkeypatch):
    # use the public function; clean up via the registry dict
    monkeypatch.setattr(base, "_REGISTRY", dict(base._REGISTRY))
    register_adapter("plugged", _make)
    assert load_adapter("plugged").kwargs == {}


# ── dotted path (Option C) ────────────────────────────────────────────────────


def _inject_fake_module(monkeypatch) -> None:
    import sys
    import types

    mod = types.ModuleType("fake_plugin_mod")
    mod.MyAdapter = _Dummy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_plugin_mod", mod)


def test_dotted_path_loads_class(monkeypatch):
    _inject_fake_module(monkeypatch)
    adapter = load_adapter("fake_plugin_mod:MyAdapter", model="m")
    assert isinstance(adapter, _Dummy)
    assert adapter.kwargs == {"model": "m"}


def test_dotted_path_bad_target_raises_value_error(monkeypatch):
    _inject_fake_module(monkeypatch)
    with pytest.raises(ValueError, match="Could not load adapter"):
        load_adapter("fake_plugin_mod:DoesNotExist")


# ── entry points (Option A) ───────────────────────────────────────────────────


def test_entry_point_discovery(monkeypatch):
    monkeypatch.setattr(base, "_entry_point_factories", lambda: {"epco": lambda: _make})
    adapter = load_adapter("epco", api_key="k")
    assert isinstance(adapter, _Dummy)
    assert adapter.kwargs == {"api_key": "k"}
    assert "epco" in available_providers()


def test_registry_wins_over_entry_point(monkeypatch):
    monkeypatch.setitem(base._REGISTRY, "dup", _make)
    sentinel_called = {"hit": False}

    def ep_factory():  # pragma: no cover - must not be called
        sentinel_called["hit"] = True
        return _make

    monkeypatch.setattr(base, "_entry_point_factories", lambda: {"dup": ep_factory})
    load_adapter("dup")
    assert sentinel_called["hit"] is False


# ── errors ────────────────────────────────────────────────────────────────────


def test_unknown_provider_lists_available():
    with pytest.raises(ValueError) as exc:
        load_adapter("nope-not-real")
    msg = str(exc.value)
    assert "Unknown LLM provider" in msg
    assert "anthropic" in msg  # the available list is shown


# ── import_plugin_modules ─────────────────────────────────────────────────────


def test_import_plugin_modules_runs_side_effects(monkeypatch):
    imported: list[str] = []

    import importlib

    def fake_import(name: str):
        imported.append(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    base.import_plugin_modules(["a.b", "c"])
    assert imported == ["a.b", "c"]


def test_import_plugin_modules_swallows_errors(monkeypatch, capsys):
    import importlib

    def boom(name: str):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(importlib, "import_module", boom)
    base.import_plugin_modules(["missing_pkg"])  # must not raise
    assert "missing_pkg" in capsys.readouterr().err
