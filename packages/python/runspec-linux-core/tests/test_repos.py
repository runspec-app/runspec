"""Package repositories: list, add and remove, on a fake filesystem root.

``repos._ROOT`` points at a temporary directory, the package manager is
stubbed, and no command or download really runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runspec_linux_core import repos
from runspec_linux_core.errors import CommandError, LinuxCoreError

DATA = Path(__file__).parent / "data"
ONE = "0B8A8FE4594E7466A8EF613766F1EE2CD85AD90C"
TWO = "AB050B78F2DD817746A942A10626B9874383DE19"
KEY_ONE = (DATA / "test-key-one.asc").read_bytes()
KEYS_TWO = (DATA / "test-keys-two.asc").read_bytes()
REAL_FETCH = repos._fetch_key

APT = {"apt_url": "https://apt.example.com", "apt_key": "https://apt.example.com/key.asc"}
RPM = {"rpm_url": "https://rpm.example.com", "rpm_key": "https://rpm.example.com/key.asc"}


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class Host:
    """A fake host: a temp root, a package manager, a key server and a command log."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, mgr: str) -> None:
        self.root = root
        self.mgr = mgr
        self.keys: dict[str, bytes] = {APT["apt_key"]: KEY_ONE, RPM["rpm_key"]: KEY_ONE}
        self.commands: list[list[str]] = []
        self.refresh_result = _done(stdout="Get:1 https://apt.example.com stable InRelease")
        self.rpm_db: set[str] = set()
        monkeypatch.setattr(repos, "_ROOT", str(root))
        monkeypatch.setattr(repos, "detect_manager", lambda: mgr)
        monkeypatch.setattr(repos, "_fetch_key", lambda src, **kw: self.keys[src])
        monkeypatch.setattr(repos, "_run", self.run)
        monkeypatch.setattr(repos, "_rpm_has_key", lambda name: name in self.rpm_db)
        monkeypatch.setattr(repos.shutil, "which", lambda name: "/usr/bin/" + name)

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        if cmd[:2] == ["rpm", "--import"]:
            from runspec_linux_core import pgpkey

            for key in pgpkey.read_keys(Path(cmd[2]).read_bytes()):
                self.rpm_db.add(key.rpm_name)
            return _done()
        if cmd[:2] == ["rpm", "-e"]:
            self.rpm_db.discard(cmd[2])
            return _done()
        return self.refresh_result

    def path(self, rel: str) -> Path:
        return self.root / rel

    def write(self, rel: str, text: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


@pytest.fixture
def apt_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch, "apt-get")


@pytest.fixture(params=["dnf", "yum", "zypper"])
def rpm_host(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch, request.param)


# ── apt ───────────────────────────────────────────────────────────────────────


def test_apt_add_writes_key_and_source(apt_host: Host) -> None:
    out = repos.add_repo("grafana", **APT, **RPM)
    assert out["changed"] and out["refreshed"]
    assert out["keys"] == [{"fingerprint": ONE, "created": "2026-11-07", "subkeys": []}]
    source = apt_host.path("etc/apt/sources.list.d/grafana.list").read_text()
    assert source.splitlines() == [repos.MARKER, "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.example.com stable main"]
    key = apt_host.path("etc/apt/keyrings/grafana.asc")
    assert key.read_bytes().startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert oct(key.stat().st_mode & 0o777) == "0o644"
    # Only this repo is refreshed.
    assert apt_host.commands[-1][:2] == ["apt-get", "update"]
    assert "Dir::Etc::sourcelist=sources.list.d/grafana.list" in apt_host.commands[-1]


def test_apt_add_again_changes_nothing(apt_host: Host) -> None:
    repos.add_repo("grafana", **APT)
    apt_host.commands.clear()
    out = repos.add_repo("grafana", **APT)
    assert out["changed"] is False and out["refreshed"] is False
    assert apt_host.commands == []


def test_apt_suite_and_components(apt_host: Host) -> None:
    repos.add_repo("pg", **APT, apt_suite="noble-pgdg", apt_components="main contrib")
    assert "https://apt.example.com noble-pgdg main contrib" in apt_host.path("etc/apt/sources.list.d/pg.list").read_text()


def test_apt_flat_repo_has_no_components(apt_host: Host) -> None:
    repos.add_repo("flat", **APT, apt_suite="./", apt_components="")
    assert apt_host.path("etc/apt/sources.list.d/flat.list").read_text().splitlines()[1].endswith("https://apt.example.com ./")
    with pytest.raises(ValueError):
        repos.add_repo("flat2", **APT, apt_suite="./", apt_components="main")


