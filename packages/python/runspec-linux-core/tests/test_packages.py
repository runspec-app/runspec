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
