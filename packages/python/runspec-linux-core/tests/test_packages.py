"""Package-manager detection, name validation, and output parsing.

The parsers are pure (string in, list of dicts out) so they are tested directly;
``detect_manager`` and ``list_packages`` are exercised with the subprocess layer
stubbed so no real package manager is touched.
"""

import subprocess

import pytest

import runspec_linux_core as core
from runspec_linux_core import packages as pkg
from runspec_linux_core.errors import CommandError


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ── detect_manager ────────────────────────────────────────────────────────────


def test_detect_manager_prefers_first_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only dnf present (apt-get absent) → dnf wins over later yum/zypper/pacman.
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda name: name == "dnf")
    assert pkg.detect_manager() == "dnf"


def test_detect_manager_apt_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda name: name in ("apt-get", "dnf"))
    assert pkg.detect_manager() == "apt-get"


# ── name validation ───────────────────────────────────────────────────────────


def test_validate_packages_splits_multiple() -> None:
    assert pkg._validate_packages("htop  vim curl") == ["htop", "vim", "curl"]


def test_validate_packages_allows_real_names() -> None:
    assert pkg._validate_packages("g++ lib32z1 python3.10 foo:amd64") == [
        "g++",
        "lib32z1",
        "python3.10",
        "foo:amd64",
    ]


def test_validate_packages_rejects_empty() -> None:
    with pytest.raises(ValueError):
        pkg._validate_packages("   ")


@pytest.mark.parametrize("bad", ["-rf", "foo;rm", "$(whoami)", "a b&"])
def test_validate_packages_rejects_injection(bad: str) -> None:
    # The "a b&" case splits into ["a", "b&"]; the "b&" token must be rejected.
    with pytest.raises(ValueError):
        pkg._validate_packages(bad)


# ── parsers ───────────────────────────────────────────────────────────────────


def test_parse_two_col() -> None:
    out = "htop\t3.0.5-7\nvim\t2:8.2\nbroken-line-no-tab\n"
    assert pkg._parse_two_col(out) == [
        {"name": "htop", "version": "3.0.5-7"},
        {"name": "vim", "version": "2:8.2"},
    ]


def test_parse_pacman_q() -> None:
    assert pkg._parse_pacman_q("htop 3.0.5-1\nvim 8.2.0-1\n") == [
        {"name": "htop", "version": "3.0.5-1"},
        {"name": "vim", "version": "8.2.0-1"},
    ]


def test_parse_apt_list() -> None:
    out = "Listing...\nhtop/focal-updates 3.0.5-1 amd64 [upgradable from: 2.2.0-2]\nvim/focal 2:8.2 amd64 [upgradable from: 2:8.1]\n"
    assert pkg._parse_apt_list(out) == [
        {"name": "htop", "current": "2.2.0-2", "candidate": "3.0.5-1"},
        {"name": "vim", "current": "2:8.1", "candidate": "2:8.2"},
    ]


def test_parse_dnf_check() -> None:
    out = "Last metadata expiration check: 0:01 ago.\n\nhttpd.x86_64    2.4.53-7.el9    appstream\nkernel.x86_64   5.14.0-70       baseos\n"
    assert pkg._parse_dnf_check(out) == [
        {"name": "httpd", "current": "", "candidate": "2.4.53-7.el9"},
        {"name": "kernel", "current": "", "candidate": "5.14.0-70"},
    ]


def test_parse_zypper_updates() -> None:
    out = (
        "S | Repository | Name | Current Version | Available Version | Arch\n"
        "--+------------+------+-----------------+-------------------+------\n"
        "v | repo-oss   | curl | 7.79.1-1        | 7.79.1-2          | x86_64\n"
    )
    assert pkg._parse_zypper_updates(out) == [
        {"name": "curl", "current": "7.79.1-1", "candidate": "7.79.1-2"},
    ]


def test_parse_pacman_qu() -> None:
    out = "htop 3.0.5-1 -> 3.1.0-1\nvim 8.2.0-1 -> 8.2.1-1\n"
    assert pkg._parse_pacman_qu(out) == [
        {"name": "htop", "current": "3.0.5-1", "candidate": "3.1.0-1"},
        {"name": "vim", "current": "8.2.0-1", "candidate": "8.2.1-1"},
    ]