def test_apt_refresh_failure_puts_everything_back(apt_host: Host) -> None:
    apt_host.refresh_result = _done(stderr="W: GPG error: https://apt.example.com stable InRelease: NO_PUBKEY 1234")
    with pytest.raises(CommandError, match="undone"):
        repos.add_repo("grafana", **APT)
    assert not apt_host.path("etc/apt/sources.list.d/grafana.list").exists()
    assert not apt_host.path("etc/apt/keyrings/grafana.asc").exists()


def test_apt_failed_update_restores_the_previous_version(apt_host: Host) -> None:
    repos.add_repo("grafana", **APT)
    before = apt_host.path("etc/apt/keyrings/grafana.asc").read_bytes()
    apt_host.keys[APT["apt_key"]] = KEYS_TWO
    apt_host.refresh_result = _done(returncode=100, stderr="E: The repository is not signed.")
    with pytest.raises(CommandError):
        repos.add_repo("grafana", **APT)
    assert apt_host.path("etc/apt/keyrings/grafana.asc").read_bytes() == before


def test_apt_no_refresh(apt_host: Host) -> None:
    out = repos.add_repo("grafana", **APT, refresh=False)
    assert out["changed"] and not out["refreshed"]
    assert apt_host.commands == []


def test_apt_needs_the_apt_pair(apt_host: Host) -> None:
    with pytest.raises(ValueError, match="this host uses apt: give --apt-url and --apt-key"):
        repos.add_repo("grafana", **RPM)


def test_apt_refuses_a_hand_made_file_without_replace(apt_host: Host) -> None:
    apt_host.write("etc/apt/sources.list.d/grafana.list", "deb https://apt.example.com stable main\n")
    with pytest.raises(LinuxCoreError, match="--replace"):
        repos.add_repo("grafana", **APT)
    out = repos.add_repo("grafana", **APT, replace=True)
    assert out["changed"]
    assert apt_host.path("etc/apt/sources.list.d/grafana.list").read_text().startswith(repos.MARKER)


def test_apt_replace_drops_a_same_named_deb822_file(apt_host: Host) -> None:
    apt_host.write("etc/apt/sources.list.d/grafana.sources", "Types: deb\nURIs: https://apt.example.com\nSuites: stable\nComponents: main\n")
    repos.add_repo("grafana", **APT, replace=True)
    assert not apt_host.path("etc/apt/sources.list.d/grafana.sources").exists()


def test_apt_refuses_the_same_url_under_another_name(apt_host: Host) -> None:
    apt_host.write("etc/apt/sources.list.d/other.list", "deb [signed-by=/x.gpg] https://apt.example.com/ stable main\n")
    with pytest.raises(LinuxCoreError, match="package-repo remove other"):
        repos.add_repo("grafana", **APT)


# ── keys ──────────────────────────────────────────────────────────────────────


def test_pinned_fingerprints_must_cover_every_key(apt_host: Host) -> None:
    apt_host.keys[APT["apt_key"]] = KEYS_TWO
    with pytest.raises(LinuxCoreError, match=TWO):
        repos.add_repo("grafana", **APT, key_fingerprints=[ONE])
    assert not apt_host.path("etc/apt/sources.list.d/grafana.list").exists()
    out = repos.add_repo("grafana", **APT, key_fingerprints=[ONE.lower(), TWO])
    assert [k["fingerprint"] for k in out["keys"]] == [ONE, TWO]


def test_bad_pin_is_refused_before_anything_runs(apt_host: Host) -> None:
    with pytest.raises(ValueError, match="full key fingerprint"):
        repos.add_repo("grafana", **APT, key_fingerprints=["D85AD90C"])


def test_http_key_needs_a_pin(apt_host: Host) -> None:
    apt_host.keys["http://apt.example.com/key.asc"] = KEY_ONE
    args = {**APT, "apt_key": "http://apt.example.com/key.asc"}
    with pytest.raises(ValueError, match="https"):
        repos.add_repo("grafana", **args)
    assert repos.add_repo("grafana", **args, key_fingerprints=[ONE])["changed"]


def test_key_from_a_file_on_the_host(apt_host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repos, "_fetch_key", REAL_FETCH)
    out = repos.add_repo("grafana", apt_url=APT["apt_url"], apt_key=str(DATA / "test-key-one.asc"))
    assert out["keys"][0]["fingerprint"] == ONE


def test_missing_key_file_on_the_host(apt_host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repos, "_fetch_key", REAL_FETCH)
    with pytest.raises(LinuxCoreError, match="can't read key file"):
        repos.add_repo("grafana", apt_url=APT["apt_url"], apt_key="/nonexistent/key.asc")


