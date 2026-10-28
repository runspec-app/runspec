"""Unit tests for the pure marketplace core (no network — the fetch is canned)."""

from __future__ import annotations

import io
import json
import shlex
import zipfile
from pathlib import Path

import pytest

from runspec_console import marketplace as mp

# --------------------------------------------------------------------------- #
# names / versions
# --------------------------------------------------------------------------- #


def test_normalize_name():
    assert mp.normalize_name("Runspec_Linux.Core") == "runspec-linux-core"
    assert mp.normalize_name("  runspec--fs ") == "runspec-fs"


def test_version_ordering_is_pep440():
    vs = [
        "1.0.dev1",
        "1.0a1",
        "1.0b2",
        "1.0rc1",
        "1.0",
        "1.0.post1",
        "1.0.1",
        "1.1",
        "2!0.1",
        "abc",
    ]
    assert sorted(vs, key=mp.version_key) == [
        "abc",
        "1.0.dev1",
        "1.0a1",
        "1.0b2",
        "1.0rc1",
        "1.0",
        "1.0.post1",
        "1.0.1",
        "1.1",
        "2!0.1",
    ]
    assert mp.compare_versions("1.0", "1.0.0") == 0
    assert mp.compare_versions("0.13.2", "0.13.10") < 0


def test_newest_version_prefers_finals():
    assert mp.newest_version(["1.0", "1.1rc1", "0.9"]) == "1.0"
    assert mp.newest_version(["1.0", "1.1rc1"], include_prerelease=True) == "1.1rc1"
    assert (
        mp.newest_version(["1.1rc1"]) == "1.1rc1"
    )  # only pre-releases → the best of them
    assert mp.newest_version([]) is None


def test_parse_filename():
    assert mp.parse_filename("runspec_linux-0.13.2-py3-none-any.whl") == (
        "runspec-linux",
        "0.13.2",
        "wheel",
    )
    assert mp.parse_filename("runspec_linux-0.13.2.tar.gz") == (
        "runspec-linux",
        "0.13.2",
        "sdist",
    )
    assert mp.parse_filename("something.egg") is None


def test_install_state():
    assert mp.install_state(None, "1.0") == "absent"
    assert mp.install_state("1.0", "1.0") == "current"
    assert mp.install_state("0.9", "1.0") == "outdated"
    assert mp.install_state("1.1", "1.0") == "newer"
    assert mp.install_state("1.0", None) == "unknown"


# --------------------------------------------------------------------------- #
# repos + pip config
# --------------------------------------------------------------------------- #


def test_simple_root_normalises_index_urls():
    assert mp.simple_root("https://pypi.org/simple") == "https://pypi.org/simple/"
    assert (
        mp.simple_root("https://nexus.corp/repository/pypi-all/simple/")
        == "https://nexus.corp/repository/pypi-all/simple/"
    )
    assert (
        mp.simple_root("nexus.corp/repository/pypi")
        == "https://nexus.corp/repository/pypi/simple/"
    )


def test_strip_and_restore_url_auth():
    clean, auth = mp.strip_url_auth("https://bob:s%40cret@nexus.corp:8443/simple/")
    assert clean == "https://nexus.corp:8443/simple/"
    assert auth == ("bob", "s@cret")
    assert (
        mp.with_url_auth(clean, auth) == "https://bob:s%40cret@nexus.corp:8443/simple/"
    )
    assert mp.strip_url_auth("https://pypi.org/simple/") == (
        "https://pypi.org/simple/",
        None,
    )


def test_repo_display_name():
    assert mp.repo_display_name("https://pypi.org/simple/") == "PyPI"
    assert (
        mp.repo_display_name("https://nexus.corp/repository/pypi-all/simple/")
        == "nexus.corp/repository/pypi-all"
    )


