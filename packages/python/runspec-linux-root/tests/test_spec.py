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


READ_ONLY = {("list-upgrades", None), ("package-repo", "list"), ("config-file", "allowed")}
# Widening where root may write is a person's call, never an agent's.
MANUAL = {("config-file", "allow"), ("config-file", "disallow")}


def test_mutating_tools_need_confirmation() -> None:
    default = SPEC["config"]["autonomy-default"]
    for name, body in RUNNABLES.items():
        commands = body.get("commands") or {None: {}}
        for command, cbody in commands.items():
            autonomy = cbody.get("autonomy", body.get("autonomy", default))
            if (name, command) in READ_ONLY:
                expected = "autonomous"
            elif (name, command) in MANUAL:
                expected = "manual"
            else:
                expected = "confirm"
            assert autonomy == expected, (name, command)


def test_entry_points_match_runnables() -> None:
    import runspec_linux_root.configfile as configfile
    import runspec_linux_root.packages as packages
    import runspec_linux_root.perf as perf
    import runspec_linux_root.power as power
    import runspec_linux_root.repos as repos
    import runspec_linux_root.services as services
    import runspec_linux_root.sudoers as sudoers

    mains = {
        "list-upgrades": packages.main_list_upgrades,
        "upgrade-packages": packages.main_upgrade_packages,
        "install-package": packages.main_install_package,
        "remove-package": packages.main_remove_package,
        "autoremove-packages": packages.main_autoremove_packages,
        "package-repo": repos.main,
        "system-service": services.main,
        "config-file": configfile.main,
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


def _run_main(monkeypatch, argv, main):
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr("runspec.parser.verify_run_as_identity", lambda user: None)
    main()


def test_package_repo_add_passes_both_pairs(monkeypatch, capsys) -> None:
    import json

    import runspec_linux_root.repos as repos

    seen = {}
    monkeypatch.setattr(repos, "add_repo", lambda name, **kw: seen.update(name=name, **kw) or {"name": name, "changed": True})
    _run_main(
        monkeypatch,
        [
            "package-repo",
            "add",
            "grafana",
            "--apt-url",
            "https://apt.grafana.com",
            "--apt-key",
            "https://apt.grafana.com/gpg-full.key",
            "--rpm-url",
            "https://rpm.grafana.com",
            "--rpm-key",
            "https://rpm.grafana.com/gpg.key",
            "--key-fingerprint",
            "AAAA,BBBB",
            "--key-fingerprint",
            "CCCC",
            "--no-refresh",
        ],
        repos.main,
    )
    assert seen["name"] == "grafana"
    assert seen["apt_url"] == "https://apt.grafana.com"
    assert seen["rpm_key"] == "https://rpm.grafana.com/gpg.key"
    assert seen["apt_suite"] == "stable" and seen["apt_components"] == "main"
    assert seen["key_fingerprints"] == ["AAAA", "BBBB", "CCCC"]
    assert seen["refresh"] is False and seen["signed_metadata"] is True and seen["replace"] is False
    assert json.loads(capsys.readouterr().out)["changed"] is True


def test_package_repo_errors_are_json(monkeypatch, capsys) -> None:
    import json

    import runspec_linux_root.repos as repos

    def boom(name, **kw):
        raise ValueError("this host uses apt: give --apt-url and --apt-key for the 'grafana' repo")

    monkeypatch.setattr(repos, "add_repo", boom)
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["package-repo", "add", "grafana"], repos.main)
    assert exc.value.code == 1
    assert "give --apt-url" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["system-service", "enable", "alloy", "--now"], ("alloy", "enable", True)),
        (["system-service", "restart", "alloy"], ("alloy", "restart", False)),
    ],
)
def test_system_service_calls_the_core_helper(monkeypatch, capsys, argv, expected) -> None:
    import runspec_linux_root.services as services

    calls = []
    monkeypatch.setattr(services, "control_service", lambda s, a, now: calls.append((s, a, now)) or {"service": s})
    _run_main(monkeypatch, argv, services.main)
    assert calls == [expected]