def test_a_non_key_download_is_refused(apt_host: Host) -> None:
    apt_host.keys[APT["apt_key"]] = b"<html>Not Found</html>"
    with pytest.raises(LinuxCoreError, match="isn't a usable public key"):
        repos.add_repo("grafana", **APT)
    assert not apt_host.path("etc/apt/sources.list.d/grafana.list").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"apt_url": "ftp://x"},
        {"apt_url": "https://x y"},
        {"apt_url": "https://x]"},
        {"apt_key": "key.asc"},
        {"apt_suite": "-o x"},
        {"apt_components": "main;rm"},
    ],
)
def test_bad_input_is_refused(apt_host: Host, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        repos.add_repo("grafana", **{**APT, **kwargs})


@pytest.mark.parametrize("name", ["", "-x", "../etc", "a b", "a/b", "x" * 65])
def test_bad_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError):
        repos.validate_name(name)


# ── dnf / yum / zypper ────────────────────────────────────────────────────────


def _repo_dir(host: Host) -> str:
    return "etc/zypp/repos.d" if host.mgr == "zypper" else "etc/yum.repos.d"


def test_rpm_add_writes_repo_file_and_imports_the_key(rpm_host: Host) -> None:
    out = repos.add_repo("grafana", **APT, **RPM)
    assert out["changed"] and out["refreshed"]
    assert out["rpm_keys_imported"] == ["gpg-pubkey-d85ad90c"]
    text = rpm_host.path(f"{_repo_dir(rpm_host)}/grafana.repo").read_text()
    assert text.startswith(repos.MARKER)
    for line in ("[grafana]", "baseurl=https://rpm.example.com", "gpgcheck=1", "repo_gpgcheck=1", "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-grafana"):
        assert line in text.splitlines()
    assert rpm_host.path("etc/pki/rpm-gpg/RPM-GPG-KEY-grafana").exists()


def test_rpm_refresh_command(rpm_host: Host) -> None:
    repos.add_repo("grafana", **RPM)
    cmds = [c for c in rpm_host.commands if c[0] != "rpm"]
    if rpm_host.mgr == "dnf":
        assert cmds == [["dnf", "-y", "--repo=grafana", "makecache", "--refresh"]]
    elif rpm_host.mgr == "yum":
        assert cmds == [
            ["yum", "-y", "--disablerepo=*", "--enablerepo=grafana", "clean", "expire-cache"],
            ["yum", "-y", "--disablerepo=*", "--enablerepo=grafana", "makecache"],
        ]
    else:
        assert cmds == [["zypper", "--non-interactive", "refresh", "grafana"]]


def test_rpm_unsigned_metadata(rpm_host: Host) -> None:
    repos.add_repo("grafana", **RPM, signed_metadata=False)
    assert "repo_gpgcheck=0" in rpm_host.path(f"{_repo_dir(rpm_host)}/grafana.repo").read_text().splitlines()


def test_rpm_needs_the_rpm_pair(rpm_host: Host) -> None:
    with pytest.raises(ValueError, match=f"this host uses {rpm_host.mgr}: give --rpm-url and --rpm-key"):
        repos.add_repo("grafana", **APT)


def test_rpm_imports_each_key_separately(rpm_host: Host) -> None:
    rpm_host.keys[RPM["rpm_key"]] = KEYS_TWO
    out = repos.add_repo("grafana", **RPM)
    assert out["rpm_keys_imported"] == ["gpg-pubkey-d85ad90c", "gpg-pubkey-4383de19"]
    assert [c[:2] for c in rpm_host.commands].count(["rpm", "--import"]) == 2


def test_rpm_key_already_imported_is_not_imported_again(rpm_host: Host) -> None:
    rpm_host.rpm_db.add("gpg-pubkey-d85ad90c")
    out = repos.add_repo("grafana", **RPM)
    assert "rpm_keys_imported" not in out
    assert not any(c[:2] == ["rpm", "--import"] for c in rpm_host.commands)


def test_rpm_refresh_failure_undoes_files_and_imported_keys(rpm_host: Host) -> None:
    rpm_host.refresh_result = _done(returncode=1, stderr="repomd.xml GPG signature verification error: Bad GPG signature")
    with pytest.raises(CommandError, match="Bad GPG signature"):
        repos.add_repo("grafana", **RPM)
    assert not rpm_host.path(f"{_repo_dir(rpm_host)}/grafana.repo").exists()
    assert not rpm_host.path("etc/pki/rpm-gpg/RPM-GPG-KEY-grafana").exists()
    assert rpm_host.rpm_db == set()