def test_pip_config_files_posix_and_windows(tmp_path):
    files = mp.pip_config_files(
        platform="linux", home=str(tmp_path), sys_prefix=str(tmp_path / "venv"), env={}
    )
    assert files[0] == Path("/etc/pip.conf")
    assert files[-1] == tmp_path / "venv" / "pip.conf"
    assert tmp_path / ".config" / "pip" / "pip.conf" in files
    win = mp.pip_config_files(
        platform="win32",
        home="C:\\Users\\me",
        sys_prefix="C:\\venvs\\ops",
        env={
            "APPDATA": "C:\\Users\\me\\AppData\\Roaming",
            "PROGRAMDATA": "C:\\ProgramData",
        },
    )
    assert win[-1] == Path("C:\\venvs\\ops") / "pip.ini"
    assert Path("C:\\Users\\me\\AppData\\Roaming") / "pip" / "pip.ini" in win
    # PIP_CONFIG_FILE replaces the chain.
    assert mp.pip_config_files(
        platform="linux", env={"PIP_CONFIG_FILE": "/x/pip.conf"}
    ) == [Path("/x/pip.conf")]


def test_read_pip_config_precedence_and_multiline_extra(tmp_path):
    glob_ = tmp_path / "global.conf"
    glob_.write_text(
        "[global]\nindex-url = https://a.corp/simple\ntrusted-host = a.corp\n"
    )
    user = tmp_path / "user.conf"
    user.write_text(
        "[global]\nindex-url = https://bob:pw@b.corp/simple/\n"
        "extra-index-url =\n    https://c.corp/simple\n    https://d.corp/simple\n"
        "[install]\ncert = /etc/ssl/corp.pem\n"
    )
    cfg = mp.read_pip_config([glob_, user, tmp_path / "missing.conf"], env={})
    assert cfg["index_url"] == "https://bob:pw@b.corp/simple/"
    assert cfg["extra_index_urls"] == ["https://c.corp/simple", "https://d.corp/simple"]
    assert cfg["trusted_hosts"] == ["a.corp"]
    assert cfg["cert"] == "/etc/ssl/corp.pem"
    assert cfg["sources"] == [str(glob_), str(user)]
    # env wins over every file
    cfg2 = mp.read_pip_config(
        [glob_, user], env={"PIP_INDEX_URL": "https://e.corp/simple"}
    )
    assert cfg2["index_url"] == "https://e.corp/simple"
    assert "$PIP_INDEX_URL" in cfg2["sources"]


def test_pip_config_repos_defaults_to_pypi_and_splits_auth():
    repos = mp.pip_config_repos(
        {"index_url": None, "extra_index_urls": [], "trusted_hosts": []}
    )
    assert [r.url for r in repos] == [mp.PYPI_SIMPLE_URL]
    assert (
        repos[0].is_pypi and repos[0].source == "pip" and repos[0].pip_role == "index"
    )
    assert repos[0].effective_pip_arg() == "none"

    repos = mp.pip_config_repos(
        {
            "index_url": "https://bob:pw@nexus.corp/repository/pypi-all/simple/",
            "extra_index_urls": [
                "https://pypi.org/simple",
                "https://nexus.corp/repository/pypi-all/simple",
            ],
            "trusted_hosts": ["nexus.corp"],
        }
    )
    assert [r.url for r in repos] == [
        "https://nexus.corp/repository/pypi-all/simple/",
        "https://pypi.org/simple/",
    ]
    nexus, pypi = repos
    assert nexus.auth == ("bob", "pw")
    assert nexus.trusted_host == "nexus.corp"
    assert nexus.effective_pip_arg() == "index-url"
    assert pypi.pip_role == "extra" and pypi.effective_pip_arg() == "none"
    assert nexus.to_public()["credential"] == "" and "auth" not in nexus.to_public()


def test_merge_repos_config_wins_but_inherits_pip_role_and_auth():
    cfg = mp.Repo(
        name="Nexus",
        url="https://nexus.corp/repository/pypi-all/simple",
        packages=["acme-tools"],
    )
    pip = mp.Repo(
        name="nexus.corp/…",
        url="https://nexus.corp/repository/pypi-all/simple/",
        source="pip",
        pip_role="extra",
        auth=("u", "p"),
    )
    other = mp.Repo(
        name="PyPI", url="https://pypi.org/simple/", source="pip", pip_role="index"
    )
    merged = mp.merge_repos([cfg], [pip, other])
    assert [r.name for r in merged] == ["Nexus", "PyPI"]
    assert (
        merged[0].pip_role == "extra"
        and merged[0].auth == ("u", "p")
        and merged[0].packages == ["acme-tools"]
    )
    assert merged[0].effective_pip_arg() == "extra-index-url"


