"""
test_save_config_pool.py — save_config disturbs the SSH pool as little as
possible.

The pool consumes only the ``[ssh]`` section, and its connection key already
encodes the connection-shaping fields (user/proxy/identity). So:

* unrelated saves (LLM tab, ``[refresh]`` tuning) touch nothing;
* a full rebuild (which drops connections) happens only when pooling toggles
  on/off or ``max_concurrent`` changes;
* every other ``[ssh]`` change applies in place (tunables) or migrates lazily
  (connection-shaping) — live connections survive.

The real save_config / _apply_ssh_config are bound onto a light stub so we
exercise them without Bridge.__init__; the on-disk read/write is patched out.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from runspec_console.bridge import Bridge


class _FakePool:
    def __init__(self) -> None:
        self.closed = False
        self.updated_with: dict[str, Any] | None = None

    def close_all(self) -> None:
        self.closed = True

    def update_settings(self, settings: dict[str, Any]) -> None:
        self.updated_with = settings


class _SaveStub:
    def __init__(self, old_cfg: dict[str, Any]) -> None:
        self._old = old_cfg
        self._adapter = "cached-adapter"
        self._ssh_pool = _FakePool()
        self.built: list[_FakePool] = []

    def get_config(self) -> dict[str, Any]:
        return self._old

    def _build_ssh_pool(self) -> _FakePool:
        p = _FakePool()
        self.built.append(p)
        return p

    save_config = Bridge.save_config
    _apply_ssh_config = Bridge._apply_ssh_config


WRITE = "runspec_console.bridge.write_config"
NORM = "runspec_console.bridge._normalize_ssh_section"


def _run_save(old: dict[str, Any], new: dict[str, Any]) -> _SaveStub:
    stub = _SaveStub(old)
    with patch(NORM, side_effect=lambda s: s), patch(WRITE):
        stub.save_config(new)
    return stub


# ── no-op cases (connections preserved) ───────────────────────────────────────


def test_llm_only_save_keeps_pool_untouched():
    pool_before = None
    old = {"ssh": {"user": "j"}, "llm": {"provider": "acme-llm"}}
    stub = _SaveStub(old)
    pool_before = stub._ssh_pool
    with patch(NORM, side_effect=lambda s: s), patch(WRITE):
        stub.save_config(
            {
                "ssh": {"user": "j"},
                "llm": {"provider": "acme-llm", "system": "be terse"},
            }
        )
    assert stub._ssh_pool is pool_before
    assert pool_before.closed is False
    assert pool_before.updated_with is None
    assert stub.built == []
    assert stub._adapter is None  # adapter still invalidated so LLM change applies


def test_refresh_only_save_does_not_touch_pool():
    # [refresh] is consumed by the refresh watcher, not the pool.
    stub = _run_save({"refresh": {"interval": 30}}, {"refresh": {"interval": 10}})
    assert stub.built == []
    assert stub._ssh_pool.closed is False
    assert stub._ssh_pool.updated_with is None


# ── in-place cases (connections preserved) ────────────────────────────────────


def test_connection_shaping_change_updates_in_place():
    # user change shapes the connection key → migrates lazily; no rebuild/drop.
    stub = _run_save({"ssh": {"user": "j"}}, {"ssh": {"user": "k"}})
    assert stub.built == []
    assert stub._ssh_pool.closed is False
    assert stub._ssh_pool.updated_with is not None  # tunables refreshed in place


def test_tunable_change_updates_in_place():
    stub = _run_save({"ssh": {"keepalive": 30}}, {"ssh": {"keepalive": 60}})
    assert stub.built == []
    assert stub._ssh_pool.closed is False
    assert stub._ssh_pool.updated_with["keepalive"] == 60


# ── rebuild cases (the only times connections drop) ───────────────────────────


def test_pool_toggle_off_rebuilds_and_closes():
    stub = _run_save({"ssh": {"pool": True}}, {"ssh": {"pool": False}})
    assert len(stub.built) == 1
    # old pool closed
    assert stub.built[0] is stub._ssh_pool  # replaced


def test_max_concurrent_change_rebuilds():
    stub = _run_save({"ssh": {"max_concurrent": 4}}, {"ssh": {"max_concurrent": 8}})
    assert len(stub.built) == 1


def test_rebuild_closes_old_pool():
    stub = _SaveStub({"ssh": {"pool": True}})
    old_pool = stub._ssh_pool
    with patch(NORM, side_effect=lambda s: s), patch(WRITE):
        stub.save_config({"ssh": {"pool": False}})
    assert old_pool.closed is True
    assert stub._ssh_pool is not old_pool