def test_dnf_forgets_its_cached_repo_keyring_when_the_key_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(tmp_path, monkeypatch, "dnf")
    repos.add_repo("grafana", **RPM)
    ours = host.path("var/cache/dnf/grafana-369737ffb0934a7b/pubring")
    other = host.path("var/cache/dnf/grafana-oss-0123456789abcdef/pubring")
    for p in (ours, other):
        p.mkdir(parents=True)
    host.keys[RPM["rpm_key"]] = KEYS_TWO
    repos.add_repo("grafana", **RPM)
    assert not ours.exists()
    assert other.exists()


def test_rpm_same_url_elsewhere_is_only_a_warning(rpm_host: Host) -> None:
    rpm_host.write(f"{_repo_dir(rpm_host)}/old.repo", "[old]\nbaseurl=https://rpm.example.com/\n")
    out = repos.add_repo("grafana", **RPM)
    assert out["warnings"] and "old.repo" in out["warnings"][0]


def test_pacman_is_not_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Host(tmp_path, monkeypatch, "pacman")
    with pytest.raises(LinuxCoreError, match="pacman"):
        repos.add_repo("grafana", **APT, **RPM)


# ── list ──────────────────────────────────────────────────────────────────────


def test_list_apt_reads_list_and_deb822_files(apt_host: Host) -> None:
    apt_host.write("etc/apt/sources.list", "# comment\ndeb http://archive.ubuntu.com/ubuntu noble main universe\n")
    apt_host.write(
        "etc/apt/sources.list.d/ubuntu.sources",
        "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu/\nSuites: noble noble-updates\nComponents: main\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\n"
        "Types: deb\nURIs: http://security.ubuntu.com/ubuntu/\nSuites: noble-security\nComponents: main\nEnabled: no\n",
    )
    repos.add_repo("grafana", **APT)
    out = repos.list_repos()
    assert out["manager"] == "apt-get" and out["count"] == 4
    by_url = {r["urls"][0]: r for r in out["repos"]}
    assert by_url["http://archive.ubuntu.com/ubuntu/"]["suites"] == ["noble", "noble-updates"]
    assert by_url["http://security.ubuntu.com/ubuntu/"]["enabled"] is False
    assert by_url["https://apt.example.com"]["managed"] is True
    assert by_url["https://apt.example.com"]["signed_by"] == "/etc/apt/keyrings/grafana.asc"
    assert [r["name"] for r in repos.list_repos("grafana")["repos"]] == ["grafana"]


def test_list_rpm(rpm_host: Host) -> None:
    rpm_host.write(f"{_repo_dir(rpm_host)}/rocky.repo", "[baseos]\nname=BaseOS\nmirrorlist=https://mirrors.example/\nenabled=1\ngpgcheck=1\n[extras]\nenabled=0\n")
    out = repos.list_repos()
    assert [(r["name"], r["id"], r["enabled"]) for r in out["repos"]] == [("rocky", "baseos", True), ("rocky", "extras", False)]
    assert out["repos"][0]["mirrorlist"] == "https://mirrors.example/"


# ── remove ────────────────────────────────────────────────────────────────────


def test_remove_deletes_source_and_key(apt_host: Host) -> None:
    repos.add_repo("grafana", **APT)
    out = repos.remove_repo("grafana")
    assert out["removed"] is True
    assert not apt_host.path("etc/apt/sources.list.d/grafana.list").exists()
    assert not apt_host.path("etc/apt/keyrings/grafana.asc").exists()


def test_remove_a_hand_made_repo_following_the_vendor_guide(apt_host: Host) -> None:
    key = apt_host.write("etc/apt/keyrings/grafana.asc", "key")
    apt_host.write("etc/apt/sources.list.d/grafana.list", "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main\n")
    repos.remove_repo("grafana")
    assert not key.exists()


def test_remove_keeps_a_key_another_repo_uses(apt_host: Host) -> None:
    key = apt_host.write("etc/apt/keyrings/shared.asc", "key")
    apt_host.write("etc/apt/sources.list.d/a.list", "deb [signed-by=/etc/apt/keyrings/shared.asc] https://a.example stable main\n")
    apt_host.write("etc/apt/sources.list.d/b.list", "deb [signed-by=/etc/apt/keyrings/shared.asc] https://b.example stable main\n")
    repos.remove_repo("a")
    assert key.exists()


def test_remove_rpm_keeps_the_rpm_database(rpm_host: Host) -> None:
    repos.add_repo("grafana", **RPM)
    out = repos.remove_repo("grafana")
    assert out["removed"] and "rpm database" in out["note"]
    assert not rpm_host.path("etc/pki/rpm-gpg/RPM-GPG-KEY-grafana").exists()
    assert rpm_host.rpm_db == {"gpg-pubkey-d85ad90c"}


def test_remove_missing_repo_is_not_an_error(apt_host: Host) -> None:
    assert repos.remove_repo("nothing") == {"manager": "apt-get", "name": "nothing", "removed": False, "files": []}