def test_repo_from_dict_roundtrip_and_validation():
    raw = {
        "name": "Nexus",
        "url": "https://nexus.corp/simple",
        "packages": ["a", " ", "b"],
        "pip_arg": "bogus",
        "tags": ["internal"],
    }
    repo = mp.repo_from_dict(raw)
    assert repo is not None
    assert (
        repo.packages == ["a", "b"]
        and repo.pip_arg == "auto"
        and repo.tags == ["internal"]
    )
    assert mp.repo_to_dict(repo) == {
        "name": "Nexus",
        "url": "https://nexus.corp/simple",
        "enabled": True,
        "scan": True,
        "packages": ["a", "b"],
        "tags": ["internal"],
    }
    assert mp.repo_from_dict({"name": "x"}) is None
    assert mp.validate_repo({"url": ""}) is not None
    assert mp.validate_repo({"url": "ftp://x"}) is not None
    assert mp.validate_repo({"url": "https://ok/simple", "pip_arg": "nope"}) is not None
    assert mp.validate_repo({"url": "https://ok/simple"}) is None


# --------------------------------------------------------------------------- #
# metadata + runnable detection
# --------------------------------------------------------------------------- #

METADATA = """Metadata-Version: 2.3
Name: runspec-linux
Version: 0.13.2
Summary: Linux system admin runnables for runspec
Keywords: runspec,linux,ops
Project-URL: Documentation, https://runspec.dev/linux
Project-URL: Source, https://github.com/x/runspec
Requires-Python: >=3.10
Requires-Dist: runspec>=0.50.1
Requires-Dist: runspec-linux-core>=0.6.2
Requires-Dist: pytest>=8.0; extra == "dev"
Description-Content-Type: text/markdown

# runspec-linux

Thirty-odd Linux runnables.
"""


def test_parse_core_metadata():
    meta = mp.parse_core_metadata(METADATA)
    assert meta["name"] == "runspec-linux" and meta["version"] == "0.13.2"
    assert meta["requires_dist"] == [
        "runspec>=0.50.1",
        "runspec-linux-core>=0.6.2",
        'pytest>=8.0; extra == "dev"',
    ]
    assert meta["project_url"] == [
        "Documentation, https://runspec.dev/linux",
        "Source, https://github.com/x/runspec",
    ]
    assert meta["description"].startswith("# runspec-linux")
    assert mp.project_links(meta) == {
        "documentation": "https://runspec.dev/linux",
        "source": "https://github.com/x/runspec",
    }
    assert mp.keywords_of(meta) == ["runspec", "linux", "ops"]


def test_is_runnable_rules():
    assert mp.is_runnable(mp.parse_core_metadata(METADATA))
    core = {"name": "runspec-linux-core", "requires_dist": ["httpx"]}
    assert not mp.is_runnable(core)
    extra_only = {"name": "acme", "requires_dist": ['runspec; extra == "cli"']}
    assert not mp.is_runnable(extra_only)
    keyword = {
        "name": "acme-tools",
        "requires_dist": ["acme-base"],
        "keywords": "ops, runspec-runnable",
    }
    assert mp.is_runnable(keyword)
    console = {"name": "runspec-console", "requires_dist": ["runspec>=0.42"]}
    assert not mp.is_runnable(console)
    assert mp.is_runnable({"name": "acme", "has_runspec_toml": True})
    assert not mp.is_runnable({"name": "runspec-console", "has_runspec_toml": True})


def test_is_hidden_globs_and_normalisation():
    assert mp.is_hidden("runspec-jira", ["runspec-jira"])
    assert mp.is_hidden("runspec_jira", ["runspec-jira"])  # name normalisation
    assert mp.is_hidden("RunSpec-Jira", ["runspec-jira"])  # case-insensitive
    assert mp.is_hidden("runspec-jira", ["runspec-*"])  # glob
    assert not mp.is_hidden("runspec-linux", ["runspec-jira", "runspec-snow"])
    assert not mp.is_hidden("runspec-jira", [])
    assert not mp.is_hidden("", ["runspec-*"])


def test_project_links_from_pypi_json_dict_and_homepage():
    meta = {
        "project_urls": {
            "Homepage": "https://h",
            "Changelog": "https://c",
            "Bug Tracker": "https://b",
            "Docs": "https://d",
        }
    }
    assert mp.project_links(meta) == {
        "homepage": "https://h",
        "changelog": "https://c",
        "issues": "https://b",
        "documentation": "https://d",
    }
    assert mp.project_links({"home_page": "https://hp"}) == {"homepage": "https://hp"}


