"""Shape of the root package's runspec.toml and entry points."""

from __future__ import annotations

from importlib.resources import files

import pytest

tomllib = pytest.importorskip("tomllib")

SPEC = tomllib.loads((files("runspec_linux_root") / "runspec.toml").read_text())
RUNNABLES = {k: v for k, v in SPEC.items() if k != "config"}


def test_every_tool_runs_as_root_except_the_bootstrap() -> None:
    for name, body in RUNNABLES.items():
        expected = "" if name == "enable-passwordless-sudo" else "root"
        assert body.get("run_as") == expected, name


def test_no_per_invocation_escalation_args() -> None:
    for name, body in RUNNABLES.items():
        args = body.get("args") or {}
        assert not {"run-as", "sudo", "become-method"} & set(args), name


def test_mutating_tools_need_confirmation() -> None:
    for name, body in RUNNABLES.items():
        if name != "list-upgrades":
            assert body.get("autonomy", SPEC["config"]["autonomy-default"]) == "confirm", name


def test_entry_points_match_runnables() -> None:
    import runspec_linux_root.packages as packages
    import runspec_linux_root.perf as perf
    import runspec_linux_root.power as power
    import runspec_linux_root.sudoers as sudoers

    mains = {
        "list-upgrades": packages.main_list_upgrades,
        "upgrade-packages": packages.main_upgrade_packages,
        "install-package": packages.main_install_package,
        "remove-package": packages.main_remove_package,
        "autoremove-packages": packages.main_autoremove_packages,
        "set-sysctl": perf.main_set_sysctl,
        "reboot-host": power.main_reboot_host,
        "enable-passwordless-sudo": sudoers.main_enable_passwordless_sudo,
    }
    assert set(mains) == set(RUNNABLES)


def test_set_sysctl_runs_the_core_helper_without_escalating(monkeypatch, capsys) -> None:
    # Already root (run_as = "root"), so the wrapper passes no run_as of its own.
    import json

    import runspec_linux_root.perf as perf

    calls = []
    monkeypatch.setattr(perf, "set_sysctl", lambda key, value, **kw: calls.append((key, value, kw)) or {"key": key, "applied": True})
    monkeypatch.setattr("sys.argv", ["set-sysctl", "--key", "vm.swappiness", "--value", "10"])
    # The suite doesn't run as root; stub the identity check the parser uses.
    monkeypatch.setattr("runspec.parser.verify_run_as_identity", lambda user: None)
    perf.main_set_sysctl()
    assert calls == [("vm.swappiness", "10", {"persist": False})]
    assert json.loads(capsys.readouterr().out)["applied"] is True
