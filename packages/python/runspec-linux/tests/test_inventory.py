"""The block-devices and network-interfaces runnables: declared flags and the JSON they print."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import runspec as rs
from runspec_linux_core import ToolNotFoundError

# Import the wrapper modules explicitly: ``runspec_linux`` re-exports the core
# ``system_info`` function under the same name as its wrapper module.
network = importlib.import_module("runspec_linux.network")
system_info = importlib.import_module("runspec_linux.system_info")

TOML = Path(__file__).resolve().parent.parent / "runspec_linux" / "runspec.toml"


def _fixed_parse(name: str, argv: list[str]):
    """A real parse bound to a runnable + argv, for patching a wrapper module's ``rs.parse``."""
    real = rs.parse
    return lambda *_a, **_k: real(name, argv, config_path=TOML)


def test_flags_default_off() -> None:
    assert rs.parse("block-devices", [], config_path=TOML).all.value is False
    assert rs.parse("block-devices", ["-a"], config_path=TOML).all.value is True
    assert rs.parse("network-interfaces", [], config_path=TOML).physical.value is False
    assert rs.parse("network-interfaces", ["--physical"], config_path=TOML).physical.value is True


def test_main_block_devices_passes_the_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(system_info.rs, "parse", _fixed_parse("block-devices", ["--all"]))
    monkeypatch.setattr(system_info, "block_devices", lambda include_loop: seen.append(include_loop) or {"devices": [{"name": "sda"}]})
    system_info.main_block_devices()
    assert seen == [True]
    assert json.loads(capsys.readouterr().out) == {"devices": [{"name": "sda"}]}


def test_main_network_interfaces_error_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def missing(physical_only: bool) -> dict:
        raise ToolNotFoundError("ip not available (iproute2)")

    monkeypatch.setattr(network.rs, "parse", _fixed_parse("network-interfaces", []))
    monkeypatch.setattr(network, "network_interfaces", missing)
    with pytest.raises(SystemExit) as exc:
        network.main_network_interfaces()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ip not available (iproute2)"}