# --------------------------------------------------------------------------- #
# simple index parsing
# --------------------------------------------------------------------------- #


def test_scan_project_names_json_and_html():
    body = json.dumps(
        {
            "meta": {},
            "projects": [
                {"name": "runspec-linux"},
                {"name": "Runspec_Fs"},
                {"name": "requests"},
                {"name": "runspecimen"},
            ],
        }
    ).encode()
    assert mp.scan_project_names(
        body, "application/vnd.pypi.simple.v1+json", ["runspec-*"]
    ) == ["runspec-linux", "runspec-fs"]
    html = b'<html><body><a href="/simple/runspec-linux/">runspec-linux</a>\n<a href="/simple/requests/">requests</a><a href="x">runspec-fs</a></body></html>'
    assert mp.scan_project_names(html, "text/html", ["runspec-*"]) == [
        "runspec-linux",
        "runspec-fs",
    ]


def test_parse_project_page_json_picks_latest_wheel_with_metadata():
    page = {
        "name": "runspec-linux",
        "versions": ["0.13.1", "0.13.2", "0.14.0rc1"],
        "files": [
            {
                "filename": "runspec_linux-0.13.1-py3-none-any.whl",
                "url": "runspec_linux-0.13.1-py3-none-any.whl",
                "core-metadata": {"sha256": "x"},
            },
            {
                "filename": "runspec_linux-0.13.2.tar.gz",
                "url": "runspec_linux-0.13.2.tar.gz",
                "core-metadata": False,
            },
            {
                "filename": "runspec_linux-0.13.2-py3-none-any.whl",
                "url": "../../files/runspec_linux-0.13.2-py3-none-any.whl",
                "core-metadata": True,
                "size": 1234,
                "upload-time": "2026-09-17T23:06:09Z",
            },
            {
                "filename": "runspec_linux-0.14.0rc1-py3-none-any.whl",
                "url": "runspec_linux-0.14.0rc1-py3-none-any.whl",
                "yanked": "broken",
            },
        ],
    }
    parsed = mp.parse_project_page(
        json.dumps(page).encode(),
        "application/vnd.pypi.simple.v1+json",
        "https://idx/simple/runspec-linux/",
    )
    latest, f = mp.pick_latest(parsed["files"], parsed["versions"])
    assert latest == "0.13.2" and f is not None
    assert (
        f.kind == "wheel"
        and f.url == "https://idx/files/runspec_linux-0.13.2-py3-none-any.whl"
    )
    assert f.metadata_url == f.url + ".metadata" and f.size == 1234
    assert [x.yanked for x in parsed["files"]][-1] is True


def test_parse_project_page_html():
    html = (
        b'<a href="../../pkgs/acme_tools-1.0.0-py3-none-any.whl#sha256=abc" data-core-metadata="sha256=def" data-requires-python="&gt;=3.10">acme_tools-1.0.0-py3-none-any.whl</a>'
        b'<a href="../../pkgs/acme_tools-1.1.0.tar.gz" data-yanked="">acme_tools-1.1.0.tar.gz</a>'
    )
    parsed = mp.parse_project_page(html, "text/html", "https://idx/simple/acme-tools/")
    latest, f = mp.pick_latest(parsed["files"], parsed["versions"])
    assert latest == "1.0.0" and f is not None
    assert f.url == "https://idx/pkgs/acme_tools-1.0.0-py3-none-any.whl"
    assert f.metadata_url == f.url + ".metadata"
    assert f.requires_python == ">=3.10"


def _wheel_bytes(meta: str, *, with_toml: bool) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("acme_tools/__init__.py", "")
        if with_toml:
            zf.writestr("acme_tools/runspec.toml", "[hello]\n")
        zf.writestr("acme_tools-1.0.0.dist-info/METADATA", meta)
    return buf.getvalue()


def test_read_wheel_metadata_detects_runspec_toml():
    meta = mp.read_wheel_metadata(
        _wheel_bytes(
            "Metadata-Version: 2.1\nName: acme-tools\nVersion: 1.0.0\n", with_toml=True
        )
    )
    assert meta["name"] == "acme-tools" and meta["has_runspec_toml"] is True
    meta2 = mp.read_wheel_metadata(
        _wheel_bytes(
            "Metadata-Version: 2.1\nName: acme-tools\nVersion: 1.0.0\n", with_toml=False
        )
    )
    assert meta2["has_runspec_toml"] is False