# ── list_packages (filter + limit + sort) ─────────────────────────────────────


def test_list_packages_filters_sorts_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pkg, "detect_manager", lambda: "apt-get")
    monkeypatch.setattr(
        pkg,
        "_run",
        lambda cmd: _completed("vim\t2\nlibvirt\t8\nhtop\t3\nzlib\t1\n"),
    )
    result = core.list_packages(name_filter="li", limit=1)
    assert result["manager"] == "apt-get"
    assert result["total"] == 2  # libvirt, zlib both contain "li"
    assert result["count"] == 1
    assert result["packages"] == [{"name": "libvirt", "version": "8"}]  # sorted, capped


def test_list_packages_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pkg, "detect_manager", lambda: "apt-get")
    monkeypatch.setattr(pkg, "_run", lambda cmd: _completed("", returncode=2, stderr="boom"))
    with pytest.raises(CommandError):
        core.list_packages()


# ── remove_package --purge ────────────────────────────────────────────────────


def _recorder(monkeypatch: pytest.MonkeyPatch, mgr: str, outputs: dict | None = None) -> list[list[str]]:
    """Stub the manager + subprocess layer; record each argv and answer by its first two words."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pkg, "detect_manager", lambda: mgr)

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return (outputs or {}).get(" ".join(cmd[:2]), _completed())

    monkeypatch.setattr(pkg, "_run", run)
    return calls


def test_remove_package_purge_on_apt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "apt-get")
    result = core.remove_package("htop vim", purge=True)
    assert calls == [["apt-get", "purge", "-y", "htop", "vim"]]
    assert result["purged"] is True and result["removed"] is True


def test_remove_package_default_is_plain_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "pacman")
    assert core.remove_package("htop")["purged"] is False
    assert calls == [["pacman", "-R", "--noconfirm", "htop"]]


def test_remove_package_purge_on_pacman(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "pacman")
    core.remove_package("htop", purge=True)
    assert calls == [["pacman", "-Rn", "--noconfirm", "htop"]]


def test_remove_package_purge_is_a_plain_remove_on_rpm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "dnf")
    assert core.remove_package("htop", purge=True)["purged"] is False
    assert calls == [["dnf", "remove", "-y", "htop"]]


# ── autoremove_packages ───────────────────────────────────────────────────────

APT_SIM = (
    "Reading package lists...\nThe following packages will be REMOVED:\n  linux-image-7.0.4+deb14-amd64\nRemv linux-image-7.0.4+deb14-amd64 [7.0.4-1]\nPurg linux-headers-7.0.4+deb14-amd64 [7.0.4-1]\n"
)


def test_parse_apt_simulate() -> None:
    assert pkg._parse_apt_simulate(APT_SIM) == [
        {"name": "linux-image-7.0.4+deb14-amd64", "version": "7.0.4-1"},
        {"name": "linux-headers-7.0.4+deb14-amd64", "version": "7.0.4-1"},
    ]


def test_parse_zypper_packages() -> None:
    out = "S  | Repository | Name   | Version | Arch\n---+------------+--------+---------+-------\ni  | repo-oss   | libfoo | 1.2-3   | x86_64\ni+ | repo-oss   | bar    | 4.5     | noarch\n"
    assert pkg._parse_zypper_packages(out) == [
        {"name": "libfoo", "version": "1.2-3"},
        {"name": "bar", "version": "4.5"},
    ]


def test_autoremove_dry_run_lists_without_removing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "apt-get", {"apt-get -s": _completed(APT_SIM)})
    result = core.autoremove_packages(purge=True, dry_run=True)
    assert calls == [["apt-get", "-s", "autoremove", "--purge"]]
    assert result["dry_run"] is True and result["removed"] is False and result["purged"] is True
    assert result["count"] == 2


def test_autoremove_apt_real_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "apt-get", {"apt-get -s": _completed(APT_SIM)})
    result = core.autoremove_packages()
    assert calls == [["apt-get", "-s", "autoremove"], ["apt-get", "autoremove", "-y"]]
    assert result["removed"] is True and result["count"] == 2 and result["purged"] is False


def test_autoremove_nothing_to_do_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "apt-get", {"apt-get -s": _completed("Reading package lists...\n")})
    result = core.autoremove_packages(purge=True)
    assert calls == [["apt-get", "-s", "autoremove", "--purge"]]
    assert result["count"] == 0 and result["removed"] is False


def test_autoremove_pacman_removes_listed_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "pacman", {"pacman -Qdt": _completed("libfoo 1.0-1\n")})
    core.autoremove_packages(purge=True)
    assert calls[-1] == ["pacman", "-Rns", "--noconfirm", "libfoo"]


def test_autoremove_pacman_no_orphans_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "pacman", {"pacman -Qdt": _completed("", returncode=1)})
    assert core.autoremove_packages()["count"] == 0
    assert len(calls) == 1


def test_autoremove_zypper_removes_unneeded(monkeypatch: pytest.MonkeyPatch) -> None:
    table = "S | Repository | Name | Version | Arch\ni | oss | libfoo | 1 | x86_64\n"
    calls = _recorder(monkeypatch, "zypper", {"zypper --non-interactive": _completed(table)})
    core.autoremove_packages()
    assert calls[-1] == ["zypper", "--non-interactive", "remove", "libfoo"]


def test_autoremove_yum_refuses_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "yum")
    with pytest.raises(CommandError, match="dry run unsupported"):
        core.autoremove_packages(dry_run=True)
    assert calls == []


def test_autoremove_yum_real_run_has_no_list(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "yum")
    result = core.autoremove_packages()
    assert calls == [["yum", "autoremove", "-y"]]
    assert result["packages"] is None and result["removed"] is True


def test_autoremove_listing_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _recorder(monkeypatch, "apt-get", {"apt-get -s": _completed("", returncode=100, stderr="locked")})
    with pytest.raises(CommandError, match="locked"):
        core.autoremove_packages()


# ── list_packages(reason=True) ────────────────────────────────────────────────


def test_list_packages_reason_marks_manual_and_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    _recorder(
        monkeypatch,
        "apt-get",
        {
            "dpkg-query -W": _completed("linux-image-7.0.4\t7.0.4-1\nlinux-image-7.0.13\t7.0.13-1\n"),
            "apt-mark showmanual": _completed("linux-image-7.0.4\nvim\n"),
        },
    )
    rows = core.list_packages(name_filter="linux-image", reason=True)["packages"]
    assert rows == [
        {"name": "linux-image-7.0.13", "version": "7.0.13-1", "installed": "auto"},
        {"name": "linux-image-7.0.4", "version": "7.0.4-1", "installed": "manual"},
    ]


def test_list_packages_reason_unknown_when_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _recorder(
        monkeypatch,
        "pacman",
        {"pacman -Q": _completed("htop 3.0-1\n"), "pacman -Qqe": _completed("", returncode=1)},
    )
    assert core.list_packages(reason=True)["packages"][0]["installed"] is None


def test_list_packages_reason_unknown_on_yum(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "yum", {"rpm -qa": _completed("htop\t3.0-1\n")})
    assert core.list_packages(reason=True)["packages"][0]["installed"] is None
    assert len(calls) == 1


def test_list_packages_without_reason_runs_one_query(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "apt-get", {"dpkg-query -W": _completed("vim\t2\n")})
    assert "installed" not in core.list_packages()["packages"][0]
    assert len(calls) == 1


# ── list_autoremovable ────────────────────────────────────────────────────────


def test_list_autoremovable_only_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, "apt-get", {"apt-get -s": _completed(APT_SIM)})
    result = core.list_autoremovable(purge=True)
    assert calls == [["apt-get", "-s", "autoremove", "--purge"]]
    assert result["count"] == 2 and result["purged"] is True and "removed" not in result


def test_list_autoremovable_refuses_yum(monkeypatch: pytest.MonkeyPatch) -> None:
    _recorder(monkeypatch, "yum")
    with pytest.raises(CommandError, match="yum"):
        core.list_autoremovable()