# --------------------------------------------------------------------------- #
# IndexClient tiers with a canned fetch
# --------------------------------------------------------------------------- #


class CannedFetch:
    def __init__(self, routes: dict[str, tuple[int, str, bytes]]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, *, headers=None, max_bytes=0, auth=None):
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        return 404, "", b""


def _simple_json(name: str, files: list[dict]) -> bytes:
    return json.dumps(
        {
            "name": name,
            "versions": sorted({f["filename"].split("-")[1] for f in files}),
            "files": files,
        }
    ).encode()


def test_client_pypi_json_tier():
    routes = {
        "https://pypi.org/simple/": (
            200,
            "application/vnd.pypi.simple.v1+json",
            json.dumps(
                {
                    "projects": [
                        {"name": "runspec-linux"},
                        {"name": "runspec-linux-core"},
                        {"name": "boto3"},
                    ]
                }
            ).encode(),
        ),
        "https://pypi.org/pypi/runspec-linux/json": (
            200,
            "application/json",
            json.dumps(
                {
                    "info": {
                        "name": "runspec-linux",
                        "version": "0.13.2",
                        "summary": "Linux runnables",
                        "description": "# hi",
                        "keywords": "runspec,linux",
                        "requires_dist": ["runspec>=0.50.1"],
                        "project_urls": {"Documentation": "https://d"},
                    },
                    "urls": [{"upload_time_iso_8601": "2026-09-17T23:06:09.176576Z"}],
                    "releases": {"0.13.1": [], "0.13.2": []},
                }
            ).encode(),
        ),
        "https://pypi.org/simple/runspec-linux/": (
            200,
            "application/vnd.pypi.simple.v1+json",
            _simple_json(
                "runspec-linux",
                [
                    {
                        "filename": "runspec_linux-0.13.2-py3-none-any.whl",
                        "url": "w.whl",
                        "core-metadata": False,
                    },
                ],
            ),
        ),
        "https://pypi.org/pypi/runspec-linux-core/json": (
            200,
            "application/json",
            json.dumps(
                {
                    "info": {
                        "name": "runspec-linux-core",
                        "version": "0.6.2",
                        "summary": "core",
                        "requires_dist": [],
                    },
                    "urls": [],
                    "releases": {"0.6.2": []},
                }
            ).encode(),
        ),
        "https://pypi.org/simple/runspec-linux-core/": (
            200,
            "application/vnd.pypi.simple.v1+json",
            _simple_json(
                "runspec-linux-core",
                [
                    {
                        "filename": "runspec_linux_core-0.6.2-py3-none-any.whl",
                        "url": "c.whl",
                    },
                ],
            ),
        ),
    }
    fetch = CannedFetch(routes)
    client = mp.IndexClient(fetch)
    repo = mp.Repo(
        name="PyPI", url="https://pypi.org/simple/", source="pip", pip_role="index"
    )
    assert client.candidate_names(repo) == ["runspec-linux", "runspec-linux-core"]
    entries, errors = mp.build_catalog(client, repo)
    assert errors == []
    assert [e["name"] for e in entries] == ["runspec-linux"]
    e = entries[0]
    assert e["version"] == "0.13.2" and e["summary"] == "Linux runnables"
    assert e["links"] == {"documentation": "https://d"} and e["keywords"] == [
        "runspec",
        "linux",
    ]
    assert e["metadata_source"] == "pypi-json" and e["upload_time"].startswith(
        "2026-09-17"
    )
    assert e["versions"] == ["0.13.2", "0.13.1"] and e["repo"] == "pypi"


def test_client_pep658_then_wheel_tiers_on_private_index():
    wheel = _wheel_bytes(
        "Metadata-Version: 2.1\nName: acme-tools\nVersion: 1.0.0\nSummary: Acme ops\nRequires-Dist: runspec\n",
        with_toml=True,
    )
    routes = {
        "https://nexus.corp/simple/": (
            200,
            "text/html",
            b'<a href="acme-tools/">acme-tools</a><a href="runspec-x/">runspec-x</a>',
        ),
        "https://nexus.corp/simple/acme-tools/": (
            200,
            "text/html",
            b'<a href="../../pkgs/acme_tools-1.0.0-py3-none-any.whl">acme_tools-1.0.0-py3-none-any.whl</a>',
        ),
        "https://nexus.corp/pkgs/acme_tools-1.0.0-py3-none-any.whl": (
            200,
            "application/octet-stream",
            wheel,
        ),
        "https://nexus.corp/simple/runspec-x/": (
            200,
            "text/html",
            b'<a href="../../pkgs/runspec_x-2.0-py3-none-any.whl" data-core-metadata="true">runspec_x-2.0-py3-none-any.whl</a>',
        ),
        "https://nexus.corp/pkgs/runspec_x-2.0-py3-none-any.whl.metadata": (
            200,
            "text/plain",
            b"Metadata-Version: 2.1\nName: runspec-x\nVersion: 2.0\nSummary: X\nRequires-Dist: runspec>=0.4\n",
        ),
    }
    fetch = CannedFetch(routes)
    client = mp.IndexClient(fetch)
    repo = mp.Repo(
        name="Nexus",
        url="https://nexus.corp/simple/",
        packages=["acme-tools"],
        auth=("u", "p"),
    )
    entries, errors = mp.build_catalog(client, repo)
    assert errors == []
    by = {e["name"]: e for e in entries}
    assert (
        by["acme-tools"]["metadata_source"] == "wheel"
        and by["acme-tools"]["has_runspec_toml"] is True
    )
    assert (
        by["runspec-x"]["metadata_source"] == "pep658"
        and by["runspec-x"]["version"] == "2.0"
    )
    # The pypi JSON API was never tried on a non-pypi host.
    assert not any("/pypi/" in c for c in fetch.calls)


def test_build_catalog_reports_explicit_non_runnable_and_missing():
    routes = {
        "https://nexus.corp/simple/plain/": (
            200,
            "text/html",
            b'<a href="../../p/plain-1.0-py3-none-any.whl" data-core-metadata="true">plain-1.0-py3-none-any.whl</a>',
        ),
        "https://nexus.corp/p/plain-1.0-py3-none-any.whl.metadata": (
            200,
            "text/plain",
            b"Metadata-Version: 2.1\nName: plain\nVersion: 1.0\n",
        ),
    }
    client = mp.IndexClient(CannedFetch(routes))
    repo = mp.Repo(
        name="Nexus",
        url="https://nexus.corp/simple/",
        scan=False,
        packages=["plain", "ghost"],
    )
    entries, errors = mp.build_catalog(client, repo)
    assert entries == []
    assert any(
        e.startswith("plain:") and "does not depend on runspec" in e for e in errors
    )
    assert any(e.startswith("ghost:") and "not in Nexus" in e for e in errors)
    entries2, _ = mp.build_catalog(client, repo, require_runspec=False)
    assert [e["name"] for e in entries2] == ["plain"]


def test_build_catalog_skips_scanned_empty_project_but_warns_when_explicit():
    """A *scanned* name that resolves to a registered-but-empty project (the
    squatted/typo case on public PyPI — e.g. `runspec-gtilab-core`, a misspelling
    of `runspec-gitlab-core`, registered with no releases) is silently skipped:
    it matches the runspec-* scan and reads back empty, but it's discovery noise,
    not something the operator can act on. The *same* name, when the operator
    lists it in `packages`, still warns (a typo there is worth flagging) — with no
    double-prefixed name."""
    routes = {
        "https://pypi.org/simple/": (
            200,
            "text/html",
            b'<a href="runspec-good/">runspec-good</a>'
            b'<a href="runspec-gtilab-core/">runspec-gtilab-core</a>',
        ),
        "https://pypi.org/simple/runspec-good/": (
            200,
            "text/html",
            b'<a href="../../p/runspec_good-1.0-py3-none-any.whl" data-core-metadata="true">runspec_good-1.0-py3-none-any.whl</a>',
        ),
        "https://pypi.org/p/runspec_good-1.0-py3-none-any.whl.metadata": (
            200,
            "text/plain",
            b"Metadata-Version: 2.1\nName: runspec-good\nVersion: 1.0\nRequires-Dist: runspec\n",
        ),
        # Registered but no release files — a 200 project page with no wheels.
        "https://pypi.org/simple/runspec-gtilab-core/": (200, "text/html", b""),
    }
    client = mp.IndexClient(CannedFetch(routes))
    repo = mp.Repo(name="PyPI", url="https://pypi.org/simple/", source="pip")
    entries, errors = mp.build_catalog(client, repo)
    assert [e["name"] for e in entries] == ["runspec-good"]
    assert errors == []  # the empty scanned project is skipped, not warned

    # Named explicitly → warned (single-prefixed, no "name: name: …" doubling).
    repo2 = mp.Repo(
        name="PyPI",
        url="https://pypi.org/simple/",
        scan=False,
        packages=["runspec-gtilab-core"],
    )
    _, errors2 = mp.build_catalog(client, repo2)
    assert errors2 == ["runspec-gtilab-core: no releases in PyPI"]


def test_client_root_error_surfaces():
    client = mp.IndexClient(CannedFetch({}))
    with pytest.raises(mp.MarketplaceError):
        client.scan_names(mp.Repo(name="x", url="https://nowhere/simple/"))


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


def test_catalog_cache_ttl_and_clear(tmp_path):
    cache = mp.CatalogCache(tmp_path / "mp.json", ttl_s=100)
    assert cache.get("https://pypi.org/simple") is None
    cache.put("https://pypi.org/simple", [{"name": "a"}], ["oops"], now=1000)
    assert cache.get("https://pypi.org/simple/", now=1050)["entries"] == [{"name": "a"}]
    assert cache.get("https://pypi.org/simple/", now=1200) is None
    assert cache.get_stale("https://pypi.org/simple/")["errors"] == ["oops"]
    fresh = mp.CatalogCache(tmp_path / "mp.json", ttl_s=100)
    assert fresh.get_stale("https://pypi.org/simple/") is not None
    fresh.clear()
    assert not (tmp_path / "mp.json").exists()


# --------------------------------------------------------------------------- #
# targets + probe + pip commands
# --------------------------------------------------------------------------- #


def test_venv_python_from_runspec_binary_paths():
    assert mp.venv_python("/opt/venvs/ops/bin/runspec") == "/opt/venvs/ops/bin/python"
    assert mp.venv_python("/opt/venvs/ops") == "/opt/venvs/ops/bin/python"
    assert (
        mp.venv_python("C:\\venvs\\ops\\Scripts\\runspec.exe")
        == "C:\\venvs\\ops\\Scripts\\python.exe"
    )
    assert mp.venv_python("C:\\venvs\\ops") == "C:\\venvs\\ops\\Scripts\\python.exe"
    assert mp.venv_root("/opt/venvs/ops/bin/runspec") == "/opt/venvs/ops"
    assert mp.venv_root("C:\\venvs\\ops\\Scripts\\runspec.exe") == "C:\\venvs\\ops"


def test_probe_snippet_runs_and_parses():
    import subprocess
    import sys

    out = subprocess.run(
        mp.probe_argv(sys.executable), capture_output=True, text=True, timeout=60
    ).stdout
    data = mp.parse_probe_output("noise\n" + out)
    assert data is not None and "pip" in data["dists"] or data is not None
    assert data["prefix"]
    assert mp.parse_probe_output("nothing here") is None


def test_pip_argv_builders_refuse_non_package_specs():
    argv = mp.pip_install_argv(
        "/v/bin/python",
        ["runspec-linux", "acme[extra]==1.0"],
        upgrade=True,
        reinstall=True,
    )
    assert argv[:4] == ["/v/bin/python", "-m", "pip", "install"]
    assert (
        "-U" in argv
        and "--force-reinstall" in argv
        and argv[-2:] == ["runspec-linux", "acme[extra]==1.0"]
    )
    assert mp.pip_uninstall_argv("/v/bin/python", ["runspec-linux"])[-2:] == [
        "--disable-pip-version-check",
        "runspec-linux",
    ]
    for bad in ["--index-url", "-e .", "https://evil/x.whl", "./local", "a b"]:
        with pytest.raises(mp.MarketplaceError):
            mp.pip_install_argv("/v/bin/python", [bad])
    with pytest.raises(mp.MarketplaceError):
        mp.pip_install_argv("/v/bin/python", [])


def test_pip_env_by_repo_role():
    pypi = mp.Repo(
        name="PyPI", url="https://pypi.org/simple/", source="pip", pip_role="index"
    )
    assert mp.pip_env(pypi) == {}
    nexus = mp.Repo(
        name="Nexus",
        url="https://nexus.corp/simple",
        source="pip",
        pip_role="index",
        trusted_host="nexus.corp",
    )
    assert mp.pip_env(nexus, ("bob", "p@ss")) == {
        "PIP_INDEX_URL": "https://bob:p%40ss@nexus.corp/simple/",
        "PIP_TRUSTED_HOST": "nexus.corp",
    }
    extra = mp.Repo(
        name="Extra", url="https://extra.corp/simple", pip_arg="extra-index-url"
    )
    assert mp.pip_env(extra) == {"PIP_EXTRA_INDEX_URL": "https://extra.corp/simple/"}
    none = mp.Repo(name="Own", url="https://own.corp/simple", pip_arg="none")
    assert mp.pip_env(none) == {}
    assert mp.pip_env(None) == {}


def test_remote_command_shapes():
    from runspec.become import PY_ENV_PRELUDE, parse_env_frame

    argv = ["/opt/v/bin/python", "-m", "pip", "install", "runspec-linux"]
    assert mp.remote_command(argv) == (
        "/opt/v/bin/python -m pip install runspec-linux",
        None,
    )
    assert mp.remote_command(argv, run_as="svc") == (
        "sudo -n -u svc /opt/v/bin/python -m pip install runspec-linux",
        None,
    )
    # With env (the index URL can embed a credential): the SPEC env prelude, the
    # values on stdin — never in the command line. The command is the venv's
    # python, so the prelude runs on it rather than sh (a shells-refused sudoers
    # rule still allows it).
    url = "https://u:p@x/simple/"
    cmd, frame = mp.remote_command(argv, env={"PIP_INDEX_URL": url})
    assert shlex.split(cmd) == ["/opt/v/bin/python", "-c", PY_ENV_PRELUDE, *argv]
    assert "u:p@" not in cmd and parse_env_frame(frame) == {"PIP_INDEX_URL": url}
    cmd, frame = mp.remote_command(argv, run_as="svc", env={"A": "b c"})
    assert shlex.split(cmd) == [
        "sudo",
        "-n",
        "-u",
        "svc",
        "/opt/v/bin/python",
        "-c",
        PY_ENV_PRELUDE,
        *argv,
    ]
    assert parse_env_frame(frame) == {"A": "b c"}
    su, frame = mp.remote_command(
        argv, run_as="svc", become_method="su", env={"A": "b"}
    )
    assert su.startswith("su -c ") and su.endswith(" svc") and "A=b" not in su
    assert parse_env_frame(frame) == {"A": "b"}


def test_summarize_pip_output_keeps_the_verdict_lines():
    out = "Collecting runspec-linux\n  Downloading x.whl\nRequirement already satisfied: runspec\nInstalling collected packages: runspec-linux\nSuccessfully installed runspec-linux-0.13.2\n"
    assert (
        mp.summarize_pip_output(out)
        == "Installing collected packages: runspec-linux\nSuccessfully installed runspec-linux-0.13.2"
    )


def test_outdated_packages_compares_normalised_names_and_skips_failed_probes():
    entries = [
        {"name": "runspec-acme", "version": "1.2.0"},
        {"name": "runspec-beta", "version": "2.0"},
        {"name": "runspec-gone", "version": ""},
    ]
    targets = [
        {
            "id": "a",
            "host": "web-01",
            "venv": "ops",
            "ok": True,
            "dists": {"runspec_acme": "1.1.0", "runspec-beta": "2.0"},
        },
        {
            "id": "b",
            "host": "web-02",
            "venv": "ops",
            "ok": True,
            "dists": {"runspec-beta": "2.1"},
        },
        {
            "id": "c",
            "host": "db-01",
            "venv": "db",
            "ok": False,
            "dists": {"runspec-acme": "0.1"},
        },
    ]
    out = mp.outdated_packages(entries, targets)
    assert out == [
        {
            "name": "runspec-acme",
            "latest": "1.2.0",
            "targets": [
                {"id": "a", "host": "web-01", "venv": "ops", "installed": "1.1.0"}
            ],
        }
    ]
